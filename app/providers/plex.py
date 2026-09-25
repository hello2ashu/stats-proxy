"""Plex stats provider.

Env vars: PLEX_URL, PLEX_TOKEN.

Movies (24) and TV Shows (9) are confirmed correct against your dashboard.
Albums needed a fix: a music ("artist"-type) library's own totalSize counts
*artists*, not albums -- Plex's metadata type numbers distinguish them
(8 = artist, 9 = album), so albums need a second call per music library
with type=9.
"""
import os
import requests

BASE = os.environ.get("PLEX_URL", "").rstrip("/")
TOKEN = os.environ.get("PLEX_TOKEN", "")
TIMEOUT = 5


def get_stats():
    headers = {"Accept": "application/json"}
    params = {"X-Plex-Token": TOKEN}

    r = requests.get(f"{BASE}/status/sessions", headers=headers, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    active_streams = r.json().get("MediaContainer", {}).get("size", 0)

    r = requests.get(f"{BASE}/library/sections", headers=headers, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    sections = r.json().get("MediaContainer", {}).get("Directory", [])

    def total_size(url, extra_params=None):
        p = {**params, "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0}
        if extra_params:
            p.update(extra_params)
        resp = requests.get(url, headers=headers, params=p, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("MediaContainer", {}).get("totalSize", 0)

    albums = movies = tv_shows = 0
    for section in sections:
        key, kind = section.get("key"), section.get("type")
        section_url = f"{BASE}/library/sections/{key}/all"
        if kind == "artist":       # music library: count albums (type=9), not artists
            albums += total_size(section_url, {"type": 9})
        elif kind == "movie":
            movies += total_size(section_url)
        elif kind == "show":
            tv_shows += total_size(section_url)

    return {
        "activeStreams": active_streams,
        "albums": albums,
        "movies": movies,
        "tvShows": tv_shows,
    }
