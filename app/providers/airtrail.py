"""Airtrail stats provider.

Env vars: AIRTRAIL_URL, AIRTRAIL_TOKEN.
AIRTRAIL_URL is expected to be the FULL endpoint (e.g.
"https://airtrail.../api/stats"), matching how it's set in your actual
compose file -- not just the host. Don't append a path here.

Response shape already confirmed (flights/distanceKm/durationSeconds/
airports under a "stats" key) -- matches your dashboard's 8 flights /
39,671 miles / 83 hours / 4 airports. Passed straight through unchanged.
"""
import os
import requests

URL = os.environ.get("AIRTRAIL_URL", "")
TOKEN = os.environ.get("AIRTRAIL_TOKEN", "")
TIMEOUT = 5


def get_stats():
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    r = requests.get(URL, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()  # already {"stats": {flights, distanceKm, durationSeconds, airports}}
