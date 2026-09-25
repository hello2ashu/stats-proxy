"""Dawarich stats provider.

Env vars: DAWARICH_URL, DAWARICH_API_KEY.

Field names below are already confirmed -- your original services.yaml
called this same endpoint directly with the key in the URL, and the
dashboard numbers (408,653 miles / 10 countries / 248 cities) already
matched these exact fields. Only the credential moved; the shape didn't.
"""
import os
import requests

BASE = os.environ.get("DAWARICH_URL", "").rstrip("/")
API_KEY = os.environ.get("DAWARICH_API_KEY", "")
TIMEOUT = 5


def get_stats():
    r = requests.get(f"{BASE}/api/v1/stats", params={"api_key": API_KEY}, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return {
        "totalDistanceKm": data.get("totalDistanceKm"),
        "totalCountriesVisited": data.get("totalCountriesVisited"),
        "totalCitiesVisited": data.get("totalCitiesVisited"),
    }
