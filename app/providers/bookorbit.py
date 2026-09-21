"""BookOrbit provider (ported from the standalone Node bookorbit-proxy).

Env
  BOOKORBIT_URL        e.g. https://bookorbit.ashish-syn-nas.synology.me
  BOOKORBIT_USERNAME
  BOOKORBIT_PASSWORD
  HTTP_TIMEOUT_SECS    default 10
"""
import logging
import os
import threading
import time

import requests

log = logging.getLogger("bookorbit")

BASE = os.environ["BOOKORBIT_URL"].rstrip("/")
USERNAME = os.environ.get("BOOKORBIT_USERNAME", "")
PASSWORD = os.environ.get("BOOKORBIT_PASSWORD", "")
TIMEOUT = float(os.environ.get("HTTP_TIMEOUT_SECS", "10"))
TOKEN_MAX_AGE = 10 * 60  # access token lives 15 min; refresh well before that

if not (USERNAME and PASSWORD):
    log.error("Set BOOKORBIT_USERNAME and BOOKORBIT_PASSWORD")

_session = requests.Session()
_lock = threading.Lock()
_token = None
_token_at = 0.0


def _login():
    global _token, _token_at
    r = _session.post(
        f"{BASE}/api/v1/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise RuntimeError(f"Login failed: {r.status_code} {r.text[:200]}")
    _token = r.json()["accessToken"]
    _token_at = time.monotonic()
    log.info("BookOrbit login refreshed.")


def _fetch():
    return _session.get(
        f"{BASE}/api/v1/user-statistics/summary",
        headers={"Authorization": f"Bearer {_token}"},
        timeout=TIMEOUT,
    )


def get_stats():
    """Returns BookOrbit's user-statistics summary unchanged (trackedBooks, startedBooks, ...)."""
    with _lock:
        if _token is None or time.monotonic() - _token_at > TOKEN_MAX_AGE:
            _login()
        r = _fetch()
        if r.status_code == 401:  # expired early - retry once with a fresh token
            _login()
            r = _fetch()
    if not r.ok:
        raise RuntimeError(f"Stats fetch failed: {r.status_code} {r.text[:200]}")
    return r.json()
