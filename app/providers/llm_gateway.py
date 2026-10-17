"""LLM gateway: one OpenAI-compatible API URL for every model in the catalogue.

Other apps (Open WebUI, n8n, Home Assistant, scripts, ...) point at

    base URL : https://<your-host>/llm/v1          (or http://<nas-ip>:<proxy-port>/llm/v1)
    API key  : the value of LLM_GATEWAY_KEY

and can then use any Gemini model, any OpenRouter free model, and the fixed
GIT_DIAGRAMS_LLM_MODEL, without ever seeing your real provider keys.

Routes
  GET  /llm/v1/models              OpenAI-style list of the usable models (?working=1 hides models whose
                                   last test failed). Extra fields: owned_by, context_length, status, recommended.
  POST /llm/v1/chat/completions    forwarded to the right provider; "model" may be an id from the list or
                                   "auto"       -> the recommended model (a Gemini model while one works)
                                   "auto-free"  -> the best working OpenRouter free model
  GET  /llm/v1/health              {"ok": true} (needs the key too)

Auth: "Authorization: Bearer <LLM_GATEWAY_KEY>" (also accepted: "x-api-key" header or ?key=).
Without LLM_GATEWAY_KEY the gateway is OFF: it spends your provider quota, so it never runs open.

Env vars
  LLM_GATEWAY_KEY        secret the calling apps must send (required to enable the gateway)
  LLM_GATEWAY_PREFIX     default /llm/v1
  LLM_GATEWAY_MAX_BODY   max request size in bytes, default 1000000
  LLM_GATEWAY_CONCURRENCY  parallel upstream calls, default 4

Limits: no real streaming (stream=true is answered as a single SSE chunk), chat completions only
(no embeddings / images). Anthropic models are not served here.
"""
import hmac
import json
import logging
import os
import threading
import time
import uuid

import requests

from . import gemini_models, git_diagrams

log = logging.getLogger("stats-proxy.llm_gateway")

GATEWAY_KEY = os.environ.get("LLM_GATEWAY_KEY", "")
PREFIX = "/" + (os.environ.get("LLM_GATEWAY_PREFIX") or "/llm/v1").strip("/")
MAX_BODY = int(os.environ.get("LLM_GATEWAY_MAX_BODY", "1000000"))
_sem = threading.Semaphore(int(os.environ.get("LLM_GATEWAY_CONCURRENCY", "4")))
UPSTREAM_TIMEOUT = 300


def enabled():
    return bool(GATEWAY_KEY)


def _err(status, message, kind="invalid_request_error"):
    return status, json.dumps({"error": {"message": message, "type": kind}}).encode(), "application/json"


def _ok(payload):
    return 200, json.dumps(payload).encode(), "application/json"


def _authorized(query, headers):
    given = ""
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    given = given or headers.get("x-api-key") or headers.get("X-Api-Key") or (query.get("key") or [""])[0]
    return bool(given) and hmac.compare_digest(given, GATEWAY_KEY)


# ---- which models can be used ---------------------------------------------------------------
def _fixed_model():
    """The single model of a fixed OpenAI-style endpoint (GIT_DIAGRAMS_LLM_*), if any."""
    if git_diagrams.LLM_KIND == "openai" and git_diagrams.LLM_MODEL:
        return git_diagrams.LLM_MODEL
    return None


def _catalogue():
    info = gemini_models.get(False)
    return info.get("models", []), info.get("recommended")


def _resolve(requested):
    """-> (model_id, base_url, api_key) or raises ValueError."""
    models, recommended = _catalogue()
    requested = (requested or "").strip()
    if requested == "auto":
        requested = recommended or _fixed_model() or ""
    elif requested == "auto-free":
        free = [m for m in models if m.get("provider") == "openrouter"]
        best = [m for m in free if m["probe"]["status"] == "ok"] or [m for m in free if m["probe"]["status"] == "untested"]
        requested = best[0]["id"] if best else ""
    if not requested:
        raise ValueError("no model available for this request (the model list may not be loaded yet)")
    if gemini_models.has(requested):
        base, key = gemini_models.connection(requested)
        return requested, base, key
    if requested == _fixed_model():
        return requested, git_diagrams.LLM_URL, git_diagrams.LLM_KEY
    raise ValueError("model %r is not in the available list (see GET %s/models)" % (requested, PREFIX))


# ---- handlers ---------------------------------------------------------------------------------
def _list_models(query):
    models, recommended = _catalogue()
    only_working = (query.get("working") or ["0"])[0] == "1"
    data = []
    fixed = _fixed_model()
    if fixed and not any(m["id"] == fixed for m in models):
        data.append({"id": fixed, "object": "model", "created": 0, "owned_by": "configured",
                     "context_length": None, "status": "untested", "recommended": False})
    for m in models:
        status = (m.get("probe") or {}).get("status", "untested")
        if only_working and status not in ("ok", "untested"):
            continue
        data.append({"id": m["id"], "object": "model", "created": 0, "owned_by": m.get("provider", "gemini"),
                     "context_length": m.get("input_limit"), "status": status,
                     "recommended": m["id"] == recommended})
    for alias, note in (("auto", "recommended model"), ("auto-free", "best working OpenRouter free model")):
        data.insert(0, {"id": alias, "object": "model", "created": 0, "owned_by": "gateway",
                        "context_length": None, "status": "alias", "recommended": alias == "auto", "note": note})
    return _ok({"object": "list", "data": data})


def _sse(completion):
    """Wrap a finished chat completion as a minimal event stream (one content chunk + stop + [DONE])."""
    choice = (completion.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    base = {"id": completion.get("id") or "chatcmpl-" + uuid.uuid4().hex[:20], "object": "chat.completion.chunk",
            "created": completion.get("created") or int(time.time()), "model": completion.get("model", "")}
    first = dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}])
    last = dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}])
    if completion.get("usage"):
        last["usage"] = completion["usage"]
    body = "data: %s\n\ndata: %s\n\ndata: [DONE]\n\n" % (json.dumps(first), json.dumps(last))
    return 200, body.encode(), "text/event-stream; charset=utf-8"


def _chat(body):
    if len(body) > MAX_BODY:
        return _err(413, "request body is larger than %d bytes" % MAX_BODY)
    try:
        req = json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        return _err(400, "body must be JSON")
    if not isinstance(req, dict) or not isinstance(req.get("messages"), list) or not req["messages"]:
        return _err(400, "'messages' must be a non-empty list")
    try:
        model, base, key = _resolve(req.get("model"))
    except ValueError as exc:
        return _err(400, str(exc))
    want_stream = bool(req.get("stream"))
    req["model"], req["stream"] = model, False
    req.pop("stream_options", None)
    headers = {"content-type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    started = time.time()
    try:
        with _sem:
            r = requests.post(base + "/chat/completions", headers=headers, json=req, timeout=UPSTREAM_TIMEOUT)
    except requests.RequestException as exc:
        return _err(502, "upstream request failed: %s" % type(exc).__name__, "upstream_error")
    log.info("llm gateway: %s -> HTTP %d in %.1fs", model, r.status_code, time.time() - started)
    try:
        payload = r.json()
    except ValueError:
        return _err(502, "upstream returned a non-JSON answer (HTTP %d)" % r.status_code, "upstream_error")
    if r.status_code != 200:
        if isinstance(payload, dict) and "error" in payload:
            return r.status_code, json.dumps(payload).encode(), "application/json"
        return _err(r.status_code, str(payload)[:300], "upstream_error")
    if isinstance(payload, dict) and payload.get("error"):       # OpenRouter sometimes answers 200 with an error body
        return 502, json.dumps(payload).encode(), "application/json"
    if want_stream:
        return _sse(payload)
    return _ok(payload)


def handle(method, path, query, headers, body=b""):
    """Return (status, body_bytes, content_type) for PREFIX* paths, else None.
    method: 'GET' or 'POST'; query: parse_qs dict; headers: the request headers; body: raw bytes."""
    if path != PREFIX and not path.startswith(PREFIX + "/"):
        return None
    if not enabled():
        return _err(404, "the LLM gateway is off (set LLM_GATEWAY_KEY to enable it)", "not_found")
    if not _authorized(query, headers):
        return _err(401, "missing or wrong API key (send Authorization: Bearer <LLM_GATEWAY_KEY>)", "authentication_error")
    route = path[len(PREFIX):].rstrip("/")
    if route == "/models" and method == "GET":
        return _list_models(query)
    if route == "/chat/completions" and method == "POST":
        return _chat(body)
    if route == "/health" and method == "GET":
        return _ok({"ok": True, "models": len(_catalogue()[0]), "recommended": _catalogue()[1]})
    return _err(404, "unknown route %s %s" % (method, path), "not_found")