"""Regression tests for the Garmin write path.

Run:  python3 -m pytest test_garmin_writer.py -v

Everything here runs against a fake `garminconnect.Garmin` that keeps a small
model of the Garmin calendar, so the create / update / reschedule / adopt paths
are exercised for real without a single network call. Two switches reproduce
Garmin's flakier days: `schedule_returns_id=False` answers a schedule call
without the new id, and `expose_schedule_ids=False` also hides it from the
calendar listing — the combination under which the writer genuinely cannot
address a copy it put there.

The three defects locked down here:

DEFECT 3  a month read cached for the life of the process let a push decide a
          copy was already on the calendar when the athlete had deleted it —
          zero copies on the Edge, job reporting success.
DEFECT 4  an update whose old copy had no reachable schedule id scheduled a
          second copy and said nothing.
DEFECT 8  titles read back off the calendar carried the `[ts:xxxxxxxx]`
          ownership marker into anything that displayed them.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import garmin_workout_writer as writer_module  # noqa: E402
from plan_store import PlanStore  # noqa: E402

# Same week the rest of the suite pins, and a date whose month (2026-08) is the
# one the calendar render caches in the reproduction.
SAT, SUN = "2026-08-01", "2026-08-02"
AUG = (2026, 8)


class FakeGarminApi:
    """Stands in for `garminconnect.Garmin`.

    Scheduling adds a calendar row, unscheduling removes one, and a duplicate
    shows up as two rows on one day — which is what the Edge 520 would show.
    """

    def __init__(self, first_workout_id=999):
        self.calendar = []
        self.templates = {}
        self.calls = []
        self.schedule_returns_id = True
        self.expose_schedule_ids = True
        self.fail_listing = False
        self._next_workout_id = first_workout_id
        self._next_schedule_id = 5000

    def upload_workout(self, payload):
        workout_id = self._next_workout_id
        self._next_workout_id += 1
        self.templates[workout_id] = payload
        self.calls.append(("upload", workout_id))
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

    def get_scheduled_workouts(self, year, month):
        prefix = f"{int(year):04d}-{int(month):02d}"
        self.calls.append(("list", prefix))
        if self.fail_listing:
            raise RuntimeError("Garmin calendar is unavailable")
        items = []
        for row in self.calendar:
            if not row["date"].startswith(prefix):
                continue
            row = dict(row)
            if not self.expose_schedule_ids:
                row.pop("workoutScheduleId")
            items.append(row)
        return {"calendarItems": items}

    # -- what the assertions read --

    def days(self):
        return sorted(row["date"] for row in self.calendar)

    def rows_on(self, date):
        return [r for r in self.calendar if r["date"] == date]

    def listings(self):
        return [c for c in self.calls if c[0] == "list"]

    def writes(self):
        return [c for c in self.calls if c[0] in ("upload", "schedule",
                                                  "unschedule")]


class NestedCalendarApi(FakeGarminApi):
    """The same calendar in Garmin's other envelope: one dated node per day
    holding that day's rows as siblings."""

    def get_scheduled_workouts(self, year, month):
        prefix = f"{int(year):04d}-{int(month):02d}"
        self.calls.append(("list", prefix))
        if self.fail_listing:
            raise RuntimeError("Garmin calendar is unavailable")
        by_date = {}
        for row in self.calendar:
            if not row["date"].startswith(prefix):
                continue
            row = dict(row)
            day = row.pop("date")
            if not self.expose_schedule_ids:
                row.pop("workoutScheduleId", None)
            by_date.setdefault(day, []).append(row)
        return {"calendarItems": [{"scheduleDate": day, "items": rows}
                                  for day, rows in sorted(by_date.items())]}


def fresh_store(tmp_path, name="state.json"):
    return PlanStore(path=str(tmp_path / name))


def writer_for(store, api=None):
    api = api or FakeGarminApi()
    writer = writer_module.GarminWorkoutWriter(client=api, store=store)
    return writer, api


def push_threshold(writer, store, date, uid="u1", duration_s=3600, **kwargs):
    return writer.push_workout(uid, "Threshold 3x12min", date,
                               duration_s=duration_s, store=store, **kwargs)


# --- DEFECT 3: a skipped push may never rest on a stale month read ----------


def test_a_stale_month_read_can_never_decide_a_copy_is_already_there(tmp_path):
    """DEFECT 3 — zero copies on the device, job reporting success.

    The writer is built once per service and lives for the life of the
    process, so the month it cached during a calendar render was still being
    trusted long afterwards. With the store's ids gone and the workout deleted
    in Garmin Connect, the adopt guard — now the only thing standing between a
    push and a duplicate — read that cache, decided a copy was already there
    and reported "done" with nothing on the calendar at all.
    """
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)

    first = push_threshold(writer, store, SAT)
    assert (first.action, first.state) == ("create", "done")

    # A calendar render reads the month the workout sits in.
    writer.scheduled_in_month(*AUG)

    # The store loses its ids, and the athlete deletes the workout in Garmin.
    store.clear_push("u1")
    api.calendar.clear()

    second = push_threshold(writer, store, SAT)

    assert api.days() == [SAT], "the session has to actually be on the calendar"
    assert second.action == "create", (
        "a stale month read decided a copy was already there: "
        f"{second.action} / {second.reason}")
    assert second.state == "done"
    assert second.workout_id and second.workout_id in api.templates
    assert writer_module.owns_workout(api.rows_on(SAT)[0]["title"], "u1")


def test_a_leftover_copy_that_is_really_there_is_still_adopted(tmp_path):
    """The other half of the same guard, which must not be lost to the fix:
    when the copy *is* on the calendar, a re-push adopts it instead of
    stacking a second session on the Edge."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    store.clear_push("u1")            # only the local ids are gone

    again = push_threshold(writer, store, SAT)

    assert again.action == "noop"
    assert again.state == "done"
    assert api.days() == [SAT], "no second copy"
    assert again.workout_id == 999 and again.schedule_id == 5000
    assert store.get_push("u1")["pushed_hash"] is None, (
        "an adopted copy's content is unknown, so the next push must rewrite it")


def test_an_unmarked_workout_on_the_day_is_never_adopted(tmp_path):
    """Ownership still comes from the marker alone. A workout the athlete
    built by hand is not ours to claim, even on a fresh read."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    hand_built = api.upload_workout({"workoutName": "Threshold 3x12min"})
    api.schedule_workout(hand_built["workoutId"], SAT)

    result = push_threshold(writer, store, SAT)

    assert result.action == "create"
    assert result.workout_id != hand_built["workoutId"]
    assert len(api.rows_on(SAT)) == 2, "ours goes beside theirs, never over it"


def test_a_month_read_is_cached_only_briefly(tmp_path, monkeypatch):
    """The cache's docstring promised the life of a request; its real
    lifetime was the life of the process. A short TTL is what makes the two
    agree for a writer the web layer never rebuilds."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(writer_module, "_monotonic", lambda: clock["t"])
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)

    ttl = writer_module.SCHEDULE_CACHE_TTL
    assert 0 < ttl <= 60, "a month read may not be trusted for long"

    writer.scheduled_in_month(*AUG)
    assert len(api.listings()) == 1
    writer.scheduled_in_month(*AUG)
    assert len(api.listings()) == 1, "within the TTL one read is enough"

    clock["t"] += ttl + 0.1
    writer.scheduled_in_month(*AUG)
    assert len(api.listings()) == 2, "after the TTL the calendar is re-read"


def test_the_month_cache_can_be_dropped_from_outside(tmp_path, monkeypatch):
    """`POST /api/refresh` means "your picture of Garmin is stale". The writer
    outlives the request, so it has to expose a way to be told."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(writer_module, "_monotonic", lambda: clock["t"])
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)

    writer.scheduled_in_month(*AUG)
    writer.invalidate_schedule_cache()
    writer.scheduled_in_month(*AUG)

    assert len(api.listings()) == 2


# --- DEFECT 4: never two copies in silence ---------------------------------


def _push_without_a_schedule_id(writer, store, api, date=SAT):
    """A first push during which Garmin never reveals the schedule id."""
    api.schedule_returns_id = False
    api.expose_schedule_ids = False
    first = push_threshold(writer, store, date)
    assert first.state == "done"
    assert store.get_push("u1")["schedule_id"] is None, "precondition"
    return first


@pytest.mark.parametrize("api_class", [FakeGarminApi, NestedCalendarApi])
def test_an_update_that_cannot_address_the_old_copy_never_leaves_two(
        tmp_path, api_class):
    """DEFECT 4 — two copies, silently.

    Garmin can accept a schedule call without returning the id and then not
    expose it in the listing either. The first push therefore stored
    `schedule_id=None`. The next content change uploaded and scheduled a
    second copy, found nothing to unschedule, and said nothing: two sessions
    on one day on the athlete's Edge, reported as a clean success.

    Either outcome is acceptable — one copy, or a conflict the athlete can
    see and resolve — but never two in silence.
    """
    store = fresh_store(tmp_path)
    writer, api = writer_for(store, api_class())
    _push_without_a_schedule_id(writer, store, api)
    writes_before = list(api.writes())

    changed = push_threshold(writer, store, SAT, duration_s=5400)

    assert len(api.rows_on(SAT)) == 1, (
        f"two copies on {SAT}: {api.rows_on(SAT)}")
    assert changed.state == "failed", (
        "refusing is the only way to keep one copy once the old one cannot "
        f"be unscheduled, got {changed.state} / {changed.reason}")
    assert api.writes() == writes_before, "a refusal writes nothing at all"

    # The athlete has to be told what to do about it, in their own terms.
    told = f"{changed.error or ''} {changed.reason or ''}"
    assert SAT in told
    assert "Garmin Connect" in told
    assert "two copies" in told or "second copy" in told
    assert "[ts:" not in told, "the ownership marker is not shown to a person"


def test_the_update_conflict_clears_once_the_old_copy_is_removed(tmp_path):
    """A conflict the athlete cannot get out of is just a different bug. Once
    the old copy is gone from Garmin Connect the same push goes through."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    _push_without_a_schedule_id(writer, store, api)
    assert push_threshold(writer, store, SAT, duration_s=5400).state == "failed"

    api.calendar.clear()              # "remove it in Garmin Connect"
    retry = push_threshold(writer, store, SAT, duration_s=5400)

    assert retry.state == "done"
    assert retry.action == "update"
    assert api.days() == [SAT] and len(api.rows_on(SAT)) == 1
    assert store.get_push("u1")["workout_id"] == retry.workout_id


def test_an_update_locates_the_old_copy_by_its_marker(tmp_path):
    """The store lost the schedule id but Garmin does expose it: the old copy
    is found through the ownership marker and unscheduled, so the update
    lands as one copy without bothering the athlete."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    store.record_push("u1", schedule_id=None, push_state="pending")

    changed = push_threshold(writer, store, SAT, duration_s=5400)

    assert changed.state == "done" and changed.action == "update"
    assert len(api.rows_on(SAT)) == 1, "the superseded copy came off"
    assert ("unschedule", 5000) in api.calls
    assert api.rows_on(SAT)[0]["workoutId"] == changed.workout_id
    assert 999 in api.templates, "the superseded template is never destroyed"


def test_an_update_says_so_when_it_cannot_check_the_old_copy(tmp_path):
    """No date on the push record means no month to read, so nothing can be
    proved either way. Proceeding is fine; proceeding in silence is not."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    store.record_push("u1", workout_id=4242, pushed_hash="stale-hash")
    assert store.get_push("u1")["scheduled_for"] is None, "precondition"

    changed = push_threshold(writer, store, SAT)

    assert changed.action == "update" and changed.state == "done"
    assert api.days() == [SAT]
    told = f"{changed.reason or ''} {changed.error or ''}"
    assert "Garmin Connect" in told, f"no warning at all: {told}"


def test_an_update_whose_unschedule_fails_still_warns(tmp_path):
    """The pre-existing warning path: Garmin refused the unschedule, so a
    previous copy may still be there and the athlete is told."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    api.calendar.clear()              # the stored id is dead on Garmin

    changed = push_threshold(writer, store, SAT, duration_s=5400)

    assert changed.state == "done"
    assert "Garmin Connect" in (changed.reason or "")


def test_a_move_that_cannot_address_the_old_copy_never_leaves_two(tmp_path):
    """The same shape one branch up: a reschedule with no reachable schedule
    id used to schedule the template on the new day and leave the old day's
    copy in place."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    _push_without_a_schedule_id(writer, store, api)

    moved = push_threshold(writer, store, SUN)

    assert api.days() in ([SAT], [SUN]), f"a copy on two days: {api.days()}"
    if moved.state == "done":
        assert api.days() == [SUN]
    else:
        assert api.days() == [SAT]
        assert "Garmin Connect" in (moved.error or "")


def test_a_move_that_cannot_check_the_old_copy_says_so(tmp_path):
    """Same silence, one branch up: a push record with ids but no date has no
    month to read, so the old copy's whereabouts are unknowable. The move
    still happens — it just stops claiming the old copy is gone."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    same_content = writer_module.workout_hash(
        writer_module.build_placeholder_workout(
            writer_module.marked_workout_name("Threshold 3x12min", "u1"),
            duration_s=3600))
    store.record_push("u1", workout_id=999, pushed_hash=same_content)
    assert store.get_push("u1")["scheduled_for"] is None, "precondition"

    moved = push_threshold(writer, store, SAT)

    assert moved.action == "reschedule" and moved.state == "done"
    assert api.days() == [SAT]
    assert "Garmin Connect" in (moved.reason or ""), f"no warning: {moved.reason}"


def test_a_dry_run_update_touches_nothing_even_when_conflicted(tmp_path):
    """dry_run never reaches the network — including the fresh reads and
    conflict checks added by these fixes."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    _push_without_a_schedule_id(writer, store, api)
    api.calls.clear()

    preview = push_threshold(writer, store, SAT, duration_s=5400, dry_run=True)

    assert preview.action == "update" and preview.state == "pending"
    assert preview.dry_run is True
    assert api.calls == [], f"dry run touched Garmin: {api.calls}"


# --- DEFECT 8: the marker never reaches a person ---------------------------


def test_a_calendar_title_is_available_without_the_ownership_marker(tmp_path):
    """DEFECT 8 — `strip_marker` was written, documented and tested, and
    nothing called it, so `Threshold 3x12min [ts:1a2b3c4d]` went to the UI.

    The module now hands out rows whose title is already clean, and the raw
    name alongside it so ownership can still be proved.
    """
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)

    rows = writer.scheduled_rows(*AUG)

    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Threshold 3x12min"
    assert "[ts:" not in row["title"]
    assert row["date"] == SAT
    assert row["workout_id"] == 999 and row["schedule_id"] == 5000
    assert writer_module.owns_workout(row["raw_title"], "u1"), \
        "the marked name is still available for ownership checks"
    assert row["marked"] is True


def test_a_hand_built_title_survives_the_cleanup_unchanged(tmp_path):
    """Only our own marker is removed. Somebody else's title is theirs."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    workout_id = api.upload_workout({"workoutName": "Coach's own [1h] ride"})
    api.schedule_workout(workout_id["workoutId"], SAT)

    row = writer.scheduled_rows(*AUG)[0]

    assert row["title"] == "Coach's own [1h] ride"
    assert row["marked"] is False


def test_display_title_cleans_both_a_row_and_a_bare_name():
    """The one call the web layer needs, in both shapes it has to hand."""
    marked = writer_module.marked_workout_name("Threshold 3x12min", "u1")
    assert writer_module.display_title(marked) == "Threshold 3x12min"
    assert writer_module.display_title(
        {"workoutName": marked}) == "Threshold 3x12min"
    assert writer_module.display_title(
        {"title": marked, "workoutScheduleId": 1}) == "Threshold 3x12min"
    assert writer_module.display_title({}) is None
    assert writer_module.display_title(None) is None


def test_scheduled_rows_reads_both_calendar_envelopes(tmp_path):
    """Rows come off the shape Garmin actually returned, and two sessions on
    one day stay two rows with their own ids — never one merged identity."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store, NestedCalendarApi())
    push_threshold(writer, store, SAT, uid="u1")
    push_threshold(writer, store, SAT, uid="u2")

    rows = sorted(writer.scheduled_rows(*AUG, refresh=True),
                  key=lambda r: r["workout_id"])

    assert [r["workout_id"] for r in rows] == [999, 1000]
    assert [r["schedule_id"] for r in rows] == [5000, 5001]
    assert all(r["date"] == SAT for r in rows)
    assert writer_module.owns_workout(rows[0]["raw_title"], "u1")
    assert writer_module.owns_workout(rows[1]["raw_title"], "u2")


# --- removal: a calendar it could not read is not an empty calendar --------


def test_removal_that_cannot_read_the_calendar_keeps_the_ids(tmp_path):
    """A removal clears the push record, so it must be certain first.

    `unpush_workout` recovered a missing schedule id through a helper that
    answers "nothing of ours is there" and "I could not read the calendar"
    with the same `None`. Taking that as "already gone" cleared the record —
    forgetting the only ids able to unschedule the copy, while the copy was
    still on the athlete's calendar and the app reported it removed. Nothing
    could remove it afterwards: the exact orphan the removal path exists to
    prevent.
    """
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    workout_id = store.get_push("u1")["workout_id"]
    store.record_push("u1", schedule_id=None)   # the id we have to recover
    api.fail_listing = True                     # ...and Garmin will not say

    result = writer.unpush_workout("u1", store=store)

    assert result.state == "failed", "a removal that removed nothing said done"
    assert ("unschedule", 5000) not in api.calls
    assert api.days() == [SAT], "the copy is still on the calendar"
    kept = store.get_push("u1")
    assert kept is not None, "the ids that could still remove it were dropped"
    assert kept["workout_id"] == workout_id
    assert kept["push_state"] == "failed"
    assert "try again" in (result.error or "")


def test_removal_still_clears_when_the_copy_is_genuinely_gone(tmp_path):
    """The other half of the same branch: the calendar *was* read and holds
    no copy of ours, so there is nothing to unschedule and forgetting the ids
    is right. A read that succeeds must not be made pessimistic by the fix."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    store.record_push("u1", schedule_id=None)
    api.calendar.clear()                        # removed in Garmin Connect

    result = writer.unpush_workout("u1", store=store)

    assert result.state == "done"
    assert store.get_push("u1") is None
