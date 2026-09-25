"""Dockhand stats provider.

Env vars:
  DOCKHAND_URL       (already referenced elsewhere in this app per the
                      startup log message -- same instance, reused here)
  DOCKHAND_USERNAME
  DOCKHAND_PASSWORD
  DOCKHAND_API_KEY   used only for /api/self-update/check

Per Homepage's own docs (gethomepage.dev/widgets/services/dockhand), the
built-in "dockhand" widget makes exactly ONE API call and picks up to 4
of these 11 fields to display: running, stopped, paused, total, cpu,
memory, images, volumes, events_today, pending_updates, stacks. That's
why your dashboard's first three cards showed a consistent, complete
set of numbers -- they're all reading the same underlying response.

VERIFY (unresolved): the exact endpoint path AND the auth mechanism.
The docs note "currently supports Dockhand's local authentication only",
which suggests a session/token login rather than raw HTTP Basic Auth --
plain basic auth against a guessed path is what returned 401 last time.
The most reliable fix: open DevTools -> Network tab on your dashboard,
reload, and copy the exact request Homepage's own widget sends (URL,
method, headers/cookies). Update _login() and STATS_PATH below to match.
"""
import os
import requests

BASE = os.environ.get("DOCKHAND_URL", "").rstrip("/")
USERNAME = os.environ.get("DOCKHAND_USERNAME", "")
PASSWORD = os.environ.get("DOCKHAND_PASSWORD", "")
API_KEY = os.environ.get("DOCKHAND_API_KEY", "")
TIMEOUT = 5

STATS_PATH = "/api/dashboard"  # VERIFY -- placeholder, confirm via DevTools


def get_stats():
    # VERIFY -- placeholder auth. If Dockhand uses session/token login
    # instead of Basic Auth, replace this with a login POST that returns
    # a token/cookie, then attach it to the stats request below.
    r = requests.get(f"{BASE}{STATS_PATH}", auth=(USERNAME, PASSWORD), timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return {
        "running": data.get("running"),
        "stopped": data.get("stopped"),
        "paused": data.get("paused"),
        "total": data.get("total"),
        "cpu": data.get("cpu"),
        "memory": data.get("memory"),
        "images": data.get("images"),
        "stacks": data.get("stacks"),
        "volumes": data.get("volumes"),
        "events_today": data.get("events_today"),
        "pending_updates": data.get("pending_updates"),
    }


def get_version():
    # Confirmed via direct curl: requires "Bearer " prefix (raw key alone
    # returns 401, despite the original services.yaml sending it raw).
    headers = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}
    r = requests.get(f"{BASE}/api/self-update/check", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return {
        "updateAvailable": data.get("updateAvailable"),
        "currentImage": data.get("currentImage"),
        "newImage": data.get("newImage"),
    }
