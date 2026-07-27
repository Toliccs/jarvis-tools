"""Glue for an external assistant reading this athlete's training.

Two things live here, both **read-only**:

- ``get_live_feed``          — what he actually rode, newest first (Garmin,
                               optionally cross-checked against Strava).
- ``suggest_plan_adjustment`` — what the engine would propose for a week.

Nothing in this module changes anything. It does not write to Garmin, it does
not write to TrainingPeaks (which is read-only by nature — see README.md), and
it does not touch the local override store. That is not an oversight: applying
a change and sending it to Garmin are gated on the athlete's explicit consent,
and those gates live in the running web app. See "Applying anything" below.

Typical use::

    # Run from inside training-sync/ — the folder name has a hyphen, so there
    # is no `training_sync` package to import.
    from garmin_client import GarminClient
    from trainingpeaks_calendar_feed import TrainingPeaksCalendarFeed
    from alfred_adapter import get_live_feed, suggest_plan_adjustment

    plan   = TrainingPeaksCalendarFeed().upcoming_workouts(limit=14)
    recent = get_live_feed(GarminClient(), limit=5)

    proposals = suggest_plan_adjustment(
        plan,
        unavailable_days={"2026-07-28": "work"},   # "I'm working Tuesday"
    )
    for p in proposals:
        print(p["action"], p["title"], "->", p["proposed_date"])
        print("   because:", p["reason"])

Applying anything
-----------------
Every proposal comes back with ``requires_approval: True``, and that is
literal. To act on one, go through the running web app's HTTP API
(``python3 web_app.py``, then ``127.0.0.1:8765``):

    POST /api/proposals/{id}/approve   apply it to the local plan
    PATCH /api/workouts/{uid}          {"push_approved": true}
    POST /api/push  {"dry_run": true}  preview — makes no network call
    POST /api/push  {"dry_run": false} send to Garmin

That path holds the consent gates, the ``TRAINING_SYNC_ALLOW_GARMIN_WRITES``
switch (off by default) and the audit history. This module deliberately does
not wrap any of it — a second route to a write is a second route to a write
that lies about having happened. Read ``GET /api/status`` first:
``capabilities.can_write_to_garmin`` is false unless credentials, the write
switch and not-demo-mode are all true.
"""

import os
import sys
from dataclasses import dataclass

# `scheduler` and its siblings are top-level modules in this folder, not a
# package. Make them importable however this file was reached.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


@dataclass
class LiveFeedEntry:
    source: str
    name: str
    start_time: str
    raw: dict


def get_live_feed(garmin_client, strava_client=None, limit=5):
    """Recent completed activities, most recent first.

    Garmin is the source of truth for effort data; Strava, if passed, is only
    a cross-check on what is public-facing. Read-only.
    """
    feed = [
        LiveFeedEntry(
            source="garmin",
            name=a.get("activityName"),
            start_time=a.get("startTimeLocal"),
            raw=a,
        )
        for a in garmin_client.recent_activities(limit=limit)
    ]
    if strava_client:
        feed += [
            LiveFeedEntry(
                source="strava",
                name=a.get("name"),
                start_time=a.get("start_date_local"),
                raw=a,
            )
            for a in strava_client.recent_activities(limit=limit)
        ]
    feed.sort(key=lambda e: e.start_time or "", reverse=True)
    return feed


def suggest_plan_adjustment(planned_workouts, unavailable_days=None,
                            garmin_signal=None, race_dates=None, **kwargs):
    """What the engine would propose for this week. Applies nothing.

    A thin, JSON-friendly wrapper over ``scheduler.propose_adjustments``, which
    is where the real work happens. This used to be an unimplemented stub with
    a note saying the rules were a coaching decision; the rules now exist, in
    ``scheduler.py``, written against the spec in that module's docstring.

    ``planned_workouts``  ``{date, title, description, uid}`` dicts — exactly
                          what ``TrainingPeaksCalendarFeed`` returns.
    ``unavailable_days``  dates, ``{date: reason}``, or ``{"date", "reason"}``
                          dicts. This is the "I can't ride Tuesday, I'm
                          working" input.
    ``garmin_signal``     ``{"readiness": {"score": 41, ...},
                          "training_status": "..."}``, or None. Optional — an
                          Edge 520 does not produce a readiness score, so for
                          this athlete it is often legitimately absent.
    ``race_dates``        dates, or ``{"date", "title", "priority",
                          "duration_min"}`` dicts. A race is a fixed point: the
                          engine will not propose moving one.

    Returns a list of plain dicts (``scheduler.Proposal.to_dict``). Each one
    carries ``action`` (move/soften/drop/keep), ``proposed_date``, a plain
    ``reason`` sentence, ``reason_details``, ``confidence``, the days it
    rejected and why (``alternatives``), and ``requires_approval: True``.

    Sessions it could not place come back as ``kind == "NO_PROPOSAL"`` with
    every rejected day listed. Those are the honest "I can't fit this" answer,
    not suggestions — they are not approvable and must not be presented as
    something to accept.

    Pure: no network call, no state written, nothing applied. Same inputs give
    the same proposal ids, so re-running after a partial approval cannot
    duplicate or contradict what was already decided.

    Note the intensity and duration of each session are *inferred from the
    title and description text* — the calendar feed carries no structured
    targets — so a proposal is only as good as that reading. See "The plan feed
    only gives text" in README.md.
    """
    from scheduler import propose_adjustments

    proposals = propose_adjustments(
        planned_workouts,
        unavailable_days=unavailable_days,
        garmin_signal=garmin_signal,
        race_dates=race_dates,
        **kwargs,
    )
    return [p.to_dict() for p in proposals]
