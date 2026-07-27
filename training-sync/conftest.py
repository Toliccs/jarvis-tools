"""Test-wide protection for the athlete's real plan.

`PlanStore` resolves its path at construction: an explicit argument, else
``TRAINING_SYNC_STATE_PATH``, else ``DEFAULT_STATE_PATH`` — which is the
athlete's live ``.plan_state.json``. That default is right in production and
a trap in a test: anything that builds a non-demo app, or a bare
``PlanStore()``, reads and then *rewrites* the real plan.

`test_web_app.py` already points the env var at a tmp dir per test, but that
fixture only covers that one file and only for the duration of a test. It does
not cover a new test file, an ad-hoc script run from this directory, or a push
job whose background thread constructs its store after the fixture has been
torn down — and the last of those has actually happened here.

So the redirect is set once for the whole session, at import time, before any
test or fixture runs, and is never torn down. Tests that genuinely want the
real file still pass its path explicitly (see
`test_the_real_state_file_is_unaffected_and_never_touched`), which this does
not affect — that test reads, and `load` does not write.
"""
import os
import tempfile

_SESSION_STATE_DIR = tempfile.mkdtemp(prefix="training-sync-tests-")

os.environ.setdefault(
    "TRAINING_SYNC_STATE_PATH",
    os.path.join(_SESSION_STATE_DIR, "plan_state.json"),
)

# Writing to a real Garmin account is opt-in, and no test may inherit that
# opt-in from the machine it happens to be running on.
os.environ.pop("TRAINING_SYNC_ALLOW_GARMIN_WRITES", None)
