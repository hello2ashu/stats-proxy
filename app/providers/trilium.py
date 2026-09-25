"""Trilium stats provider.

Env vars: TRILIUM_URL, TRILIUM_TOKEN.

Confirmed from Homepage's actual widget.js AND component.jsx source:
  - endpoint: /etapi/metrics?format=json
  - version:  metricsData.version.app
  - notes:    metricsData.database.activeNotes
  - db size:  metricsData.statistics.databaseSizeBytes  (bytes, an int)
Auth is the raw token in Authorization, no Bearer prefix (per Trilium's
own metrics docs). All three fields are exact, not guessed.
"""
import os
import requests

BASE = os.environ.get("TRILIUM_URL", "").rstrip("/")
TOKEN = os.environ.get("TRILIUM_TOKEN", "")
TIMEOUT = 5


def get_stats():
    headers = {"Authorization": TOKEN}
    r = requests.get(f"{BASE}/etapi/metrics", params={"format": "json"},
                      headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return {
        "version": (data.get("version") or {}).get("app"),
        "notes": (data.get("database") or {}).get("activeNotes", 0),
        "databaseSize": (data.get("statistics") or {}).get("databaseSizeBytes", 0),
    }