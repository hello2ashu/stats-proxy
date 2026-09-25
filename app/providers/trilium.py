"""Trilium stats provider, via its ETAPI.

Env vars: TRILIUM_URL, TRILIUM_TOKEN.

VERIFY: /etapi/app-info gives version reliably, but note count and
database size aren't standard app-info fields in every Trilium version --
your dashboard shows 430 notes / 21.2 MB, so there's a real endpoint for
this somewhere (possibly a search query for note count, and a separate
stats/backup endpoint for db size). Confirm against your instance and
adjust below.
"""
import os
import requests

BASE = os.environ.get("TRILIUM_URL", "").rstrip("/")
TOKEN = os.environ.get("TRILIUM_TOKEN", "")
TIMEOUT = 5


def get_stats():
    headers = {"Authorization": TOKEN}
    r = requests.get(f"{BASE}/etapi/app-info", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    info = r.json()
    return {
        "version": info.get("appVersion"),
        "notes": info.get("noteCount"),           # VERIFY
        "databaseSize": info.get("databaseSize"),  # VERIFY
    }
