"""stats-proxy: one small HTTP service that exposes stats from several self-hosted
apps in a shape Homepage's `customapi` widget can consume.

Routes
  GET /health              liveness (never calls upstream apps)
  GET /stats               BookOrbit stats (kept for backwards compatibility)
  GET /bookorbit/stats     BookOrbit stats
  GET /paperless/stats     Paperless-ngx totals (documents, inbox, tags, ...)
  GET /paperless/tags      Paperless-ngx tags with live document counts, sorted desc
                           optional query: ?limit=10  ?min=1

Each provider is enabled only if its *_URL env var is set. Upstream calls are cached
(CACHE_TTL_SECS, default 60) and stale data is served if an upstream is briefly down.
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


class TTLCache:
    """Caches one callable's result. Thread-safe; serves stale data on upstream errors."""

    def __init__(self, name, fn, ttl=CACHE_TTL):
        self.name, self.fn, self.ttl = name, fn, ttl
        self.value, self.fetched_at, self.next_try = None, 0.0, 0.0
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            now = time.monotonic()
            if self.value is not None and now - self.fetched_at < self.ttl:
                return self.value
            if self.value is not None and now < self.next_try:
                return self.value  # recently failed; keep serving stale
            try:
                self.value = self.fn()
                self.fetched_at = time.monotonic()
                return self.value
            except NotImplementedError:
                raise
            except Exception as exc:
                log.warning("%s refresh failed: %s", self.name, exc)
                if self.value is None:
                    raise
                self.next_try = now + RETRY_AFTER_FAIL
                return self.value


# ---- provider wiring -------------------------------------------------------
providers = {}  # name -> enabled

if os.environ.get("BOOKORBIT_URL"):
    from providers import bookorbit
    providers["bookorbit"] = True
    _bookorbit = TTLCache("bookorbit", bookorbit.get_stats)

if os.environ.get("PAPERLESS_URL"):
    from providers import paperless
    providers["paperless"] = True
    _pl_stats = TTLCache("paperless.stats", paperless.get_stats)
    _pl_tags = TTLCache("paperless.tags", paperless.get_tags)


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
            status, payload = 502, {"error": f"upstream error: {exc}"}
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if self.path != "/health":
            log.debug("%s %s", self.address_string(), fmt % args)


def main():
    if not providers:
        log.error("No providers enabled - set BOOKORBIT_URL and/or PAPERLESS_URL")
    port = int(os.environ.get("PORT", "4321"))
    log.info("stats-proxy listening on :%d, providers=%s, cache=%ss", port, sorted(providers), CACHE_TTL)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
