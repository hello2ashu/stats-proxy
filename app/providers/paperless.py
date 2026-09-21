"""Paperless-ngx provider.

Env
  PAPERLESS_URL       base URL reachable from this container, e.g. http://192.168.189.40:8777
  PAPERLESS_TOKEN     API token (Paperless: My Profile -> API Auth Token)   [preferred]
  PAPERLESS_USERNAME / PAPERLESS_PASSWORD   fallback; used to fetch a token via /api/token/
  HTTP_TIMEOUT_SECS   default 10
"""
import logging
import os

import requests

log = logging.getLogger("paperless")

BASE = os.environ["PAPERLESS_URL"].rstrip("/")
STATIC_TOKEN = os.environ.get("PAPERLESS_TOKEN", "").strip()
USERNAME = os.environ.get("PAPERLESS_USERNAME", "")
PASSWORD = os.environ.get("PAPERLESS_PASSWORD", "")
TIMEOUT = float(os.environ.get("HTTP_TIMEOUT_SECS", "10"))

if not STATIC_TOKEN and not (USERNAME and PASSWORD):
    log.error("Set PAPERLESS_TOKEN (or PAPERLESS_USERNAME + PAPERLESS_PASSWORD)")

_session = requests.Session()
_session.headers["Accept"] = "application/json"
_login_token = None


def _token(force=False):
    global _login_token
    if STATIC_TOKEN:
        return STATIC_TOKEN
    if _login_token is None or force:
        r = _session.post(
            f"{BASE}/api/token/",
            json={"username": USERNAME, "password": PASSWORD},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        _login_token = r.json()["token"]
    return _login_token


def _get(path, params=None):
    r = None
    for attempt in (1, 2):
        r = _session.get(
            f"{BASE}{path}",
            params=params,
            headers={"Authorization": f"Token {_token(force=attempt == 2)}"},
            timeout=TIMEOUT,
        )
        # A login-derived token can be revoked; re-login once. A static token can't be refreshed.
        if r.status_code == 401 and attempt == 1 and not STATIC_TOKEN:
            continue
        break
    r.raise_for_status()
    return r.json()


def get_stats():
    d = _get("/api/statistics/")
    return {
        "total": d.get("documents_total") or 0,
        "inbox": d.get("documents_inbox") or 0,
        "tags": d.get("tag_count") or 0,
        "correspondents": d.get("correspondent_count") or 0,
        "document_types": d.get("document_type_count") or 0,
        "storage_paths": d.get("storage_path_count") or 0,
        "characters": d.get("character_count") or 0,
    }


def get_tags():
    """All tags visible to the API user, with live document counts, busiest first."""
    tags, page = [], 1
    while True:
        d = _get(
            "/api/tags/",
            {"page": page, "page_size": 250, "fields": "id,name,color,document_count"},
        )
        for t in d.get("results", []):
            tags.append(
                {
                    "id": t["id"],
                    "name": t["name"],
                    "count": t.get("document_count") or 0,
                    "color": t.get("color"),
                }
            )
        if not d.get("next"):
            break
        page += 1
    tags.sort(key=lambda t: (-t["count"], t["name"].lower()))
    return tags
