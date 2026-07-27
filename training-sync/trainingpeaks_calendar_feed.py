"""Reads planned workouts from your TrainingPeaks calendar feed.

TrainingPeaks lets you generate a personal calendar feed (a secret ICS/
webcal URL) for subscribing to your schedule from Google Calendar, Outlook,
etc. — the same feature covered in their help center under "calendar feed" /
"subscribe to your calendar". It's a read-only, per-account URL you generate
yourself from Settings; this module just fetches and parses it, so it works
today with no partner approval.

Limitations vs. the Partner API (trainingpeaks_client.py):
- No write-back — this is read-only by nature of being a calendar feed.
- Structured fields (target power/HR/TSS) are only available if TrainingPeaks
  includes them in the event description text; not guaranteed for every
  workout type.

Treat the feed URL as a secret (anyone with it can see your schedule) — keep
it in .env, never commit it or log it.
"""

import os

import requests
from dotenv import load_dotenv
from icalendar import Calendar

load_dotenv()


class TrainingPeaksCalendarFeed:
    def __init__(self, feed_url=None):
        self.feed_url = feed_url or os.environ["TRAININGPEAKS_CALENDAR_FEED_URL"]

    def planned_workouts(self):
        url = self.feed_url.replace("webcal://", "https://", 1)
        resp = requests.get(url)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.content)

        workouts = []
        for component in cal.walk("VEVENT"):
            start = component.get("dtstart")
            workouts.append({
                "date": start.dt.isoformat() if start else None,
                "title": str(component.get("summary", "")),
                "description": str(component.get("description", "")),
                "uid": str(component.get("uid", "")),
            })
        workouts.sort(key=lambda w: w["date"] or "")
        return workouts

    def upcoming_workouts(self, from_date=None, limit=7):
        from datetime import date as _date

        from_date = from_date or _date.today().isoformat()
        return [w for w in self.planned_workouts() if w["date"] and w["date"] >= from_date][:limit]


if __name__ == "__main__":
    feed = TrainingPeaksCalendarFeed()
    for workout in feed.upcoming_workouts():
        print(f"{workout['date']}: {workout['title']}")
