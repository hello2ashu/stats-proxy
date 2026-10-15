"""calendar_ical provider: merge several iCal (.ics) feeds into one de-duplicated event list.

Pure standard library (no icalendar / dateutil needed). Handles what real-world feeds
(Google, iCloud, Outlook, Nextcloud, Proton, school/sports calendars) actually contain:
all-day and timed events, TZID / UTC / floating times, multi-day events, RRULE recurrence
(DAILY / WEEKLY / MONTHLY / YEARLY with INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY, BYMONTH,
BYSETPOS), EXDATE, RECURRENCE-ID overrides and STATUS:CANCELLED.

Env vars
  CALENDAR_URLS           required. One feed per line (or separated by ';'):
                              https://example.com/private/basic.ics
                              Family=https://calendar.google.com/calendar/ical/.../basic.ics   (or "Family: https://...")
                              Work=webcal://outlook.office365.com/owa/calendar/.../calendar.ics
                          "Name=" is optional (shown in the tooltip); webcal:// is treated as https://.
                          Private feed URLs contain secrets: they are never logged or returned.
  CALENDAR_TZ             time zone used to decide which "day" an event falls on
                          (default: $TZ, else America/Los_Angeles)
  CALENDAR_DEDUPE         day  (default) same title on the same day = one event
                          time same title + same day + same start time = one event
                          off  keep everything
  CALENDAR_CACHE_SECS     how often feeds are re-downloaded (default 900)
  CALENDAR_PAST_DAYS      history included (default 45)   - enough for the previous month grid
  CALENDAR_FUTURE_DAYS    future included  (default 150)
  CALENDAR_FETCH_TIMEOUT  seconds per feed (default 20)

If a feed is briefly down, its last good copy is reused; if every feed is down and nothing
was ever loaded, get_events() raises and server.py's TTLCache serves stale data / a 502.
"""
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

log = logging.getLogger("stats-proxy.calendar")

RAW_URLS = os.environ.get("CALENDAR_URLS") or os.environ.get("CALENDAR_ICAL_URLS", "")
CACHE_SECS = float(os.environ.get("CALENDAR_CACHE_SECS", "900"))
PAST_DAYS = int(os.environ.get("CALENDAR_PAST_DAYS", "45"))
FUTURE_DAYS = int(os.environ.get("CALENDAR_FUTURE_DAYS", "150"))
DEDUPE = os.environ.get("CALENDAR_DEDUPE", "day").strip().lower()
FETCH_TIMEOUT = float(os.environ.get("CALENDAR_FETCH_TIMEOUT", "20"))
MAX_BYTES = 20 * 1024 * 1024
MAX_PERIODS = 50000          # safety cap on recurrence iteration per event
PALETTE = ["#22c55e", "#3b82f6", "#f59e0b", "#ec4899", "#06b6d4", "#a855f7", "#ef4444", "#84cc16"]

WD = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

# Outlook/Exchange feeds use Windows zone names
WIN_TZ = {
    "Pacific Standard Time": "America/Los_Angeles", "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix", "Central Standard Time": "America/Chicago",
    "Eastern Standard Time": "America/New_York", "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu", "India Standard Time": "Asia/Kolkata",
    "GMT Standard Time": "Europe/London", "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris", "Central Europe Standard Time": "Europe/Budapest",
    "Tokyo Standard Time": "Asia/Tokyo", "China Standard Time": "Asia/Shanghai",
    "Singapore Standard Time": "Asia/Singapore", "AUS Eastern Standard Time": "Australia/Sydney",
    "UTC": "UTC",
}


def _zone(name):
    if not name:
        return None
    name = WIN_TZ.get(name, name)
    if name.upper() in ("UTC", "Z", "GMT"):
        return timezone.utc
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            return None
    return None


_display_name = os.environ.get("CALENDAR_TZ") or os.environ.get("TZ") or "America/Los_Angeles"
DISPLAY_TZ = _zone(_display_name)
if DISPLAY_TZ is None:
    log.warning("calendar: time zone %r not available (is the 'tzdata' package installed?) - using UTC",
                _display_name)
    DISPLAY_TZ, _display_name = timezone.utc, "UTC"
DISPLAY_TZ_NAME = _display_name


# ---- sources ---------------------------------------------------------------
_SCHEME = r"(?:https?|webcals?)://"
# entries are separated by newlines or ';', and also by a comma or whitespace when a new link follows
_SPLIT = re.compile(r"[\r\n;]+|,(?=\s*(?:[^=,]+[=:]\s*)?[A-Za-z][A-Za-z0-9+.-]*://)|(?<![=,;:])\s+(?=" + _SCHEME + ")", re.I)
_NAMED = re.compile(r"^(.+?)\s*[=:]\s*([A-Za-z][A-Za-z0-9+.-]*://.+)$")   # "Name=link" or "Name: link"
_NAME_ONLY = re.compile(r"^[^=:/]+[=:]\s*$")                               # "Name:" - the link is missing
_QUOTES = " \t\"'`<>"


def parse_sources(raw=None):
    text = RAW_URLS if raw is None else raw
    out = []
    for n, part in enumerate((p for p in _SPLIT.split(text) if p and p.strip()), 1):
        part = part.strip(_QUOTES)
        part = re.sub(r"^CALENDAR(?:_ICAL)?_URLS\s*=\s*", "", part, flags=re.I).strip(_QUOTES)
        m = _NAMED.match(part)
        name, url = (m.group(1).strip(_QUOTES), m.group(2).strip(_QUOTES)) if m and "://" not in m.group(1) else (None, part)
        url = re.sub(r"^webcals?://", "https://", url, flags=re.I)
        if not re.match(r"^https?://\S+$", url, re.I):
            if _NAME_ONLY.match(part):
                log.error("calendar: entry #%d (%r) has a name but no link - is its compose variable empty or "
                          "undefined?", n, part)
                continue
            # never log the entry itself: private feed URLs contain secrets
            log.error("calendar: ignoring entry #%d (%d chars, starts with %r) - expected an http(s):// or "
                      "webcal:// link, optionally written Name=link", n, len(part), part[:6])
            continue
        i = len(out)
        out.append({"name": name or "Calendar %d" % (i + 1), "url": url, "color": PALETTE[i % len(PALETTE)]})
    return out


SOURCES = parse_sources()


def describe():
    return "%d feed(s) [%s], tz=%s, dedupe=%s, refresh=%ds" % (
        len(SOURCES), ", ".join(s["name"] for s in SOURCES), DISPLAY_TZ_NAME, DEDUPE, int(CACHE_SECS))


# ---- iCal text parsing -----------------------------------------------------
def _unfold(text):
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def _split_unquoted(s, sep):
    parts, cur, in_q = [], [], False
    for ch in s:
        if ch == '"':
            in_q = not in_q
        if ch == sep and not in_q:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _split_prop(line):
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None
    parts = _split_unquoted(head, ";")
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _unescape(s):
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), s)


def _parse_dt(value, params, default_tz):
    """-> (date, True) for all-day values, (aware datetime, False) otherwise."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or (len(value) == 8 and value.isdigit()):
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8])), True
    is_utc = value.endswith(("Z", "z"))
    v = value.rstrip("Zz")
    dt = datetime(int(v[0:4]), int(v[4:6]), int(v[6:8]), int(v[9:11]), int(v[11:13]),
                  int(v[13:15]) if len(v) >= 15 else 0)
    tz = timezone.utc if is_utc else (_zone(params.get("TZID")) or default_tz)
    return dt.replace(tzinfo=tz), False


_DUR = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _parse_duration(s):
    m = _DUR.match(s.strip().upper())
    if not m:
        return None
    w, d, h, mi, sec = (int(x) if x else 0 for x in m.groups()[1:])
    td = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=sec)
    return -td if m.group(1) == "-" else td


def _key(v):
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return v.isoformat()


def _parse_vevents(text, default_tz):
    lines = _unfold(text)
    cal_tz = default_tz
    for line in lines:
        if line.upper().startswith("X-WR-TIMEZONE"):
            p = _split_prop(line)
            if p and _zone(p[2].strip()):
                cal_tz = _zone(p[2].strip())
            break

    events, props, in_event, alarm_depth = [], None, False, 0
    for line in lines:
        u = line.upper()
        if u == "BEGIN:VEVENT":
            in_event, props, alarm_depth = True, [], 0
            continue
        if not in_event:
            continue
        if u == "END:VEVENT":
            in_event = False
            ev = _event_from_props(props, cal_tz)
            if ev:
                events.append(ev)
            continue
        if u.startswith("BEGIN:"):
            alarm_depth += 1
            continue
        if u.startswith("END:"):
            alarm_depth -= 1
            continue
        if alarm_depth > 0:
            continue
        p = _split_prop(line)
        if p:
            props.append(p)
    return events


def _event_from_props(props, tz):
    def first(name):
        return next(((pr, v) for n, pr, v in props if n == name), None)

    ds = first("DTSTART")
    if not ds:
        return None
    try:
        start, all_day = _parse_dt(ds[1], ds[0], tz)
        end = None
        de, dur = first("DTEND") or first("DUE"), first("DURATION")
        if de:
            end, end_all = _parse_dt(de[1], de[0], tz)
            if end_all != all_day:
                end = None
        elif dur:
            td = _parse_duration(dur[1])
            if td is not None:
                end = start + td
        if end is None:
            end = start + timedelta(days=1) if all_day else start

        rid = None
        r = first("RECURRENCE-ID")
        if r:
            rv, _ = _parse_dt(r[1], r[0], tz)
            rid = _key(rv)

        exdates = set()
        for n, pr, v in props:
            if n == "EXDATE":
                for piece in v.split(","):
                    if piece.strip():
                        xv, _ = _parse_dt(piece, pr, tz)
                        exdates.add(_key(xv))

        rrule = None
        rr = first("RRULE")
        if rr:
            rrule = {}
            for tok in rr[1].split(";"):
                k, _, v = tok.partition("=")
                rrule[k.strip().upper()] = v.strip()

        def text_of(name):
            f = first(name)
            return _unescape(f[1]).strip() if f else ""

        return {
            "uid": text_of("UID"), "summary": text_of("SUMMARY") or "(no title)",
            "location": text_of("LOCATION").replace("\n", ", "),
            "status": text_of("STATUS").upper(), "start": start, "end": end, "all_day": all_day,
            "rrule": rrule, "exdates": exdates, "rid": rid,
        }
    except Exception as exc:  # one broken event must not drop the whole feed
        log.debug("calendar: skipped malformed event: %s", exc)
        return None


# ---- recurrence ------------------------------------------------------------
def _dim(y, m):
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return (date(ny, nm, 1) - date(y, m, 1)).days


_BYDAY_RE = re.compile(r"^([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)$")


def _byday_weekdays(rule):
    out = []
    for tok in (rule.get("BYDAY") or "").split(","):
        m = _BYDAY_RE.match(tok.strip().upper())
        if m:
            out.append(WD[m.group(2)])
    return out


def _month_days(y, m, rule, base_day):
    """Days of month y/m selected by BYMONTHDAY / BYDAY (with optional ordinal) / BYSETPOS."""
    n = _dim(y, m)
    bymd, byday = rule.get("BYMONTHDAY"), rule.get("BYDAY")
    days = None
    if bymd:
        days = set()
        for tok in bymd.split(","):
            v = int(tok)
            d = v if v > 0 else n + 1 + v
            if 1 <= d <= n:
                days.add(d)
    if byday:
        sel = set()
        for tok in byday.split(","):
            mt = _BYDAY_RE.match(tok.strip().upper())
            if not mt:
                continue
            wd = WD[mt.group(2)]
            match = [d for d in range(1, n + 1) if date(y, m, d).weekday() == wd]
            if not mt.group(1):
                sel.update(match)
            else:
                o = int(mt.group(1))
                if 0 < abs(o) <= len(match):
                    sel.add(match[o - 1] if o > 0 else match[o])
        days = sel if days is None else days & sel
    if days is None:
        days = {base_day} if base_day <= n else set()
    ordered = sorted(days)
    if rule.get("BYSETPOS") and ordered:
        picked = set()
        for tok in rule["BYSETPOS"].split(","):
            p = int(tok)
            if 0 < abs(p) <= len(ordered):
                picked.add(ordered[p - 1] if p > 0 else ordered[p])
        ordered = sorted(picked)
    return ordered


def _rrule_dates(base, rule, horizon, make_wall, until):
    """Yield the wall-clock dates of a recurrence, in order, up to `horizon`."""
    freq = (rule.get("FREQ") or "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        yield base                       # HOURLY/MINUTELY/... not supported: show the first one
        return
    interval = max(1, int(rule.get("INTERVAL") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT") else None
    bymonth = {int(x) for x in rule["BYMONTH"].split(",")} if rule.get("BYMONTH") else None
    wkst = WD.get((rule.get("WKST") or "MO").upper(), 0)
    wds = _byday_weekdays(rule)
    produced, k = 0, 0

    while k < MAX_PERIODS:
        if freq == "DAILY":
            d = base + timedelta(days=k * interval)
            period_first = d
            cands = [d] if (not wds or d.weekday() in wds) and (not bymonth or d.month in bymonth) else []
        elif freq == "WEEKLY":
            ws = base - timedelta(days=(base.weekday() - wkst) % 7) + timedelta(weeks=k * interval)
            period_first = ws
            cands = [ws + timedelta(days=(wd - wkst) % 7) for wd in (wds or [base.weekday()])]
            if bymonth:
                cands = [c for c in cands if c.month in bymonth]
        elif freq == "MONTHLY":
            y, m0 = divmod(base.year * 12 + base.month - 1 + k * interval, 12)
            m = m0 + 1
            period_first = date(y, m, 1)
            cands = [] if (bymonth and m not in bymonth) else [date(y, m, d) for d in _month_days(y, m, rule, base.day)]
        else:  # YEARLY
            y = base.year + k * interval
            if y > 9990:
                return
            period_first = date(y, 1, 1)
            cands = [date(y, m, d) for m in (sorted(bymonth) if bymonth else [base.month])
                     for d in _month_days(y, m, rule, base.day)]

        if period_first > horizon:
            return
        for d in sorted(cands):
            if d < base:
                continue
            if until is not None and make_wall(d) > until:
                return
            produced += 1
            if count is not None and produced > count:
                return
            if d > horizon:
                return
            yield d
        k += 1


def _until_wall(rule, all_day, tz):
    u = rule.get("UNTIL")
    if not u:
        return None
    try:
        val, was_date = _parse_dt(u, {}, tz or DISPLAY_TZ)
    except Exception:
        return None
    if all_day:
        return val if was_date else val.date()
    if was_date:
        return datetime.combine(val, time(23, 59, 59))
    return val.astimezone(tz).replace(tzinfo=None)


def _instances(ev, overrides, horizon):
    """Yield (start, end) for every occurrence of one event up to `horizon` (a date)."""
    start, end, all_day, rule = ev["start"], ev["end"], ev["all_day"], ev["rrule"]
    if not rule or ev["rid"]:
        yield start, end
        return
    dur = end - start
    if all_day:
        tz, t, base = None, None, start
        make_wall = lambda d: d  # noqa: E731
    else:
        tz, t, base = start.tzinfo, start.time(), start.date()
        make_wall = lambda d: datetime.combine(d, t)  # noqa: E731
    until = _until_wall(rule, all_day, tz)
    for d in _rrule_dates(base, rule, horizon, make_wall, until):
        occ = d if all_day else datetime.combine(d, t, tzinfo=tz)
        k = _key(occ)
        if (ev["uid"], k) in overrides or k in ev["exdates"] or d.isoformat() in ev["exdates"]:
            continue
        yield occ, occ + dur


# ---- occurrences -> one entry per day --------------------------------------
def _day_entries(ev, start, end, d_from, d_to, name, color):
    base = {"title": ev["summary"], "location": ev["location"], "calendars": [name], "color": color}
    out = []
    if ev["all_day"]:
        s, e = start, end
        if e <= s:
            e = s + timedelta(days=1)
        n = (e - s).days
        for i in range(max(0, (d_from - s).days), min(n - 1, (d_to - s).days) + 1):
            out.append(dict(base, date=(s + timedelta(days=i)).isoformat(), start=None, end=None,
                            all_day=True, day=i + 1, days=n))
        return out

    s, e = start.astimezone(DISPLAY_TZ), end.astimezone(DISPLAY_TZ)
    last = e.date()
    ends_midnight = e > s and e.time() == time(0, 0)
    if ends_midnight:
        last = (e - timedelta(days=1)).date()
    n = (last - s.date()).days + 1
    for i in range(max(0, (d_from - s.date()).days), min(n - 1, (d_to - s.date()).days) + 1):
        out.append(dict(
            base, date=(s.date() + timedelta(days=i)).isoformat(),
            start=s.strftime("%H:%M") if i == 0 else None,
            end=e.strftime("%H:%M") if (i == n - 1 and e > s and not ends_midnight) else None,
            all_day=False, day=i + 1, days=n))
    return out


def _entries_for_feed(text, name, color, d_from, d_to):
    events = _parse_vevents(text, DISPLAY_TZ)
    overrides = {(e["uid"], e["rid"]) for e in events if e["rid"]}
    horizon = d_to + timedelta(days=1)
    out = []
    for ev in events:
        if ev["status"] == "CANCELLED":
            continue
        for start, end in _instances(ev, overrides, horizon):
            out.extend(_day_entries(ev, start, end, d_from, d_to, name, color))
    return out


# ---- merge + de-duplicate --------------------------------------------------
def _norm(title):
    return re.sub(r"[\W_]+", " ", title.lower()).strip()


def _dedupe(entries):
    if DEDUPE == "off":
        return entries, 0
    merged, removed = {}, 0
    for e in entries:
        key = (_norm(e["title"]), e["date"]) + ((e["start"] or "",) if DEDUPE == "time" else ())
        cur = merged.get(key)
        if cur is None:
            merged[key] = e
            continue
        removed += 1
        for c in e["calendars"]:
            if c not in cur["calendars"]:
                cur["calendars"].append(c)
        if not cur["location"] and e["location"]:
            cur["location"] = e["location"]
        if cur["all_day"] and not e["all_day"]:          # keep the version that has a time
            for f in ("start", "end", "all_day", "day", "days"):
                cur[f] = e[f]
    return list(merged.values()), removed


# ---- fetching --------------------------------------------------------------
_last_good = {}
_lock = threading.Lock()


def _fetch(url):
    req = Request(url, headers={"User-Agent": "stats-proxy/1.0 (ical)", "Accept": "text/calendar, text/plain, */*"})
    with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("calendar feed larger than %d MB" % (MAX_BYTES // 1024 // 1024))
    text = data.decode("utf-8-sig", errors="replace")
    if "BEGIN:VCALENDAR" not in text.upper():
        raise ValueError("response is not an iCal feed (login page or wrong URL?)")
    return text


def _load(src):
    """-> (text or None, error or None, stale?). Never raises, never logs the URL."""
    try:
        text = _fetch(src["url"])
        with _lock:
            _last_good[src["url"]] = text
        return text, None, False
    except Exception as exc:
        reason = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        with _lock:
            old = _last_good.get(src["url"])
        log.warning("calendar %s fetch failed (%s)%s", src["name"], reason,
                    " - using last good copy" if old else "")
        return old, reason, old is not None


def get_events():
    if not SOURCES:
        raise RuntimeError("CALENDAR_URLS is set (%d chars) but none of its entries is a usable http(s)/webcal "
                           "link - see the 'ignoring entry' lines in the log" % len(RAW_URLS))
    today = datetime.now(DISPLAY_TZ).date()
    d_from, d_to = today - timedelta(days=PAST_DAYS), today + timedelta(days=FUTURE_DAYS)

    with ThreadPoolExecutor(max_workers=min(8, len(SOURCES))) as pool:
        loaded = list(pool.map(_load, SOURCES))

    entries, meta = [], []
    for src, (text, err, stale) in zip(SOURCES, loaded):
        n = 0
        if text:
            try:
                got = _entries_for_feed(text, src["name"], src["color"], d_from, d_to)
                entries.extend(got)
                n = len(got)
            except Exception as exc:
                err = err or "parse error: %s" % str(exc)[:160]
                log.exception("calendar %s could not be parsed", src["name"])
        meta.append({"name": src["name"], "color": src["color"], "events": n, "error": err, "stale": stale})

    if all(m["error"] and not m["events"] for m in meta):
        raise RuntimeError("all calendar feeds failed: " + "; ".join("%s: %s" % (m["name"], m["error"]) for m in meta))

    before = len(entries)
    entries, removed = _dedupe(entries)
    entries.sort(key=lambda e: (e["date"], 0 if (e["all_day"] or not e["start"]) else 1,
                                e["start"] or "", e["title"].lower()))
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timezone": DISPLAY_TZ_NAME,
        "range": {"from": d_from.isoformat(), "to": d_to.isoformat()},
        "dedupe": DEDUPE,
        "duplicates_removed": removed,
        "total_before_dedupe": before,
        "calendars": meta,
        "events": entries,
    }