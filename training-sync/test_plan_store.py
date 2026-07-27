"""Regression tests for `plan_store`: Garmin ids INHERITED from a v1 file.

Two defects lived in `_migrate`. Both are about ids the store did not create
in this schema and cannot vouch for:

DEFECT 1 — v2 proves ownership of a Garmin workout with a uid-derived marker
written into its name, but that proof was never required of ids already in the
state file. Round 2's adoption matched on the workout *title*, so a v1 file can
legitimately hold the id of a session the athlete built by hand. `_migrate`
copied every legacy `garmin` block forward verbatim, and because a round-2
adoption stored `pushed_hash=None`, the first post-upgrade push could never
take the `noop` branch: it took `update`, which unschedules the stored
schedule id — the athlete's own session, off their calendar.

DEFECT 2 — `PushIdentityConflict` is raised by `_check_id_ownership_locked`,
which only `record_push` reaches. Nothing validated ids on load, so a v1 file
where two same-titled sessions on one day were both adopted onto one schedule
id migrated intact. Pushing either one then unscheduled the other, while the
other's record went on reporting `push_state == "scheduled"`.

Nothing here touches the network. The writer is driven by a fake Garmin API
holding a small model of the calendar, so "the athlete's session is gone" is an
observable fact about that model rather than an assertion about a mock call.
"""

import copy
import hashlib
import json
import logging
import os
import shutil

import pytest

import garmin_workout_writer as writer_module
import plan_store as store_module
from plan_store import PlanStore

SUN = "2026-08-02"
MON = "2026-08-03"
TUE = "2026-08-04"

ATHLETE_TITLE = "Endurance 2hr Z2"
SHARED_TITLE = "Threshold 3x12min"


# --- fakes ------------------------------------------------------------------


class FakeGarminApi:
    """Stands in for `garminconnect.Garmin`, with a model of the calendar.

    Scheduling adds a row, unscheduling removes one, and two rows on a day is
    exactly the duplicate the Edge 520 would show. That makes the harm in both
    defects checkable directly: after the push, is the athlete's row still on
    the calendar?
    """

    def __init__(self, first_workout_id=999):
        self.calendar = []
        self.templates = {}
        self.calls = []
        self._next_workout_id = first_workout_id
        self._next_schedule_id = 6000

    # -- the athlete's own sessions, already on the calendar --

    def seed(self, workout_id, schedule_id, date, title):
        self.calendar.append({
            "workoutId": workout_id,
            "workoutScheduleId": schedule_id,
            "date": date,
            "title": title,
        })
        return {"workout_id": workout_id, "schedule_id": schedule_id}

    def row(self, schedule_id):
        return next((r for r in self.calendar
                     if r["workoutScheduleId"] == schedule_id), None)

    # -- the API surface the writer uses --

    def upload_workout(self, payload):
        workout_id = self._next_workout_id
        self._next_workout_id += 1
        self.templates[workout_id] = payload
        self.calls.append(("upload", workout_id, payload.get("workoutName")))
        return {"workoutId": workout_id}

    def schedule_workout(self, workout_id, date):
        schedule_id = self._next_schedule_id
        self._next_schedule_id += 1
        self.calls.append(("schedule", workout_id, date))
        self.calendar.append({
            "workoutId": workout_id,
            "workoutScheduleId": schedule_id,
            "date": date,
            "title": (self.templates.get(workout_id) or {}).get("workoutName"),
        })
        return {"workoutScheduleId": schedule_id}

    def unschedule_workout(self, schedule_id):
        self.calls.append(("unschedule", schedule_id))
        row = self.row(schedule_id)
        if row is None:
            raise RuntimeError(f"no scheduled workout {schedule_id}")
        self.calendar.remove(row)
        return {"ok": True}

    def get_scheduled_workouts(self, year, month):
        prefix = f"{int(year):04d}-{int(month):02d}"
        return {"calendarItems": [dict(row) for row in self.calendar
                                  if row["date"].startswith(prefix)]}


# --- v1 fixtures ------------------------------------------------------------


def _legacy_garmin(workout_id=None, schedule_id=None, date=None,
                   pushed_hash=None, push_state="scheduled", error=None):
    return {
        "workout_id": workout_id,
        "schedule_id": schedule_id,
        "scheduled_for": date,
        "pushed_at": "2026-07-20T08:00:00Z" if push_state == "scheduled" else None,
        "pushed_hash": pushed_hash,
        "push_state": push_state,
        "error": error,
    }


def v1_adopted_on_a_name(uid, workout_id, schedule_id, date, title):
    """The record round 2 wrote when it adopted a workout on a title match.

    A never-edited session, so round 2 kept it alive as a bare tombstone whose
    only content is the push identity — and `pushed_hash` is None, because the
    adopting process never sent the workout and could not know its content.
    """
    return {
        "uid": uid, "kind": "system", "rev": 0,
        "created_at": "2026-07-20T08:00:00Z",
        "updated_at": "2026-07-20T08:00:00Z",
        "note": None, "fields": {},
        "plan_snapshot": {"title": title, "date": date},
        "garmin": _legacy_garmin(workout_id, schedule_id, date),
        "orphaned_since": None,
        "tombstone": True,
    }


def v1_pushed_for_real(uid, workout_id, schedule_id, date, title,
                       pushed_hash="h1", fields=None):
    """A record this app really did write, with the hash to prove it."""
    return {
        "uid": uid, "kind": "user", "rev": 2,
        "created_at": "2026-07-19T08:00:00Z",
        "updated_at": "2026-07-20T09:30:00Z",
        "note": "moved off a work day",
        "fields": dict(fields or {"date": date, "status": "moved"}),
        "plan_snapshot": {"title": title, "date": date},
        "garmin": _legacy_garmin(workout_id, schedule_id, date,
                                 pushed_hash=pushed_hash),
        "orphaned_since": None,
    }


def write_v1(tmp_path, overrides, name="v1.json", **extra):
    payload = {
        "schema_version": 1,
        "updated_at": "2026-07-20T10:00:00Z",
        "day_availability": {},
        "workout_overrides": copy.deepcopy(overrides),
        "proposals": [],
        "rejected_signatures": [],
        "history": [],
    }
    payload.update(copy.deepcopy(extra))
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def writer_for(store, api):
    return writer_module.GarminWorkoutWriter(client=api, store=store)


def assert_no_shared_ids(store):
    """The invariant `_check_id_ownership_locked` enforces on write, asserted
    against whatever a load produced."""
    by_schedule = {}
    by_workout = {}
    for uid, entry in store.all_pushes().items():
        for value, index, label in ((entry.get("schedule_id"), by_schedule,
                                    "schedule"),
                                   (entry.get("workout_id"), by_workout,
                                    "workout")):
            if value is None:
                continue
            assert value not in index, (
                f"{label} id {value} is owned by both {index[value]!r} and "
                f"{uid!r}; unscheduling one removes the other's session")
            index[value] = uid


# --- DEFECT 1: an inherited id with no proof of authorship ------------------


def test_an_inherited_unproven_push_record_is_not_carried_forward(tmp_path):
    """DEFECT 1, at the store level.

    Round 2 adopted the athlete's hand-built 'Endurance 2hr Z2' on a title
    match. v2 cannot prove it wrote that workout — there is no marker in its
    name and no hash in the record — so the safe direction is to forget the
    ids. Forgetting risks a duplicate the athlete can see and delete; keeping
    risks silently removing a session they built themselves.
    """
    path = write_v1(tmp_path, {
        "uid-athlete": v1_adopted_on_a_name("uid-athlete", 1, 5000, SUN,
                                            ATHLETE_TITLE),
    })
    store = PlanStore(path=str(path))

    assert store.load_error is None, "a v1 file is not corrupt"
    assert store.get_push("uid-athlete") is None, (
        "an id adopted on a name match is not proof this app wrote the "
        "workout, so it must not be inherited")
    assert store.pushed_uids() == {}
    assert store.all_pushes() == {}
    assert store.stats()["pushed_count"] == 0
    # Nothing is left claiming Garmin holds a copy for that uid.
    assert store.get_override("uid-athlete") is None


@pytest.mark.parametrize("pushed_hash", [None, ""])
def test_an_inherited_id_with_a_blank_hash_is_unproven_either_way(tmp_path,
                                                                 pushed_hash):
    """A hash is a digest of what was sent. Blank is blank."""
    record = v1_adopted_on_a_name("uid-athlete", 1, 5000, SUN, ATHLETE_TITLE)
    record["garmin"]["pushed_hash"] = pushed_hash
    store = PlanStore(path=str(write_v1(tmp_path, {"uid-athlete": record})))
    assert store.get_push("uid-athlete") is None


def test_an_inherited_unproven_workout_id_alone_is_not_carried_forward(tmp_path):
    """Half an identity is still an identity: the update path recovers the
    schedule id from the calendar by workout id, so a workout id with no proof
    reaches the athlete's row just as surely."""
    path = write_v1(tmp_path, {
        "uid-athlete": v1_adopted_on_a_name("uid-athlete", 1, None, SUN,
                                            ATHLETE_TITLE),
    })
    store = PlanStore(path=str(path))
    assert store.get_push("uid-athlete") is None


def test_an_unproven_id_is_dropped_wherever_it_sits_in_the_file(tmp_path):
    """Which rules apply is a property of the *file*, not of whichever record
    the migration loop happened to look at last. A v1 file whose poisoned
    record is followed by ordinary ones must be judged the same way."""
    path = write_v1(tmp_path, {
        "uid-athlete": v1_adopted_on_a_name("uid-athlete", 1, 5000, SUN,
                                            ATHLETE_TITLE),
        "uid-never-pushed": {
            "uid": "uid-never-pushed", "kind": "user", "rev": 1,
            "created_at": "2026-07-19T08:00:00Z",
            "updated_at": "2026-07-19T08:00:00Z",
            "note": None, "fields": {"status": "skipped"},
            "plan_snapshot": None, "orphaned_since": None,
            # No `garmin` key at all — the commonest record in a real file.
        },
    })
    store = PlanStore(path=str(path))

    assert store.get_push("uid-athlete") is None
    assert store.get_override("uid-never-pushed")["fields"] == \
        {"status": "skipped"}


def test_a_push_after_the_upgrade_leaves_the_athletes_own_session_alone(tmp_path):
    """DEFECT 1, end to end, through the real writer and a fake calendar.

    Before the fix: `pushed_hash` was None, so `old_hash == new_hash` could
    never hold and the push took `update`, which unscheduled 5000. The
    athlete's session vanished from their calendar. Deterministic, not a race.
    """
    path = write_v1(tmp_path, {
        "uid-athlete": v1_adopted_on_a_name("uid-athlete", 1, 5000, SUN,
                                            ATHLETE_TITLE),
    })
    store = PlanStore(path=str(path))

    api = FakeGarminApi()
    api.seed(1, 5000, SUN, ATHLETE_TITLE)  # built by hand: no marker
    writer = writer_for(store, api)

    result = writer.push_workout("uid-athlete", ATHLETE_TITLE, SUN,
                                 duration_s=7200, store=store)

    assert result.state == "done"
    assert result.action == "create", (
        "with the unproven id forgotten there is nothing to update")
    assert ("unschedule", 5000) not in api.calls, (
        "5000 is the athlete's own session; this app never wrote it")
    assert api.row(5000) is not None, (
        "the athlete's hand-built session must still be on the calendar")
    assert result.schedule_id is not None and result.schedule_id != 5000
    # The app's own copy now carries the ownership marker, so the next push
    # can prove authorship without a title guess.
    assert writer_module.push_marker("uid-athlete") in \
        api.row(result.schedule_id)["title"]


def test_a_dry_run_over_an_inherited_unproven_id_touches_nothing(tmp_path):
    path = write_v1(tmp_path, {
        "uid-athlete": v1_adopted_on_a_name("uid-athlete", 1, 5000, SUN,
                                            ATHLETE_TITLE),
    })
    store = PlanStore(path=str(path))
    api = FakeGarminApi()
    api.seed(1, 5000, SUN, ATHLETE_TITLE)

    result = writer_for(store, api).push_workout(
        "uid-athlete", ATHLETE_TITLE, SUN, duration_s=7200, store=store,
        dry_run=True)

    assert result.action == "create"
    assert api.calls == [], "dry run never touches the network"


# --- DEFECT 2: two uids inherited onto one id ------------------------------


def test_inherited_records_colliding_on_one_schedule_id_are_disowned(tmp_path):
    """DEFECT 2, at the store level.

    Two same-titled sessions on one day; round 2's name adoption gave both the
    same ids. The migration used to keep both, carrying a state past the
    tripwire that `record_push` exists to make impossible.
    """
    path = write_v1(tmp_path, {
        "uid-one": v1_adopted_on_a_name("uid-one", 7, 5000, SUN, SHARED_TITLE),
        "uid-two": v1_adopted_on_a_name("uid-two", 7, 5000, SUN, SHARED_TITLE),
    })
    store = PlanStore(path=str(path))

    assert_no_shared_ids(store)
    for uid in ("uid-one", "uid-two"):
        entry = store.get_push(uid)
        assert entry is None or entry.get("push_state") != "scheduled", (
            f"{uid} must not keep reporting 'On Garmin' for a session this "
            "app cannot prove it owns")


def test_a_collision_between_two_proven_records_is_also_refused(tmp_path):
    """The collision rule stands on its own, independent of the missing-hash
    rule: two hashes cannot both be right about one calendar row."""
    path = write_v1(tmp_path, {
        "uid-one": v1_pushed_for_real("uid-one", 7, 5000, SUN, SHARED_TITLE,
                                      pushed_hash="h1"),
        "uid-two": v1_pushed_for_real("uid-two", 8, 5000, SUN, SHARED_TITLE,
                                      pushed_hash="h2"),
    })
    store = PlanStore(path=str(path))

    assert_no_shared_ids(store)
    assert store.get_push("uid-one") is None
    assert store.get_push("uid-two") is None
    # The user's own edits are not push identity and are never collateral.
    assert store.get_override("uid-one")["fields"]["status"] == "moved"
    assert store.get_override("uid-two")["fields"]["status"] == "moved"


def test_a_collision_on_the_workout_id_alone_is_refused(tmp_path):
    path = write_v1(tmp_path, {
        "uid-one": v1_pushed_for_real("uid-one", 7, 5000, SUN, SHARED_TITLE,
                                      pushed_hash="h1"),
        "uid-two": v1_pushed_for_real("uid-two", 7, 5001, SUN, SHARED_TITLE,
                                      pushed_hash="h2"),
    })
    store = PlanStore(path=str(path))
    assert_no_shared_ids(store)
    assert store.get_push("uid-one") is None
    assert store.get_push("uid-two") is None


def test_pushing_one_of_two_collided_sessions_spares_the_other(tmp_path):
    """DEFECT 2, end to end.

    Before the fix: pushing uid-one to a new date unscheduled 5000 — the row
    uid-two was still advertising as 'On Garmin' — and nothing anywhere said
    so.
    """
    path = write_v1(tmp_path, {
        "uid-one": v1_adopted_on_a_name("uid-one", 7, 5000, SUN, SHARED_TITLE),
        "uid-two": v1_adopted_on_a_name("uid-two", 7, 5000, SUN, SHARED_TITLE),
    })
    store = PlanStore(path=str(path))

    api = FakeGarminApi()
    api.seed(7, 5000, SUN, SHARED_TITLE)
    writer = writer_for(store, api)

    result = writer.push_workout("uid-one", SHARED_TITLE, MON,
                                 duration_s=3600, store=store)

    assert result.state == "done"
    assert ("unschedule", 5000) not in api.calls
    assert api.row(5000) is not None, (
        "pushing one session must not remove the other from the calendar")
    two = store.get_push("uid-two")
    assert two is None or two.get("push_state") != "scheduled"


def test_a_third_push_onto_a_collided_id_is_still_refused(tmp_path):
    """Disowning the collision must not leave the tripwire disarmed."""
    path = write_v1(tmp_path, {
        "uid-one": v1_pushed_for_real("uid-one", 7, 5000, SUN, SHARED_TITLE,
                                      pushed_hash="h1"),
        "uid-two": v1_pushed_for_real("uid-two", 8, 5000, SUN, SHARED_TITLE,
                                      pushed_hash="h2"),
    })
    store = PlanStore(path=str(path))
    store.record_push("uid-three", workout_id=11, schedule_id=22,
                      scheduled_for=SUN, pushed_hash="h3")
    with pytest.raises(store_module.PushIdentityConflict):
        store.record_push("uid-four", workout_id=12, schedule_id=22,
                          scheduled_for=SUN, pushed_hash="h4")


# --- the rules must not cost a legitimate record ---------------------------


def test_a_proven_record_survives_an_unproven_claim_on_its_ids(tmp_path):
    """Rule order matters. The unproven adoption goes first, which leaves the
    proven record alone rather than dropping both sides of a collision the
    unproven entry created."""
    path = write_v1(tmp_path, {
        "uid-real": v1_pushed_for_real("uid-real", 7, 5000, SUN, SHARED_TITLE,
                                       pushed_hash="h1"),
        "uid-fake": v1_adopted_on_a_name("uid-fake", 7, 5000, SUN,
                                         SHARED_TITLE),
    })
    store = PlanStore(path=str(path))

    assert store.get_push("uid-fake") is None
    real = store.get_push("uid-real")
    assert real["workout_id"] == 7 and real["schedule_id"] == 5000
    assert real["pushed_hash"] == "h1"
    assert real["push_state"] == "scheduled"
    assert_no_shared_ids(store)


def test_ordinary_v1_migration_is_still_lossless_field_by_field(tmp_path):
    """Every legitimate record keeps every field, and no rule fires."""
    availability = {SUN: {"state": "unavailable", "note": "Work",
                          "max_minutes": None,
                          "updated_at": "2026-07-20T09:00:00Z"}}
    proposals = [{"proposal_id": "prp_1", "uid": "u-edit", "status": "pending",
                  "from_date": SUN, "to_date": MON,
                  "created_at": "2026-07-19T08:00:00Z",
                  "decided_at": None, "decision_note": None}]
    signatures = [{"uid": "u-x", "signature": "move:->",
                   "rejected_at": "2026-07-19T08:00:00Z"}]
    history = [{"entry_id": "evt_1", "at": "2026-07-19T08:00:00Z",
                "action": "move", "uid": "u-edit", "actor": "user",
                "before": None, "after": {"uid": "u-edit"},
                "undoable": True}]

    overrides = {
        # a pushed, edited workout
        "u-edit": v1_pushed_for_real("u-edit", 111, 222, MON,
                                     "Threshold 3x12min", pushed_hash="h1"),
        # a v1 tombstone: nothing but push identity
        "u-tomb": {
            "uid": "u-tomb", "kind": "system", "rev": 9,
            "created_at": "2026-07-18T08:00:00Z",
            "updated_at": "2026-07-20T09:40:00Z",
            "note": None, "fields": {},
            "plan_snapshot": {"title": ATHLETE_TITLE, "date": SUN},
            "garmin": _legacy_garmin(333, 444, SUN, pushed_hash="h2"),
            "orphaned_since": None, "tombstone": True,
        },
        # an override that was never pushed
        "u-plain": {
            "uid": "u-plain", "kind": "user", "rev": 1,
            "created_at": "2026-07-19T08:00:00Z",
            "updated_at": "2026-07-19T08:00:00Z",
            "note": None, "fields": {"status": "skipped"},
            "plan_snapshot": None,
            "garmin": _legacy_garmin(push_state="never"),
            "orphaned_since": None,
        },
        # a push that failed before Garmin gave out any id: no ids to launder,
        # and the error is what the UI shows the athlete.
        "u-failed": {
            "uid": "u-failed", "kind": "user", "rev": 1,
            "created_at": "2026-07-19T08:00:00Z",
            "updated_at": "2026-07-19T08:10:00Z",
            "note": None, "fields": {"push_approved": True},
            "plan_snapshot": {"title": "VO2 5x4min", "date": TUE},
            "garmin": _legacy_garmin(push_state="failed",
                                     error="Garmin rejected the workout"),
            "orphaned_since": None,
        },
    }
    path = write_v1(tmp_path, overrides, day_availability=availability,
                    proposals=proposals, rejected_signatures=signatures,
                    history=history)

    store = PlanStore(path=str(path))
    assert store.load_error is None
    assert store.migration_notes == [], "nothing here is unproven or collided"
    assert store.stats()["migration_dropped"] == 0

    # Push identity, field by field.
    assert store.get_push("u-edit") == {
        "workout_id": 111, "schedule_id": 222, "scheduled_for": MON,
        "pushed_at": "2026-07-20T08:00:00Z", "pushed_hash": "h1",
        "push_state": "scheduled", "error": None,
    }
    assert store.get_push("u-tomb") == {
        "workout_id": 333, "schedule_id": 444, "scheduled_for": SUN,
        "pushed_at": "2026-07-20T08:00:00Z", "pushed_hash": "h2",
        "push_state": "scheduled", "error": None,
    }
    assert store.get_push("u-plain") is None, "never pushed, no identity"
    assert store.get_push("u-failed") == {
        "workout_id": None, "schedule_id": None, "scheduled_for": None,
        "pushed_at": None, "pushed_hash": None, "push_state": "failed",
        "error": "Garmin rejected the workout",
    }
    assert set(store.pushed_uids()) == {"u-edit", "u-tomb"}

    # The user's half, field by field.
    for uid in ("u-edit", "u-plain", "u-failed"):
        record = store.get_override(uid)
        source = overrides[uid]
        for key in ("uid", "kind", "rev", "created_at", "updated_at", "note",
                    "fields", "plan_snapshot", "orphaned_since"):
            assert record[key] == source[key], f"{uid}.{key} was not preserved"
        assert "garmin" in record and "tombstone" not in record
    assert "u-tomb" not in store.snapshot()["workout_overrides"]

    # Everything else in the file is untouched.
    snapshot = store.snapshot()
    assert snapshot["day_availability"] == availability
    assert snapshot["proposals"] == proposals
    assert snapshot["rejected_signatures"] == signatures
    assert snapshot["history"] == history
    assert snapshot["schema_version"] == 2

    # And it round-trips.
    store.save()
    again = PlanStore(path=str(path))
    assert again.all_pushes() == store.all_pushes()
    assert again.snapshot()["workout_overrides"] == \
        snapshot["workout_overrides"]
    assert again.migration_notes == []


def test_a_marker_proven_adoption_written_by_v2_survives_a_reload(tmp_path):
    """The runtime adoption path sets `pushed_hash=None` deliberately, having
    proved ownership by the marker instead. That is a v2 record and reloading
    it must not forget the copy on the calendar — otherwise a restart would
    strand it."""
    api = FakeGarminApi()
    seeded = api.seed(
        321, 5000, SUN,
        writer_module.marked_workout_name(ATHLETE_TITLE, "uid-ours"))
    path = tmp_path / "v2.json"
    store = PlanStore(path=str(path))
    writer = writer_for(store, api)

    adopted = writer.push_workout("uid-ours", ATHLETE_TITLE, SUN,
                                  duration_s=7200, store=store)
    assert adopted.action == "noop", "the marker proves this app wrote it"
    assert store.get_push("uid-ours")["pushed_hash"] is None

    again = PlanStore(path=str(path))
    assert again.snapshot()["schema_version"] == 2
    assert again.get_push("uid-ours")["workout_id"] == seeded["workout_id"]
    assert again.get_push("uid-ours")["schedule_id"] == seeded["schedule_id"]
    assert again.migration_notes == []


# --- visibility ------------------------------------------------------------


def test_what_the_migration_dropped_is_surfaced_not_silent(tmp_path, caplog):
    path = write_v1(tmp_path, {
        "uid-athlete": v1_adopted_on_a_name("uid-athlete", 1, 5000, SUN,
                                            ATHLETE_TITLE),
        "uid-one": v1_pushed_for_real("uid-one", 7, 7000, MON, SHARED_TITLE,
                                      pushed_hash="h1"),
        "uid-two": v1_pushed_for_real("uid-two", 8, 7000, MON, SHARED_TITLE,
                                      pushed_hash="h2"),
    })
    with caplog.at_level(logging.WARNING, logger=store_module.__name__):
        store = PlanStore(path=str(path))

    notes = store.migration_notes
    assert len(notes) == 3
    by_uid = {note["uid"]: note for note in notes}
    assert set(by_uid) == {"uid-athlete", "uid-one", "uid-two"}
    assert by_uid["uid-athlete"]["reason"] == "unproven_inherited_id"
    assert by_uid["uid-athlete"]["schedule_id"] == 5000
    assert by_uid["uid-athlete"]["workout_id"] == 1
    assert by_uid["uid-one"]["reason"] == "id_collision"
    assert by_uid["uid-two"]["reason"] == "id_collision"
    assert by_uid["uid-one"]["conflict_with"] == ["uid-two"]

    assert store.stats()["migration_dropped"] == 3
    assert [note["uid"] for note in store.stats()["migration_notes"]] == \
        [note["uid"] for note in notes]

    # A log line names the workout and the ids, so someone reading the
    # process output can go and look at the calendar.
    warnings = [record for record in caplog.records
                if record.levelno >= logging.WARNING]
    assert len(warnings) == 3
    logged = caplog.text
    assert all(uid in logged for uid in by_uid)
    assert "5000" in logged and "7000" in logged

    # Visible in the audit log the UI already renders, and never undoable.
    actions = [entry for entry in store.history(limit=20)
               if entry["action"] == "migration_drop_push"]
    assert {entry["uid"] for entry in actions} == set(by_uid)
    assert all(entry["undoable"] is False for entry in actions)
    assert all(entry["before"] is not None and entry["after"] is None
               for entry in actions)

    # The record of the drop persists with the file rather than living only in
    # this process, and a second load has nothing left to drop.
    store.save()
    again = PlanStore(path=str(path))
    assert again.migration_notes == []
    assert [e for e in again.history(limit=20)
            if e["action"] == "migration_drop_push"]


def test_a_dropped_record_leaves_the_store_writable_and_consistent(tmp_path):
    path = write_v1(tmp_path, {
        "uid-athlete": v1_adopted_on_a_name("uid-athlete", 1, 5000, SUN,
                                            ATHLETE_TITLE),
    })
    store = PlanStore(path=str(path))
    store.save()

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 2
    assert on_disk["garmin_push"] == {}
    assert "uid-athlete" not in on_disk["workout_overrides"]

    # A fresh, proven push for the same uid is unaffected by the drop.
    pushed = store.record_push("uid-athlete", workout_id=999, schedule_id=6000,
                               scheduled_for=SUN, pushed_hash="fresh")
    assert pushed["schedule_id"] == 6000
    assert store.stats()["pushed_count"] == 1


# --- the athlete's real state file ----------------------------------------


def test_the_real_state_file_is_unaffected_and_never_touched(tmp_path):
    """The shipped `.plan_state.json` is real data: schema_version 1 with no
    push identity in it at all, so neither rule has anything to act on.

    Backed up before the store opens it and restored byte-for-byte if anything
    in this test ever changes it.
    """
    real = os.path.join(
        os.path.dirname(os.path.abspath(store_module.__file__)),
        ".plan_state.json")
    if not os.path.exists(real):
        pytest.skip("no live state file in this checkout")

    backup = str(tmp_path / "plan_state.backup.json")
    shutil.copy2(real, backup)
    with open(backup, "rb") as fh:
        original = fh.read()
    digest = hashlib.sha256(original).hexdigest()

    try:
        # Read-only: `load` never writes, and nothing here calls `save`.
        store = PlanStore(path=real)
        assert store.load_error is None
        assert store.migration_notes == []
        assert store.all_pushes() == {}
        assert store.snapshot()["workout_overrides"] == {}

        # A copy may be migrated and saved freely; the original may not.
        copied = tmp_path / "real-copy.json"
        shutil.copyfile(real, copied)
        on_copy = PlanStore(path=str(copied))
        on_copy.save()
        after = json.loads(copied.read_text(encoding="utf-8"))
        before = json.loads(original.decode("utf-8"))
        assert after["day_availability"] == before["day_availability"]
        assert after["proposals"] == before["proposals"]
        assert after["rejected_signatures"] == before["rejected_signatures"]
        assert after["history"] == before["history"]
        assert after["workout_overrides"] == before["workout_overrides"]
        assert after["garmin_push"] == {}
        assert after["schema_version"] == 2
    finally:
        with open(real, "rb") as fh:
            now = fh.read()
        if hashlib.sha256(now).hexdigest() != digest:
            shutil.copy2(backup, real)
            raise AssertionError(
                "the athlete's real state file was modified; restored from "
                "the backup taken at the start of this test")
