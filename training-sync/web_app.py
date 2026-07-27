"""FastAPI backend for the training calendar.

Serves the self-contained front-end in ``static/`` and the ``/api`` surface
the calendar talks to.

Two rules shape every handler in this file:

1. **TrainingPeaks is read-only.** The only thing that ever touches
   trainingpeaks.com is the plain ``GET`` of the personal ICS feed already
   implemented in ``trainingpeaks_calendar_feed.py``. There is no endpoint
   here that writes, re-uploads, or authenticates against TrainingPeaks, and
   nothing scrapes it or drives a browser at it.
2. **Garmin Connect is the only write target**, via the authenticated
   ``python-garminconnect`` session in ``garmin_workout_writer.py``.

And one operational rule: **the app must start and render with an empty
``.env``.** Every project module is imported lazily inside the handler that
needs it, so a module mid-edit or a missing credential degrades to a
``sources`` block saying so — never a 500 and never a failed startup.

Run it:

    python web_app.py                # real data from .env
    python web_app.py --demo         # sample plan, no credentials needed
"""

import argparse
import copy
import json
import os
import re
import sys
import threading
import traceback
import uuid
from datetime import date as _date
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is in requirements
    pass


APP_VERSION = "1.0.0"

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
STATE_DIR = os.path.join(HERE, "state")
PLAN_CACHE_PATH = os.path.join(STATE_DIR, "plan_cache.json")
PUSH_LOG_PATH = os.path.join(STATE_DIR, "push_log.json")
DEMO_STATE_PATH = os.path.join(STATE_DIR, "demo_state.json")

if HERE not in sys.path:
    sys.path.insert(0, HERE)

MAX_RANGE_DAYS = 120
PUSH_LOG_LIMIT = 50
GARMIN_RETRY_SECONDS = 60          # login is slow and rate-limited
GARMIN_CONTEXT_TTL = 300
PLAN_CACHE_TTL = 900
PLAN_STALE_AFTER = 6 * 3600

AVAILABILITY_STATES = ("available", "unavailable", "limited")
PROPOSAL_REASONS = ("race_recovery", "missed_workouts", "availability_changed",
                    "readiness_low", "manual")
PUSH_SELECT_MODES = ("push_approved", "explicit", "all_effective")

# scheduler kinds -> the kinds the calendar UI knows how to draw. One UI kind
# per engine kind: a KEEP drawn as "SKIP" tells the athlete the opposite of the
# sentence printed directly underneath it.
_KIND_MAP = {
    "MOVE": "move", "SWAP": "swap", "MERGE": "merge", "SHORTEN": "shorten",
    "DOWNGRADE": "downgrade", "DROP": "skip", "KEEP": "keep",
    "NO_PROPOSAL": "no_proposal",
}
# The engine's coarse action vocabulary (scheduler.KIND_TO_ACTION), used when a
# proposal arrives without an explicit `action`.
_ACTION_MAP = {
    "MOVE": "move", "SWAP": "move", "MERGE": "move", "SHORTEN": "soften",
    "DOWNGRADE": "soften", "DROP": "drop", "KEEP": "keep",
    "NO_PROPOSAL": "keep",
}
_ACTIONS = ("move", "soften", "drop", "keep")
_CONFIDENCE_MAP = {"NONE": 0.0, "LOW": 0.35, "MED": 0.65, "HIGH": 0.9}

# Anything Alfred writes into a workout's own text is signed, so the athlete
# can tell his coach's words from Alfred's — and so re-applying can't double it.
ALFRED_MARK = "[Alfred]"


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """An error in the spec's envelope. `message` is shown verbatim in the UI."""

    def __init__(self, code, message, status=400, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}

    def body(self):
        return {"error": {"code": self.code, "message": self.message,
                          "detail": self.detail}}


_SECRET_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def redact(text):
    """The feed URL is a secret bearer token — anyone holding it can read the
    whole schedule. It must never reach a log line, an error body or a
    warning string, so every URL in surfaced text is reduced to its host."""
    if text is None:
        return None

    def _mask(match):
        url = match.group(0)
        host = url.split("//", 1)[-1].split("/", 1)[0]
        return f"https://{host}/…"

    out = _SECRET_RE.sub(_mask, str(text))
    for var in ("TRAININGPEAKS_CALENDAR_FEED_URL", "GARMIN_PASSWORD",
                "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN",
                "TRAININGPEAKS_CLIENT_SECRET", "TRAININGPEAKS_REFRESH_TOKEN"):
        value = os.environ.get(var)
        if value and len(value) > 4 and value in out:
            out = out.replace(value, "…redacted…")
    return out


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:22]}"


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(value, label="date"):
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ApiError("invalid_range", f"{label} must look like 2026-07-27.",
                       status=400, detail={"field": label, "value": value})
    try:
        return _date.fromisoformat(value)
    except ValueError:
        raise ApiError("invalid_range", f"{label} is not a real date.",
                       status=400, detail={"field": label, "value": value})


def daterange(start, end):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


_TZ_LOCK = threading.Lock()
_TZ_CACHE = {}


def _zone(name):
    """A named zone (with its DST rules), or None. Never a fixed offset."""
    if not name or ZoneInfo is None:
        return None
    name = str(name).strip().lstrip(":")
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def _system_zone():
    """The machine's zone as a *zone*, not as the offset in force today.

    `datetime.now().astimezone().tzinfo` looks like the local timezone but is
    only ever a `timezone(timedelta(...))` snapshot of *this instant's* offset.
    Applied to a date on the other side of a DST boundary it is wrong by an
    hour, which lands a late-evening workout on the wrong day.
    """
    try:
        with open("/etc/timezone", "r", encoding="utf-8") as handle:
            zone = _zone(handle.read().strip())
            if zone is not None:
                return zone
    except OSError:
        pass
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo" in link:
            zone = _zone(link.split("zoneinfo", 1)[1].lstrip("/"))
            if zone is not None:
                return zone
    except OSError:
        pass
    if ZoneInfo is not None:
        # A TZif file still carries the full transition table, so this keeps
        # DST even when the zone has no name to look up.
        try:
            with open("/etc/localtime", "rb") as handle:
                return ZoneInfo.from_file(handle, key="local")
        except Exception:
            pass
    return None


def training_tz():
    """The zone the athlete's calendar is written in.

    `TRAINING_TZ` wins, then `TZ`, then the system zone. Every one of those is
    resolved to a real zone so `astimezone()` picks the offset that applied on
    the date being converted — a June evening keeps BST when it's read in
    January.
    """
    key = (os.environ.get("TRAINING_TZ"), os.environ.get("TZ"))
    with _TZ_LOCK:
        cached = _TZ_CACHE.get(key)
    if cached is not None:
        return cached
    zone = _zone(key[0]) or _zone(key[1]) or _system_zone()
    if zone is None:
        # Nothing on this machine names a zone (no tzdata, no /etc/localtime).
        # Today's offset is all that's left; `tz_name()` says so out loud.
        zone = datetime.now().astimezone().tzinfo or timezone.utc
    with _TZ_LOCK:
        _TZ_CACHE[key] = zone
    return zone


def tz_name():
    tz = training_tz()
    key = getattr(tz, "key", None)
    if key:
        return key
    return f"{tz} (fixed offset — set TRAINING_TZ)"


def today_local():
    return datetime.now(training_tz()).date()


def normalize_plan_date(raw, tz=None):
    """`planned_workouts()` returns `start.dt.isoformat()`, which is a bare
    date for all-day VEVENTs but a full timestamp for timed ones. Split it
    into the two fields the rest of the app uses, without touching the feed
    module's own return shape (its CLI and Alfred depend on it)."""
    if not raw:
        return None, None
    text = str(raw)
    if len(text) == 10:
        return text, None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:10] if _DATE_RE.match(text[:10]) else None, None
    if moment.tzinfo is None:
        return moment.date().isoformat(), moment.strftime("%H:%M")
    local = moment.astimezone(tz or training_tz())
    return local.date().isoformat(), local.strftime("%H:%M")


def _atomic_write_json(path, payload):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------------------
# lazy module loading
#
# Nothing in this file imports a project module at import time. A module
# being rewritten, or a dependency missing, must degrade to a "not
# configured" source — it must never stop the app from starting.
# ---------------------------------------------------------------------------


_MODULE_CACHE = {}
_MODULE_LOCK = threading.Lock()


def load_module(name, refresh=False):
    """Import a sibling module, returning (module, error_message)."""
    with _MODULE_LOCK:
        if refresh:
            _MODULE_CACHE.pop(name, None)
            sys.modules.pop(name, None)
        if name in _MODULE_CACHE:
            return _MODULE_CACHE[name]
        try:
            module = __import__(name)
            result = (module, None)
        except Exception as exc:
            result = (None, redact(f"{type(exc).__name__}: {exc}"))
        _MODULE_CACHE[name] = result
        return result


# ---------------------------------------------------------------------------
# source state
# ---------------------------------------------------------------------------


def _source_state(configured, ok, last_success=None, stale=True, message=None):
    return {
        "configured": bool(configured),
        "ok": bool(ok),
        "last_success": last_success,
        "stale": bool(stale),
        "message": redact(message),
    }


NOT_CONFIGURED_MESSAGES = {
    "trainingpeaks_ics": (
        "TrainingPeaks calendar feed not set. Generate your personal feed URL "
        "in TrainingPeaks settings and add TRAININGPEAKS_CALENDAR_FEED_URL to "
        "training-sync/.env."),
    "garmin": (
        "Garmin login not set. Add GARMIN_EMAIL and GARMIN_PASSWORD to "
        "training-sync/.env."),
    "strava": (
        "Strava not set. Add STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET and "
        "STRAVA_REFRESH_TOKEN to training-sync/.env."),
}


# ---------------------------------------------------------------------------
# the backend service
# ---------------------------------------------------------------------------


class TrainingService:
    """Holds every piece of mutable process state: the plan cache, the
    Garmin session, the push jobs. One instance per app."""

    def __init__(self, demo=False):
        self.demo = demo
        self._lock = threading.RLock()

        self._store = None
        self._store_error = None

        self._garmin_client = None
        self._garmin_error = None
        self._garmin_last_attempt = 0.0
        self._garmin_last_success = None

        self._writer = None

        self._plan_cache = None
        self._plan_fetched_monotonic = 0.0
        self._plan_error = None
        self._plan_last_success = None

        self._garmin_context = None
        self._garmin_context_at = 0.0

        self._jobs = {}
        self._job_order = []
        self._active_job = None
        self._load_push_log()

    # -- store ------------------------------------------------------------

    @property
    def store(self):
        with self._lock:
            if self._store is not None:
                return self._store
            module, error = load_module("plan_store")
            if module is None:
                self._store_error = error
                raise ApiError(
                    "source_unavailable",
                    f"The local plan store could not be loaded ({error}).",
                    status=503)
            try:
                path = DEMO_STATE_PATH if self.demo else None
                self._store = module.PlanStore(path=path)
                self._store_error = None
            except Exception as exc:
                self._store_error = redact(str(exc))
                raise ApiError(
                    "source_unavailable",
                    f"The local plan store could not be opened "
                    f"({self._store_error}).", status=503)
            return self._store

    def store_or_none(self):
        try:
            return self.store
        except ApiError:
            return None

    def store_block(self):
        store = self.store_or_none()
        if store is None:
            return {"path": None, "override_count": 0, "orphan_count": 0,
                    "writable": False, "error": self._store_error}
        try:
            stats = store.stats()
        except Exception as exc:
            return {"path": None, "override_count": 0, "orphan_count": 0,
                    "writable": False, "error": redact(str(exc))}
        return {
            "path": stats.get("path"),
            "override_count": stats.get("override_count", 0),
            "orphan_count": stats.get("orphan_count", 0),
            "writable": bool(stats.get("writable")),
            "pending_proposals": stats.get("pending_proposals", 0),
            "error": stats.get("load_error"),
        }

    # -- trainingpeaks (read-only ICS) ------------------------------------

    @staticmethod
    def feed_configured():
        return bool(os.environ.get("TRAININGPEAKS_CALENDAR_FEED_URL"))

    def _classify(self, title, description, all_day):
        module, _ = load_module("scheduler")
        if module is None or not hasattr(module, "classify_workout"):
            return None
        try:
            return module.classify_workout(title or "", description or "",
                                           all_day=all_day)
        except Exception:
            return None

    def _fingerprint(self, title, plan_date):
        module, _ = load_module("plan_store")
        if module is not None and hasattr(module, "fingerprint"):
            try:
                return module.fingerprint(title, plan_date)
            except Exception:
                pass
        import hashlib
        raw = f"{(title or '').strip().lower()}|{plan_date or ''}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def _normalize_plan_entries(self, raw_workouts):
        tz = training_tz()
        entries = []
        for item in raw_workouts or []:
            uid = str(item.get("uid") or "").strip()
            if not uid:
                continue
            plan_date, start_time = normalize_plan_date(item.get("date"), tz)
            if not plan_date:
                continue
            title = str(item.get("title") or "").strip() or "Untitled workout"
            description = str(item.get("description") or "")
            classification = self._classify(title, description,
                                            all_day=start_time is None)
            duration_min = (classification or {}).get("duration_min")
            entries.append({
                "uid": uid,
                "date": plan_date,
                "start_time": start_time,
                "title": title,
                "description": description,
                "sport": (classification or {}).get("sport") or "unknown",
                "duration_s": int(duration_min * 60) if duration_min else None,
                "planned_tss": (classification or {}).get("tss")
                or (classification or {}).get("load"),
                "classification": classification,
                "fingerprint": self._fingerprint(title, plan_date),
            })
        entries.sort(key=lambda e: (e["date"], e.get("start_time") or "",
                                    e["title"]))
        return entries

    def _load_plan_cache(self):
        if self._plan_cache is not None:
            return self._plan_cache
        cached = _read_json(PLAN_CACHE_PATH)
        if isinstance(cached, dict) and isinstance(cached.get("workouts"), list):
            self._plan_cache = cached
            self._plan_last_success = cached.get("fetched_at")
        return self._plan_cache

    def _save_plan_cache(self, entries, ok=True):
        payload = {
            "schema_version": 1,
            "fetched_at": _utc_now(),
            "etag": None,
            "ok": bool(ok),
            "workout_count": len(entries),
            "workouts": entries,
        }
        try:
            _atomic_write_json(PLAN_CACHE_PATH, payload)
        except OSError:
            pass  # a read-only disk must not break the calendar
        self._plan_cache = payload
        self._plan_last_success = payload["fetched_at"]
        return payload

    def plan(self, force=False):
        """Return (entries, warnings). Never raises, never returns an empty
        plan because of a network blip — a failed fetch serves the last good
        snapshot and says so."""
        if self.demo:
            return demo_plan_entries(self), []

        warnings = []
        with self._lock:
            cache = self._load_plan_cache()
            fresh_enough = (
                cache is not None
                and not force
                and (_monotonic() - self._plan_fetched_monotonic) < PLAN_CACHE_TTL
                and self._plan_fetched_monotonic > 0
            )
            if fresh_enough:
                return list(cache.get("workouts") or []), warnings

            if not self.feed_configured():
                self._plan_error = NOT_CONFIGURED_MESSAGES["trainingpeaks_ics"]
                if cache:
                    warnings.append({
                        "code": "ics_not_configured",
                        "message": "Showing your last saved plan — the "
                                   "TrainingPeaks feed URL isn't set.",
                    })
                    return list(cache.get("workouts") or []), warnings
                return [], warnings

            module, error = load_module("trainingpeaks_calendar_feed")
            if module is None:
                self._plan_error = f"Could not load the calendar feed reader ({error})."
                return self._fallback_plan(cache, warnings)

            try:
                feed = module.TrainingPeaksCalendarFeed()
                raw = feed.planned_workouts()
            except KeyError:
                # __init__ uses os.environ[...] — a missing key is a KeyError.
                self._plan_error = NOT_CONFIGURED_MESSAGES["trainingpeaks_ics"]
                return self._fallback_plan(cache, warnings)
            except Exception as exc:
                self._plan_error = redact(
                    f"TrainingPeaks didn't respond ({type(exc).__name__}).")
                return self._fallback_plan(cache, warnings)

            entries = self._normalize_plan_entries(raw)
            self._plan_fetched_monotonic = _monotonic()
            self._plan_error = None

            if not entries and cache and cache.get("workouts"):
                # A 200 with zero events is suspicious: a wiped feed should
                # need a deliberate act to propagate, not a transient bug.
                warnings.append({
                    "code": "ics_empty",
                    "message": "TrainingPeaks returned an empty plan, so I kept "
                               "your last saved one. Refresh again if your coach "
                               "really did clear the calendar.",
                })
                return list(cache.get("workouts") or []), warnings

            self._save_plan_cache(entries, ok=True)
            self._rematch_orphans(entries)
            return entries, warnings

    def _fallback_plan(self, cache, warnings):
        if cache and cache.get("workouts"):
            when = cache.get("fetched_at") or "earlier"
            warnings.append({
                "code": "ics_stale",
                "message": f"Showing your last saved plan from {when} — "
                           f"TrainingPeaks didn't respond.",
            })
            return list(cache.get("workouts") or []), warnings
        return [], warnings

    def _rematch_orphans(self, entries):
        store = self.store_or_none()
        if store is None:
            return
        try:
            store.rematch_orphans(entries)
        except Exception:
            pass

    def plan_source_state(self):
        if self.demo:
            return _source_state(True, True, _utc_now(), False,
                                 "Demo mode: sample plan, no feed contacted.")
        if not self.feed_configured():
            return _source_state(False, False, self._plan_last_success, True,
                                 NOT_CONFIGURED_MESSAGES["trainingpeaks_ics"])
        ok = self._plan_error is None
        stale = True
        if self._plan_last_success:
            try:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(
                           self._plan_last_success.replace("Z", "+00:00"))
                       ).total_seconds()
                stale = age > PLAN_STALE_AFTER
            except ValueError:
                stale = True
        return _source_state(True, ok, self._plan_last_success,
                             stale or not ok, self._plan_error)

    # -- garmin -----------------------------------------------------------

    @staticmethod
    def garmin_configured():
        return bool(os.environ.get("GARMIN_EMAIL")
                    and os.environ.get("GARMIN_PASSWORD"))

    def garmin(self, force=False):
        """Lazily log in, at most once per GARMIN_RETRY_SECONDS. Returns None
        rather than raising — a dead Garmin subtracts context, it never stops
        the calendar rendering."""
        if self.demo or not self.garmin_configured():
            return None
        with self._lock:
            if self._garmin_client is not None and not force:
                return self._garmin_client
            waited = _monotonic() - self._garmin_last_attempt
            if not force and self._garmin_error and waited < GARMIN_RETRY_SECONDS:
                return None
            self._garmin_last_attempt = _monotonic()

            module, error = load_module("garmin_client")
            if module is None:
                self._garmin_error = f"Could not load the Garmin client ({error})."
                return None
            try:
                self._garmin_client = module.GarminClient()
                self._garmin_error = None
                self._garmin_last_success = _utc_now()
            except KeyError as exc:
                # GarminClient.__init__ reads os.environ[...] with bracket
                # syntax; a missing key escapes as KeyError. It must never
                # reach the app factory.
                self._garmin_client = None
                self._garmin_error = (
                    f"Garmin credential {exc} is not set. "
                    + NOT_CONFIGURED_MESSAGES["garmin"])
            except Exception as exc:
                self._garmin_client = None
                name = type(exc).__name__
                if "Auth" in name or "Login" in name:
                    self._garmin_error = (
                        "Garmin login was rejected — check GARMIN_EMAIL and "
                        "GARMIN_PASSWORD, or reconnect.")
                else:
                    self._garmin_error = redact(f"Garmin login failed ({name}).")
            return self._garmin_client

    def garmin_source_state(self):
        if self.demo:
            return _source_state(True, True, _utc_now(), False,
                                 "Demo mode: sample Garmin signal, nothing sent.")
        if not self.garmin_configured():
            return _source_state(False, False, None, True,
                                 NOT_CONFIGURED_MESSAGES["garmin"])
        ok = self._garmin_client is not None and self._garmin_error is None
        return _source_state(True, ok, self._garmin_last_success, not ok,
                             self._garmin_error)

    def strava_source_state(self):
        configured = bool(os.environ.get("STRAVA_CLIENT_ID")
                          and os.environ.get("STRAVA_CLIENT_SECRET")
                          and os.environ.get("STRAVA_REFRESH_TOKEN"))
        if not configured:
            return _source_state(False, False, None, True,
                                 NOT_CONFIGURED_MESSAGES["strava"])
        return _source_state(True, True, None, False,
                             "Strava is configured but the calendar doesn't "
                             "read from it — Garmin is the source of truth.")

    def writer(self):
        module, error = load_module("garmin_workout_writer")
        if module is None:
            raise ApiError("source_unavailable",
                           f"The Garmin writer could not be loaded ({error}).",
                           status=503)
        with self._lock:
            if self._writer is None:
                self._writer = module.GarminWorkoutWriter(
                    client=self.garmin(), store=self.store_or_none())
            return self._writer

    # -- garmin context (readiness / activities / scheduled) --------------

    def garmin_context(self, start, end, force=False):
        """Read-only display context. Garmin's own scheduled workouts are
        never an input to the merge — they are context and drift detection
        only."""
        if self.demo:
            return demo_garmin_context(start, end)

        cache = self._garmin_context
        if (cache and not force
                and (_monotonic() - self._garmin_context_at) < GARMIN_CONTEXT_TTL
                and cache.get("start") <= start and cache.get("end") >= end):
            return cache

        empty = {"start": start, "end": end, "readiness": {}, "activities": {},
                 "scheduled": {}, "training_status": None, "fetched_at": None}

        client = self.garmin()
        if client is None:
            return empty

        context = copy.deepcopy(empty)
        today = today_local().isoformat()

        # readiness — a today-signal; only fetched for today.
        try:
            reading = self._training_readiness(client, today)
            if reading:
                context["readiness"][today] = reading
        except Exception as exc:
            self._garmin_error = redact(f"Garmin readiness unavailable ({type(exc).__name__}).")

        try:
            status = client.training_status(today)
            context["training_status"] = _extract_training_status(status)
        except Exception:
            pass

        try:
            for activity in client.recent_activities(limit=40) or []:
                mapped = _map_activity(activity)
                if mapped and start <= mapped["date"] <= end:
                    context["activities"].setdefault(mapped["date"], []).append(mapped)
        except Exception as exc:
            self._garmin_error = redact(
                f"Garmin activities unavailable ({type(exc).__name__}).")

        try:
            context["scheduled"] = self._scheduled_by_date(start, end)
        except Exception:
            pass

        context["fetched_at"] = _utc_now()
        with self._lock:
            self._garmin_context = context
            self._garmin_context_at = _monotonic()
        return context

    @staticmethod
    def _training_readiness(client, date_str):
        getter = getattr(client, "training_readiness", None)
        if getter is None:
            api = getattr(client, "_api", None)
            getter = getattr(api, "get_training_readiness", None)
        if getter is None:
            return None
        return _extract_readiness(getter(date_str))

    def _scheduled_by_date(self, start, end):
        """`get_scheduled_workouts(year, month)` is month-keyed, so a range
        query fans out over every month it spans."""
        try:
            writer = self.writer()
        except ApiError:
            return {}
        module, _ = load_module("garmin_workout_writer")
        if module is None:
            return {}

        owned = set()
        store = self.store_or_none()
        if store is not None:
            try:
                for record in (store.all_overrides() or {}).values():
                    workout_id = (record.get("garmin") or {}).get("workout_id")
                    if workout_id:
                        owned.add(str(workout_id))
            except Exception:
                pass

        out = {}
        start_d, end_d = _date.fromisoformat(start), _date.fromisoformat(end)
        seen_months = set()
        cursor = start_d.replace(day=1)
        while cursor <= end_d:
            seen_months.add((cursor.year, cursor.month))
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

        for year, month in sorted(seen_months):
            try:
                response = writer.scheduled_in_month(year, month)
            except Exception:
                continue
            for entry in module._iter_dicts(response):
                entry_date = module._extract_date(entry)
                if not entry_date or not (start <= entry_date <= end):
                    continue
                workout_id = None
                for key in module._WORKOUT_ID_KEYS:
                    workout_id = module._coerce_id(entry.get(key))
                    if workout_id:
                        break
                schedule_id = None
                for key in module._SCHEDULE_ID_KEYS:
                    schedule_id = module._coerce_id(entry.get(key))
                    if schedule_id:
                        break
                if not (workout_id or schedule_id):
                    continue
                out.setdefault(entry_date, []).append({
                    "schedule_id": schedule_id,
                    "workout_id": workout_id,
                    "title": entry.get("workoutName") or entry.get("title"),
                    # Only ids in our own store are ours to unschedule; a
                    # workout the user built in Garmin Connect is read-only.
                    "owned_by_us": str(workout_id) in owned,
                })
        return out

    # -- sources / capabilities ------------------------------------------

    def sources_block(self):
        return {
            "trainingpeaks_ics": self.plan_source_state(),
            "garmin": self.garmin_source_state(),
            "strava": self.strava_source_state(),
        }

    def capabilities(self, sources=None):
        sources = sources or self.sources_block()
        cache = self._load_plan_cache()
        can_read = bool(
            self.demo
            or sources["trainingpeaks_ics"]["configured"]
            or (cache and cache.get("workouts")))
        scheduler_module, _ = load_module("scheduler")
        return {
            "can_read_plan": can_read,
            "can_push_to_garmin": bool(sources["garmin"]["configured"]
                                       and not self.demo),
            "can_propose": scheduler_module is not None,
        }

    # -- push jobs --------------------------------------------------------

    def _load_push_log(self):
        data = _read_json(PUSH_LOG_PATH, default={})
        jobs = (data or {}).get("jobs") or []
        for job in jobs:
            if job.get("state") in ("queued", "running"):
                # We cannot know which Garmin calls landed, so an interrupted
                # job is never silently resumed.
                job["state"] = "interrupted"
                job["finished_at"] = job.get("finished_at") or _utc_now()
            self._jobs[job["job_id"]] = job
            self._job_order.append(job["job_id"])

    def _save_push_log(self):
        try:
            jobs = [self._jobs[jid] for jid in self._job_order[-PUSH_LOG_LIMIT:]
                    if jid in self._jobs]
            _atomic_write_json(PUSH_LOG_PATH, {"schema_version": 1, "jobs": jobs})
        except OSError:
            pass

    def record_job(self, job):
        with self._lock:
            if job["job_id"] not in self._jobs:
                self._job_order.append(job["job_id"])
            self._jobs[job["job_id"]] = job
            while len(self._job_order) > PUSH_LOG_LIMIT:
                dropped = self._job_order.pop(0)
                self._jobs.pop(dropped, None)
            self._save_push_log()

    def get_job(self, job_id):
        with self._lock:
            return copy.deepcopy(self._jobs.get(job_id))

    def list_jobs(self, limit=20):
        with self._lock:
            ids = list(reversed(self._job_order))[:limit]
            return [copy.deepcopy(self._jobs[i]) for i in ids if i in self._jobs]

    def active_job_id(self):
        with self._lock:
            if self._active_job and self._jobs.get(self._active_job, {}).get(
                    "state") in ("queued", "running"):
                return self._active_job
            self._active_job = None
            return None


def _monotonic():
    import time
    return time.monotonic()


# ---------------------------------------------------------------------------
# garmin response shaping
# ---------------------------------------------------------------------------


def _first_dict(payload):
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list) and payload:
        return payload[0] if isinstance(payload[0], dict) else None
    return None


def _extract_readiness(payload):
    record = _first_dict(payload)
    if not record:
        return None
    score = record.get("score") or record.get("trainingReadinessScore")
    if score is None:
        return None
    return {
        "score": int(score),
        "level": record.get("level") or record.get("trainingReadinessLevel"),
        "feedback": record.get("feedbackLong") or record.get("feedbackShort")
        or record.get("feedback"),
        "timestamp": record.get("timestamp") or record.get("calendarDate"),
    }


def _extract_training_status(payload):
    record = _first_dict(payload)
    if not record:
        return None
    for key in ("trainingStatus", "trainingStatusFeedbackPhrase", "status"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    latest = record.get("mostRecentTrainingStatus")
    if isinstance(latest, dict):
        for value in latest.values():
            if isinstance(value, dict) and value.get("trainingStatus"):
                return str(value["trainingStatus"])
    return None


_SPORT_KEYS = {
    "cycling": "cycling", "road_biking": "cycling", "virtual_ride": "cycling",
    "indoor_cycling": "cycling", "gravel_cycling": "cycling",
    "mountain_biking": "cycling", "running": "running",
    "treadmill_running": "running", "trail_running": "running",
    "lap_swimming": "swimming", "open_water_swimming": "swimming",
    "strength_training": "strength",
}


def _map_activity(activity):
    if not isinstance(activity, dict):
        return None
    raw_start = activity.get("startTimeLocal") or activity.get("startTimeGMT")
    if not raw_start:
        return None
    text = str(raw_start).replace("T", " ")
    day = text[:10]
    if not _DATE_RE.match(day):
        return None
    type_key = ((activity.get("activityType") or {}).get("typeKey")
                if isinstance(activity.get("activityType"), dict) else None)
    duration = activity.get("duration") or activity.get("elapsedDuration")
    return {
        "activity_id": activity.get("activityId"),
        "name": activity.get("activityName"),
        "sport": _SPORT_KEYS.get(str(type_key or "").lower(), "other"),
        "duration_s": int(duration) if duration else None,
        "distance_m": int(activity.get("distance")) if activity.get("distance") else None,
        "start_time": text[11:16] or None,
        "date": day,
        "tss": activity.get("activityTrainingLoad"),
    }


# ---------------------------------------------------------------------------
# merge: effective calendar = plan + overrides
# ---------------------------------------------------------------------------


def _override_fields(record):
    return dict((record or {}).get("fields") or {})


def _push_state_for(record, effective_date):
    garmin = (record or {}).get("garmin") or {}
    state = garmin.get("push_state") or "never"
    if state == "scheduled" and garmin.get("scheduled_for") \
            and garmin.get("scheduled_for") != effective_date:
        return "drifted"
    return state


def build_effective_workout(entry, record, availability, today,
                            orphaned=False):
    """Rule 1: overrides are a sparse per-field patch, not a record swap. If
    the user moved a workout and the coach later rewrote its description, the
    user keeps their date *and* gets the new text."""
    fields = _override_fields(record)
    plan_date = entry.get("date")
    effective_date = fields.get("date") or plan_date

    title = fields.get("title") or entry.get("title")
    description = fields.get("description")
    if description is None:
        description = entry.get("description")
    duration_s = fields.get("duration_s")
    if duration_s is None:
        duration_s = entry.get("duration_s")
    sport = fields.get("sport") or entry.get("sport") or "unknown"

    status = fields.get("status")
    if orphaned:
        status = "orphaned"
    elif not status:
        status = "moved" if fields.get("date") else "planned"

    conflicts = []
    day = (availability or {}).get(effective_date) or {}
    if day.get("state") == "unavailable":
        conflicts.append("day_unavailable")
    if day.get("state") == "limited" and duration_s and day.get("max_minutes") \
            and duration_s > day["max_minutes"] * 60:
        conflicts.append("day_unavailable")
    if effective_date and effective_date < today:
        conflicts.append("past_date")
    if not entry.get("structured"):
        conflicts.append("no_structure")

    garmin = (record or {}).get("garmin") or {}
    override_block = None
    # A cleared override leaves a tombstone behind so the push ids survive
    # (plan_store._drop_override_locked). That residue is Garmin bookkeeping,
    # not one of the athlete's edits, and it is reported in `garmin` already.
    # Presenting it as an override would leave "Revert to the coach's plan"
    # offered on a workout that is already back on the coach's plan.
    if record and not (record.get("tombstone") and not fields):
        override_block = {
            "kind": record.get("kind"),
            "fields": sorted(fields.keys()),
            "updated_at": record.get("updated_at"),
            "note": record.get("note"),
        }

    return {
        "uid": entry.get("uid"),
        "plan_date": plan_date,
        "date": effective_date,
        "start_time": entry.get("start_time"),
        "title": title,
        "description": description,
        "sport": sport,
        "duration_s": duration_s,
        "planned_tss": entry.get("planned_tss"),
        "structured": None,
        # The ICS carries free text only, so there is nothing structured to
        # send. Push therefore creates a duration-only placeholder and says so.
        "structure_source": "none",
        "status": status,
        "is_moved": bool(fields.get("date")) and fields.get("date") != plan_date,
        "is_orphaned": bool(orphaned),
        "conflicts": conflicts,
        "push_approved": bool(fields.get("push_approved")),
        "garmin": {
            "workout_id": garmin.get("workout_id"),
            "schedule_id": garmin.get("schedule_id"),
            "push_state": _push_state_for(record, effective_date),
            "pushed_at": garmin.get("pushed_at"),
            "scheduled_for": garmin.get("scheduled_for"),
            "error": redact(garmin.get("error")),
        },
        "override": override_block,
        "rev": int((record or {}).get("rev") or 0),
        "classification": entry.get("classification"),
        "plan_snapshot_changed": _snapshot_changed(record, entry),
    }


def _snapshot_changed(record, entry):
    """True when the coach edited this workout since the user overrode it —
    what lets the UI say "your coach changed this since you moved it"."""
    snapshot = (record or {}).get("plan_snapshot") or {}
    if not snapshot:
        return False
    for key in ("title", "date"):
        if snapshot.get(key) and snapshot.get(key) != entry.get(key):
            return True
    return False


def _plan_snapshot(entry):
    return {
        "date": entry.get("date"),
        "title": entry.get("title"),
        "description": entry.get("description"),
        "fingerprint": entry.get("fingerprint"),
    }


class EffectiveCalendar:
    """The merged view. Built per request; cheap enough not to cache."""

    def __init__(self, service, force_plan=False):
        self.service = service
        self.entries, self.warnings = service.plan(force=force_plan)
        self.by_uid = {e["uid"]: e for e in self.entries}
        store = service.store_or_none()
        self.store = store
        self.overrides = {}
        self.availability = {}
        if store is not None:
            try:
                self.overrides = store.all_overrides() or {}
            except Exception:
                self.overrides = {}
            try:
                raw = store.all_day_availability() or {}
                self.availability = {
                    day: dict(rec, date=day, source="user")
                    for day, rec in raw.items()
                }
            except Exception:
                self.availability = {}
        self.today = today_local().isoformat()

    def availability_for(self, day):
        record = self.availability.get(day)
        if record:
            return {
                "state": record.get("state", "available"),
                "note": record.get("note"),
                "max_minutes": record.get("max_minutes"),
                "source": "user",
            }
        return {"state": "available", "note": None, "max_minutes": None,
                "source": "default"}

    def workout(self, uid):
        entry = self.by_uid.get(uid)
        record = self.overrides.get(uid)
        if entry is None and record is None:
            return None
        if entry is None:
            # Rule 7 — an override with no plan entry is retained and
            # surfaced, never dropped. It stays on the calendar at its
            # overridden date.
            snapshot = record.get("plan_snapshot") or {}
            entry = {
                "uid": uid,
                "date": snapshot.get("date")
                or _override_fields(record).get("date") or self.today,
                "start_time": None,
                "title": snapshot.get("title") or "Removed from TrainingPeaks",
                "description": snapshot.get("description") or "",
                "sport": "unknown",
                "duration_s": None,
                "planned_tss": None,
                "classification": None,
                "fingerprint": snapshot.get("fingerprint"),
            }
            return build_effective_workout(entry, record, self.availability,
                                           self.today, orphaned=True)
        return build_effective_workout(entry, record, self.availability,
                                       self.today)

    def all_workouts(self):
        uids = list(self.by_uid.keys())
        uids += [u for u in self.overrides if u not in self.by_uid]
        out = []
        for uid in uids:
            workout = self.workout(uid)
            if workout:
                out.append(workout)
        return out

    def in_range(self, start, end):
        return [w for w in self.all_workouts()
                if w["date"] and start <= w["date"] <= end and not w["is_orphaned"]]

    def orphans(self):
        return [w for w in self.all_workouts() if w["is_orphaned"]]

    def snapshot_for(self, uid):
        entry = self.by_uid.get(uid)
        return _plan_snapshot(entry) if entry else None


def _mark_double_booked(workouts_by_day):
    for day, items in workouts_by_day.items():
        sessions = [w for w in items
                    if (w.get("classification") or {}).get("intensity") != "REST"]
        if len(sessions) > 1:
            for workout in sessions:
                if "double_booked" not in workout["conflicts"]:
                    workout["conflicts"].append("double_booked")


# ---------------------------------------------------------------------------
# proposals
#
# A proposal is a *set* of operations, not one date change. A SWAP moves two
# workouts; a MERGE folds one session into another and annotates the host; a
# DOWNGRADE changes what the session is, not where it sits. Collapsing that to
# `{"date": ...}` applies half of the suggestion — and for a swap, the half
# that double-books the day the swap existed to clear. Everything below
# carries the engine's whole operation set through the API, and the approve
# handler applies it as one unit.
# ---------------------------------------------------------------------------


_INTENSITY_PHRASE = {
    "REST": "rest day", "RECOVERY": "recovery spin", "ENDURANCE": "endurance ride",
    "TEMPO": "tempo session", "THRESHOLD": "threshold session",
    "VO2": "VO2 session", "RACE": "race", "UNKNOWN": "session",
}


def _intensity_phrase(name):
    """"THRESHOLD" -> "threshold session", in the engine's own words when the
    engine is loadable."""
    if not name:
        return None
    module, _ = load_module("scheduler")
    table = getattr(module, "INTENSITY_PHRASE", None) if module else None
    if isinstance(table, dict):
        for intensity, phrase in table.items():
            if getattr(intensity, "name", None) == str(name).upper():
                return phrase
    return _INTENSITY_PHRASE.get(str(name).upper(), str(name).lower())


_VERSION_SUFFIX_RE = re.compile(r"\s+—\s+\w+ version$")


def _signed(text, line):
    """Append one signed Alfred line to a workout's own text — once, so
    re-applying the same change can't stack duplicates."""
    body = (text or "").rstrip()
    line = f"{ALFRED_MARK} {line}"
    if line in body:
        return body or None
    return (body + "\n\n" + line) if body else line


def _op_patch(op, workout, day_name=None):
    """One engine ``Operation`` -> the sparse override patch that realises it.

    Returns None when the operation asks for nothing this store can hold.
    """
    kind = str(op.get("op") or "")
    uid = op.get("workout_uid")
    if not uid:
        return None
    workout = workout or {}
    title = workout.get("title") or op.get("title") or "this session"
    current_date = workout.get("date") or op.get("from_date")
    changes, notes = {}, []

    if kind in ("reschedule", "schedule"):
        to_date = op.get("to_date")
        if to_date and to_date != current_date:
            changes["date"] = to_date
            changes["status"] = "moved"
            notes.append(f"{title} moves to {day_name or to_date}")
        if kind == "schedule" and to_date and to_date == current_date:
            changes["status"] = "planned"

        minutes = op.get("new_duration_min")
        if minutes:
            changes["duration_s"] = int(minutes) * 60
            notes.append(f"cut to {int(minutes)} min")

        intensity = op.get("new_intensity")
        if intensity:
            word = str(intensity).lower()
            was = ((workout.get("classification") or {}).get("intensity")
                   or "").upper()
            # The store has no intensity field, so an easier session shows up
            # where the athlete actually reads it — the title and the text —
            # and therefore also on the Edge when he pushes it.
            base_title = _VERSION_SUFFIX_RE.sub("", workout.get("title") or title)
            eased = f"{base_title} — {word} version"
            if eased != workout.get("title"):
                changes["title"] = eased
            line = (f"Do this as the {word} version — not the "
                    f"{_intensity_phrase(was) or 'planned'} work above.")
            described = _signed(workout.get("description"), line)
            if described:
                changes["description"] = described
            notes.append(f"{word} version instead")

    elif kind == "unschedule":
        changes["status"] = "skipped"
        notes.append(f"{title} comes off the calendar")

    elif kind == "modify_note":
        note = (op.get("note") or "").strip()
        if not note:
            return None
        described = _signed(workout.get("description"), note)
        if described:
            changes["description"] = described
        notes.append(f"{title}: {note}")

    if not changes:
        return None
    summary = ", ".join(notes) if notes else None
    return {"uid": uid, "op": kind, "from_date": op.get("from_date"),
            "to_date": op.get("to_date"), "changes": changes,
            "note": summary, "summary": summary}


def _operation_patches(proposal_dict, calendar):
    """The engine's operation list, translated in order into override patches.

    Every operation is carried, not just the one that happens to belong to the
    proposal's own workout — that is the difference between a swap that clears
    the day and a swap that double-books it.
    """
    primary = proposal_dict.get("workout_uid")
    kind = proposal_dict.get("kind")
    patches = []
    for op in proposal_dict.get("operations") or []:
        uid = op.get("workout_uid")
        workout = calendar.workout(uid) if uid else None
        day = op.get("to_date")
        patch = _op_patch(op, workout, _day_label(day))
        if patch:
            patches.append(patch)

    # MERGE: the engine's single operation annotates the *host*, and it
    # deliberately returns the week unchanged (scheduler.py:1809) — the folded
    # session is already off the calendar there. Materialise that fold, or the
    # session stays on its own day and the "one session" merge becomes two.
    if kind == "MERGE" and not any(p["uid"] == primary for p in patches):
        host_uid = next((p["uid"] for p in patches), None)
        host = calendar.workout(host_uid) if host_uid else None
        workout = calendar.workout(primary)
        host_when = _day_label((host or {}).get("date")
                               or proposal_dict.get("proposed_date"))
        host_title = (host or {}).get("title") or "the other session"
        line = (f"Folded into {host_title} on {host_when} — do it as part of "
                f"that ride, not as a session of its own.")
        changes = {"status": "skipped"}
        described = _signed((workout or {}).get("description"), line)
        if described:
            changes["description"] = described
        title = (workout or {}).get("title") or "this session"
        patches.insert(0, {
            "uid": primary, "op": "fold",
            "from_date": proposal_dict.get("original_date"),
            "to_date": (host or {}).get("date"),
            "changes": changes,
            "note": f"Folded into {host_title} on {host_when}",
            "summary": f"{title} folds into {host_title} on {host_when}",
        })
    return patches


def _day_label(day):
    """"2026-08-07" -> "Friday". Dates the athlete reads, not ISO strings."""
    if not day or not _DATE_RE.match(str(day)):
        return day
    try:
        return _date.fromisoformat(str(day)).strftime("%A")
    except ValueError:
        return day


def _merge_changes(patches, uid):
    """The union of every patch aimed at one workout."""
    changes = {}
    for patch in patches:
        if patch["uid"] == uid:
            changes.update(patch["changes"])
    return changes


def _proposal_changes(proposal_dict, patches=None):
    """The primary workout's field patch.

    Derived from the operations when the engine gave any; the older
    date/duration reading is kept as the fallback for a proposal that predates
    them, and for engine kinds that carry no operations at all.
    """
    primary = proposal_dict.get("workout_uid")
    if patches:
        changes = _merge_changes(patches, primary)
        if changes:
            return changes
        if any(patch["uid"] != primary for patch in patches):
            # Every operation belongs to another workout (a MERGE host note).
            # The primary is genuinely unchanged — don't invent a date for it.
            return {}
    kind = proposal_dict.get("kind")
    changes = {}
    if proposal_dict.get("proposed_date") and kind in (
            "MOVE", "SWAP", "MERGE", "SHORTEN"):
        changes["date"] = proposal_dict["proposed_date"]
    if proposal_dict.get("new_duration_min"):
        changes["duration_s"] = int(proposal_dict["new_duration_min"]) * 60
    if kind == "DROP":
        changes["status"] = "skipped"
    return changes


def engine_proposal_to_api(proposal_dict, reason, calendar):
    uid = proposal_dict.get("workout_uid")
    record = calendar.overrides.get(uid) or {}
    engine_id = str(proposal_dict.get("id") or "")
    engine_kind = proposal_dict.get("kind")
    patches = _operation_patches(proposal_dict, calendar)
    action = proposal_dict.get("action")
    if action not in _ACTIONS:
        action = _ACTION_MAP.get(engine_kind, "move")
    return {
        # Deterministic: the same week re-proposed yields the same ids, so a
        # re-run can't duplicate or contradict a decision already made. The
        # engine already namespaces its ids, so don't prefix them twice.
        "proposal_id": engine_id if engine_id.startswith("prp_")
        else f"prp_{engine_id or uuid.uuid4().hex[:16]}",
        "uid": uid,
        "kind": _KIND_MAP.get(engine_kind, "move"),
        "engine_kind": engine_kind,
        # The engine's own word for what this does. `kind` is how it's drawn;
        # `action` is what it means, and the two may not contradict.
        "action": action,
        "from_date": proposal_dict.get("original_date"),
        "to_date": proposal_dict.get("proposed_date"),
        "changes": _proposal_changes(proposal_dict, patches),
        # Every operation, including the ones on *other* workouts. Approval
        # applies the whole list or none of it.
        "operations": patches,
        "affects_uids": list(dict.fromkeys(
            [p["uid"] for p in patches] + ([uid] if uid else []))),
        "rationale": proposal_dict.get("reason"),
        "rationale_details": proposal_dict.get("reason_details") or [],
        "tradeoff": proposal_dict.get("tradeoff"),
        "question": proposal_dict.get("question"),
        "blocked_pending_input": bool(proposal_dict.get("blocked_pending_input")),
        "alternatives": proposal_dict.get("alternatives") or [],
        "signals_used": proposal_dict.get("signals_used") or [],
        "rule_ids": proposal_dict.get("rule_ids") or [],
        "confidence": _CONFIDENCE_MAP.get(proposal_dict.get("confidence"), 0.5),
        "status": "pending",
        "reason": reason,
        "title": proposal_dict.get("title"),
        # Alfred may not quietly overwrite a manual edit — the UI must show
        # this prominently and approval then requires force.
        "conflicts_with_override": record.get("kind") == "user",
        "created_at": _utc_now(),
        "requires_approval": True,
    }


def generate_proposals(service, calendar, start, end, reason, context=None):
    """Run the adjustment engine over a 21-day window.

    Returns ``(proposals, unresolved, warnings, covered)``. ``covered`` is the
    ``(start, end)`` the engine actually re-planned — it plans exactly one
    Monday-anchored week, whatever range was asked for — or None when it
    didn't run at all. Only that range may be superseded in the store: a
    pending suggestion outside it was never reconsidered, so deleting it would
    silently drop a decision the athlete hasn't made yet.

    Any failure degrades to an empty proposal set with a warning — never a
    500, and never a store write.
    """
    warnings = []
    module, error = load_module("scheduler")
    if module is None:
        warnings.append({
            "code": "proposals_unavailable",
            "message": f"The adjustment engine isn't available ({error}).",
        })
        return [], [], warnings, None

    start_d, end_d = _date.fromisoformat(start), _date.fromisoformat(end)
    window_start = (start_d - timedelta(days=7)).isoformat()
    window_end = (end_d + timedelta(days=7)).isoformat()

    workouts = [
        {"uid": w["uid"], "date": w["date"], "title": w["title"],
         "description": w["description"] or ""}
        for w in calendar.in_range(window_start, window_end)
        if w["status"] not in ("skipped", "done")
    ]
    unavailable = {
        day: (record.get("note") or record.get("reason") or "unavailable")
        for day, record in calendar.availability.items()
        if record.get("state") == "unavailable"
        and window_start <= day <= window_end
    }

    garmin_signal = None
    try:
        context_block = service.garmin_context(start, end)
        readings = context_block.get("readiness") or {}
        if readings:
            latest_day = max(readings)
            garmin_signal = {
                "readiness": readings[latest_day],
                "training_status": context_block.get("training_status"),
            }
    except Exception:
        garmin_signal = None

    try:
        plan = module.build_adjustment_plan(
            workouts, unavailable_days=unavailable, garmin_signal=garmin_signal,
            week_start=start)
    except NotImplementedError as exc:
        warnings.append({
            "code": "proposals_unavailable",
            "message": redact(f"Alfred's adjustment rules aren't configured "
                              f"yet ({exc})."),
        })
        return [], [], warnings, None
    except Exception as exc:
        warnings.append({
            "code": "proposals_unavailable",
            "message": redact(f"The adjustment engine couldn't run "
                              f"({type(exc).__name__})."),
        })
        return [], [], warnings, None

    payload = plan.to_dict()
    proposals = [engine_proposal_to_api(p, reason, calendar)
                 for p in payload.get("proposals") or []]
    # NO_PROPOSAL entries are not approvable — they are the honest "I can't
    # place this" answer and must be visible, not silent.
    unresolved = [engine_proposal_to_api(p, reason, calendar)
                  for p in payload.get("unresolved") or []]
    for item in unresolved:
        item["status"] = "unresolved"
        item["kind"] = "no_proposal"

    for message in payload.get("warnings") or []:
        warnings.append({"code": "engine_note", "message": redact(str(message))})

    # Don't immediately re-suggest something the user already rejected.
    store = calendar.store
    if store is not None:
        kept = []
        for proposal in proposals:
            try:
                if store.is_rejected(proposal["uid"], proposal):
                    continue
            except Exception:
                pass
            kept.append(proposal)
        proposals = kept

    # The one week the engine anchored on, widened to cover every date it
    # actually spoke about.
    anchor = payload.get("week_start") or start
    try:
        anchor_d = _date.fromisoformat(anchor)
    except (TypeError, ValueError):
        anchor_d = start_d
    anchor_d -= timedelta(days=anchor_d.weekday())
    covered_start, covered_end = anchor_d, anchor_d + timedelta(days=6)
    for item in proposals + unresolved:
        for value in (item.get("from_date"), item.get("to_date")):
            if not value or not _DATE_RE.match(str(value)):
                continue
            day = _date.fromisoformat(value)
            covered_start = min(covered_start, day)
            covered_end = max(covered_end, day)

    if covered_start > start_d or covered_end < end_d:
        warnings.append({
            "code": "engine_note",
            "message": (
                f"I re-planned the week of "
                f"{covered_start.strftime('%a')} {covered_start.day} "
                f"{covered_start.strftime('%b')} — suggestions you haven't "
                f"answered on other weeks are untouched."),
        })

    return (proposals, unresolved, warnings,
            (covered_start.isoformat(), covered_end.isoformat()))


# ---------------------------------------------------------------------------
# unresolved sessions
#
# "I can't place this anywhere without breaking something" is an answer, not a
# failure — and it's the answer that most needs to survive. The plan store
# holds *decidable* proposals (pending / approved / rejected); a NO_PROPOSAL
# is none of those and must never become approvable, so it lives here, beside
# the plan cache, and is re-stated on every calendar read.
# ---------------------------------------------------------------------------


UNRESOLVED_LIMIT = 200


def unresolved_path(service):
    name = "demo_unresolved.json" if getattr(service, "demo", False) \
        else "unresolved.json"
    return os.path.join(STATE_DIR, name)


def _unresolved_anchor(item):
    return item.get("from_date") or item.get("to_date")


def read_unresolved(service):
    data = _read_json(unresolved_path(service), default={}) or {}
    items = data.get("items")
    return list(items) if isinstance(items, list) else []


def save_unresolved(service, covered, items):
    """Replace the unresolved set for the range the engine just re-planned.

    Same rule as the proposals themselves: only the recomputed range is
    superseded. An unanswered "I can't place this" on another week stays.
    """
    if not covered:
        return read_unresolved(service)
    start, end = covered
    fresh_uids = {item.get("uid") for item in items or []}
    kept = []
    for existing in read_unresolved(service):
        anchor = _unresolved_anchor(existing)
        if anchor and start <= anchor <= end:
            continue
        if existing.get("uid") in fresh_uids:
            continue
        kept.append(existing)
    merged = (kept + list(items or []))[-UNRESOLVED_LIMIT:]
    try:
        _atomic_write_json(unresolved_path(service),
                           {"schema_version": 1, "saved_at": _utc_now(),
                            "items": merged})
    except OSError:
        pass  # read-only state dir: the response still carries them
    return merged


def live_unresolved(service, calendar, start=None, end=None):
    """The unresolved items still worth showing.

    One is stale the moment its situation changes — the workout was moved by
    hand, skipped, or has left the plan — so it's filtered out on read rather
    than lingering as a permanent scar on the calendar.
    """
    out = []
    for item in read_unresolved(service):
        anchor = _unresolved_anchor(item)
        if not anchor:
            continue
        if start and anchor < start:
            continue
        if end and anchor > end:
            continue
        workout = calendar.workout(item.get("uid")) if item.get("uid") else None
        if workout is None:
            continue
        if workout.get("status") in ("skipped", "done"):
            continue
        if workout.get("date") != item.get("from_date"):
            continue
        item = dict(item, status="unresolved", requires_approval=True,
                    title=item.get("title") or workout.get("title"))
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# demo data
# ---------------------------------------------------------------------------


def _demo_monday():
    today = today_local()
    return today - timedelta(days=today.weekday())


DEMO_WORKOUTS = [
    (0, "Recovery spin 45min", "Easy Z1 spin, 45 min. Keep it under 55% FTP — "
        "legs should feel better afterwards, not worse."),
    (1, "Threshold 3x12min", "Warm up 20min.\n3x12 @ 95-100% FTP with 6min "
        "easy between.\nCool down 10min.\nPlanned TSS: 82\nDuration: 1:15"),
    (2, "Endurance 2hr Z2", "Steady endurance ride, 2:00 in Z2. Stay aerobic, "
        "no surges on the climbs. Planned TSS: 105"),
    (3, "VO2 max 5x5", "Warm up 20min.\n5x5min @ 110-115% FTP, 5min recovery.\n"
        "Cool down 15min. This one is a key session. TSS 95"),
    (4, "Rest day", "Full rest. No training."),
    (5, "Endurance 3hr with 3x12 @ threshold", "Long ride 3:00.\nInclude 3x12min "
        "@ threshold in the middle third.\nPlanned TSS: 165"),
    (6, "Sweet spot 4x15", "Warm up 15min.\n4x15min @ 88-93% FTP, 5min easy.\n"
        "Cool down 10min. Duration: 1:45. TSS 118"),
]


def demo_plan_entries(service):
    monday = _demo_monday()
    entries = []
    for week_offset in (-1, 0, 1):
        for day_offset, title, description in DEMO_WORKOUTS:
            day = monday + timedelta(days=day_offset, weeks=week_offset)
            uid = f"demo-{day.isoformat()}-{day_offset}@trainingpeaks.example"
            classification = service._classify(title, description, all_day=True)
            duration_min = (classification or {}).get("duration_min")
            entries.append({
                "uid": uid,
                "date": day.isoformat(),
                "start_time": None,
                "title": title,
                "description": description,
                "sport": (classification or {}).get("sport") or "cycling",
                "duration_s": int(duration_min * 60) if duration_min else None,
                "planned_tss": (classification or {}).get("tss")
                or (classification or {}).get("load"),
                "classification": classification,
                "fingerprint": service._fingerprint(title, day.isoformat()),
            })
    entries.sort(key=lambda e: e["date"])
    return entries


def demo_garmin_context(start, end):
    today = today_local()
    readiness = {today.isoformat(): {
        "score": 72, "level": "MODERATE",
        "feedback": "Your recent training load is balanced and your sleep was "
                    "good — a solid day to train.",
        "timestamp": _utc_now(),
    }}
    activities = {}
    samples = [
        ("Morning Ride", "cycling", 5400, 48000, "07:10", 78),
        ("Threshold intervals", "cycling", 4500, 41000, "06:55", 92),
        ("Easy spin", "cycling", 2700, 21000, "17:30", 31),
        ("Long ride", "cycling", 10800, 96000, "08:00", 168),
    ]
    for index, (name, sport, duration, distance, at, load) in enumerate(samples):
        day = (today - timedelta(days=index + 1)).isoformat()
        if start <= day <= end:
            activities.setdefault(day, []).append({
                "activity_id": 900000 + index, "name": name, "sport": sport,
                "duration_s": duration, "distance_m": distance,
                "start_time": at, "date": day, "tss": load,
            })
    return {"start": start, "end": end, "readiness": readiness,
            "activities": activities, "scheduled": {},
            "training_status": "PRODUCTIVE", "fetched_at": _utc_now()}


# ---------------------------------------------------------------------------
# applying an approved proposal
#
# Approval is still only ever a local write: it materialises the proposal as
# overrides and stops there. Nothing here goes near Garmin — push is a second,
# separate consent.
# ---------------------------------------------------------------------------


def _conflicting_fields(record, changes):
    """The fields the athlete set by hand that this patch would overwrite."""
    if not record or record.get("kind") != "user":
        return []
    existing = record.get("fields") or {}
    return sorted(key for key, value in (changes or {}).items()
                  if key in existing and existing[key] != value)


def _rollback_patches(store, applied):
    """Undo the operations already written, newest first.

    A half-applied swap is worse than no swap: one session moved, the other
    left behind, and a double-booked day. If any operation can't be written,
    every one before it is put back.
    """
    for uid, before, changes in reversed(applied):
        try:
            if before is None:
                store.clear_override(uid, actor="system")
                continue
            fields = before.get("fields") or {}
            # A key absent from the old record maps to None, which clears it.
            restore = {key: fields.get(key) for key in changes}
            store.patch_workout(uid, fields=restore,
                                note=before.get("note") or "",
                                kind=before.get("kind") or "alfred",
                                actor="system", force=True,
                                action="rollback_proposal")
        except Exception:
            # Best effort — a rollback failure must not mask the original
            # error, which is what the athlete actually needs to read.
            pass


def proposal_patches(proposal):
    """The stored proposal's operation patches, primary workout first."""
    uid = proposal.get("uid")
    out = []
    for patch in proposal.get("operations") or []:
        if not isinstance(patch, dict):
            continue
        if not patch.get("uid") or not patch.get("changes"):
            continue
        out.append(patch)
    primary = [p for p in out if p["uid"] == uid]
    return primary + [p for p in out if p["uid"] != uid]


# ---------------------------------------------------------------------------
# push jobs
# ---------------------------------------------------------------------------


def _select_uids(calendar, start, end, select, uids):
    in_range = calendar.in_range(start, end)
    if select == "explicit":
        wanted = set(uids or [])
        return [w for w in in_range if w["uid"] in wanted]
    if select == "all_effective":
        return in_range
    return [w for w in in_range if w["push_approved"]]


def build_push_items(service, calendar, workouts, dry_run):
    """Ask the writer what it *would* do. `push_workout(dry_run=True)` decides
    the action from the stored hash and date without making a single network
    call, so the preview is honest and works with no Garmin session."""
    module, error = load_module("garmin_workout_writer")
    if module is None:
        raise ApiError("source_unavailable",
                       f"The Garmin writer could not be loaded ({error}).",
                       status=503)
    writer = module.GarminWorkoutWriter(client=None,
                                        store=calendar.store)
    today = today_local().isoformat()
    items = []
    for workout in workouts:
        if workout["status"] == "skipped":
            items.append({"uid": workout["uid"], "date": workout["date"],
                          "title": workout["title"], "action": "skip",
                          "reason": "Marked skipped.", "state": "skipped",
                          "garmin_workout_id": None, "garmin_schedule_id": None,
                          "error": None, "structured": False})
            continue
        if workout["date"] < today:
            items.append({"uid": workout["uid"], "date": workout["date"],
                          "title": workout["title"], "action": "skip",
                          "reason": "In the past — not sent to the Edge.",
                          "state": "skipped", "garmin_workout_id": None,
                          "garmin_schedule_id": None, "error": None,
                          "structured": False})
            continue
        try:
            result = writer.push_workout(
                workout["uid"], workout["title"], workout["date"],
                steps=None, duration_s=workout["duration_s"],
                sport=workout["sport"] if workout["sport"] in
                ("cycling", "running") else "cycling",
                description=workout["description"],
                store=calendar.store, dry_run=True,
                plan_snapshot=calendar.snapshot_for(workout["uid"]))
            row = result.to_dict()
            row["state"] = "skipped" if row["action"] == "noop" else "pending"
            items.append(row)
        except Exception as exc:
            items.append({"uid": workout["uid"], "date": workout["date"],
                          "title": workout["title"], "action": "create",
                          "reason": None, "state": "failed",
                          "garmin_workout_id": None, "garmin_schedule_id": None,
                          "error": redact(str(exc)), "structured": False})
    return items


def _summarise(items):
    """Counts by the action each row carries, not by whether it has run yet —
    a dry run's whole purpose is to answer "what will you send?", so its
    summary has to be populated while every row is still `pending`."""
    summary = {"created": 0, "rescheduled": 0, "updated": 0, "skipped": 0,
               "failed": 0, "noop": 0}
    for item in items:
        state, action = item.get("state"), item.get("action")
        if state == "failed":
            summary["failed"] += 1
        elif action == "noop":
            summary["noop"] += 1
        elif action == "skip" or state == "skipped":
            summary["skipped"] += 1
        elif action == "create":
            summary["created"] += 1
        elif action == "reschedule":
            summary["rescheduled"] += 1
        elif action == "update":
            summary["updated"] += 1
    return summary


def run_push_job(service, job_id):
    """Sequential, one workout at a time. A failed item never fails the job —
    partial success is a normal outcome and is reported honestly per row."""
    job = service.get_job(job_id)
    if job is None:
        return
    job["state"] = "running"
    job["started_at"] = _utc_now()
    service.record_job(job)

    calendar = EffectiveCalendar(service)
    store = calendar.store
    try:
        writer = service.writer()
    except ApiError as exc:
        job["state"] = "failed"
        job["finished_at"] = _utc_now()
        job["error"] = {"code": exc.code, "message": exc.message}
        service.record_job(job)
        return

    module, _ = load_module("garmin_workout_writer")
    done = 0
    for index, item in enumerate(job["items"]):
        current = service.get_job(job_id)
        if current and current.get("state") == "cancelled":
            return
        if item["state"] == "skipped":
            done += 1
            continue
        workout = calendar.workout(item["uid"])
        if workout is None:
            item["state"] = "failed"
            item["error"] = {"code": "unknown_uid",
                             "message": "That workout is no longer on the plan."}
            done += 1
            continue
        try:
            result = writer.push_workout(
                workout["uid"], workout["title"], workout["date"],
                steps=None, duration_s=workout["duration_s"],
                sport=workout["sport"] if workout["sport"] in
                ("cycling", "running") else "cycling",
                description=workout["description"], store=store,
                dry_run=False,
                plan_snapshot=calendar.snapshot_for(workout["uid"]))
            job["items"][index] = dict(result.to_dict(), error=(
                {"code": "garmin_rejected", "message": redact(result.error)}
                if result.error else None))
        except Exception as exc:
            name = type(exc).__name__
            auth = module is not None and isinstance(
                exc, getattr(module, "GarminAuthFailed", ()))
            not_configured = module is not None and isinstance(
                exc, getattr(module, "GarminNotConfigured", ()))
            item["state"] = "failed"
            item["error"] = {
                "code": "not_configured" if not_configured else "garmin_rejected",
                "message": redact(str(exc)),
            }
            if auth or not_configured:
                # Session-level: one banner, not the same error on eight rows.
                job["auth_error"] = redact(str(exc))
                job["state"] = "failed"
                job["finished_at"] = _utc_now()
                job["progress"] = {"done": done, "total": len(job["items"])}
                job["summary"] = _summarise(job["items"])
                service.record_job(job)
                return
            job["items"][index] = item
        done += 1
        job["progress"] = {"done": done, "total": len(job["items"])}
        service.record_job(job)

    job["summary"] = _summarise(job["items"])
    job["progress"] = {"done": len(job["items"]), "total": len(job["items"])}
    failed = job["summary"]["failed"]
    if failed and failed == len([i for i in job["items"]
                                 if i["state"] != "skipped"]):
        job["state"] = "failed"
    elif failed:
        job["state"] = "partial"
    else:
        job["state"] = "done"
    job["finished_at"] = _utc_now()
    service.record_job(job)


# ---------------------------------------------------------------------------
# request body helpers
# ---------------------------------------------------------------------------


async def read_body(request):
    try:
        raw = await request.body()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise ApiError("invalid_range", "The request body wasn't valid JSON.",
                       status=400)
    return data if isinstance(data, dict) else {}


def _translate_store_error(exc, store_module):
    if store_module is None:
        return ApiError("source_unavailable", redact(str(exc)), status=500)
    # Subclasses first; the PlanStoreError catch-all must stay last so a more
    # specific match always wins. Without that final row a bare PlanStoreError
    # — which the store raises for a refused undo — reaches the athlete as a
    # 500 "something broke", when it is an ordinary, expected "no, not that
    # one" that the UI should show as a normal message.
    mapping = [
        ("UnknownUid", "unknown_uid", 404),
        ("StaleRev", "stale_rev", 409),
        ("ProposalConflict", "proposal_conflict", 409),
        ("UnknownProposal", "unknown_uid", 404),
        ("InvalidValue", "invalid_range", 400),
        ("PlanStoreError", "store_conflict", 409),
    ]
    for name, code, status in mapping:
        klass = getattr(store_module, name, None)
        if klass and isinstance(exc, klass):
            raw = redact(str(exc))
            message = raw
            if code == "stale_rev":
                # The store's own wording is a version number; the person
                # reading it is a cyclist, not a programmer.
                message = ("This workout changed in another tab since you "
                           "opened it. Reload the week and try again.")
            elif code == "store_conflict":
                # Likewise: the store names an entry id, the athlete thinks in
                # terms of the change he just made.
                message = ("There's a newer change to that workout, so this "
                           "one can't be undone on its own. Undo the most "
                           "recent change first.") if "superseded" in raw else raw
            return ApiError(code, message, status=status,
                            detail={"store_message": raw}
                            if code in ("stale_rev", "store_conflict") else None)
    return ApiError("source_unavailable", redact(str(exc)), status=500)


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------


def create_app(demo=None):
    if demo is None:
        demo = os.environ.get("TRAINING_SYNC_DEMO", "").lower() in (
            "1", "true", "yes", "on")

    # Create the state directory up front. The store reports itself
    # unwritable when its parent doesn't exist yet, which would show the user
    # a read-only warning on a perfectly healthy first run.
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError:
        pass

    service = TrainingService(demo=demo)
    app = FastAPI(title="Training Calendar", version=APP_VERSION,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.service = service

    # -- error handling ---------------------------------------------------

    @app.exception_handler(ApiError)
    async def _api_error_handler(request, exc):
        return JSONResponse(status_code=exc.status, content=exc.body())

    @app.exception_handler(Exception)
    async def _unhandled_handler(request, exc):
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"error": {
                "code": "source_unavailable",
                "message": redact(
                    f"Something went wrong handling that request "
                    f"({type(exc).__name__}). Your saved plan is untouched."),
                "detail": {},
            }})

    def _sources_payload():
        sources = service.sources_block()
        return sources, service.capabilities(sources)

    # -- configuration & health ------------------------------------------

    @app.get("/api/status")
    def get_status():
        sources, capabilities = _sources_payload()
        return {
            "app_version": APP_VERSION,
            "training_tz": tz_name(),
            "demo": service.demo,
            "sources": sources,
            "store": service.store_block(),
            "capabilities": capabilities,
        }

    @app.post("/api/refresh")
    async def post_refresh(request: Request):
        body = await read_body(request)
        wanted = body.get("sources") or ["trainingpeaks_ics", "garmin"]
        if "trainingpeaks_ics" in wanted:
            service.plan(force=True)
        if "garmin" in wanted:
            service._garmin_context = None
            service._garmin_context_at = 0.0
            service.garmin(force=True)
        sources, capabilities = _sources_payload()
        return {
            "app_version": APP_VERSION,
            "training_tz": tz_name(),
            "demo": service.demo,
            "sources": sources,
            "store": service.store_block(),
            "capabilities": capabilities,
        }

    # -- calendar ---------------------------------------------------------

    @app.get("/api/calendar")
    def get_calendar(
        start: str = Query(...),
        end: str = Query(...),
        include_proposals: bool = Query(True),
    ):
        start_d = parse_date(start, "start")
        end_d = parse_date(end, "end")
        if end_d < start_d:
            raise ApiError("invalid_range", "The end date is before the start date.",
                           status=400)
        span = (end_d - start_d).days + 1
        if span > MAX_RANGE_DAYS:
            raise ApiError(
                "invalid_range",
                f"That's {span} days — ask for {MAX_RANGE_DAYS} or fewer.",
                status=400, detail={"max_days": MAX_RANGE_DAYS})

        calendar = EffectiveCalendar(service)
        warnings = list(calendar.warnings)
        sources, _ = _sources_payload()

        if not sources["trainingpeaks_ics"]["configured"] and not service.demo:
            warnings.append({
                "code": "ics_not_configured",
                "message": NOT_CONFIGURED_MESSAGES["trainingpeaks_ics"],
            })

        by_day = {}
        for workout in calendar.in_range(start, end):
            by_day.setdefault(workout["date"], []).append(workout)
        _mark_double_booked(by_day)

        proposals_by_day = {}
        unresolved_by_day = {}
        unresolved_all = []
        if include_proposals and calendar.store is not None:
            try:
                for proposal in calendar.store.list_proposals(
                        status="pending", start=start, end=end) or []:
                    day = proposal.get("to_date") or proposal.get("from_date")
                    if day:
                        proposals_by_day.setdefault(day, []).append(proposal)
            except Exception:
                pass
        if include_proposals:
            try:
                unresolved_all = live_unresolved(service, calendar, start, end)
            except Exception:
                unresolved_all = []
            for item in unresolved_all:
                day = _unresolved_anchor(item)
                if day:
                    unresolved_by_day.setdefault(day, []).append(item)

        context = service.garmin_context(start, end)

        days = []
        for day in daterange(start_d, end_d):
            key = day.isoformat()
            workouts = by_day.get(key, [])
            planned_tss = sum(w["planned_tss"] for w in workouts
                              if isinstance(w.get("planned_tss"), (int, float)))
            activities = (context.get("activities") or {}).get(key, [])
            actual = sum(a["tss"] for a in activities
                         if isinstance(a.get("tss"), (int, float)))
            days.append({
                "date": key,
                "availability": calendar.availability_for(key),
                "workouts": workouts,
                "proposals": proposals_by_day.get(key, []),
                # Sessions the engine could not place. Not approvable, but
                # never silent: an unsolvable week has to be visible.
                "unresolved": unresolved_by_day.get(key, []),
                "garmin": {
                    "readiness": (context.get("readiness") or {}).get(key),
                    "activities": activities,
                    "scheduled": (context.get("scheduled") or {}).get(key, []),
                },
                "load": {
                    "planned_tss": round(planned_tss, 1) if planned_tss else None,
                    "actual_tss": round(actual, 1) if actual else None,
                },
            })

        return {
            "range": {"start": start, "end": end},
            "generated_at": _utc_now(),
            "demo": service.demo,
            "sources": sources,
            "training_status": context.get("training_status"),
            "days": days,
            # Top-level: an orphaned override can point at a date outside the
            # requested range, and must still be visible.
            "orphans": calendar.orphans(),
            "unresolved": unresolved_all,
            "warnings": warnings,
        }

    # -- day availability -------------------------------------------------

    def _day_entry(calendar, day):
        workouts = [w for w in calendar.in_range(day, day)]
        _mark_double_booked({day: workouts})
        context = service.garmin_context(day, day)
        proposals = []
        if calendar.store is not None:
            try:
                proposals = [p for p in calendar.store.list_proposals(
                    status="pending", start=day, end=day) or []]
            except Exception:
                proposals = []
        try:
            unresolved = live_unresolved(service, calendar, day, day)
        except Exception:
            unresolved = []
        planned_tss = sum(w["planned_tss"] for w in workouts
                          if isinstance(w.get("planned_tss"), (int, float)))
        return {
            "date": day,
            "availability": calendar.availability_for(day),
            "workouts": workouts,
            "proposals": proposals,
            "unresolved": unresolved,
            "garmin": {
                "readiness": (context.get("readiness") or {}).get(day),
                "activities": (context.get("activities") or {}).get(day, []),
                "scheduled": (context.get("scheduled") or {}).get(day, []),
            },
            "load": {"planned_tss": round(planned_tss, 1) if planned_tss else None,
                     "actual_tss": None},
        }

    @app.put("/api/days/{day}/availability")
    async def put_availability(day: str, request: Request):
        parse_date(day, "date")
        body = await read_body(request)
        state = body.get("state")
        if state not in AVAILABILITY_STATES:
            raise ApiError("invalid_range",
                           f"state must be one of {', '.join(AVAILABILITY_STATES)}.",
                           status=400)
        store = service.store
        store_module, _ = load_module("plan_store")
        try:
            store.set_day_availability(
                day, state, note=body.get("note"),
                max_minutes=body.get("max_minutes"), actor="user")
        except Exception as exc:
            raise _translate_store_error(exc, store_module)
        # Marking a day unavailable *flags* the workouts on it. It never
        # relocates anything — only an explicit move or an approved proposal
        # changes a date.
        return _day_entry(EffectiveCalendar(service), day)

    @app.delete("/api/days/{day}/availability")
    def delete_availability(day: str):
        parse_date(day, "date")
        store = service.store
        store_module, _ = load_module("plan_store")
        try:
            store.clear_day_availability(day, actor="user")
        except Exception as exc:
            raise _translate_store_error(exc, store_module)
        return _day_entry(EffectiveCalendar(service), day)

    # -- workouts ---------------------------------------------------------

    @app.get("/api/workouts/{uid}")
    def get_workout(uid: str):
        calendar = EffectiveCalendar(service)
        workout = calendar.workout(uid)
        if workout is None:
            raise ApiError("unknown_uid",
                           "That workout isn't on your plan or in your saved changes.",
                           status=404)
        return workout

    @app.post("/api/workouts/{uid}/move")
    async def move_workout(uid: str, request: Request):
        body = await read_body(request)
        to_date = body.get("to_date")
        parse_date(to_date, "to_date")

        calendar = EffectiveCalendar(service)
        if calendar.workout(uid) is None:
            raise ApiError("unknown_uid",
                           "That workout isn't on your plan or in your saved changes.",
                           status=404)

        availability = calendar.availability_for(to_date)
        if availability["state"] == "unavailable" and not body.get("allow_unavailable"):
            raise ApiError(
                "day_unavailable",
                f"You marked {to_date} unavailable"
                + (f" ({availability['note']})" if availability.get("note") else "")
                + " — move it there anyway?",
                status=409, detail={"to_date": to_date,
                                    "allow_unavailable": True})

        store = service.store
        store_module, _ = load_module("plan_store")
        try:
            store.move_workout(uid, to_date, note=body.get("note"), kind="user",
                               expected_rev=body.get("expected_rev"),
                               plan_snapshot=calendar.snapshot_for(uid),
                               actor="user")
        except Exception as exc:
            raise _translate_store_error(exc, store_module)
        return EffectiveCalendar(service).workout(uid)

    @app.patch("/api/workouts/{uid}")
    async def patch_workout(uid: str, request: Request):
        body = await read_body(request)
        calendar = EffectiveCalendar(service)
        if calendar.workout(uid) is None:
            raise ApiError("unknown_uid",
                           "That workout isn't on your plan or in your saved changes.",
                           status=404)

        store_module, _ = load_module("plan_store")
        allowed = set(getattr(store_module, "OVERRIDABLE_FIELDS", ()))
        # Absent keys are untouched; an explicit null clears the override back
        # to the coach's plan value.
        fields = {k: v for k, v in body.items() if k in allowed}
        unknown = set(body) - allowed - {"note", "expected_rev"}
        if unknown:
            raise ApiError("invalid_range",
                           f"Can't change: {', '.join(sorted(unknown))}.",
                           status=400,
                           detail={"overridable": sorted(allowed)})
        if fields.get("date"):
            parse_date(fields["date"], "date")

        store = service.store
        try:
            store.patch_workout(uid, fields=fields, note=body.get("note"),
                                kind="user",
                                expected_rev=body.get("expected_rev"),
                                plan_snapshot=calendar.snapshot_for(uid),
                                actor="user")
        except Exception as exc:
            raise _translate_store_error(exc, store_module)
        return EffectiveCalendar(service).workout(uid)

    @app.delete("/api/workouts/{uid}/override")
    def delete_override(uid: str):
        store = service.store
        store_module, _ = load_module("plan_store")
        try:
            store.clear_override(uid, actor="user")
        except Exception as exc:
            raise _translate_store_error(exc, store_module)
        calendar = EffectiveCalendar(service)
        workout = calendar.workout(uid)
        if workout is None:
            raise ApiError("unknown_uid",
                           "That workout isn't on your plan any more.", status=404)
        return workout

    # -- proposals --------------------------------------------------------

    @app.post("/api/proposals")
    async def post_proposals(request: Request):
        body = await read_body(request)
        start = body.get("start") or today_local().isoformat()
        end = body.get("end") or (today_local() + timedelta(days=6)).isoformat()
        parse_date(start, "start")
        parse_date(end, "end")
        reason = body.get("reason") or "manual"
        if reason not in PROPOSAL_REASONS:
            raise ApiError("invalid_range",
                           f"reason must be one of {', '.join(PROPOSAL_REASONS)}.",
                           status=400)

        calendar = EffectiveCalendar(service)
        proposals, unresolved, warnings, covered = generate_proposals(
            service, calendar, start, end, reason, body.get("context"))

        stored = proposals
        if calendar.store is not None and covered is not None:
            try:
                # Regenerating replaces the *pending* set for the range the
                # engine actually re-planned — which is one Monday-anchored
                # week, not the range the month view asked for. Superseding
                # the whole request would delete undecided suggestions the
                # engine never reconsidered. Already decided ones are
                # historical record either way.
                stored = calendar.store.replace_pending_proposals(
                    covered[0], covered[1], proposals)
            except Exception as exc:
                warnings.append({"code": "proposals_unavailable",
                                 "message": redact(
                                     f"Couldn't save the proposals ({exc}).")})

        # The "I couldn't place this" answers are persisted too — they are
        # re-read on every calendar load, so they outlive the one toast.
        try:
            save_unresolved(service, covered, unresolved)
        except Exception as exc:
            warnings.append({"code": "proposals_unavailable",
                             "message": redact(
                                 f"Couldn't save the unplaceable sessions "
                                 f"({exc}).")})

        return {
            "requested_at": _utc_now(),
            "range": {"start": start, "end": end},
            "replanned": {"start": covered[0], "end": covered[1]}
            if covered else None,
            "proposals": stored,
            "unresolved": unresolved,
            "warnings": warnings,
        }

    @app.get("/api/proposals")
    def list_proposals(start: str = Query(None), end: str = Query(None),
                       status: str = Query("pending")):
        if start:
            parse_date(start, "start")
        if end:
            parse_date(end, "end")
        if status == "unresolved":
            # Not in the plan store by design: an unplaceable session is not a
            # decidable proposal, and must never be approvable.
            return {"proposals": [], "unresolved": live_unresolved(
                service, EffectiveCalendar(service), start, end)}
        store = service.store_or_none()
        if store is None:
            return {"proposals": [], "unresolved": []}
        try:
            proposals = store.list_proposals(
                status=status or None, start=start, end=end) or []
        except Exception:
            proposals = []
        try:
            unresolved = live_unresolved(service, EffectiveCalendar(service),
                                         start, end)
        except Exception:
            unresolved = []
        return {"proposals": proposals, "unresolved": unresolved}

    @app.post("/api/proposals/{proposal_id}/approve")
    async def approve_proposal(proposal_id: str, request: Request):
        body = await read_body(request)
        force = bool(body.get("force"))
        store = service.store
        store_module, _ = load_module("plan_store")
        calendar = EffectiveCalendar(service)
        proposal = store.get_proposal(proposal_id)
        if proposal is None:
            raise ApiError("unknown_uid", "That suggestion no longer exists.",
                           status=404)

        uid = proposal.get("uid")
        if proposal.get("status") == "approved":
            # Idempotent: re-approving must not re-apply the operations, or a
            # double-click would move the swapped session a second time.
            after = EffectiveCalendar(service)
            return {"proposal": proposal, "workout": after.workout(uid),
                    "workouts": [w for w in [after.workout(uid)] if w],
                    "applied": []}
        if proposal.get("status") == "rejected":
            raise ApiError(
                "proposal_conflict",
                "You've already dismissed this suggestion.", status=409,
                detail={"uid": uid})

        patches = proposal_patches(proposal)
        primary_changes = dict(proposal.get("changes") or {})
        # The primary workout is written by the store when it marks the
        # proposal approved; everything else is written here, in the engine's
        # order, and the whole set stands or falls together.
        secondary = [p for p in patches if p["uid"] != uid]
        # A KEEP has nothing to write at all — "I left the race where it is"
        # is an answer, not an edit. Approving it must not leave an empty
        # override behind.
        no_op = not primary_changes and not patches
        before_primary = store.get_override(uid) if uid else None

        if not force:
            blocked = []
            for patch in ([{"uid": uid, "changes": primary_changes}]
                          + secondary):
                if not patch["uid"] or not patch["changes"]:
                    continue
                record = (before_primary if patch["uid"] == uid
                          else store.get_override(patch["uid"]))
                fields = _conflicting_fields(record, patch["changes"])
                if fields:
                    blocked.append({"uid": patch["uid"], "fields": fields})
            if blocked:
                # Refused before the first write rather than discovered
                # half-way through it.
                names = []
                for entry in blocked:
                    workout = calendar.workout(entry["uid"]) or {}
                    names.append(workout.get("title") or entry["uid"])
                raise ApiError(
                    "proposal_conflict",
                    "You've already edited "
                    + (" and ".join(names) if names else "this workout")
                    + " by hand. Approving Alfred's version would overwrite "
                      "your change — retry with force to accept it.",
                    status=409,
                    detail={"force": True, "uid": uid, "blocked": blocked})

        applied = []
        try:
            for patch in secondary:
                before = store.get_override(patch["uid"])
                store.patch_workout(
                    patch["uid"], fields=dict(patch["changes"]),
                    note=patch.get("note"), kind="alfred",
                    plan_snapshot=calendar.snapshot_for(patch["uid"]),
                    actor="user", force=force, action="approve_proposal")
                applied.append((patch["uid"], before, dict(patch["changes"])))
            # Approving materialises the proposal into an override. It does
            # not push — approving a plan change and sending it to the device
            # are two separate consents.
            result = store.approve_proposal(
                proposal_id, force=force, actor="user",
                plan_snapshot=calendar.snapshot_for(uid))
        except Exception as exc:
            _rollback_patches(store, applied)
            error = _translate_store_error(exc, store_module)
            if error.code == "proposal_conflict":
                error.message = (
                    "You've already edited this workout by hand. Approving "
                    "Alfred's version would overwrite your change — retry with "
                    "force to accept it.")
                error.detail = {"force": True, "uid": uid}
            raise error

        if no_op and before_primary is None:
            # The store materialises every approval as an override; for a KEEP
            # that leaves an empty record with a bumped rev, which reads on the
            # calendar as "Alfred changed this" when nothing changed.
            record = store.get_override(uid)
            if record is not None and not (record.get("fields") or {}) \
                    and record.get("kind") == "alfred" \
                    and (record.get("garmin") or {}).get("push_state") in (
                        None, "never"):
                try:
                    store.clear_override(uid, actor="system")
                    result["override"] = None
                except Exception:
                    pass

        after = EffectiveCalendar(service)
        touched = [p["uid"] for p in patches]
        if uid and uid not in touched:
            touched.append(uid)
        return {
            "proposal": result.get("proposal"),
            "workout": after.workout(uid),
            # A swap moves two sessions; the caller has to be able to redraw
            # both, and to see that both actually moved.
            "workouts": [w for w in (after.workout(u) for u in touched)
                         if w is not None],
            "applied": [{"uid": p["uid"], "op": p.get("op"),
                         "summary": p.get("summary"),
                         "changes": p.get("changes")} for p in patches],
        }

    @app.post("/api/proposals/{proposal_id}/reject")
    async def reject_proposal(proposal_id: str, request: Request):
        body = await read_body(request)
        store = service.store
        store_module, _ = load_module("plan_store")
        try:
            proposal = store.reject_proposal(
                proposal_id, reason=body.get("reason"), actor="user")
        except Exception as exc:
            raise _translate_store_error(exc, store_module)
        return {"proposal": proposal}

    # -- push -------------------------------------------------------------

    @app.post("/api/push", status_code=202)
    async def post_push(request: Request):
        body = await read_body(request)
        start = body.get("start") or today_local().isoformat()
        end = body.get("end") or (today_local() + timedelta(days=6)).isoformat()
        parse_date(start, "start")
        parse_date(end, "end")
        select = body.get("select") or "push_approved"
        if select not in PUSH_SELECT_MODES:
            raise ApiError("invalid_range",
                           f"select must be one of {', '.join(PUSH_SELECT_MODES)}.",
                           status=400)
        dry_run = bool(body.get("dry_run"))

        if not dry_run:
            if service.demo:
                raise ApiError(
                    "not_configured",
                    "Demo mode never writes to Garmin. Restart without --demo "
                    "and add your Garmin login to push for real.", status=503)
            if not service.garmin_configured():
                raise ApiError("not_configured",
                               NOT_CONFIGURED_MESSAGES["garmin"], status=503)
            running = service.active_job_id()
            if running:
                raise ApiError("push_in_flight",
                               "A push is already running.", status=409,
                               detail={"job_id": running})

        calendar = EffectiveCalendar(service)
        workouts = _select_uids(calendar, start, end, select, body.get("uids"))
        items = build_push_items(service, calendar, workouts, dry_run)

        job = {
            "job_id": _new_id("psh"),
            "state": "done" if dry_run else "queued",
            "dry_run": dry_run,
            "range": {"start": start, "end": end},
            "select": select,
            "created_at": _utc_now(),
            "started_at": _utc_now() if dry_run else None,
            "finished_at": _utc_now() if dry_run else None,
            "progress": {"done": len(items) if dry_run else 0,
                         "total": len(items)},
            "items": items,
            "summary": _summarise(items) if dry_run else {
                "created": 0, "rescheduled": 0, "updated": 0,
                "skipped": 0, "failed": 0, "noop": 0},
            "note": ("Sync your Edge 520 with Garmin Connect to get these on "
                     "the device."),
        }
        service.record_job(job)

        if not dry_run:
            service._active_job = job["job_id"]
            thread = threading.Thread(target=run_push_job,
                                      args=(service, job["job_id"]),
                                      daemon=True)
            thread.start()
        return job

    @app.get("/api/push")
    def list_push(limit: int = Query(20, ge=1, le=PUSH_LOG_LIMIT)):
        return {"jobs": [
            {"job_id": j["job_id"], "state": j["state"],
             "started_at": j.get("started_at"),
             "finished_at": j.get("finished_at"),
             "dry_run": j.get("dry_run", False),
             "summary": j.get("summary")}
            for j in service.list_jobs(limit)]}

    @app.get("/api/push/{job_id}")
    def get_push(job_id: str):
        job = service.get_job(job_id)
        if job is None:
            raise ApiError("unknown_uid", "No push job with that id.", status=404)
        return job

    @app.delete("/api/push/{job_id}")
    def cancel_push(job_id: str):
        job = service.get_job(job_id)
        if job is None:
            raise ApiError("unknown_uid", "No push job with that id.", status=404)
        if job["state"] in ("queued", "running"):
            job["state"] = "cancelled"
            job["finished_at"] = _utc_now()
            # Best effort: anything already sent to Garmin stays sent.
            service.record_job(job)
        return job

    # -- history / undo ---------------------------------------------------

    @app.get("/api/history")
    def get_history(limit: int = Query(50, ge=1, le=500)):
        store = service.store_or_none()
        if store is None:
            return {"entries": []}
        try:
            return {"entries": store.history(limit=limit) or []}
        except Exception:
            return {"entries": []}

    @app.post("/api/history/{entry_id}/undo")
    def undo_history(entry_id: str):
        store = service.store
        store_module, _ = load_module("plan_store")

        # Read the audit entry first: `undo` returns the *restored value*,
        # which is legitimately null when the undo removed something (an
        # availability flag that had no prior state). Without the entry the
        # UI wouldn't know which cell to re-render.
        target = None
        try:
            for candidate in store.history(limit=500) or []:
                if candidate.get("entry_id") == entry_id:
                    target = candidate
                    break
        except Exception:
            target = None

        try:
            restored = store.undo(entry_id, actor="user")
        except Exception as exc:
            raise _translate_store_error(exc, store_module)

        action = (target or {}).get("action") or ""
        subject = (target or {}).get("uid")
        calendar = EffectiveCalendar(service)
        workout = day = None
        if action.startswith("availability"):
            if subject:
                day = _day_entry(calendar, subject)
        elif subject:
            workout = calendar.workout(subject)

        return {
            "undone": {
                "entry_id": entry_id,
                "action": action or None,
                "uid": subject,
                "actor": (target or {}).get("actor"),
            },
            "restored": restored,
            "workout": workout,
            "day": day,
        }

    # -- static front-end -------------------------------------------------

    os.makedirs(STATIC_DIR, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        page = os.path.join(STATIC_DIR, "index.html")
        if os.path.exists(page):
            return FileResponse(page)
        return HTMLResponse(_PLACEHOLDER_PAGE, status_code=200)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        icon = os.path.join(STATIC_DIR, "favicon.ico")
        if os.path.exists(icon):
            return FileResponse(icon)
        return HTMLResponse("", status_code=204)

    return app


_PLACEHOLDER_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Training Calendar</title>
<style>
 :root{color-scheme:light dark}
 body{font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
      margin:0;padding:3rem 1.5rem;display:flex;justify-content:center}
 main{max-width:34rem}
 h1{font-size:1.35rem;margin:0 0 .75rem}
 code{background:rgba(128,128,128,.16);padding:.1em .35em;border-radius:4px}
 ul{padding-left:1.1rem}
</style></head><body><main>
<h1>Training Calendar backend is running</h1>
<p>The front-end hasn't been placed in <code>static/index.html</code> yet.
The API is live in the meantime:</p>
<ul>
  <li><a href="/api/status">/api/status</a> — configuration &amp; health</li>
  <li><code>/api/calendar?start=…&amp;end=…</code> — the whole view in one call</li>
  <li><a href="/api/docs">/api/docs</a> — interactive API docs</li>
</ul>
</main></body></html>
"""


app = None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Training calendar backend (TrainingPeaks read-only, "
                    "Garmin Connect write-only).")
    parser.add_argument("--demo", action="store_true",
                        help="Serve realistic sample data. No credentials "
                             "needed and nothing is ever sent to Garmin.")
    parser.add_argument("--host", default=os.environ.get(
        "TRAINING_SYNC_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get(
        "TRAINING_SYNC_PORT", "8765")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    if args.demo:
        os.environ["TRAINING_SYNC_DEMO"] = "1"

    import uvicorn

    global app
    app = create_app(demo=args.demo or None)
    mode = "demo data" if app.state.service.demo else "your .env"
    print(f"Training calendar on http://{args.host}:{args.port}  ({mode})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
else:
    app = create_app()
