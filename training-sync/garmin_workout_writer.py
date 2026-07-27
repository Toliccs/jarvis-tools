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

Five rules govern everything this module writes:

- A calendar it could not read is not an empty calendar, and "could not read"
  includes every answer that arrives dressed as success. `read_calendar` is
  the one place that decides whether a body is an answer at all: an error
  payload, a null or empty body, and a shape whose rows cannot be placed on a
  day are all no answer, exactly like a call that raised. Every read that
  decides a write comes back saying which of the two it is (`CALENDAR_READ` vs
  `CALENDAR_BLOCKED`), and a caller holding `blocked` refuses instead of
  assuming the day is clear. The narrower version of this rule — blocked only
  when the call threw — let a rate-limited Garmin answering `{"error": ...,
  "statusCode": 429}` look exactly like an athlete with nothing scheduled, and
  a duplicate went to the Edge.

- No decision to skip or to leave a copy alone rests on a remembered read of
  the Garmin calendar. Month reads are cached (see `SCHEDULE_CACHE_TTL`)
  because a calendar render asks for the same month repeatedly, but every
  point where a *write* is decided reads fresh. The writer outlives the
  request that built it, the athlete edits their calendar in Garmin Connect,
  and a cache trusted a minute too long once reported a session pushed that
  was on nobody's calendar at all.

- It never destroys anything, and it offers no removal. There is no `delete`
  here, and no `unpush` either: rewriting a session uploads a new template and
  unschedules the old calendar entry, leaving the superseded template in the
  athlete's Garmin library where it is harmless. A workout template is cheap;
  one the athlete built by hand and lost is not. Taking a session *off* the
  calendar is done in Garmin Connect, by the athlete. A removal path lived
  here for two rounds and produced four separate reproduced defects — removals
  reported that never happened, refusals that never cleared — because it had
  to be right about a calendar it could only glimpse, and being wrong meant
  forgetting the only ids that could ever address the copy. The next push
  reads the calendar and notices the session is gone.

- A stored id is never permission to touch a row. The ids in a push record say
  which rows to look at; the ownership marker on the row is what authorises
  writing on it. `locate_owned_copies` is the single gate every write past the
  first goes through, and no `unschedule` in this module is ever handed an id
  that did not come back attached to a marked row in the same read. Without
  that rule an unmarked "Wednesday club run" answering to a stored schedule id
  was unscheduled by an update, and moved to another day by a reschedule.

- It only claims a workout it can prove it wrote — and only claims what it has
  looked at. Every workout created here carries an ownership marker keyed to
  the plan uid (`push_marker`), and the adopt path matches on that marker
  alone. Names repeat weekly and two sessions can share a title on one day, so
  a name proves nothing — matching on one is how this app came to adopt, and
  then overwrite, workouts that were never its own. The same rule governs what
  a *skip* may say: "unchanged since the last push" is a fact about the push
  record, "already on your Garmin calendar" is a claim about Garmin, and the
  second one is only made after reading it.

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
from time import monotonic as _monotonic

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


def display_title(entry):
    """The marker-free title of a calendar row, ready to show a person.

    Takes either a raw name or a calendar row as Garmin returned it, so a
    caller rendering the Garmin calendar has one thing to call and no reason
    to reach for the raw name. This is the *only* form a title read back off
    Garmin should reach a screen in: the marker is ours, not the athlete's,
    and it used to travel all the way to the UI as `... [ts:1a2b3c4d]`.

    Ownership checks want `owns_workout` on the raw name instead — stripping
    the marker is exactly what destroys the evidence they run on.
    """
    if isinstance(entry, dict):
        entry = _extract_name(entry)
    return strip_marker(entry)


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


class GarminConflict(GarminWriterError):
    """A write was refused because doing it would leave two copies.

    Raised when a previous copy of a workout is on the Garmin calendar and
    this app cannot address it — Garmin answered the schedule call without an
    id and does not expose one in the listing either. Uploading the new
    version anyway would put a second session on the athlete's Edge with
    nothing here able to remove the first, so the push stops instead and says
    what to do about it. The message is shown to the athlete verbatim.
    """

    code = "garmin_conflict"


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
    action: str = "noop"        # create|reschedule|update|noop|skip
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


def _normalize_row(node, node_date):
    """One calendar row in the only shape the rest of this module reads.

    `title` is the athlete's — marker stripped, safe to display anywhere.
    `raw_title` is Garmin's, marker intact, and is what ownership is proved
    against; the two are kept apart so no caller has to remember which one it
    is holding.
    """
    raw = _extract_name(node)
    return {
        "date": node_date,
        "workout_id": _first_id(node, _WORKOUT_ID_KEYS),
        "schedule_id": _first_id(node, _SCHEDULE_ID_KEYS),
        "title": strip_marker(raw),
        "raw_title": raw,
        "marked": bool(raw and MARKER_RE.search(raw)),
    }


def _rows_in(entry):
    """Every session inside one calendar entry, normalized.

    Envelope nodes — a day holding its sessions, a wrapper holding the list —
    carry no ids of their own and are dropped; what is left is one dict per
    session, with its own ids and its own name.
    """
    rows = []
    for node, node_date in _iter_dated_rows(entry):
        if not node_date:
            continue
        row = _normalize_row(node, node_date)
        if not (row["workout_id"] or row["schedule_id"]):
            continue
        rows.append(row)
    return rows


def _has_ids(entry):
    return bool(_first_id(entry, _WORKOUT_ID_KEYS)
                or _first_id(entry, _SCHEDULE_ID_KEYS))


# Keys under which Garmin has been seen to hang the month's entries. The list
# is tried in order; anything not on it falls back to "the response's one and
# only list", and a response with no list at all — or with several, so there is
# no telling which one is the calendar — is not an answer about that month.
_ROW_LIST_KEYS = (
    "calendarItems", "calendarEntries", "workoutScheduleList",
    "workoutSchedules", "scheduledWorkouts", "schedules", "workouts",
    "entries", "items",
)

# An answer that says it failed. Garmin returns these with a 200 as often as
# not, which is exactly why the body has to be looked at rather than the
# transport: `{"error": "rate limited", "statusCode": 429}` parsed, held no
# rows, and was read as "that month is empty".
_ERROR_KEYS = ("error", "errorMessage", "errors", "fault", "serviceError",
               "exception", "errorDescription")
_HTTP_STATUS_KEYS = ("statusCode", "status_code", "httpStatus",
                     "httpStatusCode", "responseCode")


def _error_signal(response):
    """Why `response` is a failure rather than a calendar, or None."""
    if not isinstance(response, dict):
        return None
    for key in _ERROR_KEYS:
        if response.get(key):
            return f"{key}={response[key]!r}"
    for key in _HTTP_STATUS_KEYS:
        value = response.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if isinstance(value, (int, float)) and int(value) >= 400:
            return f"{key}={int(value)}"
    return None


def _row_collection(response, _depth=0):
    """The list of entries for the month, or None when there isn't one.

    None is the honest answer for a body carrying no list at all — `{}`, a
    null body, an error envelope. It is emphatically not the same as an empty
    list, which is a month that *was* read and holds nothing.

    Wrapper objects are unwrapped, because Garmin's envelopes have changed
    shape before and an account answering inside one used to work. Ambiguity
    is not: two lists side by side, with no telling which is the calendar, is
    another way of having no answer.
    """
    if isinstance(response, list):
        return response
    if not isinstance(response, dict) or _depth > 3:
        return None
    for key in _ROW_LIST_KEYS:
        value = response.get(key)
        if isinstance(value, list):
            return value
    lists = [value for value in response.values() if isinstance(value, list)]
    if lists:
        return lists[0] if len(lists) == 1 else None
    nested = [_row_collection(value, _depth + 1)
              for value in response.values() if isinstance(value, dict)]
    found = [value for value in nested if value is not None]
    return found[0] if len(found) == 1 else None


def read_calendar(response):
    """`(rows, problem)` — a month's sessions, or why this isn't a month.

    The one place that decides whether a calendar answer is an answer. Only a
    body that parses end to end is truth about that month; everything else is
    `problem`, a short reason, and no rows.

    "Parses end to end" is deliberately strict. Every entry in the collection
    has to come out as either a session this module can place on a day or a
    day this module can see is empty. An entry carrying ids but no date it
    recognises is a session whose day was not understood, and dropping it
    silently is what made a shape change look like a clear calendar.

    An empty collection is not a problem: it is a month that was read and
    holds nothing, which is the answer every write path needs to be able to
    get. The two used to be the same empty list.
    """
    signal = _error_signal(response)
    if signal:
        return ([], f"the answer carried an error ({signal})")
    entries = _row_collection(response)
    if entries is None:
        return ([], "the answer held no list of calendar entries")

    rows = []
    for entry in entries:
        found = _rows_in(entry) if isinstance(entry, (dict, list)) else []
        if found:
            rows.extend(found)
            continue
        # Nothing came out of it. That is only honest for an entry this module
        # recognises as holding no session — a dated day with nothing on it.
        if isinstance(entry, dict) and _extract_date(entry) \
                and not _has_ids(entry):
            continue
        return ([], f"an entry in the answer could not be read ({entry!r:.120})")
    return (rows, None)


# --- the writer -------------------------------------------------------------

# Appended to a push's `reason` when this app wrote a copy but cannot say
# whether an earlier one is still on the calendar — no date on the push record,
# so there is no month to read. The push still goes through; it just doesn't
# pretend to know. Silence here is what left athletes with two sessions on the
# Edge and a job that reported success.
UNVERIFIED_PREVIOUS_COPY = (
    " (couldn't check whether the copy pushed earlier is still on your "
    "calendar — if you see two, remove the older one in Garmin Connect)"
)

# How long a month read may be reused. A calendar render asks for the same
# month once per fanned-out request and the writer is built once per process,
# so some caching is worth having — but the athlete also edits their calendar
# in Garmin Connect, where nothing invalidates anything here. Short enough
# that a stale read is a stale *render*, never a stale write decision: every
# write path passes refresh=True regardless.
SCHEDULE_CACHE_TTL = 20.0

# What a calendar read came back with. Two words, because there are two facts
# and they are not interchangeable:
#
#   CALENDAR_READ     Garmin gave an answer this module could parse end to
#                     end. The rows are the whole truth for that month, and no
#                     rows means nothing is scheduled.
#   CALENDAR_BLOCKED  There is no answer. The rows are empty because nothing
#                     could be read, which is evidence of nothing at all.
#
# CALENDAR_BLOCKED covers the answers that arrive dressed as success as well as
# the ones that raise. An error payload, a null or empty body, and a shape
# whose rows cannot be placed on a day are all "no answer": see
# `read_calendar`. The narrower version of this rule — blocked only when the
# call raised — is what let a rate-limited Garmin look exactly like an athlete
# with nothing scheduled, and the create path duplicated onto a day it had
# never seen.
#
# Everything below reads the calendar to decide a write, and every one of those
# decisions is wrong if the two collapse into one empty list. The distinction
# lives in the return value, where a caller has to say what it does about it.
CALENDAR_READ = "read"
CALENDAR_BLOCKED = "blocked"

# What `locate_owned_copies` found. Only `resolved` is permission to write on
# a row that is already there; the rest are all reasons not to.
#
#   OWNED_RESOLVED      every copy of ours in that month carries our marker
#                       and has an id that can take it off the calendar.
#   OWNED_UNADDRESSABLE a copy of ours is there and Garmin exposes no id for
#                       it. Writing anyway leaves two on the athlete's Edge.
#   OWNED_UNPROVEN      no copy of ours anywhere in the month, but something
#                       answers to an id out of our push record. Somebody
#                       else's session, or ours renamed past recognition;
#                       either way not ours to move, replace or remove.
#   OWNED_ABSENT        the month was read; nothing of ours, and nothing
#                       answering to our ids. There is no previous copy.
#   OWNED_UNREADABLE    the month could not be read at all.
OWNED_RESOLVED = "resolved"
OWNED_UNADDRESSABLE = "unaddressable"
OWNED_UNPROVEN = "unproven"
OWNED_ABSENT = "absent"
OWNED_UNREADABLE = "unreadable"


class GarminWorkoutWriter:
    """Creates, schedules and re-schedules workouts on Garmin Connect.

    Pass an existing `GarminClient` (or a raw `garminconnect.Garmin`) to
    reuse a session — Garmin login is slow and rate-limited, so one client
    should be shared across the process. Omit it and one is constructed
    lazily on first write.
    """

    def __init__(self, client=None, store=None, ftp=None, cache_ttl=None):
        self._client = client
        self._api = None
        self.store = store
        self.ftp = ftp or _env_ftp()
        # month key -> (read_at, response). Timestamped, not just stored: this
        # object is long-lived and the calendar it describes is not ours alone.
        self._schedule_cache = {}
        self.cache_ttl = (SCHEDULE_CACHE_TTL if cache_ttl is None
                          else float(cache_ttl))

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
        """Take a scheduled workout off the calendar. The template survives,
        so it can be re-scheduled without re-uploading.

        Internal to rewriting a session this app already put on the calendar,
        and the id it is given always comes off a row that carried this app's
        marker in the same read — see `locate_owned_copies`. It is not a
        removal feature: nothing in this module takes a session off the
        calendar on the athlete's behalf, because for two rounds the thing
        that did got it wrong in four different ways. Garmin Connect is where
        a session comes off.
        """
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

    def invalidate_schedule_cache(self):
        """Forget every cached month read.

        For the layer above: anything that means "your picture of Garmin is
        stale" — the athlete pressing Refresh, a reconnect, a write made
        outside this writer — should call this. Writes made *through* this
        writer already invalidate what they touch.
        """
        self._schedule_cache.clear()

    def scheduled_rows(self, year, month, refresh=False, date=None):
        """Every workout on the Garmin calendar that month, normalized.

        `get_scheduled_workouts` is month-keyed, so the month is the unit of
        lookup and this is the only place in the module that performs one.

        One dict per session — `date`, `workout_id`, `schedule_id`, `title`
        (marker stripped, safe to display), `raw_title` (marker intact, for
        ownership) and `marked` (whether this app wrote it, for some uid).

        The ids come off the same row as the name, which is the whole point:
        reading "the first id anywhere below the date" pairs one session's id
        with another session's name on any day holding two.

        A calendar that cannot be read raises `GarminWriterError`. It does
        *not* come back as an empty list: "nothing is scheduled that month"
        and "Garmin did not answer" are different facts, and every write this
        module decides depends on which one it is holding. Callers that must
        carry on without an answer use `read_scheduled_rows` and say, in the
        open, what they do about `CALENDAR_BLOCKED`.

        "Could not be read" covers the answers that arrive looking like
        success as well as the ones that raise. An error payload, a null or
        empty body and a shape whose rows this module cannot place on a day
        are all no answer at all — see `read_calendar`. A rate-limited Garmin
        answering `{"error": ..., "statusCode": 429}` used to come out of here
        as a clean, empty month, and the create path's duplicate guard, whose
        only evidence is this call, uploaded a second copy onto a day it had
        never actually seen.

        The *rows* are what is cached, for `cache_ttl` seconds, so a stale
        body can never be re-parsed into a decision and an answer that did not
        parse is never remembered at all. `refresh=True` bypasses the cache
        and is mandatory anywhere the answer decides a write: this writer is
        built once per process and lives across requests, so "we read this
        month earlier" is not evidence about what is on the calendar now.
        """
        key = f"{int(year):04d}-{int(month):02d}"
        rows = None
        if not refresh:
            cached = self._schedule_cache.get(key)
            if cached and (_monotonic() - cached[0]) < self.cache_ttl:
                rows = cached[1]
        if rows is None:
            self._schedule_cache.pop(key, None)
            api = self._ensure_api()
            try:
                response = api.get_scheduled_workouts(year, month)
            except Exception as exc:
                raise GarminWriterError(
                    f"could not read the Garmin calendar for {key}: {exc}"
                ) from exc
            rows, problem = read_calendar(response)
            if problem:
                # Not cached: a body that is not an answer must never become a
                # remembered answer.
                raise GarminWriterError(
                    f"could not read the Garmin calendar for {key}: {problem}")
            self._schedule_cache[key] = (_monotonic(), rows)
        if date:
            rows = [row for row in rows if row["date"] == date]
        return [dict(row) for row in rows]

    def read_scheduled_rows(self, year, month, refresh=False, date=None):
        """`(rows, status)` — the calendar's answer, or the fact there is none.

        The single place in this module where a failed calendar read becomes a
        value instead of an exception, so there is exactly one line to read to
        know what an empty list can mean. `status` is `CALENDAR_READ` (rows
        are the whole truth) or `CALENDAR_BLOCKED` (rows are empty because
        nothing could be read).

        "Nothing could be read" is decided in `read_calendar`, on the body,
        not on whether the call threw. An error payload, a null or empty body
        and a shape whose rows cannot be placed on a day are `CALENDAR_BLOCKED`
        exactly like a connection that failed — because they are the same fact
        about what this app knows, and the version of this that only counted
        exceptions let a rate-limited Garmin pass for an empty calendar.

        Session-level failures — credentials absent, login expired — are not
        "blocked": they are not facts about this month, they will not clear on
        a retry, and the layer above shows one reconnect banner for them
        rather than a conflict per row. They keep travelling as exceptions.
        """
        try:
            return (self.scheduled_rows(year, month, refresh=refresh,
                                        date=date), CALENDAR_READ)
        except (GarminNotConfigured, GarminAuthFailed):
            raise
        except GarminWriterError:
            return ([], CALENDAR_BLOCKED)

    def find_schedule_id(self, workout_id, date, refresh=False):
        """Find the schedule id for a workout already on the calendar.

        This is the recovery path when the local store lost a schedule id —
        without it a re-push would stack a second copy on the same day.

        Best-effort by design: `None` means "no id could be had", never "the
        copy is not there". The two are not the same and nothing here may
        decide a skip or a write on this answer — `locate_owned_copies` is the
        one that keeps them apart.
        """
        _validate_date(date)
        target = _coerce_id(workout_id)
        rows, _status = self.read_scheduled_rows(
            int(date[:4]), int(date[5:7]), refresh=refresh, date=date)
        for row in rows:
            if not row["workout_id"] or row["workout_id"] != target:
                continue
            if row["schedule_id"]:
                return row["schedule_id"]
        return None

    def find_owned_scheduled(self, uid, date, refresh=True):
        """Find the workout on `date` that *this app* scheduled for `uid`.

        Returns `(workout_id, schedule_id, status)`, where status is one of:

        - `"resolved"` — a copy of ours is on that day (the schedule id may
                         still be None if Garmin will not expose it).
        - `"absent"`   — the calendar was read and holds no copy of ours.
        - `"blocked"`  — the calendar could not be read, so nothing at all is
                         known about that day.

        This is the recovery path when the local store has lost its ids: the
        session is on the calendar, nothing here remembers pushing it, and
        uploading again would leave the athlete with two copies.

        Ownership is proved by the marker written into the name at creation,
        never inferred from the name itself. Its predecessor matched titles,
        which meant a first push could adopt a workout the athlete had built
        by hand — and, having adopted it, later rewrite it. When ownership
        cannot be proved this answers `"absent"` and the caller creates its
        own workout beside theirs, which is the recoverable outcome.

        `"blocked"` is emphatically not `"absent"`. It used to be: a failed
        read arrived here as an empty list and the caller — whose only
        protection against a duplicate is this answer — uploaded a second copy
        onto a day it had never managed to look at.

        `refresh` defaults to True and callers deciding a write must leave it
        that way. This answer is the sole guard against a duplicate, and once
        it was allowed to come from a cached month it started reporting
        workouts the athlete had since deleted in Garmin Connect as "already
        there" — a push that reported success and reached no device at all.
        """
        rows, status = self.owned_scheduled_rows(uid, date, refresh=refresh)
        if status == CALENDAR_BLOCKED:
            return (None, None, "blocked")
        rows = [row for row in rows if row["workout_id"]]
        if not rows:
            return (None, None, "absent")
        return (rows[0]["workout_id"], rows[0]["schedule_id"], "resolved")

    def owned_rows(self, uid, date, refresh=True):
        """`(rows, status)` for every row in `date`'s month this app can prove
        it wrote for `uid`, wherever in that month it now sits.

        `date` says which month to read, not which day to search. The marker
        is per-uid and a uid holds one push record, so any row in that month
        carrying it is this app's copy of that session — including the day the
        athlete dragged it to in Garmin Connect, which the recorded date knows
        nothing about.

        More than one row is possible and is exactly the state worth seeing:
        it is the duplicate on the athlete's Edge that this module exists to
        avoid. `status` carries the read itself — `CALENDAR_BLOCKED` means the
        empty list is an absence of evidence, not evidence of absence.
        """
        _validate_date(date)
        rows, status = self.read_scheduled_rows(
            int(date[:4]), int(date[5:7]), refresh=refresh)
        return ([row for row in rows
                 if owns_workout(row["raw_title"], uid)], status)

    def owned_scheduled_rows(self, uid, date, refresh=True):
        """`owned_rows` narrowed to the day itself, for the duplicate guard on
        create — which is a question about one day, not one month."""
        rows, status = self.owned_rows(uid, date, refresh=refresh)
        return ([row for row in rows if row["date"] == date], status)

    def locate_owned_copies(self, uid, workout_id, schedule_id, date):
        """The copies this app can *prove* it wrote, and may therefore touch.

        The single ownership gate for every write that lands on a row already
        on the calendar. Returns `(rows, status)` where status is one of
        `OWNED_RESOLVED`, `OWNED_UNADDRESSABLE`, `OWNED_UNPROVEN`,
        `OWNED_ABSENT` or `OWNED_UNREADABLE`, and `rows` is non-empty only for
        `OWNED_RESOLVED`.

        Proof is the ownership marker and *only* the marker. The ids in the
        push record say which rows to look at; they are not themselves
        permission. A record can be inherited with a state file, restored from
        a backup, or simply outlive the workout it described, and Garmin will
        unschedule whatever id it is handed without asking whose session it
        is. That is how a push once took an athlete's own "Wednesday club run"
        off their calendar and reported a clean update: the doctrine had been
        applied to the removal path and nowhere else, so an unmarked row
        answering to a stored schedule id was unscheduled — and in the move
        branch, unscheduled *and* re-scheduled onto another day. Every such
        record carries a `pushed_hash`, so the store's own migration
        validation waves it through; the calendar is the only thing that can
        tell them apart.

        All of them or none. If a copy of ours is there without an id that can
        address it, this answers `OWNED_UNADDRESSABLE` rather than picking off
        the ones it can reach — writing the replacement and leaving that one
        behind is the two-sessions-on-the-Edge outcome the module exists to
        avoid.

        Call it *before* writing the replacement. Afterwards the new copy is
        on the calendar carrying the same marker, and telling the two apart
        costs an exclusion the caller can get wrong — with an unscheduled new
        copy, i.e. a silently missing session, as the price.
        """
        _validate_date(date)
        # Fresh, always: this decides whether a write happens, and a month
        # remembered from a render is not evidence about the calendar now.
        month, status = self.read_scheduled_rows(
            int(date[:4]), int(date[5:7]), refresh=True)
        if status == CALENDAR_BLOCKED:
            return ([], OWNED_UNREADABLE)

        stored_workout = _coerce_id(workout_id)
        stored_schedule = _coerce_id(schedule_id)

        ours = [row for row in month if owns_workout(row["raw_title"], uid)]
        if ours:
            # A row of ours that Garmin will not hand an id for in the listing
            # is still ours, and the push record's own schedule id addresses
            # it — but only where the record demonstrably describes that row:
            # same marker, same workout id, and no other row on the calendar
            # already answering to that schedule id. The marker on the row is
            # what authorises the write; the stored id is only the handle.
            taken = {row["schedule_id"] for row in month if row["schedule_id"]}
            resolved = []
            for row in ours:
                found = row["schedule_id"]
                if (not found and stored_schedule and stored_workout
                        and row["workout_id"] == stored_workout
                        and stored_schedule not in taken):
                    found = stored_schedule
                    taken.add(found)
                if not found:
                    return ([], OWNED_UNADDRESSABLE)
                resolved.append(dict(row, schedule_id=found))
            return (resolved, OWNED_RESOLVED)

        # Nothing of ours. Does anything answer to the ids we hold? If so this
        # app is pointing at a session it cannot claim, and the honest thing is
        # to say so rather than write on it.
        claimed = any(
            (stored_workout and row["workout_id"] == stored_workout)
            or (stored_schedule and row["schedule_id"] == stored_schedule)
            for row in month
        )
        return ([], OWNED_UNPROVEN if claimed else OWNED_ABSENT)

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
            # "Nothing changed here" — a true statement about the plan and the
            # push record, which is all this decision can see. Whether a copy
            # is on the athlete's calendar is a fact about Garmin, and it is
            # checked below, before anything is claimed about it.
            result.action = "noop"
            result.workout_id = old_workout_id
            result.schedule_id = old_schedule_id
            result.state = "skipped"
            result.reason = "Unchanged since the last push."
        elif old_workout_id and old_hash == new_hash and old_date != date:
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
            if result.action != "noop":
                result.state = "pending"
            result.workout_id = old_workout_id
            result.schedule_id = old_schedule_id
            return result

        pushed_hash = new_hash
        # Where the previous copy stands, once something has looked. "unknown"
        # is the honest starting point: no month has been read yet.
        # `proof_month` is which month was read to get there — an absence
        # proved for one month says nothing about another.
        proven = []
        proof = "unknown"
        proof_month = None
        try:
            if result.action == "noop":
                # A skip is a claim about the athlete's calendar, so it is made
                # against the athlete's calendar. "Already on Garmin" used to
                # be said on the strength of the push record alone: delete the
                # workout in Garmin Connect, push again, and the job reported
                # success with nothing on the calendar and push_state still
                # "scheduled", so the UI went on saying "On Garmin" forever.
                # There is no other way back from that — nothing in this module
                # removes anything — which is exactly why the skip has to look.
                ours, status = self.owned_rows(uid, date, refresh=True)
                if status == CALENDAR_BLOCKED:
                    # Nothing is being written either way, so this is not a
                    # refusal; it is a claim this app is not entitled to make.
                    result.reason = (
                        "Unchanged since the last push. This app couldn't read"
                        f" your Garmin calendar for {date} just now, so it "
                        "can't confirm the copy is still on it.")
                    return result
                on_day = [row for row in ours if row["date"] == date]
                if on_day:
                    result.workout_id = on_day[0]["workout_id"] or old_workout_id
                    result.schedule_id = (on_day[0]["schedule_id"]
                                          or old_schedule_id)
                    result.reason = (
                        f"Already on your Garmin calendar for {date}, "
                        "unchanged.")
                    return result
                if ours:
                    # On the calendar, just not where the record says. The
                    # athlete moved it in Garmin Connect and that is their
                    # call; nothing needs sending and nothing gets moved back.
                    result.reason = (
                        "Unchanged since the last push. This app's copy is on "
                        f"your Garmin calendar for {ours[0]['date']}, not "
                        f"{date} — moved in Garmin Connect, and left there.")
                    return result
                # Read, and no copy of ours anywhere in that month: it is gone
                # from the calendar, so send it again rather than report a
                # session onto a device that never got one.
                result.action = "create"
                result.state = "pending"
                proof, proof_month = OWNED_ABSENT, date[:7]
                result.reason = (
                    f"The copy pushed earlier is no longer on your Garmin "
                    f"calendar for {date}, so it was sent again. "
                    f"{reason_structure}")

            # Where the previous copy stands is settled *before* anything is
            # written, and settled against the calendar. A stored id says which
            # row to look at; the marker on that row is what authorises writing
            # on it. Without the marker this app is looking at somebody else's
            # session, and moving or replacing it is not its call to make.
            if result.action in ("reschedule", "update"):
                if old_date:
                    proven, proof = self.locate_owned_copies(
                        uid, old_workout_id, old_schedule_id, old_date)
                    proof_month = old_date[:7]
                doing = ("moving it to " + date if result.action == "reschedule"
                         else "sending the new version")
                if proof == OWNED_UNREADABLE:
                    # The read is the transient part, so this is retryable and
                    # there is nothing for the athlete to tidy up.
                    raise GarminConflict(_unreadable_calendar_message(
                        title, old_date, doing))
                if proof == OWNED_UNADDRESSABLE:
                    # The copy is on the calendar and cannot be taken off.
                    # Writing the replacement is how one session becomes two.
                    raise GarminConflict(_conflict_message(
                        title, old_date, doing))
                if proof == OWNED_UNPROVEN:
                    raise GarminConflict(_unproven_message(title, old_date))

            if result.action == "reschedule" and proven \
                    and proven[0]["workout_id"]:
                # The only true move: our rows, provably ours, addressable,
                # and one of them carrying a template to put back down on the
                # new day. Every id handed to `unschedule` came off a row that
                # carried this app's marker in the read just above; none of
                # them came out of the store.
                for row in proven:
                    self.unschedule(row["schedule_id"])
                if store:
                    # Those ids are dead on Garmin from here on. Record that
                    # now, because if the re-schedule below fails a stored id
                    # would otherwise be a live grenade: the next move would
                    # try to unschedule it, Garmin would refuse, and the
                    # workout would be stuck with no way back through the UI.
                    store.record_push(uid, schedule_id=None,
                                      push_state="pending")
                result.workout_id = proven[0]["workout_id"]
                result.schedule_id = self.schedule(result.workout_id, date)

            elif proven:
                # A copy of ours is there and has to be replaced rather than
                # moved: either the content changed, or Garmin listed our row
                # without the workout id, so there is no template to put back
                # down and a fresh one has to go up.
                #
                # The new copy is scheduled first and the superseded one
                # removed after, so a mid-sequence failure leaves a visible
                # duplicate rather than an invisible missing session.
                result.workout_id = self.upload(payload)
                result.schedule_id = self.schedule(result.workout_id, date)
                stranded = False
                for row in proven:
                    try:
                        self.unschedule(row["schedule_id"])
                    except GarminWriterError:
                        stranded = True
                if stranded:
                    result.reason += (
                        " (the previous copy is still on your calendar — "
                        "remove it in Garmin Connect)"
                    )
                # The superseded template stays in the Garmin library. It is
                # off the calendar and off the Edge, so it costs nothing; a
                # delete here once destroyed a workout the athlete had built
                # by hand, because the id being deleted had been adopted on a
                # name match and was never ours.

            else:
                # No previous copy of ours to move or replace, whatever the
                # label says — so this is a create, and it goes through the
                # same duplicate guard every create does.
                #
                # Guard against a store that lost its ids: if a workout *this
                # app wrote for this uid* is already on that day, adopt it
                # rather than upload a second copy the athlete would see twice
                # on the Edge 520 and we could only half remove.
                #
                # Proof of authorship is required. An unmarked workout on the
                # day is somebody else's and we create beside it.
                # refresh=True is not optional here. This read is the only
                # thing standing between a push and a duplicate, and a cached
                # month has already reported a copy that the athlete had
                # deleted in Garmin Connect — nothing reached the device and
                # the job said "done".
                if proof == OWNED_ABSENT and proof_month == date[:7]:
                    # Already proved, moments ago and against this very month:
                    # no copy of ours anywhere in it, so none on this day.
                    # Reading it again would only be a second chance to be
                    # rate-limited immediately before a write.
                    adopted_id = adopted_schedule_id = None
                    adopt_status = "absent"
                else:
                    adopted_id, adopted_schedule_id, adopt_status = \
                        self.find_owned_scheduled(uid, date, refresh=True)
                if adopt_status == "blocked":
                    # The guard came back with no answer at all. An unread
                    # calendar is not an empty one, and uploading on the
                    # strength of it is precisely the duplicate this guard
                    # exists to prevent: the copy from an earlier push sits on
                    # that day, unseen, and a second one joins it on the Edge.
                    # The read is the transient part, so this is retryable.
                    raise GarminConflict(_unreadable_calendar_message(
                        title, date, "sending it now"))
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
                    result.workout_id = self.upload(payload)
                    result.schedule_id = self.schedule(result.workout_id, date)
                    if proof == "unknown" and old_workout_id:
                        # No date on the push record, so there was no month to
                        # read and the earlier copy's whereabouts cannot be
                        # proved either way. Proceeding is fine; proceeding in
                        # silence is what left people with two sessions on the
                        # Edge and a job that said "done".
                        result.reason += UNVERIFIED_PREVIOUS_COPY

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
        except GarminConflict as exc:
            # A refusal, not a breakage: nothing was written, and the athlete
            # can clear it themselves. It goes in `reason` as well as `error`
            # because `reason` is what the UI shows verbatim, and the previous
            # reason ("replacing it on ...") describes a push that did not
            # happen.
            result.state = "failed"
            result.error = str(exc)
            result.reason = str(exc)
            if store:
                store.mark_push_failed(uid, exc)
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


def _conflict_message(title, date, doing):
    """What the athlete is told when a push is refused to avoid a duplicate.

    Shown verbatim, so it names the day, says what this app could not do and
    what they can do instead. `strip_marker` because a title can arrive here
    already carrying our tag, and the tag is not theirs to read.
    """
    clean = strip_marker(title) or "That workout"
    return (
        f'Not sent: the copy of "{clean}" pushed earlier is still on your '
        f"Garmin calendar for {date or 'an earlier day'}, and Garmin will not "
        "say which calendar entry it is, so this app cannot take it off. "
        f"{doing[:1].upper()}{doing[1:]} now would leave two copies on your "
        "Edge. "
        f"Remove the older \"{clean}\" from {date or 'that day'} in Garmin "
        "Connect, then push again."
    )


def _unproven_message(title, date):
    """What the athlete is told when a write is refused because the row this
    app's records point at is not one it can prove it wrote.

    Different again from the other two: nothing is stuck, and retrying will
    not help. Something is on the calendar answering to an id in the push
    record, but without this app's marker on it — somebody else's session, or
    one of ours renamed past recognition. Either way it is not this app's to
    move or replace, and the only person who can settle it is the athlete.
    """
    clean = strip_marker(title) or "That workout"
    return (
        f"Not sent: the session on your Garmin calendar for "
        f"{date or 'that day'} that this app's records point at isn't one it "
        "can prove it wrote — it carries no marker of this app's, so it may "
        "well be a session you built yourself. This app will not move or "
        f'replace a session it did not put there. If "{clean}" should come '
        "from here, remove that entry in Garmin Connect and push again."
    )


def _unreadable_calendar_message(title, date, doing):
    """What the athlete is told when a write is refused because the calendar
    could not be read.

    Different from `_conflict_message` in the one way that matters to them:
    nothing is stuck and there is nothing to tidy up in Garmin Connect. The
    read failed, and a read failing is the kind of thing that stops happening,
    so the remedy is to try again rather than to go and move something.
    """
    clean = strip_marker(title) or "That workout"
    return (
        f'Not sent: this app couldn\'t read your Garmin calendar for '
        f"{date or 'that day'}, so it can't tell whether a copy of "
        f'"{clean}" is already on it. {doing[:1].upper()}{doing[1:]} could '
        "leave two copies on your Edge, so nothing was sent. Try again in a "
        "moment."
    )


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
