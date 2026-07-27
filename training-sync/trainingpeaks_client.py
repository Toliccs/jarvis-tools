"""TrainingPeaks Partner API client skeleton.

TrainingPeaks does not offer self-serve API keys for individual athletes.
Reading/writing structured workouts requires approved Partner API OAuth2
credentials (apply at https://www.trainingpeaks.com/partners/). This client
will not attempt to scrape or drive a headless browser against
trainingpeaks.com to work around that — doing so would violate their
Terms of Service.

Fill in TRAININGPEAKS_CLIENT_ID / SECRET / REFRESH_TOKEN in .env once you
have partner credentials and this client becomes functional; until then
every call raises TrainingPeaksNotAuthorized.
"""

import os

from dotenv import load_dotenv

load_dotenv()

TOKEN_URL = "https://oauth.trainingpeaks.com/oauth/token"
API_BASE = "https://api.trainingpeaks.com/v1"


class TrainingPeaksNotAuthorized(RuntimeError):
    pass


class TrainingPeaksClient:
    def __init__(self, client_id=None, client_secret=None, refresh_token=None):
        self.client_id = client_id or os.environ.get("TRAININGPEAKS_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("TRAININGPEAKS_CLIENT_SECRET")
        self.refresh_token = refresh_token or os.environ.get("TRAININGPEAKS_REFRESH_TOKEN")
        self._access_token = None

    def _require_credentials(self):
        if not (self.client_id and self.client_secret and self.refresh_token):
            raise TrainingPeaksNotAuthorized(
                "No TrainingPeaks Partner API credentials configured. "
                "Apply at https://www.trainingpeaks.com/partners/ and set "
                "TRAININGPEAKS_CLIENT_ID/SECRET/REFRESH_TOKEN in .env. "
                "See training-sync/README.md for details."
            )

    def recent_workouts(self, athlete_id, limit=10):
        self._require_credentials()
        raise NotImplementedError(
            "Implement once Partner API credentials are available — "
            "see TrainingPeaks Partner API docs for the workouts endpoint."
        )

    def update_planned_workout(self, athlete_id, workout_id, changes):
        """Write-back for race-day plan adjustments. Requires Partner API
        write scope, which is granted per-integration during approval."""
        self._require_credentials()
        raise NotImplementedError(
            "Implement once Partner API credentials are available."
        )


if __name__ == "__main__":
    client = TrainingPeaksClient()
    try:
        client.recent_workouts(athlete_id="me")
    except TrainingPeaksNotAuthorized as exc:
        print(exc)
