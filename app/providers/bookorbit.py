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
_last_total = None  # last good totalBooks, reused if the new endpoint hiccups


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


def _get(path):
    return _session.get(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {_token}"},
        timeout=TIMEOUT,
    )


def get_stats():
    """User-statistics summary (trackedBooks, startedBooks, inProgressBooks, completedBooks, ...)
    plus totalBooks from the library-wide /statistics/summary endpoint."""
    global _last_total
    with _lock:
        if _token is None or time.monotonic() - _token_at > TOKEN_MAX_AGE:
            _login()
        r = _get("/api/v1/user-statistics/summary")
        if r.status_code == 401:  # expired early - retry once with a fresh token
            _login()
            r = _get("/api/v1/user-statistics/summary")
        if not r.ok:
            raise RuntimeError(f"Stats fetch failed: {r.status_code} {r.text[:200]}")
        stats = r.json()

        # Only totalBooks comes from the new endpoint. A failure here must not break
        # the started/in-progress/completed numbers, so keep the last known total.
        try:
            t = _get("/api/v1/statistics/summary")
            if not t.ok:
                raise RuntimeError(f"{t.status_code} {t.text[:200]}")
            _last_total = t.json()["totalBooks"]
        except Exception as exc:
            log.warning("totalBooks fetch failed (%s) - using last known value: %s", exc, _last_total)

    stats["totalBooks"] = _last_total
    return stats