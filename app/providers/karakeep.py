"""Karakeep stats provider.

Env vars: KARAKEEP_URL, KARAKEEP_API_KEY.

VERIFY: your dashboard shows bookmarks/favorites/archived/highlights (46,
0, 0, 0) -- confirm those four field names against your instance's
/api/v1/stats (or equivalent) response.
"""
import os
import requests

BASE = os.environ.get("KARAKEEP_URL", "").rstrip("/")
API_KEY = os.environ.get("KARAKEEP_API_KEY", "")
TIMEOUT = 5


def get_stats():
    headers = {"Authorization": f"Bearer {API_KEY}"}
    r = requests.get(f"{BASE}/api/v1/stats", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return {
        "bookmarks": data.get("numBookmarks"),    # VERIFY
        "favorites": data.get("numFavorites"),    # VERIFY
        "archived": data.get("numArchived"),      # VERIFY
        "highlights": data.get("numHighlights"),  # VERIFY
    }
