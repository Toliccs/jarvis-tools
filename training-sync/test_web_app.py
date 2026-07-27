"""Regression tests for the calendar backend (`web_app.py`).

Every test here pins a defect that was reproduced against the running app, so
each one names the behaviour it protects rather than the function it calls.

Nothing in this file touches the network. The app runs in `--demo` mode, which
never contacts TrainingPeaks or Garmin, and each test gets its own state
directory so an approval in one can't leak into another.
"""

import datetime as dt
import os
import sys
import time
from datetime import timedelta

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import web_app  # noqa: E402

try:
    # Starlette raises RuntimeError (not ImportError) when httpx is missing,
    # so this can't be an importorskip.
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - dev dependency only
    TestClient = None


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """A private state dir per test, and no inherited timezone."""
    monkeypatch.setattr(web_app, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(web_app, "DEMO_STATE_PATH",
                        str(tmp_path / "demo_state.json"))
    monkeypatch.setattr(web_app, "PUSH_LOG_PATH", str(tmp_path / "push.json"))
    monkeypatch.setattr(web_app, "PLAN_CACHE_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("TRAINING_TZ", "Europe/London")
    # Writing to a real Garmin account is opt-in. No test may inherit it from
    # the machine it happens to run on.
    monkeypatch.delenv(web_app.ALLOW_GARMIN_WRITES_ENV, raising=False)
    web_app._TZ_CACHE.clear()
    yield
    web_app._TZ_CACHE.clear()


@pytest.fixture
def client():
    if TestClient is None:
        pytest.skip("the HTTP tests need httpx installed (pip install httpx)")
    with TestClient(web_app.create_app(demo=True)) as test_client:
        yield test_client


def monday():
    return web_app._demo_monday()


def week2(offset=0):
    """A day in the demo plan's *next* week — far enough ahead that nothing
    trips the "that day has already gone" rule."""
    return (monday() + timedelta(days=7 + offset)).isoformat()


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def block(client, day, note="working"):
    return ok(client.put(f"/api/days/{day}/availability",
                         json={"state": "unavailable", "note": note}))


def propose(client, start, end=None, reason="availability_changed"):
    return ok(client.post("/api/proposals", json={
        "start": start, "end": end or start, "reason": reason}))


def calendar(client, start, end):
    return ok(client.get("/api/calendar",
                         params={"start": start, "end": end}))


def workouts_by_day(payload):
    return {day["date"]: [w["title"] for w in day["workouts"]]
            for day in payload["days"]}


def double_booked(payload):
    return {day["date"] for day in payload["days"]
            for w in day["workouts"] if "double_booked" in w["conflicts"]}


def find(proposals, engine_kind):
    for proposal in proposals:
        if proposal.get("engine_kind") == engine_kind:
            return proposal
    return None


def workout(client, uid):
    return ok(client.get(f"/api/workouts/{uid}"))


# ---------------------------------------------------------------------------
# defect 2 — a proposal is a set of operations, not one date change
# ---------------------------------------------------------------------------


def test_approving_a_swap_moves_both_sessions_and_leaves_no_day_double_booked(
        client):
    """The whole point of a SWAP is to clear a day. Applying only the
    proposal's own workout moved one session onto the victim's day and left
    the victim there — two sessions on one day, which is the H8 violation the
    swap was computed to avoid."""
    tuesday = week2(1)
    block(client, tuesday, "work trip")
    payload = propose(client, week2(0), week2(6))

    swap = find(payload["proposals"], "SWAP")
    assert swap is not None, "demo week 2 with Tuesday blocked should swap"
    assert len(swap["operations"]) == 2, swap["operations"]
    assert {op["uid"] for op in swap["operations"]} == set(swap["affects_uids"])

    ok(client.post(f"/api/proposals/{swap['proposal_id']}/approve", json={}))

    after = calendar(client, week2(0), week2(6))
    assert double_booked(after) == set()
    by_day = workouts_by_day(after)
    assert by_day[tuesday] == [], by_day
    # Both halves landed: the swapped-in session on the vacated day, and the
    # victim on the day the engine found for it.
    victim = next(op for op in swap["operations"] if op["uid"] != swap["uid"])
    assert workout(client, swap["uid"])["date"] == swap["to_date"]
    assert workout(client, victim["uid"])["date"] == victim["to_date"]


def test_a_swap_that_would_half_apply_applies_nothing(client):
    """Two sessions move or neither does. A conflict on the second operation
    must not leave the first one written."""
    block(client, week2(1), "work trip")
    swap = find(propose(client, week2(0), week2(6))["proposals"], "SWAP")
    assert swap is not None
    victim = next(op for op in swap["operations"] if op["uid"] != swap["uid"])

    # The athlete moves the victim somewhere else by hand first.
    ok(client.patch(f"/api/workouts/{victim['uid']}",
                    json={"date": week2(5)}))
    before = workout(client, swap["uid"])["date"]

    refused = client.post(f"/api/proposals/{swap['proposal_id']}/approve",
                          json={})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "proposal_conflict"

    assert workout(client, swap["uid"])["date"] == before
    assert workout(client, victim["uid"])["date"] == week2(5)
    stored = ok(client.get("/api/proposals", params={"status": "pending"}))
    assert any(p["proposal_id"] == swap["proposal_id"]
               for p in stored["proposals"])


def test_forcing_a_conflicted_swap_applies_every_operation(client):
    block(client, week2(1), "work trip")
    swap = find(propose(client, week2(0), week2(6))["proposals"], "SWAP")
    victim = next(op for op in swap["operations"] if op["uid"] != swap["uid"])
    ok(client.patch(f"/api/workouts/{victim['uid']}", json={"date": week2(5)}))

    ok(client.post(f"/api/proposals/{swap['proposal_id']}/approve",
                   json={"force": True}))
    assert workout(client, victim["uid"])["date"] == victim["to_date"]
    assert workout(client, swap["uid"])["date"] == swap["to_date"]
    assert double_booked(calendar(client, week2(0), week2(6))) == set()


def test_approving_a_merge_folds_the_session_instead_of_stacking_it(client):
    """MERGE annotates the host and leaves the folded session off the week.
    Moving the workout onto the host's day instead produced exactly the
    double-booked day the merge was avoiding."""
    block(client, week2(1), "work trip")
    block(client, week2(4), "away")
    merge = find(propose(client, week2(0), week2(6))["proposals"], "MERGE")
    assert merge is not None, "Tuesday+Friday blocked should fold Tuesday in"
    assert merge["kind"] == "merge"

    ok(client.post(f"/api/proposals/{merge['proposal_id']}/approve", json={}))

    after = calendar(client, week2(0), week2(6))
    assert double_booked(after) == set()
    folded = workout(client, merge["uid"])
    assert folded["status"] == "skipped"
    assert web_app.ALFRED_MARK in (folded["description"] or "")
    host_uid = next(op["uid"] for op in merge["operations"]
                    if op["uid"] != merge["uid"])
    host = workout(client, host_uid)
    assert web_app.ALFRED_MARK in (host["description"] or "")


def test_approving_a_downgrade_changes_the_session_not_just_its_date(client):
    """"The tempo version instead of threshold" has to survive approval. It
    used to be dropped entirely, leaving the full session moved onto the day
    the downgrade existed to protect."""
    block(client, week2(6), "family")
    downgrade = find(propose(client, week2(0), week2(6))["proposals"],
                     "DOWNGRADE")
    assert downgrade is not None
    assert downgrade["kind"] == "downgrade"
    assert downgrade["action"] == "soften"
    assert downgrade["changes"].get("title")
    assert "version" in downgrade["changes"]["title"]

    ok(client.post(f"/api/proposals/{downgrade['proposal_id']}/approve",
                   json={}))
    after = workout(client, downgrade["uid"])
    assert "version" in after["title"].lower()
    assert web_app.ALFRED_MARK in (after["description"] or "")
    assert after["date"] == downgrade["to_date"]


def test_a_failure_part_way_through_rolls_the_earlier_operations_back(
        client, monkeypatch):
    """The victim's move is written before the store marks the proposal
    approved. If that second write fails, the first must be undone — a
    half-applied swap leaves a session stranded on the wrong day."""
    block(client, week2(1), "work trip")
    swap = find(propose(client, week2(0), week2(6))["proposals"], "SWAP")
    victim = next(op for op in swap["operations"] if op["uid"] != swap["uid"])
    before = workout(client, victim["uid"])

    store = client.app.state.service.store

    def boom(*args, **kwargs):
        raise RuntimeError("store went away mid-approval")

    monkeypatch.setattr(store, "approve_proposal", boom)
    failed = client.post(f"/api/proposals/{swap['proposal_id']}/approve",
                         json={})
    assert failed.status_code >= 400

    after = workout(client, victim["uid"])
    assert after["date"] == before["date"]
    assert after["override"] is None
    assert workout(client, swap["uid"])["date"] == swap["from_date"]
    pending = ok(client.get("/api/proposals", params={"status": "pending"}))
    assert any(p["proposal_id"] == swap["proposal_id"]
               for p in pending["proposals"])


def test_approving_the_same_swap_twice_moves_nothing_twice(client):
    """A double-click, or an "approve all" over a list the user already
    accepted, must not move the swapped session a second time."""
    block(client, week2(1), "work trip")
    swap = find(propose(client, week2(0), week2(6))["proposals"], "SWAP")
    victim = next(op for op in swap["operations"] if op["uid"] != swap["uid"])

    ok(client.post(f"/api/proposals/{swap['proposal_id']}/approve", json={}))
    revs = {uid: workout(client, uid)["rev"] for uid in swap["affects_uids"]}
    ok(client.post(f"/api/proposals/{swap['proposal_id']}/approve", json={}))

    assert {uid: workout(client, uid)["rev"]
            for uid in swap["affects_uids"]} == revs
    assert workout(client, victim["uid"])["date"] == victim["to_date"]
    assert double_booked(calendar(client, week2(0), week2(6))) == set()


def test_a_rejected_proposal_cannot_then_be_approved(client):
    block(client, week2(1), "work trip")
    swap = find(propose(client, week2(0), week2(6))["proposals"], "SWAP")
    victim = next(op for op in swap["operations"] if op["uid"] != swap["uid"])
    before = workout(client, victim["uid"])["date"]

    ok(client.post(f"/api/proposals/{swap['proposal_id']}/reject",
                   json={"reason": "no"}))
    refused = client.post(f"/api/proposals/{swap['proposal_id']}/approve",
                          json={})
    assert refused.status_code == 409
    assert workout(client, victim["uid"])["date"] == before


def test_approving_a_drop_skips_the_session(client):
    block(client, week2(5), "wedding")
    drop = find(propose(client, week2(0), week2(6))["proposals"], "DROP")
    assert drop is not None
    assert drop["kind"] == "skip" and drop["action"] == "drop"
    ok(client.post(f"/api/proposals/{drop['proposal_id']}/approve", json={}))
    assert workout(client, drop["uid"])["status"] == "skipped"


# ---------------------------------------------------------------------------
# defect 3 — regenerating must only supersede the week it re-planned
# ---------------------------------------------------------------------------


def test_month_view_regeneration_keeps_undecided_suggestions_on_other_weeks(
        client):
    """The engine only ever plans the Monday-anchored week containing `start`.
    Superseding the month grid's whole 5-6 week range deleted pending
    suggestions it never reconsidered."""
    this_week = monday().isoformat()
    block(client, (monday() + timedelta(days=1)).isoformat(), "this week")
    block(client, week2(1), "next week")

    first = propose(client, this_week, (monday() + timedelta(days=6)).isoformat())
    second = propose(client, week2(0), week2(6))
    assert first["proposals"] and second["proposals"]
    kept = {p["proposal_id"] for p in second["proposals"]}

    # What the month view sends: a whole 6-week grid.
    grid_end = (monday() + timedelta(days=20)).isoformat()
    again = propose(client, this_week, grid_end)
    assert again["replanned"] == {"start": this_week,
                                  "end": (monday() + timedelta(days=6)).isoformat()}

    pending = ok(client.get("/api/proposals", params={"status": "pending"}))
    still_there = {p["proposal_id"] for p in pending["proposals"]}
    assert kept <= still_there, "next week's undecided suggestion vanished"


def test_regeneration_still_supersedes_its_own_week(client):
    block(client, week2(1), "work trip")
    first = propose(client, week2(0), week2(6))
    ids = {p["proposal_id"] for p in first["proposals"]}
    propose(client, week2(0), week2(6))
    pending = ok(client.get("/api/proposals", params={"status": "pending"}))
    seen = [p["proposal_id"] for p in pending["proposals"]]
    assert len(seen) == len(set(seen)), "regenerating duplicated a proposal"
    assert ids <= set(seen)


def test_a_failed_engine_run_never_deletes_pending_suggestions(client,
                                                               monkeypatch):
    block(client, week2(1), "work trip")
    before = propose(client, week2(0), week2(6))["proposals"]
    assert before

    module, _ = web_app.load_module("scheduler")

    def explode(*args, **kwargs):
        raise RuntimeError("engine mid-edit")

    monkeypatch.setattr(module, "build_adjustment_plan", explode)
    again = propose(client, week2(0), week2(6))
    assert again["replanned"] is None
    assert again["warnings"]

    pending = ok(client.get("/api/proposals", params={"status": "pending"}))
    assert {p["proposal_id"] for p in before} <= {
        p["proposal_id"] for p in pending["proposals"]}


# ---------------------------------------------------------------------------
# defect 6 — KEEP is not SKIP
# ---------------------------------------------------------------------------


RACE_DEMO = list(web_app.DEMO_WORKOUTS)
RACE_DEMO[5] = (5, "Regional road race",
                "Road race, 120km. Sign on 08:30. Planned TSS: 190")


def test_keep_is_labelled_keep_and_approving_it_writes_nothing(client,
                                                              monkeypatch):
    """A race on a day you've blocked out gets a KEEP: "left where it is".
    Drawing that as SKIP told the athlete the opposite of the sentence under
    it, and approving it left an empty override behind."""
    monkeypatch.setattr(web_app, "DEMO_WORKOUTS", RACE_DEMO)
    race_day = week2(5)
    block(client, race_day, "family thing")

    keep = find(propose(client, week2(0), week2(6))["proposals"], "KEEP")
    assert keep is not None
    assert keep["kind"] == "keep", "a KEEP must not render as SKIP"
    assert keep["action"] == "keep"
    assert keep["changes"] == {} and keep["operations"] == []
    assert keep["to_date"] is None

    ok(client.post(f"/api/proposals/{keep['proposal_id']}/approve", json={}))

    after = workout(client, keep["uid"])
    assert after["date"] == race_day, "a KEEP must not move anything"
    assert after["override"] is None, "a KEEP must not leave a junk override"
    assert after["rev"] == 0
    decided = ok(client.get("/api/proposals", params={"status": "approved"}))
    assert any(p["proposal_id"] == keep["proposal_id"]
               for p in decided["proposals"])


def test_a_workout_only_garmin_holds_is_not_reported_as_an_edit(client):
    """Garmin holding a copy is not the athlete having edited anything.

    The store projects an override record for such a uid so the app can still
    offer to remove the copy, and flags it `push_only`. If that projection is
    passed through as an override, the UI offers "Revert to the coach's plan"
    on a session that is already exactly the coach's plan — and the revert it
    offers has nothing to revert.
    """
    day = week2(1)
    uid = calendar(client, day, day)["days"][0]["workouts"][0]["uid"]
    store = client.app.state.service.store
    store.record_push(uid, workout_id=987, schedule_id=654, scheduled_for=day,
                      push_state="scheduled")

    after = workout(client, uid)
    assert after["garmin"]["workout_id"] == 987, "the copy is still reported"
    assert after["override"] is None, \
        "a push-only record is Garmin bookkeeping, not one of the user's edits"
    assert after["rev"] == 0


def test_every_engine_kind_maps_to_its_own_ui_kind():
    module, _ = web_app.load_module("scheduler")
    assert set(web_app._KIND_MAP) == set(module.KIND_TO_ACTION)
    assert len(set(web_app._KIND_MAP.values())) == len(web_app._KIND_MAP)
    for engine_kind, action in module.KIND_TO_ACTION.items():
        assert web_app._ACTION_MAP[engine_kind] == action


# ---------------------------------------------------------------------------
# defect 7 — a zone, not the offset that happens to be in force today
# ---------------------------------------------------------------------------


def test_training_tz_is_a_named_zone(monkeypatch):
    monkeypatch.setenv("TRAINING_TZ", "Europe/London")
    web_app._TZ_CACHE.clear()
    assert getattr(web_app.training_tz(), "key", None) == "Europe/London"
    assert web_app.tz_name() == "Europe/London"


def test_tz_env_var_is_honoured_when_training_tz_is_unset(monkeypatch):
    monkeypatch.delenv("TRAINING_TZ", raising=False)
    monkeypatch.setenv("TZ", "Europe/London")
    web_app._TZ_CACHE.clear()
    tz = web_app.training_tz()
    assert getattr(tz, "key", None) == "Europe/London"
    # The bug: `datetime.now().astimezone().tzinfo` is a fixed offset, so it
    # answers the same for every date of the year.
    assert tz.utcoffset(dt.datetime(2026, 6, 20)) \
        != tz.utcoffset(dt.datetime(2026, 1, 20))


DST_CASES = [
    # Same clock time either side of a DST boundary. A captured offset gets
    # one of these wrong, and the workout renders on the wrong day.
    ("2026-06-20T23:30:00+00:00", ("2026-06-21", "00:30")),   # BST, UTC+1
    ("2026-01-20T23:30:00+00:00", ("2026-01-20", "23:30")),   # GMT, UTC+0
    ("2026-10-24T23:30:00+00:00", ("2026-10-25", "00:30")),   # last BST night
    ("2026-10-25T23:30:00+00:00", ("2026-10-25", "23:30")),   # first GMT night
]


@pytest.mark.parametrize("raw,expected", DST_CASES)
def test_timed_workouts_use_the_offset_in_force_on_their_own_date(
        monkeypatch, raw, expected):
    monkeypatch.setenv("TRAINING_TZ", "Europe/London")
    web_app._TZ_CACHE.clear()
    assert web_app.normalize_plan_date(raw) == expected


@pytest.mark.parametrize("raw,expected", DST_CASES)
def test_dst_is_respected_on_the_default_path_too(raw, expected):
    """`TRAINING_TZ` unset is the *default* configuration, and it was the
    broken one: the system zone was captured as today's offset and then
    applied to dates months away.

    `tzset()` here makes the machine's own clock London-local, so a captured
    offset would be today's real London offset — the exact failure reported.
    """
    previous = {key: os.environ.get(key) for key in ("TRAINING_TZ", "TZ")}
    os.environ.pop("TRAINING_TZ", None)
    os.environ["TZ"] = "Europe/London"
    if hasattr(time, "tzset"):
        time.tzset()
    web_app._TZ_CACHE.clear()
    try:
        assert web_app.normalize_plan_date(raw) == expected
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if hasattr(time, "tzset"):
            time.tzset()
        web_app._TZ_CACHE.clear()


def test_all_day_workouts_are_left_alone(monkeypatch):
    monkeypatch.setenv("TRAINING_TZ", "Europe/London")
    web_app._TZ_CACHE.clear()
    assert web_app.normalize_plan_date("2026-06-20") == ("2026-06-20", None)


# ---------------------------------------------------------------------------
# defect 8 — "I couldn't place this" has to survive the toast
# ---------------------------------------------------------------------------


def test_unplaceable_sessions_are_persisted_and_shown_on_the_calendar(client):
    block(client, week2(1), "work trip")
    block(client, week2(2), "work trip")
    payload = propose(client, week2(0), week2(6))
    assert payload["unresolved"], "blocking Tue+Wed should leave one unplaceable"
    stuck = payload["unresolved"][0]
    assert stuck["kind"] == "no_proposal" and stuck["status"] == "unresolved"

    # The UI reloads the calendar straight after asking — this is where the
    # answer used to disappear.
    after = calendar(client, week2(0), week2(6))
    assert [item["uid"] for item in after["unresolved"]] == [stuck["uid"]]
    day = next(d for d in after["days"] if d["date"] == stuck["from_date"])
    assert [item["uid"] for item in day["unresolved"]] == [stuck["uid"]]
    assert day["unresolved"][0]["rationale"]

    # And on a cold start, from disk.
    with TestClient(web_app.create_app(demo=True)) as fresh:
        again = calendar(fresh, week2(0), week2(6))
        assert [i["uid"] for i in again["unresolved"]] == [stuck["uid"]]

    listed = ok(client.get("/api/proposals", params={"status": "unresolved"}))
    assert [i["uid"] for i in listed["unresolved"]] == [stuck["uid"]]
    assert listed["proposals"] == []


def test_an_unplaceable_session_is_never_approvable(client):
    block(client, week2(1), "work trip")
    block(client, week2(2), "work trip")
    stuck = propose(client, week2(0), week2(6))["unresolved"][0]
    assert stuck["requires_approval"] is True
    pending = ok(client.get("/api/proposals", params={"status": "pending"}))
    assert stuck["uid"] not in {p["uid"] for p in pending["proposals"]}
    refused = client.post(
        f"/api/proposals/{stuck['proposal_id']}/approve", json={})
    assert refused.status_code == 404


def test_an_unplaceable_session_clears_once_the_athlete_answers_it(client):
    block(client, week2(1), "work trip")
    block(client, week2(2), "work trip")
    stuck = propose(client, week2(0), week2(6))["unresolved"][0]

    ok(client.patch(f"/api/workouts/{stuck['uid']}", json={"status": "skipped"}))
    after = calendar(client, week2(0), week2(6))
    assert after["unresolved"] == []


def test_unresolved_items_on_other_weeks_survive_regeneration(client):
    block(client, week2(1), "work trip")
    block(client, week2(2), "work trip")
    stuck = propose(client, week2(0), week2(6))["unresolved"][0]

    third = (monday() + timedelta(days=14)).isoformat()
    propose(client, third, (monday() + timedelta(days=20)).isoformat())
    after = calendar(client, week2(0), (monday() + timedelta(days=20)).isoformat())
    assert stuck["uid"] in {i["uid"] for i in after["unresolved"]}


# ---------------------------------------------------------------------------
# the safety property the whole app rests on
# ---------------------------------------------------------------------------


def test_a_proposal_alone_changes_nothing(client):
    block(client, week2(1), "work trip")
    before = workouts_by_day(calendar(client, week2(0), week2(6)))
    payload = propose(client, week2(0), week2(6))
    assert payload["proposals"]
    assert all(p["requires_approval"] for p in payload["proposals"])
    assert workouts_by_day(calendar(client, week2(0), week2(6))) == before


def test_approving_never_pushes_to_garmin(client):
    block(client, week2(1), "work trip")
    swap = find(propose(client, week2(0), week2(6))["proposals"], "SWAP")
    ok(client.post(f"/api/proposals/{swap['proposal_id']}/approve", json={}))
    for uid in swap["affects_uids"]:
        moved = workout(client, uid)
        assert moved["push_approved"] is False
        assert moved["garmin"]["push_state"] == "never"
    assert ok(client.get("/api/push"))["jobs"] == []


def test_demo_mode_refuses_a_real_push(client):
    refused = client.post("/api/push", json={
        "start": week2(0), "end": week2(6), "dry_run": False})
    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "not_configured"


# ---------------------------------------------------------------------------
# helpers for the push-path tests
# ---------------------------------------------------------------------------


def approve_for_push(client, uid):
    return ok(client.patch(f"/api/workouts/{uid}",
                           json={"push_approved": True}))


def push(client, **body):
    """POST /api/push. The endpoint answers 202 — the job is accepted, not
    finished, even when a dry run finishes inside the request."""
    response = client.post("/api/push", json=body)
    assert response.status_code == 202, response.text
    return response.json()


def pretend_pushed(store, uid, date, workout_id=555, schedule_id=777):
    """Put a workout in the state a successful push leaves behind. No network:
    this is exactly what `record_push` writes when the writer comes back."""
    return store.record_push(uid, workout_id=workout_id,
                             schedule_id=schedule_id, scheduled_for=date,
                             pushed_hash="hash-abc", push_state="scheduled")


PUSH_IDENTITY_KEYS = ("push_state", "workout_id", "schedule_id",
                      "scheduled_for", "pushed_hash")


def push_identity(store, uid):
    return {k: (store.get_push(uid) or {}).get(k) for k in PUSH_IDENTITY_KEYS}


# ---------------------------------------------------------------------------
# defect 4 — a rollback restores the prior state, push state included
# ---------------------------------------------------------------------------


def test_a_rolled_back_swap_leaves_the_push_state_exactly_as_it_found_it(
        client, monkeypatch):
    """The rollback used to re-patch the fields back, which the store reads as
    a fresh user edit and answers by flipping a pushed workout from
    "scheduled" to "drifted".

    That is terminal, not cosmetic: re-pushing matches the stored hash, takes
    the noop branch and returns before it can record a new state, so the
    "changed since it was pushed" badge never clears again. A change the
    athlete never approved would have permanently marked his session wrong.
    """
    block(client, week2(1), "work trip")
    swap = find(propose(client, week2(0), week2(6))["proposals"], "SWAP")
    assert swap is not None
    victim = next(op for op in swap["operations"] if op["uid"] != swap["uid"])

    store = client.app.state.service.store
    victim_before = workout(client, victim["uid"])
    pretend_pushed(store, victim["uid"], victim_before["date"])
    assert workout(client, victim["uid"])["garmin"]["push_state"] == "scheduled"
    before = push_identity(store, victim["uid"])

    def boom(*args, **kwargs):
        raise RuntimeError("store went away mid-approval")

    monkeypatch.setattr(store, "approve_proposal", boom)
    failed = client.post(f"/api/proposals/{swap['proposal_id']}/approve",
                         json={})
    assert failed.status_code >= 400

    after = workout(client, victim["uid"])
    assert after["date"] == victim_before["date"], "the move must be undone"
    assert push_identity(store, victim["uid"]) == before
    assert after["garmin"]["push_state"] == "scheduled", \
        "a rollback is a compensating action, not an edit"


def test_the_rollback_clears_the_drift_badge_without_blunting_it(client,
                                                                 monkeypatch):
    """The user-visible half of the same defect, and its other side: the
    warning badge the UI draws off `push_state` must be clear after a
    rollback, and a real edit afterwards must still raise it."""
    block(client, week2(1), "work trip")
    swap = find(propose(client, week2(0), week2(6))["proposals"], "SWAP")
    victim = next(op for op in swap["operations"] if op["uid"] != swap["uid"])

    store = client.app.state.service.store
    pretend_pushed(store, victim["uid"], workout(client, victim["uid"])["date"])
    approve_for_push(client, victim["uid"])
    # push_approved is not one of the fields a push can drift on.
    assert workout(client, victim["uid"])["garmin"]["push_state"] == "scheduled"

    monkeypatch.setattr(store, "approve_proposal",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert client.post(f"/api/proposals/{swap['proposal_id']}/approve",
                       json={}).status_code >= 400
    assert workout(client, victim["uid"])["garmin"]["push_state"] == "scheduled"

    # ...and the athlete's own move still drifts it, as it always did.
    ok(client.patch(f"/api/workouts/{victim['uid']}", json={"date": week2(5)}))
    assert workout(client, victim["uid"])["garmin"]["push_state"] == "drifted"


# ---------------------------------------------------------------------------
# defect 5 — approving a suggestion that changes nothing changes nothing
# ---------------------------------------------------------------------------


def test_approving_a_no_op_keep_does_not_consume_the_athletes_undo(
        client, monkeypatch):
    """A KEEP writes no fields. The store still materialised it as an empty
    patch, which bumped the rev and appended an undoable entry — and that
    entry, being the newest change to the uid, retired the athlete's own. His
    Undo stopped offering his edit and started refusing it with "there's a
    newer change to that workout"."""
    monkeypatch.setattr(web_app, "DEMO_WORKOUTS", RACE_DEMO)
    race_day = week2(5)
    block(client, race_day, "family thing")
    keep = find(propose(client, week2(0), week2(6))["proposals"], "KEEP")
    assert keep is not None

    # The athlete already owns a record on this workout.
    approve_for_push(client, keep["uid"])
    mine = ok(client.get("/api/history"))["entries"][0]
    assert mine["uid"] == keep["uid"] and mine["undoable"] is True
    rev_before = workout(client, keep["uid"])["rev"]

    ok(client.post(f"/api/proposals/{keep['proposal_id']}/approve", json={}))

    after = workout(client, keep["uid"])
    assert after["rev"] == rev_before, "a no-op proposal must not bump the rev"
    assert after["push_approved"] is True, "his own edit must survive"

    entries = ok(client.get("/api/history"))["entries"]
    still = next(e for e in entries if e["entry_id"] == mine["entry_id"])
    assert still["undoable"] is True, "approving a no-op ate his undo"
    ok(client.post(f"/api/history/{mine['entry_id']}/undo"))
    assert workout(client, keep["uid"])["push_approved"] is False


def test_approving_a_no_op_keep_writes_no_history_entry(client, monkeypatch):
    monkeypatch.setattr(web_app, "DEMO_WORKOUTS", RACE_DEMO)
    block(client, week2(5), "family thing")
    keep = find(propose(client, week2(0), week2(6))["proposals"], "KEEP")
    approve_for_push(client, keep["uid"])

    before = [e["entry_id"] for e in ok(client.get("/api/history"))["entries"]]
    ok(client.post(f"/api/proposals/{keep['proposal_id']}/approve", json={}))
    after = [e["entry_id"] for e in ok(client.get("/api/history"))["entries"]]
    assert after == before, "a proposal that writes nothing wrote history"

    decided = ok(client.get("/api/proposals", params={"status": "approved"}))
    assert any(p["proposal_id"] == keep["proposal_id"]
               for p in decided["proposals"]), "the decision must be recorded"


def test_a_decided_proposal_is_never_labelled_stale(client):
    """Approving a move is exactly what makes its premise stop holding. The
    record of a decision he made is history, not an offer that expired."""
    block(client, week2(1), "work trip")
    swap = find(propose(client, week2(0), week2(6))["proposals"], "SWAP")
    ok(client.post(f"/api/proposals/{swap['proposal_id']}/approve", json={}))
    ok(client.delete(f"/api/days/{week2(1)}/availability"))

    decided = ok(client.get("/api/proposals", params={"status": "approved"}))
    same = next(p for p in decided["proposals"]
                if p["proposal_id"] == swap["proposal_id"])
    assert same["stale"] is False and same["stale_reason"] is None


# ---------------------------------------------------------------------------
# defect 6 — an unresolved flag dies with the block that caused it
# ---------------------------------------------------------------------------


def test_unplaceable_sessions_clear_when_the_block_that_caused_them_goes(
        client):
    """Block a week, get told six sessions can't be placed, then cancel the
    trip. The amber flags used to burn on the calendar forever: the staleness
    filter checked the workout but never the availability that made it
    unplaceable."""
    for offset in range(7):
        block(client, week2(offset), "training camp cancelled later")
    stuck = propose(client, week2(0), week2(6))["unresolved"]
    assert stuck, "a fully blocked week can't be placed"
    assert len(calendar(client, week2(0), week2(6))["unresolved"]) == len(stuck)

    for offset in range(7):
        ok(client.delete(f"/api/days/{week2(offset)}/availability"))

    # Regenerating the *other* week, exactly as reported.
    third = (monday() + timedelta(days=14)).isoformat()
    propose(client, third, (monday() + timedelta(days=20)).isoformat())

    after = calendar(client, week2(0), week2(6))
    assert after["unresolved"] == [], \
        "unplaceable sessions outlived the block that caused them"
    assert all(not day["unresolved"] for day in after["days"])
    listed = ok(client.get("/api/proposals", params={"status": "unresolved"}))
    assert listed["unresolved"] == []


def test_unplaceable_sessions_survive_while_the_block_is_still_there(client):
    """The other half: the filter must not throw away a live flag."""
    block(client, week2(1), "work trip")
    block(client, week2(2), "work trip")
    stuck = propose(client, week2(0), week2(6))["unresolved"][0]
    other = week2(2) if stuck["from_date"] != week2(2) else week2(1)
    ok(client.delete(f"/api/days/{other}/availability"))
    # Its own day is still blocked, so the answer still stands.
    after = calendar(client, week2(0), week2(6))
    assert stuck["uid"] in {i["uid"] for i in after["unresolved"]}


# ---------------------------------------------------------------------------
# defect 7 — a suggestion may not outlive the reason printed on it
# ---------------------------------------------------------------------------


def test_a_proposal_whose_reason_has_gone_is_marked_stale_everywhere(client):
    """Block Tuesday, get a suggestion, cancel the trip. The suggestion used
    to stay pending and keep rendering "You've marked Tuesday unavailable
    (conference)" on the calendar — the athlete's primary interface showing
    him a confident, wrong explanation."""
    tuesday = week2(1)
    block(client, tuesday, "conference")
    proposal = propose(client, week2(0), week2(6))["proposals"][0]
    assert proposal["stale"] is False
    assert tuesday in proposal["premise"]["unavailable_days"]

    ok(client.delete(f"/api/days/{tuesday}/availability"))
    # Regenerating the grid re-plans this week only, so the suggestion on next
    # week is never reconsidered — which is what left it standing.
    propose(client, monday().isoformat(),
            (monday() + timedelta(days=20)).isoformat())

    listed = ok(client.get("/api/proposals", params={"status": "pending"}))
    same = next(p for p in listed["proposals"]
                if p["proposal_id"] == proposal["proposal_id"])
    assert same["stale"] is True
    assert same["stale_reason"], "a stale suggestion has to say why"

    grid = calendar(client, week2(0), week2(6))
    on_day = [p for day in grid["days"] for p in day["proposals"]
              if p["proposal_id"] == proposal["proposal_id"]]
    assert on_day and all(p["stale"] for p in on_day), \
        "the calendar rendered a suggestion whose reason is no longer true"


def test_a_stale_proposal_can_never_be_applied(client):
    tuesday = week2(1)
    block(client, tuesday, "conference")
    proposal = propose(client, week2(0), week2(6))["proposals"][0]
    before = workout(client, proposal["uid"])["date"]

    ok(client.delete(f"/api/days/{tuesday}/availability"))

    refused = client.post(
        f"/api/proposals/{proposal['proposal_id']}/approve", json={})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "proposal_stale"
    assert workout(client, proposal["uid"])["date"] == before

    # Not even with force: force is consent to overwrite your own edit, never
    # consent to act on a reason that has stopped being true.
    forced = client.post(f"/api/proposals/{proposal['proposal_id']}/approve",
                         json={"force": True})
    assert forced.status_code == 409
    assert workout(client, proposal["uid"])["date"] == before


def test_a_proposal_is_stale_once_its_workout_has_moved_by_hand(client):
    block(client, week2(1), "work trip")
    proposal = propose(client, week2(0), week2(6))["proposals"][0]
    ok(client.patch(f"/api/workouts/{proposal['uid']}",
                    json={"date": week2(4)}))
    refused = client.post(
        f"/api/proposals/{proposal['proposal_id']}/approve", json={})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "proposal_stale"
    assert workout(client, proposal["uid"])["date"] == week2(4)


def test_a_live_proposal_is_not_marked_stale(client):
    block(client, week2(1), "work trip")
    payload = propose(client, week2(0), week2(6))
    assert payload["proposals"] and all(not p["stale"]
                                        for p in payload["proposals"])
    listed = ok(client.get("/api/proposals", params={"status": "pending"}))
    assert listed["proposals"] and all(not p["stale"]
                                       for p in listed["proposals"])
    swap = find(payload["proposals"], "SWAP")
    ok(client.post(f"/api/proposals/{swap['proposal_id']}/approve", json={}))


# ---------------------------------------------------------------------------
# safety — approval and push are two separate consents
# ---------------------------------------------------------------------------


def test_push_everything_still_only_pushes_what_was_approved(client):
    """`select: "all_effective"` pushed every workout in the range regardless
    of push_approved, which quietly collapses the two consents into one."""
    approved_uid = calendar(client, week2(0), week2(6))["days"][2]["workouts"][0]["uid"]
    approve_for_push(client, approved_uid)

    job = push(client, start=week2(0), end=week2(6), dry_run=True,
               select="all_effective")

    sent = [i for i in job["items"] if i["state"] != "skipped"]
    assert [i["uid"] for i in sent] == [approved_uid], sent
    refused = [i for i in job["items"]
               if "Not approved for push" in (i.get("reason") or "")]
    assert refused, "the skipped ones have to say why"
    assert job["not_approved"]["count"] == len(refused)
    assert approved_uid not in job["not_approved"]["uids"]


def test_naming_a_uid_explicitly_does_not_substitute_for_approving_it(client):
    day = calendar(client, week2(0), week2(6))["days"][2]
    uid = day["workouts"][0]["uid"]
    job = push(client, start=week2(0), end=week2(6), dry_run=True,
               select="explicit", uids=[uid])
    assert [i["state"] for i in job["items"]] == ["skipped"]
    assert "Not approved for push" in job["items"][0]["reason"]
    assert job["not_approved"] == {"count": 1, "uids": [uid]}


class _FakePushResult:
    """What the writer hands back, without the writer or the network."""

    def __init__(self, uid):
        self.uid = uid
        self.error = None

    def to_dict(self):
        return {"uid": self.uid, "title": "", "date": None, "action": "create",
                "state": "pushed", "reason": None, "garmin_workout_id": 1,
                "garmin_schedule_id": 2, "error": None, "structured": False}


class _RecordingWriter:
    def __init__(self):
        self.pushed = []

    def push_workout(self, uid, *args, **kwargs):
        self.pushed.append(uid)
        return _FakePushResult(uid)


def test_the_push_worker_never_hands_an_unapproved_workout_to_the_writer(
        client, monkeypatch):
    """Approval can be withdrawn between a job being built and being run — or
    the job can be reloaded from disk after a restart. The rule has to live
    next to the write, not only where the list was chosen."""
    service = client.app.state.service
    days = [d for d in calendar(client, week2(0), week2(6))["days"]
            if d["workouts"]]
    approved, withdrawn = days[2]["workouts"][0]["uid"], days[3]["workouts"][0]["uid"]
    approve_for_push(client, approved)

    writer = _RecordingWriter()
    monkeypatch.setattr(service, "writer", lambda: writer)
    monkeypatch.setattr(service, "garmin_writes_allowed", lambda: True)

    rows = [{"uid": uid, "title": "", "date": None, "action": "create",
             "state": "pending", "reason": None, "garmin_workout_id": None,
             "garmin_schedule_id": None, "error": None, "structured": False}
            for uid in (approved, withdrawn)]
    service.record_job({"job_id": "psh_recheck", "state": "queued",
                        "dry_run": False, "items": rows,
                        "progress": {"done": 0, "total": len(rows)}})
    web_app.run_push_job(service, "psh_recheck")

    assert writer.pushed == [approved], \
        "an unapproved workout was sent to Garmin"
    after = service.get_job("psh_recheck")
    row = next(i for i in after["items"] if i["uid"] == withdrawn)
    assert row["state"] == "skipped"
    assert "Not approved for push" in row["reason"]


def test_the_write_path_itself_refuses_when_garmin_writes_are_off(client):
    """The endpoint gate is not the only gate: the loop that actually calls
    Garmin checks too, so no future caller can route around it."""
    service = client.app.state.service
    service.record_job({"job_id": "psh_gate", "state": "queued",
                        "dry_run": False, "items": [],
                        "progress": {"done": 0, "total": 0}})
    web_app.run_push_job(service, "psh_gate")
    after = service.get_job("psh_gate")
    assert after["state"] == "failed"
    assert after["error"]["code"] == "writes_disabled"


# ---------------------------------------------------------------------------
# safety — a real push needs an explicit opt-in on this machine
# ---------------------------------------------------------------------------


@pytest.fixture
def configured_client(monkeypatch):
    """A non-demo app with Garmin credentials present, and a session that can
    never be opened.

    The refusal has to happen before anything reaches Garmin, so `garmin()` is
    replaced with a tripwire: if the gate ever moved below the login, this
    fixture fails the test rather than dialling garmin.com with a fake
    password.
    """
    if TestClient is None:
        pytest.skip("the HTTP tests need httpx installed (pip install httpx)")
    monkeypatch.setenv("GARMIN_EMAIL", "rider@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "not-a-real-password")

    def no_login(self, *args, **kwargs):
        raise AssertionError("the test tried to open a real Garmin session")

    monkeypatch.setattr(web_app.TrainingService, "garmin", no_login)
    with TestClient(web_app.create_app(demo=False)) as test_client:
        yield test_client


def test_a_real_push_is_refused_until_garmin_writes_are_opted_in(
        configured_client):
    refused = configured_client.post("/api/push", json={
        "start": week2(0), "end": week2(6), "dry_run": False})
    assert refused.status_code == 403, refused.text
    body = refused.json()["error"]
    assert body["code"] == "writes_disabled"
    assert web_app.ALLOW_GARMIN_WRITES_ENV in body["message"]
    assert body["detail"]["env_var"] == web_app.ALLOW_GARMIN_WRITES_ENV


def test_the_opt_in_is_reported_so_the_ui_can_explain_itself(
        configured_client, monkeypatch):
    status = ok(configured_client.get("/api/status"))["capabilities"]
    assert status["can_push_to_garmin"] is True, "credentials are present"
    assert status["can_write_to_garmin"] is False
    assert web_app.ALLOW_GARMIN_WRITES_ENV in \
        status["garmin_writes_disabled_reason"]

    monkeypatch.setenv(web_app.ALLOW_GARMIN_WRITES_ENV, "1")
    on = ok(configured_client.get("/api/status"))["capabilities"]
    assert on["can_write_to_garmin"] is True
    assert on["garmin_writes_disabled_reason"] is None


def test_preview_is_always_available_even_with_writes_switched_off(client):
    """The gate must never cost the athlete the answer to "what would you
    send?" — the preview makes no network call at all."""
    uid = calendar(client, week2(0), week2(6))["days"][2]["workouts"][0]["uid"]
    approve_for_push(client, uid)
    job = push(client, start=week2(0), end=week2(6), dry_run=True)
    assert job["dry_run"] is True and job["state"] == "done"
    assert job["summary"]["created"] == 1
    assert ok(client.get("/api/status"))["capabilities"][
        "can_write_to_garmin"] is False
