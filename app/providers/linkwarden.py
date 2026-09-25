"""Linkwarden stats provider.

Env vars: LINKWARDEN_URL, LINKWARDEN_TOKEN.

Confirmed from Homepage's actual widget.js AND component.jsx source:
there's no dedicated stats endpoint -- it calls /api/v1/collections and
/api/v1/tags and derives everything itself:
  - links:       sum of collection._count.links across all collections
  - collections: length of the collections list
  - tags:        length of the tags list
Collections may be wrapped as {"response": [...]} or returned raw.
Tags may be wrapped as {"response": [...]}, {"data": {"tags": [...]}},
or returned raw -- all three are handled, matching the real component.
"""
import os
import requests

BASE = os.environ.get("LINKWARDEN_URL", "").rstrip("/")
TOKEN = os.environ.get("LINKWARDEN_TOKEN", "")
TIMEOUT = 5


def get_stats():
    headers = {"Authorization": f"Bearer {TOKEN}"}

    r = requests.get(f"{BASE}/api/v1/collections", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    collections = payload.get("response", payload) if isinstance(payload, dict) else payload

    r = requests.get(f"{BASE}/api/v1/tags", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict):
        tags = payload.get("response", (payload.get("data") or {}).get("tags", payload))
    else:
        tags = payload

    collections = collections if isinstance(collections, list) else []
    tags = tags if isinstance(tags, list) else []

    total_links = sum((c.get("_count") or {}).get("links", 0) for c in collections)

    return {
        "links": total_links,
        "collections": len(collections),
        "tags": len(tags),
    }