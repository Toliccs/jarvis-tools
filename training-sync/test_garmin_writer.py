"""Regression tests for the Garmin write path.

Run:  python3 -m pytest test_garmin_writer.py -v

Everything here runs against a fake `garminconnect.Garmin` that keeps a small
model of the Garmin calendar, so the create / update / reschedule / adopt paths
are exercised for real without a single network call. Two switches reproduce
Garmin's flakier days: `schedule_returns_id=False` answers a schedule call
without the new id, and `expose_schedule_ids=False` also hides it from the
calendar listing — the combination under which the writer genuinely cannot
address a copy it put there.

`listing_payload` is the third switch, and the one this round turns on most:
it makes the fake answer a calendar read with an arbitrary 200-shaped body,
which is how Garmin returns an error, an empty envelope, or a shape nobody
here has seen before.

The defects locked down here:

DEFECT 3  a month read cached for the life of the process let a push decide a
          copy was already on the calendar when the athlete had deleted it —
          zero copies on the Edge, job reporting success.
DEFECT 4  an update whose old copy had no reachable schedule id scheduled a
          second copy and said nothing.
DEFECT 8  titles read back off the calendar carried the `[ts:xxxxxxxx]`
          ownership marker into anything that displayed them.
DEFECT D4 a create whose calendar read failed treated the day as clear.
DEFECT D6 the same, for every failure that came back looking like success: a
          rate-limited Garmin was indistinguishable from an empty calendar.
DEFECT D7 a stored schedule id went straight to `unschedule`, so an unmarked
          session of the athlete's own was taken off their calendar by an
          update, and moved to another day by a reschedule.
DEFECT D8 a no-op said "Already on Garmin" without reading the calendar, and
          went on saying it after the athlete deleted the session there.

There is no removal path to test any more, and no tests for one. Taking a
session off the Garmin calendar is done in Garmin Connect; `unpush_workout`
produced four separate reproduced defects across two rounds and is gone.
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

_UNSET = object()


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
        self.fail_unschedule = False
        # A 200-shaped answer to substitute for the real listing. `_UNSET`
        # rather than None because None is itself one of the answers under
        # test — an empty body is exactly the case that used to read as an
        # empty calendar.
        self.listing_payload = _UNSET
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
        if self.fail_unschedule:
            raise RuntimeError("Garmin refused the unschedule")
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
        if self.listing_payload is not _UNSET:
            return self.listing_payload
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
        if self.listing_payload is not _UNSET:
            return self.listing_payload
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
    writer.scheduled_rows(*AUG)

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

    writer.scheduled_rows(*AUG)
    assert len(api.listings()) == 1
    writer.scheduled_rows(*AUG)
    assert len(api.listings()) == 1, "within the TTL one read is enough"

    clock["t"] += ttl + 0.1
    writer.scheduled_rows(*AUG)
    assert len(api.listings()) == 2, "after the TTL the calendar is re-read"


def test_the_month_cache_can_be_dropped_from_outside(tmp_path, monkeypatch):
    """`POST /api/refresh` means "your picture of Garmin is stale". The writer
    outlives the request, so it has to expose a way to be told."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(writer_module, "_monotonic", lambda: clock["t"])
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)

    writer.scheduled_rows(*AUG)
    writer.invalidate_schedule_cache()
    writer.scheduled_rows(*AUG)

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
    """The pre-existing warning path: the old copy is provably ours and on the
    calendar, Garmin refuses to take it off, and the athlete is told it may
    still be there."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    api.fail_unschedule = True        # Garmin will not take it off

    changed = push_threshold(writer, store, SAT, duration_s=5400)

    assert changed.state == "done"
    assert ("unschedule", 5000) in api.calls
    assert "Garmin Connect" in (changed.reason or "")


def test_an_update_does_not_warn_about_a_copy_the_calendar_says_is_gone(
        tmp_path):
    """The same setup as it used to be written, and the honest answer to it.
    The stored schedule id is dead on Garmin, but the calendar was read and
    holds no copy of ours — so there is nothing to unschedule, nothing to warn
    about, and above all no id fired blind at `unschedule` on the off-chance."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    api.calendar.clear()              # the stored id is dead on Garmin

    changed = push_threshold(writer, store, SAT, duration_s=5400)

    assert changed.state == "done"
    assert not [c for c in api.calls if c[0] == "unschedule"], (
        "an id out of the store was fired at Garmin with nothing behind it")
    assert len(api.rows_on(SAT)) == 1
    assert "Garmin Connect" not in (changed.reason or ""), (
        f"warned about a copy it had proved was gone: {changed.reason}")


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


# --- D6: "unreadable" includes every answer that did not parse -------------
#
# The previous round fixed the exception-raising half of this and called it
# fixed at the source. It was not: `read_scheduled_rows` reported
# CALENDAR_BLOCKED only when the underlying call *raised*, so every 200-shaped
# answer that yielded no rows — an empty body, an error envelope, a shape whose
# date key this module does not know — arrived as `([], CALENDAR_READ)`, which
# says "Garmin answered and that month is empty". A rate-limited account was
# therefore indistinguishable from an athlete with nothing scheduled, and the
# create path's adopt guard, whose whole job is to not duplicate, uploaded a
# second copy onto a day it had never actually seen.


UNPARSEABLE_ANSWERS = [
    pytest.param(None, id="null-body"),
    pytest.param({}, id="empty-object"),
    pytest.param({"error": "rate limited", "statusCode": 429}, id="rate-limit"),
    pytest.param({"errorMessage": "Bad Gateway", "httpStatus": 502},
                 id="bad-gateway"),
    pytest.param(
        {"calendarItems": [{"workoutId": 999, "workoutScheduleId": 5000,
                            "workoutName": "Threshold 3x12min",
                            "scheduledOnDay": "2026-08-01"}]},
        id="unknown-date-key"),
    pytest.param({"calendarItems": "2026-08-01"}, id="not-a-collection"),
]


@pytest.mark.parametrize("answer", UNPARSEABLE_ANSWERS)
def test_an_answer_that_did_not_parse_is_never_an_empty_calendar(tmp_path,
                                                                 answer):
    """D6 — the source fix, for 200-shaped failures as well as raised ones.

    An error payload, a null or empty body and a shape this module does not
    understand are all "no answer about that month". None of them may come
    back as CALENDAR_READ, because CALENDAR_READ is a claim: it says the rows
    are the whole truth and an empty list means an empty month.
    """
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    api.listing_payload = answer

    with pytest.raises(writer_module.GarminWriterError):
        writer.scheduled_rows(*AUG, refresh=True)

    rows, status = writer.read_scheduled_rows(*AUG, refresh=True)
    assert (rows, status) == ([], writer_module.CALENDAR_BLOCKED), (
        f"{answer!r} was read as a calendar this app could see")


@pytest.mark.parametrize("answer", [
    pytest.param([], id="bare-empty-list"),
    pytest.param({"calendarItems": []}, id="empty-collection"),
    pytest.param({"calendarItems": [{"calendarDate": "2026-08-03",
                                     "items": []}]}, id="empty-day-node"),
])
def test_a_month_that_really_is_empty_still_reads_as_empty(tmp_path, answer):
    """The half that must not be lost to the fix. A well-formed answer holding
    no sessions is proof of an empty month, and a push that refused on it would
    be a writer that can never write."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    api.listing_payload = answer

    assert writer.read_scheduled_rows(*AUG, refresh=True) == (
        [], writer_module.CALENDAR_READ)


def test_a_wrapped_envelope_is_still_a_calendar(tmp_path):
    """Strictness must not become brittleness. Garmin's envelopes have changed
    shape before, and a body whose rows are one wrapper down is still an
    answer — refusing it would blank the panel and block every push on an
    account that works."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    api.listing_payload = {"payload": {"calendarItems": [
        {"workoutId": 999, "workoutScheduleId": 5000, "calendarDate": SAT,
         "workoutName": writer_module.marked_workout_name("Threshold", "u1")},
    ]}}

    rows, status = writer.read_scheduled_rows(*AUG, refresh=True)

    assert status == writer_module.CALENDAR_READ
    assert [(r["date"], r["workout_id"], r["schedule_id"]) for r in rows] == [
        (SAT, 999, 5000)]
    assert writer_module.owns_workout(rows[0]["raw_title"], "u1")


def test_a_body_with_no_telling_which_list_is_the_calendar_is_blocked(tmp_path):
    """Two lists side by side and no known key on either: there is no saying
    which one is the month, so there is no answer."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    api.listing_payload = {"alpha": [], "beta": []}

    assert writer.read_scheduled_rows(*AUG, refresh=True) == (
        [], writer_module.CALENDAR_BLOCKED)


def test_a_rate_limited_garmin_never_looks_like_a_clear_day(tmp_path):
    """D6, the consequence that reaches the athlete's Edge.

    A live push record, this app's copy on the calendar, and Garmin answering
    429 with a 200-shaped body. The adopt guard read that as "nothing is on
    that day" and uploaded a duplicate.
    """
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    store.clear_push("u1")                  # only the local ids are gone
    writes_before = list(api.writes())
    api.listing_payload = {"error": "rate limited", "statusCode": 429}

    second = push_threshold(writer, store, SAT)

    assert len(api.rows_on(SAT)) == 1, (
        f"a duplicate went to the Edge: {api.rows_on(SAT)}")
    assert second.state == "failed", (
        "a rate-limited answer was taken for an empty calendar: "
        f"{second.action} / {second.state} / {second.reason}")
    assert api.writes() == writes_before, "a refusal writes nothing at all"
    told = f"{second.error or ''} {second.reason or ''}"
    assert SAT in told and "again" in told
    assert "[ts:" not in told


def test_an_unparseable_answer_is_not_cached_as_a_month(tmp_path):
    """A body that is not an answer must not become a remembered answer: the
    next read has to go back to Garmin rather than reuse the failure."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    api.listing_payload = {"error": "rate limited", "statusCode": 429}
    assert writer.read_scheduled_rows(*AUG) == (
        [], writer_module.CALENDAR_BLOCKED)

    api.listing_payload = _UNSET
    rows, status = writer.read_scheduled_rows(*AUG)

    assert status == writer_module.CALENDAR_READ
    assert len(api.listings()) == 2, "the failed answer was cached"


# --- D7: a stored id locates a row; only the marker authorises writing on it


def _a_row_this_app_did_not_write(store, api, date, name, uid="u1",
                                  pushed_hash="whatever"):
    """The athlete's own session on the calendar, and a push record whose ids
    happen to point at it — a state file restored from another install, an id
    Garmin reused, a record that outlived the workout it described. Every such
    record carries a `pushed_hash`, so the store's migration validation passes
    it through."""
    theirs = api.upload_workout({"workoutName": name})["workoutId"]
    api.schedule_workout(theirs, date)
    row = api.rows_on(date)[-1]
    store.record_push(uid, workout_id=row["workoutId"],
                      schedule_id=row["workoutScheduleId"],
                      scheduled_for=date, pushed_hash=pushed_hash)
    return row


def test_an_update_never_unschedules_a_row_it_cannot_prove_it_wrote(tmp_path):
    """D7 — the athlete's own session taken off their calendar by a push.

    The removal path was taught that the ids in a push record say where to
    look and are not themselves permission. The update path was not: a stored
    schedule id went straight to `unschedule`, so an unmarked "Wednesday club
    run" answering to that id was removed from the athlete's calendar while
    the push reported a clean update.
    """
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    row = _a_row_this_app_did_not_write(store, api, SAT, "Wednesday club run")
    writes_before = list(api.writes())

    changed = push_threshold(writer, store, SAT, duration_s=5400)

    assert not [c for c in api.calls if c[0] == "unschedule"], (
        "a session this app cannot prove it wrote was unscheduled")
    assert api.rows_on(SAT) == [row], "the athlete's own session is gone"
    assert api.writes() == writes_before, "a refusal writes nothing at all"
    assert changed.state == "failed", (
        f"reported a write it had no right to make: {changed.reason}")
    kept = store.get_push("u1")
    assert kept is not None and kept["workout_id"] == row["workoutId"]
    told = f"{changed.error or ''} {changed.reason or ''}"
    assert SAT in told and "Garmin Connect" in told, told
    assert "[ts:" not in told


def test_a_move_never_touches_a_row_it_cannot_prove_it_wrote(tmp_path):
    """D7, the worse half. The reschedule branch unscheduled the stored id
    *and* re-scheduled that workout id onto the new day, moving a session this
    app had no evidence it wrote."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    same_content = writer_module.workout_hash(
        writer_module.build_placeholder_workout(
            writer_module.marked_workout_name("Threshold 3x12min", "u1"),
            duration_s=3600))
    row = _a_row_this_app_did_not_write(store, api, SAT, "Wednesday club run",
                                        pushed_hash=same_content)
    writes_before = list(api.writes())

    moved = push_threshold(writer, store, SUN)

    assert api.rows_on(SAT) == [row], "their session was moved off its day"
    assert api.rows_on(SUN) == [], "their workout was scheduled onto a new day"
    assert api.writes() == writes_before, "a refusal writes nothing at all"
    assert moved.state == "failed", (
        f"moved a session it could not prove it wrote: {moved.reason}")
    told = f"{moved.error or ''} {moved.reason or ''}"
    assert "Garmin Connect" in told, told


def test_the_write_paths_only_ever_unschedule_a_marked_row(tmp_path):
    """The positive statement of the same rule: the id that reaches
    `unschedule` comes off a calendar row carrying this app's marker, not out
    of the store. Here the store's schedule id points at somebody else's row
    and ours is elsewhere in the month — the right one still comes off."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)                  # ours: 999 / 5000
    theirs = api.upload_workout({"workoutName": "Club chaingang"})["workoutId"]
    api.schedule_workout(theirs, SAT)                   # theirs: 1000 / 5001
    store.record_push("u1", schedule_id=5001)           # the store is wrong

    changed = push_threshold(writer, store, SAT, duration_s=5400)

    assert changed.state == "done"
    assert ("unschedule", 5000) in api.calls
    assert ("unschedule", 5001) not in api.calls, "removed their session"
    assert theirs in [r["workoutId"] for r in api.rows_on(SAT)]
    assert changed.workout_id in [r["workoutId"] for r in api.rows_on(SAT)]


def test_a_stored_id_is_not_used_when_another_row_already_answers_to_it(
        tmp_path):
    """The stored id is a handle onto a row the marker has already proved is
    ours. When something else on the calendar is already answering to that id
    the handle is not ours to pull, whatever the record says."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)                  # ours: 999 / 5000
    theirs = api.upload_workout({"workoutName": "Club chaingang"})["workoutId"]
    api.schedule_workout(theirs, SAT)                   # theirs: 1000 / 5001
    # Garmin stops exposing schedule ids for our row only, and the record's id
    # happens to be the one their row answers to.
    api.calendar[0].pop("workoutScheduleId")
    store.record_push("u1", schedule_id=5001)
    writer.invalidate_schedule_cache()

    changed = push_threshold(writer, store, SAT, duration_s=5400)

    assert not [c for c in api.calls if c[0] == "unschedule"]
    assert changed.state == "failed", (
        f"pulled an id another session answers to: {changed.reason}")
    assert theirs in [r["workoutId"] for r in api.rows_on(SAT)]


def test_a_move_with_no_template_id_on_the_row_still_lands_as_one_copy(
        tmp_path):
    """Our row is provably ours and addressable, but Garmin lists it without
    the workout id — so there is no template to put back down on the new day.
    A fresh one goes up and the old row comes off: one copy, not two, and not
    a refusal either."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    api.calendar[0].pop("workoutId")        # listed without the template id
    writer.invalidate_schedule_cache()

    moved = push_threshold(writer, store, SUN)

    assert moved.state == "done"
    assert api.days() == [SUN], f"a copy was left behind: {api.days()}"
    assert ("unschedule", 5000) in api.calls
    assert writer_module.owns_workout(api.rows_on(SUN)[0]["title"], "u1")


def test_a_move_follows_a_copy_the_athlete_dragged_in_garmin_connect(tmp_path):
    """The recorded date says which month to read, not which day holds the
    truth. Dragging our copy to another day in Garmin Connect does not change
    who wrote it, and the next move must take that copy with it rather than
    leave it behind as a second session."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    api.calendar[0]["date"] = "2026-08-05"      # moved in Garmin Connect
    writer.invalidate_schedule_cache()

    moved = push_threshold(writer, store, SUN)

    assert moved.state == "done"
    assert api.days() == [SUN], f"a copy was left behind: {api.days()}"


# --- D8: a noop may not claim a copy it has not seen -----------------------


def test_a_noop_never_claims_a_copy_the_calendar_does_not_hold(tmp_path):
    """D8 — "job reports success, zero copies on the Edge".

    Push, delete the workout in Garmin Connect, push again. The second push
    compared the plan against its own push record, saw nothing had changed and
    answered "Already on Garmin for this date, unchanged" without looking at
    the calendar at all — while the athlete's calendar held nothing.
    """
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    api.calendar.clear()                    # deleted in Garmin Connect
    api.calls.clear()

    second = push_threshold(writer, store, SAT)

    assert "Already on Garmin" not in (second.reason or ""), (
        f"claimed a copy that is not there: {second.reason}")
    assert len(api.rows_on(SAT)) == 1, (
        "the session reached no calendar at all and the push said so was fine:"
        f" {second.action} / {second.state} / {second.reason}")
    assert second.state == "done"
    assert store.get_push("u1")["workout_id"] == second.workout_id
    assert writer_module.owns_workout(api.rows_on(SAT)[0]["title"], "u1")


def test_a_noop_that_can_see_its_copy_still_skips(tmp_path):
    """The half that must not be lost: unchanged content, unchanged day, and
    the copy provably on the calendar is a genuine no-op that writes
    nothing."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    writes_before = list(api.writes())

    second = push_threshold(writer, store, SAT)

    assert (second.action, second.state) == ("noop", "skipped")
    assert api.writes() == writes_before, "a no-op wrote to Garmin"
    assert len(api.rows_on(SAT)) == 1
    assert SAT in (second.reason or ""), second.reason
    assert second.workout_id == 999 and second.schedule_id == 5000


def test_a_noop_that_cannot_read_the_calendar_says_so_instead_of_claiming(
        tmp_path):
    """An unreadable calendar is not evidence the copy is there. Nothing is
    written either way, so this is not a refusal — it is a claim the module
    may not make, and the athlete is told which."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    writes_before = list(api.writes())
    api.listing_payload = {"error": "rate limited", "statusCode": 429}

    second = push_threshold(writer, store, SAT)

    assert second.state == "skipped" and second.action == "noop"
    assert api.writes() == writes_before, "a no-op wrote to Garmin"
    assert "Already on" not in (second.reason or ""), second.reason
    assert "couldn't" in (second.reason or "").lower(), second.reason
    assert store.get_push("u1")["push_state"] == "scheduled"


def test_a_noop_leaves_a_copy_the_athlete_moved_where_they_put_it(tmp_path):
    """Our copy is on the calendar, just not on the recorded day. It is still
    on the calendar, so nothing is sent — and the reason says where it is
    rather than claiming the recorded day."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    api.calendar[0]["date"] = "2026-08-05"      # moved in Garmin Connect
    writes_before = list(api.writes())

    second = push_threshold(writer, store, SAT)

    assert second.state == "skipped"
    assert api.writes() == writes_before, "a second copy was sent"
    assert "2026-08-05" in (second.reason or ""), second.reason


def test_a_dry_run_noop_touches_nothing(tmp_path):
    """The verification the no-op now does is a read, and dry_run does not
    read either. A preview says what it would send and reaches nothing."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    api.calls.clear()

    preview = push_threshold(writer, store, SAT, dry_run=True)

    assert (preview.action, preview.state) == ("noop", "skipped")
    assert preview.dry_run is True
    assert api.calls == [], f"dry run touched Garmin: {api.calls}"


# --- ROOT CAUSE: a calendar that did not answer is not an empty calendar ----
#
# `scheduled_rows` used to answer a failed read with `[]`, so every consumer
# below it — the adopt guard, the owned-row lookup, the removal path — was
# handed "the calendar is empty" for "the calendar said nothing". The three
# defects underneath all grew out of that one collapsed distinction.


def test_a_month_that_could_not_be_read_is_never_answered_as_empty(tmp_path):
    """The source fix. Rows come back only when there are rows to come back
    from; a read that failed raises, and the caller that wants to carry on
    without an answer has to ask for the status and say what it does about
    it."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)

    api.fail_listing = True
    with pytest.raises(writer_module.GarminWriterError):
        writer.scheduled_rows(*AUG, refresh=True)

    rows, status = writer.read_scheduled_rows(*AUG, refresh=True)
    assert (rows, status) == ([], writer_module.CALENDAR_BLOCKED)

    # ...and the genuinely empty month is a different answer entirely.
    api.fail_listing = False
    api.calendar.clear()
    rows, status = writer.read_scheduled_rows(*AUG, refresh=True)
    assert (rows, status) == ([], writer_module.CALENDAR_READ)


def test_the_owned_row_lookup_says_when_it_could_not_look(tmp_path):
    """`find_owned_scheduled` answered "(None, None)" both for "nothing of
    ours is on that day" and for "no idea, the read failed" — and its one
    caller creates a workout on the strength of that answer."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)

    assert writer.find_owned_scheduled("u1", SAT) == (999, 5000, "resolved")
    assert writer.find_owned_scheduled("u1", SUN) == (None, None, "absent")

    api.fail_listing = True
    assert writer.find_owned_scheduled("u1", SAT) == (None, None, "blocked")


def test_a_session_level_failure_is_not_a_blocked_month(tmp_path, monkeypatch):
    """Missing credentials and an expired login are not facts about one
    month, and the layer above shows a single reconnect banner for them. They
    keep travelling as exceptions rather than being flattened into "the
    calendar could not be read"."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)

    def no_session():
        raise writer_module.GarminNotConfigured("Garmin login not set.")

    # Patched on the writer, so nothing here can construct a real client.
    monkeypatch.setattr(writer, "_ensure_api", no_session)

    with pytest.raises(writer_module.GarminNotConfigured):
        writer.read_scheduled_rows(*AUG, refresh=True)
    with pytest.raises(writer_module.GarminNotConfigured):
        writer.find_owned_scheduled("u1", SAT)
    with pytest.raises(writer_module.GarminNotConfigured):
        writer.locate_owned_copies("u1", 999, 5000, SAT)
    assert api.calls == [], "a session-level failure reached the network"


# --- DEFECT D4: a create that cannot see the calendar must not create ------


def test_a_create_that_cannot_read_the_calendar_never_sends_a_second_copy(
        tmp_path):
    """D4 — two sessions on the Edge, state "done", reason "Not yet on
    Garmin."

    The adopt guard is the *only* protection against a duplicate on create,
    and it was reading a swallowed error as an empty calendar: the copy from
    the earlier push was sitting on the day, the fresh read failed, and the
    push uploaded a second one and called it a clean create.
    """
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    store.clear_push("u1")            # only the local ids are gone
    writes_before = list(api.writes())
    api.fail_listing = True           # ...and Garmin will not say what is there

    second = push_threshold(writer, store, SAT)

    assert len(api.rows_on(SAT)) == 1, (
        f"a second copy on {SAT}: {api.rows_on(SAT)}")
    assert second.state == "failed", (
        "an unreadable calendar was taken for an empty one: "
        f"{second.action} / {second.state} / {second.reason}")
    assert api.writes() == writes_before, "a refusal writes nothing at all"

    told = f"{second.error or ''} {second.reason or ''}"
    assert SAT in told
    assert "two copies" in told or "second copy" in told
    assert "[ts:" not in told, "the ownership marker is not shown to a person"


def test_the_create_conflict_clears_as_soon_as_the_calendar_answers(tmp_path):
    """A refusal the athlete cannot get out of is just a different bug: the
    read was the transient thing, so the retry adopts the copy that is really
    there rather than stacking a second one."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    push_threshold(writer, store, SAT)
    store.clear_push("u1")
    api.fail_listing = True
    assert push_threshold(writer, store, SAT).state == "failed"

    api.fail_listing = False
    retry = push_threshold(writer, store, SAT)

    assert retry.state == "done" and retry.action == "noop"
    assert len(api.rows_on(SAT)) == 1
    assert retry.workout_id == 999


def test_a_create_still_goes_ahead_on_a_calendar_that_answers_empty(tmp_path):
    """The half that must not be lost: a read that succeeded and found
    nothing is proof, and the workout is sent."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)

    first = push_threshold(writer, store, SAT)

    assert (first.action, first.state) == ("create", "done")
    assert len(api.rows_on(SAT)) == 1


def test_a_dry_run_create_touches_nothing_even_with_the_calendar_down(
        tmp_path):
    """dry_run reaches no network at all, including the fresh read the create
    path now depends on."""
    store = fresh_store(tmp_path)
    writer, api = writer_for(store)
    api.fail_listing = True

    preview = push_threshold(writer, store, SAT, dry_run=True)

    assert preview.action == "create" and preview.state == "pending"
    assert preview.dry_run is True
    assert api.calls == [], f"dry run touched Garmin: {api.calls}"
