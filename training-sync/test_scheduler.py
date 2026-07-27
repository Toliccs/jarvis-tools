"""Tests for the classification + rescheduling engine.

Run:  python3 -m pytest test_scheduler.py -v

Every test pins ``now``/``today`` explicitly. The engine reads the clock only
through those parameters, so the suite is deterministic and will still pass in
2027.
"""

from datetime import date, datetime

import pytest

from scheduler import (
    Confidence,
    EngineConfig,
    Intensity,
    Proposal,
    ProposalError,
    build_adjustment_plan,
    classify_workout,
    propose_adjustments,
)

# Week of Monday 27 Jul 2026 - Sunday 2 Aug 2026.
MON, TUE, WED, THU, FRI, SAT, SUN = (
    "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30",
    "2026-07-31", "2026-08-01", "2026-08-02",
)
NOW = datetime(2026, 7, 27, 7, 0)  # Monday morning, before the 18:00 cutoff
TODAY = date(2026, 7, 27)


def plan_for(workouts, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("today", TODAY)
    return build_adjustment_plan(workouts, **kwargs)


def proposals_for(workouts, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("today", TODAY)
    return propose_adjustments(workouts, **kwargs)


def by_uid(proposals, uid):
    return next((p for p in proposals if p.workout_uid == uid), None)


# ---------------------------------------------------------------------------
# §1 CLASSIFICATION
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title,description,intensity,confidence", [
    # The §8 table, verbatim.
    ("Recovery spin 45min", "", "RECOVERY", "HIGH"),
    ("Rest day", "", "REST", "HIGH"),
    ("Recovery day", "", "REST", "HIGH"),
    ("VO2 max 5x5", "", "VO2", "HIGH"),
    ("Endurance 2hr Z2", "", "ENDURANCE", "HIGH"),
    ("Gran Fondo", "", "RACE", "HIGH"),
    # One per remaining class.
    ("Tempo 3x15 sweet spot", "", "TEMPO", "HIGH"),
    ("Threshold 2x20min", "2x20 at FTP", "THRESHOLD", "HIGH"),
    ("Day off", "", "REST", "HIGH"),
    ("Openers", "20min easy with 3x1min primers", "RECOVERY", "HIGH"),
])
def test_classifies_each_intensity(title, description, intensity, confidence):
    info = classify_workout(title, description)
    assert info["intensity"] == intensity
    assert info["confidence"] == confidence


def test_recovery_spin_is_not_rest():
    """The single most common misclassification (§1.1 Tier 2 guard):
    'recovery day' is a day off, 'recovery spin' is a ride."""
    assert classify_workout("Recovery day", "")["intensity"] == "REST"
    spin = classify_workout("Recovery spin 45min", "")
    assert spin["intensity"] == "RECOVERY"
    assert spin["duration_min"] == 45


def test_rest_guard_rejects_rest_when_there_is_an_activity():
    assert classify_workout("Rest day", "")["intensity"] == "REST"
    # A duration or an activity token means it isn't actually a day off.
    assert classify_workout("Recovery day", "45min easy spin")["intensity"] != "REST"


def test_race_simulation_is_a_workout_not_a_race():
    """§1.1 R_RACE_NEGATE — 'race simulation' must not pin an unmovable race."""
    info = classify_workout("Race simulation 3x20", "3x20 at race pace")
    assert info["intensity"] == "THRESHOLD"
    assert info["is_race"] is False
    assert "R_RACE_NEGATE" in info["matched_rules"]


def test_pre_race_spin_is_not_a_race():
    info = classify_workout("Pre-race spin", "30min with a few primers")
    assert info["is_race"] is False
    assert info["intensity"] == "RECOVERY"


def test_conflicting_families_take_the_dominant_stressor():
    """§1.3 — the hardest thing in the session sets the recovery cost, the
    duration stays the ride's real length, and confidence drops a notch."""
    info = classify_workout("Endurance 3hr with 3x12 @ threshold", "")
    assert info["intensity"] == "THRESHOLD"
    assert info["confidence"] == "MED"
    assert info["duration_min"] == 180
    assert info["min_possible"] == "ENDURANCE"
    assert info["max_possible"] == "THRESHOLD"


def test_interval_rest_is_not_a_recovery_ride():
    """'6min recovery' between efforts must not read as a recovery ride.

    §1.3's fold would still land on THRESHOLD without the guard, so the thing
    to assert is what the guard actually buys: full confidence and a lower
    bound of THRESHOLD. Without it the session looks like a RECOVERY/THRESHOLD
    conflict, confidence drops to MED, and the reason string starts hedging
    about a session the feed described perfectly clearly.
    """
    info = classify_workout("Threshold 3x12min",
                            "3x12 @ 95-100% FTP, 6min recovery between efforts.")
    assert info["intensity"] == "THRESHOLD"
    assert info["is_hard"] is True
    assert info["confidence"] == "HIGH"
    assert info["min_possible"] == "THRESHOLD"
    assert "R_CONFLICT" not in info["matched_rules"]


@pytest.mark.parametrize("title", [
    "Threshold 3x12min",
    "Threshold 3 x 12 min",   # spaced notation — the case the mask exists for
    "VO2 6 x 5 min",
])
def test_interval_reps_are_not_read_as_session_duration(title):
    """Rep length is not session length: '3 x 12 min' is three twelve-minute
    efforts, not a twelve-minute workout."""
    info = classify_workout(title, "")
    assert info["duration_min"] not in (5, 12), \
        f"{title!r} parsed its rep length as the session duration"


def test_title_and_description_durations_are_not_summed():
    """One 3-hour ride described twice is still a 3-hour ride."""
    info = classify_workout("Endurance 3hr Z2", "Long ride, 3hr steady, zone 2.")
    assert info["duration_min"] == 180


@pytest.mark.parametrize("description", [
    "Easy Z1 spin, 45 min. Keep it under 55% FTP.",
    "Recovery ride, 50-60% FTP.",
    "Coffee ride, nothing over 60% of FTP.",
])
def test_subthreshold_ftp_ceiling_is_not_a_threshold_session(description):
    """'under 55% FTP' is how a recovery ride is written, not a threshold cue.

    The bare \\bftp\\b in R_THRESHOLD used to fire on the ceiling, fold to
    THRESHOLD as the dominant stressor, and mark the day hard — so the easiest
    ride of the week defended itself against every spacing rule and inflated
    the week's load with a threshold IF proxy.
    """
    info = classify_workout("Recovery spin", description)
    assert info["intensity"] == "RECOVERY"
    assert info["is_hard"] is False
    assert info["confidence"] == "HIGH"
    assert "R_CONFLICT" not in info["matched_rules"]


@pytest.mark.parametrize("description,expected", [
    ("2x20 @ 88-92% FTP", "THRESHOLD"),   # sweet spot cites FTP for real
    ("3x12 @ 95-100% FTP", "THRESHOLD"),
])
def test_ftp_at_or_above_the_band_still_reads_as_threshold(description, expected):
    """The ceiling guard must not swallow genuine threshold prescriptions."""
    assert classify_workout("Bike", description)["intensity"] == expected


def test_stated_total_duration_is_not_added_to_its_own_segments():
    """A 'Duration:' field restates the session length, it does not extend it.

    Summing it with the warm-up and cool-down lines it summarises made this
    75-minute session 111 minutes, which is the number that would have been
    pushed to the head unit as the placeholder length.
    """
    info = classify_workout("Threshold 3x12min",
                            "Warm up 20min.\n"
                            "3x12 @ 95-100% FTP with 6min easy between.\n"
                            "Cool down 10min.\n"
                            "Planned TSS: 82\n"
                            "Duration: 1:15")
    assert info["duration_min"] == 75


def test_segment_lines_without_a_stated_total_still_sum():
    """The sum-across-lines behaviour is the fallback, and stays intact."""
    info = classify_workout("Endurance ride",
                            "Warm up 15min.\nSteady zone 2 for 90min.\nCool down 15min.")
    assert info["duration_min"] == 120


def test_unknown_refuses_to_guess_and_asks():
    """§1.5 — a 60-minute ride is equally likely Z2 or VO2."""
    info = classify_workout("Ride 60min", "")
    assert info["intensity"] == "UNKNOWN"
    assert info["confidence"] == "NONE"
    assert info["load"] is None  # contributes nothing to any load claim
    assert info["question"]
    # Evaluated at its upper bound for every hard constraint.
    assert info["max_possible"] == "VO2"
    assert info["is_hard"] is True


def test_empty_title_is_unknown_not_a_crash():
    info = classify_workout("", "")
    assert info["intensity"] == "UNKNOWN"
    assert info["question"]


def test_explicit_tss_wins_over_estimation():
    info = classify_workout("Endurance 2hr Z2", "Planned TSS: 140")
    assert info["tss"] == 140
    assert info["load"] == 140
    assert info["load_confidence"] == "HIGH"


def test_sport_detection():
    assert classify_workout("Endurance 2hr Z2", "Long ride")["sport"] == "cycling"
    assert classify_workout("Easy run 40min", "")["sport"] == "running"
    assert classify_workout("Swim 2000m", "Pool session, 45min")["sport"] == "swimming"
    assert classify_workout("Gym strength", "45min core and legs")["sport"] == "strength"


def test_learned_override_short_circuits_the_ladder():
    """§1.7 — how the engine stops asking the same question every week."""
    overrides = {"ride #min": {"intensity": "ENDURANCE", "duration_min": 60}}
    info = classify_workout("Ride 60min", "", learned_overrides=overrides)
    assert info["intensity"] == "ENDURANCE"
    assert info["user_override"] is True
    assert info["question"] is None


# ---------------------------------------------------------------------------
# §2 THE CORE FLOW — "I have to work Tuesday"
# ---------------------------------------------------------------------------


def base_week():
    """A realistic build week. Tuesday holds the interval session."""
    return [
        {"uid": "w-mon", "date": MON, "title": "Recovery spin 45min",
         "description": "Easy legs, Z1."},
        {"uid": "w-tue", "date": TUE, "title": "Threshold 3x12min",
         "description": "3x12 @ 95-100% FTP, 6min recovery between efforts."},
        {"uid": "w-wed", "date": WED, "title": "Rest day", "description": "Full rest."},
        {"uid": "w-thu", "date": THU, "title": "Endurance 2hr Z2",
         "description": "Steady aerobic ride."},
        {"uid": "w-fri", "date": FRI, "title": "Rest day", "description": ""},
        {"uid": "w-sat", "date": SAT, "title": "VO2 max 5x5",
         "description": "5x5min at VO2 max, 5min recoveries."},
        {"uid": "w-sun", "date": SUN, "title": "Endurance 3hr Z2",
         "description": "Long ride, steady."},
    ]


def test_tuesday_interval_moves_to_wednesday():
    """The user's exact scenario: Tuesday is a work day, so the threshold
    session lands on Wednesday's rest day."""
    proposals = proposals_for(base_week(), unavailable_days={TUE: "work"})
    moved = by_uid(proposals, "w-tue")
    assert moved is not None
    assert moved.action == "move"
    assert moved.kind == "MOVE"
    assert moved.original_date == date(2026, 7, 28)
    assert moved.proposed_date == date(2026, 7, 29)  # Wednesday


def test_the_move_explains_itself_in_plain_english():
    """§5.1 — the reason names the trigger and the constraint, in second
    person, with no rule ids."""
    proposals = proposals_for(base_week(), unavailable_days={TUE: "work"})
    moved = by_uid(proposals, "w-tue")
    reason = moved.reason
    assert reason.strip()
    assert "Tuesday" in reason and "Wednesday" in reason
    assert "working" in reason.lower()
    for jargon in ("H1", "H2", "R_", "rule", "score"):
        assert jargon not in reason
    # The road not taken is attached for the rationale panel.
    assert moved.alternatives
    assert all(alt.reason.strip() for alt in moved.alternatives)


def test_only_the_displaced_workout_is_touched():
    proposals = proposals_for(base_week(), unavailable_days={TUE: "work"})
    assert [p.workout_uid for p in proposals] == ["w-tue"]


def test_rest_days_are_not_treated_as_displaced_sessions():
    """Wednesday's 'Rest day' entry is calendar furniture, not a session."""
    proposals = proposals_for(base_week(), unavailable_days={WED: "work"})
    assert proposals == []


# ---------------------------------------------------------------------------
# H1 — REFUSING TO STACK TWO HARD DAYS
# ---------------------------------------------------------------------------


def test_never_stacks_two_hard_days():
    """Friday is free and closer to nothing else, but Saturday is a VO2 day,
    so Friday must be rejected for a threshold session."""
    proposals = proposals_for(base_week(), unavailable_days={TUE: "work"})
    moved = by_uid(proposals, "w-tue")
    assert moved.proposed_date != date(2026, 7, 31)  # not Friday, next to Sat VO2
    friday = next((a for a in moved.alternatives if a.date == date(2026, 7, 31)), None)
    assert friday is not None, "Friday should be listed as a rejected option"
    assert friday.rule_id == "H1"
    assert "back to back" in friday.reason


def test_h1_rejects_a_hard_session_landing_beside_a_hard_day():
    """Wednesday is the only empty day, but it now sits between two hard days,
    so there is nowhere legal left and the engine must not pretend otherwise."""
    week = [
        {"uid": "h-mon", "date": MON, "title": "Endurance 2hr Z2", "description": ""},
        {"uid": "h-tue", "date": TUE, "title": "VO2 max 5x5", "description": "5x5min VO2"},
        {"uid": "h-thu", "date": THU, "title": "Threshold 2x20min", "description": "2x20 FTP"},
        {"uid": "h-fri", "date": FRI, "title": "Endurance 2hr Z2", "description": ""},
        {"uid": "h-sat", "date": SAT, "title": "Threshold 3x12min", "description": "3x12 FTP"},
        {"uid": "h-sun", "date": SUN, "title": "Endurance 3hr Z2", "description": ""},
    ]
    proposals = proposals_for(week, unavailable_days={TUE: "work"})
    moved = by_uid(proposals, "h-tue")
    # Wednesday is the only free day and it is flanked by Thursday's threshold.
    assert moved.proposed_date != date(2026, 7, 29)
    blocked = {a.rule_id for a in moved.alternatives}
    assert "H1" in blocked


def test_escape_valve_allows_a_block_the_coach_already_prescribed():
    """H1's escape valve: if the plan already runs two hard days together, the
    engine may too — it just may never make the week harder-packed."""
    week = [
        {"uid": "b-mon", "date": MON, "title": "Threshold 2x20min", "description": "2x20 FTP"},
        {"uid": "b-tue", "date": TUE, "title": "VO2 max 5x5", "description": "5x5min VO2"},
        {"uid": "b-wed", "date": WED, "title": "Rest day", "description": ""},
        {"uid": "b-thu", "date": THU, "title": "Endurance 2hr Z2", "description": ""},
        {"uid": "b-fri", "date": FRI, "title": "Rest day", "description": ""},
        {"uid": "b-sat", "date": SAT, "title": "Endurance 3hr Z2", "description": ""},
        {"uid": "b-sun", "date": SUN, "title": "Rest day", "description": ""},
    ]
    # Monday+Tuesday are already back-to-back hard, so a run of 2 is allowed.
    proposals = proposals_for(week, unavailable_days={TUE: "work"})
    moved = by_uid(proposals, "b-tue")
    assert moved.kind in ("MOVE", "SWAP", "MERGE", "SHORTEN")
    assert moved.proposed_date is not None


# ---------------------------------------------------------------------------
# §3 RACE PROXIMITY
# ---------------------------------------------------------------------------


def race_week():
    """Saturday is a C-priority race, so Thursday and Friday are taper days."""
    return [
        {"uid": "r-mon", "date": MON, "title": "Recovery spin 45min", "description": "Z1"},
        {"uid": "r-tue", "date": TUE, "title": "Threshold 3x12min", "description": "3x12 FTP"},
        {"uid": "r-wed", "date": WED, "title": "Rest day", "description": ""},
        {"uid": "r-thu", "date": THU, "title": "Rest day", "description": ""},
        {"uid": "r-fri", "date": FRI, "title": "Rest day", "description": ""},
        {"uid": "r-sun", "date": SUN, "title": "Recovery spin 45min", "description": "Z1"},
    ]


RACE = [{"date": SAT, "title": "the Gran Fondo", "priority": "C", "duration_min": 240}]


def test_nothing_is_ever_scheduled_onto_race_day():
    proposals = proposals_for(race_week(), unavailable_days={TUE: "work"}, race_dates=RACE)
    for proposal in proposals:
        assert proposal.proposed_date != date(2026, 8, 1)


def test_taper_days_reject_additions():
    """§3.2 T1 / H4 — no inbound placements into the taper window."""
    proposals = proposals_for(race_week(), unavailable_days={TUE: "work"}, race_dates=RACE)
    moved = by_uid(proposals, "r-tue")
    assert moved.proposed_date not in (date(2026, 7, 30), date(2026, 7, 31))
    taper_rejections = [a for a in moved.alternatives if a.rule_id in ("H4", "H3", "T3_FINAL48")]
    assert taper_rejections, "taper days should appear as rejected options"


def test_taper_window_rejects_additions_even_when_it_is_the_closest_day():
    """H4 in isolation.

    An A-priority race the following Wednesday tapers back over Wed-Sun of
    this week. Wednesday is empty and one day away — the obvious slot on every
    other measure. Only H4 keeps the session out of the taper, and the honest
    answer becomes 'skip it', not 'squeeze it in'.
    """
    week = [
        {"uid": "tw-mon", "date": MON, "title": "Threshold 2x20min", "description": "2x20 FTP"},
        {"uid": "tw-tue", "date": TUE, "title": "Threshold 3x12min", "description": "3x12 FTP"},
    ]
    race = [{"date": "2026-08-05", "title": "the national champs",
             "priority": "A", "duration_min": 180}]
    proposals = proposals_for(week, unavailable_days={TUE: "work"}, race_dates=race)
    stuck = by_uid(proposals, "tw-tue")
    assert stuck.proposed_date is None, "nothing may be added inside an A-race taper"
    assert any(a.rule_id == "H4" and a.date == date(2026, 7, 29)
               for a in stuck.alternatives)


def test_race_day_is_never_a_candidate_even_for_an_easy_spin():
    """H3 in isolation.

    Friday is the closest legal-looking day (Monday is full, Wednesday and
    Thursday are taper), and a recovery spin is exactly the kind of session
    the final-48h rule *does* allow — so H3 is the only thing standing between
    the athlete and a spin scheduled on top of their race.
    """
    week = [
        {"uid": "rd-mon", "date": MON, "title": "Threshold 2x20min", "description": "2x20 FTP"},
        {"uid": "rd-tue", "date": TUE, "title": "Recovery spin 45min", "description": "Z1"},
    ]
    race = [{"date": FRI, "title": "the Gran Fondo", "priority": "C", "duration_min": 240}]
    proposals = proposals_for(week, unavailable_days={TUE: "work"}, race_dates=race)
    moved = by_uid(proposals, "rd-tue")
    assert moved.proposed_date != date(2026, 7, 31), "never schedule onto race day"
    friday = next(a for a in moved.alternatives if a.date == date(2026, 7, 31))
    assert friday.rule_id == "H3"
    assert "Gran Fondo itself" in friday.reason
    # H3 also splits the week either side of the race: a Tuesday session does
    # not get "moved" to after Friday's race, so there is genuinely nowhere
    # left and the engine says so rather than inventing a slot.
    assert moved.kind == "NO_PROPOSAL"
    assert {a.rule_id for a in moved.alternatives} == {"H8", "H4", "H3"}


def test_the_race_itself_is_a_fixed_point():
    """§3.6 — marking the race day unavailable is a decision about the race.
    The engine must not answer it by proposing you ride the Gran Fondo on
    Sunday, nor treat it as an unsolvable scheduling puzzle."""
    week = race_week() + [{"uid": "r-race", "date": SAT, "title": "Gran Fondo",
                           "description": "C race"}]
    proposals = proposals_for(week, unavailable_days={SAT: "work"}, race_dates=RACE)
    race_proposal = by_uid(proposals, "r-race")
    assert race_proposal.kind == "KEEP"
    assert race_proposal.action == "keep"
    assert race_proposal.proposed_date is None
    assert "race" in race_proposal.reason.lower()


def test_workout_inside_the_taper_is_kept_not_reshuffled():
    """§3.2 T2 — displaced taper intensity is skipped, never relocated inside
    the taper, because taper placement is deliberate."""
    week = [
        {"uid": "t-thu", "date": THU, "title": "Openers", "description": "20min with 3x1min"},
        {"uid": "t-fri", "date": FRI, "title": "Threshold 2x10min", "description": "2x10 FTP"},
    ]
    race = [{"date": SAT, "title": "the Gran Fondo", "priority": "A", "duration_min": 240}]
    proposals = proposals_for(week, unavailable_days={FRI: "work"}, race_dates=race)
    kept = by_uid(proposals, "t-fri")
    assert kept.action in ("keep", "drop")
    assert kept.proposed_date is None
    assert "taper" in kept.reason.lower() or "race week" in kept.reason.lower()


def test_openers_are_dropped_rather_than_moved():
    """§3.4 / H10 — openers only work the day before the race, so if that day
    is gone they are skipped, never quietly slid to two days out."""
    week = [{"uid": "o-fri", "date": FRI, "title": "Openers",
             "description": "20min easy with 3x1min primers"}]
    race = [{"date": SAT, "title": "the Gran Fondo", "priority": "C", "duration_min": 240}]
    proposals = proposals_for(week, unavailable_days={FRI: "work"}, race_dates=race)
    openers = by_uid(proposals, "o-fri")
    assert openers.action == "drop"
    assert openers.proposed_date is None


def test_openers_with_no_race_are_just_an_easy_ride():
    """The openers rule is about a race. With no race on the calendar it is a
    20-minute spin like any other and must not be dropped over its name."""
    week = [{"uid": "o-fri", "date": FRI, "title": "Openers",
             "description": "20min easy with 3x1min primers"}]
    proposals = proposals_for(week, unavailable_days={FRI: "work"})
    openers = by_uid(proposals, "o-fri")
    assert openers.action == "move"
    assert openers.proposed_date is not None


# ---------------------------------------------------------------------------
# §2.4 rung 7 — THE "NO GOOD SLOT" CASE
# ---------------------------------------------------------------------------


def packed_week():
    """Every other day already holds a session, the neighbours are hard, and
    no endurance ride is adjacent to fold the intervals into."""
    return [
        {"uid": "p-mon", "date": MON, "title": "Threshold 2x20min", "description": "2x20 FTP"},
        {"uid": "p-tue", "date": TUE, "title": "VO2 max 5x5", "description": "5x5min VO2"},
        {"uid": "p-wed", "date": WED, "title": "Threshold 3x12min", "description": "3x12 FTP"},
        {"uid": "p-thu", "date": THU, "title": "Endurance 3hr Z2", "description": ""},
        {"uid": "p-fri", "date": FRI, "title": "Endurance 2hr Z2", "description": ""},
        {"uid": "p-sat", "date": SAT, "title": "Endurance 4hr Z2", "description": ""},
        {"uid": "p-sun", "date": SUN, "title": "Endurance 2hr Z2", "description": ""},
    ]


def test_no_good_slot_yields_an_honest_no_proposal():
    """§2.4 rung 7 — an unsolvable week must be visible, not silent, and must
    never be resolved by quietly deleting the session."""
    plan = plan_for(packed_week(), unavailable_days={TUE: "work"})
    assert plan.proposals == [], "nothing should be presented as solved"
    assert len(plan.unresolved) == 1
    stuck = plan.unresolved[0]
    assert stuck.kind == "NO_PROPOSAL"
    assert stuck.action == "keep"
    assert stuck.proposed_date is None
    assert stuck.operations == [], "NO_PROPOSAL must carry no operations"


def test_no_proposal_lists_every_rejected_day_with_a_reason():
    """The spec's own test: six candidate days, six populated alternatives."""
    plan = plan_for(packed_week(), unavailable_days={TUE: "work"})
    stuck = plan.unresolved[0]
    assert len(stuck.alternatives) == 6
    assert {a.date for a in stuck.alternatives} == {
        date(2026, 7, 27), date(2026, 7, 29), date(2026, 7, 30),
        date(2026, 7, 31), date(2026, 8, 1), date(2026, 8, 2)}
    for alt in stuck.alternatives:
        assert alt.reason.strip()
        assert alt.rule_id


def test_no_proposal_asks_the_user_rather_than_choosing_badly():
    plan = plan_for(packed_week(), unavailable_days={TUE: "work"})
    stuck = plan.unresolved[0]
    assert stuck.question
    assert "can't place" in stuck.reason.lower()


def test_unresolved_still_reaches_the_caller():
    """propose_adjustments returns the unsolvable ones too — the calendar has
    to render them as a flagged day."""
    proposals = proposals_for(packed_week(), unavailable_days={TUE: "work"})
    assert any(p.kind == "NO_PROPOSAL" for p in proposals)


# ---------------------------------------------------------------------------
# §4 GARMIN SIGNAL
# ---------------------------------------------------------------------------


def test_low_readiness_softens_todays_hard_session():
    signal = {"readiness": {"score": 41, "level": "LOW",
                            "timestamp": "2026-07-28T06:12:00"},
              "training_status": "PRODUCTIVE"}
    proposals = propose_adjustments(base_week(), garmin_signal=signal,
                                    now=datetime(2026, 7, 28, 7, 0),
                                    today=date(2026, 7, 28))
    softened = by_uid(proposals, "w-tue")
    assert softened.action == "soften"
    assert softened.new_intensity == Intensity.TEMPO
    assert "41" in softened.reason
    assert softened.signals_used


def test_high_readiness_never_proposes_going_harder():
    """§4.1 — the asymmetry rule. Upward adjustment is the athlete's call."""
    signal = {"readiness": {"score": 88, "level": "HIGH",
                            "timestamp": "2026-07-28T06:12:00"},
              "training_status": "PRODUCTIVE"}
    plan = build_adjustment_plan(base_week(), garmin_signal=signal,
                                 now=datetime(2026, 7, 28, 7, 0),
                                 today=date(2026, 7, 28))
    assert plan.all_proposals == []
    assert any("fresh" in w for w in plan.warnings)


def test_stale_readiness_is_ignored():
    """§4.2 — never act on a readiness score from two days ago."""
    signal = {"readiness": {"score": 20, "level": "POOR",
                            "timestamp": "2026-07-25T06:00:00"}}
    plan = build_adjustment_plan(base_week(), garmin_signal=signal,
                                 now=datetime(2026, 7, 28, 7, 0),
                                 today=date(2026, 7, 28))
    assert plan.all_proposals == []
    assert any("stale" in w.lower() for w in plan.warnings)


def test_readiness_does_not_reach_beyond_tomorrow():
    """§4.3 — readiness is a today-signal; Saturday's VO2 is out of horizon."""
    signal = {"readiness": {"score": 41, "level": "LOW",
                            "timestamp": "2026-07-27T06:12:00"}}
    proposals = proposals_for(base_week(), garmin_signal=signal)
    assert by_uid(proposals, "w-sat") is None


@pytest.mark.parametrize("score,level", [(30, "LOW"), (15, "POOR")])
def test_unknown_intensity_is_never_downgraded(score, level):
    """§4.6 — the engine can't downgrade what it can't classify. Poor
    readiness would otherwise rewrite it to a 45-minute recovery ride on the
    strength of a guess about what the session even was."""
    week = [{"uid": "u-1", "date": TUE, "title": "Ride 60min", "description": ""}]
    signal = {"readiness": {"score": score, "level": level,
                            "timestamp": "2026-07-28T06:12:00"}}
    proposals = propose_adjustments(week, garmin_signal=signal,
                                    now=datetime(2026, 7, 28, 7, 0),
                                    today=date(2026, 7, 28))
    assert proposals == []


def test_unknown_workout_carries_a_question_when_displaced():
    """§1.5(4) — shown, but marked 'needs one answer from you first'."""
    week = [
        {"uid": "u-tue", "date": TUE, "title": "Ride 60min", "description": ""},
        {"uid": "u-wed", "date": WED, "title": "VO2 max 5x5", "description": "5x5min VO2"},
    ]
    proposals = proposals_for(week, unavailable_days={TUE: "work"})
    unknown = by_uid(proposals, "u-tue")
    assert unknown.question
    assert "how hard" in unknown.question


# ---------------------------------------------------------------------------
# §6 SAFETY
# ---------------------------------------------------------------------------


def test_proposals_always_require_approval():
    proposals = proposals_for(base_week(), unavailable_days={TUE: "work"})
    assert proposals
    for proposal in proposals:
        assert proposal.requires_approval is True


def test_requires_approval_cannot_be_switched_off():
    """§6.2 — structurally enforced, not a convention."""
    proposals = proposals_for(base_week(), unavailable_days={TUE: "work"})
    with pytest.raises(ProposalError):
        proposals[0].requires_approval = False


def test_a_proposal_without_a_reason_is_a_bug():
    """§5.1 — enforced in __post_init__."""
    with pytest.raises(ProposalError):
        Proposal(workout_uid="x", original_date=date(2026, 7, 28),
                 proposed_date=date(2026, 7, 29), action="move", reason="   ",
                 confidence=Confidence.HIGH)


def test_every_proposal_has_a_reason_and_an_inverse():
    """Reasons are mandatory (§5.1); operations record their inverse so the
    apply layer can undo the whole plan (§6.3)."""
    for unavailable in ({TUE: "work"}, {THU: "travel"}, {MON: "family"}):
        for proposal in proposals_for(base_week(), unavailable_days=unavailable):
            assert proposal.reason.strip()
            for operation in proposal.operations:
                assert operation.inverse is not None


def test_engine_is_deterministic():
    """§6.4 — same inputs, same proposal ids."""
    first = proposals_for(base_week(), unavailable_days={TUE: "work"})
    second = proposals_for(base_week(), unavailable_days={TUE: "work"})
    assert [p.id for p in first] == [p.id for p in second]
    assert all(p.id.startswith("prp_") for p in first)


def test_engine_imports_no_clients():
    """§6.1 — structurally pure: no network, no credentials, no I/O."""
    import scheduler

    source = open(scheduler.__file__, encoding="utf-8").read()
    for forbidden in ("import requests", "import garminconnect", "from garminconnect",
                      "import garmin_client", "import strava_client", "trainingpeaks.com"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# CONSTRAINTS AND EDGE CASES
# ---------------------------------------------------------------------------


def test_past_days_are_never_candidates():
    """H7 — and 'today' counts as past after the 18:00 cutoff."""
    proposals = propose_adjustments(
        base_week(), unavailable_days={THU: "work"},
        now=datetime(2026, 7, 30, 19, 0), today=date(2026, 7, 30))
    moved = by_uid(proposals, "w-thu")
    assert moved is not None and moved.proposed_date is not None
    # It is Thursday evening: Monday-Thursday are all in the past.
    assert moved.proposed_date > date(2026, 7, 30)
    assert all(a.date > date(2026, 7, 30) or a.rule_id == "H7"
               for a in moved.alternatives)


def test_unavailable_days_are_never_candidates():
    """H6 — including a second day marked off in the same request."""
    proposals = proposals_for(base_week(), unavailable_days={TUE: "work", WED: "work"})
    for proposal in proposals:
        assert proposal.proposed_date not in (date(2026, 7, 28), date(2026, 7, 29))


def test_multiple_displaced_workouts_resolve_hardest_first():
    """§2.3 — the important session gets first pick of the week."""
    week = [
        {"uid": "m-a", "date": TUE, "title": "VO2 max 5x5", "description": "5x5min VO2"},
        {"uid": "m-b", "date": TUE, "title": "Recovery spin 45min", "description": "Z1"},
        {"uid": "m-c", "date": THU, "title": "Endurance 2hr Z2", "description": ""},
    ]
    plan = plan_for(week, unavailable_days={TUE: "work"},
                    config=EngineConfig(allow_doubles=False))
    order = [p.workout_uid for p in plan.all_proposals]
    assert order.index("m-a") < order.index("m-b")


DOUBLE_DAY = [
    {"uid": "s-a", "date": TUE, "title": "VO2 max 5x5", "description": "5x5min VO2"},
    {"uid": "s-b", "date": TUE, "title": "Endurance 2hr Z2", "description": ""},
    {"uid": "s-c", "date": SAT, "title": "Endurance 3hr Z2", "description": ""},
]


def test_two_workouts_never_stack_on_one_day_by_default():
    """H8 with MAX_SESSIONS_PER_DAY=1 — committing between picks is what keeps
    this true. Note the second session cannot be re-placed at all here: two
    sessions leaving one day and landing on two empty days costs a rest day,
    which H2 forbids. The engine drops it *out loud* rather than silently."""
    plan = plan_for(DOUBLE_DAY, unavailable_days={TUE: "work"})
    targets = [p.proposed_date for p in plan.all_proposals if p.proposed_date]
    assert len(targets) == len(set(targets))
    assert len(plan.all_proposals) == 2, "both displaced sessions must be accounted for"
    dropped = by_uid(plan.all_proposals, "s-b")
    assert dropped.action == "drop"
    assert dropped.reason.strip() and "skip" in dropped.reason.lower()


def test_doubles_config_lets_the_second_session_share_a_day():
    """The same week with ALLOW_DOUBLES — nothing is dropped."""
    plan = plan_for(DOUBLE_DAY, unavailable_days={TUE: "work"},
                    config=EngineConfig(allow_doubles=True))
    assert all(p.action == "move" for p in plan.all_proposals)
    assert all(p.proposed_date is not None for p in plan.all_proposals)


def test_candidates_stay_inside_the_target_week_by_default():
    """§2.0 — spilling into next week distorts two weeks, so it is opt-in.

    Sunday's ride has nowhere to go in a full week: by default it is dropped,
    and only with ALLOW_SPILL_NEXT_WEEK may it slide to Monday.
    """
    plan = plan_for(packed_week(), unavailable_days={SUN: "work"})
    contained = by_uid(plan.all_proposals, "p-sun")
    assert contained.proposed_date is None
    assert all(a.date <= date(2026, 8, 2) for a in contained.alternatives)

    spilled = plan_for(packed_week(), unavailable_days={SUN: "work"},
                       config=EngineConfig(allow_spill_next_week=True))
    moved = by_uid(spilled.all_proposals, "p-sun")
    assert moved.proposed_date == date(2026, 8, 3), "should reach next Monday"


def test_availability_alone_produces_no_proposals():
    """Marking a free day off has nothing to move."""
    assert proposals_for(base_week(), unavailable_days={FRI: "work"}) == []


def test_accepts_the_raw_calendar_feed_shape():
    """The feed hands back ISO datetimes for timed events and plain dates for
    all-day ones; both must work without pre-processing."""
    week = [
        {"uid": "f-1", "date": "2026-07-28T06:00:00+00:00", "title": "Threshold 3x12min",
         "description": "3x12 FTP"},
        {"uid": "f-2", "date": "2026-07-29", "title": "Rest day", "description": ""},
    ]
    proposals = proposals_for(week, unavailable_days={TUE: "work"})
    assert by_uid(proposals, "f-1").original_date == date(2026, 7, 28)


def test_empty_inputs_do_not_crash():
    assert propose_adjustments([], now=NOW, today=TODAY) == []
    assert propose_adjustments(base_week(), now=NOW, today=TODAY) == []


def test_week_summaries_are_reported():
    plan = plan_for(base_week(), unavailable_days={TUE: "work"})
    assert plan.week_summary_before.hard_days == 2  # Tuesday threshold + Saturday VO2
    assert plan.week_summary_after.rest_days >= plan.week_summary_before.rest_days
    assert plan.trigger.startswith("user marked Tuesday")


def test_plan_serialises_for_the_web_layer():
    plan = plan_for(base_week(), unavailable_days={TUE: "work"})
    payload = plan.to_dict()
    assert payload["proposals"][0]["action"] == "move"
    assert payload["proposals"][0]["requires_approval"] is True
    assert payload["week_start"] == "2026-07-27"


# ---------------------------------------------------------------------------
# REGRESSIONS — THE OVERRIDE STORE AND THE GARMIN WRITER
#
# These cover the two modules either side of the engine: the local override
# store and the only write path to Garmin Connect. Nothing here touches the
# network — the writer is driven by a fake Garmin API that keeps a small model
# of the calendar, and every store lives in a pytest tmp_path.
# ---------------------------------------------------------------------------

import garmin_workout_writer as writer_module  # noqa: E402
import plan_store as store_module  # noqa: E402
from plan_store import PlanStore  # noqa: E402


class FakeGarminApi:
    """Stands in for `garminconnect.Garmin`.

    Keeps a tiny model of the Garmin calendar so the writer's create /
    reschedule / adopt paths can be exercised for real: scheduling adds a row,
    unscheduling removes it, and a duplicate shows up as two rows on one day —
    which is exactly what the Edge 520 would display.

    Two switches reproduce Garmin's flakier days:
    `schedule_returns_id=False` answers a schedule call without the id, and
    `expose_schedule_ids=False` also hides it from the calendar listing, so
    the writer's `find_schedule_id` recovery genuinely misses.
    """

    def __init__(self, first_workout_id=999):
        self.calendar = []          # rows the Edge would sync
        self.templates = {}
        self.calls = []
        self.schedule_returns_id = True
        self.expose_schedule_ids = True
        self.fail_schedule = False
        self._next_workout_id = first_workout_id
        self._next_schedule_id = 5000

    def upload_workout(self, payload):
        workout_id = self._next_workout_id
        self._next_workout_id += 1
        self.templates[workout_id] = payload
        self.calls.append(("upload", workout_id))
        return {"workoutId": workout_id}

    def schedule_workout(self, workout_id, date):
        if self.fail_schedule:
            raise RuntimeError("Garmin is having a moment")
        schedule_id = self._next_schedule_id
        self._next_schedule_id += 1
        self.calls.append(("schedule", workout_id, date))
        self.calendar.append({
            "workoutId": workout_id,
            "workoutScheduleId": schedule_id,
            "date": date,
            "title": (self.templates.get(workout_id) or {}).get("workoutName"),
        })
        if not self.schedule_returns_id:
            return {"ok": True}
        return {"workoutScheduleId": schedule_id}

    def unschedule_workout(self, schedule_id):
        self.calls.append(("unschedule", schedule_id))
        row = next((r for r in self.calendar
                    if r["workoutScheduleId"] == schedule_id), None)
        if row is None:
            raise RuntimeError(f"no scheduled workout {schedule_id}")
        self.calendar.remove(row)
        return {"ok": True}

    def delete_workout(self, workout_id):
        self.calls.append(("delete", workout_id))
        self.templates.pop(workout_id, None)
        return {"ok": True}

    def get_scheduled_workouts(self, year, month):
        prefix = f"{int(year):04d}-{int(month):02d}"
        items = []
        for row in self.calendar:
            if not row["date"].startswith(prefix):
                continue
            row = dict(row)
            if not self.expose_schedule_ids:
                row.pop("workoutScheduleId")
            items.append(row)
        return {"calendarItems": items}

    def days(self):
        """The dates the head unit would show a session on, with repeats."""
        return sorted(row["date"] for row in self.calendar)


def fresh_store(tmp_path, name="state.json"):
    return PlanStore(path=str(tmp_path / name))


def writer_for(store, api=None):
    api = api or FakeGarminApi()
    writer = writer_module.GarminWorkoutWriter(client=api, store=store)
    return writer, api


def push_threshold(writer, store, date, uid="u1", duration_s=3600):
    """The push the UI makes for a feed entry with no structure."""
    return writer.push_workout(uid, "Threshold 3x12min", date,
                               duration_s=duration_s, store=store)


# --- DEFECT 1: clear_override must not destroy the Garmin identity ---------


def test_clear_override_keeps_the_garmin_push_identity(tmp_path):
    """"Revert to the coach's plan" drops the user's edit, not the record of
    what is already on the device.

    The push ids are not part of the override — they are the app's only handle
    on the copy sitting on the Garmin calendar. Dropping them makes the next
    push take the `create` path and stack a second session on the Edge 520
    that nothing in the app can remove.
    """
    store = fresh_store(tmp_path)
    store.move_workout("u1", SAT)
    store.record_push("u1", workout_id=111, schedule_id=222,
                      scheduled_for=SAT, pushed_hash="h1")

    store.clear_override("u1")

    assert store.get_override("u1")["fields"] == {}, "the user's edit must go"
    push = store.get_push("u1")
    assert push["workout_id"] == 111
    assert push["schedule_id"] == 222
    assert push["scheduled_for"] == SAT
    assert push["pushed_hash"] == "h1"
    assert "u1" in store.pushed_uids(), \
        "the push flow must still know this workout is ours"


def test_clear_override_drops_a_record_that_was_never_pushed(tmp_path):
    """With nothing on Garmin there is nothing to remember: the record goes."""
    store = fresh_store(tmp_path)
    store.move_workout("u1", SAT)
    assert store.clear_override("u1")["fields"]["date"] == SAT
    assert store.get_override("u1") is None
    assert store.clear_override("u1") is None, "still idempotent"


def test_repush_after_a_revert_does_not_duplicate_on_the_calendar(tmp_path):
    """The Edge-520 symptom, end to end: push, revert, push again.

    The second push must recognise the workout as already scheduled and leave
    the single existing copy alone, rather than uploading a second one beside
    it.
    """
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)

    first = push_threshold(writer, store, SAT)
    assert first.action == "create" and first.state == "done"

    store.clear_override("u1")

    second = push_threshold(writer, store, SAT)
    assert second.action == "noop"
    assert second.workout_id == first.workout_id
    assert api.days() == [SAT], "the session must appear on the Edge once"


def test_revert_then_move_reschedules_the_existing_copy(tmp_path):
    """The same, with a new date: reschedule the template, never re-create."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)

    first = push_threshold(writer, store, SAT)
    store.clear_override("u1")
    store.move_workout("u1", SUN)

    second = push_threshold(writer, store, SUN)
    assert second.action == "reschedule"
    assert second.workout_id == first.workout_id
    assert api.days() == [SUN]
    assert [c for c in api.calls if c[0] == "upload"] == [("upload", 999)]


# --- DEFECT 1b: the adoption guard on the create path ----------------------


def test_create_adopts_a_workout_already_on_that_day(tmp_path):
    """Belt and braces for a store that lost its ids anyway.

    A quarantined state file leaves the app with no memory of the push while
    the workout is still on the Garmin calendar. Uploading again would put a
    second copy on the day, and unpush could only ever remove one of them.
    """
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    first = push_threshold(writer, store, SAT)

    amnesiac = fresh_store(tmp_path, "lost.json")
    again = push_threshold(writer, amnesiac, SAT)

    assert api.days() == [SAT], "no second copy may reach the calendar"
    assert [c for c in api.calls if c[0] == "upload"] == [("upload", 999)]
    assert again.state == "done"
    assert again.workout_id == first.workout_id
    assert again.schedule_id == first.schedule_id
    assert amnesiac.get_push("u1")["workout_id"] == first.workout_id
    assert "already on your Garmin calendar" in again.reason


def test_create_still_uploads_when_the_day_holds_a_different_session(tmp_path):
    """The guard is not a blanket "something is on that day" veto — a
    different workout on the same day is not this one."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    writer.push_workout("u-other", "Endurance 2hr Z2", SAT, duration_s=7200,
                        store=store)

    result = push_threshold(writer, store, SAT)
    assert result.action == "create" and result.state == "done"
    assert result.workout_id != store.get_push("u-other")["workout_id"]
    assert api.days() == [SAT, SAT]


# --- DEFECT 4: undo() must honour its own supersession guard ---------------


def test_a_superseded_history_entry_cannot_be_undone(tmp_path):
    """Only the newest change to a workout may be undone.

    `history()` says so, but it said it on a copy — the stored flag stayed
    True, so `undo` accepted the older entry, and because its `before` was
    None it deleted the whole override: the newer move and the Garmin ids
    with it.
    """
    store = fresh_store(tmp_path)
    store.move_workout("u1", THU)
    store.move_workout("u1", FRI)
    store.record_push("u1", workout_id=111, schedule_id=222,
                      scheduled_for=FRI, pushed_hash="h1")

    entries = store.history(uid="u1")
    superseded = next(e for e in entries
                      if e["action"] == "move" and not e["undoable"])

    with pytest.raises(store_module.PlanStoreError):
        store.undo(superseded["entry_id"])

    assert store.get_override("u1")["fields"]["date"] == FRI
    assert store.get_push("u1")["workout_id"] == 111


def test_the_newest_change_is_still_undoable(tmp_path):
    """The guard rejects superseded entries only — Undo still works."""
    store = fresh_store(tmp_path)
    store.move_workout("u1", THU)
    store.move_workout("u1", FRI)

    newest = next(e for e in store.history(uid="u1") if e["undoable"])
    restored = store.undo(newest["entry_id"])
    assert restored["fields"]["date"] == THU

    # And after undoing it, the older move is the newest surviving change.
    again = next(e for e in store.history(uid="u1") if e["undoable"])
    assert again["action"] == "move"


def test_undoing_a_first_edit_keeps_the_garmin_identity(tmp_path):
    """The Undo button on a move toast, on a workout pushed since.

    The entry's `before` is None because the move created the override, so the
    undo removes it — but the push ids are not the user's edit and must
    survive, for the same reason "revert to the coach's plan" leaves them.
    """
    store = fresh_store(tmp_path)
    store.move_workout("u1", SAT)
    store.record_push("u1", workout_id=111, schedule_id=222,
                      scheduled_for=SAT, pushed_hash="h1")

    entry = next(e for e in store.history(uid="u1") if e["undoable"])
    store.undo(entry["entry_id"])

    assert store.get_override("u1")["fields"] == {}
    assert store.get_push("u1")["workout_id"] == 111
    assert "u1" in store.pushed_uids()


# --- DEFECT 5: a schedule id known to be dead must be cleared --------------


def test_record_push_clears_an_id_only_when_asked(tmp_path):
    """Omitting an id leaves it alone; passing None clears it — the same
    patch semantics `patch_workout` already uses."""
    store = fresh_store(tmp_path)
    store.record_push("u1", workout_id=111, schedule_id=555,
                      scheduled_for=SAT, pushed_hash="h1")

    store.mark_push_failed("u1", "Garmin said no")
    assert store.get_push("u1")["schedule_id"] == 555, "omitted stays put"

    store.record_push("u1", schedule_id=None, push_state="pending")
    push = store.get_push("u1")
    assert push["schedule_id"] is None
    assert push["workout_id"] == 111, "clearing one id leaves the other"
    assert push["scheduled_for"] == SAT


def test_a_reschedule_never_keeps_a_schedule_id_it_has_unscheduled(tmp_path):
    """Garmin can accept a schedule call and answer without the new id.

    The old id was unscheduled moments earlier, so keeping it means the next
    move calls unschedule on an id that no longer exists, Garmin refuses, and
    the workout is stuck on the wrong day with no way back through the UI.
    """
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    assert store.get_push("u1")["schedule_id"] == 5000

    api.schedule_returns_id = False
    api.expose_schedule_ids = False
    moved = push_threshold(writer, store, SUN)

    assert moved.action == "reschedule" and moved.state == "done"
    push = store.get_push("u1")
    assert push["schedule_id"] is None, "a dead id must not be kept"
    assert push["scheduled_for"] == SUN
    assert push["workout_id"] == 999

    # Recoverable: the next move works instead of raising on the dead id.
    api.schedule_returns_id = True
    api.expose_schedule_ids = True
    again = push_threshold(writer, store, MON)
    assert again.state == "done"
    assert api.days() == [MON], "no copy left behind on the old day"


def test_a_failed_reschedule_leaves_a_recoverable_state(tmp_path):
    """The unschedule succeeded and the re-schedule then failed. The stored
    id is gone from Garmin either way, so it must not be left behind to
    strand the workout on the next attempt."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)

    api.fail_schedule = True
    failed = push_threshold(writer, store, SUN)
    assert failed.state == "failed"
    assert store.get_push("u1")["schedule_id"] is None

    api.fail_schedule = False
    retry = push_threshold(writer, store, SUN)
    assert retry.state == "done"
    assert api.days() == [SUN]
