# Training Sync — Garmin + TrainingPeaks integration for Alfred

Data-access layer for an "Alfred can adjust my training after a race" workflow.
This package only handles **legitimate, credentialed data access** — it does
not attempt to defeat bot detection or scrape TrainingPeaks in violation of
their Terms of Service.

## Status of each integration

| Source | Access method | Status |
|---|---|---|
| Garmin Connect | Authenticated client using your own login (`garminconnect`/`garth`, same endpoints the mobile app uses) | **Working** — implemented in `garmin_client.py` |
| Strava | Official OAuth API + webhooks | **Working** — implemented in `strava_client.py`, useful as an alternate live-feed source since Garmin already pushes here reliably |
| TrainingPeaks | Official Partner API (OAuth2) | **Blocked on API access** — see below |

### Why TrainingPeaks is different

TrainingPeaks does not offer a self-serve API key for individual/self-coached
athletes. Third-party apps (TrainerRoad, Zwift, etc.) integrate through
TrainingPeaks' **Partner API**, which requires applying at
https://www.trainingpeaks.com/partners/ and getting approved as an
integration partner. There is no supported way to get read/write access to
your own account's structured workout data without that approval — headless
scraping or anti-bot-evasion tooling against their site would violate their
ToS, so this repo won't build that.

`trainingpeaks_client.py` is written as a ready-to-fill skeleton: once you
have partner OAuth2 credentials, drop them into the `.env` and the client
will work. Until then, calls raise `TrainingPeaksNotAuthorized` with a
pointer back to this README.

## Setup

```bash
cd training-sync
pip install -r requirements.txt
cp .env.example .env   # fill in your own credentials, never commit .env
```

Garmin auth uses your normal Garmin Connect email/password (stored only in
your local `.env`, never logged or transmitted anywhere but Garmin's own
login endpoint).

## Layout

- `garmin_client.py` — pulls recent activities, current stats, and (where
  the API exposes it) live activity data from Garmin Connect.
- `strava_client.py` — OAuth2 client + webhook handler for activity events.
- `trainingpeaks_client.py` — skeleton for the official Partner API; not
  functional until you have approved credentials.
- `alfred_adapter.py` — example glue code showing how Alfred would pull a
  live feed and where race-day training-adjustment logic plugs in. The
  actual adjustment logic (how a race result should change your plan) is
  intentionally left as a stub — that's a coaching decision, not something
  to hardcode without knowing your training methodology.

## Plugging into Alfred

These clients are plain Python with no framework dependency, so import them
directly from wherever Alfred's own code runs:

```python
from training_sync.garmin_client import GarminClient
from training_sync.alfred_adapter import get_live_feed

client = GarminClient()
feed = get_live_feed(client)
```
