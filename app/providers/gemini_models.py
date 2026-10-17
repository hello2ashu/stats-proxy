"""Model catalogue: which text models can YOUR API keys use right now? (Gemini + OpenRouter free)

Once a week (default: Sunday 00:00, container time) this provider
  1. asks the Gemini API (and OpenRouter, if you gave it a key) for every model they offer,
  2. keeps only models that can write text (drops TTS, image, live/voice, embedding, video, ...),
     and from OpenRouter only the FREE ones (prompt and completion price both 0),
  3. ranks them (Gemini: newest Flash first, then Flash-Lite, Pro, Gemma; OpenRouter: biggest context first),
  4. sends each of the top few a tiny test request, so you can see which ones really work on
     your keys (free tiers often have no quota for some models),
  5. picks a recommended model (always a Gemini model while one works), and saves everything to disk.

Git Diagrams shows this list as a model picker (grouped by provider), and
GIT_DIAGRAMS_LLM_MODEL=auto always uses the current recommendation.

Env vars
  GEMINI_API_KEY                    key from aistudio.google.com (falls back to GIT_DIAGRAMS_LLM_KEY
                                    when GIT_DIAGRAMS_LLM_URL points at generativelanguage.googleapis.com)
  GEMINI_BASE_URL                   default https://generativelanguage.googleapis.com/v1beta/openai
  GEMINI_MODELS_REFRESH_WEEKDAY     0=Monday .. 6=Sunday, default 6
  GEMINI_MODELS_REFRESH_HOUR        0-23, default 0 (midnight). Uses the container's time zone: set TZ.
  GEMINI_MODELS_PROBE_MAX           how many top Gemini models get a test request, default 10 (0 = none)
  GEMINI_MODELS_PROBE_DELAY_SECS    pause between test requests, default 6 (free tiers limit requests/minute)
  GEMINI_MODELS_DISABLED            turn the Gemini part off

  OPENROUTER_API_KEY                key from openrouter.ai/keys (OPENROUTER_KEY also works). Without it the
                                    OpenRouter part is off.
  OPENROUTER_BASE_URL               default https://openrouter.ai/api/v1
  OPENROUTER_MODELS_PROBE_MAX       how many top free models get a test request, default 3 (0 = none).
                                    Free models are capped per day (about 50 requests without credits),
                                    so keep this small.
  OPENROUTER_MIN_CONTEXT            ignore free models with a smaller context window, default 32000 tokens
  OPENROUTER_MODELS_DISABLED        turn the OpenRouter part off
Data is stored in GIT_DIAGRAMS_CACHE_DIR (default /tmp/git-diagrams-cache); mount a volume there to
keep it across container rebuilds.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta

import requests

log = logging.getLogger("stats-proxy.gemini_models")

_LLM_URL = os.environ.get("GIT_DIAGRAMS_LLM_URL", "")
BASE = (os.environ.get("GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
NATIVE = BASE[: -len("/openai")] if BASE.endswith("/openai") else "https://generativelanguage.googleapis.com/v1beta"
KEY = os.environ.get("GEMINI_API_KEY") or (
    os.environ.get("GIT_DIAGRAMS_LLM_KEY", "") if "generativelanguage.googleapis.com" in _LLM_URL else "")
WEEKDAY = int(os.environ.get("GEMINI_MODELS_REFRESH_WEEKDAY", "6")) % 7
HOUR = int(os.environ.get("GEMINI_MODELS_REFRESH_HOUR", "0")) % 24
PROBE_MAX = int(os.environ.get("GEMINI_MODELS_PROBE_MAX", "10"))
PROBE_DELAY = float(os.environ.get("GEMINI_MODELS_PROBE_DELAY_SECS", "6"))
CACHE_DIR = os.environ.get("GIT_DIAGRAMS_CACHE_DIR", "/tmp/git-diagrams-cache")
DISABLED = bool(os.environ.get("GEMINI_MODELS_DISABLED"))

OR_BASE = (os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
OR_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY", "")
OR_PROBE_MAX = int(os.environ.get("OPENROUTER_MODELS_PROBE_MAX", "3"))
OR_MIN_CONTEXT = int(os.environ.get("OPENROUTER_MIN_CONTEXT", "32000"))
OR_DISABLED = bool(os.environ.get("OPENROUTER_MODELS_DISABLED"))

STALE_AFTER = 7 * 86400 + 3600
MIN_MANUAL_GAP = 120            # seconds between manual refreshes
SCHEMA = 2                      # 2: every model has a "provider"

# Models that exist on the API but cannot answer a text prompt like ours.
EXCLUDE = re.compile(r"(tts|image|live|native-audio|transcribe|translate|robotics|computer-use|embedding|"
                     r"veo|lyria|antigravity|deep-research|banana|omni|customtools|aqa|imagen|realtime|learnlm)")
OR_EXCLUDE = re.compile(r"(embed|moderation|guard|safeguard|tts|whisper|image|audio|lyria|veo)", re.I)
FAMILY = re.compile(r"^gemini-(?P<ver>\d+(?:\.\d+)?)-(?P<fam>flash-lite|flash|pro)(?P<rest>.*)$")
ALIASES = {"gemini-flash-latest": "flash", "gemini-flash-lite-latest": "flash-lite", "gemini-pro-latest": "pro"}
FAMILY_ORDER = {"flash": 0, "flash-lite": 1, "pro": 2, "gemma": 3, "other": 4}

_lock = threading.Lock()
_state = {"data": None, "running": False, "last_manual": 0.0, "error": ""}
_wake = threading.Event()
_started = False


def gemini_enabled():
    return bool(KEY) and not DISABLED


def openrouter_enabled():
    return bool(OR_KEY) and not OR_DISABLED


def enabled():
    return gemini_enabled() or openrouter_enabled()


def _provider_on(provider):
    return openrouter_enabled() if provider == "openrouter" else gemini_enabled()


def provider_of(model_id):
    """'gemini' or 'openrouter' for a model in the catalogue (anything unknown counts as gemini)."""
    with _lock:
        for m in (_state["data"] or {}).get("models", []):
            if m["id"] == model_id:
                return m.get("provider", "gemini")
    return "gemini"


def connection(model_id=None):
    """(base_url, api_key) for OpenAI-style calls to the provider that serves model_id."""
    if model_id and provider_of(model_id) == "openrouter":
        return OR_BASE, OR_KEY
    return BASE, KEY


def input_limit(model_id):
    """Context window in tokens if the provider reported one, else None."""
    with _lock:
        for m in (_state["data"] or {}).get("models", []):
            if m["id"] == model_id:
                return m.get("input_limit")
    return None


# ---- Gemini model list -----------------------------------------------------------------------
def _clean_id(name):
    return name[len("models/"):] if name.startswith("models/") else name


def _describe(mid):
    m = FAMILY.match(mid)
    alias = False
    if m:
        fam, ver = m.group("fam"), float(m.group("ver"))
    elif mid in ALIASES:
        fam, ver, alias = ALIASES[mid], 0.0, True
    elif mid.startswith("gemma-"):
        g = re.match(r"gemma-(\d+(?:\.\d+)?)", mid)
        fam, ver = "gemma", float(g.group(1)) if g else 0.0
    else:
        fam, ver = "other", 0.0
    return {"family": fam, "version": ver, "alias": alias, "preview": "preview" in mid or "-exp" in mid}


def _rank_key(m):
    if m.get("provider") == "openrouter":       # biggest context window first
        return (1, -(m.get("input_limit") or 0), m["id"])
    return (0, FAMILY_ORDER.get(m["family"], 9), m["alias"], -m["version"], m["preview"], m["id"])


def _text_capable(mid):
    return mid.startswith(("gemini-", "gemma-")) and not EXCLUDE.search(mid)


def _list_native():
    """Rich list (token limits, supported methods). Returns [] if the endpoint refuses the key."""
    out, token = [], ""
    for _ in range(10):
        params = {"pageSize": 1000}
        if token:
            params["pageToken"] = token
        r = requests.get(NATIVE + "/models", params=params, headers={"x-goog-api-key": KEY}, timeout=30)
        r.raise_for_status()
        body = r.json()
        for m in body.get("models", []):
            methods = m.get("supportedGenerationMethods") or []
            out.append({"id": _clean_id(m.get("name", "")), "name": m.get("displayName") or "",
                        "input_limit": m.get("inputTokenLimit"), "output_limit": m.get("outputTokenLimit"),
                        "can_generate": "generateContent" in methods})
        token = body.get("nextPageToken") or ""
        if not token:
            break
    return out


def _list_compat():
    """Fallback: OpenAI-style /models (no token limits or method list)."""
    r = requests.get(BASE + "/models", headers={"Authorization": "Bearer " + KEY}, timeout=30)
    r.raise_for_status()
    return [{"id": _clean_id(m.get("id", "")), "name": m.get("display_name") or "",
             "input_limit": None, "output_limit": None, "can_generate": True} for m in r.json().get("data", [])]


def list_gemini():
    """-> (text models, source). Raises if neither endpoint works."""
    try:
        raw, source = _list_native(), "native"
        if not raw:
            raise ValueError("empty list")
    except Exception as exc:
        log.info("native model list unavailable (%s); using the OpenAI-compatible list", exc)
        raw, source = _list_compat(), "openai-compatible"
    models, seen = [], set()
    for m in raw:
        mid = m["id"]
        if not mid or mid in seen or not m["can_generate"] or not _text_capable(mid):
            continue
        seen.add(mid)
        entry = {**m, **_describe(mid), "provider": "gemini", "probe": {"status": "untested"}}
        entry["name"] = entry["name"] or mid
        models.append(entry)
    return models, source


# ---- OpenRouter free model list -------------------------------------------------------------
def _price_is_zero(value):
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def list_openrouter():
    """-> (free text models, 'openrouter'). The public /models endpoint needs no key, but we send it."""
    headers = {"Authorization": "Bearer " + OR_KEY} if OR_KEY else {}
    r = requests.get(OR_BASE + "/models", headers=headers, timeout=30)
    r.raise_for_status()
    models, seen = [], set()
    for m in r.json().get("data", []):
        mid = m.get("id") or ""
        pricing = m.get("pricing") or {}
        if not mid or mid in seen:
            continue
        if not (_price_is_zero(pricing.get("prompt")) and _price_is_zero(pricing.get("completion"))):
            continue                                         # same test as the jq filter: both prices "0"
        arch = m.get("architecture") or {}
        outs = arch.get("output_modalities") or []
        if outs and ("text" not in outs or "image" in outs or "audio" in outs):
            continue
        ctx = m.get("context_length") or 0
        if ctx < OR_MIN_CONTEXT or OR_EXCLUDE.search(mid):
            continue
        seen.add(mid)
        models.append({"id": mid, "name": m.get("name") or mid, "input_limit": ctx,
                       "output_limit": (m.get("top_provider") or {}).get("max_completion_tokens"),
                       "can_generate": True, "provider": "openrouter", "family": "openrouter", "version": 0.0,
                       "alias": False, "preview": False, "probe": {"status": "untested"}})
    return models, "openrouter"


# ---- test request ----------------------------------------------------------------------------
def probe(model_id, provider="gemini"):
    """One tiny chat request: does this model really work on this key right now?"""
    base, key = (OR_BASE, OR_KEY) if provider == "openrouter" else (BASE, KEY)
    started = time.time()
    try:
        r = requests.post(
            base + "/chat/completions", timeout=60,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={"model": model_id, "max_tokens": 16,
                  "messages": [{"role": "user", "content": "Reply with the single word OK."}]})
    except requests.RequestException as exc:
        return {"status": "error", "detail": type(exc).__name__}
    ms = int((time.time() - started) * 1000)
    if r.status_code == 200:
        try:
            if r.json().get("error"):                        # OpenRouter can answer 200 with an error body
                return {"status": "error", "detail": "provider error"}
        except ValueError:
            return {"status": "error", "detail": "bad JSON"}
        return {"status": "ok", "ms": ms}
    text = r.text[:300]
    if r.status_code == 402:
        return {"status": "no_quota", "detail": "HTTP 402"}
    if r.status_code == 429:
        none_at_all = re.search(r"limit:\s*0|free_tier|free-models-per-day", text, re.I)
        return {"status": "no_quota" if none_at_all else "rate_limited", "detail": "HTTP 429"}
    if r.status_code in (401, 403):
        return {"status": "denied", "detail": "HTTP %d" % r.status_code}
    if r.status_code == 404:
        return {"status": "unavailable", "detail": "HTTP 404"}
    return {"status": "error", "detail": "HTTP %d" % r.status_code}


def _recommend(models):
    """Best model for diagram building: newest working Gemini Flash that is not a preview.
    An OpenRouter model is only recommended when no Gemini model is usable."""
    gem = [m for m in models if m.get("provider", "gemini") == "gemini"]
    working = [m for m in gem if m["probe"]["status"] == "ok"]
    pool = working or [m for m in gem if m["probe"]["status"] == "untested"]
    for m in pool:
        if not m["preview"] and not m["alias"] and m["family"] in ("flash", "flash-lite"):
            return m["id"]
    if pool:
        return pool[0]["id"]
    ors = [m for m in models if m.get("provider") == "openrouter" and m["probe"]["status"] == "ok"]
    return ors[0]["id"] if ors else None


# ---- refresh + persistence ---------------------------------------------------------------------
def _path():
    return os.path.join(CACHE_DIR, "gemini-models.json")


def _save(data):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, _path())
    except OSError as exc:
        log.warning("could not save the model list to %s: %s", CACHE_DIR, exc)


def _load():
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("schema") != SCHEMA:
            return None
        data["models"] = [m for m in data.get("models", []) if _provider_on(m.get("provider", "gemini"))]
        return data
    except (OSError, ValueError):
        return None


def _prev_models(provider):
    with _lock:
        return [m for m in (_state["data"] or {}).get("models", []) if m.get("provider", "gemini") == provider]


def refresh(do_probe=True):
    """Re-read the model lists and test the top candidates. Safe to call from any thread."""
    with _lock:
        if _state["running"]:
            return
        _state["running"] = True
    try:
        models, sources, errors = [], [], []
        for provider, lister in (("gemini", list_gemini), ("openrouter", list_openrouter)):
            if not _provider_on(provider):
                continue
            try:
                found, src = lister()
                models += found
                sources.append(src)
            except Exception as exc:
                errors.append("%s: %s: %s" % (provider, type(exc).__name__, str(exc)[:150]))
                models += _prev_models(provider)             # keep what we had for the provider that failed
        if not sources:
            raise RuntimeError("; ".join(errors) or "no provider is enabled")
        models.sort(key=_rank_key)
        if do_probe:
            tested = 0
            for provider, cap in (("gemini", PROBE_MAX), ("openrouter", OR_PROBE_MAX)):
                if cap <= 0:
                    continue
                for m in [x for x in models if x.get("provider", "gemini") == provider][:cap]:
                    if tested:
                        time.sleep(PROBE_DELAY)
                    m["probe"] = probe(m["id"], provider)
                    tested += 1
        data = {"schema": SCHEMA, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "source": "+".join(sources), "models": models, "recommended": _recommend(models)}
        with _lock:
            _state["data"], _state["error"] = data, "; ".join(errors)
        _save(data)
        ok = sum(1 for m in models if m["probe"]["status"] == "ok")
        log.info("models refreshed: %d text models (%d openrouter free), %d tested ok, recommended=%s",
                 len(models), sum(1 for m in models if m.get("provider") == "openrouter"), ok, data["recommended"])
    except Exception as exc:
        msg = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        log.warning("model refresh failed - keeping the previous list: %s", msg)
        with _lock:
            _state["error"] = msg
    finally:
        with _lock:
            _state["running"] = False


def next_run(now=None):
    """Next scheduled refresh (local time): WEEKDAY at HOUR:00."""
    now = now or datetime.now()
    target = now.replace(hour=HOUR, minute=0, second=0, microsecond=0)
    target += timedelta(days=(WEEKDAY - now.weekday()) % 7)
    if target <= now:
        target += timedelta(days=7)
    return target


def _loop():
    data = _state["data"]
    age = None
    if data:
        try:
            age = time.time() - datetime.fromisoformat(data["updated_at"]).timestamp()
        except (KeyError, ValueError):
            age = None
    if data is None or age is None or age > STALE_AFTER:
        time.sleep(5)               # let the server finish starting first
        refresh()
    target = next_run()
    log.info("models: next scheduled refresh %s", target.isoformat(timespec="minutes"))
    while True:
        _wake.wait(timeout=min(3600, max(1.0, (target - datetime.now()).total_seconds())))
        _wake.clear()
        if datetime.now() >= target:
            refresh()
            target = next_run()
            log.info("models: next scheduled refresh %s", target.isoformat(timespec="minutes"))


def start():
    """Load the saved list and start the weekly scheduler (idempotent)."""
    global _started
    if not enabled() or _started:
        return
    _started = True
    with _lock:
        _state["data"] = _load()
    threading.Thread(target=_loop, daemon=True, name="gemini-models").start()


# ---- API used by git_diagrams ------------------------------------------------------------------
def get(force=False):
    """Current catalogue. force=True starts a background refresh (rate-limited)."""
    if force and enabled():
        with _lock:
            allowed = not _state["running"] and time.time() - _state["last_manual"] > MIN_MANUAL_GAP
            if allowed:
                _state["last_manual"] = time.time()
        if allowed:
            threading.Thread(target=refresh, daemon=True, name="gemini-refresh").start()
    with _lock:
        data = dict(_state["data"] or {})
        running, error = _state["running"], _state["error"]
    recommended = data.get("recommended")
    models = [dict(m, recommended=(m["id"] == recommended)) for m in data.get("models", [])]
    return {"enabled": enabled(), "providers": {"gemini": gemini_enabled(), "openrouter": openrouter_enabled()},
            "models": models, "recommended": recommended,
            "updated_at": data.get("updated_at"), "source": data.get("source"),
            "next_refresh_at": next_run().isoformat(timespec="minutes") if enabled() else None,
            "refreshing": running, "error": error}


def ids():
    with _lock:
        return {m["id"] for m in (_state["data"] or {}).get("models", [])
                if _provider_on(m.get("provider", "gemini"))}


def has(model_id):
    return enabled() and model_id in ids()


def recommended():
    with _lock:
        return (_state["data"] or {}).get("recommended")