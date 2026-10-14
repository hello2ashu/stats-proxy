"""Dockhand stats provider.

Env vars:
  DOCKHAND_URL            base URL of the Dockhand instance
  DOCKHAND_TOKEN          API token (dh_...), sent as "Authorization: Bearer <token>"
                          (DOCKHAND_API_KEY is accepted as a fallback name)
  DOCKHAND_ENV            Dockhand environment id for stats (default: 1 = local)
  DOCKHAND_CONTAINER      name of the Dockhand container (default: Dockhand)
  DOCKHAND_STATS_TIMEOUT  seconds to wait for /api/dashboard/stats (default: 30)
  DOCKER_SOCK             docker socket path (default: /var/run/docker.sock)

Stats: GET /api/dashboard/stats?env=<id>. Response shape (verified):
  containers{total,running,stopped,paused,restarting,unhealthy,pendingUpdates},
  images{total}, volumes{total}, stacks{total}, events{total,today},
  metrics{cpuPercent,memoryPercent,...}

Current version: read from /app/package.json inside the Dockhand container via the
Docker socket (same value as `docker exec Dockhand node -p "require('/app/package.json').version"`,
but needs no docker CLI). Latest version: GitHub releases. If the socket read or the
GitHub call fails, get_version() falls back to Dockhand's own /api/self-update/check flag.
"""
import http.client
import io
import json
import logging
import os
import re
import socket
import tarfile
import time

import requests

log = logging.getLogger("stats-proxy.dockhand")

BASE = os.environ.get("DOCKHAND_URL", "").rstrip("/")
API_KEY = os.environ.get("DOCKHAND_TOKEN") or os.environ.get("DOCKHAND_API_KEY", "")
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}
TIMEOUT = 5

STATS_PATH = "/api/dashboard/stats"
STATS_TIMEOUT = float(os.environ.get("DOCKHAND_STATS_TIMEOUT", "30"))
ENV_ID = os.environ.get("DOCKHAND_ENV", "1")

GITHUB_LATEST_URL = "https://api.github.com/repos/Finsys/dockhand/releases/latest"
CONTAINER_NAME = os.environ.get("DOCKHAND_CONTAINER", "Dockhand")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
_VERSION_CACHE = {"ts": 0, "latest": None}
_CACHE_TTL = 3600  # GitHub's unauthenticated limit is 60 req/hr


# ---- stats -----------------------------------------------------------------
def _r1(v):
    return round(v, 1) if isinstance(v, (int, float)) else v


def get_stats():
    r = requests.get(f"{BASE}{STATS_PATH}", params={"env": ENV_ID},
                     headers=HEADERS, timeout=STATS_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    c = d.get("containers") or {}
    m = d.get("metrics") or {}
    return {
        "running": c.get("running"),
        "stopped": c.get("stopped"),
        "paused": c.get("paused"),
        "total": c.get("total"),
        "restarting": c.get("restarting"),
        "unhealthy": c.get("unhealthy"),
        "cpu": _r1(m.get("cpuPercent")),            # percent
        "memory": _r1(m.get("memoryPercent")),      # percent
        "images": (d.get("images") or {}).get("total"),
        "volumes": (d.get("volumes") or {}).get("total"),
        "stacks": (d.get("stacks") or {}).get("total"),
        "events_today": (d.get("events") or {}).get("today"),
        "pending_updates": c.get("pendingUpdates"),
    }


# ---- version ---------------------------------------------------------------
def _parse_version(v):
    """'v1.2.3' -> (1, 2, 3). Ignores any suffix like '-beta'."""
    nums = re.findall(r"\d+", v.split("-")[0])
    return tuple(int(n) for n in nums)


class _UnixConn(http.client.HTTPConnection):
    """HTTP over a unix socket, so no docker CLI or extra packages are needed."""

    def __init__(self, path, timeout=TIMEOUT):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _get_current_version():
    """Same value as: docker exec Dockhand node -p "require('/app/package.json').version"."""
    conn = _UnixConn(DOCKER_SOCK)
    try:
        conn.request("GET", f"/containers/{CONTAINER_NAME}/archive?path=/app/package.json")
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"docker API {resp.status}: {body[:200]!r}")
    finally:
        conn.close()
    with tarfile.open(fileobj=io.BytesIO(body)) as tar:
        member = tar.getmembers()[0]  # the archive holds just package.json
        return json.load(tar.extractfile(member))["version"]


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
    try:
        current = _get_current_version()
        latest = _get_latest_version()
        update_available = _parse_version(latest) > _parse_version(current)
        return {
            "updateAvailable": update_available,   # use "yes" if update_available else "no" for text
            "currentImage": current,
            "newImage": latest.lstrip("v"),
        }
    except Exception as exc:
        # Socket not mounted / GitHub unreachable: trust Dockhand's own flag instead.
        log.warning("version check via socket+GitHub failed (%s: %s); using /api/self-update/check",
                    type(exc).__name__, exc)
        r = requests.get(f"{BASE}/api/self-update/check", headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        return {
            "updateAvailable": d.get("updateAvailable"),
            "currentImage": d.get("currentImage"),
            "newImage": d.get("newImage"),
        }
