"""Airtrail stats provider.

Env vars: AIRTRAIL_URL, AIRTRAIL_TOKEN.

Response shape already confirmed (flights/distanceKm/durationSeconds/
airports under a "stats" key) -- matches your dashboard's 8 flights /
39,671 miles / 83 hours / 4 airports. Passed straight through unchanged.
"""
import os
import requests

BASE = os.environ.get("AIRTRAIL_URL", "").rstrip("/")
TOKEN = os.environ.get("AIRTRAIL_TOKEN", "")
TIMEOUT = 5


def get_stats():
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    r = requests.get(f"{BASE}/api/stats", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()  # already {"stats": {flights, distanceKm, durationSeconds, airports}}
