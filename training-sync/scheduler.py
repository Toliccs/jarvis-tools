"""Workout classification + rescheduling engine.

Implements the RESCHEDULING / ADJUSTMENT ENGINE spec: free-text
classification of TrainingPeaks calendar events (§1), moving workouts off
days the athlete can't train (§2), race proximity rules (§3), Garmin signal
adjustment (§4) and the plain-English reason strings (§5).

Two hard properties, both structural rather than conventional:

* **Pure.** Nothing in this module opens a socket, reads a file, or holds a
  credential. It imports no client. Data in, ``AdjustmentPlan`` out. That
  makes it unit-testable and makes it impossible for a scheduling bug to
  become a write to somebody's calendar.
* **Never applies anything.** Every ``Proposal`` carries
  ``requires_approval=True`` and the attribute refuses to be set to False.
  There is no flag, kwarg or env var that turns a proposal into an action.
  Applying is a separate module's job, against Garmin Connect only —
  TrainingPeaks is read-only by nature (it's an ICS feed) and by policy.

Three places where the implementation deliberately reads the spec rather
than transcribing it, because a literal transcription misbehaves:

1. **§2.2 week-shape penalties are scored as a delta against the coach's
   own week.** ``spacing_penalty`` and ``day_load_imbalance`` are computed
   on the whole week after the hypothetical move (as specified) but then
   have the planned week's value subtracted. Absolute values run to several
   hundred points for a perfectly normal Tue/Thu week, which would put every
   candidate above ``MIN_ACCEPTABLE_SCORE`` (120) and escalate every single
   move to the fallback ladder. Ranking between candidates is unaffected
   (it's a constant offset); what changes is that the acceptability gate now
   means "this doesn't make the week worse than it already was".
2. **§1.2 duration parsing masks interval expressions first.** ``"Threshold
   3x12min"`` under the literal first-match-wins ladder parses as a 12-minute
   workout. Interval spans are matched and blanked before duration parsing,
   so the rep length can't be mistaken for the session length. The ``XhYY``
   pattern is also tried before the ``hours`` pattern, since ``3h30``
   otherwise matches ``3h`` and silently loses the 30.
3. **§1.1 Tier 1's negation guard also covers ``pre-race`` / ``post-race``.**
   Without it, "Pre-race spin" matches ``\\brace\\b`` and classifies as a
   race, which would then pin an easy spin as an unmovable A-priority event.

Public API:
    classify_workout(title, description) -> dict
    propose_adjustments(workouts, unavailable_days, garmin_signal, race_dates)
        -> list[Proposal]
    build_adjustment_plan(...) -> AdjustmentPlan   (the richer, typed form)
"""

from __future__ import annotations

import hashlib
import re
import statistics
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import IntEnum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# §7 CONFIG CONSTANTS
# ---------------------------------------------------------------------------

TODAY_CUTOFF_HOUR = 18
ALLOW_SPILL_NEXT_WEEK = False
ALLOW_DOUBLES = False
MAX_SESSIONS_PER_DAY = 1
MAX_DAY_DURATION_MIN = 240
DAY_LOAD_CEILING_MULT = 1.6
TAPER_DAYS = {"A": 7, "B": 4, "C": 2, "UNKNOWN": 4}
FINAL_PROTECT_DAYS = 2
POST_RACE_DAYS_PER_HOUR = 1
POST_RACE_DAYS_CAP = 3
POST_RACE_DAYS_DEFAULT = 2
READINESS_MAX_AGE_HOURS = 18
READINESS_BANDS = (25, 50, 75)
MIN_ACCEPTABLE_SCORE = 120

W_DIST = 10
W_SPACING = 25
W_WEEKEND = 8
W_ORDER = 5
W_LOADBAL = 3
W_LOWCONF = 6

LONG_RIDE_MIN = 150  # what counts as a "needs a whole weekend morning" ride
SHORTEN_STEP_MIN = 15


class EngineConfig:
    """All of §7 in one overridable object.

    Instantiate with keyword overrides to change engine behaviour in tests
    or from the web layer, e.g. ``EngineConfig(allow_doubles=True)``.
    """

    def __init__(self, **overrides):
        self.today_cutoff_hour = TODAY_CUTOFF_HOUR
        self.allow_spill_next_week = ALLOW_SPILL_NEXT_WEEK
        self.allow_doubles = ALLOW_DOUBLES
        self.max_sessions_per_day = 2 if ALLOW_DOUBLES else MAX_SESSIONS_PER_DAY
        self.max_day_duration_min = MAX_DAY_DURATION_MIN
        self.day_load_ceiling_mult = DAY_LOAD_CEILING_MULT
        self.taper_days = dict(TAPER_DAYS)
        self.final_protect_days = FINAL_PROTECT_DAYS
        self.readiness_max_age_hours = READINESS_MAX_AGE_HOURS
        self.readiness_bands = READINESS_BANDS
        self.min_acceptable_score = MIN_ACCEPTABLE_SCORE
        self.w_dist = W_DIST
        self.w_spacing = W_SPACING
        self.w_weekend = W_WEEKEND
        self.w_order = W_ORDER
        self.w_loadbal = W_LOADBAL
        self.w_lowconf = W_LOWCONF
        for key, value in overrides.items():
            if not hasattr(self, key):
                raise TypeError(f"unknown EngineConfig option: {key}")
            setattr(self, key, value)
        if overrides.get("allow_doubles") and "max_sessions_per_day" not in overrides:
            self.max_sessions_per_day = 2


# ---------------------------------------------------------------------------
# §0 DATA STRUCTURES
# ---------------------------------------------------------------------------


class Intensity(IntEnum):
    """Ordered — comparisons are meaningful."""

    REST = 0
    RECOVERY = 1
    ENDURANCE = 2
    TEMPO = 3
    THRESHOLD = 4
    VO2 = 5  # includes anaerobic / neuromuscular / sprint
    RACE = 6
    UNKNOWN = -1  # NEVER coerced to a number in comparisons — see §1.5


class Confidence(IntEnum):
    NONE = 0  # UNKNOWN only
    LOW = 1  # weak single token, or conflicting evidence
    MED = 2  # inferred from structure (NxM, duration), not a stated zone
    HIGH = 3  # explicit zone/keyword, or explicit TSS/IF in description


HARD_CLASSES = {Intensity.THRESHOLD, Intensity.VO2, Intensity.RACE}
EASY_CLASSES = {Intensity.RECOVERY, Intensity.ENDURANCE}

IF_PROXY = {
    Intensity.REST: 0.0,
    Intensity.RECOVERY: 0.55,
    Intensity.ENDURANCE: 0.68,
    Intensity.TEMPO: 0.80,
    Intensity.THRESHOLD: 0.90,
    Intensity.VO2: 0.95,
    Intensity.RACE: 1.00,
}

CLASS_DEFAULT_MIN = {
    Intensity.RECOVERY: 45,
    Intensity.ENDURANCE: 120,
    Intensity.TEMPO: 75,
    Intensity.THRESHOLD: 75,
    Intensity.VO2: 70,
    Intensity.RACE: 120,
}

MIN_USEFUL_MIN = {
    Intensity.RECOVERY: 30,
    Intensity.ENDURANCE: 60,
    Intensity.TEMPO: 45,
    Intensity.THRESHOLD: 45,
    Intensity.VO2: 40,
}

# §2.4 rung 5: the downgrade that makes an otherwise-illegal slot legal.
# Note this differs from §4.4's readiness ladder (VO2 -> THRESHOLD): here the
# point is to stop the session being *hard* at all, so it clears H1.
DOWNGRADE_FOR_LEGALITY = {
    Intensity.VO2: Intensity.ENDURANCE,
    Intensity.RACE: None,  # never
    Intensity.THRESHOLD: Intensity.TEMPO,
    Intensity.TEMPO: Intensity.ENDURANCE,
}

# §4.4 readiness ladder: one step easier.
DOWNGRADE_ONE_STEP = {
    Intensity.VO2: Intensity.THRESHOLD,
    Intensity.THRESHOLD: Intensity.TEMPO,
    Intensity.TEMPO: Intensity.ENDURANCE,
}

INTENSITY_PHRASE = {
    Intensity.REST: "rest day",
    Intensity.RECOVERY: "recovery spin",
    Intensity.ENDURANCE: "endurance ride",
    Intensity.TEMPO: "tempo session",
    Intensity.THRESHOLD: "threshold session",
    Intensity.VO2: "VO2 session",
    Intensity.RACE: "race",
    Intensity.UNKNOWN: "session",
}

ACTIONS = ("move", "soften", "drop", "keep")

# Proposal kind -> the coarse action vocabulary the calendar UI renders.
KIND_TO_ACTION = {
    "MOVE": "move",
    "SWAP": "move",
    "MERGE": "move",
    "SHORTEN": "soften",
    "DOWNGRADE": "soften",
    "DROP": "drop",
    "KEEP": "keep",
    "NO_PROPOSAL": "keep",
}


@dataclass
class Classification:
    intensity: Intensity
    confidence: Confidence
    min_possible: Intensity
    max_possible: Intensity
    duration_min: Optional[int] = None
    duration_source: str = "none"  # "explicit" | "class_default" | "none"
    tss: Optional[float] = None
    load: Optional[float] = None
    load_confidence: Confidence = Confidence.NONE
    sport: str = "unknown"
    is_openers: bool = False
    is_key: bool = False
    matched_rules: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    user_override: bool = False
    warnings: List[str] = field(default_factory=list)

    @property
    def is_hard(self) -> bool:
        """§1.5(1): UNKNOWN is evaluated at ``max_possible`` for every hard
        constraint, so it is never stacked next to a known hard day."""
        return self.max_possible in HARD_CLASSES

    @property
    def is_easy(self) -> bool:
        """UNKNOWN is never "easy" — §1.5(3) forbids using ``min_possible``
        to satisfy a requirement that a slot be easy."""
        return self.max_possible in EASY_CLASSES and self.intensity != Intensity.UNKNOWN

    @property
    def is_rest(self) -> bool:
        return self.intensity == Intensity.REST

    @property
    def effective_duration_min(self) -> int:
        if self.duration_min is not None:
            return self.duration_min
        if self.intensity in CLASS_DEFAULT_MIN:
            return CLASS_DEFAULT_MIN[self.intensity]
        return 0

    def to_dict(self) -> dict:
        return {
            "sport": self.sport,
            "intensity": self.intensity.name,
            "intensity_value": int(self.intensity),
            "confidence": self.confidence.name,
            "confidence_value": int(self.confidence),
            "min_possible": self.min_possible.name,
            "max_possible": self.max_possible.name,
            "duration_min": self.duration_min,
            "duration_source": self.duration_source,
            "tss": self.tss,
            "load": round(self.load, 1) if self.load is not None else None,
            "load_confidence": self.load_confidence.name,
            "is_openers": self.is_openers,
            "is_key": self.is_key,
            "is_race": self.intensity == Intensity.RACE,
            "is_hard": self.is_hard,
            "matched_rules": list(self.matched_rules),
            "evidence": list(self.evidence),
            "user_override": self.user_override,
            "warnings": list(self.warnings),
            "question": unknown_question(self) if self.intensity == Intensity.UNKNOWN else None,
        }


@dataclass
class PlannedWorkout:
    uid: str
    date: date
    title: str
    description: str = ""
    classification: Optional[Classification] = None
    origin: str = "trainingpeaks_feed"  # "trainingpeaks_feed" | "user"

    @property
    def duration_min(self) -> int:
        return self.classification.effective_duration_min if self.classification else 0

    @property
    def load(self) -> Optional[float]:
        return self.classification.load if self.classification else None

    @property
    def intensity(self) -> Intensity:
        return self.classification.intensity if self.classification else Intensity.UNKNOWN

    @property
    def is_hard(self) -> bool:
        return bool(self.classification and self.classification.is_hard)

    @property
    def is_rest(self) -> bool:
        return bool(self.classification and self.classification.is_rest)

    @property
    def is_session(self) -> bool:
        """Rest entries are calendar furniture, not sessions that occupy a day."""
        return not self.is_rest


@dataclass
class RaceRef:
    date: date
    title: str = "your race"
    priority: str = "B"  # "A" | "B" | "C" | "UNKNOWN"
    duration_min: Optional[int] = None
    taper_days: int = 4

    def __post_init__(self):
        self.priority = (self.priority or "B").upper()
        if self.priority not in TAPER_DAYS:
            self.priority = "UNKNOWN"
        self.taper_days = TAPER_DAYS[self.priority]

    @property
    def post_race_days(self) -> int:
        """§3.5 — one recovery day per hour raced, clamped to [1, 3]."""
        if self.duration_min is None:
            return POST_RACE_DAYS_DEFAULT
        hours = -(-int(self.duration_min) // 60)  # ceil
        return max(1, min(POST_RACE_DAYS_CAP, hours * POST_RACE_DAYS_PER_HOUR))

    @property
    def taper_window(self) -> Tuple[date, date]:
        return (self.date - timedelta(days=self.taper_days), self.date - timedelta(days=1))


@dataclass
class DayContext:
    date: date
    weekday: int
    workouts: List[PlannedWorkout]
    is_unavailable: bool = False
    unavailable_reason: Optional[str] = None
    is_race_day: bool = False
    is_planned_rest: bool = False
    in_taper_window: bool = False
    in_post_race_window: bool = False
    is_past: bool = False
    day_load: Optional[float] = None
    max_intensity: Intensity = Intensity.REST


@dataclass
class WeekSummary:
    total_load: Optional[float]
    load_known_fraction: float
    rest_days: int
    hard_days: int
    longest_ride_min: int

    def to_dict(self) -> dict:
        return {
            "total_load": round(self.total_load, 1) if self.total_load is not None else None,
            "load_known_fraction": round(self.load_known_fraction, 2),
            "rest_days": self.rest_days,
            "hard_days": self.hard_days,
            "longest_ride_min": self.longest_ride_min,
        }


@dataclass
class Operation:
    """One concrete, reversible change. Garmin Connect is the only write
    target; nothing here ever touches TrainingPeaks."""

    op: str  # "schedule" | "unschedule" | "reschedule" | "modify_note"
    workout_uid: str
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    new_duration_min: Optional[int] = None
    new_intensity: Optional[Intensity] = None
    note: Optional[str] = None
    inverse: Optional["Operation"] = None

    def to_dict(self) -> dict:
        return {
            "op": self.op,
            "workout_uid": self.workout_uid,
            "from_date": _iso(self.from_date),
            "to_date": _iso(self.to_date),
            "new_duration_min": self.new_duration_min,
            "new_intensity": self.new_intensity.name if self.new_intensity else None,
            "note": self.note,
        }


@dataclass
class RejectedOption:
    date: date
    reason: str
    rule_id: str

    def to_dict(self) -> dict:
        return {"date": _iso(self.date), "reason": self.reason, "rule_id": self.rule_id}


class ProposalError(ValueError):
    pass


@dataclass
class Proposal:
    """A suggestion. Never an action.

    The first six fields are the contract the calendar UI renders; everything
    after them is the machine trace the rationale panel and the apply layer
    need. ``requires_approval`` is True and cannot be set otherwise.
    """

    workout_uid: str
    original_date: date
    proposed_date: Optional[date]
    action: str  # "move" | "soften" | "drop" | "keep"
    reason: str
    confidence: Confidence

    kind: str = "MOVE"
    workout: Optional[PlannedWorkout] = None
    operations: List[Operation] = field(default_factory=list)
    reason_details: List[str] = field(default_factory=list)
    tradeoff: Optional[str] = None
    question: Optional[str] = None
    rule_ids: List[str] = field(default_factory=list)
    score: float = 0.0
    alternatives: List[RejectedOption] = field(default_factory=list)
    blocked_pending_input: bool = False
    signals_used: List[str] = field(default_factory=list)
    new_duration_min: Optional[int] = None
    new_intensity: Optional[Intensity] = None
    requires_approval: bool = True
    id: str = ""

    def __post_init__(self):
        # §5.1 — a proposal without a reason is a bug, not a degraded output.
        # Raised rather than asserted so `python -O` can't strip the check.
        if not (self.reason or "").strip():
            raise ProposalError(f"Proposal for {self.workout_uid} has no reason string.")
        if self.action not in ACTIONS:
            raise ProposalError(f"unknown action {self.action!r}; expected one of {ACTIONS}")
        if self.kind not in KIND_TO_ACTION:
            raise ProposalError(f"unknown kind {self.kind!r}")
        object.__setattr__(self, "requires_approval", True)
        if not self.id:
            object.__setattr__(self, "id", make_proposal_id(
                self.workout_uid, self.kind, self.original_date, self.proposed_date))

    def __setattr__(self, name, value):
        # §6.2 — structurally un-disable-able.
        if name == "requires_approval" and value is not True:
            raise ProposalError(
                "Proposal.requires_approval is always True; proposals are never self-applying."
            )
        super().__setattr__(name, value)

    @property
    def target_date(self) -> Optional[date]:
        return self.proposed_date

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workout_uid": self.workout_uid,
            "title": self.workout.title if self.workout else None,
            "original_date": _iso(self.original_date),
            "proposed_date": _iso(self.proposed_date),
            "action": self.action,
            "kind": self.kind,
            "reason": self.reason,
            "reason_details": list(self.reason_details),
            "tradeoff": self.tradeoff,
            "question": self.question,
            "confidence": self.confidence.name,
            "score": round(self.score, 1),
            "rule_ids": list(self.rule_ids),
            "alternatives": [a.to_dict() for a in self.alternatives],
            "operations": [o.to_dict() for o in self.operations],
            "blocked_pending_input": self.blocked_pending_input,
            "signals_used": list(self.signals_used),
            "new_duration_min": self.new_duration_min,
            "new_intensity": self.new_intensity.name if self.new_intensity else None,
            "requires_approval": True,
        }


@dataclass
class AdjustmentPlan:
    week_start: date
    trigger: str
    proposals: List[Proposal] = field(default_factory=list)
    unresolved: List[Proposal] = field(default_factory=list)
    week_summary_before: Optional[WeekSummary] = None
    week_summary_after: Optional[WeekSummary] = None
    warnings: List[str] = field(default_factory=list)
    generated_at: Optional[datetime] = None

    @property
    def all_proposals(self) -> List[Proposal]:
        return list(self.proposals) + list(self.unresolved)

    def to_dict(self) -> dict:
        return {
            "week_start": _iso(self.week_start),
            "trigger": self.trigger,
            "proposals": [p.to_dict() for p in self.proposals],
            "unresolved": [p.to_dict() for p in self.unresolved],
            "week_summary_before": self.week_summary_before.to_dict()
            if self.week_summary_before else None,
            "week_summary_after": self.week_summary_after.to_dict()
            if self.week_summary_after else None,
            "warnings": list(self.warnings),
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
        }


def make_proposal_id(uid: str, kind: str, from_date, to_date) -> str:
    """§6.4 — deterministic: same inputs, same id, so re-running after a
    partial approval can't duplicate or contradict what was already applied."""
    raw = f"{uid}|{kind}|{_iso(from_date)}|{_iso(to_date)}"
    return "prp_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _iso(value) -> Optional[str]:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


# ---------------------------------------------------------------------------
# §1 CLASSIFICATION FROM FREE TEXT
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Lowercase, NFKD-fold (so ``vo₂`` -> ``vo2``, ``café`` -> ``cafe``),
    strip combining marks. Punctuation is kept — ``2:00`` and ``5x5`` need it."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


def override_key(title: str) -> str:
    """§1.7 — digits collapse to ``#`` so "5x5 VO2" and "6x5 VO2" share a key."""
    return re.sub(r"\d+", "#", _normalize(title or "").strip())


# -- Tier 1: race ------------------------------------------------------------
# The negation guard runs first. `pre-race` / `post-race` / `race week` are
# additions to the spec's literal list: without them "Pre-race spin" matches
# \brace\b and an easy 40-minute spin becomes an immovable A-priority event.
_R_RACE_NEGATE = re.compile(
    r"race pace|race sim(?:ulation)?|race prep|race rehearsal|race specific"
    r"|race winning|race day nutrition|pre-? ?race|post-? ?race|race week"
)
_R_RACE = re.compile(
    r"\brace\b|\bcrit(?:erium)?\b|\bgran ?fondo\b|\bsportive\b|\btt\b"
    r"|\btime ?trial\b|\bstage \d\b|\bhill ?climb comp|\bpriority [abc]\b"
    r"|\b[abc][- ]race\b"
)
_R_RACE_EVENT = re.compile(r"\bevent\b")
_R_RACE_PRIORITY = re.compile(r"\bpriority ([abc])\b|\b([abc])[- ]race\b")

# -- Tier 2: rest ------------------------------------------------------------
_R_REST = re.compile(
    r"\brest day\b|\bday off\b|\boff day\b|\bno training\b|\bno rid(?:e|ing)\b"
    r"|^\s*rest\s*$|\bfull rest\b|\brecovery day\b",
    re.MULTILINE,
)
# Stripped before the activity-token guard so "Rest day - no ride" doesn't
# disqualify itself on the word "ride".
_R_REST_PHRASES = re.compile(
    r"\bno (?:training|rid(?:e|ing)|run(?:ning)?|swim|workout|session)\b"
    r"|\bday off\b|\boff day\b|\brest day\b|\bfull rest\b|\brecovery day\b"
)
_ACTIVITY_TOKEN = re.compile(
    r"\b(?:rid(?:e|es|ing)|spin|spins|run|runs|running|jog\w*|swim\w*|walk"
    r"|gym|core|strength|zwift|turbo|trainer|intervals?)\b"
)

# -- Tier 3 / 3b: recovery, openers -----------------------------------------
_R_RECOVERY = re.compile(
    r"\brecovery\b|\bactive recovery\b|\beasy spin\b|^\s*spin\s*$|\bz1\b"
    r"|\bzone 1\b|\bcoffee ride\b|\bshakeout\b|\blegs? loosener\b|\bregen\b"
    r"|\bcafe ride\b",
    re.MULTILINE,
)
_R_OPENERS = re.compile(r"\bopeners?\b|\bprimers?\b|\bactivation\b|\bpre-? ?race spin\b")
# The rest *between intervals* is not a recovery ride. Without this guard
# "Threshold 3x12min / 6min recovery between efforts" classifies as RECOVERY
# on the strength of the word "recovery" in the description, and a threshold
# session gets treated as an easy day for every spacing rule downstream.
_R_INTERVAL_RECOVERY = re.compile(
    r"\b\d+\s*(?:min|mins|minutes|sec|secs|s|')?\s*(?:easy\s+)?recover(?:y|ies)\b"
    r"|\brecover(?:y|ies)\s+(?:between|of|@|jog|spin between)"
    r"|\bwith\s+\d+\s*(?:min|mins|minutes)?\s*recover"
)
# A percentage-of-FTP reference below the threshold band is a *ceiling*, not a
# target: "keep it under 55% FTP" is the defining line of a recovery spin, but
# the bare \bftp\b in R_THRESHOLD reads it as threshold work, marks the day
# hard, and every spacing rule downstream then defends a rest day as if it
# were an interval session. Sub-threshold percentages are blanked before the
# named-zone tier runs. 88%+ still counts — sweet spot and above cite FTP for
# real. Only the *suppression* is done here: the number never upgrades a
# session, because interval target power ("3x12 @ 95-100% FTP") is not the
# same quantity as whole-session IF and must not be read through those bands.
SUBTHRESHOLD_FTP_CEILING = 88
_R_PCT_FTP = re.compile(r"(\d{1,3})\s*(?:-|to)?\s*(\d{1,3})?\s*%\s*(?:of\s+)?ftp")

# -- Tier 4: explicit zone / named intensity --------------------------------
_TIER4 = [
    ("R_TEST", re.compile(r"\bftp test\b|\bramp test\b|\b20 ?min test\b|\bfitness test\b"),
     Intensity.THRESHOLD),
    ("R_VO2", re.compile(
        r"\bvo2\b|\bv02\b|\bz5\b|\bz6\b|\bz7\b|\bzone 5\b|\banaerobic\b"
        r"|\bneuromuscular\b|\bsprints?\b|\bmax effort\b|\battacks?\b"
        r"|\bsupra-? ?threshold\b"), Intensity.VO2),
    ("R_THRESHOLD", re.compile(
        r"\bthreshold\b|\bftp\b|\bz4\b|\bzone 4\b|\blt2\b|\bover-? ?unders?\b"
        r"|\bcritical power\b|\bcp20\b"), Intensity.THRESHOLD),
    ("R_TEMPO", re.compile(r"\btempo\b|\bsweet ?spot\b|\bsst?\b|\bz3\b|\bzone 3\b"),
     Intensity.TEMPO),
    ("R_ENDURANCE", re.compile(
        r"\bendurance\b|\baerobic\b|\bbase\b|\bz2\b|\bzone 2\b|\bsteady\b"
        r"|\blong ride\b|\blsd\b|\bmaf\b|\bfat ?max\b"), Intensity.ENDURANCE),
]

# -- Tier 5: structured cues -------------------------------------------------
_R_IF = re.compile(r"(?:\bif\b|intensity factor)[ :=]*([01]?\.\d+)")
_R_TSS = re.compile(r"(?:planned\s+)?\btss\b[ :=]*(\d+)|\b(\d+)\s*tss\b")
_R_INTERVAL = re.compile(
    r"(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(s\b|sec\b|secs\b|seconds\b|min\b|mins\b|minutes\b|')?"
)

# -- Tier 6: weak tokens -----------------------------------------------------
_R_WEAK_HARD = re.compile(
    r"\bhard\b|\bintervals?\b|\bintensity\b|\bhill repeats?\b|\bbig gear\b")
_R_WEAK_EASY = re.compile(r"\beasy\b|\bchill\b|\bsocial\b")

_R_KEY = re.compile(r"\bkey\b|\bkey workout\b|\bpriority\b")

# -- sport -------------------------------------------------------------------
_SPORT_BIKE = re.compile(
    r"\b(?:bike|biking|rid(?:e|es|ing)|cycl\w*|zwift|turbo|trainer|spin|spins"
    r"|watts?|ftp|crit|criterium|gran ?fondo|sportive|time ?trial|tt)\b")
_SPORT_RUN = re.compile(r"\b(?:run|runs|running|jog\w*|shakeout run|track session)\b")
_SPORT_SWIM = re.compile(r"\b(?:swim|swims|swimming|pool)\b")
_SPORT_STRENGTH = re.compile(r"\b(?:gym|strength|core|yoga|lifting|s&c)\b")

# -- duration ----------------------------------------------------------------
# Order matters: XhYY before hours, or "3h30" matches "3h" and loses the 30.
_DUR_PATTERNS = [
    ("hmm", re.compile(r"\b(\d{1,2}):(\d{2})\b"), lambda m: int(m.group(1)) * 60 + int(m.group(2))),
    ("hXm", re.compile(r"\b(\d{1,2})\s*h\s*(\d{1,2})\b"), lambda m: int(m.group(1)) * 60 + int(m.group(2))),
    ("hours", re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:hrs?|hours?|h)\b"), lambda m: float(m.group(1)) * 60),
    ("minutes", re.compile(r"\b(\d+)\s*(?:min|mins|minutes|')"), lambda m: float(m.group(1))),
]
DURATION_MIN_BOUND = 10
DURATION_MAX_BOUND = 480
# Lines that *label a total* rather than describe a segment. TrainingPeaks
# exports routinely end with "Duration: 1:15" under a warm-up/main-set/cool-down
# block; that field restates the session length, it does not extend it.
_R_TOTAL_FIELD = re.compile(
    r"^\s*[-*•]?\s*(?:total(?:\s+(?:time|duration))?|duration|ride time"
    r"|session time|elapsed(?:\s+time)?)\s*[:=]"
)


def _mask(text: str, pattern: re.Pattern) -> str:
    """Blank out matched spans, preserving length and line structure."""
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


def _mask_subthreshold_ftp(text: str) -> str:
    """Blank out "55% FTP" style references whose top end sits below the
    threshold band, so they cannot fire R_THRESHOLD. Length is preserved for
    the same reason ``_mask`` preserves it."""
    def repl(m):
        top = max(int(g) for g in m.groups() if g)
        return " " * len(m.group(0)) if top < SUBTHRESHOLD_FTP_CEILING else m.group(0)
    return _R_PCT_FTP.sub(repl, text)


def _parse_intervals(text: str) -> List[Tuple[int, float, Optional[str]]]:
    out = []
    for m in _R_INTERVAL.finditer(text):
        unit = (m.group(3) or "").strip() or None
        out.append((int(m.group(1)), float(m.group(2)), unit))
    return out


def _rep_minutes(reps: int, value: float, unit: Optional[str]) -> float:
    if unit and unit.startswith("s"):
        return value / 60.0
    if unit == "'":
        return value
    if unit and unit.startswith("min"):
        return value
    # No unit stated. Small numbers read as minutes — "5x5" is 5-minute reps,
    # which is how every cycling plan writes it.
    return value


def _field_duration(masked_text: str) -> Optional[float]:
    """§1.2 within one field — first pattern that hits wins; max within a
    line, sum across lines (a warm-up line plus a main-set line)."""
    per_line = []
    totals = []
    for line in masked_text.splitlines():
        if not line.strip():
            continue
        for _name, pattern, to_min in _DUR_PATTERNS:
            values = [to_min(m) for m in pattern.finditer(line)]
            if values:
                # A line that *states the total* ("Duration: 1:15") is not
                # another segment to add on: summing it with the warm-up and
                # cool-down lines it summarises double-counts the session and
                # pushes an inflated placeholder to the head unit.
                (totals if _R_TOTAL_FIELD.match(line) else per_line).append(max(values))
                break
    if totals:
        return max(totals)
    if not per_line:
        return None
    return sum(per_line) if len(per_line) > 1 else per_line[0]


def _parse_duration(masked_title: str, masked_description: str) -> Tuple[Optional[int], str]:
    """Title and description are parsed as separate fields and combined with
    ``max``, not ``sum``. "Endurance 3hr Z2" / "Long ride, 3hr steady" is one
    3-hour ride described twice, not a 6-hour day — summing across the
    title/description boundary doubles almost every workout in the feed."""
    values = [v for v in (_field_duration(masked_title), _field_duration(masked_description))
              if v]
    if not values:
        return None, "none"
    total = max(values)
    if not (DURATION_MIN_BOUND <= total <= DURATION_MAX_BOUND):
        return None, "none"
    return int(round(total)), "explicit"


def _parse_tss(text: str) -> Optional[float]:
    m = _R_TSS.search(text)
    if not m:
        return None
    return float(m.group(1) or m.group(2))


def _detect_sport(text: str, intensity: Intensity) -> str:
    """§1.4 — bike tokens win; a non-bike sport is only claimed when no bike
    token is present."""
    if _SPORT_BIKE.search(text):
        return "cycling"
    if _SPORT_RUN.search(text):
        return "running"
    if _SPORT_SWIM.search(text):
        return "swimming"
    if _SPORT_STRENGTH.search(text):
        return "strength"
    if intensity == Intensity.REST:
        return "rest"
    return "unknown"


def _evidence(text: str, pattern: re.Pattern) -> Optional[str]:
    m = pattern.search(text)
    return m.group(0).strip() if m else None


def _downgrade_one_notch(conf: Confidence) -> Confidence:
    return Confidence(max(Confidence.LOW, int(conf) - 1)) if conf > Confidence.LOW else conf


def _if_to_intensity(value: float) -> Intensity:
    if value < 0.65:
        return Intensity.RECOVERY
    if value < 0.75:
        return Intensity.ENDURANCE
    if value < 0.85:
        return Intensity.TEMPO
    if value <= 0.95:
        return Intensity.THRESHOLD
    return Intensity.VO2


def _interval_to_intensity(reps: int, rep_min: float) -> Intensity:
    if rep_min <= 2:
        return Intensity.VO2
    if rep_min <= 8:
        return Intensity.VO2 if reps >= 4 else Intensity.THRESHOLD
    if rep_min <= 20:
        return Intensity.THRESHOLD
    return Intensity.TEMPO


def unknown_question(classification: Classification, title: str = "") -> str:
    label = title.strip() or "that session"
    return (f"I couldn't tell how hard '{label}' is — is that a hard session "
            f"or an easy one?")


def classify(title: str, description: str = "", *, all_day: bool = False,
             learned_overrides: Optional[dict] = None) -> Classification:
    """The typed classifier. ``classify_workout`` is the dict-returning twin."""
    title_text = _normalize(title or "")
    description_text = _normalize(description or "")
    text = f"{title_text}\n{description_text}"
    rules: List[str] = []
    evidence: List[str] = []
    warnings: List[str] = []

    # --- Tier 0: learned overrides -----------------------------------------
    if learned_overrides:
        entry = learned_overrides.get(override_key(title))
        if entry:
            intensity = Intensity[str(entry["intensity"]).upper()]
            duration = entry.get("duration_min")
            cls = Classification(
                intensity=intensity, confidence=Confidence.HIGH,
                min_possible=intensity, max_possible=intensity,
                duration_min=duration,
                duration_source="explicit" if duration else "none",
                sport=_detect_sport(text, intensity),
                matched_rules=["R_USER_OVERRIDE"], evidence=[override_key(title)],
                user_override=True,
            )
            return _estimate_load(cls)

    # --- always-parsed fields ----------------------------------------------
    intervals = _parse_intervals(text)
    # see module docstring, note 2 — interval spans can't be read as durations
    duration_min, duration_source = _parse_duration(
        _mask(title_text, _R_INTERVAL), _mask(description_text, _R_INTERVAL))
    tss = _parse_tss(text)
    is_key = bool(_R_KEY.search(text))
    if intervals:
        rules.append("R_INTERVAL_NXM")
        evidence.append(_evidence(text, _R_INTERVAL) or "")

    intensity: Optional[Intensity] = None
    confidence = Confidence.NONE
    min_possible = Intensity.RECOVERY
    max_possible = Intensity.VO2
    is_openers = False
    race_negated = False

    # --- Tier 1: RACE -------------------------------------------------------
    if _R_RACE_NEGATE.search(text):
        race_negated = True
        rules.append("R_RACE_NEGATE")
        evidence.append(_evidence(text, _R_RACE_NEGATE) or "")
    else:
        if _R_RACE.search(text):
            intensity = Intensity.RACE
            confidence = Confidence.HIGH
            rules.append("R_RACE")
            evidence.append(_evidence(text, _R_RACE) or "")
        elif all_day and _R_RACE_EVENT.search(text):
            intensity = Intensity.RACE
            confidence = Confidence.MED
            rules.append("R_RACE")
            evidence.append(_evidence(text, _R_RACE_EVENT) or "")

    # --- Tier 2: REST -------------------------------------------------------
    # REST short-circuits rather than joining the §1.3 fold below: "Recovery
    # day" matches both REST and RECOVERY, and max() of those two families
    # would turn a day off into a spin.
    if intensity is None and _R_REST.search(text):
        stripped = _mask(text, _R_REST_PHRASES)
        has_activity = bool(_ACTIVITY_TOKEN.search(stripped))
        has_duration = bool(duration_min)
        if not has_activity and not has_duration:
            intensity = Intensity.REST
            confidence = Confidence.HIGH
            min_possible = max_possible = Intensity.REST
            rules.append("R_REST")
            evidence.append(_evidence(text, _R_REST) or "")
        else:
            rules.append("R_REST_GUARD")

    # --- Tiers 3, 3b and 4 resolve together under §1.3 ---------------------
    # The spec orders RECOVERY above the named-zone tier, but they are not
    # mutually exclusive in real feed text, and §1.3 already says what to do
    # when several families match: the dominant stressor sets the recovery
    # cost. Folding them means an explicit "threshold" can't be outranked by
    # an incidental "recovery" earlier in the ladder.
    if intensity is None:
        matched: List[Tuple[str, Intensity, str]] = []
        recovery_text = _mask(text, _R_INTERVAL_RECOVERY)
        hit = _evidence(recovery_text, _R_RECOVERY)
        if hit:
            matched.append(("R_RECOVERY", Intensity.RECOVERY, hit))
        hit = _evidence(text, _R_OPENERS)
        if hit:
            matched.append(("R_OPENERS", Intensity.RECOVERY, hit))
        tier4_text = _mask_subthreshold_ftp(text)
        for rule_id, pattern, value in _TIER4:
            hit = _evidence(tier4_text, pattern)
            if hit:
                matched.append((rule_id, value, hit))
        if matched:
            families = {v for _, v, _ in matched}
            intensity = max(families)
            confidence = Confidence.HIGH
            for rule_id, _v, hit in matched:
                rules.append(rule_id)
                evidence.append(hit)
            if any(r == "R_TEST" for r, _, _ in matched):
                is_key = True  # tests are hard AND date-sensitive (§2.H1)
            if any(r == "R_OPENERS" for r, _, _ in matched) \
                    and intensity == Intensity.RECOVERY:
                is_openers = True
            if len(families) > 1:
                # §1.3 — the dominant stressor sets the recovery cost, but we
                # are less sure than if only one thing had matched.
                confidence = _downgrade_one_notch(confidence)
                rules.append("R_CONFLICT")
                min_possible, max_possible = min(families), max(families)
            else:
                min_possible = max_possible = intensity
            if intensity == Intensity.RECOVERY and duration_min and duration_min > 90:
                confidence = min(confidence, Confidence.MED)
                warnings.append(f"'{title}' is labelled recovery but is "
                                f"{duration_min} min — worth a check.")

    # --- Tier 5: structured cues -------------------------------------------
    if intensity is None:
        m = _R_IF.search(text)
        if m:
            intensity = _if_to_intensity(float(m.group(1)))
            confidence = Confidence.HIGH
            min_possible = max_possible = intensity
            rules.append("R_IF")
            evidence.append(m.group(0).strip())

    if intensity is None and intervals:
        reps, value, unit = intervals[0]
        intensity = _interval_to_intensity(reps, _rep_minutes(reps, value, unit))
        confidence = Confidence.MED
        min_possible = max_possible = intensity
        rules.append("R_INTERVAL_NXM")

    if intensity is None and duration_min:
        rules.append("R_DURATION_ONLY")
        if duration_min >= 90:
            intensity = Intensity.ENDURANCE
            confidence = Confidence.MED
            min_possible = max_possible = intensity
        elif duration_min >= 45:
            # A 60-minute ride is equally likely Z2 or VO2. Refuse to guess.
            intensity = Intensity.UNKNOWN
            confidence = Confidence.NONE
        else:
            intensity = Intensity.RECOVERY
            confidence = Confidence.LOW
            min_possible, max_possible = Intensity.RECOVERY, Intensity.ENDURANCE

    # --- Tier 6: weak tokens -----------------------------------------------
    if intensity is None and _R_WEAK_HARD.search(text):
        intensity = Intensity.THRESHOLD
        confidence = Confidence.LOW
        min_possible, max_possible = Intensity.TEMPO, Intensity.VO2
        rules.append("R_WEAK_HARD")
        evidence.append(_evidence(text, _R_WEAK_HARD) or "")

    if intensity is None and _R_WEAK_EASY.search(text):
        intensity = Intensity.ENDURANCE
        confidence = Confidence.LOW
        min_possible, max_possible = Intensity.RECOVERY, Intensity.ENDURANCE
        rules.append("R_WEAK_EASY")
        evidence.append(_evidence(text, _R_WEAK_EASY) or "")

    # --- Tier 7: UNKNOWN ----------------------------------------------------
    if intensity is None:
        intensity = Intensity.UNKNOWN
        confidence = Confidence.NONE
        rules.append("R_UNKNOWN")

    # §1.1 Tier 1 — a negated race nudges toward THRESHOLD, but only when we
    # had nothing better. It never overrides a stated zone.
    if race_negated and confidence <= Confidence.LOW:
        if intensity == Intensity.UNKNOWN or intensity < Intensity.THRESHOLD:
            intensity = Intensity.THRESHOLD
            confidence = Confidence.LOW
            min_possible, max_possible = Intensity.TEMPO, Intensity.VO2
            rules.append("R_RACE_NEGATE_NUDGE")

    if intensity == Intensity.UNKNOWN:
        # §1.5 — a bound, not a point estimate.
        min_possible, max_possible = Intensity.RECOVERY, Intensity.VO2
    elif "R_CONFLICT" not in rules and min_possible == Intensity.RECOVERY \
            and max_possible == Intensity.VO2:
        min_possible = max_possible = intensity

    cls = Classification(
        intensity=intensity,
        confidence=confidence,
        min_possible=min_possible,
        max_possible=max_possible,
        duration_min=duration_min,
        duration_source=duration_source,
        tss=tss,
        sport=_detect_sport(text, intensity),
        is_openers=is_openers,
        is_key=is_key,
        matched_rules=list(dict.fromkeys(rules)),
        evidence=[e for e in evidence if e],
        warnings=warnings,
    )
    return _estimate_load(cls)


def _estimate_load(cls: Classification) -> Classification:
    """§1.6 — TSS if stated, else duration x IF-proxy², else class default,
    else None. ``None`` is a real answer: it means "I can't size this", and
    §5.3 forces every load claim built on it to be hedged."""
    if cls.tss is not None:
        cls.load = float(cls.tss)
        cls.load_confidence = Confidence.HIGH
    elif cls.duration_source == "explicit" and cls.intensity != Intensity.UNKNOWN:
        proxy = IF_PROXY.get(cls.intensity, 0.0)
        cls.load = (cls.duration_min / 60.0) * proxy ** 2 * 100
        cls.load_confidence = Confidence.MED
    elif cls.intensity not in (Intensity.UNKNOWN,) and cls.intensity in CLASS_DEFAULT_MIN:
        default = CLASS_DEFAULT_MIN[cls.intensity]
        proxy = IF_PROXY.get(cls.intensity, 0.0)
        cls.load = (default / 60.0) * proxy ** 2 * 100
        cls.load_confidence = Confidence.LOW
        if cls.duration_source == "none":
            cls.duration_source = "class_default"
    elif cls.intensity == Intensity.REST:
        cls.load = 0.0
        cls.load_confidence = Confidence.HIGH
    else:
        cls.load = None
        cls.load_confidence = Confidence.NONE
    return cls


def classify_workout(title: str, description: str = "", *, all_day: bool = False,
                     learned_overrides: Optional[dict] = None) -> dict:
    """Classify one free-text calendar event.

    Returns a plain dict (sport, intensity, confidence, duration, TSS, load,
    the rules that fired, the substrings that matched, and — when the text
    can't be read — the question to put to the athlete).
    """
    cls = classify(title, description, all_day=all_day, learned_overrides=learned_overrides)
    out = cls.to_dict()
    out["title"] = title
    if cls.intensity == Intensity.UNKNOWN:
        out["question"] = unknown_question(cls, title)
    return out


# ---------------------------------------------------------------------------
# INPUT NORMALISATION
# ---------------------------------------------------------------------------


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    # The ICS feed hands back "2026-07-28" for all-day events and
    # "2026-07-28T06:00:00+00:00" for timed ones. Take the calendar date.
    return date.fromisoformat(text[:10])


def _to_planned(workouts: Iterable, learned_overrides=None) -> List[PlannedWorkout]:
    out: List[PlannedWorkout] = []
    for i, item in enumerate(workouts or []):
        if isinstance(item, PlannedWorkout):
            if item.classification is None:
                item.classification = classify(
                    item.title, item.description, learned_overrides=learned_overrides)
            out.append(item)
            continue
        day = _as_date(item.get("date"))
        if day is None:
            continue
        uid = str(item.get("uid") or f"local-{i}-{day.isoformat()}")
        title = str(item.get("title") or "")
        description = str(item.get("description") or "")
        all_day = bool(item.get("all_day", "T" not in str(item.get("date") or "")))
        cls = classify(title, description, all_day=all_day,
                       learned_overrides=learned_overrides)
        out.append(PlannedWorkout(uid=uid, date=day, title=title,
                                  description=description, classification=cls,
                                  origin=str(item.get("origin") or "trainingpeaks_feed")))
    out.sort(key=lambda w: (w.date, w.uid))
    return out


def _to_unavailable(unavailable_days) -> Dict[date, Optional[str]]:
    out: Dict[date, Optional[str]] = {}
    if not unavailable_days:
        return out
    if isinstance(unavailable_days, dict):
        for key, value in unavailable_days.items():
            day = _as_date(key)
            if day:
                out[day] = value if isinstance(value, str) else (
                    (value or {}).get("reason") if isinstance(value, dict) else None)
        return out
    for entry in unavailable_days:
        if isinstance(entry, dict):
            day = _as_date(entry.get("date"))
            if day:
                out[day] = entry.get("reason")
        else:
            day = _as_date(entry)
            if day:
                out[day] = None
    return out


def _to_races(race_dates, planned: Sequence[PlannedWorkout]) -> List[RaceRef]:
    races: Dict[date, RaceRef] = {}
    for entry in race_dates or []:
        if isinstance(entry, RaceRef):
            races[entry.date] = entry
            continue
        if isinstance(entry, dict):
            day = _as_date(entry.get("date"))
            if not day:
                continue
            races[day] = RaceRef(
                date=day, title=str(entry.get("title") or "your race"),
                priority=str(entry.get("priority") or "B"),
                duration_min=entry.get("duration_min"))
        else:
            day = _as_date(entry)
            if day:
                races[day] = RaceRef(date=day)
    # Races the classifier found in the plan itself.
    for w in planned:
        if w.intensity == Intensity.RACE and w.date not in races:
            priority = "B"
            m = _R_RACE_PRIORITY.search(_normalize(f"{w.title} {w.description}"))
            if m:
                priority = (m.group(1) or m.group(2) or "B").upper()
            races[w.date] = RaceRef(date=w.date, title=w.title or "your race",
                                    priority=priority,
                                    duration_min=w.classification.duration_min)
    return sorted(races.values(), key=lambda r: r.date)


def _parse_readiness(garmin_signal) -> Tuple[Optional[int], Optional[str], Optional[datetime]]:
    if not garmin_signal:
        return None, None, None
    readiness = garmin_signal.get("readiness")
    stamp = garmin_signal.get("timestamp") or garmin_signal.get("fetched_at")
    if isinstance(readiness, dict):
        score = readiness.get("score")
        level = readiness.get("level")
        stamp = readiness.get("timestamp") or stamp
    elif isinstance(readiness, (int, float)):
        score, level = int(readiness), None
    else:
        score, level = None, None
    when = None
    if stamp:
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if when.tzinfo is not None:
                when = when.replace(tzinfo=None)
        except ValueError:
            when = None
    return (int(score) if score is not None else None, level, when)


# ---------------------------------------------------------------------------
# §2 CONTEXT
# ---------------------------------------------------------------------------

WeekMap = Dict[date, List[PlannedWorkout]]


class _Context:
    """Everything the constraint and scoring functions need, computed once."""

    def __init__(self, planned, unavailable, races, week_start, today, now, config):
        self.config = config
        self.today = today
        self.now = now
        self.week_start = week_start
        self.week_days = [week_start + timedelta(days=i) for i in range(7)]
        # §2.0 — 21-day window so races and post-race windows just outside the
        # week are visible; candidate slots stay inside the week.
        self.window_days = [week_start - timedelta(days=7) + timedelta(days=i)
                            for i in range(21)]
        # H1 and spacing look one day either side of the week.
        self.flank_days = [week_start - timedelta(days=1) + timedelta(days=i)
                           for i in range(9)]
        self.candidate_days = list(self.week_days)
        if config.allow_spill_next_week:
            self.candidate_days += [week_start + timedelta(days=7 + i) for i in range(7)]
        self.unavailable = unavailable
        self.races = races
        self.warnings: List[str] = []

        self.baseline: WeekMap = {d: [] for d in self.window_days}
        for w in planned:
            self.baseline.setdefault(w.date, []).append(w)
        # uid -> the date the coach wrote, for the ordering penalty.
        self.plan_dates: Dict[str, date] = {w.uid: w.date for w in planned}

        self.planned_rest_count = self._rest_days(self.baseline)
        self.planned_max_consecutive_hard = max(
            1, self._max_consecutive_hard(self.baseline))
        self.planned_min_hard_gap = self._min_hard_gap(self.baseline)
        self.baseline_spacing = self._spacing_penalty(self.baseline)
        self.baseline_imbalance = self._load_imbalance(self.baseline) or 0.0
        self.max_planned_day_load = self._max_day_load(self.baseline)
        longest = max((w.duration_min for d in self.week_days
                       for w in self.baseline.get(d, []) if w.is_session), default=0)
        self.longest_planned_session_min = longest
        self.max_day_duration_min = max(config.max_day_duration_min, int(1.5 * longest))

    # -- day-level helpers ---------------------------------------------------
    def sessions(self, week: WeekMap, day: date) -> List[PlannedWorkout]:
        return [w for w in week.get(day, []) if w.is_session]

    def day_is_hard(self, week: WeekMap, day: date) -> bool:
        return any(w.is_hard for w in week.get(day, []))

    def is_past(self, day: date) -> bool:
        if day < self.today:
            return True
        return day == self.today and self.now.hour >= self.config.today_cutoff_hour

    def race_on(self, day: date) -> Optional[RaceRef]:
        for race in self.races:
            if race.date == day:
                return race
        return None

    def taper_race(self, day: date) -> Optional[RaceRef]:
        for race in self.races:
            start, end = race.taper_window
            if start <= day <= end:
                return race
        return None

    def post_race(self, day: date) -> Optional[RaceRef]:
        for race in self.races:
            if race.date < day <= race.date + timedelta(days=race.post_race_days):
                return race
        return None

    def days_to_race(self, day: date) -> Optional[Tuple[int, RaceRef]]:
        upcoming = [r for r in self.races if r.date >= day]
        if not upcoming:
            return None
        race = min(upcoming, key=lambda r: r.date)
        return (race.date - day).days, race

    # -- week-shape metrics --------------------------------------------------
    def _rest_days(self, week: WeekMap) -> int:
        return sum(1 for d in self.week_days if not self.sessions(week, d))

    def _hard_days(self, week: WeekMap, days: Sequence[date]) -> List[date]:
        return [d for d in days if self.day_is_hard(week, d)]

    def _max_consecutive_hard(self, week: WeekMap) -> int:
        hard = set(self._hard_days(week, self.flank_days))
        run = best = 0
        for d in self.flank_days:
            run = run + 1 if d in hard else 0
            best = max(best, run)
        return best

    def _min_hard_gap(self, week: WeekMap) -> int:
        hard = self._hard_days(week, self.flank_days)
        gaps = [(b - a).days for a, b in zip(hard, hard[1:])]
        return min(gaps) if gaps else 99

    def _spacing_penalty(self, week: WeekMap) -> float:
        hard = self._hard_days(week, self.flank_days)
        penalty = 0.0
        for a, b in zip(hard, hard[1:]):
            gap = (b - a).days
            if gap == 1:
                penalty += 40
            elif gap == 2:
                penalty += 12
        return penalty

    def _load_imbalance(self, week: WeekMap) -> Optional[float]:
        loads = []
        for d in self.week_days:
            day_loads = [w.load for w in self.sessions(week, d)]
            if any(l is None for l in day_loads):
                return None
            loads.append(sum(day_loads))
        return statistics.pstdev(loads) if len(loads) > 1 else 0.0

    def _max_day_load(self, week: WeekMap) -> Optional[float]:
        best = 0.0
        for d in self.week_days:
            day_loads = [w.load for w in self.sessions(week, d)]
            if any(l is None for l in day_loads):
                continue
            best = max(best, sum(day_loads))
        return best or None

    def day_duration(self, week: WeekMap, day: date) -> int:
        return sum(w.duration_min for w in self.sessions(week, day))

    def day_load(self, week: WeekMap, day: date) -> Optional[float]:
        loads = [w.load for w in self.sessions(week, day)]
        if any(l is None for l in loads):
            return None
        return sum(loads)

    def summarise(self, week: WeekMap) -> WeekSummary:
        sessions = [w for d in self.week_days for w in self.sessions(week, d)]
        known = [w.load for w in sessions if w.load is not None]
        fraction = (len(known) / len(sessions)) if sessions else 1.0
        return WeekSummary(
            total_load=sum(known) if known else None,
            load_known_fraction=fraction,
            rest_days=self._rest_days(week),
            hard_days=len(self._hard_days(week, self.week_days)),
            longest_ride_min=max((w.duration_min for w in sessions), default=0),
        )


def _copy_week(week: WeekMap) -> WeekMap:
    return {d: list(items) for d, items in week.items()}


def _place(week: WeekMap, workout: PlannedWorkout, day: date) -> WeekMap:
    out = _copy_week(week)
    out.setdefault(day, []).append(replace(workout, date=day))
    return out


def _remove(week: WeekMap, uid: str) -> WeekMap:
    return {d: [w for w in items if w.uid != uid] for d, items in week.items()}


# ---------------------------------------------------------------------------
# §2.1 HARD CONSTRAINTS
# ---------------------------------------------------------------------------


@dataclass
class _Verdict:
    legal: bool
    rule_id: Optional[str] = None
    reason: Optional[str] = None


def _day_name(day: date) -> str:
    return day.strftime("%A")


def check_hard_constraints(ctx: _Context, week: WeekMap, workout: PlannedWorkout,
                           day: date, *, intensity: Optional[Intensity] = None) -> _Verdict:
    """H1-H10. A candidate failing any of these is rejected, never scored."""
    cls = workout.classification
    intensity = intensity if intensity is not None else cls.max_possible
    is_hard = intensity in HARD_CLASSES
    cfg = ctx.config

    # H10 — openers are pinned to the day before the race.
    if cls.is_openers:
        pinned = [r.date - timedelta(days=1) for r in ctx.races]
        if pinned and day not in pinned:
            return _Verdict(False, "H10",
                            "openers only do their job the day before the race.")

    # H6 — the athlete told us they can't train.
    if day in ctx.unavailable:
        reason = ctx.unavailable.get(day)
        suffix = f" ({reason})" if reason else ""
        return _Verdict(False, "H6", f"you've marked {_day_name(day)} unavailable{suffix}.")

    # H7 — the past.
    if ctx.is_past(day):
        return _Verdict(False, "H7", f"{_day_name(day)} has already gone.")

    # H3 — race isolation.
    race_here = ctx.race_on(day)
    if race_here:
        return _Verdict(False, "H3", f"{_day_name(day)} is {race_here.title} itself.")
    for race in ctx.races:
        if (workout.date < race.date < day) or (day < race.date < workout.date):
            return _Verdict(False, "H3",
                            f"{_day_name(day)} is on the other side of {race.title} "
                            f"on {_day_name(race.date)}.")

    # H4 — protected taper window (no additions).
    taper = ctx.taper_race(day)
    if taper:
        return _Verdict(False, "H4",
                        f"{_day_name(day)} is inside your taper for {taper.title}.")

    # §3.3 — the final 48 hours take only recovery and openers.
    to_race = ctx.days_to_race(day)
    if to_race and to_race[0] <= cfg.final_protect_days:
        if not (cls.is_openers or intensity in (Intensity.REST, Intensity.RECOVERY)):
            return _Verdict(False, "T3_FINAL48",
                            f"{_day_name(day)} is inside the last 48 hours before "
                            f"{to_race[1].title}.")

    # H5 — post-race recovery window (hard placements only).
    post = ctx.post_race(day)
    if post and is_hard:
        return _Verdict(False, "H5",
                        f"{_day_name(day)} is still recovery time after {post.title}.")

    # H8 — day capacity.
    existing = ctx.sessions(week, day)
    if len(existing) + 1 > cfg.max_sessions_per_day:
        return _Verdict(False, "H8",
                        f"{_day_name(day)} already has {existing[0].title} on it.")
    projected = ctx.day_duration(week, day) + workout.duration_min
    if projected > ctx.max_day_duration_min:
        return _Verdict(False, "H8",
                        f"that would make {_day_name(day)} {projected} minutes long.")

    # H1 — no back-to-back hard days (escape valve: never harder-packed than
    # the coach's own week).
    if is_hard:
        after = _place(week, workout, day)
        if intensity != cls.max_possible:  # hypothetical downgrade
            after[day] = [w if w.uid != workout.uid else
                          replace(w, classification=replace(
                              w.classification, intensity=intensity,
                              min_possible=intensity, max_possible=intensity))
                          for w in after[day]]
        run = ctx._max_consecutive_hard(after)
        if run > ctx.planned_max_consecutive_hard:
            neighbours = [d for d in (day - timedelta(days=1), day + timedelta(days=1))
                          if ctx.day_is_hard(week, d)]
            if neighbours:
                return _Verdict(False, "H1",
                                f"it would sit next to {_day_name(neighbours[0])}'s hard "
                                f"session — two hard days back to back.")
            return _Verdict(False, "H1",
                            "that would pack more hard days together than your "
                            "plan already has.")

    # H2 — rest-day floor.
    after = _place(week, workout, day)
    if ctx._rest_days(after) < ctx.planned_rest_count:
        return _Verdict(False, "H2",
                        f"putting it on {_day_name(day)} would leave you a rest day "
                        f"short this week.")

    # H9 — day load ceiling.
    load_after = ctx.day_load(after, day)
    if load_after is not None and ctx.max_planned_day_load:
        ceiling = cfg.day_load_ceiling_mult * ctx.max_planned_day_load
        if load_after > ceiling:
            return _Verdict(False, "H9",
                            f"that would make {_day_name(day)} much bigger than any "
                            f"day you had planned.")
    elif load_after is None:
        ctx.warnings.append(f"Couldn't size {_day_name(day)} — some sessions have no "
                            f"duration or TSS in the feed.")

    return _Verdict(True)


# ---------------------------------------------------------------------------
# §2.2 SCORING
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    date: date
    legal: bool
    score: float = 0.0
    breakdown: Dict[str, float] = field(default_factory=dict)
    rule_id: Optional[str] = None
    reason: Optional[str] = None


def score_candidate(ctx: _Context, week: WeekMap, workout: PlannedWorkout,
                    day: date) -> Tuple[float, Dict[str, float]]:
    """Lower is better. Week-shape terms (spacing, load balance) are deltas
    against the coach's own week — see module docstring, note 1."""
    cfg = ctx.config
    after = _place(week, workout, day)
    cls = workout.classification

    dist = cfg.w_dist * abs((day - workout.date).days)
    spacing = cfg.w_spacing * max(0.0, ctx._spacing_penalty(after) - ctx.baseline_spacing)

    weekend = 0.0
    if (workout.duration_min >= LONG_RIDE_MIN and day.weekday() < 5
            and workout.date.weekday() >= 5):
        weekend = cfg.w_weekend * 15

    # Coaches sequence sessions deliberately: penalise flipping this workout
    # past a sibling it originally sat before (or after).
    inversions = 0
    for other in [w for d in ctx.week_days for w in ctx.sessions(week, d)]:
        if other.uid == workout.uid:
            continue
        base_date = ctx.plan_dates.get(other.uid, other.date)
        if (workout.date < base_date and day > other.date) or \
           (workout.date > base_date and day < other.date):
            inversions += 1
    ordering = cfg.w_order * 5 * inversions

    imbalance_after = ctx._load_imbalance(after)
    loadbal = 0.0
    if imbalance_after is not None:
        loadbal = cfg.w_loadbal * (imbalance_after - ctx.baseline_imbalance)

    lowconf = cfg.w_lowconf * (3 - int(cls.confidence))

    breakdown = {"distance": dist, "spacing": spacing, "weekend": weekend,
                 "ordering": ordering, "load_balance": loadbal, "low_confidence": lowconf}
    return sum(breakdown.values()), breakdown


def evaluate_candidates(ctx: _Context, week: WeekMap, workout: PlannedWorkout,
                        *, intensity: Optional[Intensity] = None) -> List[_Candidate]:
    """§2.0 — every day of the target week except the one it's on now.

    Spilling into next week distorts two weeks instead of one, so it stays
    behind ``ALLOW_SPILL_NEXT_WEEK`` and is off by default.
    """
    out = []
    for day in ctx.candidate_days:
        if day == workout.date:
            continue
        verdict = check_hard_constraints(ctx, week, workout, day, intensity=intensity)
        if not verdict.legal:
            out.append(_Candidate(day, False, rule_id=verdict.rule_id, reason=verdict.reason))
            continue
        score, breakdown = score_candidate(ctx, week, workout, day)
        out.append(_Candidate(day, True, score=score, breakdown=breakdown))
    return out


def _rank(ctx: _Context, week: WeekMap, candidates: Sequence[_Candidate]) -> List[_Candidate]:
    """Tie-breakers, in order: lower score; later date (more recovery before a
    hard session, more notice for the athlete); fewer existing sessions;
    earliest weekday index for determinism."""
    legal = [c for c in candidates if c.legal]
    return sorted(legal, key=lambda c: (
        round(c.score, 6), -c.date.toordinal(), len(ctx.sessions(week, c.date)),
        c.date.weekday()))


def _top_alternatives(candidates: Sequence[_Candidate], chosen: Optional[date],
                      limit: Optional[int] = 3) -> List[RejectedOption]:
    out = []
    for c in candidates:
        if c.date == chosen:
            continue
        if c.legal:
            continue
        out.append(RejectedOption(c.date, c.reason or "not a good fit.", c.rule_id or "SOFT"))
    out.sort(key=lambda r: r.date)
    return out if limit is None else out[:limit]


# ---------------------------------------------------------------------------
# §5 REASON STRINGS
# ---------------------------------------------------------------------------


def _unavailable_phrase(reason: Optional[str], day: date) -> str:
    name = _day_name(day)
    reason = (reason or "").strip().lower()
    if not reason:
        return f"not free on {name}"
    if reason.startswith("work"):
        return f"working {name}"
    if reason.startswith("travel"):
        return f"travelling {name}"
    if reason.startswith("family"):
        return f"tied up with family on {name}"
    return f"{reason} on {name}"


def _phrase(cls: Classification) -> str:
    return INTENSITY_PHRASE.get(cls.intensity, "session")


def _hedge(reason: str, cls: Classification, title: str) -> str:
    """§5.3 — mandatory hedging when we aren't sure what we're looking at."""
    if cls.confidence <= Confidence.LOW and cls.intensity != Intensity.UNKNOWN:
        return (f"I'm not certain how hard '{title}' is, but treating it as "
                f"{cls.intensity.name.lower()}: {reason[0].lower()}{reason[1:]}")
    if cls.intensity == Intensity.UNKNOWN:
        return (f"I can't tell how hard '{title}' is, so I've assumed the worst case: "
                f"{reason[0].lower()}{reason[1:]}")
    return reason


# ---------------------------------------------------------------------------
# §2.3 / §2.4 DISPLACEMENT
# ---------------------------------------------------------------------------


def _displacement_order(workouts: Sequence[PlannedWorkout]) -> List[PlannedWorkout]:
    """§2.3 — key first, then hardest, then biggest, then earliest, so the
    session that matters gets first pick of the week."""
    return sorted(workouts, key=lambda w: (
        not w.classification.is_key,
        -int(w.classification.max_possible),
        -(w.load or 0.0),
        w.date,
    ))


def _blocked_by_unknown(ctx: _Context, week: WeekMap, workout: PlannedWorkout,
                        chosen: Optional[date]) -> bool:
    """§1.5(4) — does the answer to "how hard is this?" change the placement?"""
    cls = workout.classification
    if cls.intensity != Intensity.UNKNOWN:
        return False
    optimistic = evaluate_candidates(ctx, week, workout, intensity=cls.min_possible)
    pessimistic = {c.date: c.legal for c in evaluate_candidates(ctx, week, workout)}
    for c in optimistic:
        if c.legal and not pessimistic.get(c.date, False):
            return True
    return chosen is None


def _resolve_one(ctx: _Context, week: WeekMap, workout: PlannedWorkout,
                 trigger_reason: Optional[str],
                 load_known_fraction: float) -> Tuple[Proposal, WeekMap]:
    """The §2.4 fallback ladder for a single displaced workout. Returns the
    proposal and the week with it committed (unchanged for DROP/NO_PROPOSAL)."""
    cls = workout.classification
    title = workout.title or "this session"
    from_day = _day_name(workout.date)
    unavail = _unavailable_phrase(trigger_reason, workout.date)
    candidates = evaluate_candidates(ctx, week, workout)
    ranked = _rank(ctx, week, candidates)
    question = unknown_question(cls, title) if cls.intensity == Intensity.UNKNOWN else None

    def finish(proposal: Proposal, new_week: WeekMap) -> Tuple[Proposal, WeekMap]:
        proposal.question = proposal.question or question
        if question:
            proposal.blocked_pending_input = _blocked_by_unknown(
                ctx, week, workout, proposal.proposed_date)
            if proposal.blocked_pending_input:
                proposal.reason = f"{proposal.reason} {question}"
        if load_known_fraction < 1.0 and "roughly" not in proposal.reason:
            missing = proposal.reason_details
            missing.append("I couldn't size every session this week, so any load "
                           "figures here are rough.")
        return proposal, new_week

    # --- §3.2 T2: a workout already inside a taper may not move within it ---
    taper = ctx.taper_race(workout.date)
    if taper and not cls.is_openers:
        reason = (f"Leave it. It's race week for {taper.title} and moving intensity "
                  f"around a taper is more likely to hurt than help — you're "
                  f"{unavail}, so I'd just skip it.")
        proposal = Proposal(
            workout_uid=workout.uid, original_date=workout.date, proposed_date=None,
            action="keep", reason=_hedge(reason, cls, title), confidence=cls.confidence,
            kind="KEEP", workout=workout,
            reason_details=[
                f"{from_day} sits inside the {taper.taper_days}-day taper for {taper.title}.",
                "Taper intensity placement is deliberate — relocating it inside the "
                "window silently changes the taper.",
            ],
            tradeoff="You lose the session, but the taper stays as your coach built it.",
            rule_ids=["T2", "H4"],
            alternatives=_top_alternatives(candidates, None, limit=None))
        return finish(proposal, week)

    # --- H10 / §3.4: openers ----------------------------------------------
    # Only when there is actually a race to open for. "Openers" with no race
    # on the calendar is just a 20-minute spin, and dropping it because of its
    # name would be the engine enforcing a rule that doesn't apply.
    if cls.is_openers and ctx.races:
        reason = (f"You're {unavail}, so I'd skip the openers rather than move them — "
                  f"doing them two days out doesn't do the same job.")
        proposal = Proposal(
            workout_uid=workout.uid, original_date=workout.date, proposed_date=None,
            action="drop", reason=_hedge(reason, cls, title), confidence=cls.confidence,
            kind="DROP", workout=workout,
            operations=[Operation(op="unschedule", workout_uid=workout.uid,
                                  from_date=workout.date,
                                  inverse=Operation(op="schedule", workout_uid=workout.uid,
                                                    to_date=workout.date))],
            reason_details=["Openers are pinned to the day before the race (H10)."],
            tradeoff="Openers are optional; arriving fresh is not.",
            rule_ids=["H10", "R_OPENERS"],
            alternatives=_top_alternatives(candidates, None, limit=None))
        return finish(proposal, week)

    # --- rung 1: MOVE ------------------------------------------------------
    if ranked and ranked[0].score <= ctx.config.min_acceptable_score:
        best = ranked[0]
        to_day = _day_name(best.date)
        blocker = next((c for c in candidates if c.rule_id == "H1"), None)
        tail = ("that doesn't put two hard sessions back to back"
                if blocker else "that keeps the rest of your week intact")
        reason = (f"Moved your {_phrase(cls)} from {from_day} to {to_day} because "
                  f"you're {unavail} — {to_day} was the closest day {tail}.")
        details = [f"{from_day} is unavailable" + (f" ({trigger_reason})." if trigger_reason else ".")]
        for alt in _top_alternatives(candidates, best.date):
            details.append(f"Not {_day_name(alt.date)}: {alt.reason}")
        proposal = Proposal(
            workout_uid=workout.uid, original_date=workout.date, proposed_date=best.date,
            action="move", reason=_hedge(reason, cls, title), confidence=cls.confidence,
            kind="MOVE", workout=workout, score=best.score,
            operations=[Operation(
                op="reschedule", workout_uid=workout.uid, from_date=workout.date,
                to_date=best.date,
                inverse=Operation(op="reschedule", workout_uid=workout.uid,
                                  from_date=best.date, to_date=workout.date))],
            reason_details=details, rule_ids=["H1", "H2", "H6"],
            alternatives=_top_alternatives(candidates, best.date))
        return finish(proposal, _place(week, workout, best.date))

    # --- rung 2: SWAP (3-way shuffle) --------------------------------------
    if cls.is_hard:
        swap = _try_swap(ctx, week, workout, candidates)
        if swap:
            target_day, victim, victim_day = swap
            reason = (f"Swapped things around: {_day_name(target_day)}'s "
                      f"{_phrase(victim.classification)} moves to {_day_name(victim_day)} "
                      f"so your {_phrase(cls)} gets a fresh day — you're {unavail}.")
            ops = [
                Operation(op="reschedule", workout_uid=victim.uid, from_date=target_day,
                          to_date=victim_day,
                          inverse=Operation(op="reschedule", workout_uid=victim.uid,
                                            from_date=victim_day, to_date=target_day)),
                Operation(op="reschedule", workout_uid=workout.uid, from_date=workout.date,
                          to_date=target_day,
                          inverse=Operation(op="reschedule", workout_uid=workout.uid,
                                            from_date=target_day, to_date=workout.date)),
            ]
            proposal = Proposal(
                workout_uid=workout.uid, original_date=workout.date,
                proposed_date=target_day, action="move",
                reason=_hedge(reason, cls, title), confidence=cls.confidence,
                kind="SWAP", workout=workout, operations=ops,
                reason_details=[
                    "No day was free for a hard session without moving something.",
                    f"{victim.title} is easy enough to sit on {_day_name(victim_day)}.",
                ],
                tradeoff="Two sessions move instead of one — approve or reject them together.",
                rule_ids=["H1", "H2", "SWAP"],
                alternatives=_top_alternatives(candidates, target_day))
            moved = _place(_remove(week, victim.uid), victim, victim_day)
            return finish(proposal, _place(moved, workout, target_day))

    # --- rung 3: MERGE -----------------------------------------------------
    merge = _try_merge(ctx, week, workout, candidates)
    if merge:
        host_day, host = merge
        reason = (f"Folded {from_day}'s {_phrase(cls)} into {_day_name(host_day)}'s "
                  f"{_phrase(host.classification)} — same work, one session, and it "
                  f"keeps the rest of the week clear.")
        note = f"+ {workout.title} moved from {from_day}"
        proposal = Proposal(
            workout_uid=workout.uid, original_date=workout.date, proposed_date=host_day,
            action="move", reason=_hedge(reason, cls, title), confidence=cls.confidence,
            kind="MERGE", workout=workout,
            operations=[Operation(op="modify_note", workout_uid=host.uid,
                                  from_date=host_day, to_date=host_day, note=note,
                                  inverse=Operation(op="modify_note", workout_uid=host.uid,
                                                    from_date=host_day, to_date=host_day,
                                                    note=""))],
            reason_details=[
                f"{_day_name(host_day)} already has an endurance ride the intervals "
                f"can live inside.",
                f"Combined that day is {host.duration_min + workout.duration_min} minutes.",
            ],
            tradeoff="One long day instead of two — it's a bigger single session.",
            rule_ids=["MERGE", "H1", "H8"],
            alternatives=_top_alternatives(candidates, host_day))
        return finish(proposal, week)

    # --- rung 4: SHORTEN ---------------------------------------------------
    shorten = _try_shorten(ctx, week, workout, candidates)
    if shorten:
        target_day, new_duration = shorten
        reason = (f"Kept your {_phrase(cls)} in the week but cut it to {new_duration} "
                  f"minutes on {_day_name(target_day)}; the full {workout.duration_min} "
                  f"would make {_day_name(target_day)} your biggest day of the week.")
        proposal = Proposal(
            workout_uid=workout.uid, original_date=workout.date, proposed_date=target_day,
            action="soften", reason=_hedge(reason, cls, title), confidence=cls.confidence,
            kind="SHORTEN", workout=workout, new_duration_min=new_duration,
            operations=[Operation(op="reschedule", workout_uid=workout.uid,
                                  from_date=workout.date, to_date=target_day,
                                  new_duration_min=new_duration,
                                  inverse=Operation(op="reschedule",
                                                    workout_uid=workout.uid,
                                                    from_date=target_day,
                                                    to_date=workout.date,
                                                    new_duration_min=workout.duration_min))],
            reason_details=[f"You're {unavail}.",
                            f"Cut from {workout.duration_min} to {new_duration} minutes."],
            tradeoff=f"You lose {workout.duration_min - new_duration} minutes of the session.",
            rule_ids=["SHORTEN", "H8", "H9"],
            alternatives=_top_alternatives(candidates, target_day))
        shortened = replace(workout, classification=replace(
            cls, duration_min=new_duration, duration_source="explicit"))
        shortened.classification = _estimate_load(shortened.classification)
        return finish(proposal, _place(week, shortened, target_day))

    # --- rung 5: DOWNGRADE -------------------------------------------------
    downgrade = _try_downgrade(ctx, week, workout, candidates)
    if downgrade:
        target_day, easier = downgrade
        reason = (f"Proposing the {easier.name.lower()} version instead of "
                  f"{cls.intensity.name.lower()} on {_day_name(target_day)} — you're "
                  f"{unavail} and that was the only day left, but it sits next to a "
                  f"hard session.")
        proposal = Proposal(
            workout_uid=workout.uid, original_date=workout.date, proposed_date=target_day,
            action="soften", reason=_hedge(reason, cls, title), confidence=Confidence.LOW,
            kind="DOWNGRADE", workout=workout, new_intensity=easier,
            operations=[Operation(op="reschedule", workout_uid=workout.uid,
                                  from_date=workout.date, to_date=target_day,
                                  new_intensity=easier,
                                  inverse=Operation(op="reschedule",
                                                    workout_uid=workout.uid,
                                                    from_date=target_day,
                                                    to_date=workout.date,
                                                    new_intensity=cls.intensity))],
            reason_details=["Every day that could take the full session was blocked.",
                            f"An easier {easier.name.lower()} ride fits without stacking "
                            f"two hard days."],
            tradeoff="This changes what your coach asked for — it's a different session, "
                     "not the same one on a different day.",
            rule_ids=["DOWNGRADE", "H1"],
            alternatives=_top_alternatives(candidates, target_day))
        eased = replace(workout, classification=replace(
            cls, intensity=easier, min_possible=easier, max_possible=easier))
        eased.classification = _estimate_load(eased.classification)
        return finish(proposal, _place(week, eased, target_day))

    # --- rung 6: DROP ------------------------------------------------------
    sibling = _equal_or_harder_sibling(ctx, week, workout)
    if sibling is not None:
        reason = (f"I'd skip this one. You're {unavail}, you've still got "
                  f"{sibling.title} on {_day_name(sibling.date)}, and the only slots "
                  f"left would have cost you a rest day.")
        proposal = Proposal(
            workout_uid=workout.uid, original_date=workout.date, proposed_date=None,
            action="drop", reason=_hedge(reason, cls, title), confidence=cls.confidence,
            kind="DROP", workout=workout,
            operations=[Operation(op="unschedule", workout_uid=workout.uid,
                                  from_date=workout.date,
                                  inverse=Operation(op="schedule",
                                                    workout_uid=workout.uid,
                                                    to_date=workout.date))],
            reason_details=[f"{sibling.title} covers the same kind of work this week."],
            tradeoff="You lose the session entirely.",
            rule_ids=["DROP"],
            alternatives=_top_alternatives(candidates, None, limit=None))
        return finish(proposal, week)

    # --- rung 7: NO_PROPOSAL ----------------------------------------------
    counts: Dict[str, int] = {}
    for c in candidates:
        if not c.legal:
            counts[c.rule_id] = counts.get(c.rule_id, 0) + 1
    clause_for = {
        "H6": "{n} you've marked unavailable",
        "H1": "{n} would stack two hard days",
        "H2": "{n} would cost you a rest day",
        "H8": "{n} already have a session on them",
        "H7": "{n} have already gone",
        "H3": "{n} are the wrong side of your race",
        "H4": "{n} are inside your taper",
        "H5": "{n} are still race recovery",
        "H9": "{n} would be far bigger than any day you'd planned",
        "H10": "{n} aren't the day before the race",
    }
    clauses = [clause_for[rule].format(n=n) for rule, n in
               sorted(counts.items(), key=lambda kv: -kv[1]) if rule in clause_for]
    detail = ", ".join(clauses[:-1]) + (" and " + clauses[-1] if len(clauses) > 1
                                        else (clauses[0] if clauses else "nothing fits"))
    reason = (f"I can't place {title} anywhere this week without breaking something — "
              f"of the {len(candidates)} other days, {detail}. Your call.")
    proposal = Proposal(
        workout_uid=workout.uid, original_date=workout.date, proposed_date=None,
        action="keep", reason=_hedge(reason, cls, title), confidence=cls.confidence,
        kind="NO_PROPOSAL", workout=workout, operations=[],
        reason_details=[f"{_day_name(c.date)}: {c.reason}" for c in candidates if not c.legal],
        question="Want me to drop it, or shorten another day to make room?",
        rule_ids=sorted({c.rule_id for c in candidates if c.rule_id}),
        alternatives=_top_alternatives(candidates, None, limit=None))
    return finish(proposal, week)


def _try_swap(ctx, week, workout, candidates):
    """§2.4 rung 2 — 3-way shuffle: w -> d, and d's easy occupant v -> e."""
    for cand in sorted(candidates, key=lambda c: abs((c.date - workout.date).days)):
        if cand.legal or cand.rule_id != "H8":
            continue
        occupants = ctx.sessions(week, cand.date)
        if len(occupants) != 1:
            continue
        victim = occupants[0]
        if not victim.classification.is_easy:
            continue
        without = _remove(week, victim.uid)
        if not check_hard_constraints(ctx, without, workout, cand.date).legal:
            continue
        for other in ctx.week_days:
            if other in (cand.date, workout.date):
                continue
            trial = _place(without, workout, cand.date)
            if check_hard_constraints(ctx, trial, victim, other).legal:
                return cand.date, victim, other
    return None


def _try_merge(ctx, week, workout, candidates):
    """§2.4 rung 3 — fold intervals into a neighbouring endurance ride."""
    cls = workout.classification
    if cls.confidence < Confidence.MED or cls.intensity == Intensity.UNKNOWN:
        return None
    if cls.intensity not in HARD_CLASSES and cls.intensity != Intensity.TEMPO:
        return None
    for offset in (0, 1, -1):
        day = workout.date + timedelta(days=offset)
        if day not in ctx.week_days or day in ctx.unavailable or ctx.is_past(day):
            continue
        hosts = [w for w in ctx.sessions(week, day)
                 if w.classification.intensity == Intensity.ENDURANCE
                 and w.classification.confidence >= Confidence.MED]
        if not hosts:
            continue
        host = hosts[0]
        combined = host.duration_min + workout.duration_min
        if combined > ctx.max_day_duration_min:
            continue
        merged = replace(workout, date=day)
        trial = _place(_remove(week, host.uid), merged, day)
        if ctx._max_consecutive_hard(trial) > ctx.planned_max_consecutive_hard:
            continue
        return day, host
    return None


def _try_shorten(ctx, week, workout, candidates):
    """§2.4 rung 4 — the slot is nearly right; trim in 15-minute steps."""
    cls = workout.classification
    floor = MIN_USEFUL_MIN.get(cls.intensity)
    if floor is None or not workout.duration_min:
        return None
    blocked = [c for c in candidates if not c.legal and c.rule_id in ("H8", "H9")]
    for cand in sorted(blocked, key=lambda c: abs((c.date - workout.date).days)):
        if ctx.sessions(week, cand.date):
            continue  # a full day can't be fixed by making the session shorter
        duration = workout.duration_min - SHORTEN_STEP_MIN
        while duration >= floor:
            trial = replace(workout, classification=replace(
                cls, duration_min=duration, duration_source="explicit"))
            trial.classification = _estimate_load(trial.classification)
            if check_hard_constraints(ctx, week, trial, cand.date).legal:
                return cand.date, duration
            duration -= SHORTEN_STEP_MIN
    return None


def _try_downgrade(ctx, week, workout, candidates):
    """§2.4 rung 5 — only when the *slot* is the problem (H1)."""
    cls = workout.classification
    if cls.is_key or cls.intensity == Intensity.RACE:
        return None
    easier = DOWNGRADE_FOR_LEGALITY.get(cls.intensity)
    if easier is None:
        return None
    blocked = [c for c in candidates if not c.legal and c.rule_id == "H1"]
    for cand in sorted(blocked, key=lambda c: abs((c.date - workout.date).days)):
        if check_hard_constraints(ctx, week, workout, cand.date, intensity=easier).legal:
            return cand.date, easier
    return None


def _equal_or_harder_sibling(ctx, week, workout):
    """§2.4 rung 6 — is the week's quality already covered by another session?"""
    cls = workout.classification
    if cls.intensity == Intensity.UNKNOWN:
        return None
    for day in ctx.week_days:
        for other in ctx.sessions(week, day):
            if other.uid == workout.uid:
                continue
            ocls = other.classification
            if ocls.intensity == Intensity.UNKNOWN:
                continue
            same_family = (ocls.intensity in HARD_CLASSES) == (cls.intensity in HARD_CLASSES)
            if same_family and ocls.intensity >= cls.intensity:
                return other
    return None


# ---------------------------------------------------------------------------
# §4 GARMIN SIGNAL ADJUSTMENT
# ---------------------------------------------------------------------------


def _readiness_band(score: int) -> str:
    low, mid, high = READINESS_BANDS
    if score >= high:
        return "HIGH"
    if score >= mid:
        return "MODERATE"
    if score >= low:
        return "LOW"
    return "POOR"


def garmin_pass(ctx: _Context, week: WeekMap, garmin_signal,
                already_proposed: Sequence[str]) -> Tuple[List[Proposal], List[str]]:
    """§4 — a second pass over the (possibly rescheduled) week.

    The asymmetry rule, stated here as well as in the README: **this only ever
    reduces load or intensity.** High readiness produces a note, never a
    proposal to go harder. Going up is the athlete's call.
    """
    proposals: List[Proposal] = []
    warnings: List[str] = []
    if not garmin_signal:
        return proposals, warnings

    score, level, stamp = _parse_readiness(garmin_signal)
    status = str(garmin_signal.get("training_status") or "").upper() or None
    status_days = int(garmin_signal.get("status_days") or 0)
    acwr = garmin_signal.get("acwr")
    poor_streak = int(garmin_signal.get("poor_readiness_days") or 0)

    # §4.5 — PEAKING into a race defers entirely to §3.
    if status == "PEAKING":
        to_race = ctx.days_to_race(ctx.today)
        if to_race and to_race[0] <= 7:
            warnings.append("You're peaking for a race this week — I've left the "
                            "taper alone and made no readiness adjustments.")
            return proposals, warnings

    # §4.2 — freshness gate.
    if score is None:
        warnings.append("No training readiness from Garmin — left the week as planned.")
        return proposals, warnings
    if stamp is not None:
        age_h = (ctx.now - stamp).total_seconds() / 3600.0
        if age_h > ctx.config.readiness_max_age_hours:
            warnings.append(f"Garmin readiness is {int(age_h)}h old — too stale to act "
                            f"on, so I left the week as planned.")
            return proposals, warnings

    band = level.upper() if level else _readiness_band(score)
    if band in ("PRIME", "GOOD"):
        band = "HIGH"
    signals = [f"training_readiness={score} ({band})"
               + (f" @{stamp.isoformat(timespec='minutes')}" if stamp else "")]
    if status:
        signals.append(f"training_status={status}" + (f" {status_days}d" if status_days else ""))
    if acwr is not None:
        signals.append(f"acwr={acwr}")

    if band == "HIGH":
        warnings.append(f"Garmin has your readiness at {score} — you're fresh, "
                        f"this week should feel good.")

    # §4.5 — which sessions the training status wants capped.
    hard_sessions = [w for d in ctx.week_days for w in ctx.sessions(week, d) if w.is_hard]
    hard_sessions.sort(key=lambda w: w.date)
    overreaching = (status == "OVERREACHING" and status_days >= 2) or (
        acwr is not None and float(acwr) > 1.5)
    second_hard = hard_sessions[1] if len(hard_sessions) > 1 else None

    if poor_streak >= 2:
        warnings.append("Two poor-readiness days in a row — worth looking at the whole "
                        "week, not just today.")

    for day in ctx.week_days:
        for workout in ctx.sessions(week, day):
            if workout.uid in already_proposed:
                continue
            cls = workout.classification
            horizon = (day - ctx.today).days

            # §4.6 never-touch list.
            if ctx.race_on(day) or cls.intensity == Intensity.RACE:
                continue
            if cls.confidence == Confidence.NONE:
                continue  # can't downgrade what we can't classify — ask instead
            if cls.is_key and ctx.taper_race(day):
                continue

            binding_readiness = 0 <= horizon <= 1
            wants: List[Tuple[str, Optional[Intensity], Optional[int], str, List[str]]] = []

            if binding_readiness:
                if band == "LOW" and cls.intensity in DOWNGRADE_ONE_STEP:
                    easier = DOWNGRADE_ONE_STEP[cls.intensity]
                    wants.append(("DOWNGRADE", easier, None, "G_DOWN1", ["G_DOWN1"]))
                elif band == "LOW" and cls.intensity == Intensity.ENDURANCE:
                    new_dur = max(MIN_USEFUL_MIN[Intensity.ENDURANCE],
                                  int(workout.duration_min * 0.75))
                    wants.append(("SHORTEN", None, new_dur, "G_DOWN1", ["G_DOWN1"]))
                elif band == "POOR" and cls.intensity != Intensity.RECOVERY:
                    wants.append(("DOWNGRADE", Intensity.RECOVERY, 45, "G_DOWN2",
                                  ["G_DOWN2"]))
                elif band == "MODERATE" and cls.intensity in HARD_CLASSES:
                    warnings.append(
                        f"Readiness is {score} — if the first two intervals of "
                        f"{workout.title} feel flat, cut the set short.")

            if overreaching and second_hard is not None and workout.uid == second_hard.uid:
                easier = DOWNGRADE_ONE_STEP.get(cls.intensity)
                if easier:
                    wants.append(("DOWNGRADE", easier, None, "G_OVERREACH", ["G_OVERREACH"]))
            if status == "UNPRODUCTIVE" and cls.intensity in DOWNGRADE_ONE_STEP \
                    and binding_readiness:
                easier = DOWNGRADE_ONE_STEP[cls.intensity]
                wants.append(("DOWNGRADE", easier, workout.duration_min,
                              "G_UNPRODUCTIVE", ["G_UNPRODUCTIVE"]))

            if not wants:
                continue

            # "Take the more conservative result" when several rules fire.
            wants.sort(key=lambda w: (int(w[1]) if w[1] is not None else 99,
                                      w[2] if w[2] is not None else 9999))
            kind, new_intensity, new_duration, rule, rule_ids = wants[0]
            proposals.append(_garmin_proposal(
                ctx, workout, kind, new_intensity, new_duration, rule, rule_ids,
                score, band, status, signals))

    return proposals, warnings


def _garmin_proposal(ctx, workout, kind, new_intensity, new_duration, rule, rule_ids,
                     score, band, status, signals) -> Proposal:
    cls = workout.classification
    title = workout.title or "this session"
    day = _day_name(workout.date)
    if rule.startswith("G_OVERREACH"):
        signal_phrase = ("Garmin has had you flagged as overreaching for a couple of "
                         "days, so I'd take the dig out of your second hard session")
    elif rule == "G_UNPRODUCTIVE":
        signal_phrase = ("Garmin says your training is unproductive right now, so I'd "
                         "keep the hours and drop the intensity")
    else:
        signal_phrase = f"Garmin has your readiness at {score} this morning ({band.lower()})"

    if kind == "SHORTEN":
        reason = (f"Kept {title} on {day} but cut it to {new_duration} minutes — "
                  f"{signal_phrase}.")
        action = "soften"
        op = Operation(op="reschedule", workout_uid=workout.uid, from_date=workout.date,
                       to_date=workout.date, new_duration_min=new_duration,
                       inverse=Operation(op="reschedule", workout_uid=workout.uid,
                                         from_date=workout.date, to_date=workout.date,
                                         new_duration_min=workout.duration_min))
    else:
        reason = (f"Proposing the {new_intensity.name.lower()} version of {title} on "
                  f"{day} instead of {cls.intensity.name.lower()} — {signal_phrase}.")
        action = "soften"
        op = Operation(op="reschedule", workout_uid=workout.uid, from_date=workout.date,
                       to_date=workout.date, new_intensity=new_intensity,
                       new_duration_min=new_duration,
                       inverse=Operation(op="reschedule", workout_uid=workout.uid,
                                         from_date=workout.date, to_date=workout.date,
                                         new_intensity=cls.intensity,
                                         new_duration_min=workout.duration_min))

    details = [f"Readiness {score} ({band})."]
    if status:
        details.append(f"Garmin training status: {status.title()}.")
    details.append("This only ever reduces the session — I never propose going harder "
                   "off a readiness score.")

    return Proposal(
        workout_uid=workout.uid, original_date=workout.date, proposed_date=workout.date,
        action=action, reason=_hedge(reason, cls, title),
        confidence=min(cls.confidence, Confidence.MED),
        kind=kind, workout=workout, operations=[op], new_intensity=new_intensity,
        new_duration_min=new_duration, reason_details=details, rule_ids=rule_ids,
        tradeoff="You do less than your coach planned today.",
        signals_used=list(signals))


# ---------------------------------------------------------------------------
# PUBLIC API
# ---------------------------------------------------------------------------


def build_adjustment_plan(workouts, unavailable_days=None, garmin_signal=None,
                          race_dates=None, *, today=None, now=None, week_start=None,
                          learned_overrides=None, config=None) -> AdjustmentPlan:
    """The full §0.1 ``AdjustmentPlan``: proposals, the unsolvable ones, and
    the before/after week summaries.

    Pure. Give it the same inputs and it returns the same proposal ids.
    """
    config = config or EngineConfig()
    now = now or datetime.now()
    today = _as_date(today) or now.date()

    planned = _to_planned(workouts, learned_overrides=learned_overrides)
    unavailable = _to_unavailable(unavailable_days)
    races = _to_races(race_dates, planned)

    if week_start is not None:
        anchor = _as_date(week_start)
    else:
        focus = sorted(d for d in unavailable
                       if any(w.date == d for w in planned)) or sorted(unavailable)
        anchor = focus[0] if focus else today
    anchor = anchor - timedelta(days=anchor.weekday())  # Monday

    ctx = _Context(planned, unavailable, races, anchor, today, now, config)
    before = ctx.summarise(ctx.baseline)

    # §2.3 — everything displaced comes off the calendar first, so the vacated
    # day already counts as rest while we look for homes.
    all_displaced = [w for d in unavailable
                     for w in ctx.baseline.get(d, [])
                     if d in ctx.week_days and w.is_session]
    # §3.6 — a race is a fixed point, not a session to reshuffle. "I can't make
    # Saturday" when Saturday is the race is a decision about the race; the
    # engine must not answer it by proposing you ride the Gran Fondo on Sunday.
    fixed_races = [w for w in all_displaced if w.intensity == Intensity.RACE]
    displaced = [w for w in all_displaced if w.intensity != Intensity.RACE]

    working = _copy_week(ctx.baseline)
    for w in displaced:
        working = _remove(working, w.uid)

    proposals: List[Proposal] = []
    unresolved: List[Proposal] = []

    for workout in fixed_races:
        reason = unavailable.get(workout.date)
        proposals.append(Proposal(
            workout_uid=workout.uid, original_date=workout.date, proposed_date=None,
            action="keep",
            reason=(f"Left {workout.title} where it is. You've marked "
                    f"{_day_name(workout.date)} unavailable"
                    + (f" ({reason})" if reason else "")
                    + " — but that's a decision about the race itself, not something "
                      "I can reschedule around."),
            confidence=workout.classification.confidence, kind="KEEP", workout=workout,
            reason_details=["Races are fixed points on the calendar (§3.6)."],
            rule_ids=["H3"]))
    for workout in _displacement_order(displaced):
        reason = unavailable.get(workout.date)
        proposal, working = _resolve_one(ctx, working, workout, reason,
                                         before.load_known_fraction)
        if proposal.kind == "NO_PROPOSAL":
            unresolved.append(proposal)
        else:
            proposals.append(proposal)

    handled = {p.workout_uid for p in proposals + unresolved}
    garmin_proposals, garmin_warnings = garmin_pass(ctx, working, garmin_signal, handled)
    proposals.extend(garmin_proposals)

    after = ctx.summarise(working)
    warnings = list(dict.fromkeys(ctx.warnings + garmin_warnings))
    if after.load_known_fraction < 1.0:
        warnings.append(
            f"I could only size {int(after.load_known_fraction * 100)}% of this week's "
            f"sessions — the feed doesn't give durations for all of them.")

    trigger_days = ", ".join(f"{_day_name(d)} {d.isoformat()}"
                             for d in sorted(unavailable) if d in ctx.week_days)
    trigger = (f"user marked {trigger_days} unavailable" if trigger_days
               else "garmin signal review")

    return AdjustmentPlan(
        week_start=anchor, trigger=trigger, proposals=proposals, unresolved=unresolved,
        week_summary_before=before, week_summary_after=after, warnings=warnings,
        generated_at=now)


def propose_adjustments(workouts, unavailable_days=None, garmin_signal=None,
                        race_dates=None, **kwargs) -> List[Proposal]:
    """Return the proposals for a week. Never applies anything.

    ``workouts``          list of ``{date, title, description, uid}`` dicts,
                          exactly what ``TrainingPeaksCalendarFeed`` returns,
                          or ``PlannedWorkout`` objects.
    ``unavailable_days``  dates, ``{date: reason}``, or ``{"date","reason"}`` dicts.
    ``garmin_signal``     ``{"readiness": {"score":41,...}, "training_status": ...}``
                          or None.
    ``race_dates``        dates or ``{"date","title","priority","duration_min"}``.

    Unsolvable conflicts come back as ``kind="NO_PROPOSAL"`` proposals with
    every rejected day listed — an unsolvable week must be visible, not silent.
    """
    return build_adjustment_plan(workouts, unavailable_days, garmin_signal,
                                 race_dates, **kwargs).all_proposals


# ---------------------------------------------------------------------------
# DEMO
# ---------------------------------------------------------------------------


def _demo_week():
    """A realistic build week: Monday 27 Jul 2026 through Sunday 2 Aug."""
    return [
        {"uid": "tp-1", "date": "2026-07-27", "title": "Recovery spin 45min",
         "description": "Easy legs, Z1, keep it under 150W."},
        {"uid": "tp-2", "date": "2026-07-28", "title": "Threshold 3x12min",
         "description": "3x12 @ 95-100% FTP, 6min recovery between efforts."},
        {"uid": "tp-3", "date": "2026-07-29", "title": "Rest day",
         "description": "Full rest."},
        {"uid": "tp-4", "date": "2026-07-30", "title": "Endurance 2hr Z2",
         "description": "Steady aerobic ride, 2hr, zone 2 throughout."},
        {"uid": "tp-5", "date": "2026-07-31", "title": "Rest day", "description": ""},
        {"uid": "tp-6", "date": "2026-08-01", "title": "VO2 max 5x5",
         "description": "5x5min at VO2 max, 5min recoveries. Key session."},
        {"uid": "tp-7", "date": "2026-08-02", "title": "Endurance 3hr Z2",
         "description": "Long ride, 3hr steady, zone 2."},
    ]


def _print_proposal(proposal: Proposal) -> None:
    where = proposal.proposed_date.isoformat() if proposal.proposed_date else "—"
    print(f"  [{proposal.action.upper():6}] {proposal.kind:12} "
          f"{proposal.original_date} -> {where}   (confidence {proposal.confidence.name})")
    print(f"     {proposal.reason}")
    for bullet in proposal.reason_details[:4]:
        print(f"       · {bullet}")
    if proposal.tradeoff:
        print(f"     trade-off: {proposal.tradeoff}")
    if proposal.question:
        print(f"     question:  {proposal.question}")
    if proposal.signals_used:
        print(f"     signals:   {', '.join(proposal.signals_used)}")
    print(f"     approval required: {proposal.requires_approval}")


if __name__ == "__main__":
    NOW = datetime(2026, 7, 27, 7, 0)

    print("=" * 78)
    print("CLASSIFICATION (§8 test strings)")
    print("=" * 78)
    samples = [
        ("Recovery spin 45min", ""),
        ("Rest day", ""),
        ("Recovery day", ""),
        ("VO2 max 5x5", "5x5min at VO2 max"),
        ("Endurance 2hr Z2", "Steady aerobic ride"),
        ("Race simulation 3x20", "3x20 at race pace"),
        ("Gran Fondo", "Marmotte, 175km"),
        ("Endurance 3hr with 3x12 @ threshold", ""),
        ("Ride 60min", ""),
        ("Openers", "20min easy with 3x1min primers"),
        ("", ""),
    ]
    print(f"{'title':38} {'sport':9} {'intensity':10} {'conf':5} {'min':>4} {'TSS/load':>9}")
    print("-" * 78)
    for title, description in samples:
        info = classify_workout(title, description)
        load = info["load"]
        print(f"{(title or '(empty)'):38} {info['sport']:9} {info['intensity']:10} "
              f"{info['confidence']:5} {str(info['duration_min'] or '-'):>4} "
              f"{str(load if load is not None else '-'):>9}")
        if info["question"]:
            print(f"{'':38} ? {info['question']}")

    print()
    print("=" * 78)
    print("SCENARIO 1 — 'I have to work Tuesday'  (the core flow)")
    print("=" * 78)
    plan = build_adjustment_plan(
        _demo_week(),
        unavailable_days={"2026-07-28": "work"},
        now=NOW, today=date(2026, 7, 27))
    print(f"trigger: {plan.trigger}")
    print(f"before:  {plan.week_summary_before.to_dict()}")
    print(f"after:   {plan.week_summary_after.to_dict()}")
    for proposal in plan.all_proposals:
        _print_proposal(proposal)
    for warning in plan.warnings:
        print(f"  ! {warning}")

    print()
    print("=" * 78)
    print("SCENARIO 2 — same week, but Saturday is a race")
    print("=" * 78)
    plan = build_adjustment_plan(
        _demo_week()[:5] + [
            {"uid": "tp-race", "date": "2026-08-01", "title": "Gran Fondo",
             "description": "B race, 4hr"},
            {"uid": "tp-7", "date": "2026-08-02", "title": "Recovery spin 45min",
             "description": "Easy legs the day after."}],
        unavailable_days={"2026-07-28": "work"},
        race_dates=[{"date": "2026-08-01", "title": "the Gran Fondo",
                     "priority": "C", "duration_min": 240}],
        now=NOW, today=date(2026, 7, 27))
    for proposal in plan.all_proposals:
        _print_proposal(proposal)

    print()
    print("=" * 78)
    print("SCENARIO 3 — nowhere to put it (every other day is taken)")
    print("=" * 78)
    packed = [
        {"uid": "p-1", "date": "2026-07-27", "title": "Threshold 2x20min",
         "description": "2x20 at FTP."},
        {"uid": "p-2", "date": "2026-07-28", "title": "VO2 max 5x5",
         "description": "5x5min at VO2 max."},
        {"uid": "p-3", "date": "2026-07-29", "title": "Threshold 3x12min",
         "description": "3x12 at FTP."},
        {"uid": "p-4", "date": "2026-07-30", "title": "Endurance 3hr Z2", "description": ""},
        {"uid": "p-5", "date": "2026-07-31", "title": "Endurance 2hr Z2", "description": ""},
        {"uid": "p-6", "date": "2026-08-01", "title": "Endurance 4hr Z2", "description": ""},
        {"uid": "p-7", "date": "2026-08-02", "title": "Endurance 2hr Z2", "description": ""},
    ]
    plan = build_adjustment_plan(packed, unavailable_days={"2026-07-28": "work"},
                                 now=NOW, today=date(2026, 7, 27))
    for proposal in plan.all_proposals:
        _print_proposal(proposal)
        print(f"     alternatives considered: {len(proposal.alternatives)}")
        for alt in proposal.alternatives:
            print(f"       - {_day_name(alt.date)}: {alt.reason}  [{alt.rule_id}]")

    print()
    print("=" * 78)
    print("SCENARIO 4 — Garmin says readiness is 41 this morning")
    print("=" * 78)
    plan = build_adjustment_plan(
        _demo_week(),
        garmin_signal={"readiness": {"score": 41, "level": "LOW",
                                     "timestamp": "2026-07-27T06:12:00"},
                       "training_status": "PRODUCTIVE"},
        now=NOW, today=date(2026, 7, 28))
    for proposal in plan.all_proposals:
        _print_proposal(proposal)
    for warning in plan.warnings:
        print(f"  ! {warning}")

    print()
    print("Nothing above was applied. Every line is a proposal awaiting approval.")
