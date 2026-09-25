"""
New routes for stats-proxy (app/homelab_stats.py or wherever your
existing /github/stats and /paperless/stats routes live).

This follows the same pattern your repo already uses: the real
hostname, username/password/API key/token stays in an environment
variable that only stats-proxy's container sees. Homepage only ever
calls http://stats-proxy:4321/<service>/... with no secrets attached.

Written as a Flask Blueprint since that's the lightest fit for a
single-file "shared proxy" like this one. If stats-proxy is actually
FastAPI, the shape (env vars in, requests.get with auth, small dict
out) is identical -- just swap @bp.route for @router.get and
jsonify(...) for a plain return.

Register it in your main app with:
    from homelab_stats import bp as homelab_bp
    app.register_blueprint(homelab_bp)

Add these to stats-proxy's environment (compose.yaml / .env), then
delete the plaintext values from services.yaml entirely:

    DOCKHAND_URL=https://dockhand.ashish-syn-nas.synology.me
    DOCKHAND_USERNAME=ashish
    DOCKHAND_PASSWORD=...
    DOCKHAND_API_KEY=...            # the dh_... token used for self-update/check

    SYNOLOGY_URL=https://ashish-syn-nas.synology.me
    SYNOLOGY_USERNAME=remote_stats
    SYNOLOGY_PASSWORD=...
    SYNOLOGY_VOLUME=volume_1

    TRILIUM_URL=https://trillium.ashish-syn-nas.synology.me
    TRILIUM_TOKEN=...

    LINKWARDEN_URL=https://linkwarden.ashish-syn-nas.synology.me
    LINKWARDEN_TOKEN=...

    KARAKEEP_URL=https://karakeep.ashish-syn-nas.synology.me
    KARAKEEP_API_KEY=...

    DAWARICH_URL=https://dawarich.ashish-syn-nas.synology.me
    DAWARICH_API_KEY=...

    AIRTRAIL_URL=https://airtrail.ashish-syn-nas.synology.me
    AIRTRAIL_TOKEN=...

    TREK_URL=https://trek.ashish-syn-nas.synology.me
    TREK_API_KEY=...

    PLEX_URL=https://plex.ashish-syn-nas.synology.me
    PLEX_TOKEN=...

IMPORTANT: field names marked "VERIFY" below are best-guesses at each
app's API shape. Hit the endpoint directly (curl localhost:4321/x/stats)
after deploying and adjust the returned dict / the Homepage mappings
to match reality -- don't trust the numbers until you've checked one.
"""

import os
import requests
from flask import Blueprint, jsonify

bp = Blueprint("homelab_stats", __name__)

TIMEOUT = 5


def _get(url, **kwargs):
    r = requests.get(url, timeout=TIMEOUT, **kwargs)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- Dockhand
@bp.route("/dockhand/stats")
def dockhand_stats():
    base = os.environ["DOCKHAND_URL"]
    auth = (os.environ["DOCKHAND_USERNAME"], os.environ["DOCKHAND_PASSWORD"])
    # VERIFY: swap in Dockhand's real containers/resources endpoint(s).
    containers = _get(f"{base}/api/containers", auth=auth)
    resources = _get(f"{base}/api/resources", auth=auth)
    activity = _get(f"{base}/api/activity", auth=auth)
    return jsonify({
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
    })


@bp.route("/dockhand/version")
def dockhand_version():
    base = os.environ["DOCKHAND_URL"]
    headers = {
        "Authorization": os.environ["DOCKHAND_API_KEY"],
        "Accept": "application/json",
    }
    data = _get(f"{base}/api/self-update/check", headers=headers)
    return jsonify({
        "updateAvailable": data.get("updateAvailable"),
        "currentImage": data.get("currentImage"),
        "newImage": data.get("newImage"),
    })


# ------------------------------------------------------------- Synology
@bp.route("/synology/stats")
def synology_stats():
    base = os.environ["SYNOLOGY_URL"]
    username = os.environ["SYNOLOGY_USERNAME"]
    password = os.environ["SYNOLOGY_PASSWORD"]
    volume = os.environ.get("SYNOLOGY_VOLUME", "volume_1")

    sid = _get(
        f"{base}/webapi/auth.cgi",
        params={
            "api": "SYNO.API.Auth", "version": 6, "method": "login",
            "account": username, "passwd": password,
            "session": "DSM", "format": "sid",
        },
    )["data"]["sid"]

    info = _get(
        f"{base}/webapi/entry.cgi",
        params={
            "api": "SYNO.Core.System", "version": 1, "method": "info",
            "_sid": sid,
        },
    )["data"]
    vol = _get(
        f"{base}/webapi/entry.cgi",
        params={
            "api": "SYNO.Core.Storage.Volume", "version": 1, "method": "get",
            "volume_path": f"/{volume}", "_sid": sid,
        },
    )["data"]["volume"]

    return jsonify({
        "uptime": info.get("up_time"),
        "volumeAvailable": vol.get("size", {}).get("free_size"),
        "cpu": info.get("cpu_load"),  # VERIFY: real key differs by DSM version
        "mem": info.get("mem_load"),  # VERIFY
    })


# --------------------------------------------------------------- Trilium
@bp.route("/trilium/stats")
def trilium_stats():
    base = os.environ["TRILIUM_URL"]
    headers = {"Authorization": os.environ["TRILIUM_TOKEN"]}
    # VERIFY: Trilium's ETAPI exposes /etapi/app-info; note count usually
    # needs a search query (e.g. /etapi/notes?search=...) rather than a
    # single summary field.
    info = _get(f"{base}/etapi/app-info", headers=headers)
    return jsonify({
        "noteCount": info.get("noteCount"),   # VERIFY, may not exist as-is
        "appVersion": info.get("appVersion"),
    })


# ------------------------------------------------------------ Linkwarden
@bp.route("/linkwarden/stats")
def linkwarden_stats():
    base = os.environ["LINKWARDEN_URL"]
    headers = {"Authorization": f"Bearer {os.environ['LINKWARDEN_TOKEN']}"}
    # VERIFY: Linkwarden's dashboard endpoint / field names.
    data = _get(f"{base}/api/v1/dashboard", headers=headers)
    return jsonify({
        "numberOfLinks": data.get("numberOfLinks"),
        "numberOfCollections": data.get("numberOfCollections"),
    })


# -------------------------------------------------------------- Karakeep
@bp.route("/karakeep/stats")
def karakeep_stats():
    base = os.environ["KARAKEEP_URL"]
    headers = {"Authorization": f"Bearer {os.environ['KARAKEEP_API_KEY']}"}
    # VERIFY: Karakeep's stats endpoint / field names.
    data = _get(f"{base}/api/v1/stats", headers=headers)
    return jsonify({
        "numBookmarks": data.get("numBookmarks"),
        "numTags": data.get("numTags"),
    })


# -------------------------------------------------------------- Dawarich
@bp.route("/dawarich/stats")
def dawarich_stats():
    base = os.environ["DAWARICH_URL"]
    api_key = os.environ["DAWARICH_API_KEY"]
    data = _get(f"{base}/api/v1/stats", params={"api_key": api_key})
    return jsonify({
        "totalDistanceKm": data.get("totalDistanceKm"),
        "totalCountriesVisited": data.get("totalCountriesVisited"),
        "totalCitiesVisited": data.get("totalCitiesVisited"),
    })


# -------------------------------------------------------------- Airtrail
@bp.route("/airtrail/stats")
def airtrail_stats():
    base = os.environ["AIRTRAIL_URL"]
    headers = {
        "Authorization": f"Bearer {os.environ['AIRTRAIL_TOKEN']}",
        "Accept": "application/json",
    }
    data = _get(f"{base}/api/stats", headers=headers)
    return jsonify(data)  # already shaped as {"stats": {...}} per your mappings


# ------------------------------------------------------------------ Trek
@bp.route("/trek/stats")
def trek_stats():
    base = os.environ["TREK_URL"]
    headers = {"X-API-Key": os.environ["TREK_API_KEY"]}
    data = _get(f"{base}/api/v1/stats", headers=headers)
    return jsonify(data)


# ------------------------------------------------------------------ Plex
@bp.route("/plex/stats")
def plex_stats():
    base = os.environ["PLEX_URL"]
    token = os.environ["PLEX_TOKEN"]
    # VERIFY: Plex's real endpoints are XML by default; add
    # ?X-Plex-Token=<token>&Accept=application/json or set the header.
    headers = {"Accept": "application/json"}
    sessions = _get(f"{base}/status/sessions", params={"X-Plex-Token": token}, headers=headers)
    library = _get(f"{base}/library/sections", params={"X-Plex-Token": token}, headers=headers)
    return jsonify({
        "activeStreams": sessions.get("MediaContainer", {}).get("size"),
        "totalMediaCount": library.get("MediaContainer", {}).get("size"),  # VERIFY: likely needs per-section sum
    })
