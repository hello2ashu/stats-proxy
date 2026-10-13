"""Dockhand stats provider.

Env vars:
  DOCKHAND_URL      base URL of the Dockhand instance
  DOCKHAND_TOKEN    API token (dh_...), sent as "Authorization: Bearer <token>"
                    (DOCKHAND_API_KEY is accepted as a fallback name)

VERIFY: STATS_PATH is still a guess. Test it with:
  docker exec stats-proxy sh -c 'curl -s -i -H "Authorization: Bearer $DOCKHAND_TOKEN" "$DOCKHAND_URL/api/dashboard"'
A 200 means it's right. A 404 means copy the real path from DevTools -> Network.
"""
import os
import re
import subprocess
import time

import requests

BASE = os.environ.get("DOCKHAND_URL", "").rstrip("/")
API_KEY = os.environ.get("DOCKHAND_TOKEN") or os.environ.get("DOCKHAND_API_KEY", "")
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}
TIMEOUT = 5

STATS_PATH = "/api/dashboard"  # VERIFY -- placeholder

GITHUB_LATEST_URL = "https://api.github.com/repos/Finsys/dockhand/releases/latest"
CONTAINER_NAME = "Dockhand"
_VERSION_CACHE = {"ts": 0, "latest": None}
_CACHE_TTL = 3600  # GitHub's unauthenticated limit is 60 req/hr


def get_stats():
    r = requests.get(f"{BASE}{STATS_PATH}", headers=HEADERS, timeout=TIMEOUT)
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


def _parse_version(v):
    """'v1.2.3' -> (1, 2, 3). Ignores any suffix like '-beta'."""
    nums = re.findall(r"\d+", v.split("-")[0])
    return tuple(int(n) for n in nums)


def _get_current_version():
    try:
        out = subprocess.run(
            ["docker", "exec", CONTAINER_NAME,
             "node", "-p", "require('/app/package.json').version"],
            capture_output=True, text=True, timeout=TIMEOUT, check=True,
        )
        return out.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        # No docker CLI/socket in this container: fall back to the image tag
        # reported by Dockhand itself (e.g. "finsys/dockhand:v1.2.3" -> "1.2.3").
        r = requests.get(f"{BASE}/api/self-update/check", headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        image = r.json().get("currentImage") or ""
        m = re.search(r"\d+(?:\.\d+)+", image)
        if not m:
            raise RuntimeError(f"could not determine current version from {image!r}")
        return m.group(0)


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
        "updateAvailable": update_available,   # use "yes" if update_available else "no" for text
        "currentImage": current,
        "newImage": latest.lstrip("v"),
    }