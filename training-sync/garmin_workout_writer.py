"""Builds structured workouts and writes them to Garmin Connect.

Garmin Connect is the *only* write target in this project. A workout
scheduled here shows up on the Garmin calendar and lands on the Edge 520 the
next time the head unit syncs — which is why every status this module
returns distinguishes "accepted by Garmin Connect" from "on your bike".
Nothing here touches TrainingPeaks; that feed is read-only input.

Two things about the underlying `garminconnect` API drive the design:

- `schedule_workout(workout_id, date)` and `unschedule_workout(schedule_id)`
  take *different* ids. Both must be persisted or a moved workout can't be
  cleaned up and you end up with duplicates on the device.
- `get_scheduled_workouts(year, month)` is month-keyed, so looking up an
  existing schedule means fanning out over the months a range spans.

Two rules govern everything this module writes:

- It never destroys anything. There is no `delete` here at all: rewriting a
  session uploads a new template and unschedules the old calendar entry,
  leaving the superseded template in the athlete's Garmin library where it
  is harmless. A workout template is cheap; one the athlete built by hand
  and lost is not.
- It only claims a workout it can prove it wrote. Every workout created here
  carries an ownership marker keyed to the plan uid (`push_marker`), and the
  adopt path matches on that marker alone. Names repeat weekly and two
  sessions can share a title on one day, so a name proves nothing — matching
  on one is how this app came to adopt, and then overwrite, workouts that
  were never its own.

Credentials are read lazily. Importing this module never touches the
network or the environment, so the web layer can start and report "Garmin
not configured" rather than failing at import.
"""

import copy
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime

from dotenv import load_dotenv

# Same as the other modules here: read .env at import so credential checks
# see the real config. This touches no network and is safe when .env is
# absent — the credential guard still raises lazily, never at import.
load_dotenv()

# --- Garmin type vocabulary -------------------------------------------------
# Mirrors garminconnect.workout's own constants so the raw dicts we build are
# byte-compatible with what the typed models emit — but without requiring
# pydantic, which is an optional extra of that package.

SPORT_TYPES = {
    "cycling": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "running": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "swimming": {"sportTypeId": 3, "sportTypeKey": "swimming", "displayOrder": 3},
    "other": {"sportTypeId": 8, "sportTypeKey": "other", "displayOrder": 8},
}

STEP_TYPES = {
    "warmup": {"stepTypeId": 1, "stepTypeKey": "warmup", "displayOrder": 1},
    "cooldown": {"stepTypeId": 2, "stepTypeKey": "cooldown", "displayOrder": 2},
    "interval": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
    "recovery": {"stepTypeId": 4, "stepTypeKey": "recovery", "displayOrder": 4},
    "rest": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
    "repeat": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
}

# `time` and `iterations` are exactly what garminconnect's own step helpers
# emit, so they are the values with the strongest evidence behind them.
# `lap.button` is Garmin's standard open-ended condition, used only for steps
# with no duration.
CONDITION_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time",
                  "displayOrder": 2, "displayable": True}
CONDITION_ITERATIONS = {"conditionTypeId": 7, "conditionTypeKey": "iterations",
                        "displayOrder": 7, "displayable": False}
CONDITION_LAP_BUTTON = {"conditionTypeId": 1, "conditionTypeKey": "lap.button",
                        "displayOrder": 1, "displayable": True}

TARGET_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target",
               "displayOrder": 1}
TARGET_POWER = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone",
                "displayOrder": 2}
TARGET_CADENCE = {"workoutTargetTypeId": 3, "workoutTargetTypeKey": "cadence",
                  "displayOrder": 3}
TARGET_HR = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone",
             "displayOrder": 4}

# Zone name -> (low %FTP, high %FTP). Used to turn "threshold" into watts when
# an FTP is known. Names cover both the coach's words and plain zone numbers.
ZONE_FTP_PCT = {
    "recovery": (0.30, 0.55), "z1": (0.30, 0.55),
    "endurance": (0.56, 0.75), "z2": (0.56, 0.75), "aerobic": (0.56, 0.75),
    "tempo": (0.76, 0.87), "z3": (0.76, 0.87),
    "sweetspot": (0.88, 0.94), "sweet_spot": (0.88, 0.94),
    "threshold": (0.95, 1.05), "z4": (0.95, 1.05), "ftp": (0.95, 1.05),
    "vo2": (1.06, 1.20), "vo2max": (1.06, 1.20), "z5": (1.06, 1.20),
    "anaerobic": (1.21, 1.50), "z6": (1.21, 1.50),
    "neuromuscular": (1.51, 2.00), "z7": (1.51, 2.00),
}

# Fallback when FTP is unknown: target Garmin's own power zone by number.
ZONE_NUMBER = {
    "recovery": 1, "z1": 1,
    "endurance": 2, "z2": 2, "aerobic": 2,
    "tempo": 3, "z3": 3,
    "sweetspot": 3, "sweet_spot": 3,
    "threshold": 4, "z4": 4, "ftp": 4,
    "vo2": 5, "vo2max": 5, "z5": 5,
    "anaerobic": 6, "z6": 6,
    "neuromuscular": 7, "z7": 7,
}


# --- ownership marker -------------------------------------------------------
# Garmin's calendar tells us a workout's name, its ids and its date. None of
# that distinguishes a session this app created from one the athlete built by
# hand, and the two need very different treatment: ours may be rewritten and
# unscheduled, theirs may not be touched at all.
#
# So every workout created here carries a marker in its name, derived from the
# plan uid. It is the only evidence of authorship that survives a round trip
# through Garmin, and it is per-uid, so it cannot merge two sessions that
# happen to share a title on one day.
#
# Absence of a marker is never treated as permission. An unmarked workout is
# somebody else's: we create our own beside it rather than claim it.

PUSH_MARKER_PREFIX = "ts"
MARKER_RE = re.compile(r"\[" + PUSH_MARKER_PREFIX + r":[0-9a-f]{8}\]")
MAX_WORKOUT_NAME = 80


def push_marker(uid):
    """The ownership marker this app writes for `uid`.

    A short digest rather than the raw uid: ICS uids are long and ugly, and
    this ends up on the athlete's head unit.
    """
    token = hashlib.sha1(
        f"training-sync|{uid}".encode("utf-8")).hexdigest()[:8]
    return f"[{PUSH_MARKER_PREFIX}:{token}]"


def marked_workout_name(title, uid, limit=MAX_WORKOUT_NAME):
    """The workout name as it goes to Garmin: the title plus our marker.

    The title is truncated rather than the marker — a clipped name is a
    cosmetic problem, a clipped marker is a workout we can no longer prove is
    ours and would therefore abandon on the calendar.
    """
    marker = push_marker(uid)
    base = " ".join(str(title or "").split()) or "Workout"
    room = limit - len(marker) - 1
    if room < 1:
        return marker[:limit]
    return f"{base[:room].rstrip()} {marker}"


def owns_workout(name, uid):
    """True only when `name` carries the marker this app writes for `uid`."""
    return push_marker(uid) in str(name or "")


def strip_marker(name):
    """The name with any ownership marker removed, for showing a person.

    The marker is bookkeeping. Anywhere a workout name read back off the
    Garmin calendar is displayed, put it through here first — the athlete
    should see the title their coach wrote, not our tag.
    """
    return MARKER_RE.sub("", str(name or "")).strip() or None


class GarminWriterError(Exception):
    """Base class for writer errors."""

    code = "garmin_writer_error"


class GarminNotConfigured(GarminWriterError):
    """Garmin credentials are absent. Raised on first use, never at import."""

    code = "not_configured"


class GarminAuthFailed(GarminWriterError):
    code = "garmin_auth_failed"


class GarminRejected(GarminWriterError):
    """Garmin accepted the request but refused the content."""

    code = "garmin_rejected"


def is_configured():
    """True when Garmin credentials are present in the environment."""
    return bool(os.environ.get("GARMIN_EMAIL")
                and os.environ.get("GARMIN_PASSWORD"))


# --- internal workout representation ---------------------------------------


@dataclass
class Step:
    """One executable step.

    duration_s=None means "until you press lap" — useful for an open warmup.
    A target is either a zone name (resolved against FTP when known), an
    explicit watt range, or nothing at all.
    """

    duration_s: int = None
    kind: str = "interval"          # warmup|interval|recovery|cooldown|rest
    zone: str = None                # "threshold", "z2", ...
    power_low: int = None           # explicit watts, wins over zone
    power_high: int = None
    cadence: int = None
    description: str = None

    def total_seconds(self):
        return int(self.duration_s or 0)


@dataclass
class Repeat:
    """A repeated block, e.g. 4x(5min hard + 5min easy)."""

    times: int
    steps: list = field(default_factory=list)

    def total_seconds(self):
        return int(self.times) * sum(s.total_seconds() for s in self.steps)


def _resolve_target(step, ftp=None):
    """Turn a Step's target fields into a Garmin targetType block."""
    low, high = step.power_low, step.power_high

    if low is None and high is None and step.zone:
        key = step.zone.strip().lower().replace(" ", "_")
        pct = ZONE_FTP_PCT.get(key)
        if pct and ftp:
            low = int(round(pct[0] * ftp))
            high = int(round(pct[1] * ftp))
        elif key in ZONE_NUMBER:
            # No FTP known — target the zone by number instead of inventing
            # watts. The Edge resolves it against the rider's own zones.
            target = dict(TARGET_POWER)
            return target, None, None, ZONE_NUMBER[key]

    if low is not None or high is not None:
        if low is None:
            low = high
        if high is None:
            high = low
        if low > high:
            low, high = high, low
        return dict(TARGET_POWER), int(low), int(high), None

    if step.cadence:
        return dict(TARGET_CADENCE), int(step.cadence), int(step.cadence), None

    return dict(TARGET_NONE), None, None, None


class _StepNumberer:
    def __init__(self):
        self.step_order = 0
        self.child_group = 0

    def next_order(self):
        self.step_order += 1
        return self.step_order

    def next_group(self):
        self.child_group += 1
        return self.child_group


def _build_executable(step, numberer, ftp, child_id=None):
    target, low, high, zone_number = _resolve_target(step, ftp)
    step_type = STEP_TYPES.get(step.kind, STEP_TYPES["interval"])

    if step.duration_s:
        end_condition = dict(CONDITION_TIME)
        end_value = float(int(step.duration_s))
    else:
        end_condition = dict(CONDITION_LAP_BUTTON)
        end_value = None

    payload = {
        "type": "ExecutableStepDTO",
        "stepOrder": numberer.next_order(),
        "stepType": dict(step_type),
        "endCondition": end_condition,
        "endConditionValue": end_value,
        "targetType": target,
    }
    if low is not None:
        payload["targetValueOne"] = low
    if high is not None:
        payload["targetValueTwo"] = high
    if zone_number is not None:
        payload["zoneNumber"] = zone_number
    if step.description:
        payload["description"] = step.description
    if child_id is not None:
        payload["childStepId"] = child_id
    return payload


def _build_repeat(block, numberer, ftp):
    group_id = numberer.next_group()
    payload = {
        "type": "RepeatGroupDTO",
        "stepOrder": numberer.next_order(),
        "stepType": dict(STEP_TYPES["repeat"]),
        "numberOfIterations": int(block.times),
        "endCondition": dict(CONDITION_ITERATIONS),
        "endConditionValue": float(int(block.times)),
        "smartRepeat": False,
        "childStepId": group_id,
        "workoutSteps": [
            _build_executable(child, numberer, ftp, child_id=group_id)
            for child in block.steps
        ],
    }
    return payload


def build_workout(name, steps, sport="cycling", description=None, ftp=None):
    """Build a Garmin workout payload from a list of Step / Repeat objects.

    Returns the dict that `upload_workout` accepts.
    """
    if not name or not str(name).strip():
        raise GarminWriterError("workout needs a name")
    steps = list(steps or [])
    if not steps:
        raise GarminWriterError("workout needs at least one step")

    sport_key = (sport or "cycling").lower()
    sport_type = SPORT_TYPES.get(sport_key, SPORT_TYPES["other"])
    ftp = ftp or _env_ftp()

    numberer = _StepNumberer()
    built = []
    for item in steps:
        if isinstance(item, Repeat):
            built.append(_build_repeat(item, numberer, ftp))
        elif isinstance(item, Step):
            built.append(_build_executable(item, numberer, ftp))
        else:
            raise GarminWriterError(
                f"steps must be Step or Repeat, got {type(item).__name__}")

    total = sum(item.total_seconds() for item in steps)

    payload = {
        "workoutName": str(name)[:80],
        "sportType": dict(sport_type),
        "estimatedDurationInSecs": int(total),
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": dict(sport_type),
                "workoutSteps": built,
            }
        ],
    }
    if description:
        payload["description"] = str(description)[:1024]
    return payload


def build_placeholder_workout(name, duration_s=None, sport="cycling",
                              description=None):
    """A duration-only workout: one open step, no targets.

    Most TrainingPeaks calendar entries carry no structured steps — only a
    title and free text. Rather than invent intervals, we send a timed
    placeholder so the Edge at least shows the right name and length. The
    caller must tell the user that targets did *not* sync.
    """
    step = Step(duration_s=int(duration_s) if duration_s else None,
                kind="interval", description=None)
    return build_workout(name, [step], sport=sport, description=description)


def _env_ftp():
    raw = os.environ.get("GARMIN_FTP") or os.environ.get("FTP")
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def workout_hash(payload):
    """Stable hash of the workout *content*.

    Deliberately excludes the date. Content and date are compared
    separately so that moving a workout to another day is recognised as a
    cheap reschedule of the same template, not a content change that would
    re-upload it. Folding the date in here would silently turn every drag
    into a duplicate template on Garmin.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


# --- results ----------------------------------------------------------------


@dataclass
class PushResult:
    """Per-workout outcome. Deliberately verbose: the UI shows `reason`
    verbatim, and `state` distinguishes Garmin Connect from the device."""

    uid: str
    title: str = None
    date: str = None
    action: str = "noop"        # create|reschedule|update|noop|skip|unschedule
    state: str = "pending"      # pending|done|failed|skipped
    workout_id: int = None
    schedule_id: int = None
    reason: str = None
    error: str = None
    structured: bool = False
    dry_run: bool = False

    def to_dict(self):
        return {
            "uid": self.uid,
            "title": self.title,
            "date": self.date,
            "action": self.action,
            "state": self.state,
            "garmin_workout_id": self.workout_id,
            "garmin_schedule_id": self.schedule_id,
            "reason": self.reason,
            "error": self.error,
            "structured": self.structured,
            "dry_run": self.dry_run,
        }

    @property
    def ok(self):
        return self.state in ("done", "skipped")


# --- response parsing -------------------------------------------------------

_WORKOUT_ID_KEYS = ("workoutId", "workout_id")
_SCHEDULE_ID_KEYS = ("workoutScheduleId", "scheduleId", "workout_schedule_id")
_NAME_KEYS = ("workoutName", "workout_name", "title")


def _iter_dicts(obj):
    """Walk arbitrarily nested JSON yielding every dict.

    Garmin's response envelopes vary between endpoints and versions; this
    lets the id extractors work without hardcoding one shape.
    """
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_dicts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_dicts(item)


def _coerce_id(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _extract_workout_id(response):
    for candidate in _iter_dicts(response):
        for key in _WORKOUT_ID_KEYS:
            found = _coerce_id(candidate.get(key))
            if found:
                return found
    # Some payloads return the workout object itself with a plain "id".
    if isinstance(response, dict):
        return _coerce_id(response.get("id"))
    return None


def _extract_schedule_id(response):
    for candidate in _iter_dicts(response):
        for key in _SCHEDULE_ID_KEYS:
            found = _coerce_id(candidate.get(key))
            if found:
                return found
    if isinstance(response, dict):
        return _coerce_id(response.get("id"))
    return None


# Every spelling of "the day this sits on" seen across Garmin's calendar
# endpoints. A key missing from this list reads as an undated row, the lookup
# finds nothing, and the push falls through to `create` — i.e. a duplicate on
# the athlete's calendar. Erring towards more keys is the safe direction.
_DATE_KEYS = (
    "calendarDate", "calendarDateLocal", "date", "localDate",
    "scheduleDate", "scheduledDate", "scheduledAtDate",
    "workoutDate", "workoutScheduleDate",
)


def _extract_date(entry):
    for key in _DATE_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return None


def _iter_dated_rows(obj, inherited=None):
    """Yield `(node, date)` for every dict, carrying the enclosing date down.

    Garmin returns the calendar either as flat rows that each carry their own
    date, or as one node per day whose children are that day's sessions. The
    inherited date covers the second shape; the node is still the unit that
    ids and names are read from.

    That anchoring is the point. Reading "the first id anywhere below the
    date" pairs one session's id with another session's name whenever a day
    holds two — which is a supported state here, and one that produced both a
    silently dropped session and a workout claimed from the wrong owner.
    """
    if isinstance(obj, dict):
        own = _extract_date(obj) or inherited
        yield obj, own
        for value in obj.values():
            yield from _iter_dated_rows(value, own)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_dated_rows(item, inherited)


def _first_id(entry, keys):
    for key in keys:
        found = _coerce_id(entry.get(key))
        if found:
            return found
    return None


def _extract_name(entry):
    for key in _NAME_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# --- the writer -------------------------------------------------------------


class GarminWorkoutWriter:
    """Creates, schedules and re-schedules workouts on Garmin Connect.

    Pass an existing `GarminClient` (or a raw `garminconnect.Garmin`) to
    reuse a session — Garmin login is slow and rate-limited, so one client
    should be shared across the process. Omit it and one is constructed
    lazily on first write.
    """

    def __init__(self, client=None, store=None, ftp=None):
        self._client = client
        self._api = None
        self.store = store
        self.ftp = ftp or _env_ftp()
        self._schedule_cache = {}

    # -- connection --

    def _ensure_api(self):
        if self._api is not None:
            return self._api

        if self._client is None:
            if not is_configured():
                raise GarminNotConfigured(
                    "Garmin login not set. Add GARMIN_EMAIL and "
                    "GARMIN_PASSWORD to training-sync/.env."
                )
            self._client = self._build_client()

        # GarminClient wraps the library; a raw Garmin object is also fine.
        self._api = getattr(self._client, "_api", self._client)
        return self._api

    @staticmethod
    def _build_client():
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        try:
            from garmin_client import GarminClient
        except ImportError as exc:  # pragma: no cover - packaging issue
            raise GarminWriterError(
                f"could not import garmin_client: {exc}") from exc

        try:
            return GarminClient()
        except KeyError as exc:
            # GarminClient reads os.environ[...] with bracket syntax, so a
            # missing key surfaces as KeyError. Translate it rather than let
            # it escape as an opaque crash.
            raise GarminNotConfigured(
                f"Garmin credential {exc} is not set. Add GARMIN_EMAIL and "
                "GARMIN_PASSWORD to training-sync/.env."
            ) from exc
        except Exception as exc:
            name = type(exc).__name__
            if "Auth" in name:
                raise GarminAuthFailed(
                    f"Garmin login failed: {exc}") from exc
            raise GarminWriterError(f"Garmin login failed: {exc}") from exc

    # -- low-level operations --

    def upload(self, payload):
        """Upload a workout template. Returns its workout id."""
        api = self._ensure_api()
        try:
            response = api.upload_workout(payload)
        except Exception as exc:
            raise GarminRejected(
                f"Garmin rejected the workout: {exc}") from exc
        workout_id = _extract_workout_id(response)
        if not workout_id:
            raise GarminRejected(
                "Garmin accepted the upload but returned no workout id")
        return workout_id

    def schedule(self, workout_id, date):
        """Place a workout on the Garmin calendar. Returns the schedule id.

        The schedule id is a different id from the workout id and is the one
        `unschedule_workout` needs — persist it.
        """
        api = self._ensure_api()
        _validate_date(date)
        try:
            response = api.schedule_workout(workout_id, date)
        except Exception as exc:
            raise GarminRejected(
                f"Garmin refused to schedule workout {workout_id} "
                f"on {date}: {exc}") from exc
        self._schedule_cache.pop(date[:7], None)
        schedule_id = _extract_schedule_id(response)
        if not schedule_id:
            # Not fatal: recover it from the calendar so a move can still be
            # undone later.
            schedule_id = self.find_schedule_id(workout_id, date)
        return schedule_id

    def unschedule(self, schedule_id):
        """Remove a scheduled workout from the calendar. The template
        survives, so it can be re-scheduled without re-uploading."""
        api = self._ensure_api()
        try:
            api.unschedule_workout(schedule_id)
        except Exception as exc:
            raise GarminRejected(
                f"Garmin refused to unschedule {schedule_id}: {exc}") from exc
        self._schedule_cache.clear()
        return True

    # There is deliberately no `delete` here. Removing a workout from the
    # calendar is `unschedule`, which leaves the template intact; deleting the
    # template destroys something that may not be ours and gains nothing an
    # unschedule does not already give. The athlete can tidy their own Garmin
    # library, and only they can tell which templates they still want.

    def scheduled_in_month(self, year, month, refresh=False):
        """`get_scheduled_workouts` is month-keyed, so this is the unit of
        lookup. Cached per writer instance for the life of a request."""
        api = self._ensure_api()
        key = f"{int(year):04d}-{int(month):02d}"
        if refresh:
            self._schedule_cache.pop(key, None)
        if key in self._schedule_cache:
            return self._schedule_cache[key]
        try:
            response = api.get_scheduled_workouts(year, month)
        except Exception as exc:
            raise GarminWriterError(
                f"could not read the Garmin calendar for {key}: {exc}"
            ) from exc
        self._schedule_cache[key] = response
        return response

    def find_schedule_id(self, workout_id, date, refresh=False):
        """Find the schedule id for a workout already on the calendar.

        This is the recovery path when the local store lost a schedule id —
        without it a re-push would stack a second copy on the same day.
        """
        _validate_date(date)
        year, month = int(date[:4]), int(date[5:7])
        try:
            response = self.scheduled_in_month(year, month, refresh=refresh)
        except GarminWriterError:
            return None

        target = _coerce_id(workout_id)
        for node, node_date in _iter_dated_rows(response):
            if node_date != date:
                continue
            # Both ids come off the same row. A row is one session; taking
            # them from wherever they happen to appear below the date mixes
            # two sessions' identities on any day that holds more than one.
            entry_workout = _first_id(node, _WORKOUT_ID_KEYS)
            if not entry_workout or entry_workout != target:
                continue
            schedule_id = _first_id(node, _SCHEDULE_ID_KEYS)
            if schedule_id:
                return schedule_id
        return None

    def find_owned_scheduled(self, uid, date, refresh=False):
        """Find the workout on `date` that *this app* scheduled for `uid`.

        Returns `(workout_id, schedule_id)`, either of which may be None.
        This is the recovery path when the local store has lost its ids: the
        session is on the calendar, nothing here remembers pushing it, and
        uploading again would leave the athlete with two copies.

        Ownership is proved by the marker written into the name at creation,
        never inferred from the name itself. Its predecessor matched titles,
        which meant a first push could adopt a workout the athlete had built
        by hand — and, having adopted it, later rewrite it. When ownership
        cannot be proved this returns nothing and the caller creates its own
        workout, which is the recoverable outcome.

        A calendar read that fails is not an error here; the caller creates.
        """
        _validate_date(date)
        year, month = int(date[:4]), int(date[5:7])
        try:
            response = self.scheduled_in_month(year, month, refresh=refresh)
        except GarminWriterError:
            return (None, None)

        for node, node_date in _iter_dated_rows(response):
            if node_date != date:
                continue
            if not owns_workout(_extract_name(node), uid):
                continue
            workout_id = _first_id(node, _WORKOUT_ID_KEYS)
            if not workout_id:
                continue
            return (workout_id, _first_id(node, _SCHEDULE_ID_KEYS))
        return (None, None)

    # -- the idempotent push --

    def push_workout(self, uid, title, date, steps=None, duration_s=None,
                     sport="cycling", description=None, store=None,
                     dry_run=False, plan_snapshot=None):
        """Create-or-update a workout and place it on `date`.

        Idempotent by design. Given the same content and date, a second push
        is a no-op rather than a duplicate on the calendar:

        - unchanged content, unchanged date  -> noop
        - unchanged content, new date        -> reschedule (reuses template)
        - changed content                    -> update (new template
                                                scheduled, old one taken off
                                                the calendar, never deleted)
        - never pushed                       -> create

        On `update` the new workout is uploaded and scheduled *before* the
        old one is taken off the calendar, so a mid-sequence failure leaves a
        visible duplicate rather than an invisible missing session.

        The workout goes to Garmin under a name carrying this app's ownership
        marker for `uid`, which is what lets a later push recognise its own
        work on the calendar without guessing from the title.
        """
        store = store or self.store
        _validate_date(date)

        name = marked_workout_name(title, uid)
        structured = bool(steps)
        if structured:
            payload = build_workout(name, steps, sport=sport,
                                    description=description, ftp=self.ftp)
            reason_structure = (
                f"{_count_steps(steps)} steps sent to Garmin."
            )
        else:
            payload = build_placeholder_workout(
                name, duration_s=duration_s, sport=sport,
                description=description)
            if duration_s:
                reason_structure = (
                    f"Sent as a {int(duration_s) // 60} min timed placeholder; "
                    "no power targets in the feed."
                )
            else:
                reason_structure = (
                    "Sent as an open-ended placeholder; no duration or "
                    "targets in the feed."
                )

        new_hash = workout_hash(payload)
        # Read the push identity from the push map, not from the override —
        # the two are separate state and the athlete's edits never touch this.
        garmin = (store.get_push(uid) if store else None) or {}
        old_workout_id = _coerce_id(garmin.get("workout_id"))
        old_schedule_id = _coerce_id(garmin.get("schedule_id"))
        old_date = garmin.get("scheduled_for")
        old_hash = garmin.get("pushed_hash")

        result = PushResult(uid=uid, title=title, date=date,
                            structured=structured, dry_run=dry_run)

        # Decide the action before doing anything, so dry_run can answer
        # "here's what I'll send" without writing.
        if old_workout_id and old_hash == new_hash and old_date == date:
            result.action = "noop"
            result.workout_id = old_workout_id
            result.schedule_id = old_schedule_id
            result.state = "skipped"
            result.reason = "Already on Garmin for this date, unchanged."
            return result
        if old_workout_id and old_hash == new_hash and old_date != date:
            result.action = "reschedule"
            result.reason = (
                f"Already on Garmin for {old_date or 'another day'}, "
                f"moving to {date}. {reason_structure}"
            )
        elif old_workout_id:
            result.action = "update"
            result.reason = (
                f"Changed since the last push; replacing it on {date}. "
                f"{reason_structure}"
            )
        else:
            result.action = "create"
            result.reason = f"Not yet on Garmin. {reason_structure}"

        if dry_run:
            result.state = "pending"
            result.workout_id = old_workout_id
            result.schedule_id = old_schedule_id
            return result

        pushed_hash = new_hash
        try:
            if result.action == "reschedule":
                schedule_id = old_schedule_id
                if not schedule_id and old_date:
                    # Recovering an id the store does not have: read the
                    # calendar fresh rather than trust a cached month, which
                    # may itself predate whatever lost the id.
                    schedule_id = self.find_schedule_id(old_workout_id,
                                                        old_date, refresh=True)
                if schedule_id:
                    self.unschedule(schedule_id)
                    if store:
                        # That id is dead on Garmin from here on. Record it
                        # now, because if the re-schedule below fails the
                        # stored id would otherwise be a live grenade: the
                        # next move would try to unschedule it, Garmin would
                        # refuse, and the workout would be stuck on the wrong
                        # day with no way back through the UI.
                        store.record_push(uid, schedule_id=None,
                                          push_state="pending")
                new_schedule_id = self.schedule(old_workout_id, date)
                result.workout_id = old_workout_id
                result.schedule_id = new_schedule_id

            elif result.action == "update":
                new_workout_id = self.upload(payload)
                new_schedule_id = self.schedule(new_workout_id, date)
                result.workout_id = new_workout_id
                result.schedule_id = new_schedule_id
                # Only now remove the superseded copy.
                stale_schedule = old_schedule_id
                if not stale_schedule and old_date:
                    stale_schedule = self.find_schedule_id(old_workout_id,
                                                           old_date,
                                                           refresh=True)
                if stale_schedule:
                    try:
                        self.unschedule(stale_schedule)
                    except GarminWriterError:
                        result.reason += (
                            " (the previous copy is still on your calendar — "
                            "remove it in Garmin Connect)"
                        )
                # The superseded template stays in the Garmin library. It is
                # off the calendar and off the Edge, so it costs nothing; a
                # delete here once destroyed a workout the athlete had built
                # by hand, because the id being deleted had been adopted on a
                # name match and was never ours.

            else:  # create
                # Guard against a store that lost its ids: if a workout *this
                # app wrote for this uid* is already on that day, adopt it
                # rather than upload a second copy the athlete would see
                # twice on the Edge 520 and we could only half remove.
                #
                # Proof of authorship is required. An unmarked workout on the
                # day is somebody else's and we create beside it.
                adopted_id, adopted_schedule_id = self.find_owned_scheduled(
                    uid, date)
                if adopted_id:
                    result.action = "noop"
                    result.workout_id = adopted_id
                    result.schedule_id = (
                        adopted_schedule_id
                        or self.find_schedule_id(adopted_id, date))
                    # It is ours by the marker, but this process did not send
                    # it and cannot know which version it holds. Leaving the
                    # hash unset makes the next push rewrite it rather than
                    # trust content we cannot see.
                    pushed_hash = None
                    # The athlete reads this; show the title they know, not
                    # the marked-up name that went over the wire.
                    result.reason = (
                        f"{title} is already on your Garmin calendar for "
                        f"{date} from an earlier push, so that copy was "
                        "adopted rather than a second one sent."
                    )
                else:
                    new_workout_id = self.upload(payload)
                    new_schedule_id = self.schedule(new_workout_id, date)
                    result.workout_id = new_workout_id
                    result.schedule_id = new_schedule_id

            result.state = "done"
            if store:
                store.record_push(
                    uid,
                    workout_id=result.workout_id,
                    schedule_id=result.schedule_id,
                    scheduled_for=date,
                    pushed_hash=pushed_hash,
                    push_state="scheduled",
                    plan_snapshot=plan_snapshot,
                )
        except (GarminNotConfigured, GarminAuthFailed):
            # Session-level problems, not per-workout ones. Let them out so
            # the UI shows a single "reconnect Garmin" banner instead of the
            # same error repeated on eight rows.
            raise
        except GarminWriterError as exc:
            result.state = "failed"
            result.error = str(exc)
            if store:
                store.mark_push_failed(uid, exc)

        return result

    def unpush_workout(self, uid, store=None):
        """Take a workout this app scheduled off the Garmin calendar.

        Only ids recorded in our own push map are touched — a workout the
        athlete created in Garmin Connect directly is not ours to unschedule.
        The template is left in their library either way; this removes the
        session from the calendar, it does not destroy anything.
        """
        store = store or self.store
        garmin = (store.get_push(uid) if store else None) or {}
        workout_id = _coerce_id(garmin.get("workout_id"))
        schedule_id = _coerce_id(garmin.get("schedule_id"))
        date = garmin.get("scheduled_for")

        result = PushResult(uid=uid, date=date, action="unschedule")
        if not workout_id and not schedule_id:
            result.state = "skipped"
            result.reason = "This app never pushed that workout."
            return result

        try:
            if not schedule_id and date:
                schedule_id = self.find_schedule_id(workout_id, date)
            if schedule_id:
                self.unschedule(schedule_id)
            result.state = "done"
            result.workout_id = workout_id
            result.schedule_id = schedule_id
            result.reason = "Removed from your Garmin calendar."
            if store:
                store.clear_push(uid)
        except (GarminNotConfigured, GarminAuthFailed):
            raise
        except GarminWriterError as exc:
            result.state = "failed"
            result.error = str(exc)
            if store:
                store.mark_push_failed(uid, exc)
        return result

    def push_many(self, items, store=None, dry_run=False):
        """Push a list of dicts shaped like push_workout's kwargs.

        Sequential and per-item: one failure never fails the batch. Partial
        success is a normal outcome and is reported honestly per row.
        """
        results = []
        for item in items or []:
            item = dict(item)
            uid = item.pop("uid")
            try:
                results.append(self.push_workout(
                    uid, store=store or self.store, dry_run=dry_run, **item))
            except GarminNotConfigured:
                raise
            except Exception as exc:
                results.append(PushResult(
                    uid=uid, title=item.get("title"), date=item.get("date"),
                    action="create", state="failed", error=str(exc),
                    dry_run=dry_run))
        return results


def _count_steps(steps):
    total = 0
    for item in steps or []:
        if isinstance(item, Repeat):
            total += len(item.steps)
        else:
            total += 1
    return total


def _validate_date(value):
    if not isinstance(value, str):
        raise GarminWriterError(f"date must be a YYYY-MM-DD string, got {value!r}")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise GarminWriterError(
            f"date must be YYYY-MM-DD, got {value!r}") from exc
    return value


def describe_payload(payload):
    """One-line human summary of a built workout, for the push preview.

    The user approves this description, not a guess — so it must say plainly
    when a workout is going over as a bare timer.
    """
    payload = copy.deepcopy(payload)
    segments = payload.get("workoutSegments") or []
    steps = segments[0].get("workoutSteps", []) if segments else []
    total = payload.get("estimatedDurationInSecs") or 0
    minutes = int(total) // 60

    targeted = any(
        step.get("targetType", {}).get("workoutTargetTypeKey") not in
        (None, "no.target")
        for step in _iter_dicts(steps)
        if step.get("type") == "ExecutableStepDTO"
    )
    count = sum(1 for step in _iter_dicts(steps)
                if step.get("type") == "ExecutableStepDTO")

    if count <= 1 and not targeted:
        if minutes:
            return (f"{minutes} min {payload['sportType']['sportTypeKey']}, "
                    "no steps — the Edge will show a timer, not intervals.")
        return "Title and date only — no duration parsed."
    detail = f"{count} steps"
    if minutes:
        detail += f" · {minutes} min"
    if targeted:
        detail += " · power targets"
    return detail


if __name__ == "__main__":
    # Offline demo: build and describe a workout without touching Garmin.
    demo = build_workout(
        "Threshold 3x12",
        [
            Step(duration_s=900, kind="warmup", zone="endurance"),
            Repeat(times=3, steps=[
                Step(duration_s=720, kind="interval", zone="threshold"),
                Step(duration_s=360, kind="recovery", zone="recovery"),
            ]),
            Step(duration_s=600, kind="cooldown", zone="recovery"),
        ],
        description="3x12 @ 95-100% FTP",
        ftp=250,
    )
    print(json.dumps(demo, indent=2))
    print()
    print("preview:", describe_payload(demo))
    print("hash:   ", workout_hash(demo))
    print("garmin: ", "configured" if is_configured() else "NOT configured")
