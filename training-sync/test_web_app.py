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
    # A non-demo app opens the store at its *default* path, which is the
    # developer's own training-sync/.plan_state.json. Point it at the tmp dir
    # so no test can read — or write — the real plan.
    monkeypatch.setenv("TRAINING_SYNC_STATE_PATH",
                       str(tmp_path / "plan_state.json"))
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


# ---------------------------------------------------------------------------
# defect 7 — a rollback must not fabricate a push that never happened
# ---------------------------------------------------------------------------


def test_a_failed_approval_invents_no_push_record_for_a_never_pushed_workout(
        client, monkeypatch):
    """`push_record` falls back to the override's projected `garmin` block,
    and the store now attaches that projection to *every* record — a blank one
    whose `push_state` is "never". "never" is truthy, so the rollback's
    "nothing to restore" guard let it through and `record_push` wrote a push
    entry, plus a ('push', uid) line in the audit log, for a workout this app
    has never sent anywhere.

    Nothing reaches the calendar, so the harm is entirely in the record: the
    athlete's history claims a push that did not happen, and the store starts
    reporting a Garmin block for a uid Garmin has never heard of.
    """
    block(client, week2(1), "work trip")
    swap = find(propose(client, week2(0), week2(6))["proposals"], "SWAP")
    assert swap is not None
    victim = next(op for op in swap["operations"] if op["uid"] != swap["uid"])
    uid = victim["uid"]

    store = client.app.state.service.store
    # The projection only exists once the athlete owns a record on the uid —
    # ticking "approve for push" is the cheapest way to get one, and is
    # exactly what the reproduction did.
    approve_for_push(client, uid)
    assert store.get_push(uid) is None, "nothing has been pushed"
    assert store.get_override(uid)["garmin"]["push_state"] == "never", \
        "the blank projection is what the guard has to see through"

    monkeypatch.setattr(store, "approve_proposal",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert client.post(f"/api/proposals/{swap['proposal_id']}/approve",
                       json={}).status_code >= 400

    assert store.get_push(uid) is None, \
        "a rollback fabricated a push record for a workout never pushed"
    assert store.all_pushes() == {}
    actions = [(e["action"], e["uid"])
               for e in ok(client.get("/api/history"))["entries"]]
    assert ("push", uid) not in actions, \
        "the audit log claims a push that never happened"


def test_a_rollback_still_repairs_a_push_that_really_happened(client,
                                                              monkeypatch):
    """The other side of the same guard: treating "never" as "no record" must
    not stop a genuine push record being put back."""
    block(client, week2(1), "work trip")
    swap = find(propose(client, week2(0), week2(6))["proposals"], "SWAP")
    victim = next(op for op in swap["operations"] if op["uid"] != swap["uid"])
    store = client.app.state.service.store
    pretend_pushed(store, victim["uid"], workout(client, victim["uid"])["date"])
    before = push_identity(store, victim["uid"])

    monkeypatch.setattr(store, "approve_proposal",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert client.post(f"/api/proposals/{swap['proposal_id']}/approve",
                       json={}).status_code >= 400
    assert push_identity(store, victim["uid"]) == before


# ---------------------------------------------------------------------------
# gap 5 / defect 8 — a fake Garmin account, so the real write path can run
# ---------------------------------------------------------------------------


class FakeGarminCalendar:
    """A tiny model of the athlete's Garmin calendar, standing in for
    `garminconnect.Garmin`.

    Only the calls a removal makes are implemented — list a month, unschedule
    a row — so nothing in these tests can reach the network even by accident.
    An unschedule removes the row, which is what the Edge 520 would stop
    showing after the next sync.
    """

    def __init__(self):
        self.rows = []
        self.calls = []

    def add(self, date, workout_id, schedule_id, name):
        self.rows.append({"date": date, "workoutId": workout_id,
                          "workoutScheduleId": schedule_id,
                          "workoutName": name})

    def get_scheduled_workouts(self, year, month):
        prefix = "%04d-%02d" % (int(year), int(month))
        self.calls.append(("list", prefix))
        return [dict(row) for row in self.rows
                if str(row["date"]).startswith(prefix)]

    def unschedule_workout(self, schedule_id):
        self.calls.append(("unschedule", schedule_id))
        row = next((r for r in self.rows
                    if r["workoutScheduleId"] == schedule_id), None)
        if row is None:
            raise RuntimeError("no scheduled workout %s" % schedule_id)
        self.rows.remove(row)
        return {"ok": True}


@pytest.fixture
def live_client(monkeypatch):
    """A non-demo app whose Garmin account is a `FakeGarminCalendar`.

    Everything above the last hop is the real thing — the real endpoint, the
    real `GarminWorkoutWriter`, the real store — and only the bottom-most
    `garminconnect` calls are faked, so the write path under test is the one
    that would run against a real account. The plan comes from the demo
    generator because a non-demo app has no ICS feed configured and these
    tests need workouts to act on.

    Writes are still opt-in: the env gate is *not* set here, so every test
    that wants a real removal has to say so.
    """
    if TestClient is None:
        pytest.skip("the HTTP tests need httpx installed (pip install httpx)")
    monkeypatch.setenv("GARMIN_EMAIL", "rider@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "not-a-real-password")
    garmin = FakeGarminCalendar()
    monkeypatch.setattr(
        web_app.TrainingService, "plan",
        lambda self, force=False: (web_app.demo_plan_entries(self), []))
    monkeypatch.setattr(web_app.TrainingService, "garmin",
                        lambda self, force=False: garmin)
    with TestClient(web_app.create_app(demo=False)) as test_client:
        test_client.garmin = garmin
        yield test_client


def garmin_reread(client):
    """Forget the app's cached picture of the Garmin month.

    These tests change the fake account behind the app's back, which is what
    another device — or the athlete in Garmin Connect — does in real life; a
    refresh is how the app finds out. Both caches are dropped: the service's
    context and the writer's own month read.
    """
    service = client.app.state.service
    service._garmin_context = None
    service._garmin_context_at = 0.0
    try:
        service.writer().invalidate_schedule_cache()
    except web_app.ApiError:
        # No Garmin session, so there is no writer to invalidate — and that is
        # now a refusal rather than a writer holding `client=None`. See
        # `test_a_writer_is_never_built_without_a_garmin_session`.
        service._writer = None
        service._writer_client = None


def a_pushed_session(client, day=None, workout_id=4242, schedule_id=8484,
                     name=None):
    """A session that is on the plan, on the fake Garmin calendar, and in the
    store's push map — the state a successful push leaves behind."""
    day = day or week2(1)
    uid = calendar(client, day, day)["days"][0]["workouts"][0]["uid"]
    module, _ = web_app.load_module("garmin_workout_writer")
    if name is None:
        name = module.marked_workout_name(
            workout(client, uid)["title"], uid)
    client.garmin.add(day, workout_id, schedule_id, name)
    pretend_pushed(client.app.state.service.store, uid, day,
                   workout_id=workout_id, schedule_id=schedule_id)
    garmin_reread(client)
    return uid, day


# ---------------------------------------------------------------------------
# round 6 — the removal path is gone
#
# This app used to expose `POST /api/workouts/{uid}/unpush`, backed by
# `run_unpush` and `_removal_is_retryable`, with a "Remove from Garmin" button
# on the card, in the card menu and in the push drawer. All of it is deleted.
#
# It went because it could not report its own outcome honestly. A refusal was
# written to the store as `push_state: "failed"` with the reason on `error` —
# byte-for-byte what a refused *push* leaves — so telling the two apart needed
# a side table in process memory. That table did not survive a restart and the
# record did: the card then read a failed push, printed the removal sentence
# and offered "Retry", and Retry opens the push drawer. The athlete asked for a
# session to come OFF and the card offered to send it.
#
# The tests below pin the absence, because "we deleted it" is only true until
# someone helpfully adds it back.
# ---------------------------------------------------------------------------


def test_the_unpush_endpoint_is_gone(live_client, monkeypatch):
    """Not disabled, not gated — absent. A 404/405 from the router, with the
    athlete's Garmin calendar untouched."""
    monkeypatch.setenv(web_app.ALLOW_GARMIN_WRITES_ENV, "1")
    uid, _ = a_pushed_session(live_client)

    refused = live_client.post(f"/api/workouts/{uid}/unpush")

    assert refused.status_code in (404, 405), refused.text
    assert len(live_client.garmin.rows) == 1, \
        "something still took the session off the calendar"
    assert not [c for c in live_client.garmin.calls if c[0] == "unschedule"]


def test_no_route_on_the_app_can_take_a_session_off_garmin(live_client):
    """The endpoint could come back under another name. This asks the router
    itself: no path this app serves mentions removal."""
    paths = {route.path for route in live_client.app.routes
             if getattr(route, "path", None)}
    for word in ("unpush", "unschedule", "remove"):
        assert not [p for p in paths if word in p.lower()], \
            f"a route matching {word!r} is back: {sorted(paths)}"


def test_the_removal_machinery_is_deleted_from_the_module(live_client):
    """`run_unpush`, `_removal_is_retryable` and the per-uid side table that
    made a refused removal distinguishable from a refused push. The side table
    is named here because it is what made the whole feature dishonest across a
    restart: process memory holding the only copy of "which act failed"."""
    for name in ("run_unpush", "_removal_is_retryable"):
        assert not hasattr(web_app, name), f"{name} is back"
    service = live_client.app.state.service
    for name in ("note_removal_failure", "clear_removal_failure",
                 "removal_notice", "_removal_failures"):
        assert not hasattr(service, name), f"{name} is back"


def test_a_workout_payload_carries_no_removal_block(live_client):
    """`garmin.removal` was the field the card read to print the removal
    sentence. Nothing produces it now, so nothing can render a stale one."""
    uid, _ = a_pushed_session(live_client)
    garmin = workout(live_client, uid)["garmin"]
    assert "removal" not in garmin, garmin
    # What replaces it: the plain fact that a copy is on the calendar. Every
    # sentence about removal is now drawn from this and points at Garmin
    # Connect.
    assert garmin["on_garmin"] is True


def test_a_skipped_session_is_told_where_removal_actually_happens(client):
    """The athlete looks for "Remove" where the app told him there is a copy.
    That is the place that has to answer him — and the answer is Garmin
    Connect, not a button here that might not have worked."""
    day = week2(1)
    uid = calendar(client, day, day)["days"][0]["workouts"][0]["uid"]
    pretend_pushed(client.app.state.service.store, uid, day)
    approve_for_push(client, uid)
    ok(client.patch(f"/api/workouts/{uid}", json={"status": "skipped"}))

    row = next(i for i in push(client, start=week2(0), end=week2(6),
                               dry_run=True)["items"] if i["uid"] == uid)
    assert "Garmin Connect" in row["reason"], row["reason"]
    assert "Remove from Garmin" not in row["reason"], \
        "the copy still points at a button this app no longer has"


def test_a_skipped_session_still_on_garmin_says_so_instead_of_going_silent(
        client):
    """Marking a pushed session skipped used to end the story with
    `reason: "Marked skipped."` — and the session stayed on the Edge. The
    preview has to say the copy is still there and point at the way off."""
    day = week2(1)
    uid = calendar(client, day, day)["days"][0]["workouts"][0]["uid"]
    store = client.app.state.service.store
    pretend_pushed(store, uid, day)
    approve_for_push(client, uid)
    ok(client.patch(f"/api/workouts/{uid}", json={"status": "skipped"}))

    job = push(client, start=week2(0), end=week2(6), dry_run=True)
    row = next(i for i in job["items"] if i["uid"] == uid)
    assert row["action"] == "skip" and row["state"] == "skipped"
    assert row["on_garmin"] is True
    assert row["reason"] != "Marked skipped.", \
        "the copy is still on the athlete's calendar and this said nothing"
    assert "still on your Garmin calendar" in row["reason"]
    assert row["garmin_workout_id"] == 555
    assert job["summary"]["skipped"] == 1


def test_a_skipped_session_that_was_never_pushed_still_reads_plainly(client):
    day = week2(1)
    uid = calendar(client, day, day)["days"][0]["workouts"][0]["uid"]
    approve_for_push(client, uid)
    ok(client.patch(f"/api/workouts/{uid}", json={"status": "skipped"}))
    job = push(client, start=week2(0), end=week2(6), dry_run=True)
    row = next(i for i in job["items"] if i["uid"] == uid)
    assert row["reason"] == "Marked skipped."
    assert row["on_garmin"] is False


def test_a_preview_never_removes_anything_by_itself(live_client, monkeypatch):
    """Removal is an explicit act. Skipping a pushed session and previewing a
    push must not quietly unschedule it — not even with writes switched on."""
    monkeypatch.setenv(web_app.ALLOW_GARMIN_WRITES_ENV, "1")
    uid, day = a_pushed_session(live_client)
    approve_for_push(live_client, uid)
    ok(live_client.patch(f"/api/workouts/{uid}", json={"status": "skipped"}))

    push(live_client, start=week2(0), end=week2(6), dry_run=True)
    assert len(live_client.garmin.rows) == 1, "a preview removed a session"
    assert not [c for c in live_client.garmin.calls if c[0] == "unschedule"]
    assert live_client.app.state.service.store.get_push(uid) is not None


# ---------------------------------------------------------------------------
# defect 8 — the ownership marker leaked into the UI
# ---------------------------------------------------------------------------


def test_the_ownership_marker_never_reaches_the_calendar_ui(live_client):
    """Every workout this app writes carries `[ts:xxxxxxxx]` in its Garmin
    name, so a later push can prove the copy is its own. It is bookkeeping,
    and `days[].garmin.scheduled[].title` was handing it straight to the
    athlete: "Threshold 3x12min [ts:1a2b3c4d]".
    """
    module, _ = web_app.load_module("garmin_workout_writer")
    day = week2(1)
    uid = calendar(live_client, day, day)["days"][0]["workouts"][0]["uid"]
    title = workout(live_client, uid)["title"]
    marked = module.marked_workout_name(title, uid)
    assert module.MARKER_RE.search(marked), "the fixture must carry a marker"
    live_client.garmin.add(day, 4242, 8484, marked)
    pretend_pushed(live_client.app.state.service.store, uid, day,
                   workout_id=4242, schedule_id=8484)
    garmin_reread(live_client)

    rows = calendar(live_client, day, day)["days"][0]["garmin"]["scheduled"]
    assert rows, "the scheduled row must still be reported"
    assert "[ts:" not in (rows[0]["title"] or "")
    assert rows[0]["title"] == title
    # Stripping the marker for display must not cost the ownership proof.
    assert rows[0]["owned_by_us"] is True


def test_an_unmarked_garmin_session_keeps_its_own_name_and_is_not_ours(
        live_client):
    day = week2(1)
    live_client.garmin.add(day, 1111, 2222, "Club chaingang")
    rows = calendar(live_client, day, day)["days"][0]["garmin"]["scheduled"]
    assert rows[0]["title"] == "Club chaingang"
    assert rows[0]["owned_by_us"] is False


def test_a_push_in_flight_is_still_the_only_writer_of_the_push_record(
        live_client, monkeypatch):
    """The removal endpoint used to serialise itself against a running push,
    because two writers of one push record is how a copy ends up on the
    calendar with no id left to reach it by. With the removal gone the push
    job is the only writer left — this pins that nothing else touches the
    record while a job is running."""
    monkeypatch.setenv(web_app.ALLOW_GARMIN_WRITES_ENV, "1")
    uid, day = a_pushed_session(live_client)
    service = live_client.app.state.service
    before = push_identity(service.store, uid)
    service.record_job({"job_id": "psh_busy", "state": "running",
                        "dry_run": False, "items": [],
                        "progress": {"done": 0, "total": 0}})
    service._active_job = "psh_busy"

    # Everything the athlete can still do to that session from the API.
    ok(live_client.patch(f"/api/workouts/{uid}", json={"status": "skipped"}))
    push(live_client, start=week2(0), end=week2(6), dry_run=True)
    calendar(live_client, day, day)

    assert push_identity(service.store, uid) == before
    assert len(live_client.garmin.rows) == 1
    assert not [c for c in live_client.garmin.calls if c[0] == "unschedule"]


# ---------------------------------------------------------------------------
# D5 — the calendar panel re-implemented the reader and lost a whole envelope
#
# `garmin_scheduled` walked every dict in the response and pulled ids off
# whichever one carried a date. Garmin's other shape is one node per *day*
# holding that day's sessions: the sessions carry no date of their own, the
# dated parent carries no ids, and the panel came back empty on every account
# that answers that way. The writer owns the canonical reader; nothing here
# gets a second opinion.
# ---------------------------------------------------------------------------


class NestedGarminCalendar(FakeGarminCalendar):
    """The same fake account, answering in Garmin's nested envelope.

    One node per day, sessions beneath it. The rows themselves are identical —
    only the shape of the wrapper differs, which is the whole point: a reader
    that handles one and not the other reports a different calendar for the
    same account.
    """

    def get_scheduled_workouts(self, year, month):
        prefix = "%04d-%02d" % (int(year), int(month))
        self.calls.append(("list", prefix))
        by_day = {}
        for row in self.rows:
            if not str(row["date"]).startswith(prefix):
                continue
            by_day.setdefault(row["date"], []).append({
                "workoutId": row["workoutId"],
                "workoutScheduleId": row["workoutScheduleId"],
                "workoutName": row["workoutName"],
            })
        return {"calendarItems": [
            {"calendarDate": day, "items": items}
            for day, items in sorted(by_day.items())
        ]}


class UnreadableGarminCalendar(FakeGarminCalendar):
    """An account whose calendar cannot be read right now.

    Not the same fact as an empty calendar, and the difference is the whole
    reason this class exists: one means "nothing is scheduled", the other
    means "no answer", and a panel that renders both as a blank day tells the
    athlete the first when the truth is the second.
    """

    def get_scheduled_workouts(self, year, month):
        self.calls.append(("list", "%04d-%02d" % (int(year), int(month))))
        raise RuntimeError("503 Service Unavailable")


class TwoHundredShapedFailureCalendar(FakeGarminCalendar):
    """The nastier half of the same distinction.

    Not every failed read raises. A rate limit, a session that quietly expired
    server-side, a shape nobody has seen before: all arrive as HTTP 200 with a
    body holding no sessions. Answering "nothing is scheduled" to those is
    indistinguishable from answering it to a genuinely empty month, and one of
    those two answers is a lie about the athlete's calendar.

    `body` is set per test to whichever 200-shaped nothing is being modelled.
    """

    body = {"retryAfter": 30, "message": "Too many requests"}

    def get_scheduled_workouts(self, year, month):
        self.calls.append(("list", "%04d-%02d" % (int(year), int(month))))
        return self.body


def nested_client(monkeypatch, calendar_class=NestedGarminCalendar):
    """A `live_client` whose fake account answers in a given envelope shape."""
    if TestClient is None:
        pytest.skip("the HTTP tests need httpx installed (pip install httpx)")
    monkeypatch.setenv("GARMIN_EMAIL", "rider@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "not-a-real-password")
    garmin = calendar_class()
    monkeypatch.setattr(
        web_app.TrainingService, "plan",
        lambda self, force=False: (web_app.demo_plan_entries(self), []))
    monkeypatch.setattr(web_app.TrainingService, "garmin",
                        lambda self, force=False: garmin)
    client = TestClient(web_app.create_app(demo=False))
    client.__enter__()
    client.garmin = garmin
    return client


def scheduled_on(client, day):
    return calendar(client, day, day)["days"][0]["garmin"]


def test_the_garmin_panel_reads_the_nested_calendar_envelope(monkeypatch):
    """D5: on an account answering in the nested shape the panel was silently
    empty — inner rows carry no date so they were skipped, and the dated
    parent carries no ids so it was skipped too."""
    client = nested_client(monkeypatch)
    try:
        day = week2(1)
        uid = calendar(client, day, day)["days"][0]["workouts"][0]["uid"]
        module, _ = web_app.load_module("garmin_workout_writer")
        marked = module.marked_workout_name(workout(client, uid)["title"], uid)
        client.garmin.add(day, 999, 111, marked)
        client.garmin.add(day, 7777, 222, "Sunday club run")
        pretend_pushed(client.app.state.service.store, uid, day,
                       workout_id=999, schedule_id=111)
        garmin_reread(client)

        block = scheduled_on(client, day)
        rows = block["scheduled"]
        assert [(r["workout_id"], r["title"]) for r in rows] == [
            (999, workout(client, uid)["title"]), (7777, "Sunday club run")], \
            "the nested envelope was read as an empty calendar"
        assert block["scheduled_readable"] is True
    finally:
        client.__exit__(None, None, None)


def test_a_garmin_id_stays_paired_with_the_name_on_its_own_row(monkeypatch):
    """Two sessions on one day is a supported state. Reading "the first id
    anywhere below the date" pairs one session's id with the other's name,
    which is how a workout gets claimed from the wrong owner."""
    client = nested_client(monkeypatch)
    try:
        day = week2(1)
        uid = calendar(client, day, day)["days"][0]["workouts"][0]["uid"]
        module, _ = web_app.load_module("garmin_workout_writer")
        marked = module.marked_workout_name(workout(client, uid)["title"], uid)
        client.garmin.add(day, 7777, 222, "Sunday club run")
        client.garmin.add(day, 999, 111, marked)
        pretend_pushed(client.app.state.service.store, uid, day,
                       workout_id=999, schedule_id=111)
        garmin_reread(client)

        rows = {r["title"]: r for r in scheduled_on(client, day)["scheduled"]}
        assert rows["Sunday club run"]["workout_id"] == 7777
        assert rows["Sunday club run"]["schedule_id"] == 222
        assert rows["Sunday club run"]["owned_by_us"] is False
        ours = rows[workout(client, uid)["title"]]
        assert (ours["workout_id"], ours["schedule_id"]) == (999, 111)
        assert ours["owned_by_us"] is True
    finally:
        client.__exit__(None, None, None)


def test_the_calendar_reader_is_not_re_implemented_in_the_web_app():
    """The duplicate is deleted, not fixed in parallel. Two readers of the
    same response is how the two answers drifted apart in the first place."""
    with open(os.path.join(HERE, "web_app.py"), encoding="utf-8") as fh:
        source = fh.read()
    for private in ("_iter_dicts", "_extract_date", "_WORKOUT_ID_KEYS",
                    "_SCHEDULE_ID_KEYS"):
        assert f"module.{private}" not in source, \
            f"web_app reaches into the writer's {private} instead of using " \
            "its canonical reader"
    assert "scheduled_rows" in source, \
        "the panel must read the calendar through the writer's own reader"


def test_a_garmin_calendar_that_could_not_be_read_is_not_shown_as_empty(
        monkeypatch):
    """"Nothing is scheduled" and "Garmin didn't answer" are different facts.
    The panel renders both, so it has to be told which one it holds."""
    client = nested_client(monkeypatch, UnreadableGarminCalendar)
    try:
        day = week2(1)
        block = scheduled_on(client, day)
        assert block["scheduled"] == []
        assert block["scheduled_readable"] is False, \
            "an unreadable month was reported as an empty day"
    finally:
        client.__exit__(None, None, None)


def test_a_rate_limited_month_is_unread_not_empty(monkeypatch):
    """End to end, through the real writer: Garmin answers 200 with a
    rate-limit body and no sessions in it. Reported as a clear day, that tells
    the athlete he has nothing on his calendar on the strength of an answer
    that contained nothing at all."""
    client = nested_client(monkeypatch, TwoHundredShapedFailureCalendar)
    try:
        block = scheduled_on(client, week2(1))
        assert block["scheduled"] == []
        assert block["scheduled_readable"] is False, \
            "a rate limit was reported as a clear Garmin calendar"
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("body", [{}, None, "", {"error": "unauthorized"},
                                  {"payload": None}])
def test_every_two_hundred_shaped_nothing_is_unread_not_empty(monkeypatch,
                                                              body):
    """An empty body, a null, an error envelope, a shape the reader doesn't
    know. None of them is evidence about the athlete's calendar."""
    class Calendar(TwoHundredShapedFailureCalendar):
        pass
    Calendar.body = body
    client = nested_client(monkeypatch, Calendar)
    try:
        assert scheduled_on(client, week2(1))["scheduled_readable"] is False, \
            f"{body!r} was reported as a read, empty calendar"
    finally:
        client.__exit__(None, None, None)


def test_an_empty_garmin_month_is_reported_as_read(monkeypatch):
    """The other half of the same distinction: a month that really is empty
    must not raise a "couldn't read your calendar" warning at the athlete."""
    client = nested_client(monkeypatch)
    try:
        block = scheduled_on(client, week2(1))
        assert block["scheduled"] == []
        assert block["scheduled_readable"] is True
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# refusals: the writer now stops rather than acting on a calendar it could not
# read, and the athlete has to be able to tell that apart from a breakage —
# and from a success
# ---------------------------------------------------------------------------


def block_calendar_reads(client):
    """Make the fake account stop answering month reads.

    Exactly the state the writer's `CALENDAR_BLOCKED` describes: Garmin is
    reachable enough to be asked and gives no usable answer, so nothing on
    that day is known. Every write decision downstream must refuse.
    """
    def refuse(year, month):
        client.garmin.calls.append(("list-failed", "%04d-%02d" % (year, month)))
        raise RuntimeError("503 Service Unavailable")
    client.garmin.get_scheduled_workouts = refuse
    garmin_reread(client)


def allow_calendar_reads(client):
    del client.garmin.get_scheduled_workouts
    garmin_reread(client)


def test_a_refused_push_reaches_the_card_in_the_writers_own_words(
        live_client, monkeypatch):
    """The writer refuses a create it cannot prove is safe and writes the
    reason onto the push record. The card used to render every one of those as
    a bare "✕ Push failed"."""
    monkeypatch.setenv(web_app.ALLOW_GARMIN_WRITES_ENV, "1")
    day = week2(1)
    uid = calendar(live_client, day, day)["days"][0]["workouts"][0]["uid"]
    approve_for_push(live_client, uid)
    block_calendar_reads(live_client)

    job = push(live_client, start=day, end=day, dry_run=False)
    for _ in range(50):
        job = ok(live_client.get(f"/api/push/{job['job_id']}"))
        if job["state"] in ("done", "partial", "failed"):
            break
        time.sleep(0.05)
    row = next(i for i in job["items"] if i["uid"] == uid)
    assert row["state"] == "failed"
    assert "couldn't read your Garmin calendar" in row["error"]["message"]

    garmin = workout(live_client, uid)["garmin"]
    assert garmin["push_state"] == "failed"
    assert "couldn't read your Garmin calendar" in (garmin["error"] or ""), \
        "the refusal never reached the card"
    # Nothing was created, so nothing of ours is on the calendar — which is
    # what decides whether the card adds "and this app can't take it off".
    assert garmin["on_garmin"] is False
    assert "removal" not in garmin


def test_a_blocked_month_is_never_reported_as_a_clear_calendar(live_client):
    """The heart of it. A month this app could not read comes back with no
    sessions, and so does a month that really is empty. Drawn the same way
    they say opposite things: "you're clear" versus "I don't know". The API
    has to keep them apart on every day it emits."""
    day = week2(1)
    live_client.garmin.add(day, 1111, 2222, "Club chaingang")
    readable = calendar(live_client, day, day)["days"][0]["garmin"]
    assert readable["scheduled_readable"] is True
    assert len(readable["scheduled"]) == 1

    block_calendar_reads(live_client)
    blind = calendar(live_client, day, day)["days"][0]["garmin"]

    assert blind["scheduled"] == [], "no rows survive an unread month"
    assert blind["scheduled_readable"] is False, \
        "an unread month was reported as a read one holding nothing"


def test_a_two_hundred_shaped_failure_is_reported_as_unread_not_empty(
        live_client, monkeypatch):
    """Not every failed read raises. A rate-limit body, an empty body or a
    shape the reader doesn't know all arrive as HTTP 200 with nothing usable
    in them, and the writer answers CALENDAR_BLOCKED for those exactly as it
    does for a 503. This app must not re-derive "was it read?" from the row
    count — that is the collapse the whole distinction exists to stop."""
    module, _ = web_app.load_module("garmin_workout_writer")
    writer = live_client.app.state.service.writer()
    monkeypatch.setattr(writer, "read_scheduled_rows",
                        lambda *a, **k: ([], module.CALENDAR_BLOCKED))
    garmin_reread(live_client)

    day = week2(1)
    block = calendar(live_client, day, day)["days"][0]["garmin"]
    assert block["scheduled"] == []
    assert block["scheduled_readable"] is False


def test_the_calendar_follows_the_writers_own_words_for_read_and_blocked(
        live_client, monkeypatch):
    """web_app used to compare the writer's status against the literals
    "read" and "blocked". The values matched, so nothing was broken — but a
    writer that renamed CALENDAR_READ would have silently marked every month
    blocked and blanked the panel, with no test failing anywhere. The
    constants are the contract; this proves this app reads them."""
    module, _ = web_app.load_module("garmin_workout_writer")
    monkeypatch.setattr(module, "CALENDAR_READ", "answered")
    monkeypatch.setattr(module, "CALENDAR_BLOCKED", "no-answer")
    writer = live_client.app.state.service.writer()
    day = week2(1)
    live_client.garmin.add(day, 1111, 2222, "Club chaingang")
    monkeypatch.setattr(writer, "read_scheduled_rows",
                        lambda year, month, **k: (
                            writer.scheduled_rows(year, month, **k), "answered"))
    garmin_reread(live_client)

    block = calendar(live_client, day, day)["days"][0]["garmin"]
    assert block["scheduled_readable"] is True, \
        "the renamed CALENDAR_READ was not followed"
    assert len(block["scheduled"]) == 1


def test_an_unrecognised_calendar_status_fails_closed(live_client, monkeypatch):
    """Anything that is not the writer's own CALENDAR_READ is treated as no
    answer. Failing open here would draw a day as clear on the strength of a
    status this app did not understand."""
    writer = live_client.app.state.service.writer()
    monkeypatch.setattr(writer, "read_scheduled_rows",
                        lambda *a, **k: ([], "something-new"))
    garmin_reread(live_client)

    day = week2(1)
    block = calendar(live_client, day, day)["days"][0]["garmin"]
    assert block["scheduled_readable"] is False


# ---------------------------------------------------------------------------
# migration orphans — a Garmin copy the app can no longer manage
#
# The store refuses to inherit push identity it cannot vouch for. That is the
# right call and it has a cost the athlete pays: a session left on their Garmin
# calendar that nothing here will ever touch again, and that a later push will
# stack a duplicate beside. Only they can remove it, so they have to be able to
# see it — on the page, not in the JSON.
# ---------------------------------------------------------------------------


def orphaning_state_file(tmp_path, title="Threshold 3x12min",
                         day="2026-08-04"):
    """A state file holding push identity that cannot be accounted for — the
    shape the pre-marker builds left behind."""
    import json
    path = tmp_path / "plan_state.json"
    path.write_text(json.dumps({
        "schema_version": 2,
        "workout_overrides": {},
        "garmin_push": {
            "orphan@tp": {
                "uid": "orphan@tp", "workout_id": 4242, "schedule_id": 8484,
                "scheduled_for": day, "push_state": "scheduled",
                "pushed_hash": None,
                "plan_snapshot": {"title": title, "date": day},
            },
        },
        "day_availability": {}, "proposals": [],
        "rejected_signatures": [], "history": [],
    }), encoding="utf-8")
    return path


def test_a_migration_orphan_carries_what_the_athlete_needs_to_find_it(
        client, tmp_path, monkeypatch):
    """`/api/status` forwarded the ids and the reason and dropped the title,
    the day and the store's ready-to-show sentence — everything the athlete
    would use to find the stray copy in Garmin Connect."""
    path = orphaning_state_file(tmp_path)
    monkeypatch.setattr(web_app, "DEMO_STATE_PATH", str(path))
    service = client.app.state.service
    service._store = None

    store = ok(client.get("/api/status"))["store"]
    assert store["migration_dropped"] == 1
    note = store["migration_notes"][0]
    assert note["uid"] == "orphan@tp"
    assert note["title"] == "Threshold 3x12min"
    assert note["scheduled_for"] == "2026-08-04"
    assert note["workout_id"] == 4242
    assert "Garmin Connect" in note["message"]


def test_a_clean_state_file_reports_no_migration_orphans(client):
    store = ok(client.get("/api/status"))["store"]
    assert store["migration_dropped"] == 0
    assert store["migration_notes"] == []


# ---------------------------------------------------------------------------
# the page's own decisions
#
# `static/index.html` carries a marked block of pure functions — the ones that
# decide what the athlete is *told*, separately from how it is drawn. They are
# lifted out verbatim and run under node here, so the sentences on the card
# have a regression test behind them and not just a code review.
# ---------------------------------------------------------------------------


import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

UI_LOGIC_START = "/* --8<-- ui-logic-start --8<-- */"
UI_LOGIC_END = "/* --8<-- ui-logic-end --8<-- */"


def ui_logic_source():
    with open(os.path.join(HERE, "static", "index.html"),
              encoding="utf-8") as fh:
        page = fh.read()
    assert UI_LOGIC_START in page and UI_LOGIC_END in page, \
        "the page lost its extractable ui-logic block"
    body = page.split(UI_LOGIC_START, 1)[1].split(UI_LOGIC_END, 1)[0]
    for forbidden in ("document.", "window.", "fetch(", "S."):
        assert forbidden not in body, \
            f"the ui-logic block must stay pure — found {forbidden!r}"
    return body


def ui_call(expression):
    """Evaluate one call against the page's own decision functions."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - dev dependency only
        pytest.skip("the UI-logic tests need node on PATH")
    script = ui_logic_source() + "\nconsole.log(JSON.stringify(" \
        + expression + " === undefined ? null : " + expression + "));\n"
    result = subprocess.run([node, "-e", script], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


def a_workout(**garmin):
    return {"uid": "u1", "title": "Threshold", "status": "planned",
            "push_approved": False, "garmin": dict(
                {"workout_id": None, "schedule_id": None, "push_state": "never",
                 "error": None, "on_garmin": False}, **garmin)}


def test_no_card_state_offers_to_take_a_session_off_garmin():
    """The defect this deletion closes, pinned at the exact function that had
    it. A refused removal left `push_state: failed` with the removal sentence
    on `error` — identical to a refused push — and after a restart the side
    table that knew the difference was gone. `pushInfo` then fell to the
    push_state==="failed" branch, printed the removal sentence and returned
    retry:true, so the card's primary button became "Retry", which opens the
    PUSH drawer. He asked for the session to come OFF and was offered to send
    it. There is no removal now, so no state may claim one."""
    leftover = a_workout(
        workout_id=4242, schedule_id=8484, on_garmin=True, push_state="failed",
        error="Couldn't confirm what's on your Garmin calendar for 2026-08-04, "
              "so nothing was removed.")
    info = ui_call("pushInfo(%s, null)" % json.dumps(leftover))

    # The writer's sentence still reaches him verbatim...
    assert "nothing was removed" in info["text"]
    # ...and the card no longer offers, or even names, an action this app has
    # dropped.
    for key in ("remove", "removeLabel", "retryable"):
        assert key not in info, f"{key} is back on the card"
    # `retry` is honest for a failed PUSH — nothing was sent, sending is the
    # remedy — and it is exactly why the removal could not share this branch.
    assert info["retry"] is True
    assert "Garmin Connect" in info["hint"], \
        "a copy is still on his calendar and nothing said where it goes"


def test_every_card_state_that_names_a_copy_on_garmin_says_where_it_goes():
    """Wherever the card tells him a session of his is sitting on the Garmin
    calendar, the same sentence follows. Losing it is the other half of the
    original bug: a copy on the Edge with nothing on screen pointing at it."""
    skipped = a_workout(workout_id=4242, schedule_id=8484, on_garmin=True)
    skipped["status"] = "skipped"
    info = ui_call("pushInfo(%s, null)" % json.dumps(skipped))
    assert "still on your Garmin calendar" in info["text"]
    assert "Garmin Connect" in info["hint"]
    assert "remove" not in info

    # And not said where it would be false: nothing of ours is there.
    clear = a_workout(push_state="failed", error="Not sent.")
    assert ui_call("pushInfo(%s, null)" % json.dumps(clear))["hint"] is None


def test_the_card_never_hands_back_a_clickable_removal():
    """Belt and braces on the pure layer: no `pushInfo` result may carry a key
    the renderer could turn into a Remove button, in any state the record can
    be in."""
    states = ["never", "scheduled", "drifted", "failed"]
    for state in states:
        for on_garmin in (True, False):
            for status in ("planned", "skipped"):
                w = a_workout(push_state=state, on_garmin=on_garmin,
                              workout_id=4242 if on_garmin else None,
                              error="Something went wrong.")
                w["status"] = status
                info = ui_call("pushInfo(%s, null)" % json.dumps(w))
                if info is None:
                    continue
                assert "remove" not in info, (state, on_garmin, status)
                assert "removeLabel" not in info, (state, on_garmin, status)


def test_a_refused_push_is_shown_in_the_writers_own_words():
    workout_json = json.dumps(a_workout(
        push_state="failed",
        error="Not sent: this app couldn't read your Garmin calendar for "
              "2026-08-04, so it can't tell whether a copy is already on it. "
              "Try again in a moment."))
    info = ui_call(f"pushInfo({workout_json}, null)")
    assert "couldn't read your Garmin calendar" in info["text"]
    assert info["retry"] is True


def test_the_unchanged_push_states_still_read_the_same_way():
    """The refusal branches are additions, not a rewrite."""
    assert ui_call(
        'pushInfo(%s, null)' % json.dumps(a_workout(
            workout_id=1, on_garmin=True, push_state="scheduled"))
    )["text"].startswith("✓ On Garmin")
    skipped = dict(a_workout(workout_id=1, on_garmin=True,
                             push_state="scheduled"), status="skipped")
    info = ui_call("pushInfo(%s, null)" % json.dumps(skipped))
    assert "Garmin Connect" in info["hint"]
    assert "still on your Garmin calendar" in info["text"]
    assert ui_call("pushInfo(%s, null)" % json.dumps(a_workout())) is None


def test_an_unreadable_garmin_day_does_not_read_as_a_clear_one():
    """The panel's one unforgivable answer is "nothing is on your calendar"
    when the truth is "this app has no idea". The day still has to carry a
    mark of its own — without it an unanswered day is drawn like a clear one."""
    panel = ui_call('garminPanel({"garmin": {"scheduled": [], '
                    '"scheduled_readable": false}})')
    assert panel["state"] == "unreadable"
    assert panel["text"] == "Garmin calendar unread"

    assert ui_call('garminPanel({"garmin": {"scheduled": [], '
                   '"scheduled_readable": true}})') is None


def test_an_unreadable_month_is_explained_once_and_not_seven_times():
    """The calendar is read a month at a time, so one failed read blanks every
    day on screen. The explanation and the way out belong in one banner."""
    days = ", ".join(
        '{"date": "2026-08-%02d", "garmin": {"scheduled_readable": false}}' % d
        for d in range(3, 10))
    notices = ui_call("calendarNotices({\"days\": [%s]})" % days)
    assert len(notices) == 1
    assert "Couldn't read your Garmin calendar" in notices[0]["title"]
    assert "not evidence the day is clear" in notices[0]["body"]
    assert "refused rather than risk a second copy" in notices[0]["body"]
    assert len(notices[0]["days"]) == 7

    assert ui_call('calendarNotices({"days": [{"date": "2026-08-03", '
                   '"garmin": {"scheduled_readable": true}}]})') == []
    assert ui_call("calendarNotices(null)") == []


def test_an_answer_the_page_cannot_recognise_is_drawn_as_no_answer():
    """`scheduled_readable` is set on every day the server emits, so anything
    else reaching this function means the page is not holding an answer — the
    field missing, null, a payload shape from a version that didn't have it.
    "I don't know" is the honest render for that; "you're clear" is not, and
    an `=== false` test quietly gives the second one."""
    for garmin in ('{}', '{"scheduled": []}', '{"scheduled_readable": null}',
                   '{"scheduled_readable": "yes"}'):
        panel = ui_call('garminPanel({"garmin": %s})' % garmin)
        assert panel and panel["state"] == "unreadable", garmin
        notices = ui_call(
            'calendarNotices({"days": [{"date": "2026-08-03", "garmin": %s}]})'
            % garmin)
        assert len(notices) == 1, garmin

    # ...and a real answer is still an answer.
    assert ui_call('garminPanel({"garmin": {"scheduled": [], '
                   '"scheduled_readable": true}})') is None


def test_the_day_strip_and_the_banner_never_disagree():
    """A day marked unread below the fold with no banner above it — or the
    reverse — is how one of these gets missed. Both ask the same question."""
    days = ('{"date": "2026-08-03", "garmin": {"scheduled_readable": true, '
            '"scheduled": []}}, '
            '{"date": "2026-08-04", "garmin": {"scheduled_readable": false, '
            '"scheduled": []}}')
    flagged = ui_call('unreadableDays({"days": [%s]})' % days)
    assert flagged == ["2026-08-04"]
    assert ui_call('garminPanel({"garmin": {"scheduled_readable": false, '
                   '"scheduled": []}})')["state"] == "unreadable"
    assert ui_call('garminPanel({"garmin": {"scheduled_readable": true, '
                   '"scheduled": []}})') is None


def test_the_garmin_panel_separates_our_copies_from_his_own():
    panel = ui_call(
        'garminPanel({"garmin": {"scheduled_readable": true, "scheduled": ['
        '{"title": "Threshold", "owned_by_us": true},'
        '{"title": "Club chaingang", "owned_by_us": false}]}})')
    assert panel["state"] == "read"
    assert panel["text"] == "2 sessions on Garmin"
    assert panel["rows"][0] == {"title": "Threshold", "ours": True,
                                "note": "sent from here"}
    assert panel["rows"][1]["ours"] is False
    assert "not this app's" in panel["rows"][1]["note"]


def test_the_page_tells_the_athlete_about_a_migration_orphan():
    """A dropped push identity leaves a session on his Garmin calendar that
    nothing here will ever touch again. It cannot live in /api/status and
    nowhere else — he is the only one who can remove it."""
    status = json.dumps({"store": {"migration_dropped": 1, "migration_notes": [
        {"uid": "orphan@tp", "title": "Threshold 3x12min",
         "scheduled_for": "2026-08-04",
         "message": "Threshold 3x12min on 2026-08-04 may still be on your "
                    "Garmin calendar. Check that day in Garmin Connect."}]}})
    notices = ui_call(f"storeNotices({status})")
    assert len(notices) == 1
    notice = notices[0]
    assert "can no longer manage" in notice["title"]
    assert "Garmin Connect" in notice["body"]
    assert notice["items"][0]["title"] == "Threshold 3x12min"
    assert notice["items"][0]["date"] == "2026-08-04"

    assert ui_call('storeNotices({"store": {"migration_dropped": 0, '
                   '"migration_notes": []}})') == []


def test_the_page_script_still_parses():
    """The page is one file with no build step, so a stray brace ships. The
    tests above run a slice of it; this runs the parser over the whole thing."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - dev dependency only
        pytest.skip("the UI-logic tests need node on PATH")
    with open(os.path.join(HERE, "static", "index.html"),
              encoding="utf-8") as fh:
        page = fh.read()
    script = page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                     delete=False) as fh:
        fh.write(script)
        checked = fh.name
    try:
        result = subprocess.run([node, "--check", checked],
                                capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(checked)
    assert result.returncode == 0, result.stderr


def test_an_unconfigured_garmin_account_is_not_reported_as_an_unread_calendar(
        client):
    """Demo mode, and any app with no Garmin login, has no calendar to read.
    Saying "couldn't read your Garmin calendar" there is a second, scarier
    version of the "Garmin isn't connected" banner that already covers it."""
    day = week2(1)
    block = calendar(client, day, day)["days"][0]["garmin"]
    assert block["scheduled"] == []
    assert block["scheduled_readable"] is True


def test_a_real_app_with_no_garmin_session_says_so_once_not_per_day(
        live_client, monkeypatch):
    """Same rule off the demo path: no session means the source banner, not a
    warning on every day in view."""
    monkeypatch.setattr(web_app.TrainingService, "garmin",
                        lambda self, force=False: None)
    garmin_reread(live_client)
    day = week2(1)
    block = calendar(live_client, day, day)["days"][0]["garmin"]
    assert block["scheduled"] == []
    assert block["scheduled_readable"] is True


def test_a_login_failure_is_not_reported_per_day_as_an_unread_calendar(
        live_client, monkeypatch):
    """A session-level failure is one reconnect banner, not a warning on every
    day in view. The writer keeps the two apart; so must this."""
    module, _ = web_app.load_module("garmin_workout_writer")
    writer = live_client.app.state.service.writer()

    def expired():
        raise module.GarminAuthFailed("your Garmin session has expired")

    # What an expired session really looks like: the calendar call never
    # happens, because the session behind it could not be re-established.
    monkeypatch.setattr(writer, "_ensure_api", expired)
    garmin_reread(live_client)
    day = week2(1)
    block = calendar(live_client, day, day)["days"][0]["garmin"]
    assert block["scheduled"] == []
    assert block["scheduled_readable"] is True, \
        "an expired login was reported as an unreadable calendar per day"


def test_a_card_with_nothing_on_garmin_and_nothing_to_say_stays_silent():
    """A stale removal warning used to survive here on a session no longer on
    the calendar. Nothing carries that state any more, and a workout with no
    push history and no copy on Garmin still earns no badge at all."""
    gone = a_workout(on_garmin=False, push_state="never")
    assert ui_call("pushInfo(%s, null)" % json.dumps(gone)) is None


def test_the_page_has_no_removal_code_left_in_it():
    """The endpoint is gone; the page must not still be calling it, and the
    two functions that did must not still be here waiting for a caller."""
    with open(os.path.join(HERE, "static", "index.html"),
              encoding="utf-8") as fh:
        page = fh.read()
    script = page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    # Comments explain why the feature is gone, and must be allowed to name
    # it. Code must not.
    code = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    for gone in ("removeFromGarmin", "removeBlockedReason", "/unpush"):
        assert gone not in code, f"{gone} is still live code on the page"


def help_drawer_source(script):
    """The body of `helpDrawer()`, from its opening brace to the matching one.

    Split on a bare "\n}" instead and the first nested arrow function ends the
    slice, which is how a copy test passes while the sentence it checks for
    sits twenty lines further down.
    """
    head = script.index("function helpDrawer()")
    start = script.index("{", head)
    depth = 0
    for i in range(start, len(script)):
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
            if depth == 0:
                return script[start:i + 1]
    raise AssertionError("helpDrawer() has no matching closing brace")


def test_the_page_says_plainly_that_undo_is_not_wired_up():
    """Multi-level undo works over HTTP and is deliberately not on the page:
    the store's undo is a per-uid stack, so undoing one half of an approved
    SWAP would put both sessions back on the same day — the exact double
    booking the swap was computed to avoid. Leaving it unwired is a choice,
    and an athlete hunting a menu for it deserves to be told."""
    with open(os.path.join(HERE, "static", "index.html"),
              encoding="utf-8") as fh:
        page = fh.read()
    script = page.split("<script>", 1)[1].rsplit("</script>", 1)[0]

    # Nothing on the page calls the undo endpoint — a one-click half-undo is
    # the thing that must not ship.
    code = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    assert "/api/history" not in code, \
        "undo is wired but the store still has no group-undo for swaps"

    # And it is said out loud, in the drawer the athlete opens with "?".
    drawer = help_drawer_source(script)
    assert "no undo button" in drawer.lower()
    assert "swap" in drawer.lower(), \
        "the reason has to be given, not just the absence"


def test_the_page_says_where_removal_actually_happens():
    """Same rule for the other deleted feature: the help drawer is where an
    athlete looks after failing to find a Remove button."""
    with open(os.path.join(HERE, "static", "index.html"),
              encoding="utf-8") as fh:
        page = fh.read()
    script = page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    drawer = help_drawer_source(script)
    # The same sentence as the cards, by reference and not by a near-copy that
    # can drift away from it.
    assert "GARMIN_REMOVES_IT" in drawer
    assert "Garmin Connect" in ui_logic_source(), \
        "the shared sentence stopped naming where removal happens"


# ---------------------------------------------------------------------------
# round 6 — the login the backoff didn't cover
#
# `TrainingService.writer()` cached a `GarminWorkoutWriter` built with
# `client=self.garmin()`, and `garmin()` returns None when the login failed or
# is inside its GARMIN_RETRY_SECONDS backoff. A writer holding `client=None`
# builds its own on first use: `_ensure_api` -> `_build_client` -> a fresh
# `GarminClient()`, a real login on whatever thread got there first, outside
# the throttle that exists to stop exactly that. `_scheduled_by_date` calls
# `writer()`, and a plain `GET /api/calendar` calls `_scheduled_by_date`.
# ---------------------------------------------------------------------------


def test_a_writer_is_never_built_without_a_garmin_session(live_client,
                                                          monkeypatch):
    """No session, no writer. The refusal is the point: a writer with
    `client=None` is one `_ensure_api` away from opening its own login."""
    monkeypatch.setattr(web_app.TrainingService, "garmin",
                        lambda self, force=False: None)
    service = live_client.app.state.service
    service._writer = None
    service._writer_client = None

    with pytest.raises(web_app.ApiError):
        service.writer()


def test_a_calendar_render_after_a_failed_login_opens_no_new_login(
        live_client, monkeypatch):
    """The reachable sequence, over real HTTP.

    Something asks for the writer while the login is down — `garmin()` answers
    None, because it failed a moment ago and is inside its backoff. The writer
    used to be built and CACHED with `client=None` at that instant. The login
    then recovers, the athlete's browser polls the calendar, and
    `_scheduled_by_date` gets that same cached writer back: `_ensure_api` sees
    no client, calls `_build_client`, and opens its own Garmin login — on a
    request thread, with nothing throttling it.
    """
    module, _ = web_app.load_module("garmin_workout_writer")
    logins = []

    def refuse_to_login():
        logins.append(1)
        raise AssertionError("a fresh Garmin login was opened")

    monkeypatch.setattr(module.GarminWorkoutWriter, "_build_client",
                        staticmethod(refuse_to_login))

    service = live_client.app.state.service
    service._writer = None
    service._writer_client = None

    # 1. the login is down and something reaches for the writer.
    session = live_client.garmin
    monkeypatch.setattr(web_app.TrainingService, "garmin",
                        lambda self, force=False: None)
    try:
        service.writer()
    except web_app.ApiError:
        pass  # the fix: no writer at all rather than one holding None

    # 2. the login comes back, and the browser polls the calendar.
    monkeypatch.setattr(web_app.TrainingService, "garmin",
                        lambda self, force=False: session)
    service._garmin_context = None
    service._garmin_context_at = 0.0
    day = week2(1)
    session.add(day, 1111, 2222, "Club chaingang")
    payload = calendar(live_client, day, day)

    assert logins == [], "the cached writer opened its own Garmin login"
    # And it worked: the writer was rebound to the live session, so the read
    # actually happened rather than being reported as blocked forever.
    block = payload["days"][0]["garmin"]
    assert block["scheduled_readable"] is True
    assert len(block["scheduled"]) == 1


def test_a_stale_writer_is_rebuilt_when_the_session_is_replaced(live_client):
    """A reconnect hands back a different client object. A writer still
    holding the old one would go on using a session nobody else has."""
    service = live_client.app.state.service
    first = service.writer()
    assert service.writer() is first, "the writer should be shared, not rebuilt"

    fresh = FakeGarminCalendar()
    service._garmin_client = fresh
    original = web_app.TrainingService.garmin
    try:
        web_app.TrainingService.garmin = lambda self, force=False: fresh
        second = service.writer()
    finally:
        web_app.TrainingService.garmin = original
    assert second is not first, "the writer kept a replaced Garmin session"


# ---------------------------------------------------------------------------
# round 6 — a failure indicator with no explanation under it
#
# `/api/calendar` built its `sources` block before it read Garmin, and a Garmin
# failure only becomes known by attempting the read. So the FIRST render of the
# calendar — the one the athlete actually looks at — reported the Garmin source
# as ok:false, stale:true, message:null: a red light with nothing next to it.
# From the second call on the sentence appeared.
# ---------------------------------------------------------------------------


def test_the_first_calendar_render_explains_a_garmin_failure(live_client):
    """One request, from a cold service. Whatever the source block says about
    Garmin, it has to say why."""
    service = live_client.app.state.service
    service._garmin_error = None
    service._garmin_context = None
    service._garmin_context_at = 0.0

    day = week2(1)
    garmin = calendar(live_client, day, day)["sources"]["garmin"]

    if not garmin["ok"]:
        assert garmin["message"], \
            "a failure indicator on the first render with nothing to explain it"


def test_the_explanation_does_not_wait_for_a_second_request(live_client):
    """The proof it was an ordering bug and not a timing one: the answer the
    second call gives is the answer the first one gives."""
    service = live_client.app.state.service
    service._garmin_error = None
    service._garmin_context = None
    service._garmin_context_at = 0.0

    day = week2(1)
    first = calendar(live_client, day, day)["sources"]["garmin"]
    service._garmin_context = None
    service._garmin_context_at = 0.0
    second = calendar(live_client, day, day)["sources"]["garmin"]

    assert first["ok"] == second["ok"]
    assert first["message"] == second["message"], \
        "the first render told a different story from the second"


# ---------------------------------------------------------------------------
# the hard guard: nothing in this suite may reach Garmin or TrainingPeaks
#
# Guarded at `garminconnect.Garmin`, not at this app's `GarminClient` wrapper.
# `web_app.load_module()` imports by name and the writer's `_build_client` does
# its own `from garmin_client import GarminClient`, so patching the wrapper
# protects only the paths that happen to go through the copy you patched. The
# constructor of the library that opens the socket is the one boundary every
# path has to cross.
# ---------------------------------------------------------------------------


@pytest.fixture
def no_outbound_calls(monkeypatch):
    attempts = []
    try:
        import garminconnect
    except Exception:  # pragma: no cover - library not installed here
        garminconnect = None
    if garminconnect is not None:
        def forbidden(*args, **kwargs):
            attempts.append("garminconnect.Garmin")
            raise AssertionError("outbound Garmin call attempted")
        monkeypatch.setattr(garminconnect, "Garmin", forbidden)
    import urllib.request

    def no_urlopen(*args, **kwargs):
        attempts.append("urllib.request.urlopen")
        raise AssertionError("outbound HTTP call attempted")

    monkeypatch.setattr(urllib.request, "urlopen", no_urlopen)
    yield attempts
    assert attempts == [], attempts


def test_a_full_calendar_render_makes_no_outbound_call(live_client,
                                                       no_outbound_calls):
    """The whole read path, over real HTTP, with the network nailed shut at
    the library boundary."""
    day = week2(1)
    live_client.garmin.add(day, 1111, 2222, "Club chaingang")
    garmin_reread(live_client)
    ok(live_client.get("/api/status"))
    calendar(live_client, week2(0), week2(6))
    ok(live_client.get("/api/history"))


def test_a_push_with_writes_on_makes_no_outbound_call(live_client, monkeypatch,
                                                      no_outbound_calls):
    """And the write path, with the env gate actually set — the fake account
    is the only thing it may ever reach."""
    monkeypatch.setenv(web_app.ALLOW_GARMIN_WRITES_ENV, "1")
    day = week2(1)
    uid = calendar(live_client, day, day)["days"][0]["workouts"][0]["uid"]
    approve_for_push(live_client, uid)
    job = push(live_client, start=day, end=day, dry_run=False)
    for _ in range(50):
        job = ok(live_client.get(f"/api/push/{job['job_id']}"))
        if job["state"] in ("done", "partial", "failed"):
            break
        time.sleep(0.05)
    assert job["state"] in ("done", "partial", "failed")
