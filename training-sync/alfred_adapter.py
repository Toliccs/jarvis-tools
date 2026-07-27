"""Glue code showing how Alfred consumes the live feed and where race-day
training-adjustment logic plugs in.

The adjustment logic itself is intentionally left as a stub: how a race
result (e.g. blew up on the bike leg, ran negative splits, DNF) should
change next week's plan is a coaching decision specific to your training
philosophy, not something to hardcode here.
"""

from dataclasses import dataclass


@dataclass
class LiveFeedEntry:
    source: str
    name: str
    start_time: str
    raw: dict


def get_live_feed(garmin_client, strava_client=None, limit=5):
    """Merge recent activities from Garmin (source of truth for effort
    data) and optionally Strava (confirms what's public-facing), most
    recent first."""
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


def suggest_plan_adjustment(race_activity, upcoming_planned_workouts):
    """Extension point: given a completed race activity and the upcoming
    planned workouts (once trainingpeaks_client.py can fetch them), return
    a list of proposed changes for a human (or an approved write-back) to
    apply.

    Not implemented — plug in your own rules/model here once you know
    what signals you want to react to (e.g. HR drift, power decay,
    perceived effort notes, DNF/DNS).
    """
    raise NotImplementedError(
        "Define your own race-day adjustment rules here before wiring "
        "this into an automated write-back."
    )
