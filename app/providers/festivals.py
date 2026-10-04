"""festivals: Hindu festival calendar for one or more locations, primarily
scraped from drikpanchang.com, with a fallback that computes a smaller set
of well-known major festivals via a self-hosted drikpanchangAPI instance
(https://github.com/vishwaramm/drikpanchangAPI) when scraping fails.

Why two data sources
---------------------
drikpanchang.com publishes ~300+ named festivals/vrats/observances a year
with no public API - the only way to get that full list is to scrape its
rendered HTML. That's inherently fragile (a markup change breaks it), so
when scraping fails this falls back to computing just the well-established
*major* festivals (Diwali, Holi, Raksha Bandhan, ...) from raw panchang
data (tithi/masa/paksha) via a self-hosted drikpanchangAPI instance - see
MAJOR_FESTIVALS below. The fallback is deliberately a much shorter list;
it exists to keep something useful on the dashboard during an outage, not
to replace the scrape.

Locations
---------
Each location needs a drikpanchang.com "geoname-id" (for the scrape) and a
latitude/longitude/timezone-offset (for the fallback API). Configured via
FESTIVALS_LOCATIONS, see LOCATIONS below for the default (Seattle and New
Delhi).

Output
------
Writes FESTIVALS_FILE as JSON:
  {
    "seattle": {"source": "drikpanchang", "festivals": [{"name", "date", "weekday", "note"}, ...]},
    "new_delhi": {"source": "drikpanchang", "festivals": [...]},
    "updated_at": "..."
  }
`source` is "drikpanchang", "fallback-api", or "unavailable" (with an
`error` field) per location, independently - one location falling back or
failing doesn't affect the other.

Env vars
  FESTIVALS_DISABLED          set to disable this provider entirely (default: enabled)
  FESTIVALS_LOCATIONS         "name:geoname_id:lat:lon:tz_offset,..." - see LOCATIONS default below
  FESTIVALS_FILE              output JSON path (default: festivals.json next to HOMEPAGE_CONFIG, or ./festivals.json)
  FESTIVALS_REFRESH_INTERVAL_SECS  seconds between refreshes (default 86400 - once a day)
  FESTIVALS_FALLBACK_WINDOW_DAYS   how many days ahead the fallback computes, since it's a
                                   per-day API call in a loop (default 120)
  DRIKPANCHANG_BASE_URL       default "https://www.drikpanchang.com"
  DRIKPANCHANG_API_URL        base URL of a self-hosted vishwaramm/drikpanchangAPI instance,
                               e.g. "http://drikpanchang-api:5050" - fallback is skipped
                               (location marked "unavailable") if this isn't set
"""
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("stats-proxy.festivals")

DRIKPANCHANG_BASE_URL = os.environ.get("DRIKPANCHANG_BASE_URL", "https://www.drikpanchang.com").rstrip("/")
DRIKPANCHANG_API_URL = os.environ.get("DRIKPANCHANG_API_URL", "").rstrip("/")
REQUEST_TIMEOUT = 20
REFRESH_INTERVAL_SECS = int(os.environ.get("FESTIVALS_REFRESH_INTERVAL_SECS", "86400"))
FALLBACK_WINDOW_DAYS = int(os.environ.get("FESTIVALS_FALLBACK_WINDOW_DAYS", "120"))

# name -> (drikpanchang geoname-id, latitude, longitude, timezone offset in hours)
# geoname-ids are from geonames.org; Seattle=5809844, New Delhi=1261481.
DEFAULT_LOCATIONS = {
    "seattle": (5809844, 47.6062, -122.3321, -8.0),
    "new_delhi": (1261481, 28.6139, 77.2090, 5.5),
}


def _parse_locations():
    raw = os.environ.get("FESTIVALS_LOCATIONS")
    if not raw:
        return dict(DEFAULT_LOCATIONS)
    out = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 5:
            log.warning("FESTIVALS_LOCATIONS entry %r ignored - expected "
                        "name:geoname_id:lat:lon:tz_offset", entry)
            continue
        name, geoname_id, lat, lon, tz = parts
        try:
            out[name] = (int(geoname_id), float(lat), float(lon), float(tz))
        except ValueError:
            log.warning("FESTIVALS_LOCATIONS entry %r has a non-numeric field - ignored", entry)
    return out or dict(DEFAULT_LOCATIONS)


LOCATIONS = _parse_locations()

FESTIVALS_FILE = os.environ.get("FESTIVALS_FILE") or os.path.join(
    os.path.dirname(os.environ.get("HOMEPAGE_CONFIG", "./services.yaml")) or ".", "festivals.json"
)

MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
               "july", "august", "september", "october", "november", "december"]

_WEEKDAYS = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
_MONTHS = "|".join(m.capitalize() for m in MONTH_NAMES)
_DATE_RE = re.compile(
    rf"(?P<month>{_MONTHS})\s+(?P<day>\d{{1,2}}),\s*(?P<year>\d{{4}}),\s*(?P<weekday>{_WEEKDAYS})"
)

# A conservative set of widely-agreed-upon major festivals, used only by the
# fallback path. Tithi is 1-15 within each paksha; "purnima"/"amavasya" stand
# in for tithi 15 of the shukla/krishna paksha respectively. This is NOT an
# attempt to replicate drikpanchang's full event list - just enough to keep
# the dashboard useful during a scraping outage. Regional variation exists
# for a few of these (e.g. Hanuman Jayanti); one common convention is used.
MAJOR_FESTIVALS = [
    {"name": "Makar Sankranti", "fixed_month_day": (1, 14)},  # solar transit, not tithi-based
    {"name": "Vasant Panchami", "masa": "Magha", "paksha": "Shukla", "tithi": 5},
    {"name": "Maha Shivaratri", "masa": "Phalguna", "paksha": "Krishna", "tithi": 14},
    {"name": "Holi", "masa": "Phalguna", "paksha": "Shukla", "tithi": "purnima"},
    {"name": "Ram Navami", "masa": "Chaitra", "paksha": "Shukla", "tithi": 9},
    {"name": "Hanuman Jayanti", "masa": "Chaitra", "paksha": "Shukla", "tithi": "purnima"},
    {"name": "Akshaya Tritiya", "masa": "Vaishakha", "paksha": "Shukla", "tithi": 3},
    {"name": "Buddha Purnima", "masa": "Vaishakha", "paksha": "Shukla", "tithi": "purnima"},
    {"name": "Guru Purnima", "masa": "Ashadha", "paksha": "Shukla", "tithi": "purnima"},
    {"name": "Raksha Bandhan", "masa": "Shravana", "paksha": "Shukla", "tithi": "purnima"},
    {"name": "Krishna Janmashtami", "masa": "Bhadrapada", "paksha": "Krishna", "tithi": 8},
    {"name": "Ganesh Chaturthi", "masa": "Bhadrapada", "paksha": "Shukla", "tithi": 4},
    {"name": "Vijayadashami (Dussehra)", "masa": "Ashwin", "paksha": "Shukla", "tithi": 10},
    {"name": "Diwali", "masa": "Kartika", "paksha": "Krishna", "tithi": "amavasya"},
    {"name": "Chhath Puja", "masa": "Kartika", "paksha": "Shukla", "tithi": 6},
]


# ---- primary: scrape drikpanchang.com ---------------------------------------

def _dedupe_repeated_name(blob):
    """drikpanchang repeats each festival's name 2-3 times back-to-back
    within its anchor's text (desktop/mobile/tooltip variants), with
    inconsistent whitespace between repeats. Recovers the underlying name
    by finding the shortest prefix the whole blob is just repeats of,
    ignoring whitespace differences between copies."""
    compact = blob.replace(" ", "")
    if not compact:
        return blob.strip()
    for n in (3, 2, 1):
        if len(compact) % n:
            continue
        unit_len = len(compact) // n
        unit = compact[:unit_len]
        if unit * n == compact:
            count = 0
            for i, ch in enumerate(blob):
                if ch != " ":
                    count += 1
                if count == unit_len:
                    return blob[: i + 1].strip()
    return blob.strip()


def _scrape_month(year, month, geoname_id):
    """Fetch and parse one month's festival list. Returns a list of
    {name, date (ISO), weekday, note}. Raises on a request/parse failure -
    callers decide how to handle that (see refresh())."""
    url = f"{DRIKPANCHANG_BASE_URL}/festivals/month/festivals-{MONTH_NAMES[month - 1]}.html"
    resp = requests.get(
        url, params={"year": year, "geoname-id": geoname_id},
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (compatible; stats-proxy/1.0; +https://github.com/hello2ashu/stats-proxy)"},
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    out = []
    seen = set()
    for a in soup.find_all("a"):
        text = " ".join(a.get_text(" ", strip=True).split())  # normalize whitespace
        m = _DATE_RE.search(text)
        if not m:
            continue  # not a festival entry - a nav link, month switcher, etc.
        name = _dedupe_repeated_name(text[: m.start()])
        if not name:
            continue
        note = text[m.end():].strip()
        try:
            iso_date = datetime.strptime(
                f"{m.group('month')} {m.group('day')} {m.group('year')}", "%B %d %Y"
            ).date().isoformat()
        except ValueError:
            continue
        key = (name, iso_date)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "date": iso_date, "weekday": m.group("weekday"), "note": note})

    return out


def _scrape_year(year, geoname_id):
    """All 12 months for one location, sorted by date. Raises if EVERY
    month fails (a handful of individual month failures are tolerated and
    logged, since a transient blip on one request shouldn't lose the whole
    year - but if the whole thing is down, let the caller fall back)."""
    out = []
    failures = 0
    for month in range(1, 13):
        try:
            out.extend(_scrape_month(year, month, geoname_id))
        except Exception as exc:
            failures += 1
            log.warning("drikpanchang scrape failed for %04d-%02d (geoname-id=%s): %s",
                        year, month, geoname_id, exc)
    if failures == 12:
        raise RuntimeError(f"all 12 months failed to scrape for geoname-id={geoname_id}")
    out.sort(key=lambda f: f["date"])
    return out


# ---- fallback: self-hosted drikpanchangAPI ----------------------------------

def _fetch_panchang(iso_date, lat, lon, tz):
    url = f"{DRIKPANCHANG_API_URL}/api/v1/panchang"
    resp = requests.get(
        url, params={"date": iso_date, "latitude": lat, "longitude": lon, "timezone": tz},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_masa_paksha_tithi(panchang):
    """drikpanchangAPI's exact response field names for masa/paksha/tithi
    aren't documented anywhere stable, so this probes a few plausible
    shapes defensively and returns (masa, paksha, tithi_number_or_name) or
    (None, None, None) if nothing recognizable was found - callers must
    treat that as 'can't match this day' rather than erroring, since a
    response-shape mismatch here is a likely source of silent fallback
    failures worth logging once rather than crashing the whole refresh."""
    if not isinstance(panchang, dict):
        return None, None, None
    data = panchang.get("data", panchang)  # some APIs wrap the payload in {"data": {...}}

    masa = None
    for key in ("masa", "month_name", "lunar_month", "maasa"):
        if isinstance(data.get(key), str):
            masa = data[key]
            break

    paksha = None
    for key in ("paksha", "paksa"):
        if isinstance(data.get(key), str):
            paksha = data[key]
            break

    tithi = None
    for key in ("tithi_number", "tithi"):
        val = data.get(key)
        if isinstance(val, int):
            tithi = val
            break
        if isinstance(val, str):
            tithi = val
            break

    return masa, paksha, tithi


def _tithi_matches(wanted, masa, paksha, tithi_val):
    if masa is None or paksha is None or tithi_val is None:
        return False
    if wanted["masa"].lower() not in str(masa).lower():
        return False
    if wanted["paksha"].lower() not in str(paksha).lower():
        return False
    want_tithi = wanted["tithi"]
    if want_tithi in ("purnima", "amavasya"):
        return want_tithi in str(tithi_val).lower() or str(tithi_val) == "15"
    try:
        return int(tithi_val) == int(want_tithi)
    except (TypeError, ValueError):
        return str(tithi_val).lower() == str(want_tithi).lower()


def _fallback_major_festivals(lat, lon, tz):
    """Walks the next FESTIVALS_FALLBACK_WINDOW_DAYS days calling the
    self-hosted drikpanchangAPI once per day, and reports any day whose
    panchang matches one of MAJOR_FESTIVALS. Raises if DRIKPANCHANG_API_URL
    isn't configured, or if every single API call in the window fails
    (so a location with no fallback configured is correctly reported as
    'unavailable', not silently empty)."""
    if not DRIKPANCHANG_API_URL:
        raise RuntimeError("DRIKPANCHANG_API_URL not set - no fallback available")

    today = date.today()
    out = []
    api_failures = 0
    shape_warned = False

    for offset in range(FALLBACK_WINDOW_DAYS):
        day = today + timedelta(days=offset)

        # Fixed-date (solar) festivals need no API call at all.
        for fest in MAJOR_FESTIVALS:
            if "fixed_month_day" in fest and (day.month, day.day) == fest["fixed_month_day"]:
                out.append({"name": fest["name"], "date": day.isoformat(),
                            "weekday": day.strftime("%A"), "note": "fixed solar date (approximate)"})

        try:
            panchang = _fetch_panchang(day.isoformat(), lat, lon, tz)
        except Exception as exc:
            api_failures += 1
            log.debug("drikpanchangAPI call failed for %s: %s", day.isoformat(), exc)
            continue

        masa, paksha, tithi_val = _extract_masa_paksha_tithi(panchang)
        if masa is None and not shape_warned:
            log.warning("drikpanchangAPI response didn't contain a recognizable masa/paksha/tithi "
                        "field - fallback festival matching will find nothing until this is fixed. "
                        "Response keys seen: %s",
                        list(panchang.keys()) if isinstance(panchang, dict) else type(panchang).__name__)
            shape_warned = True

        for fest in MAJOR_FESTIVALS:
            if "fixed_month_day" in fest:
                continue
            if _tithi_matches(fest, masa, paksha, tithi_val):
                out.append({"name": fest["name"], "date": day.isoformat(),
                            "weekday": day.strftime("%A"), "note": f"{masa}, {paksha} {tithi_val}"})

    if api_failures == FALLBACK_WINDOW_DAYS:
        raise RuntimeError(f"drikpanchangAPI unreachable for all {FALLBACK_WINDOW_DAYS} days tried")

    out.sort(key=lambda f: f["date"])
    return out


# ---- orchestration -----------------------------------------------------------

def refresh():
    year = date.today().year
    result = {}
    for name, (geoname_id, lat, lon, tz) in LOCATIONS.items():
        try:
            festivals = _scrape_year(year, geoname_id)
            result[name] = {"source": "drikpanchang", "festivals": festivals}
            log.info("festivals[%s]: %d scraped from drikpanchang", name, len(festivals))
        except Exception as exc:
            log.warning("festivals[%s]: drikpanchang scrape failed (%s), trying fallback", name, exc)
            try:
                festivals = _fallback_major_festivals(lat, lon, tz)
                result[name] = {"source": "fallback-api", "festivals": festivals}
                log.info("festivals[%s]: %d found via fallback API", name, len(festivals))
            except Exception as exc2:
                result[name] = {"source": "unavailable", "festivals": [], "error": str(exc2)}
                log.error("festivals[%s]: fallback also failed: %s", name, exc2)

    result["updated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    tmp_path = FESTIVALS_FILE + ".tmp"
    os.makedirs(os.path.dirname(tmp_path) or ".", exist_ok=True)
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, FESTIVALS_FILE)


def get_cached():
    """Contents of the festivals file; empty per-location entries until the
    first refresh has completed."""
    try:
        with open(FESTIVALS_FILE, "rb") as f:
            return json.loads(f.read())
    except FileNotFoundError:
        return {name: {"source": "pending", "festivals": []} for name in LOCATIONS}


def _refresh_loop():
    while True:
        try:
            refresh()
        except Exception:
            log.exception("festivals refresh crashed")
        time.sleep(REFRESH_INTERVAL_SECS)


def start():
    threading.Thread(target=_refresh_loop, daemon=True, name="festivals-refresh").start()