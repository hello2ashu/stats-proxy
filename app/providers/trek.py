"""Trek stats provider.

Env vars: TREK_URL, TREK_API_KEY.
TREK_URL is expected to be the FULL endpoint (e.g.
"https://trek.../api/v1/stats"), matching how it's set in your actual
compose file -- not just the host. Don't append a path here.

Response shape already confirmed (total_trips/total_countries/
total_cities/last_trip.country) -- matches your dashboard's 4 trips /
19 countries / 16 cities / US. Passed straight through unchanged.
"""
import os
import requests

URL = os.environ.get("TREK_URL", "")
API_KEY = os.environ.get("TREK_API_KEY", "")
TIMEOUT = 5


def get_stats():
    headers = {"X-API-Key": API_KEY}
    r = requests.get(URL, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()  # already {total_trips, total_countries, total_cities, last_trip: {country}}