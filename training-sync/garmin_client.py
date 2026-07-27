"""Authenticated Garmin Connect client.

Uses your own Garmin login (via the `garminconnect` package) against the
same endpoints the Garmin Connect mobile app uses. This is not an official
public API, but it is authenticated as you, not bot-detection evasion.
"""

import os

from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv()


class GarminClient:
    def __init__(self, email=None, password=None):
        email = email or os.environ["GARMIN_EMAIL"]
        password = password or os.environ["GARMIN_PASSWORD"]
        self._api = Garmin(email, password)
        self._api.login()

    def recent_activities(self, limit=10):
        return self._api.get_activities(0, limit)

    def latest_activity(self):
        activities = self.recent_activities(limit=1)
        return activities[0] if activities else None

    def activity_details(self, activity_id):
        return self._api.get_activity_details(activity_id)

    def daily_stats(self, date_str):
        """date_str format: YYYY-MM-DD"""
        return self._api.get_stats(date_str)

    def training_status(self, date_str):
        return self._api.get_training_status(date_str)


if __name__ == "__main__":
    client = GarminClient()
    latest = client.latest_activity()
    if latest:
        print(f"Latest activity: {latest.get('activityName')} "
              f"({latest.get('startTimeLocal')})")
    else:
        print("No recent activities found.")
