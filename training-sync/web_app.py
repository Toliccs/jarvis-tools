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
import inspect
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

# Writing to the athlete's real Garmin account is opt-in, per machine, and off
# unless this is explicitly set. Credentials in .env are what makes a push
# *possible*; this is what makes it *intended*. Dry-run preview is unaffected —
# it makes no network call at all — so the app is fully usable with writes off.
ALLOW_GARMIN_WRITES_ENV = "TRAINING_SYNC_ALLOW_GARMIN_WRITES"
WRITES_DISABLED_MESSAGE = (
    "Writing to Garmin is switched off on this machine. Preview still works; "
    f"to send for real, set {ALLOW_GARMIN_WRITES_ENV}=1 in training-sync/.env "
    "and restart.")

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
            # Push identity the last load refused to inherit. The store makes
            # this visible three ways on purpose — a log line, the audit
            # history, and here — because it means a session this app used to
            # believe it owned on Garmin is now unmanaged, and the athlete may
            # be looking at a copy nothing here will touch again. Zero for any
            # file this schema wrote, so a non-zero count is worth showing.
            "migration_dropped": stats.get("migration_dropped", 0),
            "migration_notes": [
                {k: note.get(k) for k in
                 ("uid", "reason", "workout_id", "schedule_id",
                  "scheduled_for", "conflict_with")}
                for note in (stats.get("migration_notes") or [])
            ],
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

    @staticmethod
    def garmin_writes_opted_in():
        """The explicit "yes, this machine may write to my Garmin account".

        Read on every call rather than captured at startup, so flipping it in
        the environment doesn't need a code change to take effect in tests.
        """
        return os.environ.get(ALLOW_GARMIN_WRITES_ENV, "").strip().lower() in (
            "1", "true", "yes", "on")

    def garmin_writes_allowed(self):
        """Demo never writes; nor does a machine that hasn't opted in."""
        return bool(not self.demo and self.garmin_configured()
                    and self.garmin_writes_opted_in())

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
                    # Marker stripped. Every workout this app writes carries
                    # `[ts:xxxxxxxx]` in its Garmin name so a later push can
                    # prove the copy is its own; that is bookkeeping, and this
                    # value goes straight onto the athlete's calendar. The
                    # writer owns the stripping, so ownership and display
                    # can't drift apart — and note ownership below is proved
                    # against the store, never against the stripped name.
                    "title": module.display_title(entry),
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
            # Separate from the above on purpose: credentials say a push is
            # *possible*, the opt-in says it is *intended*. The UI needs both
            # so it can offer the preview and explain why Send is off.
            "can_write_to_garmin": self.garmin_writes_allowed(),
            "garmin_writes_opted_in": self.garmin_writes_opted_in(),
            "garmin_writes_disabled_reason": (
                None if self.garmin_writes_allowed() or self.demo
                or not sources["garmin"]["configured"]
                else WRITES_DISABLED_MESSAGE),
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


def push_record(store, uid, record=None):
    """What this app has on Garmin for `uid`, whichever way the store keeps it.

    `PlanStore.get_push` is the accessor to ask; the push identity is not part
    of the athlete's edit and is being moved out of the override record into
    its own map. Reading it off the override still works while it lives there
    and costs nothing once it doesn't.
    """
    if store is not None:
        try:
            block = store.get_push(uid)
        except Exception:
            block = None
        if block:
            return dict(block)
    return dict((record or {}).get("garmin") or {})


def _on_garmin(garmin):
    """True when this app has a copy of the session on the athlete's Garmin
    calendar — the condition that makes "Remove from Garmin" meaningful, and
    the only thing that makes it permitted.

    Either id is enough. A push that landed but never got a schedule id back
    is still a copy on the calendar, and the writer can recover the missing id
    from the month; treating that case as "nothing there" would strand the
    session on the Edge with no way off.
    """
    garmin = garmin or {}
    return bool(garmin.get("workout_id") or garmin.get("schedule_id"))


def _push_state_for(garmin, effective_date):
    garmin = garmin or {}
    state = garmin.get("push_state") or "never"
    if state == "scheduled" and garmin.get("scheduled_for") \
            and garmin.get("scheduled_for") != effective_date:
        return "drifted"
    return state


def build_effective_workout(entry, record, availability, today,
                            orphaned=False, garmin=None):
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

    if garmin is None:
        garmin = (record or {}).get("garmin") or {}
    override_block = None
    # A uid the athlete has never edited still projects an override record
    # while Garmin is holding a copy of it — plan_store flags that projection
    # `push_only`. That residue is Garmin bookkeeping, not one of the
    # athlete's edits, and it is reported in `garmin` already. Presenting it
    # as an override would leave "Revert to the coach's plan" offered on a
    # workout that is already exactly the coach's plan.
    if record and not (record.get("push_only") and not fields):
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
            "push_state": _push_state_for(garmin, effective_date),
            "pushed_at": garmin.get("pushed_at"),
            "scheduled_for": garmin.get("scheduled_for"),
            "error": redact(garmin.get("error")),
            # "there is a copy of this on the athlete's calendar right now",
            # which is not the same question as push_state: a "failed" or
            # "drifted" session is still sitting on the Edge, and a "never"
            # one never was. This is what the UI offers removal on.
            "on_garmin": _on_garmin(garmin),
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
        self._pushes = {}
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

    def push_for(self, uid):
        """The Garmin push record, read once per calendar and cached."""
        if uid not in self._pushes:
            self._pushes[uid] = push_record(self.store, uid,
                                            self.overrides.get(uid))
        return self._pushes[uid]

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
                                           self.today, orphaned=True,
                                           garmin=self.push_for(uid))
        return build_effective_workout(entry, record, self.availability,
                                       self.today, garmin=self.push_for(uid))

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


def _proposal_premise(proposal_dict, patches, calendar):
    """The situation this suggestion argues from, recorded as it is made.

    A proposal is never a bare instruction — it is "you've marked Tuesday
    unavailable, so this session has to move". Cancel the trip and the
    argument is gone, but the suggestion isn't: it sits on the calendar with a
    confident, now-false sentence under it, and approving it still moves the
    session. So what has to remain true is written down here and re-checked on
    every read.

    Only the *primary* workout is pinned. A second session edited by hand is
    the conflict case, which already has its own, more useful answer.
    """
    uid = proposal_dict.get("workout_uid")
    days = [proposal_dict.get("original_date")]
    days += [patch.get("from_date") for patch in patches or []]
    blocked = sorted({
        day for day in days
        if day and (calendar.availability.get(day) or {}).get("state")
        == "unavailable"
    })
    workout = calendar.workout(uid) if uid else None
    return {
        "uid": uid,
        "workout_date": (workout or {}).get("date")
        or proposal_dict.get("original_date"),
        "unavailable_days": blocked,
    }


def proposal_stale_reason(proposal, calendar):
    """Why this suggestion no longer applies, in his words — or None while it
    still does.

    Read from the premise recorded when the suggestion was made. A proposal
    stored before premises existed carries none, and is left alone: inventing
    one would withdraw a suggestion that may be perfectly live.
    """
    premise = proposal.get("premise")
    if not isinstance(premise, dict):
        return None
    for day in premise.get("unavailable_days") or []:
        state = (calendar.availability.get(day) or {}).get("state")
        if state != "unavailable":
            return (f"You're free on {_day_label(day)} again, so the reason "
                    f"for this has gone.")
    uid = premise.get("uid") or proposal.get("uid")
    if not uid:
        return None
    workout = calendar.workout(uid)
    if workout is None:
        return "That session has left your plan, so this no longer applies."
    if workout.get("status") in ("skipped", "done"):
        return ("You've already dealt with that session, so this no longer "
                "applies.")
    was = premise.get("workout_date")
    if was and workout.get("date") != was:
        return (f"You've since moved that session to "
                f"{_day_label(workout.get('date'))}, so this no longer "
                f"applies.")
    return None


def mark_stale(proposals, calendar):
    """Tag every proposal with whether its premise still holds.

    Stale ones are kept and labelled rather than deleted — a suggestion that
    quietly vanishes is as confusing as one that lies. What they may not do is
    apply: the approve handler refuses them, force or not.

    Only undecided ones are tested. A proposal he already approved moved the
    session, which is precisely the change that would read as a broken
    premise; it is history, not an offer, and must not be labelled stale.
    """
    out = []
    for proposal in proposals or []:
        reason = None
        if proposal.get("status") == "pending":
            try:
                reason = proposal_stale_reason(proposal, calendar)
            except Exception:
                reason = None
        out.append(dict(proposal, stale=bool(reason), stale_reason=reason))
    return out


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
        # What has to still be true for that rationale to still be true.
        "premise": _proposal_premise(proposal_dict, patches, calendar),
        "stale": False,
        "stale_reason": None,
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

    One is stale the moment its situation changes, and "the situation" is not
    only the workout. "I can't place this anywhere" was an answer about a week
    the athlete had blocked out; unblock the days and the sentence is no
    longer true, so the amber flag has to go with them. Checking only the
    workout left six of them burning on the calendar forever after the trip
    was cancelled.
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
        # The same premise-still-holds test the proposals get. Items written
        # before premises existed carry none and fall through to the checks
        # above, exactly as they did before.
        try:
            if proposal_stale_reason(item, calendar):
                continue
        except Exception:
            pass
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


def _last_entry_id(store, uid):
    """The id of the audit entry the write just made — the handle a rollback
    needs in order to take back exactly that write and nothing else."""
    try:
        entries = store.history(limit=1, uid=uid) or []
    except Exception:
        return None
    return entries[0].get("entry_id") if entries else None


def _restore_push_state(store, uid, before):
    """Put the Garmin push state back to what it was before a rolled-back write.

    Undo restores the athlete's edit. Whether the *push* state travels with it
    depends on where the store keeps the push record, so it is checked and
    repaired here rather than assumed. What must not survive a rollback is
    "drifted": that state is terminal, because a re-push matches the stored
    hash, takes the noop branch and returns before it can record anything new
    — so the "changed since it was pushed" warning would never clear again.

    One residue: `record_push` re-stamps `pushed_at` when it writes a
    "scheduled" state, and offers no way to say "this landed earlier", so a
    repaired record reads as pushed at the moment of the rollback rather than
    the moment of the push. `pushed_at` is passed through here already, and
    the moment the store accepts it the residue goes with it.

    "never" counts as *no* push record, not as a state to restore. Every
    override the store hands out now carries a `garmin` projection, and for a
    workout that was never sent that projection is a blank one whose
    `push_state` is "never" — truthy, and enough to get past a bare "is there
    anything here?" test. Restoring it called `record_push`, which wrote a
    real push entry and a ('push', uid) line in the audit log for a workout
    this app has never sent anywhere. Nothing reached the calendar, but the
    athlete's history claimed a push that did not happen.
    """
    if not before or not before.get("push_state") \
            or before.get("push_state") == "never":
        return
    try:
        after = store.get_push(uid) or {}
    except Exception:
        return
    if after.get("push_state") == before.get("push_state"):
        return
    # push_state is the whole point of the repair, so it is always passed
    # explicitly — never left to a default that would invent a state the
    # workout was never in. The rest is passed only where the store's
    # `record_push` takes it; its shape is being reworked underneath this file.
    kwargs = {"push_state": before.get("push_state"), "actor": "system"}
    optional = ("workout_id", "schedule_id", "scheduled_for", "pushed_hash",
                "error", "pushed_at")
    try:
        accepted = set(inspect.signature(store.record_push).parameters)
    except (TypeError, ValueError):
        accepted = set(optional)
    kwargs.update({key: before.get(key) for key in optional
                   if key in accepted})
    try:
        store.record_push(uid, **kwargs)
    except Exception:
        pass


def _rollback_patches(store, applied):
    """Take the operations already written back, newest first.

    A half-applied swap is worse than no swap: one session moved, the other
    left behind, and a double-booked day. If any operation can't be written,
    every one before it is put back.

    Each is reversed through the store's own undo, which restores the whole
    prior record. Re-patching the fields back looked like a fresh user edit,
    and a user edit flips an already-pushed workout from "scheduled" to
    "drifted" — permanently, for the reason in `_restore_push_state`. A
    rollback is a compensating action, not an edit: it has to leave the
    workout exactly as it found it, push state included.
    """
    for uid, entry_id, push_before in reversed(applied):
        try:
            if entry_id:
                store.undo(entry_id, actor="system")
            _restore_push_state(store, uid, push_before)
        except Exception:
            # Best effort — a rollback failure must not mask the original
            # error, which is what the athlete actually needs to read.
            pass


def _primary_write(proposal):
    """The field patch approving this proposal would write to its own workout,
    derived exactly as ``PlanStore.approve_proposal`` derives it — including
    its fallback to `to_date` for a proposal that carries no explicit changes.

    Asking "would approving this write anything?" any other way risks
    answering no while the store goes on to write something.
    """
    changes = dict(proposal.get("changes") or {})
    if not changes and proposal.get("to_date"):
        changes["date"] = proposal["to_date"]
    if changes.get("date"):
        changes.setdefault("status", "moved")
    return changes


def _decide_without_writing(store, proposal_id):
    """Mark a proposal approved without touching the workout record at all.

    The store materialises every approval as a patch, and a patch is never
    free even when its field set is empty: it bumps the rev and appends an
    undoable history entry. On a workout the athlete already owns — he ticked
    "approve for push", say — that entry becomes the newest change to the uid
    and retires his own, so his Undo silently stops offering the edit he
    actually made and starts refusing it with "there's a newer change".

    A KEEP writes nothing, so nothing is written: the decision is recorded on
    the proposal alone. The store exposes no write-free approve, hence the
    narrow reach past its accessors here; if it ever grows one, or its state
    is shaped differently, this falls back to the normal path rather than
    losing the athlete's decision.
    """
    lock = getattr(store, "_lock", None)
    state = getattr(store, "_state", None)
    proposals = state.get("proposals") if isinstance(state, dict) else None
    if lock is None or not isinstance(proposals, list):
        return (store.approve_proposal(proposal_id, actor="user")
                or {}).get("proposal")
    with lock:
        for record in proposals:
            if record.get("proposal_id") != proposal_id:
                continue
            record["status"] = "approved"
            record["decided_at"] = _utc_now()
            store.save()
            return copy.deepcopy(record)
    raise ApiError("unknown_uid", "That suggestion no longer exists.",
                   status=404)


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
    """Returns ``(to_push, refused)``.

    Approving a plan change and sending it to the device are two separate
    consents, so a workout without `push_approved` is never sent — not by an
    explicit uid list and not by "push everything". "Everything" means every
    session the athlete has approved; the rest come back in `refused` and are
    reported row by row with the reason, rather than being silently included
    or silently dropped.
    """
    in_range = calendar.in_range(start, end)
    if select == "explicit":
        wanted = set(uids or [])
        candidates = [w for w in in_range if w["uid"] in wanted]
    else:
        candidates = list(in_range)
    approved = [w for w in candidates if w["push_approved"]]
    if select == "push_approved":
        # The caller asked for the approved set by name; everything else in
        # the week was never a candidate and reporting it would be noise.
        return approved, []
    return approved, [w for w in candidates if not w["push_approved"]]


def _not_approved_item(workout):
    """A push row for a session the caller asked for but hasn't approved."""
    return {"uid": workout["uid"], "date": workout["date"],
            "title": workout["title"], "action": "skip",
            "reason": "Not approved for push — tick “Approve for push” on the "
                      "session first.",
            "state": "skipped", "garmin_workout_id": None,
            "garmin_schedule_id": None, "error": None, "structured": False,
            "on_garmin": _on_garmin(workout.get("garmin"))}


# Said on a push row for a session the athlete has taken off his week while a
# copy of it is still sitting on his Garmin calendar. Skipping is a local
# decision and never unschedules anything by itself — so the row has to name
# the copy and point at the one thing that removes it, rather than reporting
# "skipped" and leaving the session on the Edge.
STILL_ON_GARMIN = ("Marked skipped — but this app's copy is still on your "
                   "Garmin calendar. Use “Remove from Garmin” on the session "
                   "to take it off.")


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
        garmin = calendar.push_for(workout["uid"])
        on_garmin = _on_garmin(garmin)
        if workout["status"] == "skipped":
            # A skip is a local decision; it never takes anything off the
            # calendar on its own. Where a copy is still there, say so and
            # carry its ids, so the row can offer the removal rather than
            # ending the story at "skipped".
            items.append({"uid": workout["uid"], "date": workout["date"],
                          "title": workout["title"], "action": "skip",
                          "reason": (STILL_ON_GARMIN if on_garmin
                                     else "Marked skipped."),
                          "state": "skipped",
                          "garmin_workout_id": garmin.get("workout_id"),
                          "garmin_schedule_id": garmin.get("schedule_id"),
                          "error": None, "structured": False,
                          "on_garmin": on_garmin})
            continue
        if workout["date"] < today:
            items.append({"uid": workout["uid"], "date": workout["date"],
                          "title": workout["title"], "action": "skip",
                          "reason": "In the past — not sent to the Edge.",
                          "state": "skipped",
                          "garmin_workout_id": garmin.get("workout_id"),
                          "garmin_schedule_id": garmin.get("schedule_id"),
                          "error": None, "structured": False,
                          "on_garmin": on_garmin})
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
            row["on_garmin"] = on_garmin
            items.append(row)
        except Exception as exc:
            items.append({"uid": workout["uid"], "date": workout["date"],
                          "title": workout["title"], "action": "create",
                          "reason": None, "state": "failed",
                          "garmin_workout_id": None, "garmin_schedule_id": None,
                          "error": redact(str(exc)), "structured": False,
                          "on_garmin": on_garmin})
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

    # The last gate before the only code in this app that writes to the
    # athlete's Garmin account. The endpoint checks it too; this one is on the
    # write path itself, so no future caller can route around it.
    if not service.garmin_writes_allowed():
        job["state"] = "failed"
        job["finished_at"] = _utc_now()
        job["error"] = {"code": "writes_disabled",
                        "message": WRITES_DISABLED_MESSAGE}
        service.record_job(job)
        return

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
        if not workout["push_approved"]:
            # Approval can be withdrawn between the job being built and being
            # run. The rule lives here, next to the write, and not only where
            # the list was chosen.
            job["items"][index] = _not_approved_item(workout)
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
            row = result.to_dict()
            job["items"][index] = dict(
                row,
                on_garmin=_on_garmin({
                    "workout_id": row.get("garmin_workout_id"),
                    "schedule_id": row.get("garmin_schedule_id")}),
                error=({"code": "garmin_rejected",
                        "message": redact(result.error)}
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
# removal: taking a session back off the Garmin calendar
#
# The other half of push, and the only other thing in this app that writes to
# the athlete's Garmin account. Until this existed, a session that reached the
# Edge stayed there: skipping it, dropping it or reverting it changed the local
# plan and nothing else, and the push-only record the store keeps for it was
# exactly as permanent as the v1 tombstone it replaced.
#
# One synchronous call, not a job: it is one session, the athlete pressed the
# button, and he is looking at the answer.
# ---------------------------------------------------------------------------


def run_unpush(service, uid):
    """Unschedule this app's copy of `uid` and forget the push record.

    `unschedule` is deliberately the whole of it. The workout template stays
    in the athlete's Garmin library — taking a session off a calendar and
    destroying a template are different acts, and only the first is ours to
    make. Ownership is proved the same way a push proves it: only ids this
    app recorded are touched, so a session the athlete built in Garmin Connect
    is never at risk.

    The env gate is re-checked here, on the write path itself, exactly as
    `run_push_job` does. The endpoint's check is what the athlete reads; this
    one is so no future caller can route around it.
    """
    if not service.garmin_writes_allowed():
        raise ApiError("writes_disabled", WRITES_DISABLED_MESSAGE, status=403,
                       detail={"env_var": ALLOW_GARMIN_WRITES_ENV})

    module, _ = load_module("garmin_workout_writer")
    writer = service.writer()
    try:
        result = writer.unpush_workout(uid, store=service.store)
    except Exception as exc:
        not_configured = module is not None and isinstance(
            exc, getattr(module, "GarminNotConfigured", ()))
        auth = module is not None and isinstance(
            exc, getattr(module, "GarminAuthFailed", ()))
        raise ApiError(
            "not_configured" if not_configured else "garmin_rejected",
            redact(str(exc)), status=503 if (not_configured or auth) else 502)

    if result.state == "failed":
        # The writer has already recorded the failure against the push record,
        # which must survive: the copy is still on the calendar, and saying
        # otherwise would lose the only handle left on it.
        raise ApiError(
            "garmin_rejected",
            redact(result.error) or "Garmin refused to remove that session.",
            status=502, detail={"uid": uid})
    return result.to_dict()


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
                pending = calendar.store.list_proposals(
                    status="pending", start=start, end=end) or []
                # Every suggestion carries whether its premise still holds, so
                # the calendar can never print a confident reason that has
                # since become false.
                for proposal in mark_stale(pending, calendar):
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
                proposals = mark_stale(calendar.store.list_proposals(
                    status="pending", start=day, end=day) or [], calendar)
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

    @app.post("/api/workouts/{uid}/unpush")
    def unpush_workout(uid: str):
        """Take this app's copy of a session back off the Garmin calendar.

        A Garmin *write*, and the only one besides push, so it carries the
        same consents in the same order: this app put the session there, the
        account is connected, this machine is allowed to write at all, and the
        athlete asked for this session by name. Nothing calls it
        automatically — skipping or dropping a session is *reported* as
        leaving a copy on the calendar, never used as licence to unschedule
        one.

        The refusals come before the write, cheapest and most specific first,
        so a session that was never ours is turned away without a single
        Garmin call.
        """
        calendar = EffectiveCalendar(service)
        workout = calendar.workout(uid)
        if workout is None:
            raise ApiError(
                "unknown_uid",
                "That workout isn't on your plan or in your saved changes.",
                status=404)
        if not _on_garmin(calendar.push_for(uid)):
            raise ApiError(
                "not_pushed",
                "This app hasn't put that session on your Garmin calendar, so "
                "there's nothing of ours to take off it.",
                status=409, detail={"uid": uid})
        if service.demo:
            raise ApiError(
                "not_configured",
                "Demo mode never writes to Garmin — including removals. "
                "Restart without --demo and add your Garmin login to remove "
                "it for real.", status=503)
        if not service.garmin_configured():
            raise ApiError("not_configured", NOT_CONFIGURED_MESSAGES["garmin"],
                           status=503)
        if not service.garmin_writes_opted_in():
            raise ApiError("writes_disabled", WRITES_DISABLED_MESSAGE,
                           status=403,
                           detail={"env_var": ALLOW_GARMIN_WRITES_ENV})
        running = service.active_job_id()
        if running:
            # A push runs on its own thread and writes the same push record.
            # Racing it is the one way to end up with a copy on the calendar
            # and no id left to remove it by — the exact orphan this endpoint
            # exists to prevent. Two writers, one record: serialise them.
            raise ApiError(
                "push_in_flight",
                "A push is running. Wait for it to finish before removing a "
                "session — otherwise the two could disagree about what's on "
                "your calendar.", status=409, detail={"job_id": running})

        result = run_unpush(service, uid)
        # The month read behind `days[].garmin.scheduled` is cached for the
        # life of the context. A session the athlete removed a second ago must
        # not still be listed there — that is precisely the "job said success,
        # calendar says otherwise" confusion this whole path exists to end.
        with service._lock:
            service._garmin_context = None
            service._garmin_context_at = 0.0
        return {"result": result,
                "workout": EffectiveCalendar(service).workout(uid)}

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
            "proposals": mark_stale(stored, calendar),
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
        calendar = EffectiveCalendar(service)
        try:
            proposals = mark_stale(store.list_proposals(
                status=status or None, start=start, end=end) or [], calendar)
        except Exception:
            proposals = []
        try:
            unresolved = live_unresolved(service, calendar, start, end)
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

        # A suggestion whose premise has gone must never apply, force or not:
        # it would move a session for a reason the athlete can read on the
        # card and knows to be false.
        stale = proposal_stale_reason(proposal, calendar)
        if stale:
            raise ApiError(
                "proposal_stale",
                stale + " Ask Alfred again for a suggestion that fits how the "
                        "week looks now.",
                status=409, detail={"uid": uid, "stale": True})

        patches = proposal_patches(proposal)
        # What the store itself would write for the primary workout, derived
        # by the store's own rule (it falls back to `to_date` when a proposal
        # carries no explicit changes) — so "does approving this write
        # anything?" is answered by the same rule that would do the writing.
        primary_changes = _primary_write(proposal)
        # The primary workout is written by the store when it marks the
        # proposal approved; everything else is written here, in the engine's
        # order, and the whole set stands or falls together.
        secondary = [p for p in patches if p["uid"] != uid]

        # A KEEP has nothing to write at all — "I left the race where it is"
        # is an answer, not an edit — and so it never enters the write path.
        # An empty patch is not free: it bumps the rev and appends an undoable
        # entry, which retires the athlete's *own* last change. Approving a
        # suggestion that alters nothing used to eat his undo.
        if not primary_changes and not patches:
            decided = _decide_without_writing(store, proposal_id)
            after = EffectiveCalendar(service)
            return {"proposal": decided, "workout": after.workout(uid),
                    "workouts": [w for w in [after.workout(uid)] if w],
                    "applied": []}

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
                push_before = calendar.push_for(patch["uid"])
                store.patch_workout(
                    patch["uid"], fields=dict(patch["changes"]),
                    note=patch.get("note"), kind="alfred",
                    plan_snapshot=calendar.snapshot_for(patch["uid"]),
                    actor="user", force=force, action="approve_proposal")
                applied.append((patch["uid"],
                                _last_entry_id(store, patch["uid"]),
                                push_before))
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
            if not service.garmin_writes_opted_in():
                # Credentials make a write possible; this makes it intended.
                # An accidental run — a stray curl, a script, a browser tab
                # left open — must not reach the athlete's Garmin account.
                raise ApiError("writes_disabled", WRITES_DISABLED_MESSAGE,
                               status=403,
                               detail={"env_var": ALLOW_GARMIN_WRITES_ENV})
            running = service.active_job_id()
            if running:
                raise ApiError("push_in_flight",
                               "A push is already running.", status=409,
                               detail={"job_id": running})

        calendar = EffectiveCalendar(service)
        workouts, refused = _select_uids(calendar, start, end, select,
                                         body.get("uids"))
        items = build_push_items(service, calendar, workouts, dry_run)
        # Listed, not dropped: "I asked for the whole week and 4 went" is only
        # honest if the other 3 say why they didn't.
        items += [_not_approved_item(w) for w in refused]

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
            "not_approved": {"count": len(refused),
                             "uids": [w["uid"] for w in refused]},
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
