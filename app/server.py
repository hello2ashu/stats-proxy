"""stats-proxy: one small HTTP service that exposes stats from several self-hosted
apps in a shape Homepage's `customapi` widget can consume.

Routes
  GET /health              liveness (never calls upstream apps)
  GET /stats               BookOrbit stats (kept for backwards compatibility)
  GET /bookorbit/stats     BookOrbit stats
  GET /paperless/stats     Paperless-ngx totals (documents, inbox, tags, ...)
  GET /paperless/tags      Paperless-ngx tags with live document counts, sorted desc
                           optional query: ?limit=10  ?min=1
  GET /github/stats        Homepage repo-sync counts {deployed, undeployed, total, updated_at}
  POST /webhook            GitHub App webhook -> immediate repo sync (alias: /github/webhook)

Each provider is enabled only if its env var is set (BOOKORBIT_URL, PAPERLESS_URL, GITHUB_USER). Upstream calls are cached
(CACHE_TTL_SECS, default 60) and stale data is served if an upstream is briefly down.

Logging (LOG_LEVEL, default INFO) - one line per upstream refresh:
  bookorbit OK in 0.31s (tracked=10 ...)              successful refresh
  paperless.stats FAILED (#2, 10.01s): ...            failed refresh, says what is served instead
  paperless.stats RECOVERED after 3 failed attempts   first success after failures
Set LOG_LEVEL=WARNING to keep only failures.
"""
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("stats-proxy")

CACHE_TTL = float(os.environ.get("CACHE_TTL_SECS", "60"))
RETRY_AFTER_FAIL = 15.0  # seconds to serve stale data before retrying a failing upstream


def _short(exc, limit=300):
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= limit else text[: limit - 3] + "..."


class TTLCache:
    """Caches one callable's result. Thread-safe; serves stale data on upstream errors.

    Every real refresh is logged: OK (duration + short summary), FAILED (error + what is
    being served instead) and RECOVERED (first success after one or more failures).
    """

    def __init__(self, name, fn, summarize=None, ttl=CACHE_TTL):
        self.name, self.fn, self.summarize, self.ttl = name, fn, summarize, ttl
        self.value, self.fetched_at, self.next_try = None, 0.0, 0.0
        self.failures = 0
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            now = time.monotonic()
            if self.value is not None and now - self.fetched_at < self.ttl:
                return self.value
            if self.value is not None and now < self.next_try:
                return self.value  # recently failed; keep serving stale

            started = time.monotonic()
            try:
                value = self.fn()
            except NotImplementedError:
                raise
            except Exception as exc:
                self.failures += 1
                elapsed = time.monotonic() - started
                if self.value is None:
                    log.error("%s FAILED (#%d, %.2fs): %s - no cached data to serve",
                              self.name, self.failures, elapsed, _short(exc))
                    raise
                age = int(time.monotonic() - self.fetched_at)
                log.warning("%s FAILED (#%d, %.2fs): %s - serving stale data from %ds ago, retry in %ds",
                            self.name, self.failures, elapsed, _short(exc), age, int(RETRY_AFTER_FAIL))
                self.next_try = time.monotonic() + RETRY_AFTER_FAIL
                return self.value

            elapsed = time.monotonic() - started
            if self.failures:
                log.info("%s RECOVERED after %d failed attempt(s)", self.name, self.failures)
                self.failures = 0
            self.value, self.fetched_at = value, time.monotonic()
            detail = ""
            if self.summarize:
                try:
                    detail = f" ({self.summarize(value)})"
                except Exception:
                    pass  # a summary problem must never affect serving data
            log.info("%s OK in %.2fs%s", self.name, elapsed, detail)
            return value


# ---- provider wiring -------------------------------------------------------
providers = {}  # name -> enabled
caches = []

if os.environ.get("BOOKORBIT_URL"):
    from providers import bookorbit
    providers["bookorbit"] = True
    _bookorbit = TTLCache(
        "bookorbit", bookorbit.get_stats,
        lambda d: "tracked={trackedBooks} started={startedBooks} in_progress={inProgressBooks} "
                  "completed={completedBooks}".format_map(
                      {k: d.get(k) for k in ("trackedBooks", "startedBooks", "inProgressBooks", "completedBooks")}),
    )
    caches.append(_bookorbit)
    log.info("bookorbit provider enabled -> %s", bookorbit.BASE)

if os.environ.get("PAPERLESS_URL"):
    from providers import paperless
    providers["paperless"] = True
    _pl_stats = TTLCache(
        "paperless.stats", paperless.get_stats,
        lambda d: f"documents={d['total']} inbox={d['inbox']} tags={d['tags']} correspondents={d['correspondents']}",
    )
    _pl_tags = TTLCache(
        "paperless.tags", paperless.get_tags,
        lambda d: f"{len(d)} tags" + (f", top: {d[0]['name']}={d[0]['count']}" if d else ""),
    )
    caches += [_pl_stats, _pl_tags]
    auth = "token" if paperless.STATIC_TOKEN else ("username/password" if paperless.USERNAME else "MISSING")
    log.info("paperless provider enabled -> %s (auth: %s)", paperless.BASE, auth)

if os.environ.get("GITHUB_USER"):
    from providers import github_sync
    _missing = github_sync.missing_env()
    if _missing:
        log.error("github sync disabled - missing env: %s", ", ".join(_missing))
    else:
        providers["github"] = True
        log.info("github sync provider enabled -> %s", github_sync.describe())
        github_sync.check_config_dir()
if os.environ.get("DOCKHAND_URL") and os.environ.get("DOCKHAND_USERNAME"):
    from providers import dockhand
    providers["dockhand"] = True
    _dockhand_stats = TTLCache(
        "dockhand.stats", dockhand.get_stats,
        lambda d: "running={running} stopped={stopped} total={total}".format_map(d),
    )
    _dockhand_version = TTLCache(
        "dockhand.version", dockhand.get_version,
        lambda d: f"updateAvailable={d.get('updateAvailable')}",
        ttl=3600,
    )
    caches += [_dockhand_stats, _dockhand_version]
    log.info("dockhand provider enabled -> %s", dockhand.BASE)

if os.environ.get("SYNOLOGY_URL"):
    from providers import synology
    providers["synology"] = True
    _synology = TTLCache("synology.stats", synology.get_stats,
                          lambda d: f"uptime={d.get('uptime')} cpu={d.get('cpu')}")
    caches.append(_synology)
    log.info("synology provider enabled -> %s", synology.BASE)

if os.environ.get("TRILIUM_URL"):
    from providers import trilium
    providers["trilium"] = True
    _trilium = TTLCache("trilium.stats", trilium.get_stats,
                         lambda d: f"notes={d.get('notes')} version={d.get('version')}")
    caches.append(_trilium)
    log.info("trilium provider enabled -> %s", trilium.BASE)

if os.environ.get("LINKWARDEN_URL"):
    from providers import linkwarden
    providers["linkwarden"] = True
    _linkwarden = TTLCache("linkwarden.stats", linkwarden.get_stats,
                            lambda d: f"links={d.get('links')} collections={d.get('collections')}")
    caches.append(_linkwarden)
    log.info("linkwarden provider enabled -> %s", linkwarden.BASE)

if os.environ.get("KARAKEEP_URL"):
    from providers import karakeep
    providers["karakeep"] = True
    _karakeep = TTLCache("karakeep.stats", karakeep.get_stats,
                          lambda d: f"bookmarks={d.get('bookmarks')}")
    caches.append(_karakeep)
    log.info("karakeep provider enabled -> %s", karakeep.BASE)

if os.environ.get("DAWARICH_URL"):
    from providers import dawarich
    providers["dawarich"] = True
    _dawarich = TTLCache("dawarich.stats", dawarich.get_stats,
                          lambda d: f"distance={d.get('totalDistanceKm')}km")
    caches.append(_dawarich)
    log.info("dawarich provider enabled -> %s", dawarich.BASE)

if os.environ.get("AIRTRAIL_URL"):
    from providers import airtrail
    providers["airtrail"] = True
    _airtrail = TTLCache("airtrail.stats", airtrail.get_stats,
                          lambda d: f"flights={d.get('stats', {}).get('flights')}")
    caches.append(_airtrail)
    log.info("airtrail provider enabled -> %s", airtrail.URL)

if os.environ.get("TREK_URL"):
    from providers import trek
    providers["trek"] = True
    _trek = TTLCache("trek.stats", trek.get_stats,
                      lambda d: f"trips={d.get('total_trips')}")
    caches.append(_trek)
    log.info("trek provider enabled -> %s", trek.URL)

if os.environ.get("PLEX_URL"):
    from providers import plex
    providers["plex"] = True
    _plex = TTLCache("plex.stats", plex.get_stats,
                      lambda d: f"movies={d.get('movies')} tv={d.get('tvShows')}")
    caches.append(_plex)
    log.info("plex provider enabled -> %s", plex.BASE)
if os.environ.get("DOCKHAND_URL") and os.environ.get("DOCKHAND_USERNAME"):
    from providers import dockhand
    providers["dockhand"] = True
    _dockhand_stats = TTLCache(
        "dockhand.stats", dockhand.get_stats,
        lambda d: "running={running} stopped={stopped} total={total}".format_map(d),
    )
    _dockhand_version = TTLCache(
        "dockhand.version", dockhand.get_version,
        lambda d: f"updateAvailable={d.get('updateAvailable')}",
        ttl=3600,
    )
    caches += [_dockhand_stats, _dockhand_version]
    log.info("dockhand provider enabled -> %s", dockhand.BASE)

if os.environ.get("SYNOLOGY_URL"):
    from providers import synology
    providers["synology"] = True
    _synology = TTLCache("synology.stats", synology.get_stats,
                          lambda d: f"uptime={d.get('uptime')} cpu={d.get('cpu')}")
    caches.append(_synology)
    log.info("synology provider enabled -> %s", synology.BASE)

if os.environ.get("TRILIUM_URL"):
    from providers import trilium
    providers["trilium"] = True
    _trilium = TTLCache("trilium.stats", trilium.get_stats,
                         lambda d: f"notes={d.get('notes')} version={d.get('version')}")
    caches.append(_trilium)
    log.info("trilium provider enabled -> %s", trilium.BASE)

if os.environ.get("LINKWARDEN_URL"):
    from providers import linkwarden
    providers["linkwarden"] = True
    _linkwarden = TTLCache("linkwarden.stats", linkwarden.get_stats,
                            lambda d: f"links={d.get('links')} collections={d.get('collections')}")
    caches.append(_linkwarden)
    log.info("linkwarden provider enabled -> %s", linkwarden.BASE)

if os.environ.get("KARAKEEP_URL"):
    from providers import karakeep
    providers["karakeep"] = True
    _karakeep = TTLCache("karakeep.stats", karakeep.get_stats,
                          lambda d: f"bookmarks={d.get('bookmarks')}")
    caches.append(_karakeep)
    log.info("karakeep provider enabled -> %s", karakeep.BASE)

if os.environ.get("DAWARICH_URL"):
    from providers import dawarich
    providers["dawarich"] = True
    _dawarich = TTLCache("dawarich.stats", dawarich.get_stats,
                          lambda d: f"distance={d.get('totalDistanceKm')}km")
    caches.append(_dawarich)
    log.info("dawarich provider enabled -> %s", dawarich.BASE)

if os.environ.get("AIRTRAIL_URL"):
    from providers import airtrail
    providers["airtrail"] = True
    _airtrail = TTLCache("airtrail.stats", airtrail.get_stats,
                          lambda d: f"flights={d.get('stats', {}).get('flights')}")
    caches.append(_airtrail)
    log.info("airtrail provider enabled -> %s", airtrail.URL)

if os.environ.get("TREK_URL"):
    from providers import trek
    providers["trek"] = True
    _trek = TTLCache("trek.stats", trek.get_stats,
                      lambda d: f"trips={d.get('total_trips')}")
    caches.append(_trek)
    log.info("trek provider enabled -> %s", trek.URL)

if os.environ.get("PLEX_URL"):
    from providers import plex
    providers["plex"] = True
    _plex = TTLCache("plex.stats", plex.get_stats,
                      lambda d: f"movies={d.get('movies')} tv={d.get('tvShows')}")
    caches.append(_plex)
    log.info("plex provider enabled -> %s", plex.BASE)
def route(path, query):
    """Return (status, payload)."""
    if path == "/health":
        return 200, {"status": "ok", "providers": sorted(providers)}

    if path in ("/stats", "/bookorbit/stats"):
        if "bookorbit" not in providers:
            return 404, {"error": "bookorbit provider disabled (BOOKORBIT_URL not set)"}
        return 200, _bookorbit.get()

    if path == "/paperless/stats":
        if "paperless" not in providers:
            return 404, {"error": "paperless provider disabled (PAPERLESS_URL not set)"}
        return 200, _pl_stats.get()

    if path == "/paperless/tags":
        if "paperless" not in providers:
            return 404, {"error": "paperless provider disabled (PAPERLESS_URL not set)"}
        tags = _pl_tags.get()
        min_count = int(query.get("min", ["0"])[0])
        tags = [t for t in tags if t["count"] >= min_count]
        if "limit" in query:
            tags = tags[: int(query["limit"][0])]
        return 200, tags  # root-level array -> Homepage dynamic-list needs no `items:` path

    if path == "/github/stats":
        if "github" not in providers:
            return 404, {"error": "github provider disabled (GITHUB_USER not set or env incomplete)"}
        return 200, github_sync.get_stats()
        
    if path == "/dockhand/stats":
        if "dockhand" not in providers:
            return 404, {"error": "dockhand provider disabled"}
        return 200, _dockhand_stats.get()

    if path == "/dockhand/version":
        if "dockhand" not in providers:
            return 404, {"error": "dockhand provider disabled"}
        return 200, _dockhand_version.get()

    if path == "/synology/stats":
        if "synology" not in providers:
            return 404, {"error": "synology provider disabled"}
        return 200, _synology.get()

    if path == "/trilium/stats":
        if "trilium" not in providers:
            return 404, {"error": "trilium provider disabled"}
        return 200, _trilium.get()

    if path == "/linkwarden/stats":
        if "linkwarden" not in providers:
            return 404, {"error": "linkwarden provider disabled"}
        return 200, _linkwarden.get()

    if path == "/karakeep/stats":
        if "karakeep" not in providers:
            return 404, {"error": "karakeep provider disabled"}
        return 200, _karakeep.get()

    if path == "/dawarich/stats":
        if "dawarich" not in providers:
            return 404, {"error": "dawarich provider disabled"}
        return 200, _dawarich.get()

    if path == "/airtrail/stats":
        if "airtrail" not in providers:
            return 404, {"error": "airtrail provider disabled"}
        return 200, _airtrail.get()

    if path == "/trek/stats":
        if "trek" not in providers:
            return 404, {"error": "trek provider disabled"}
        return 200, _trek.get()

    if path == "/plex/stats":
        if "plex" not in providers:
            return 404, {"error": "plex provider disabled"}
        return 200, _plex.get()    

    return 404, {"error": "not found"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        try:
            status, payload = route(url.path, parse_qs(url.query))
        except NotImplementedError as exc:
            status, payload = 501, {"error": str(exc)}
        except ValueError:
            status, payload = 400, {"error": "bad query parameter"}
        except Exception as exc:  # upstream down and nothing cached
            status, payload = 502, {"error": f"upstream error: {_short(exc)}"}
        if status >= 500:
            log.warning("GET %s -> %d (%s)", url.path, status, payload.get("error"))
        self._send(status, json.dumps(payload).encode(), "application/json")

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/webhook", "/github/webhook") or "github" not in providers:
            return self._send(404, b'{"error": "not found"}', "application/json")
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > github_sync.MAX_BODY_BYTES:
            return self._send(413, b"payload too large", "text/plain")
        status, text = github_sync.handle_webhook(self.headers, self.rfile.read(length))
        self._send(status, text.encode(), "text/plain")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if self.path != "/health":
            log.debug("%s %s", self.address_string(), fmt % args)


def _warm_up():
    """Fetch once at startup so success/failure shows in the logs right away
    (failures are already logged by TTLCache) and the first widget request is instant."""
    for cache in caches:
        try:
            cache.get()
        except Exception:
            pass


def main():
    if not providers:
        log.error("No providers enabled - set BOOKORBIT_URL, PAPERLESS_URL and/or GITHUB_USER (+ HOMEPAGE_CONFIG, HOMEPAGE_GROUP, DOCKHAND_URL)")
    port = int(os.environ.get("PORT", "4321"))
    log.info("stats-proxy listening on :%d, providers=%s, cache=%ss", port, sorted(providers), CACHE_TTL)
    threading.Thread(target=_warm_up, daemon=True).start()
    if "github" in providers:
        github_sync.start()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
