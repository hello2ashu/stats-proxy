"""Karakeep stats provider.

Env vars: KARAKEEP_URL, KARAKEEP_API_KEY.

Confirmed from Homepage's actual widget.js AND component.jsx source:
  - endpoint: /api/v1/users/me/stats
  - fields:   numBookmarks, numFavorites, numArchived, numHighlights
              (also numLists and numTags are available if you want them
              on the dashboard too -- not currently mapped in
              services.yaml, add a field below + a mapping there if so)
All confirmed exact, not guessed.
"""
import os
import requests

BASE = os.environ.get("KARAKEEP_URL", "").rstrip("/")
API_KEY = os.environ.get("KARAKEEP_API_KEY", "")
TIMEOUT = 5


def get_stats():
    headers = {"Authorization": f"Bearer {API_KEY}"}
    r = requests.get(f"{BASE}/api/v1/users/me/stats", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return {
        "bookmarks": data.get("numBookmarks"),
        "favorites": data.get("numFavorites"),
        "archived": data.get("numArchived"),
        "highlights": data.get("numHighlights"),
        "lists": data.get("numLists"),
        "tags": data.get("numTags"),
    }