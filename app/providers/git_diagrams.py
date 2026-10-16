"""Git Diagrams provider: one Mermaid flow diagram per GitHub repo, plus a small viewer page.

Routes (all handled by handle(); nothing else in proxy.py needs to know about them)
  GET /git-diagrams                 viewer page (repo tabs; click a tab -> that repo's diagram)
  GET /git-diagrams/repos           repo list  {user, updated_at, repos: [...]}
  GET /git-diagrams/repo/<name>     {name, mermaid, html_url, generated_at, facts}
  GET /git-diagrams/overview        {mermaid}  account overview grouped by language
  Add ?refresh=1 to any data route to bypass the cache.

New repos appear automatically: the repo list is re-fetched from GitHub whenever its cache
(GIT_DIAGRAMS_LIST_TTL_SECS, default 600) expires, and the viewer polls every 5 minutes.

Each diagram shows, as far as the repo contains it:
  repo -> top-level files/folders -> (docker compose file -> services -> depends_on edges,
  published ports, Dockerfile base images, GitHub Actions CI)

Env vars
  GIT_DIAGRAMS_USER             GitHub user (default: GITHUB_USER, which you already set)
  GIT_DIAGRAMS_TOKEN            optional; falls back to GITHUB_TOKEN. Without a token GitHub allows
                                60 API calls/hour per IP. A token raises that to 5000/hour.
  GIT_DIAGRAMS_INCLUDE_PRIVATE  1 = also list private repos (needs TOKEN *and* KEY, see below)
  GIT_DIAGRAMS_KEY              optional shared secret. When set, every /git-diagrams* request must
                                carry ?key=<value>. REQUIRED for private repos so their file lists
                                are not readable by anyone who can reach the proxy.
  GIT_DIAGRAMS_LIST_TTL_SECS    repo-list cache, default 600
  GIT_DIAGRAMS_TTL_SECS         per-repo diagram cache, default 1800
  GIT_DIAGRAMS_DISABLED         set to turn the provider off

Token scopes: a fine-grained personal access token with read-only "Contents" and "Metadata"
on the repos you want drawn is enough.
"""
import hmac
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, unquote

import requests

USER = os.environ.get("GIT_DIAGRAMS_USER") or os.environ.get("GITHUB_USER", "")
TOKEN = os.environ.get("GIT_DIAGRAMS_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
KEY = os.environ.get("GIT_DIAGRAMS_KEY", "")
WANT_PRIVATE = os.environ.get("GIT_DIAGRAMS_INCLUDE_PRIVATE", "").lower() in ("1", "true", "yes")
PRIVATE_OK = bool(WANT_PRIVATE and TOKEN and KEY)  # never expose private repos without a key
LIST_TTL = float(os.environ.get("GIT_DIAGRAMS_LIST_TTL_SECS", "600"))
DIAGRAM_TTL = float(os.environ.get("GIT_DIAGRAMS_TTL_SECS", "1800"))
TIMEOUT = 15

MAX_TOP_LEVEL = 16      # top-level files/folders drawn per repo
MAX_FILE_NODES = 28     # total file/folder nodes (top level + children)
CHILDREN_PER_DIR = 4
COMPOSE_NAMES = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
IGNORED = {".git", ".gitignore", ".gitattributes", ".DS_Store", "LICENSE", "LICENSE.md", "node_modules"}
DB_WORDS = ("postgres", "mysql", "mariadb", "mongo", "redis", "valkey", "memcached", "sqlite")


class EmptyRepo(Exception):
    pass


def warnings():
    """Config problems worth logging at startup."""
    out = []
    if not USER:
        out.append("no GitHub user (set GIT_DIAGRAMS_USER or GITHUB_USER)")
    if WANT_PRIVATE and not TOKEN:
        out.append("GIT_DIAGRAMS_INCLUDE_PRIVATE is set but no token - listing public repos only")
    if WANT_PRIVATE and TOKEN and not KEY:
        out.append("GIT_DIAGRAMS_INCLUDE_PRIVATE is set but GIT_DIAGRAMS_KEY is not - "
                   "listing public repos only (private repos are never served without a key)")
    if not TOKEN:
        out.append("no token: GitHub allows only 60 API calls/hour; set GIT_DIAGRAMS_TOKEN")
    return out


def describe():
    return "user=%s, private=%s, key=%s, token=%s" % (
        USER, "yes" if PRIVATE_OK else "no", "required" if KEY else "none", "yes" if TOKEN else "no")


# ---- tiny thread-safe cache that serves stale data if GitHub is unreachable ------------------
class _Cache:
    def __init__(self, ttl):
        self.ttl, self.items, self.lock = ttl, {}, threading.Lock()

    def get(self, key, fn, force=False):
        with self.lock:
            hit = self.items.get(key)
        now = time.time()
        if hit and not force and now - hit[0] < self.ttl:
            return hit[1]
        try:
            value = fn()
        except Exception:
            if hit:
                return hit[1]
            raise
        with self.lock:
            self.items[key] = (time.time(), value)
        return value


_repo_cache = _Cache(LIST_TTL)
_diagram_cache = _Cache(DIAGRAM_TTL)


# ---- GitHub API ------------------------------------------------------------------------------
def _get(path, raw=False):
    headers = {
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "User-Agent": "stats-proxy-git-diagrams",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    r = requests.get("https://api.github.com" + path, headers=headers, timeout=TIMEOUT)
    if r.status_code == 409:  # "Git Repository is empty"
        raise EmptyRepo()
    if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
        reset = r.headers.get("X-RateLimit-Reset", "?")
        raise RuntimeError("GitHub rate limit reached (resets at epoch %s) - set GIT_DIAGRAMS_TOKEN" % reset)
    r.raise_for_status()
    return r.text if raw else r.json()


def _fetch_repos():
    repos, page = [], 1
    while page <= 10:  # 1000 repos max
        if PRIVATE_OK:
            path = "/user/repos?per_page=100&page=%d&affiliation=owner&sort=pushed" % page
        else:
            path = "/users/%s/repos?per_page=100&page=%d&type=owner&sort=pushed" % (quote(USER), page)
        batch = _get(path)
        for r in batch:
            if r["owner"]["login"].lower() != USER.lower():
                continue
            repos.append({
                "name": r["name"],
                "full_name": r["full_name"],
                "description": r.get("description") or "",
                "language": r.get("language") or "",
                "private": bool(r.get("private")),
                "fork": bool(r.get("fork")),
                "archived": bool(r.get("archived")),
                "pushed_at": r.get("pushed_at") or "",
                "html_url": r["html_url"],
                "default_branch": r.get("default_branch") or "main",
            })
        if len(batch) < 100:
            break
        page += 1
    repos.sort(key=lambda r: r["name"].lower())
    return {"repos": repos, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def get_repos(force=False):
    return _repo_cache.get("repos", _fetch_repos, force)


def _find_repo(name, force=False):
    for attempt in (0, 1):  # a brand-new repo may not be in the cache yet: refresh once
        for r in get_repos(force or attempt == 1)["repos"]:
            if r["name"] == name:
                return r
    return None


# ---- docker compose / Dockerfile parsing (no PyYAML needed) ----------------------------------
def _unq(s):
    return s.strip().strip("'\"")


def parse_compose(text):
    """Minimal parser for the parts of a compose file that matter for a flow diagram:
    service names, image/build, depends_on (list or map form) and published ports."""
    services = {}
    in_services = False
    svc_indent = prop_indent = dep_indent = None
    cur = key = None
    for raw in text.splitlines():
        line = re.sub(r"\s+#.*$", "", raw).rstrip()
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_services = (s == "services:")
            svc_indent = cur = key = None
            continue
        if not in_services:
            continue
        if svc_indent is None:
            svc_indent = indent
        if indent == svc_indent and s.endswith(":"):
            cur = _unq(s[:-1])
            services[cur] = {"image": "", "build": False, "depends": [], "ports": []}
            prop_indent = dep_indent = key = None
            continue
        if cur is None:
            continue
        if prop_indent is None and indent > svc_indent:
            prop_indent = indent
        if indent == prop_indent:
            k, _, v = s.partition(":")
            key, v = k.strip(), _unq(v)
            dep_indent = None
            if key == "image":
                services[cur]["image"] = v
            elif key == "build":
                services[cur]["build"] = True
            elif key in ("depends_on", "ports") and v.startswith("["):
                items = [_unq(x) for x in v.strip("[]").split(",") if x.strip()]
                services[cur]["depends" if key == "depends_on" else "ports"] += items
        elif indent > (prop_indent or 0):
            if key == "depends_on":
                if s.startswith("- "):
                    services[cur]["depends"].append(_unq(s[2:]))
                elif s.endswith(":") or ": " in s:
                    if dep_indent is None:
                        dep_indent = indent
                    if indent == dep_indent:
                        services[cur]["depends"].append(_unq(s.split(":")[0]))
            elif key == "ports" and s.startswith("- "):
                services[cur]["ports"].append(_unq(s[2:]))
    return services


def _host_port(spec):
    parts = spec.split("/")[0].split(":")
    if len(parts) >= 2:
        return parts[-2]
    return parts[0]


def dockerfile_bases(text):
    out = []
    for m in re.finditer(r"^\s*FROM\s+(?:--\S+\s+)?(\S+)", text, re.I | re.M):
        if m.group(1).lower() != "scratch" and m.group(1) not in out:
            out.append(m.group(1))
    return out[:3]


# ---- Mermaid generation ----------------------------------------------------------------------
def esc(text, limit=48):
    text = re.sub(r'["<>#&;`\\]', " ", str(text)).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."


CLASSDEFS = [
    "classDef repo fill:#1d4ed8,stroke:#60a5fa,color:#ffffff,stroke-width:2px",
    "classDef dir fill:#172554,stroke:#3b82f6,color:#dbeafe",
    "classDef file fill:#1c2028,stroke:#3a414c,color:#cbd5e1",
    "classDef tool fill:#052e16,stroke:#22c55e,color:#bbf7d0",
    "classDef svc fill:#1e1b4b,stroke:#818cf8,color:#e0e7ff",
    "classDef db fill:#431407,stroke:#f59e0b,color:#fde68a",
    "classDef ext fill:#0c4a6e,stroke:#06b6d4,color:#cffafe",
    "classDef more fill:#422006,stroke:#d4a72c,color:#fef3c7",
]


def _fetch_text(repo, path):
    return _get("/repos/%s/contents/%s?ref=%s" % (repo["full_name"], quote(path), quote(repo["default_branch"], safe="")),
                raw=True)


def build_repo(repo):
    name = repo["name"]
    head = 'flowchart LR\n  R["%s"]:::repo' % esc(name)
    lines, facts = [], {"files": 0, "compose_services": 0, "dockerfile": False, "ci": False}
    try:
        tree = _get("/repos/%s/git/trees/%s?recursive=1" % (repo["full_name"], quote(repo["default_branch"], safe="")))["tree"]
    except EmptyRepo:
        return 'flowchart LR\n  R["%s (empty repository)"]:::repo\n  %s' % (esc(name), CLASSDEFS[0]), facts

    types = {t["path"]: t["type"] for t in tree}
    top = sorted((p for p in types if "/" not in p and p not in IGNORED),
                 key=lambda p: (types[p] != "tree", p.lower()))
    facts["files"] = len(top)

    # --- folders / files -------------------------------------------------------------------
    ids, n = {}, 0
    lines.append('  subgraph FILES["Repository files"]\n    direction TB')
    for p in top[:MAX_TOP_LEVEL]:
        n += 1
        ids[p] = "F%d" % n
        is_dir = types[p] == "tree"
        lines.append('    F%d["%s"]:::%s' % (n, esc(p + ("/" if is_dir else "")), "dir" if is_dir else "file"))
    for p in top[:MAX_TOP_LEVEL]:
        if types[p] != "tree" or n >= MAX_FILE_NODES:
            continue
        kids = sorted((q for q in types if q.startswith(p + "/") and q.count("/") == 1 and q.rsplit("/", 1)[1] not in IGNORED),
                      key=lambda q: (types[q] != "tree", q.lower()))
        for q in kids[:CHILDREN_PER_DIR]:
            if n >= MAX_FILE_NODES:
                break
            n += 1
            ids[q] = "F%d" % n
            is_dir = types[q] == "tree"
            lines.append('    F%d["%s"]:::%s' % (n, esc(q.rsplit("/", 1)[1] + ("/" if is_dir else "")), "dir" if is_dir else "file"))
            lines.append("    %s --> F%d" % (ids[p], n))
    if len(top) > MAX_TOP_LEVEL:
        lines.append('    MORE["+ %d more"]:::more' % (len(top) - MAX_TOP_LEVEL))
    lines.append("  end")
    lines.append("  R --> FILES")

    # --- docker compose: services and their dependencies ----------------------------------
    composes = sorted((p for p in types if p.rsplit("/", 1)[-1] in COMPOSE_NAMES and p.count("/") <= 1 and types[p] == "blob"),
                      key=lambda p: (p.count("/"), p))[:2]
    published = []
    for ci, path in enumerate(composes):
        try:
            services = parse_compose(_fetch_text(repo, path))
        except Exception:
            continue
        if not services:
            continue
        facts["compose_services"] += len(services)
        lines.append('  C%d(["%s"]):::tool' % (ci, esc(path)))
        lines.append("  R --> C%d" % ci)
        lines.append('  subgraph RT%d["Runtime: docker compose"]\n    direction TB' % ci)
        sid = {name_: "S%d_%d" % (ci, i) for i, name_ in enumerate(services)}
        needed = {d for s in services.values() for d in s["depends"] if d in services}
        for sname, s in services.items():
            is_db = any(w in (s["image"] or sname).lower() for w in DB_WORDS)
            label = esc(sname) + ("<br/>" + esc(s["image"].split("/")[-1], 36) if s["image"] else
                                  ("<br/>built from source" if s["build"] else ""))
            shape = '[("%s")]' % label if is_db else '["%s"]' % label
            lines.append("    %s%s:::%s" % (sid[sname], shape, "db" if is_db else "svc"))
        lines.append("  end")
        for sname, s in services.items():
            if sname not in needed:
                lines.append("  C%d --> %s" % (ci, sid[sname]))          # entry points of the stack
            for d in s["depends"]:
                if d in sid:
                    lines.append("  %s -.->|needs| %s" % (sid[sname], sid[d]))
            for spec in s["ports"][:2]:
                hp = _host_port(spec)
                if hp.isdigit():
                    published.append((hp, sid[sname]))
    if published:
        lines.append('  USER(["Browser / reverse proxy"]):::ext')
        for hp, target in published[:10]:
            lines.append("  USER -->|port %s| %s" % (hp, target))

    # --- Dockerfile + CI -----------------------------------------------------------------
    if "Dockerfile" in types:
        facts["dockerfile"] = True
        lines.append('  DF(["Dockerfile"]):::tool\n  R --> DF')
        try:
            for bi, base in enumerate(dockerfile_bases(_fetch_text(repo, "Dockerfile"))):
                lines.append('  DB%d(["FROM %s"]):::ext\n  DF --> DB%d' % (bi, esc(base, 40), bi))
        except Exception:
            pass
    if any(p.startswith(".github/workflows/") for p in types):
        facts["ci"] = True
        lines.append('  CI(["GitHub Actions"]):::tool\n  R -.-> CI')

    return head + "\n" + "\n".join(lines) + "\n  " + "\n  ".join(CLASSDEFS), facts


def build_overview(repos):
    by_lang = {}
    for r in repos:
        by_lang.setdefault(r["language"] or "Config / no code", []).append(r)
    lines = ["flowchart TD", '  U(("%s")):::repo' % esc(USER)]
    for li, (lang, rs) in enumerate(sorted(by_lang.items())):
        lines.append('  subgraph L%d["%s (%d)"]' % (li, esc(lang), len(rs)))
        for i, r in enumerate(rs):
            tag = " (private)" if r["private"] else (" (fork)" if r["fork"] else "")
            lines.append('    P%d_%d["%s%s"]:::%s' % (li, i, esc(r["name"]), tag, "more" if r["fork"] else "svc"))
        lines.append("  end")
        lines.append("  U --> L%d" % li)
    return "\n".join(lines) + "\n  " + "\n  ".join(CLASSDEFS)


# ---- HTTP entry point (called from proxy.py's Handler.do_GET) --------------------------------
def _json(status, payload):
    return status, json.dumps(payload).encode(), "application/json"


def handle(path, query):
    """Return (status, body_bytes, content_type) for /git-diagrams* paths, else None."""
    if path != "/git-diagrams" and not path.startswith("/git-diagrams/"):
        return None
    if KEY and not hmac.compare_digest(query.get("key", [""])[0], KEY):
        return _json(401, {"error": "missing or wrong ?key="})
    if not USER:
        return _json(404, {"error": "git diagrams provider has no GitHub user configured"})
    force = query.get("refresh", ["0"])[0] == "1"
    try:
        if path in ("/git-diagrams", "/git-diagrams/"):
            return 200, PAGE.encode(), "text/html; charset=utf-8"
        if path == "/git-diagrams/repos":
            data = get_repos(force)
            return _json(200, {"user": USER, **data})
        if path == "/git-diagrams/overview":
            repos = get_repos(force)["repos"]
            return _json(200, {"mermaid": build_overview(repos), "count": len(repos)})
        if path.startswith("/git-diagrams/repo/"):
            name = unquote(path[len("/git-diagrams/repo/"):])
            repo = _find_repo(name, force)
            if not repo:
                return _json(404, {"error": "unknown repository %r" % name})

            def make():
                mermaid, facts = build_repo(repo)
                return {"name": name, "mermaid": mermaid, "facts": facts, "html_url": repo["html_url"],
                        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            return _json(200, _diagram_cache.get(name, make, force))
    except Exception as exc:
        return _json(502, {"error": "%s: %s" % (type(exc).__name__, str(exc)[:300])})
    return _json(404, {"error": "not found"})


# ---- the viewer page -------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Git diagrams</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@600;700&family=Manrope:wght@400;600;700&display=swap');
  :root{
    --bg:#14171c; --card:#1c2028; --line:#2a2f38; --text:#e8eaed; --muted:#9aa3af;
    --green:#22c55e; --blue:#3b82f6; --amber:#f59e0b; --red:#ef4444;
  }
  *{ box-sizing:border-box; }
  html,body{ height:100%; margin:0; background:var(--bg); color:var(--text);
    font-family:'Manrope',system-ui,-apple-system,'Segoe UI',sans-serif; font-size:14px; }
  .app{ display:flex; flex-direction:column; height:100%; padding:14px 16px 16px; gap:12px; }

  header{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  h1{ margin:0; font-family:'Outfit',sans-serif; font-weight:700; font-size:20px; }
  .meta{ color:var(--muted); font-size:12.5px; }
  .spacer{ flex:1; }
  input[type=search]{ background:var(--card); border:1px solid var(--line); color:var(--text);
    border-radius:8px; padding:7px 10px; width:200px; font:inherit; }
  input[type=search]:focus,button:focus-visible,.tab:focus-visible{ outline:2px solid var(--blue); outline-offset:1px; }
  button{ font:inherit; color:var(--text); background:var(--card); border:1px solid var(--line);
    border-radius:8px; padding:6px 11px; cursor:pointer; }
  button:hover{ border-color:var(--blue); }

  .tabs{ display:flex; gap:6px; flex-wrap:wrap; max-height:112px; overflow-y:auto; padding:2px 0; }
  .tab{ display:inline-flex; align-items:center; gap:6px; background:var(--card); border:1px solid var(--line);
    color:var(--muted); border-radius:999px; padding:5px 12px; cursor:pointer; white-space:nowrap; }
  .tab:hover{ color:var(--text); border-color:#3a414c; }
  .tab[aria-selected=true]{ color:#fff; background:#1d4ed8; border-color:#60a5fa; }
  .tab .flag{ font-size:11px; opacity:.8; }
  .tab .new{ background:var(--green); color:#04210f; border-radius:999px; padding:0 6px; font-size:10.5px; font-weight:700; }

  .panel{ flex:1; min-height:0; display:flex; flex-direction:column; background:var(--card);
    border:1px solid var(--line); border-radius:12px; overflow:hidden; }
  .bar{ display:flex; align-items:center; gap:8px; padding:9px 12px; border-bottom:1px solid var(--line); flex-wrap:wrap; }
  .bar .title{ font-family:'Outfit',sans-serif; font-weight:700; font-size:15px; }
  .bar .desc{ color:var(--muted); font-size:12.5px; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; }
  .bar button{ padding:4px 9px; font-size:12.5px; }
  .stage{ flex:1; min-height:0; overflow:auto; padding:14px; cursor:grab; }
  .stage.drag{ cursor:grabbing; user-select:none; }
  .stage svg{ display:block; margin:0 auto; height:auto; }
  .note{ padding:36px 16px; color:var(--muted); text-align:center; }
  .note.err{ color:#fca5a5; }
  .note code{ background:#14171c; border:1px solid var(--line); padding:1px 5px; border-radius:5px; }
  pre.src{ margin:0; padding:14px; color:#cbd5e1; font-size:12px; overflow:auto; }
  @media (prefers-reduced-motion:no-preference){ .stage svg{ animation:in .18s ease-out; } }
  @keyframes in{ from{ opacity:0 } to{ opacity:1 } }
</style>
</head>
<body>
<div class="app">
  <header>
    <h1>Git diagrams</h1>
    <span class="meta" id="meta">loading repositories...</span>
    <span class="spacer"></span>
    <input type="search" id="filter" placeholder="Filter repositories" aria-label="Filter repositories">
    <button id="refresh" title="Fetch the repo list and this diagram fresh from GitHub">Refresh</button>
  </header>
  <div class="tabs" id="tabs" role="tablist" aria-label="Repositories"></div>
  <section class="panel">
    <div class="bar">
      <span class="title" id="ptitle">&nbsp;</span>
      <span class="desc" id="pdesc"></span>
      <button id="zout" title="Zoom out" aria-label="Zoom out">&minus;</button>
      <button id="zfit" title="Fit to width">Fit</button>
      <button id="zin" title="Zoom in" aria-label="Zoom in">+</button>
      <button id="copy" title="Copy the Mermaid source">Copy source</button>
      <button id="open" title="Open the repository on GitHub">GitHub</button>
    </div>
    <div class="stage" id="stage"><div class="note">Pick a repository above.</div></div>
  </section>
</div>

<script src="https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"></script>
<script>
(function () {
  'use strict';
  var qs = new URLSearchParams(location.search);
  var KEY = qs.get('key') || '';
  var OVERVIEW = '__overview__';
  var $ = function (id) { return document.getElementById(id); };
  var state = { repos: [], active: null, zoom: 1, base: 0, src: '', htmlUrl: '', token: 0, rid: 0, seen: null, user: '' };

  function url(path, extra) {
    var p = new URLSearchParams(extra || {});
    if (KEY) p.set('key', KEY);
    var s = p.toString();
    return 'git-diagrams/' + path + (s ? '?' + s : '');
  }
  function getJson(u) {
    return fetch(u, { cache: 'no-store' }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
        return j;
      });
    });
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function note(html, err) { $('stage').innerHTML = '<div class="note' + (err ? ' err' : '') + '">' + html + '</div>'; }

  if (window.mermaid) {
    mermaid.initialize({
      startOnLoad: false, theme: 'dark', securityLevel: 'strict',
      flowchart: { htmlLabels: true, curve: 'basis', nodeSpacing: 28, rankSpacing: 46 },
      themeVariables: { fontFamily: 'Manrope, system-ui, sans-serif', fontSize: '14px',
        lineColor: '#6b7686', primaryTextColor: '#e8eaed', clusterBkg: '#171b22', clusterBorder: '#2f3640' }
    });
  }

  /* ---- seen-repos memory so brand-new repos get a "new" badge ---- */
  function loadSeen() { try { return JSON.parse(localStorage.getItem('gd_seen') || 'null'); } catch (e) { return null; } }
  function saveSeen(names) { try { localStorage.setItem('gd_seen', JSON.stringify(names)); } catch (e) { /* ignore */ } }

  /* ---- tabs ---- */
  function renderTabs() {
    var f = $('filter').value.trim().toLowerCase();
    var html = tabHtml(OVERVIEW, 'All repos', '', false);
    state.repos.forEach(function (r) {
      if (f && r.name.toLowerCase().indexOf(f) < 0) return;
      var flags = (r.private ? '<span class="flag" title="private">private</span>' : '') +
                  (r.fork ? '<span class="flag" title="fork">fork</span>' : '') +
                  (r.archived ? '<span class="flag" title="archived">archived</span>' : '');
      var isNew = state.seen && state.seen.indexOf(r.name) < 0;
      html += tabHtml(r.name, r.name, flags, isNew);
    });
    $('tabs').innerHTML = html;
  }
  function tabHtml(id, label, flags, isNew) {
    return '<button class="tab" role="tab" data-id="' + esc(id) + '" aria-selected="' + (state.active === id) + '">' +
           esc(label) + flags + (isNew ? '<span class="new">new</span>' : '') + '</button>';
  }
  $('tabs').addEventListener('click', function (e) {
    var b = e.target.closest('.tab');
    if (b) select(b.getAttribute('data-id'), false);
  });
  $('filter').addEventListener('input', renderTabs);

  /* ---- repo list ---- */
  function loadRepos(force) {
    return getJson(url('repos', force ? { refresh: 1 } : null)).then(function (d) {
      var names = d.repos.map(function (r) { return r.name; });
      if (state.seen === null) state.seen = loadSeen();
      if (state.seen === null) state.seen = names;           // first visit: nothing is "new"
      var added = names.filter(function (n) { return state.seen.indexOf(n) < 0; });
      state.repos = d.repos; state.user = d.user;
      var t = new Date(d.updated_at);
      $('meta').textContent = d.user + ' \u00b7 ' + d.repos.length + ' repositories' +
        (added.length ? ' \u00b7 ' + added.length + ' new' : '') + ' \u00b7 checked ' + t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      renderTabs();
      return d;
    });
  }

  /* ---- diagram ---- */
  function select(id, force) {
    state.active = id;
    var tok = ++state.token;
    try { history.replaceState(null, '', '#repo=' + encodeURIComponent(id)); } catch (e) { /* iframe may block */ }
    renderTabs();
    var tab = $('tabs').querySelector('[aria-selected=true]');
    if (tab && tab.scrollIntoView) tab.scrollIntoView({ block: 'nearest' });
    var isOv = id === OVERVIEW;
    var repo = state.repos.filter(function (r) { return r.name === id; })[0];
    $('ptitle').textContent = isOv ? 'All repositories' : id;
    $('pdesc').textContent = isOv ? 'Grouped by language' : (repo && repo.description) || '';
    note('Building diagram...');
    getJson(isOv ? url('overview', force ? { refresh: 1 } : null) : url('repo/' + encodeURIComponent(id), force ? { refresh: 1 } : null))
      .then(function (d) {
        if (tok !== state.token) return;                      // user already clicked another tab
        state.src = d.mermaid; state.htmlUrl = isOv ? ('https://github.com/' + state.user) : d.html_url;
        return draw(d.mermaid);
      })
      .catch(function (e) { if (tok === state.token) note('Could not build this diagram: ' + esc(e.message), true); });
  }

  function draw(src) {
    if (!window.mermaid) {
      $('stage').innerHTML = '<div class="note err">Mermaid could not load from cdn.jsdelivr.net, so only the source is shown.</div><pre class="src">' + esc(src) + '</pre>';
      return;
    }
    return mermaid.render('g' + (++state.rid), src).then(function (out) {
      $('stage').innerHTML = out.svg;
      var svg = $('stage').querySelector('svg');
      var vb = svg.viewBox && svg.viewBox.baseVal;
      state.base = vb && vb.width ? vb.width : svg.getBoundingClientRect().width;
      svg.removeAttribute('height'); svg.style.maxWidth = 'none';
      fit();
    }).catch(function (e) {
      $('stage').innerHTML = '<div class="note err">The diagram could not be drawn (' + esc(e.message || e) + '). Source below.</div><pre class="src">' + esc(src) + '</pre>';
    });
  }
  function applyZoom() {
    var svg = $('stage').querySelector('svg');
    if (svg && state.base) svg.style.width = Math.round(state.base * state.zoom) + 'px';
  }
  function fit() {
    var w = $('stage').clientWidth - 28;
    state.zoom = state.base ? Math.max(0.3, Math.min(1.4, w / state.base)) : 1;
    applyZoom();
  }
  $('zin').addEventListener('click', function () { state.zoom = Math.min(4, state.zoom * 1.2); applyZoom(); });
  $('zout').addEventListener('click', function () { state.zoom = Math.max(0.2, state.zoom / 1.2); applyZoom(); });
  $('zfit').addEventListener('click', fit);
  $('stage').addEventListener('wheel', function (e) {
    if (!e.ctrlKey) return;
    e.preventDefault();
    state.zoom = Math.max(0.2, Math.min(4, state.zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1)));
    applyZoom();
  }, { passive: false });
  (function pan() {
    var st = $('stage'), down = false, x = 0, y = 0, sl = 0, stp = 0;
    st.addEventListener('mousedown', function (e) { down = true; x = e.clientX; y = e.clientY; sl = st.scrollLeft; stp = st.scrollTop; st.classList.add('drag'); });
    window.addEventListener('mouseup', function () { down = false; st.classList.remove('drag'); });
    window.addEventListener('mousemove', function (e) { if (down) { st.scrollLeft = sl - (e.clientX - x); st.scrollTop = stp - (e.clientY - y); } });
  })();
  $('copy').addEventListener('click', function () {
    var b = $('copy');
    (navigator.clipboard ? navigator.clipboard.writeText(state.src) : Promise.reject()).then(
      function () { b.textContent = 'Copied'; }, function () { b.textContent = 'Copy blocked'; });
    setTimeout(function () { b.textContent = 'Copy source'; }, 1600);
  });
  $('open').addEventListener('click', function () { if (state.htmlUrl) window.open(state.htmlUrl, '_blank', 'noopener'); });
  $('refresh').addEventListener('click', function () {
    loadRepos(true).then(function () { if (state.active) select(state.active, true); }).catch(function (e) { note(esc(e.message), true); });
  });
  window.addEventListener('resize', function () { if (state.base) fit(); });

  /* ---- start, and poll for new repos every 5 minutes ---- */
  loadRepos(false).then(function () {
    var m = /repo=([^&]+)/.exec(location.hash);
    var want = m ? decodeURIComponent(m[1]) : OVERVIEW;
    var ok = want === OVERVIEW || state.repos.some(function (r) { return r.name === want; });
    select(ok ? want : OVERVIEW, false);
    saveSeen(state.repos.map(function (r) { return r.name; }));
  }).catch(function (e) {
    $('meta').textContent = '';
    var msg = esc(e.message);
    note(/key/i.test(msg) ? 'This page needs <code>?key=</code> in its address.' : 'Could not load repositories: ' + msg, true);
  });
  setInterval(function () { loadRepos(false).catch(function () { /* keep showing what we have */ }); }, 5 * 60 * 1000);
})();
</script>
</body>
</html>
"""