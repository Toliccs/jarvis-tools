"""Strava OAuth2 client for reading activities.

Since Garmin already reliably pushes activities to Strava, this can serve
as an alternate/confirming live-feed source alongside garmin_client.py.
Requires an app registered at https://www.strava.com/settings/api and an
initial OAuth authorization to obtain a refresh token (one-time manual
step, not something to automate around).
"""

import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"


class StravaClient:
    def __init__(self, client_id=None, client_secret=None, refresh_token=None):
        self.client_id = client_id or os.environ["STRAVA_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["STRAVA_CLIENT_SECRET"]
        self.refresh_token = refresh_token or os.environ["STRAVA_REFRESH_TOKEN"]
        self._access_token = None
        self._expires_at = 0

    def _ensure_token(self):
        if self._access_token and time.time() < self._expires_at - 60:
            return
        resp = requests.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        })
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = data["expires_at"]
        # Strava may rotate the refresh token; keep the latest in memory.
        self.refresh_token = data.get("refresh_token", self.refresh_token)

    def _get(self, path, params=None):
        self._ensure_token()
        resp = requests.get(
            f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {self._access_token}"},
            params=params or {},
        )
        resp.raise_for_status()
        return resp.json()

    def recent_activities(self, limit=10):
        return self._get("/athlete/activities", params={"per_page": limit})

    def latest_activity(self):
        activities = self.recent_activities(limit=1)
        return activities[0] if activities else None


if __name__ == "__main__":
    client = StravaClient()
    latest = client.latest_activity()
    if latest:
        print(f"Latest Strava activity: {latest.get('name')} "
              f"({latest.get('start_date_local')})")
    else:
        print("No recent activities found.")
