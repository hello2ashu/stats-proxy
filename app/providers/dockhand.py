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


import re
import subprocess
import time

GITHUB_LATEST_URL = "https://api.github.com/repos/Finsys/dockhand/releases/latest"
CONTAINER_NAME = "Dockhand"
_VERSION_CACHE = {"ts": 0, "latest": None}
_CACHE_TTL = 3600  # GitHub's unauthenticated limit is 60 req/hr, so cache for an hour


def _parse_version(v):
    """'v1.2.3' -> (1, 2, 3). Ignores any suffix like '-beta'."""
    nums = re.findall(r"\d+", v.split("-")[0])
    return tuple(int(n) for n in nums)


def _get_current_version():
    out = subprocess.run(
        ["docker", "exec", CONTAINER_NAME,
         "node", "-p", "require('/app/package.json').version"],
        capture_output=True, text=True, timeout=TIMEOUT, check=True,
    )
    return out.stdout.strip()


def _get_latest_version():
    now = time.time()
    if _VERSION_CACHE["latest"] and now - _VERSION_CACHE["ts"] < _CACHE_TTL:
        return _VERSION_CACHE["latest"]
    r = requests.get(
        GITHUB_LATEST_URL,
        headers={"Accept": "application/vnd.github+json"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    tag = r.json()["tag_name"]
    _VERSION_CACHE.update(ts=now, latest=tag)
    return tag


def get_version():
    current = _get_current_version()
    latest = _get_latest_version()
    update_available = _parse_version(latest) > _parse_version(current)
    return {
        "updateAvailable": update_available,   # True / False
        "currentImage": current,
        "newImage": latest.lstrip("v"),
    }
