"""Linkwarden stats provider.

Env vars: LINKWARDEN_URL, LINKWARDEN_TOKEN.

VERIFY: field names on the dashboard endpoint -- your Homepage widget
shows 247 links / 64 collections / 2 tags, so confirm those three exist
under these names in your instance's API response.
"""
import os
import requests

BASE = os.environ.get("LINKWARDEN_URL", "").rstrip("/")
TOKEN = os.environ.get("LINKWARDEN_TOKEN", "")
TIMEOUT = 5


def get_stats():
    headers = {"Authorization": f"Bearer {TOKEN}"}
    r = requests.get(f"{BASE}/api/v1/dashboard", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return {
        "links": data.get("numberOfLinks"),             # VERIFY
        "collections": data.get("numberOfCollections"),  # VERIFY
        "tags": data.get("numberOfTags"),                # VERIFY
    }
