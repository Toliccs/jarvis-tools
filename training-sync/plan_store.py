"""Local override / state store for the training calendar.

The TrainingPeaks calendar feed is read-only: you cannot write a moved
workout back to it. So this module owns the *override layer* that sits on
top of the coach's plan. The calendar the user actually sees is

    effective plan = ICS feed + overrides

Everything the user does that the feed can't hold lives here: "I work
Tuesday", "I moved Wednesday's threshold to Thursday", "I approved Alfred's
suggestion", "this one is already on my Edge 520".

Design rules that the rest of the app depends on:

- Keyed by ICS UID, so an override survives a plan re-fetch.
- A re-fetch never destroys an override. If a uid disappears from the feed
  the override is marked *orphaned*, not deleted (and we try to re-key it by
  fingerprint first, in case TrainingPeaks regenerated its UIDs).
- Writes are atomic: serialize to a temp file in the same directory, fsync,
  then os.replace(). A crash mid-write can never leave a torn state file.
- Every mutation appends to an audit log so the last change per workout can
  be undone. This is the safety net that makes "your edits are never lost"
  actually true.

Nothing in this module talks to the network, and nothing here writes to
TrainingPeaks — it is a local JSON file and its accessors.
"""

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone

DEFAULT_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  ".plan_state.json")

SCHEMA_VERSION = 1
HISTORY_LIMIT = 500

AVAILABILITY_STATES = ("available", "unavailable", "limited")
WORKOUT_STATUSES = ("planned", "moved", "skipped", "done", "orphaned")
PUSH_STATES = ("never", "pending", "scheduled", "drifted", "failed")
PROPOSAL_STATUSES = ("pending", "approved", "rejected")

# Fields a user (or an approved proposal) may override on a plan workout.
OVERRIDABLE_FIELDS = (
    "date", "status", "title", "description",
    "duration_s", "sport", "push_approved",
)

# Days a uid must be continuously missing from the feed before auto-prune
# is even considered. Cleanup is otherwise explicit.
ORPHAN_PRUNE_DAYS = 30

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PlanStoreError(Exception):
    """Base class for store errors."""

    code = "plan_store_error"


class UnknownUid(PlanStoreError):
    code = "unknown_uid"


class StaleRev(PlanStoreError):
    code = "stale_rev"


class ProposalConflict(PlanStoreError):
    """Alfred tried to overwrite a field the user set by hand."""

    code = "proposal_conflict"


class UnknownProposal(PlanStoreError):
    code = "unknown_proposal"


class InvalidValue(PlanStoreError):
    code = "invalid_value"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_date(value, label="date"):
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise InvalidValue(f"{label} must be YYYY-MM-DD, got {value!r}")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise InvalidValue(f"{label} is not a real date: {value!r}") from exc
    return value


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def fingerprint(title, plan_date):
    """Fallback identity for a workout, used to re-key overrides when
    TrainingPeaks regenerates the feed and every UID changes.

    ICS UIDs *ought* to be stable, but that is not contractually
    guaranteed. Without this, a feed regeneration would silently orphan
    every override the user ever made.
    """
    normalized = re.sub(r"\s+", " ", (title or "").strip().lower())
    raw = f"{normalized}|{plan_date or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _empty_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now(),
        "day_availability": {},
        "workout_overrides": {},
        "proposals": [],
        "rejected_signatures": [],
        "history": [],
    }


class PlanStore:
    """Durable JSON store for day availability, workout overrides,
    proposals and push status.

    Thread-safe within a process (an RLock guards read-modify-write), and
    crash-safe on disk (atomic replace). Not safe against two *processes*
    writing the same file concurrently — the app runs a single uvicorn
    worker against it.
    """

    def __init__(self, path=None, autoload=True):
        self.path = os.path.abspath(
            path
            or os.environ.get("TRAINING_SYNC_STATE_PATH")
            or DEFAULT_STATE_PATH
        )
        self._lock = threading.RLock()
        self._state = _empty_state()
        self.load_error = None
        if autoload:
            self.load()

    # ---------- persistence ----------

    def load(self):
        """Read state from disk. A missing file is normal (first run). A
        corrupt file is preserved as .corrupt-<ts> rather than silently
        overwritten — it may be recoverable by hand, and it holds the
        user's edits."""
        with self._lock:
            if not os.path.exists(self.path):
                self._state = _empty_state()
                return self._state
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    raise ValueError("state file is not a JSON object")
            except (ValueError, OSError) as exc:
                self.load_error = str(exc)
                self._quarantine_corrupt_file()
                self._state = _empty_state()
                return self._state

            base = _empty_state()
            base.update(data)
            base["schema_version"] = SCHEMA_VERSION
            for key in ("day_availability", "workout_overrides"):
                if not isinstance(base.get(key), dict):
                    base[key] = {}
            for key in ("proposals", "rejected_signatures", "history"):
                if not isinstance(base.get(key), list):
                    base[key] = []
            self._state = base
            return self._state

    def _quarantine_corrupt_file(self):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = f"{self.path}.corrupt-{stamp}"
        try:
            os.replace(self.path, backup)
        except OSError:
            pass

    def save(self):
        with self._lock:
            self._save_locked()

    def _save_locked(self):
        self._state["updated_at"] = _now()
        self._state["schema_version"] = SCHEMA_VERSION
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)

        payload = json.dumps(self._state, indent=2, sort_keys=False,
                             ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(
            prefix=os.path.basename(self.path) + ".",
            suffix=".tmp",
            dir=directory,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            # The plan is private data; keep it owner-only.
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
            self._fsync_dir(directory)
        except BaseException:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    @staticmethod
    def _fsync_dir(directory):
        """Make the rename itself durable, not just the file contents."""
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)

    @property
    def writable(self):
        directory = os.path.dirname(self.path) or "."
        if os.path.exists(self.path):
            return os.access(self.path, os.W_OK)
        return os.path.isdir(directory) and os.access(directory, os.W_OK)

    def snapshot(self):
        """Deep copy of the whole state, for debugging / status endpoints."""
        with self._lock:
            return copy.deepcopy(self._state)

    def stats(self):
        with self._lock:
            overrides = self._state["workout_overrides"]
            return {
                "path": self.path,
                "override_count": len(overrides),
                "orphan_count": sum(
                    1 for rec in overrides.values()
                    if rec.get("orphaned_since")
                ),
                "availability_count": len(self._state["day_availability"]),
                "pending_proposals": sum(
                    1 for p in self._state["proposals"]
                    if p.get("status") == "pending"
                ),
                "writable": self.writable,
                "load_error": self.load_error,
            }

    # ---------- history / audit ----------

    def _append_history(self, action, uid, actor, before, after,
                        undoable=True):
        entry = {
            "entry_id": _new_id("evt"),
            "at": _now(),
            "action": action,
            "uid": uid,
            "actor": actor,
            "before": copy.deepcopy(before),
            "after": copy.deepcopy(after),
            "undoable": undoable,
        }
        history = self._state["history"]
        history.append(entry)
        if len(history) > HISTORY_LIMIT:
            del history[: len(history) - HISTORY_LIMIT]
        return entry

    def history(self, limit=50, uid=None):
        with self._lock:
            entries = self._state["history"]
            if uid is not None:
                entries = [e for e in entries if e.get("uid") == uid]
            entries = list(reversed(entries))[:limit]
            # Only the most recent entry per uid may be undone; anything
            # older has been superseded and undoing it would resurrect
            # stale data on top of newer edits.
            seen = set()
            out = []
            for entry in entries:
                entry = copy.deepcopy(entry)
                key = (entry.get("uid"), entry.get("action"))
                target = entry.get("uid")
                if entry["undoable"] and target is not None:
                    if target in seen:
                        entry["undoable"] = False
                    seen.add(target)
                del key
                out.append(entry)
            return out

    def undo(self, entry_id, actor="user"):
        """Restore the `before` snapshot of a history entry, appending a
        compensating entry. History is append-only — never rewritten."""
        with self._lock:
            entry = next(
                (e for e in self._state["history"]
                 if e["entry_id"] == entry_id),
                None,
            )
            if entry is None:
                raise UnknownUid(f"no history entry {entry_id}")
            if not entry.get("undoable"):
                raise PlanStoreError(f"history entry {entry_id} is not undoable")

            uid = entry.get("uid")
            action = entry.get("action", "")
            before = entry.get("before")

            if action.startswith("availability"):
                current = self._state["day_availability"].get(uid)
                if before is None:
                    self._state["day_availability"].pop(uid, None)
                else:
                    self._state["day_availability"][uid] = copy.deepcopy(before)
                self._append_history("undo_availability", uid, actor,
                                     current, before, undoable=False)
            else:
                current = self._state["workout_overrides"].get(uid)
                if before is None:
                    self._state["workout_overrides"].pop(uid, None)
                else:
                    restored = copy.deepcopy(before)
                    # Keep rev monotonic so stale-rev checks stay meaningful.
                    if current:
                        restored["rev"] = int(current.get("rev", 0)) + 1
                    restored["updated_at"] = _now()
                    self._state["workout_overrides"][uid] = restored
                self._append_history("undo", uid, actor, current,
                                     self._state["workout_overrides"].get(uid),
                                     undoable=False)
            self._save_locked()
            return self._state["workout_overrides"].get(uid) if not \
                action.startswith("availability") else \
                self._state["day_availability"].get(uid)

    # ---------- day availability ----------

    def get_day_availability(self, date):
        _validate_date(date)
        with self._lock:
            record = self._state["day_availability"].get(date)
            if record is None:
                return {
                    "date": date,
                    "state": "available",
                    "note": None,
                    "max_minutes": None,
                    "source": "default",
                }
            out = copy.deepcopy(record)
            out["date"] = date
            out["source"] = "user"
            return out

    def all_day_availability(self):
        with self._lock:
            return copy.deepcopy(self._state["day_availability"])

    def availability_in_range(self, start, end):
        _validate_date(start, "start")
        _validate_date(end, "end")
        with self._lock:
            return {
                date: copy.deepcopy(rec)
                for date, rec in self._state["day_availability"].items()
                if start <= date <= end
            }

    def set_day_availability(self, date, state, note=None, max_minutes=None,
                             actor="user"):
        """Mark a day available / unavailable / limited.

        This *flags* the day. It never moves a workout — relocation is an
        explicit move or an approved proposal. Marking a day unavailable
        must not silently rewrite the plan underneath the user.
        """
        _validate_date(date)
        if state not in AVAILABILITY_STATES:
            raise InvalidValue(
                f"state must be one of {AVAILABILITY_STATES}, got {state!r}")
        if max_minutes is not None:
            if not isinstance(max_minutes, int) or max_minutes <= 0:
                raise InvalidValue("max_minutes must be a positive integer")
        if state != "limited":
            max_minutes = None

        with self._lock:
            before = copy.deepcopy(self._state["day_availability"].get(date))
            record = {
                "state": state,
                "note": note,
                "max_minutes": max_minutes,
                "updated_at": _now(),
            }
            if state == "available" and not note:
                # "available" is the default; storing it is only meaningful
                # as an explicit override of a recurring rule, which we keep.
                self._state["day_availability"][date] = record
            else:
                self._state["day_availability"][date] = record
            self._append_history("availability_set", date, actor, before,
                                 record)
            self._save_locked()
            return self.get_day_availability(date)

    def clear_day_availability(self, date, actor="user"):
        _validate_date(date)
        with self._lock:
            before = copy.deepcopy(self._state["day_availability"].pop(date, None))
            if before is not None:
                self._append_history("availability_clear", date, actor,
                                     before, None)
                self._save_locked()
            return self.get_day_availability(date)

    # ---------- workout overrides ----------

    def get_override(self, uid):
        with self._lock:
            record = self._state["workout_overrides"].get(uid)
            return copy.deepcopy(record) if record else None

    def all_overrides(self):
        with self._lock:
            return copy.deepcopy(self._state["workout_overrides"])

    def has_override(self, uid):
        with self._lock:
            return uid in self._state["workout_overrides"]

    def rev(self, uid):
        with self._lock:
            record = self._state["workout_overrides"].get(uid)
            return int(record.get("rev", 0)) if record else 0

    def _blank_override(self, uid, kind, plan_snapshot=None):
        return {
            "uid": uid,
            "kind": kind,
            "rev": 0,
            "created_at": _now(),
            "updated_at": _now(),
            "note": None,
            "fields": {},
            "plan_snapshot": copy.deepcopy(plan_snapshot) if plan_snapshot else None,
            "garmin": {
                "workout_id": None,
                "schedule_id": None,
                "scheduled_for": None,
                "pushed_at": None,
                "pushed_hash": None,
                "push_state": "never",
                "error": None,
            },
            "orphaned_since": None,
        }

    def _check_rev(self, record, expected_rev):
        if expected_rev is None:
            return
        current = int(record.get("rev", 0)) if record else 0
        if int(expected_rev) != current:
            raise StaleRev(
                f"expected rev {expected_rev} but store is at {current}")

    def patch_workout(self, uid, fields=None, note=None, kind="user",
                      expected_rev=None, plan_snapshot=None, actor=None,
                      force=False, action="patch"):
        """Sparse field-level patch of a workout override.

        `fields` is a dict of the override fields to change. A key mapped
        to None *clears* that override, reverting the field to the coach's
        plan value — matching the HTTP semantics where an explicit null
        clears and an absent key is untouched.

        Overrides are a per-field patch, not a whole-record replacement, so
        if the user moved a workout and the coach later rewrote its
        description, the user keeps their date *and* gets the new text.

        `kind="alfred"` (an approved proposal) may not silently overwrite a
        field the user set by hand — that raises ProposalConflict unless
        `force=True`. The reverse is unrestricted: a human edit always wins
        over a machine one, no confirmation. Machine changes need consent;
        human changes don't.
        """
        fields = fields or {}
        unknown = set(fields) - set(OVERRIDABLE_FIELDS)
        if unknown:
            raise InvalidValue(
                f"not overridable: {sorted(unknown)}; "
                f"allowed: {sorted(OVERRIDABLE_FIELDS)}")
        if "date" in fields and fields["date"] is not None:
            _validate_date(fields["date"], "date")
        if "status" in fields and fields["status"] is not None:
            if fields["status"] not in WORKOUT_STATUSES:
                raise InvalidValue(
                    f"status must be one of {WORKOUT_STATUSES}")
        actor = actor or ("alfred" if kind == "alfred" else "user")

        with self._lock:
            record = self._state["workout_overrides"].get(uid)
            self._check_rev(record, expected_rev)
            before = copy.deepcopy(record)

            if record is None:
                record = self._blank_override(uid, kind, plan_snapshot)
            else:
                record = copy.deepcopy(record)

            if kind == "alfred" and before is not None \
                    and before.get("kind") == "user" and not force:
                collisions = [
                    key for key, value in fields.items()
                    if key in before.get("fields", {})
                    and before["fields"][key] != value
                ]
                if collisions:
                    raise ProposalConflict(
                        "you have already edited "
                        f"{', '.join(sorted(collisions))} on this workout; "
                        "approving would overwrite your change")

            for key, value in fields.items():
                if value is None:
                    record["fields"].pop(key, None)
                else:
                    record["fields"][key] = value

            if note is not None:
                record["note"] = note or None
            if plan_snapshot is not None:
                record["plan_snapshot"] = copy.deepcopy(plan_snapshot)

            # A user edit re-stamps ownership; an alfred edit never
            # downgrades a record the user owns.
            if kind == "user" or record.get("kind") is None:
                record["kind"] = kind
            elif kind == "alfred" and record.get("kind") != "user":
                record["kind"] = "alfred"

            record["rev"] = int(record.get("rev", 0)) + 1
            record["updated_at"] = _now()

            # Local state changed after a successful push -> drifted, so the
            # UI can prompt a re-push instead of quietly disagreeing with
            # what is actually on the device.
            garmin = record.get("garmin") or {}
            if garmin.get("push_state") == "scheduled":
                relevant = {"date", "title", "description", "duration_s", "sport"}
                if relevant & set(fields):
                    garmin["push_state"] = "drifted"
                    record["garmin"] = garmin

            self._state["workout_overrides"][uid] = record
            self._append_history(action, uid, actor, before, record)
            self._save_locked()
            return copy.deepcopy(record)

    def move_workout(self, uid, to_date, note=None, kind="user",
                     expected_rev=None, plan_snapshot=None, actor=None,
                     force=False):
        """Move a workout to a different day. The drag-and-drop write."""
        _validate_date(to_date, "to_date")
        return self.patch_workout(
            uid,
            fields={"date": to_date, "status": "moved"},
            note=note,
            kind=kind,
            expected_rev=expected_rev,
            plan_snapshot=plan_snapshot,
            actor=actor,
            force=force,
            action="move",
        )

    def set_status(self, uid, status, note=None, expected_rev=None,
                   actor="user"):
        return self.patch_workout(uid, fields={"status": status}, note=note,
                                  expected_rev=expected_rev, actor=actor,
                                  action="status")

    def set_push_approved(self, uid, approved=True, expected_rev=None,
                          actor="user"):
        """Approving a plan change and pushing it to the device are two
        separate consents. This records only the first."""
        return self.patch_workout(uid, fields={"push_approved": bool(approved)},
                                  expected_rev=expected_rev, actor=actor,
                                  action="push_approve")

    def clear_override(self, uid, actor="user"):
        """Drop the whole override, reverting to the coach's plan.

        Idempotent. Deliberately does *not* touch Garmin: if the workout was
        already pushed, the record's push info is retained on a tombstone so
        the caller can still unschedule it, and the UI can prompt to re-push.
        """
        with self._lock:
            before = self._state["workout_overrides"].pop(uid, None)
            if before is None:
                return None
            self._append_history("clear_override", uid, actor, before, None)
            self._save_locked()
            return copy.deepcopy(before)

    def approved_for_push(self, uids=None):
        """Uids explicitly marked push_approved. Explicit selection is the
        default posture — no surprise writes to the Edge 520."""
        with self._lock:
            out = []
            for uid, record in self._state["workout_overrides"].items():
                if uids is not None and uid not in uids:
                    continue
                if record.get("fields", {}).get("push_approved"):
                    out.append(uid)
            return out

    # ---------- push status ----------

    def get_push(self, uid):
        with self._lock:
            record = self._state["workout_overrides"].get(uid)
            if not record:
                return None
            return copy.deepcopy(record.get("garmin"))

    def record_push(self, uid, workout_id=None, schedule_id=None,
                    scheduled_for=None, pushed_hash=None,
                    push_state="scheduled", error=None, actor="system",
                    plan_snapshot=None):
        """Persist what actually landed on Garmin.

        Both ids matter and neither substitutes for the other:
        `unschedule_workout` takes the *schedule* id, while `delete_workout`
        takes the *workout* id. Storing only the workout id makes a move
        impossible to clean up and leaves duplicates on the Edge 520.
        """
        if push_state not in PUSH_STATES:
            raise InvalidValue(f"push_state must be one of {PUSH_STATES}")
        if scheduled_for is not None:
            _validate_date(scheduled_for, "scheduled_for")

        with self._lock:
            record = self._state["workout_overrides"].get(uid)
            before = copy.deepcopy(record)
            if record is None:
                record = self._blank_override(uid, "system", plan_snapshot)
            else:
                record = copy.deepcopy(record)

            garmin = record.get("garmin") or {}
            if workout_id is not None:
                garmin["workout_id"] = workout_id
            if schedule_id is not None:
                garmin["schedule_id"] = schedule_id
            if scheduled_for is not None:
                garmin["scheduled_for"] = scheduled_for
            if pushed_hash is not None:
                garmin["pushed_hash"] = pushed_hash
            garmin["push_state"] = push_state
            garmin["error"] = error
            if push_state == "scheduled":
                garmin["pushed_at"] = _now()
            record["garmin"] = garmin
            record["updated_at"] = _now()

            self._state["workout_overrides"][uid] = record
            self._append_history("push", uid, actor, before, record,
                                 undoable=False)
            self._save_locked()
            return copy.deepcopy(garmin)

    def mark_push_failed(self, uid, error, actor="system"):
        return self.record_push(uid, push_state="failed", error=str(error),
                                actor=actor)

    def clear_push(self, uid, actor="system"):
        """Forget Garmin ids for a uid (after unscheduling/deleting)."""
        with self._lock:
            record = self._state["workout_overrides"].get(uid)
            if not record:
                return None
            before = copy.deepcopy(record)
            record = copy.deepcopy(record)
            record["garmin"] = {
                "workout_id": None,
                "schedule_id": None,
                "scheduled_for": None,
                "pushed_at": None,
                "pushed_hash": None,
                "push_state": "never",
                "error": None,
            }
            record["updated_at"] = _now()
            self._state["workout_overrides"][uid] = record
            self._append_history("push_clear", uid, actor, before, record,
                                 undoable=False)
            self._save_locked()
            return copy.deepcopy(record["garmin"])

    def pushed_uids(self):
        """Uids this app has scheduled on Garmin. The push flow must never
        unschedule anything outside this set — workouts the user created in
        Garmin Connect directly are not ours to touch."""
        with self._lock:
            return {
                uid: copy.deepcopy(rec["garmin"])
                for uid, rec in self._state["workout_overrides"].items()
                if (rec.get("garmin") or {}).get("workout_id") is not None
            }

    # ---------- orphans ----------

    def mark_orphaned(self, uid, when=None, actor="system"):
        with self._lock:
            record = self._state["workout_overrides"].get(uid)
            if not record:
                return None
            if record.get("orphaned_since"):
                return copy.deepcopy(record)
            before = copy.deepcopy(record)
            record = copy.deepcopy(record)
            record["orphaned_since"] = when or _now()
            record["updated_at"] = _now()
            self._state["workout_overrides"][uid] = record
            self._append_history("orphan", uid, actor, before, record,
                                 undoable=False)
            self._save_locked()
            return copy.deepcopy(record)

    def clear_orphan(self, uid, actor="system"):
        with self._lock:
            record = self._state["workout_overrides"].get(uid)
            if not record or not record.get("orphaned_since"):
                return None
            record = copy.deepcopy(record)
            record["orphaned_since"] = None
            record["updated_at"] = _now()
            self._state["workout_overrides"][uid] = record
            self._save_locked()
            return copy.deepcopy(record)

    def orphans(self):
        with self._lock:
            return {
                uid: copy.deepcopy(rec)
                for uid, rec in self._state["workout_overrides"].items()
                if rec.get("orphaned_since")
            }

    def rematch_orphans(self, plan_entries, actor="system"):
        """Re-key overrides whose uid vanished from the feed.

        `plan_entries` is the current plan as a list of dicts with at least
        `uid`, `title` and `date`. An override whose stored fingerprint
        matches exactly one otherwise-unclaimed plan entry is silently
        re-keyed to the new uid. Ambiguous matches are left orphaned — a
        wrong guess would attach the user's edits to someone else's session.

        This is the whole defense against a TrainingPeaks feed regeneration
        wiping the user's work.
        """
        with self._lock:
            plan_by_uid = {}
            for entry in plan_entries or []:
                uid = entry.get("uid")
                if uid:
                    plan_by_uid[uid] = entry

            claimed = set(self._state["workout_overrides"]) & set(plan_by_uid)

            # Fingerprint -> unclaimed plan uids
            by_fp = {}
            for uid, entry in plan_by_uid.items():
                if uid in claimed:
                    continue
                snapshot = entry.get("plan_snapshot") or entry
                fp = entry.get("fingerprint") or fingerprint(
                    snapshot.get("title"), snapshot.get("date"))
                by_fp.setdefault(fp, []).append(uid)

            rekeyed = []
            for old_uid in list(self._state["workout_overrides"]):
                if old_uid in plan_by_uid:
                    # Still present upstream; make sure it isn't flagged.
                    rec = self._state["workout_overrides"][old_uid]
                    if rec.get("orphaned_since"):
                        rec["orphaned_since"] = None
                    continue

                record = self._state["workout_overrides"][old_uid]
                snapshot = record.get("plan_snapshot") or {}
                fp = snapshot.get("fingerprint")
                if not fp and snapshot:
                    fp = fingerprint(snapshot.get("title"),
                                     snapshot.get("date"))
                candidates = by_fp.get(fp, []) if fp else []

                if len(candidates) == 1:
                    new_uid = candidates[0]
                    before = copy.deepcopy(record)
                    moved = copy.deepcopy(record)
                    moved["uid"] = new_uid
                    moved["orphaned_since"] = None
                    moved["updated_at"] = _now()
                    del self._state["workout_overrides"][old_uid]
                    self._state["workout_overrides"][new_uid] = moved
                    by_fp[fp] = []
                    self._append_history("rekey", new_uid, actor, before,
                                         moved, undoable=False)
                    rekeyed.append((old_uid, new_uid))
                else:
                    if not record.get("orphaned_since"):
                        record["orphaned_since"] = _now()

            self._save_locked()
            return rekeyed

    # ---------- proposals ----------

    def add_proposal(self, proposal):
        """Store a single proposal dict. Returns the stored copy."""
        with self._lock:
            record = copy.deepcopy(proposal)
            record.setdefault("proposal_id", _new_id("prp"))
            record.setdefault("status", "pending")
            record.setdefault("created_at", _now())
            record.setdefault("decided_at", None)
            record.setdefault("decision_note", None)
            self._state["proposals"].append(record)
            self._save_locked()
            return copy.deepcopy(record)

    def replace_pending_proposals(self, start, end, proposals):
        """Regenerating proposals for a range replaces the *pending* set for
        that range. Already approved/rejected ones are historical record and
        are never touched."""
        _validate_date(start, "start")
        _validate_date(end, "end")
        with self._lock:
            kept = []
            for existing in self._state["proposals"]:
                if existing.get("status") != "pending":
                    kept.append(existing)
                    continue
                date = existing.get("from_date") or existing.get("to_date")
                if date and start <= date <= end:
                    continue  # superseded
                kept.append(existing)

            stored = []
            for proposal in proposals or []:
                record = copy.deepcopy(proposal)
                record.setdefault("proposal_id", _new_id("prp"))
                record["status"] = "pending"
                record.setdefault("created_at", _now())
                record.setdefault("decided_at", None)
                record.setdefault("decision_note", None)
                stored.append(record)

            self._state["proposals"] = kept + stored
            self._save_locked()
            return copy.deepcopy(stored)

    def get_proposal(self, proposal_id):
        with self._lock:
            for proposal in self._state["proposals"]:
                if proposal.get("proposal_id") == proposal_id:
                    return copy.deepcopy(proposal)
            return None

    def list_proposals(self, status=None, start=None, end=None, uid=None):
        with self._lock:
            out = []
            for proposal in self._state["proposals"]:
                if status and proposal.get("status") != status:
                    continue
                if uid and proposal.get("uid") != uid:
                    continue
                if start or end:
                    date = proposal.get("to_date") or proposal.get("from_date")
                    if date:
                        if start and date < start:
                            continue
                        if end and date > end:
                            continue
                out.append(copy.deepcopy(proposal))
            return out

    def approve_proposal(self, proposal_id, force=False, actor="user",
                         plan_snapshot=None):
        """Materialize a proposal into an override.

        This is the single write path from proposal to effect: an approved
        proposal simply *is* an override with kind="alfred", so there is no
        runtime precedence contest between the two layers.

        Approving does not push to Garmin. Push is always separate.
        """
        with self._lock:
            proposal = None
            for candidate in self._state["proposals"]:
                if candidate.get("proposal_id") == proposal_id:
                    proposal = candidate
                    break
            if proposal is None:
                raise UnknownProposal(f"no proposal {proposal_id}")

            if proposal.get("status") == "approved":
                # Idempotent re-approve.
                return {
                    "proposal": copy.deepcopy(proposal),
                    "override": self.get_override(proposal.get("uid")),
                }
            if proposal.get("status") == "rejected":
                raise ProposalConflict(
                    f"proposal {proposal_id} was already rejected")

            uid = proposal.get("uid")
            if not uid:
                raise InvalidValue("proposal has no uid")

            changes = dict(proposal.get("changes") or {})
            if not changes and proposal.get("to_date"):
                changes["date"] = proposal["to_date"]
            if changes.get("date"):
                changes.setdefault("status", "moved")

            # patch_workout raises ProposalConflict when this would clobber
            # a manual edit and force is False.
            override = self.patch_workout(
                uid,
                fields=changes,
                kind="alfred",
                plan_snapshot=plan_snapshot,
                actor=actor,
                force=force,
                action="approve_proposal",
            )

            proposal["status"] = "approved"
            proposal["decided_at"] = _now()
            self._save_locked()
            return {
                "proposal": copy.deepcopy(proposal),
                "override": override,
            }

    def reject_proposal(self, proposal_id, reason=None, actor="user"):
        """Reject a proposal and remember its signature, so the next run
        doesn't immediately re-suggest the identical change."""
        with self._lock:
            proposal = None
            for candidate in self._state["proposals"]:
                if candidate.get("proposal_id") == proposal_id:
                    proposal = candidate
                    break
            if proposal is None:
                raise UnknownProposal(f"no proposal {proposal_id}")
            if proposal.get("status") == "rejected":
                return copy.deepcopy(proposal)
            if proposal.get("status") == "approved":
                raise ProposalConflict(
                    f"proposal {proposal_id} was already approved")

            proposal["status"] = "rejected"
            proposal["decided_at"] = _now()
            proposal["decision_note"] = reason

            signature = self.proposal_signature(proposal)
            self._state["rejected_signatures"].append({
                "uid": proposal.get("uid"),
                "signature": signature,
                "rejected_at": _now(),
            })
            self._append_history("reject_proposal", proposal.get("uid"),
                                 actor, None, proposal, undoable=False)
            self._save_locked()
            return copy.deepcopy(proposal)

    @staticmethod
    def proposal_signature(proposal):
        kind = proposal.get("kind", "move")
        from_date = proposal.get("from_date") or ""
        to_date = proposal.get("to_date") or ""
        return f"{kind}:{from_date}->{to_date}"

    def is_rejected(self, uid, proposal):
        """Has the user already said no to exactly this change?"""
        signature = self.proposal_signature(proposal)
        with self._lock:
            return any(
                entry.get("uid") == uid and entry.get("signature") == signature
                for entry in self._state["rejected_signatures"]
            )

    def prune_proposals(self, keep_pending=True, before=None):
        """Drop decided proposals older than `before` (ISO timestamp)."""
        with self._lock:
            kept = []
            for proposal in self._state["proposals"]:
                if proposal.get("status") == "pending" and keep_pending:
                    kept.append(proposal)
                    continue
                decided = proposal.get("decided_at") or ""
                if before and decided and decided >= before:
                    kept.append(proposal)
            removed = len(self._state["proposals"]) - len(kept)
            self._state["proposals"] = kept
            self._save_locked()
            return removed


if __name__ == "__main__":
    import sys

    store = PlanStore()
    stats = store.stats()
    print(f"state file: {stats['path']}")
    print(f"  writable:   {stats['writable']}")
    print(f"  overrides:  {stats['override_count']} "
          f"({stats['orphan_count']} orphaned)")
    print(f"  day rules:  {stats['availability_count']}")
    print(f"  proposals:  {stats['pending_proposals']} pending")
    if stats["load_error"]:
        print(f"  load error: {stats['load_error']}", file=sys.stderr)
