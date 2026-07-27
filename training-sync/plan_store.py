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
- The Garmin push identity is *not* an override. It lives in its own
  top-level map (`garmin_push`), keyed by the same uid, because it is not
  the user's data: it is this app's only handle on the copy already sitting
  on the Garmin calendar. Keeping it inside the override record meant every
  user-facing edit — undo, revert, patch — could clobber it, and one that
  did left a duplicate on the Edge 520 that nothing here could remove. The
  two maps are written by two disjoint sets of methods and no user-edit
  method touches the push map at all.
- A stored id is only ever a *claim on somebody's calendar*, so it has to be
  earned. Every id written under this schema was proved ours at push time by
  the ownership marker the writer puts in the workout name, and `record_push`
  stamps that fact onto the record itself. Ids inherited from an older file
  carry no such stamp: the schema they came from adopted a Garmin workout on
  a title match, so one of them can be the athlete's own session. Every load
  therefore re-checks the evidence each record carries and drops what it
  cannot vouch for — see `_validate_push_identity` for the two rules, and
  `_disown` for what a drop actually costs.
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
import logging
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone

_LOG = logging.getLogger(__name__)

DEFAULT_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  ".plan_state.json")

# v2 lifted the Garmin push identity out of every override record and into
# its own top-level map. `_migrate` carries a v1 file forward on load.
#
# Deliberately *not* bumped for the identity-proof field below, and nothing in
# `_validate_push_identity` reads this number. Keying that check on the version
# is what made the previous attempt a no-op: the build that first shipped v2
# writes this stamp and validates nothing, so a file it had touched arrived
# claiming to have been checked. A number in the file is written by whichever
# build wrote the file last, and that build is exactly the thing under
# suspicion. `identity_proof` is per-record and additive; an older build
# preserves it verbatim, so a downgrade and back costs nothing.
SCHEMA_VERSION = 2
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

# The only keys an override record may carry. Anything else — most of all a
# Garmin block smuggled in by an old history snapshot — is dropped on the way
# in, so the two maps cannot re-couple by accident.
OVERRIDE_KEYS = (
    "uid", "kind", "rev", "created_at", "updated_at",
    "note", "fields", "plan_snapshot", "orphaned_since",
)

# The Garmin-facing half. `plan_snapshot` rides along so a push-only record
# still knows which session it belongs to after the feed drops the uid.
PUSH_KEYS = (
    "workout_id", "schedule_id", "scheduled_for",
    "pushed_at", "pushed_hash", "push_state", "error",
)

# Positive proof that an id was earned rather than inherited, written onto the
# record at the moment it was earned. Deliberately *not* in PUSH_KEYS: it is
# bookkeeping for the load path, not part of the push view the app reads, and
# keeping it out means it can never be copied out of a legacy `garmin` block
# (which `_migrate` fills key-by-key from PUSH_KEYS) and passed off as proof.
PUSH_PROOF_KEY = "identity_proof"
PUSH_PROOF_METHODS = ("record_push",)

# Days a uid must be continuously missing from the feed before auto-prune
# is even considered. Cleanup is otherwise explicit.
ORPHAN_PRUNE_DAYS = 30

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _Unset:
    """Sentinel for "the caller said nothing about this field".

    Needed wherever None is a value in its own right rather than an absence:
    on the Garmin push record None means *clear this id*, which is a very
    different instruction from *leave the stored one alone*.
    """

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<unset>"


UNSET = _Unset()


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


class PushIdentityConflict(PlanStoreError):
    """Two workouts tried to claim one id on the Garmin calendar.

    A schedule id identifies a single row on that calendar. If two uids both
    believed they owned it, unscheduling one would silently remove the other
    from the athlete's day. Unreachable by construction now that adoption is
    ownership-proven — this is the tripwire that keeps it that way.

    It guards the *write* path only, which is not the whole way in: a state
    file written by an older schema arrives with its ids already set, having
    never passed through `record_push`. `_migrate` applies the same rule on
    load, so the invariant holds over everything in the map rather than only
    over what this schema put there.
    """

    code = "push_identity_conflict"


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
        "garmin_push": {},
        "proposals": [],
        "rejected_signatures": [],
        "history": [],
    }


def _blank_push(uid):
    return {
        "uid": uid,
        "workout_id": None,
        "schedule_id": None,
        "scheduled_for": None,
        "pushed_at": None,
        "pushed_hash": None,
        "push_state": "never",
        "error": None,
        "plan_snapshot": None,
        "created_at": _now(),
        "updated_at": _now(),
    }


def _push_view(entry):
    """The Garmin block as the rest of the app reads it."""
    entry = entry or {}
    return {key: entry.get(key) for key in PUSH_KEYS} if entry else None


def _holds_a_copy(entry):
    """True when Garmin is holding something of ours for this uid."""
    entry = entry or {}
    return entry.get("workout_id") is not None \
        or entry.get("schedule_id") is not None


def _override_only(record):
    """Strip a record down to the user's half.

    Every write into `workout_overrides` goes through here, so a stale
    history snapshot from before the split — or any other record that still
    carries a `garmin` block — cannot put push identity back into the edit
    layer. This is what makes "an edit cannot touch the ids" structural
    rather than a rule someone has to remember.
    """
    record = record or {}
    out = {key: copy.deepcopy(record[key])
           for key in OVERRIDE_KEYS if key in record}
    if not isinstance(out.get("fields"), dict):
        out["fields"] = {}
    return out


# There was a `_schema_version_of(data)` here, and the load path branched on
# it. It is gone rather than left unused: the whole defect was that a file's
# claimed version decided whether its ids were checked, and the build that
# writes the current claim checks nothing. Nothing should read that number to
# decide what to trust.


def _identity_proof():
    """The stamp `record_push` writes when an id is earned.

    Its presence means: this id entered the map through this schema's write
    path, which the writer only reaches after it either created the workout
    itself or matched the ownership marker it wrote into the name. It is a
    record of *where the id came from*, not a signature — anything that can
    call `record_push` can mint one, exactly as anything that can call
    `record_push` can already set an id. That is the same trust boundary,
    written down. What it rules out is the thing that actually happened: a
    file arriving from a build that never checked anything.
    """
    return {
        "method": "record_push",
        "schema_version": SCHEMA_VERSION,
        "at": _now(),
    }


def _identity_is_proven(entry):
    """Whether this record can show how it came by its ids.

    Two forms of evidence, and neither is a version stamp:

    - the `identity_proof` stamp above, written by this schema's write path;
    - a non-empty `pushed_hash`, which is a digest of a payload this app
      uploaded. Only a build that *created* the workout could compute one, so
      it proves authorship on its own. It is all an older file can offer, and
      dropping those records would orphan sessions this app genuinely owns.

    A blank hash counts for nothing: a real one is a digest of what was sent,
    so an empty string is the same absence of proof as None.
    """
    entry = entry or {}
    proof = entry.get(PUSH_PROOF_KEY)
    if isinstance(proof, dict) and proof.get("method") in PUSH_PROOF_METHODS:
        return True
    return bool(entry.get("pushed_hash"))


def _orphan_message(note):
    """What to tell the athlete about a session this app just let go of.

    They are the only one who can clean it up, so the sentence carries the two
    things they need to find it — which workout, and which day — and says
    plainly where to go. A dropped record with no plan snapshot has neither,
    so it falls back to the Garmin id, which is at least visible in Garmin
    Connect.

    It says "if a duplicate appears" rather than promising one, because which
    way it goes depends on something this module cannot see: whether the copy
    on the calendar carries the ownership marker. It usually will not (see
    `_disown`), but the store never talks to Garmin and has no business
    asserting what is on it. Both outcomes lead to the same instruction, so
    the athlete loses nothing to the hedge: look at that day, and if there are
    two, the older one is the stray.
    """
    title = note.get("title")
    when = note.get("scheduled_for")
    ident = note.get("schedule_id")
    if ident is None:
        ident = note.get("workout_id")
    subject = title or "A workout this app pushed"
    where = f" on {when}" if when else ""
    tag = "" if title or ident is None else f" (Garmin id {ident})"
    return (
        f"{subject}{where}{tag} may still be on your Garmin calendar. This "
        "app can no longer prove that copy is its own, so it will not remove "
        "or update it. Check that day in Garmin Connect: if a duplicate "
        "appears after the next push, the older copy is the stray and is "
        "yours to delete."
    )


def _disown(push, uid, reason, notes, **extra):
    """Drop a push record the store cannot vouch for, and say what it costs.

    Dropping means the app forgets it ever pushed that uid. It does not mean
    the copy on the Garmin calendar goes away, and for most of the records
    this runs on it cannot be undone by pushing again either. The commonest
    case by far is an id written before the ownership marker existed (the
    marker arrived in 1c7aa87; everything older adopted on a title match). No
    marker means no later push can recognise that copy as its own: the push
    takes the create branch and puts a second row on the day. For those
    records two sessions on the calendar is the *guaranteed* outcome of a
    drop, not the worst case, and the stray one is permanently beyond this
    app's reach — nothing here will ever touch it again, because nothing here
    can prove it may. Only the athlete can remove it.

    An id written by 1c7aa87 itself is dropped too — it carries no proof
    stamp, and nothing in the file distinguishes it from a title-match
    adoption — but that copy *is* marked, so the next push re-adopts it and
    the drop costs only a stale "not on Garmin" in the meantime. The store
    cannot tell the two apart without reading the Garmin calendar, which it
    never does, so the message it writes hedges accordingly.

    Dropping is still the right direction, but the trade is narrower than
    "forgetting is free". Keeping an id we cannot vouch for risks silently
    unscheduling a session somebody else built, which the athlete cannot undo
    and may never notice; a visible duplicate they have been told about beats
    an invisible deletion. The telling is what keeps that true, so every note
    carries the title and date of the session it orphaned and a sentence
    saying what to look for in Garmin Connect — see `_record_migration_drops`.
    """
    entry = push.pop(uid, None) or {}
    snapshot = entry.get("plan_snapshot") or {}
    note = {
        "uid": uid,
        "reason": reason,
        "workout_id": entry.get("workout_id"),
        "schedule_id": entry.get("schedule_id"),
        "push_state": entry.get("push_state"),
        "scheduled_for": entry.get("scheduled_for"),
        # The athlete's half: what to look for, and what to do about it.
        "title": snapshot.get("title") if isinstance(snapshot, dict) else None,
        "entry": copy.deepcopy(entry),
    }
    note.update(extra)
    note["message"] = _orphan_message(note)
    notes.append(note)
    return note


def _validate_push_identity(push, notes):
    """Refuse to inherit a Garmin id this schema cannot stand behind.

    Runs on every load, against the evidence each record carries — never
    against the file's `schema_version`. The previous version of this check
    ran rule 1 only when the file claimed to be older, which sounded right and
    was not: the build that first shipped the current version stamp performs
    no validation, so a file that went v1 -> that build -> here arrived
    claiming to have been checked and sailed past. That is the ordinary
    upgrade order; the order the rule did work for was the one nobody takes.

    Two rules, in this order:

    1. An entry that holds an id it cannot account for is dropped — see
       `_identity_is_proven` for what counts. That is exactly the shape the
       old adopt-on-a-title-match left behind, and the workout underneath it
       may be one the athlete built by hand. The order matters: running this
       first means an unproven claim on a proven record's ids costs only the
       unproven one.

       `pushed_hash is None` is a legitimate state under this schema — the
       writer leaves it unset after adopting a workout it proved is ours by
       the marker, so the next push rewrites content it could not read rather
       than trusting it. Those records carry the `identity_proof` stamp
       instead, which is why the check cannot simply be "is there a hash".

    2. No id may be owned twice. A schedule id is one row on the athlete's
       calendar; two uids claiming it means unscheduling either silently
       removes the other. `record_push` has refused this since the ids were
       split out, but nothing enforced it on the way in, so a file written by
       another build could carry the state straight past the tripwire. Every
       uid in a colliding set is disowned: which of them (if either) really
       owns the row is not knowable from here, and guessing wrong is the
       destructive outcome.
    """
    for uid, entry in list(push.items()):
        if _holds_a_copy(entry) and not _identity_is_proven(entry):
            _disown(push, uid, "unproven_inherited_id", notes)

    claims = {}
    for uid, entry in push.items():
        for field in ("workout_id", "schedule_id"):
            value = entry.get(field)
            if value is not None:
                claims.setdefault((field, value), []).append(uid)

    conflicts = {}
    for uids in claims.values():
        if len(uids) < 2:
            continue
        for uid in uids:
            conflicts.setdefault(uid, set()).update(
                other for other in uids if other != uid)

    for uid in sorted(conflicts):
        _disown(push, uid, "id_collision", notes,
                conflict_with=sorted(conflicts[uid]))

    return notes


def _migrate(data, notes=None):
    """Carry an older state file forward: lift `garmin` out of every override,
    then validate the push identity it brought with it.

    v1 kept the push identity inside the override record and, once an
    override had been cleared, kept a bare "tombstone" record alive purely to
    hold it. Both become a plain entry in `garmin_push`; a tombstone has
    nothing else in it, so nothing of it remains in the edit layer.

    Nothing the app can still stand behind is dropped, and history is left
    exactly as written — it is an audit log, not state. What *is* dropped is
    push identity that fails `_validate_push_identity`: ids no record can
    account for, and ids two uids both claim. That check runs over the whole
    map on every load, not only when the file looks old, because "the file
    looks old" is a claim made by the build that wrote it. Every drop is
    appended to `notes` so the caller can surface it; a migration that drops
    nothing leaves `notes` empty, which is the case for every legitimate
    record and for the shipped state file.

    Legacy ids are copied forward key-by-key from PUSH_KEYS, so a `garmin`
    block cannot smuggle an `identity_proof` in with them — an old id is
    judged on the hash it really has.
    """
    notes = [] if notes is None else notes
    overrides = data.get("workout_overrides")
    if not isinstance(overrides, dict):
        overrides = {}
    push = data.get("garmin_push")
    if not isinstance(push, dict):
        push = {}

    for uid, record in list(overrides.items()):
        if not isinstance(record, dict):
            overrides.pop(uid, None)
            continue
        legacy = record.pop("garmin", None)
        was_tombstone = bool(record.pop("tombstone", False))

        if isinstance(legacy, dict) and uid not in push:
            entry = _blank_push(uid)
            for key in PUSH_KEYS:
                if legacy.get(key) is not None:
                    entry[key] = legacy[key]
            entry["plan_snapshot"] = copy.deepcopy(record.get("plan_snapshot"))
            entry["created_at"] = record.get("created_at") or entry["created_at"]
            entry["updated_at"] = record.get("updated_at") or entry["updated_at"]
            # A never-pushed record has no identity worth carrying over.
            if _holds_a_copy(entry) or entry["push_state"] != "never":
                push[uid] = entry

        if was_tombstone and not record.get("fields"):
            overrides.pop(uid, None)
        else:
            overrides[uid] = _override_only(record)

    for uid, entry in list(push.items()):
        if not isinstance(entry, dict):
            push.pop(uid, None)
            continue
        entry.setdefault("uid", uid)

    _validate_push_identity(push, notes)

    data["workout_overrides"] = overrides
    data["garmin_push"] = push
    return data


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
        # Push records the last load refused to inherit. Empty for every
        # record that can show where its ids came from, whatever version the
        # file it arrived in claims to be; see `_validate_push_identity`.
        self.migration_notes = []
        if autoload:
            self.load()

    # ---------- persistence ----------

    def load(self):
        """Read state from disk. A missing file is normal (first run). A
        corrupt file is preserved as .corrupt-<ts> rather than silently
        overwritten — it may be recoverable by hand, and it holds the
        user's edits."""
        with self._lock:
            self.migration_notes = []
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
            for key in ("day_availability", "workout_overrides", "garmin_push"):
                if not isinstance(base.get(key), dict):
                    base[key] = {}
            for key in ("proposals", "rejected_signatures", "history"):
                if not isinstance(base.get(key), list):
                    base[key] = []
            # Migrate on the file's own shape. What the file *claims* decides
            # nothing: the version is stamped afterwards and the identity
            # rules never read it, because the build that wrote the stamp is
            # the one being checked up on.
            notes = []
            _migrate(base, notes)
            base["schema_version"] = SCHEMA_VERSION
            self._state = base
            self.migration_notes = notes
            self._record_migration_drops(notes)
            return self._state

    def _record_migration_drops(self, notes):
        """Make a dropped push identity visible rather than silent.

        Silence is the failure mode to avoid here. A drop leaves a copy on the
        athlete's Garmin calendar that this app will not touch again, and
        usually cannot reach again at all (`_disown` has the cases). Where it
        cannot, the next push stacks a duplicate beside it and the athlete is
        the only one who can remove either — so they have to be told which
        session, and on which day.

        Three ways, because they may notice any one of them: a log line for
        whoever is watching the process, `migration_notes` / `stats()` for the
        status endpoint (which `/api/status` forwards to the UI), and a
        history entry so the audit log the UI already renders says what
        happened to that workout. Each note carries `title`, `scheduled_for`
        and a ready-to-show `message`.

        The history entry is written here rather than saved immediately —
        `load` does not write to disk — so it persists with the next save,
        alongside a state that no longer holds the id.
        """
        for note in notes:
            _LOG.warning(
                "plan_store: not inheriting the Garmin identity for %s "
                "(reason=%s workout_id=%s schedule_id=%s%s). %s",
                note.get("uid"), note.get("reason"), note.get("workout_id"),
                note.get("schedule_id"),
                (" conflict_with=%s" % (note.get("conflict_with"),))
                if note.get("conflict_with") else "",
                note.get("message"),
            )
            self._append_history("migration_drop_push", note.get("uid"),
                                 "migration", note.get("entry"), None,
                                 undoable=False)

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
                "pushed_count": sum(
                    1 for entry in self._state["garmin_push"].values()
                    if _holds_a_copy(entry)
                ),
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
                # Push identity the last load refused to inherit. Zero once
                # every record carries its own proof; each note names the
                # session it orphaned so the UI can tell the athlete what to
                # remove in Garmin Connect.
                "migration_dropped": len(self.migration_notes),
                "migration_notes": copy.deepcopy(self.migration_notes),
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

    def _undoable_entry_ids(self):
        """Entry ids that may still be undone, read off the stored history.

        Only the most recent *surviving* change per uid may be undone; anything
        older has been superseded and undoing it would resurrect stale data on
        top of newer edits. `history` reports this and `undo` enforces it from
        the same computation, so the flag the UI shows can never disagree with
        what the store will actually accept.

        An undo writes a compensating entry rather than rewriting history, so
        the entry it reversed is still sitting there with its `undoable` flag
        set. Walking newest-first and stopping at the first flagged entry would
        therefore keep offering a change that has already been taken back —
        undoing it again does nothing, and the edit before it could never be
        reached. Each compensation cancels the next older change to the same
        uid instead, which makes Undo an actual stack: repeat it and you walk
        back through the edits one at a time.
        """
        undoable = set()
        seen = set()
        cancelled = {}
        for entry in reversed(self._state["history"]):
            target = entry.get("uid")
            if str(entry.get("action") or "").startswith("undo"):
                # A compensation retires the change it reversed, not this one.
                cancelled[target] = cancelled.get(target, 0) + 1
                continue
            if not entry.get("undoable"):
                continue
            if cancelled.get(target):
                cancelled[target] -= 1
                continue
            if target is not None:
                if target in seen:
                    continue
                seen.add(target)
            undoable.add(entry.get("entry_id"))
        return undoable

    def history(self, limit=50, uid=None):
        with self._lock:
            undoable = self._undoable_entry_ids()
            entries = self._state["history"]
            if uid is not None:
                entries = [e for e in entries if e.get("uid") == uid]
            entries = list(reversed(entries))[:limit]
            out = []
            for entry in entries:
                entry = copy.deepcopy(entry)
                entry["undoable"] = entry.get("entry_id") in undoable
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
            if entry_id not in self._undoable_entry_ids():
                raise PlanStoreError(
                    f"history entry {entry_id} has been superseded by a newer "
                    "change to the same workout; undo that one first")

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
                    # The entry created the override, so undoing it removes
                    # the override. What is already on Garmin is not in this
                    # map and is therefore not at risk.
                    self._state["workout_overrides"].pop(uid, None)
                else:
                    # `before` may be a pre-split snapshot carrying a stale
                    # `garmin` block. _override_only drops it: an undo restores
                    # the user's edit and nothing else, ever.
                    restored = _override_only(before)
                    # Keep rev monotonic so stale-rev checks stay meaningful.
                    if current:
                        restored["rev"] = int(current.get("rev", 0)) + 1
                    restored["updated_at"] = _now()
                    self._state["workout_overrides"][uid] = restored
                self._append_history("undo", uid, actor, current,
                                     self._state["workout_overrides"].get(uid),
                                     undoable=False)
            self._save_locked()
            if action.startswith("availability"):
                return self._state["day_availability"].get(uid)
            return self.get_override(uid)

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

    def _projected_locked(self, uid):
        """The override as callers read it: the user's record with a
        read-only view of the Garmin push attached.

        The `garmin` key here is a *projection*. It is assembled on the way
        out and never stored, so writing a projected record back through any
        edit method cannot carry push identity with it.

        A uid with no override but a live copy on Garmin still projects — as
        an empty override — because the app has to keep showing the athlete
        the session it can still remove from their calendar. When the copy
        goes (unpush), so does the record: unlike the tombstone it replaces,
        this state always has an exit.
        """
        record = self._state["workout_overrides"].get(uid)
        entry = self._state["garmin_push"].get(uid)
        if record is None:
            if not _holds_a_copy(entry):
                return None
            record = {
                "uid": uid,
                "kind": "system",
                "rev": 0,
                "created_at": entry.get("created_at"),
                "updated_at": entry.get("updated_at"),
                "note": None,
                "fields": {},
                "plan_snapshot": copy.deepcopy(entry.get("plan_snapshot")),
                "orphaned_since": None,
                "push_only": True,
            }
        else:
            record = copy.deepcopy(record)
        record["garmin"] = _push_view(entry) or _push_view(_blank_push(uid))
        return record

    def get_override(self, uid):
        with self._lock:
            return self._projected_locked(uid)

    def all_overrides(self):
        with self._lock:
            uids = list(self._state["workout_overrides"])
            uids += [uid for uid in self._state["garmin_push"]
                     if uid not in self._state["workout_overrides"]]
            projected = ((uid, self._projected_locked(uid)) for uid in uids)
            return {uid: record for uid, record in projected
                    if record is not None}

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
            # what is actually on the device. This is the one thing an edit
            # says to the push record, and `_mark_drift_locked` can only set
            # a state — it has no way to reach an id.
            relevant = {"date", "title", "description", "duration_s", "sport"}
            if relevant & set(fields):
                self._mark_drift_locked(uid)

            record = _override_only(record)
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

        Idempotent. Deliberately does *not* touch Garmin: the push identity
        lives in its own map, so the caller can still unschedule the copy on
        the calendar and the UI can still prompt to re-push. Nothing is left
        behind here — v1 kept a "tombstone" record alive to hold the ids and
        that record had no way out; there is no longer anything to hold.
        """
        with self._lock:
            record = self._state["workout_overrides"].pop(uid, None)
            if record is None:
                return None
            before = copy.deepcopy(record)
            self._append_history("clear_override", uid, actor, before, None)
            self._save_locked()
            before["garmin"] = _push_view(self._state["garmin_push"].get(uid)) \
                or _push_view(_blank_push(uid))
            return before

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
        """What Garmin is holding for this uid, or None if nothing ever was.

        Read straight off `garmin_push` — no override record is needed and
        none is consulted, so a workout the user has never edited is just as
        knowable as one they have.
        """
        with self._lock:
            entry = self._state["garmin_push"].get(uid)
            return _push_view(entry) if entry else None

    def all_pushes(self):
        with self._lock:
            return {uid: _push_view(entry)
                    for uid, entry in self._state["garmin_push"].items()}

    def _mark_drift_locked(self, uid):
        """Say "the plan moved away from what is on the device".

        A state, not an identity: this is the only thing the edit surface can
        say to the push record, and it cannot say anything about an id.
        """
        entry = self._state["garmin_push"].get(uid)
        if entry and entry.get("push_state") == "scheduled":
            entry["push_state"] = "drifted"
            entry["updated_at"] = _now()

    def _check_id_ownership_locked(self, uid, workout_id, schedule_id):
        """Refuse an id another uid already claims.

        Change 2(c). Two uids on one schedule id means unscheduling either
        one removes the other's session from the athlete's calendar.
        """
        for other, entry in self._state["garmin_push"].items():
            if other == uid:
                continue
            if schedule_id is not None \
                    and entry.get("schedule_id") == schedule_id:
                raise PushIdentityConflict(
                    f"Garmin schedule {schedule_id} is already recorded "
                    f"against {other!r}; two workouts cannot share one "
                    "calendar entry")
            if workout_id is not None \
                    and entry.get("workout_id") == workout_id:
                raise PushIdentityConflict(
                    f"Garmin workout {workout_id} is already recorded "
                    f"against {other!r}")

    def record_push(self, uid, workout_id=UNSET, schedule_id=UNSET,
                    scheduled_for=UNSET, pushed_hash=UNSET,
                    push_state="scheduled", error=None, actor="system",
                    plan_snapshot=None):
        """Persist what actually landed on Garmin.

        Both ids matter and neither substitutes for the other:
        `unschedule_workout` takes the *schedule* id, while `delete_workout`
        takes the *workout* id. Storing only the workout id makes a move
        impossible to clean up and leaves duplicates on the Edge 520.

        Each field follows the same patch semantics as `patch_workout`: omit
        it to leave the stored value alone, pass None to *clear* it. Clearing
        has to be sayable — after a reschedule the old schedule id is already
        gone from Garmin, and keeping it means the next move tries to
        unschedule an id that no longer exists, which Garmin refuses and which
        strands the workout on the wrong day.
        """
        if push_state not in PUSH_STATES:
            raise InvalidValue(f"push_state must be one of {PUSH_STATES}")
        if scheduled_for is not UNSET and scheduled_for is not None:
            _validate_date(scheduled_for, "scheduled_for")

        with self._lock:
            entry = self._state["garmin_push"].get(uid)
            before = copy.deepcopy(entry)
            entry = copy.deepcopy(entry) if entry else _blank_push(uid)

            if workout_id is not UNSET:
                entry["workout_id"] = workout_id
            if schedule_id is not UNSET:
                entry["schedule_id"] = schedule_id
            if scheduled_for is not UNSET:
                entry["scheduled_for"] = scheduled_for
            if pushed_hash is not UNSET:
                entry["pushed_hash"] = pushed_hash
            entry["push_state"] = push_state
            entry["error"] = error
            if push_state == "scheduled":
                entry["pushed_at"] = _now()
            if plan_snapshot is not None:
                entry["plan_snapshot"] = copy.deepcopy(plan_snapshot)
            entry["updated_at"] = _now()

            # This is where an id is earned, so this is where it is stamped.
            # The stamp is what lets a marker-proven adoption — which has no
            # hash by design — survive a load that trusts nothing else, and
            # it is what an inherited id cannot have. It goes when the ids go:
            # a proof for a copy that is no longer there proves nothing, and
            # leaving it behind would let an id set later ride on it.
            if _holds_a_copy(entry):
                entry[PUSH_PROOF_KEY] = _identity_proof()
            else:
                entry.pop(PUSH_PROOF_KEY, None)

            # Validate before writing, so a refused push leaves no residue.
            self._check_id_ownership_locked(uid, entry.get("workout_id"),
                                            entry.get("schedule_id"))

            self._state["garmin_push"][uid] = entry
            self._append_history("push", uid, actor, before, entry,
                                 undoable=False)
            self._save_locked()
            return _push_view(entry)

    def mark_push_failed(self, uid, error, actor="system"):
        return self.record_push(uid, push_state="failed", error=str(error),
                                actor=actor)

    def clear_push(self, uid, actor="system"):
        """Forget Garmin ids for a uid, after it has been unscheduled.

        The entry goes entirely rather than being blanked in place. A record
        holding nothing is exactly the state v1's tombstone got stuck in, and
        the only honest answer to "is anything of ours on Garmin?" once the
        calendar entry is gone is "no record at all".
        """
        with self._lock:
            entry = self._state["garmin_push"].pop(uid, None)
            if entry is None:
                return None
            self._append_history("push_clear", uid, actor, entry, None,
                                 undoable=False)
            self._save_locked()
            return None

    def pushed_uids(self):
        """Uids this app has scheduled on Garmin. The push flow must never
        unschedule anything outside this set — workouts the user created in
        Garmin Connect directly are not ours to touch."""
        with self._lock:
            return {
                uid: _push_view(entry)
                for uid, entry in self._state["garmin_push"].items()
                if entry.get("workout_id") is not None
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

            known = set(self._state["workout_overrides"]) \
                | set(self._state["garmin_push"])
            claimed = known & set(plan_by_uid)

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
            for old_uid in list(dict.fromkeys(known)):
                record = self._state["workout_overrides"].get(old_uid)
                push = self._state["garmin_push"].get(old_uid)

                if old_uid in plan_by_uid:
                    # Still present upstream; make sure it isn't flagged.
                    if record and record.get("orphaned_since"):
                        record["orphaned_since"] = None
                    continue

                # A push-only uid keeps its own copy of the plan snapshot, so
                # a re-keyed feed can still find the session Garmin holds.
                snapshot = (record or {}).get("plan_snapshot") \
                    or (push or {}).get("plan_snapshot") or {}
                fp = snapshot.get("fingerprint")
                if not fp and snapshot:
                    fp = fingerprint(snapshot.get("title"),
                                     snapshot.get("date"))
                candidates = by_fp.get(fp, []) if fp else []

                if len(candidates) == 1:
                    new_uid = candidates[0]
                    before = copy.deepcopy(record)
                    moved = None
                    if record is not None:
                        moved = copy.deepcopy(record)
                        moved["uid"] = new_uid
                        moved["orphaned_since"] = None
                        moved["updated_at"] = _now()
                        del self._state["workout_overrides"][old_uid]
                        self._state["workout_overrides"][new_uid] = moved
                    if push is not None:
                        # The push identity follows the workout to its new
                        # uid. Left behind, the app loses its only handle on
                        # the copy already on the Garmin calendar and the
                        # next push stacks a duplicate beside it.
                        moved_push = copy.deepcopy(push)
                        moved_push["uid"] = new_uid
                        del self._state["garmin_push"][old_uid]
                        self._state["garmin_push"][new_uid] = moved_push
                    by_fp[fp] = []
                    self._append_history("rekey", new_uid, actor, before,
                                         moved, undoable=False)
                    rekeyed.append((old_uid, new_uid))
                elif record is not None and not record.get("orphaned_since"):
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
    print(f"  on garmin:  {stats['pushed_count']}")
    print(f"  day rules:  {stats['availability_count']}")
    print(f"  proposals:  {stats['pending_proposals']} pending")
    if stats["load_error"]:
        print(f"  load error: {stats['load_error']}", file=sys.stderr)
