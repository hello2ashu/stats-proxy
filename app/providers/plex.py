"""Plex stats provider.

Env vars: PLEX_URL, PLEX_TOKEN.

VERIFY -- this one needs real checking. /library/sections lists your
libraries but doesn't include item counts; getting a per-library total
means a second call per section with X-Plex-Container-Size=0 (returns
just totalSize, not every item). "artist" is Plex's internal type for a
music library, but its totalSize counts artists, not albums -- if your
dashboard's "94 Albums" is really an album count, that needs a different
call (typically .../all?type=9 within the music section for albums
specifically). Confirm against your instance before trusting this.
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

    albums = movies = tv_shows = 0
    for section in sections:
        key, kind = section.get("key"), section.get("type")
        count_params = {**params, "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0}
        r = requests.get(f"{BASE}/library/sections/{key}/all", headers=headers,
                          params=count_params, timeout=TIMEOUT)
        r.raise_for_status()
        total = r.json().get("MediaContainer", {}).get("totalSize", 0)
        if kind == "artist":     # VERIFY: counts artists, not albums -- see module docstring
            albums += total
        elif kind == "movie":
            movies += total
        elif kind == "show":
            tv_shows += total

    return {
        "activeStreams": active_streams,
        "albums": albums,      # VERIFY
        "movies": movies,
        "tvShows": tv_shows,
    }
