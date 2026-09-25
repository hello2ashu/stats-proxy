"""Trek stats provider.

Env vars: TREK_URL, TREK_API_KEY.

Response shape already confirmed (total_trips/total_countries/
total_cities/last_trip.country) -- matches your dashboard's 4 trips /
19 countries / 16 cities / US. Passed straight through unchanged.
"""
import os
import requests

BASE = os.environ.get("TREK_URL", "").rstrip("/")
API_KEY = os.environ.get("TREK_API_KEY", "")
TIMEOUT = 5


def get_stats():
    headers = {"X-API-Key": API_KEY}
    r = requests.get(f"{BASE}/api/v1/stats", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()  # already {total_trips, total_countries, total_cities, last_trip: {country}}
