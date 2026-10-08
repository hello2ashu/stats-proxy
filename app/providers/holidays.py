"""holidays: editable US + Indian national holiday lists, a daily Krishna/
Shukla paksha + tithi reading (scraped from drikpanchang.com), and an
"upcoming festivals in the next N days" view built on top of festivals.py's
cached data. All three are bundled into one GET /festivals/dashboard
response, meant for a single Homepage customapi widget.

Editable holiday lists
-----------------------
US_HOLIDAYS_FILE and INDIAN_HOLIDAYS_FILE are plain YAML files you can hand-
edit directly - add, remove, or rename an entry and it takes effect on the
next request (the lists are re-read every time; no redeploy or restart needed). If a file doesn't exist yet, a sensible
default is written out on first run so there's something to edit. Each
entry is either a fixed month/day:

    - name: Independence Day
      month: 7
      day: 4

or a "nth weekday of month" rule for floating holidays (nth: -1 means the
LAST occurrence, e.g. Memorial Day is the last Monday of May):

    - name: Thanksgiving Day
      month: 11
      weekday: Thursday
      nth: 4

Daily panchang (paksha/tithi)
-------------------------------
Scraped from drikpanchang.com/panchang/day-panchang.html the same way
festivals.py scrapes the festival list - no self-hosted API needed, works
for any configured location. This is inherently fragile (an unofficial
scrape of someone else's HTML); if the site's markup changes enough that
none of Masa/Paksha/Tithi can be found in the page text, that day is
reported as unavailable rather than guessed at.

Env vars (on top of festivals.py's)
  US_HOLIDAYS_FILE       path to the editable US holidays YAML (default: us_holidays.yaml
                         next to FESTIVALS_FILE)
  INDIAN_HOLIDAYS_FILE   path to the editable Indian national holidays YAML (default:
                         indian_national_holidays.yaml next to FESTIVALS_FILE)
  UPCOMING_WINDOW_DAYS   how many days ahead "upcoming festivals" looks (default 15)
"""
import calendar
import logging
import os
import re
import threading
import time
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup
from ruamel.yaml import YAML

from . import festivals

_yaml = YAML()
_yaml.default_flow_style = False

log = logging.getLogger("stats-proxy.holidays")

UPCOMING_WINDOW_DAYS = int(os.environ.get("UPCOMING_WINDOW_DAYS", "15"))

_festivals_dir = os.path.dirname(festivals.FESTIVALS_FILE) or "."
US_HOLIDAYS_FILE = os.environ.get("US_HOLIDAYS_FILE") or os.path.join(_festivals_dir, "us_holidays.yaml")
INDIAN_HOLIDAYS_FILE = os.environ.get("INDIAN_HOLIDAYS_FILE") or os.path.join(
    _festivals_dir, "indian_national_holidays.yaml"
)

DEFAULT_US_HOLIDAYS = [
    {"name": "New Year's Day", "month": 1, "day": 1},
    {"name": "Martin Luther King Jr. Day", "month": 1, "weekday": "Monday", "nth": 3},
    {"name": "Presidents' Day", "month": 2, "weekday": "Monday", "nth": 3},
    {"name": "Memorial Day", "month": 5, "weekday": "Monday", "nth": -1},
    {"name": "Juneteenth", "month": 6, "day": 19},
    {"name": "Independence Day", "month": 7, "day": 4},
    {"name": "Labor Day", "month": 9, "weekday": "Monday", "nth": 1},
    {"name": "Columbus Day", "month": 10, "weekday": "Monday", "nth": 2},
    {"name": "Veterans Day", "month": 11, "day": 11},
    {"name": "Thanksgiving Day", "month": 11, "weekday": "Thursday", "nth": 4},
    {"name": "Christmas Day", "month": 12, "day": 25},
]

# Fixed-date, gazetted national holidays only - religious/regional festivals
# are already covered by the full scraped list in festivals.py, so they're
# deliberately not duplicated here.
DEFAULT_INDIAN_HOLIDAYS = [
    {"name": "Republic Day", "month": 1, "day": 26},
    {"name": "Independence Day", "month": 8, "day": 15},
    {"name": "Gandhi Jayanti", "month": 10, "day": 2},
]

_WEEKDAY_NUM = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
                "Friday": 4, "Saturday": 5, "Sunday": 6}


def _load_or_seed(path, default):
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _yaml.dump(default, f)
        log.info("seeded editable holiday list at %s (%d entries)", path, len(default))
        return list(default)
    try:
        with open(path, encoding="utf-8") as f:
            data = _yaml.load(f)
        if not isinstance(data, list):
            raise ValueError("expected a YAML list of holiday entries")
        return data
    except Exception as exc:
        log.error("couldn't read %s (%s) - using built-in defaults for this refresh "
                  "without touching your file", path, exc)
        return list(default)


def _nth_weekday_of_month(year, month, weekday_name, nth):
    """nth=1..5 for the 1st..5th occurrence in the month, nth=-1 for the LAST."""
    wd = _WEEKDAY_NUM[weekday_name]
    days_in_month = calendar.monthrange(year, month)[1]
    matches = [d for d in range(1, days_in_month + 1) if date(year, month, d).weekday() == wd]
    index = -1 if nth == -1 else nth - 1
    return date(year, month, matches[index])


def _occurrences(entries, year):
    """Resolve each holiday-rule entry to an actual date for `year`. Entries
    with a bad/missing rule are skipped with a warning rather than crashing
    the whole list - a typo in a hand-edited file shouldn't take every
    other holiday down with it."""
    out = []
    for entry in entries:
        name = entry.get("name")
        if not name:
            log.warning("holiday entry missing 'name', skipped: %r", entry)
            continue
        try:
            if "day" in entry:
                d = date(year, int(entry["month"]), int(entry["day"]))
            else:
                d = _nth_weekday_of_month(year, int(entry["month"]), entry["weekday"], int(entry["nth"]))
        except Exception as exc:
            log.warning("couldn't resolve a date for holiday %r (%s), skipped", name, exc)
            continue
        out.append({"name": name, "date": d.isoformat(), "weekday": d.strftime("%A")})
    out.sort(key=lambda h: h["date"])
    return out


def get_us_holidays(year):
    return _occurrences(_load_or_seed(US_HOLIDAYS_FILE, DEFAULT_US_HOLIDAYS), year)


def get_indian_holidays(year):
    return _occurrences(_load_or_seed(INDIAN_HOLIDAYS_FILE, DEFAULT_INDIAN_HOLIDAYS), year)


def _next_occurrence(occurrences_this_year, occurrences_next_year, today_iso):
    for h in occurrences_this_year + occurrences_next_year:
        if h["date"] >= today_iso:
            return h
    return None


# ---- upcoming festivals (from festivals.py's cache) -------------------------

def get_upcoming_festivals(window_days=UPCOMING_WINDOW_DAYS):
    """Festivals from festivals.py's cached data falling within the next
    `window_days` days, per location. Reads the existing cache rather than
    re-scraping - festivals.py's own background refresh keeps that current."""
    cached = festivals.get_cached()
    today = date.today()
    cutoff = (today + timedelta(days=window_days)).isoformat()
    today_iso = today.isoformat()

    out = {}
    for name, info in cached.items():
        if name == "updated_at" or not isinstance(info, dict):
            continue
        all_festivals = info.get("festivals", [])
        upcoming = [f for f in all_festivals if today_iso <= f.get("date", "") <= cutoff]
        out[name] = {"source": info.get("source", "unknown"), "festivals": upcoming}
    return out


# ---- daily panchang (paksha/tithi) scrape -----------------------------------

_MASA_NAMES = ["Chaitra", "Vaishakha", "Jyeshtha", "Ashadha", "Shravana", "Bhadrapada",
               "Ashwin", "Ashwina", "Kartika", "Kartik", "Margashirsha", "Agrahayana",
               "Pausha", "Magha", "Phalguna"]
_TITHI_NAMES = ["Pratipada", "Dwitiya", "Tritiya", "Chaturthi", "Panchami", "Shashthi",
                "Saptami", "Ashtami", "Navami", "Dashami", "Ekadashi", "Dwadashi",
                "Trayodashi", "Chaturdashi", "Purnima", "Amavasya"]

_PAKSHA_RE = re.compile(
    r"(?P<p1>Krishna|Shukla)\s*Paksha|Paksha\s*[:\-]?\s*(?P<p2>Krishna|Shukla)|"
    r"(?P<p3>Krishna|Shukla)\s+(?:" + "|".join(_TITHI_NAMES) + r")\b",
    re.IGNORECASE,
)
_MASA_RE = re.compile(r"\b(" + "|".join(_MASA_NAMES) + r")\b", re.IGNORECASE)
_TITHI_RE = re.compile(r"\b(" + "|".join(_TITHI_NAMES) + r")\b", re.IGNORECASE)


def _split_camel(text):
    """drikpanchang-derived text sometimes concatenates words with no space
    between them (flattened nested tags) - insert a space at any
    lower->upper letter transition so the word-boundary regexes above still
    match (e.g. 'AshtamiShukla' -> 'Ashtami Shukla')."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)


def _scrape_day_panchang(iso_date, geoname_id):
    """Best-effort scrape of one day's Masa/Paksha/Tithi for one location.
    Returns {"masa", "paksha", "tithi", "is_purnima", "is_amavasya"}, or
    raises if none of the three fields could be found in the page text -
    callers must treat that as 'unavailable for this day', never guessed."""
    d = date.fromisoformat(iso_date)
    url = f"{festivals.DRIKPANCHANG_BASE_URL}/panchang/day-panchang.html"
    resp = requests.get(
        url, params={"date": d.strftime("%d/%m/%Y"), "geoname-id": geoname_id},
        timeout=festivals.REQUEST_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (compatible; stats-proxy/1.0; "
                                "+https://github.com/hello2ashu/stats-proxy)"},
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    text = _split_camel(" ".join(soup.get_text(" ", strip=True).split()))

    paksha_m = _PAKSHA_RE.search(text)
    paksha = next((g for g in (paksha_m.groups() if paksha_m else ()) if g), None)
    masa_m = _MASA_RE.search(text)
    tithi_m = _TITHI_RE.search(text)
    masa = masa_m.group(1) if masa_m else None
    tithi = tithi_m.group(1) if tithi_m else None

    if masa is None and paksha is None and tithi is None:
        raise RuntimeError(f"couldn't find Masa/Paksha/Tithi in drikpanchang's "
                           f"response for {iso_date} (geoname-id={geoname_id})")

    paksha_norm = paksha.capitalize() if paksha else None
    return {
        "masa": masa,
        "paksha": paksha_norm,
        "tithi": tithi,
        "is_purnima": bool(tithi and tithi.lower() == "purnima"),
        "is_amavasya": bool(tithi and tithi.lower() == "amavasya"),
    }


_panchang_cache = {}  # (geoname_id, iso_date) -> reading; successes only, so failures retry


def get_panchang_window(location_name, days=UPCOMING_WINDOW_DAYS):
    """Daily panchang for today through `days` days ahead, for one of the
    locations configured in festivals.LOCATIONS. Each day is fetched
    independently - a failure on one day doesn't lose the rest of the
    window, it's just reported with an 'error' field for that date."""
    if location_name not in festivals.LOCATIONS:
        raise ValueError(f"unknown location {location_name!r} - configured: "
                         f"{sorted(festivals.LOCATIONS)}")
    geoname_id = festivals.LOCATIONS[location_name][0]
    today = date.today()
    for stale in [k for k in _panchang_cache if k[1] < today.isoformat()]:
        del _panchang_cache[stale]  # drop days that have already passed
    out = []
    for offset in range(days):
        d = today + timedelta(days=offset)
        key = (geoname_id, d.isoformat())
        if key in _panchang_cache:
            out.append(_panchang_cache[key])
            continue
        try:
            reading = _scrape_day_panchang(d.isoformat(), geoname_id)
            reading["date"] = d.isoformat()
            reading["weekday"] = d.strftime("%A")
            _panchang_cache[key] = reading
            out.append(reading)
        except Exception as exc:
            log.debug("panchang scrape failed for %s on %s: %s", location_name, d.isoformat(), exc)
            out.append({"date": d.isoformat(), "weekday": d.strftime("%A"), "error": str(exc)})
    return out


# ---- bundled dashboard response ---------------------------------------------

def get_dashboard(panchang_location=None):
    """Everything in one response, shaped for a single Homepage customapi
    widget: upcoming festivals (per location, next UPCOMING_WINDOW_DAYS
    days), the full US and Indian national holiday lists with each one's
    next occurrence highlighted, and a daily panchang window for one
    location (defaults to the first configured location, typically
    'new_delhi' - paksha/tithi are most meaningful for the Indian
    location; pass panchang_location explicitly for another one)."""
    today_iso = date.today().isoformat()
    this_year = date.today().year

    us_this_year = get_us_holidays(this_year)
    us_next_year = get_us_holidays(this_year + 1)
    in_this_year = get_indian_holidays(this_year)
    in_next_year = get_indian_holidays(this_year + 1)

    location = panchang_location or next(iter(festivals.LOCATIONS), None)
    panchang = get_panchang_window(location) if location else []

    return {
        "upcoming_festivals": get_upcoming_festivals(),
        "us_holidays": {
            "all": us_this_year,
            "next": _next_occurrence(us_this_year, us_next_year, today_iso),
        },
        "indian_holidays": {
            "all": in_this_year,
            "next": _next_occurrence(in_this_year, in_next_year, today_iso),
        },
        "panchang": {"location": location, "days": panchang},
    }


# ---- background warm-up ------------------------------------------------------

def _warm_loop():
    while True:
        for name in festivals.LOCATIONS:
            try:
                get_panchang_window(name)
            except Exception:
                log.exception("panchang warm-up failed for %s", name)
        time.sleep(6 * 3600)  # failed days are retried each pass; successes are cached


def start():
    threading.Thread(target=_warm_loop, daemon=True, name="panchang-warmup").start()
