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
