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
  then overwrite, workouts that were never its own. The one refinement is that
  a uid is not permanent: TrainingPeaks regenerating its feed changes every
  one, the store re-keys the push record to follow, and the marker already on
  Garmin still spells the old uid. This app's marker under a uid it no longer
  uses, on a row this uid's own ids point at, is still this app's work — see
  `_is_ours`. An *unmarked* row is never ours, whatever the record says. The
  same rule governs what a *skip* may say: "unchanged since the last push" is
  a fact about the push record, "already on your Garmin calendar" is a claim
  about Garmin, and the second one is only made after reading it.

- A month boundary is not a safety boundary. `get_scheduled_workouts` is
  month-keyed, so "is our copy still there" has to be asked one month at a
  time — but the athlete drags sessions across month edges in Garmin Connect,
  whose own grid shows the neighbouring months' days so that they can. Every
  ownership read therefore covers the months either side as well
  (`_plausible_months`), and a copy found off-month is *said out loud* rather
  than acted on in silence. Scoped to the single recorded month, all three
  branches concluded "not there", sent a second copy and re-pointed the push
  record at it — orphaning the first where nothing would ever find it again,
  because nothing here removes anything. The fan-out is bounded, so a copy
  dragged further than that is still out of reach: the months that could not
  be accounted for are named in the result rather than passed off as proof.

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
# Being per-uid is also its one weakness, because a uid outlives nothing: when
# TrainingPeaks regenerates its feed every uid changes at once. The store
# re-keys the push record to follow (`rematch_orphans`), and the name already
# written on the Garmin calendar goes on spelling the uid from before. That
# row is still this app's work, and `_is_ours` says so when the record's own
# ids point at it. Refusing it instead bricked every pushed workout on the
# first feed regeneration.
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


def _answers_to(row, workout_id, schedule_id):
    """Whether a calendar row is one of the rows a push record points at."""
    return bool((workout_id and row["workout_id"] == workout_id)
                or (schedule_id and row["schedule_id"] == schedule_id))


def _is_ours(row, uid, workout_id=None, schedule_id=None):
    """Whether this app can prove it wrote `row` for `uid`.

    Two ways, and both are the marker:

    - the row carries the marker for `uid`. This is the ordinary proof and
      needs nothing else.
    - the row carries *a* marker of this app's and the push record for `uid`
      points at it. That is what a re-key looks like from the calendar's side:
      TrainingPeaks regenerates its feed, every uid changes, `rematch_orphans`
      moves the push record onto the new uid, and the name already on Garmin
      still encodes the old one. The marker proves this app wrote the row; the
      stored id proves this record describes it.

    An unmarked row is never ours, whatever the record says — that rule is
    what stopped a push unscheduling an athlete's own "Wednesday club run",
    and neither branch here relaxes it.
    """
    if owns_workout(row["raw_title"], uid):
        return True
    return bool(row["marked"] and _answers_to(row, workout_id, schedule_id))


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


class GarminCalendarUnreadable(GarminWriterError):
    """A month's calendar could not be read, and why it could not.

    `transient` is the only part the athlete needs: a call that failed, or a
    Garmin answering "rate limited", is a condition that clears, and waiting is
    the whole remedy. An answer that arrived intact and is simply not a
    calendar this module can parse will arrive identically on every retry, and
    telling somebody to try again in a moment for that is telling them to wait
    for something that will not happen.
    """

    code = "calendar_unreadable"

    def __init__(self, message, transient=True, problem=None):
        super().__init__(message)
        self.transient = bool(transient)
        self.problem = problem


class CalendarStatus(str):
    """A read status that also carries why, without changing what it is.

    `status == CALENDAR_BLOCKED` is the only question nearly every caller
    asks, and it goes on being answered the same way — this is a `str`, equal
    to and interchangeable with the plain constants. The extra facts ride
    along for the one caller that has to tell the athlete what to do about it:
    whether waiting would help (`transient`), what did not parse (`problem`),
    and which months went unread (`unread`).

    They travel *with* the status rather than beside it because they are the
    same fact. Every version of this that kept them in a second variable
    ended up with one of the two out of date.
    """

    __slots__ = ("transient", "problem", "unread")

    def __new__(cls, value, transient=True, problem=None, unread=()):
        status = str.__new__(cls, value)
        status.transient = bool(transient)
        status.problem = problem
        status.unread = tuple(unread)
        return status


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


def _rows_under(obj, inherited=None):
    """Every session in this subtree, one row each.

    Garmin hangs a calendar in three shapes, and this reads all of them:

    - flat rows that each carry their own date;
    - one node per day whose children are that day's sessions — the enclosing
      date carries down to them;
    - a schedule row with the workout template *nested inside it*, which is
      the ordinary REST idiom for a schedule and the one shape where a single
      session's ids live on two levels: the schedule id on the row, the
      workout id and the marked name on the template underneath.

    That last shape is why this walks the tree instead of flattening it.
    Yielding every dict turned one session into two rows — a parent with the
    schedule id and no name, a child with the name and no schedule id. Only
    the child looked like ours, it had nothing to unschedule, and the stored
    schedule id could not stand in for it because its own parent row was
    already answering to that id. Update and reschedule refused permanently,
    and re-pushing landed back in the same place.

    Merging is deliberately narrow: one child, the same day, and nothing that
    contradicts. Anchoring is still the point — a day node holding two
    sessions keeps them apart, because pairing one session's id with another
    session's name is how this app came to adopt, and then overwrite, a
    workout that was never its own.
    """
    if isinstance(obj, list):
        rows = []
        for item in obj:
            rows.extend(_rows_under(item, inherited))
        return rows
    if not isinstance(obj, dict):
        return []

    own_date = _extract_date(obj) or inherited
    children = []
    for value in obj.values():
        children.extend(_rows_under(value, own_date))

    row = _normalize_row(obj, own_date)
    if not (row["workout_id"] or row["schedule_id"]):
        # An envelope — a day, a wrapper, the response itself. It is not a
        # session, and whatever is under it stands on its own.
        return children
    if len(children) == 1 and _same_session(row, children[0]):
        return [_merge_row(row, children[0])]
    return [row] + children


def _same_session(parent, child):
    """Whether a lone nested row is the enclosing row's own template.

    Nothing may contradict: same day, no id the two disagree about, no two
    different names. Rows that disagree are two sessions, and merging them is
    exactly the id-name mispairing this module refuses to do.
    """
    if parent["date"] != child["date"]:
        return False
    for key in ("workout_id", "schedule_id"):
        if parent[key] and child[key] and parent[key] != child[key]:
            return False
    if parent["raw_title"] and child["raw_title"] \
            and parent["raw_title"] != child["raw_title"]:
        return False
    return True


def _merge_row(parent, child):
    """One row out of a schedule row and the template nested in it."""
    raw = parent["raw_title"] or child["raw_title"]
    return {
        "date": parent["date"] or child["date"],
        "workout_id": parent["workout_id"] or child["workout_id"],
        "schedule_id": parent["schedule_id"] or child["schedule_id"],
        "title": strip_marker(raw),
        "raw_title": raw,
        "marked": bool(raw and MARKER_RE.search(raw)),
    }


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
    carry no ids of their own and are dropped; what is left is one row per
    session, with its own ids and its own name. A row this module cannot place
    on a day is dropped too, which is what makes the entry unreadable to
    `read_calendar` rather than silently missing.
    """
    return [row for row in _rows_under(entry) if row["date"]]


def _has_ids(entry):
    return bool(_first_id(entry, _WORKOUT_ID_KEYS)
                or _first_id(entry, _SCHEDULE_ID_KEYS))


def _shift_month(key, delta):
    """`"2026-08"` shifted by whole months, year rollover included."""
    index = int(key[:4]) * 12 + (int(key[5:7]) - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _plausible_months(dates):
    """Every month a copy of one session could plausibly be sitting in.

    `get_scheduled_workouts` is month-keyed, so "where is our copy" has to be
    asked one month at a time and the set of months is a decision, not a
    detail. Scoping it to the single month of the recorded date is the
    decision that produced two copies on the Edge: the athlete drags a session
    from Aug 31 to Sep 1 in Garmin Connect — an ordinary gesture, and Garmin's
    month grid shows the neighbouring months' days precisely so it can be made
    — and a lookup that reads only August concludes the copy is gone and sends
    a second one. A month boundary is not a safety boundary.

    So: the months of the dates in question, plus one either side of each,
    which is exactly how far a drag on a month grid can reach. Bounded on
    purpose — a read is a chance to be rate-limited immediately before a
    write, and an unbounded fan-out over the year would be its own outage.
    A copy dragged further than this is still out of reach, which is why the
    months that could not be accounted for are named rather than passed off
    as proof the calendar is clear.
    """
    months = []
    for date in dates:
        if not date:
            continue
        for delta in (-1, 0, 1):
            key = _shift_month(date[:7], delta)
            if key not in months:
                months.append(key)
    return sorted(months)


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
                raise GarminCalendarUnreadable(
                    f"could not read the Garmin calendar for {key}: {exc}",
                    transient=True, problem=f"the call failed ({exc})") from exc
            rows, problem = read_calendar(response)
            if problem:
                # Not cached: a body that is not an answer must never become a
                # remembered answer.
                #
                # An answer that *says* it failed — an error envelope, a 429 —
                # describes a condition on Garmin's side, which is the kind of
                # thing waiting clears. A body that arrived intact and simply
                # is not a calendar this module can parse will parse the same
                # way for ever, and the two get different advice.
                raise GarminCalendarUnreadable(
                    f"could not read the Garmin calendar for {key}: {problem}",
                    transient=bool(_error_signal(response)), problem=problem)
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
        except GarminCalendarUnreadable as exc:
            # Still `CALENDAR_BLOCKED`, and still compares equal to it. What
            # rides along is why, so the athlete can be told whether waiting
            # is the remedy or whether nothing will ever change.
            return ([], CalendarStatus(CALENDAR_BLOCKED,
                                       transient=exc.transient,
                                       problem=exc.problem))
        except GarminWriterError as exc:
            return ([], CalendarStatus(CALENDAR_BLOCKED, problem=str(exc)))

    def _read_window(self, dates, refresh=True):
        """`(rows, status)` for every month a copy could plausibly be in.

        The month of each date and one either side — see `_plausible_months`.
        A month that could not be read is not fatal on its own: only the
        months of the dates themselves are load-bearing, and a neighbour
        Garmin would not hand over narrows the search rather than stopping the
        push. Which months went unread rides on the status, so a caller that
        writes anyway can say what it did not check instead of implying it
        checked everything.
        """
        primary = [date[:7] for date in dates if date]
        rows, unread, blocked = [], [], []
        for key in _plausible_months(dates):
            found, status = self.read_scheduled_rows(
                int(key[:4]), int(key[5:7]), refresh=refresh)
            if status == CALENDAR_BLOCKED:
                unread.append(key)
                if key in primary:
                    blocked.append(status)
                continue
            rows.extend(found)
        if blocked:
            # The month the decision is actually about could not be read, so
            # there is no answer. Prefer a structural reason to report: it is
            # the one whose remedy is not "wait".
            worst = next((status for status in blocked
                          if not getattr(status, "transient", True)),
                         blocked[0])
            return ([], CalendarStatus(
                CALENDAR_BLOCKED,
                transient=all(getattr(status, "transient", True)
                              for status in blocked),
                problem=getattr(worst, "problem", None),
                unread=unread))
        return (rows, CalendarStatus(CALENDAR_READ, unread=unread))

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

    # `find_owned_scheduled` lived here: `owned_rows` narrowed to one day,
    # flattened to `(workout_id, schedule_id, status)` with a third vocabulary
    # of its own — "resolved" / "absent" / "blocked" — for facts
    # `CALENDAR_READ`, `CALENDAR_BLOCKED` and an empty list already carry. It
    # is gone rather than left unused. Its one caller, the create path's
    # duplicate guard, now reads `owned_rows` directly, which is what let that
    # guard see the copy sitting one day — or one month — over instead of
    # answering "absent" about a day it had looked at and a calendar it had
    # not. Two spellings of "the calendar could not be read" is one too many
    # for a project whose every defect so far has come from those two facts
    # collapsing into one.

    def owned_rows(self, uid, date, refresh=True, workout_id=None,
                   schedule_id=None, also=None):
        """`(rows, status)` for every row this app can prove it wrote for
        `uid`, wherever the athlete has since moved it to.

        `date` says where to look, not which day holds the truth. The marker
        is per-uid and a uid holds one push record, so any row carrying it is
        this app's copy of that session — including the day, or the *month*,
        the athlete dragged it to in Garmin Connect, which the recorded date
        knows nothing about. `_plausible_months` decides how far that search
        reaches; a lookup scoped to one month reported a dragged copy gone and
        sent a second one.

        More than one row is possible and is exactly the state worth seeing:
        it is the duplicate on the athlete's Edge that this module exists to
        avoid. `status` carries the read itself — `CALENDAR_BLOCKED` means the
        empty list is an absence of evidence, not evidence of absence.
        """
        _validate_date(date)
        if also:
            _validate_date(also)
        rows, status = self._read_window([date, also], refresh=refresh)
        return ([row for row in rows
                 if _is_ours(row, uid, workout_id, schedule_id)], status)


    def locate_owned_copies(self, uid, workout_id, schedule_id, date,
                            also=None):
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

        The marker is per-uid, and a uid is not forever: when TrainingPeaks
        regenerates its feed every uid changes, and `rematch_orphans` re-keys
        the push record to the new one — the whole defense against that
        regeneration wiping the athlete's work. The marker already written on
        the Garmin calendar still encodes the old uid, so it is *this app's
        marker under a name this app no longer uses*, which `_is_ours` reads
        as ours when the record's own ids point at that row. Refusing it
        instead told the athlete, once per workout, to go and delete his own
        synced sessions in Garmin Connect — on the strength of "it carries no
        marker of this app's", which `row["marked"]` says plainly is untrue.

        `also` is a second date to search around — the day a push is moving a
        session *to*, as well as the day it was recorded on. Both, because a
        copy is only ever missed by not looking where it is.

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
        if also:
            _validate_date(also)
        # Fresh, always: this decides whether a write happens, and a month
        # remembered from a render is not evidence about the calendar now.
        month, status = self._read_window([date, also], refresh=True)
        if status == CALENDAR_BLOCKED:
            return ([], CalendarStatus(OWNED_UNREADABLE,
                                       transient=status.transient,
                                       problem=status.problem,
                                       unread=status.unread))

        stored_workout = _coerce_id(workout_id)
        stored_schedule = _coerce_id(schedule_id)

        ours = [row for row in month
                if _is_ours(row, uid, stored_workout, stored_schedule)]
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
                    return ([], CalendarStatus(OWNED_UNADDRESSABLE,
                                               unread=status.unread))
                resolved.append(dict(row, schedule_id=found))
            return (resolved, CalendarStatus(OWNED_RESOLVED,
                                             unread=status.unread))

        # Nothing of ours. Does anything answer to the ids we hold? If so this
        # app is pointing at a session it cannot claim, and the honest thing is
        # to say so rather than write on it. Anything answering to those ids
        # that carried this app's marker has already been taken as ours above,
        # so what is left here is unmarked — which is exactly what the refusal
        # goes on to tell the athlete.
        claimed = any(_answers_to(row, stored_workout, stored_schedule)
                      for row in month)
        return ([], CalendarStatus(OWNED_UNPROVEN if claimed else OWNED_ABSENT,
                                   unread=status.unread))

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
        # `proof_months` is which months were read to get there — an absence
        # proved for one month says nothing about another, which is exactly
        # what a copy dragged across a month boundary went on to prove.
        # `unchecked` is the other half of the same bookkeeping: months this
        # push wanted and did not get.
        proven = []
        proof = "unknown"
        proof_months = set()
        unchecked = set()
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
                ours, status = self.owned_rows(
                    uid, date, refresh=True, workout_id=old_workout_id,
                    schedule_id=old_schedule_id)
                unchecked.update(status.unread)
                if status == CALENDAR_BLOCKED:
                    # Nothing is being written either way, so this is not a
                    # refusal; it is a claim this app is not entitled to make.
                    result.reason = (
                        "Unchanged since the last push. "
                        + _cannot_confirm_message(date, status))
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
                    # On the calendar, just not where the record says — a
                    # different day, or a day in another month, which is one
                    # drag on Garmin's month grid and used to be invisible
                    # here. The athlete moved it and that is their call;
                    # nothing needs sending and nothing gets moved back.
                    result.workout_id = ours[0]["workout_id"] or old_workout_id
                    result.schedule_id = (ours[0]["schedule_id"]
                                          or old_schedule_id)
                    result.reason = (
                        "Unchanged since the last push. This app's copy is on "
                        f"your Garmin calendar for {ours[0]['date']}, not "
                        f"{date} — moved in Garmin Connect, and left there.")
                    return result
                # Read, and no copy of ours anywhere it could plausibly be: it
                # is gone from the calendar, so send it again rather than
                # report a session onto a device that never got one.
                result.action = "create"
                result.state = "pending"
                proof = OWNED_ABSENT
                proof_months = set(_plausible_months([date])) - unchecked
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
                        uid, old_workout_id, old_schedule_id, old_date,
                        also=date)
                    unchecked.update(proof.unread)
                    proof_months = (set(_plausible_months([old_date, date]))
                                    - unchecked)
                doing = ("moving it to " + date if result.action == "reschedule"
                         else "sending the new version")
                if proof == OWNED_UNREADABLE:
                    # Whether there is anything to do about it depends on what
                    # kind of unreadable it was, and the message says which.
                    raise GarminConflict(_unreadable_calendar_message(
                        title, old_date, doing, proof))
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
                result.reason += _moved_copy_note(proven, old_date)
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
                result.reason += _moved_copy_note(proven, old_date)
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
                if proof == OWNED_ABSENT and date[:7] in proof_months:
                    # Already proved, moments ago and against the months this
                    # day could plausibly be in: no copy of ours anywhere in
                    # them, so none on this day. Reading again would only be a
                    # second chance to be rate-limited immediately before a
                    # write.
                    ours, adopt_status = [], CALENDAR_READ
                else:
                    ours, adopt_status = self.owned_rows(uid, date,
                                                         refresh=True)
                    unchecked.update(adopt_status.unread)
                if adopt_status == CALENDAR_BLOCKED:
                    # The guard came back with no answer at all. An unread
                    # calendar is not an empty one, and uploading on the
                    # strength of it is precisely the duplicate this guard
                    # exists to prevent: the copy from an earlier push sits on
                    # that day, unseen, and a second one joins it on the Edge.
                    raise GarminConflict(_unreadable_calendar_message(
                        title, date, "sending it now", adopt_status))
                adopted = next((row for row in ours
                                if row["date"] == date and row["workout_id"]),
                               None)
                elsewhere = [row for row in ours if row["date"] != date]
                if not adopted and elsewhere:
                    # A copy of *this* session — this app's marker, this uid —
                    # is on the calendar, just not on this day. There is no
                    # record pointing at it to move it by, so sending this one
                    # would put a second copy on the Edge with nothing here
                    # able to take either off. Refusing is recoverable; two
                    # copies are not.
                    raise GarminConflict(_moved_copy_conflict_message(
                        title, elsewhere[0]["date"], date))
                if adopted:
                    result.action = "noop"
                    result.workout_id = adopted["workout_id"]
                    result.schedule_id = (
                        adopted["schedule_id"]
                        or self.find_schedule_id(adopted["workout_id"], date))
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

            if unchecked and result.action != "noop":
                # Something was written, and part of the calendar this push
                # wanted to look at could not be read. Say which part: an
                # unread month is where a copy hides, and passing it off as
                # "checked" is the whole shape of this defect.
                result.reason += _unchecked_months_note(unchecked)

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


def _unreadable_calendar_message(title, date, doing, status=None):
    """What the athlete is told when a write is refused because the calendar
    could not be read.

    Different from `_conflict_message` in the one way that matters to them:
    nothing is stuck and there is nothing to tidy up in Garmin Connect.

    Which leaves what they *should* do, and that depends on why the read
    failed. A call that failed, or a Garmin answering "rate limited", clears
    on its own and "try again in a moment" is the whole remedy. An answer that
    arrived intact and simply is not a calendar this module can parse — one
    sibling row with a date key nobody here knows, a null body — will arrive
    identically every time, and telling somebody to wait for that is telling
    them to wait for something that never happens. `read_calendar` is strict
    per entry by design, so one odd row blocks a whole month; that is
    fail-closed and right, but it is a fault here, not weather.
    """
    clean = strip_marker(title) or "That workout"
    opening = f"{doing[:1].upper()}{doing[1:]}"
    if status is not None and not getattr(status, "transient", True):
        detail = _for_display(getattr(status, "problem", None))
        return (
            f"Not sent: Garmin's answer for {date or 'that day'} wasn't a "
            f"calendar this app could read ({detail}), so it can't tell "
            f'whether a copy of "{clean}" is already on it. {opening} could '
            "leave two copies on your Edge, so nothing was sent. Retrying "
            "will get the same answer — this one is a fault in this app, not "
            "a passing glitch. Until it is fixed you can put the session on "
            "your calendar in Garmin Connect yourself."
        )
    return (
        f'Not sent: this app couldn\'t read your Garmin calendar for '
        f"{date or 'that day'}, so it can't tell whether a copy of "
        f'"{clean}" is already on it. {opening} could '
        "leave two copies on your Edge, so nothing was sent. Try again in a "
        "moment."
    )


def _cannot_confirm_message(date, status):
    """What a no-op says when it could not read the calendar it was checking.

    Nothing is written either way, so this is not a refusal — it is a claim
    the module is not entitled to make. Same split as above: whether waiting
    is the remedy.
    """
    if not getattr(status, "transient", True):
        detail = _for_display(getattr(status, "problem", None))
        return (
            f"This app couldn't read your Garmin calendar for {date} — "
            f"Garmin's answer wasn't a calendar it understands ({detail}) — "
            "so it can't confirm the copy is still on it. Retrying will get "
            "the same answer; this one is a fault in this app."
        )
    return (
        f"This app couldn't read your Garmin calendar for {date} just now, "
        "so it can't confirm the copy is still on it."
    )


def _moved_copy_conflict_message(title, found_on, wanted_on):
    """What the athlete is told when this app's copy of the session it is
    about to send is already on the calendar, on another day.

    Sending would leave two copies of one session on the Edge, and there is no
    push record pointing at the other one to move it by. Refusing is
    recoverable in Garmin Connect; two copies are not recoverable here at all,
    because nothing in this module takes a session off a calendar.
    """
    clean = strip_marker(title) or "That workout"
    return (
        f'Not sent: this app\'s copy of "{clean}" is already on your Garmin '
        f"calendar for {found_on} — moved there in Garmin Connect, and this "
        f"app has no record tying it to {wanted_on} any more. Sending it to "
        f"{wanted_on} now would leave two copies of the same session on your "
        f'Edge. Move "{clean}" back to {wanted_on} in Garmin Connect (or '
        "remove it), then push again."
    )


def _moved_copy_note(rows, recorded_for):
    """Appended when the copy a push acted on was not where it was recorded.

    The copy is found by its marker, wherever the athlete dragged it to, which
    is the right thing to act on — but "already on Garmin for 2026-08-01" is
    then not what happened, and a push that quietly reaches into another month
    is exactly the kind of silence this module keeps having to undo.
    """
    found = sorted({row["date"] for row in rows
                    if row["date"] and row["date"] != recorded_for})
    if not found:
        return ""
    return (
        " (this app's earlier copy was on " + ", ".join(found)
        + f", not {recorded_for or 'the day recorded'} — you had moved it in "
        "Garmin Connect, and that is the copy this push acted on)"
    )


def _unchecked_months_note(months):
    """Appended when a copy was sent without every plausible month being read.

    Naming them is the point: an unread month is precisely where a copy hides,
    and reporting a clean push over one is how a session ended up on the Edge
    twice.
    """
    listed = ", ".join(sorted(months))
    return (
        f" (couldn't read your Garmin calendar for {listed}, so if a copy of "
        "this session had been moved there you may now have two — check "
        f"{listed} in Garmin Connect)"
    )


def _for_display(text):
    """A diagnostic string with any ownership marker taken out of it.

    Garmin's own rows go into these messages verbatim, and one of ours read
    back carries `[ts:1a2b3c4d]` in its name. The marker is this app's
    bookkeeping and has no business on the athlete's screen.
    """
    return MARKER_RE.sub("", str(text or "no detail")).strip() or "no detail"


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
