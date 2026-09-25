"""Synology DSM stats provider (session-login webapi, same API Homepage's
built-in diskstation widget uses).

Env vars: SYNOLOGY_URL, SYNOLOGY_USERNAME, SYNOLOGY_PASSWORD,
SYNOLOGY_VOLUME (defaults to volume_1).

VERIFY: cpu_load / mem_load field names on SYNO.Core.System info -- these
vary a bit by DSM version. Check the raw response once if the numbers
look off.
"""
import os
import requests

BASE = os.environ.get("SYNOLOGY_URL", "").rstrip("/")
USERNAME = os.environ.get("SYNOLOGY_USERNAME", "")
PASSWORD = os.environ.get("SYNOLOGY_PASSWORD", "")
VOLUME = os.environ.get("SYNOLOGY_VOLUME", "volume_1")
TIMEOUT = 5


def _login():
    r = requests.get(
        f"{BASE}/webapi/auth.cgi",
        params={
            "api": "SYNO.API.Auth", "version": 6, "method": "login",
            "account": USERNAME, "passwd": PASSWORD,
            "session": "DSM", "format": "sid",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["data"]["sid"]


def get_stats():
    sid = _login()

    r = requests.get(
        f"{BASE}/webapi/entry.cgi",
        params={"api": "SYNO.Core.System", "version": 1, "method": "info", "_sid": sid},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    info = r.json()["data"]

    r = requests.get(
        f"{BASE}/webapi/entry.cgi",
        params={
            "api": "SYNO.Core.Storage.Volume", "version": 1, "method": "get",
            "volume_path": f"/{VOLUME}", "_sid": sid,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    vol = r.json()["data"]["volume"]

    return {
        "uptime": info.get("up_time"),
        "available": vol.get("size", {}).get("free_size"),
        "cpu": info.get("cpu_load"),   # VERIFY
        "mem": info.get("mem_load"),   # VERIFY
    }
