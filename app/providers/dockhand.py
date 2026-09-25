"""Dockhand stats provider.

Env vars:
  DOCKHAND_URL       (already referenced elsewhere in this app per the
                      startup log message -- same instance, reused here)
  DOCKHAND_USERNAME
  DOCKHAND_PASSWORD
  DOCKHAND_API_KEY   used only for /api/self-update/check

VERIFY: /api/containers, /api/resources, /api/activity are placeholders
inferred from the Homepage widget's field names (running/stopped/paused/
total, cpu/memory/images/stacks, volumes/events_today/pending_updates) --
not confirmed against Dockhand's real API. Hit each once after deploying
and fix paths/field names as needed; the self-update/check path is the
one field you already had verbatim in services.yaml, so that one's solid.
"""
import os
import requests

BASE = os.environ.get("DOCKHAND_URL", "").rstrip("/")
USERNAME = os.environ.get("DOCKHAND_USERNAME", "")
PASSWORD = os.environ.get("DOCKHAND_PASSWORD", "")
API_KEY = os.environ.get("DOCKHAND_API_KEY", "")
TIMEOUT = 5


def get_stats():
    auth = (USERNAME, PASSWORD)

    r = requests.get(f"{BASE}/api/containers", auth=auth, timeout=TIMEOUT)
    r.raise_for_status()
    containers = r.json()

    r = requests.get(f"{BASE}/api/resources", auth=auth, timeout=TIMEOUT)
    r.raise_for_status()
    resources = r.json()

    r = requests.get(f"{BASE}/api/activity", auth=auth, timeout=TIMEOUT)
    r.raise_for_status()
    activity = r.json()

    return {
        "running": containers.get("running"),
        "stopped": containers.get("stopped"),
        "paused": containers.get("paused"),
        "total": containers.get("total"),
        "cpu": resources.get("cpu"),
        "memory": resources.get("memory"),
        "images": resources.get("images"),
        "stacks": resources.get("stacks"),
        "volumes": activity.get("volumes"),
        "events_today": activity.get("events_today"),
        "pending_updates": activity.get("pending_updates"),
    }


def get_version():
    headers = {"Authorization": API_KEY, "Accept": "application/json"}
    r = requests.get(f"{BASE}/api/self-update/check", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return {
        "updateAvailable": data.get("updateAvailable"),
        "currentImage": data.get("currentImage"),
        "newImage": data.get("newImage"),
    }
