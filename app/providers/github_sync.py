"""GitHub -> Homepage repo sync (formerly the standalone github-repo-sync container).

Keeps the "Repo List - Deployed / Undeployed" groups in Homepage's services.yaml up to
date, on a schedule and instantly when a GitHub App webhook reports a repo change.
The actual sync is scripts/github_repos_to_homepage.py, unchanged, run as a subprocess.

Required env (the provider is skipped, with an error in the log, if any are missing)
  GITHUB_USER         GitHub username or org
  HOMEPAGE_CONFIG     path to Homepage's services.yaml inside this container, e.g. /config/services.yaml
  HOMEPAGE_GROUP      group name to write repos under, e.g. "Repo List"
  DOCKHAND_URL        Dockhand base URL (repos are split into Deployed / Undeployed)

Optional env
  GITHUB_TOKEN            GitHub PAT for the sync script. Strongly recommended: without one, only
                          public repos can be seen at all (see EXTRA_ARGS below), and requests are
                          capped at 60/hour. A classic PAT needs the 'repo' scope (or a fine-grained
                          PAT with Contents: Read-only) to include private repos.
  DOCKHAND_TOKEN          Dockhand API token
  SYNC_INTERVAL_SECS      seconds between scheduled syncs (default 3600)
  GITHUB_WEBHOOK_SECRET   shared secret of the GitHub App webhook (strongly recommended)
  EXTRA_ARGS              extra flags for the sync script (default "--show-stars --sort updated
                          --include-private"). --include-private is a no-op without GITHUB_TOKEN,
                          so both public and private repos are read whenever a token is set, and
                          only public repos otherwise. Every synced repo (private or public) is
                          written with its own colored visibility dot - see EXTRA_ARGS'
                          --hide-visibility to turn that off.
  STATS_FILE              stats JSON written by the script (default: repo-stats.json next to HOMEPAGE_CONFIG)
  DOCKHAND_REPOS_PATH, DOCKHAND_URL_ALIAS_MAP, DEPLOYED_SUFFIX, UNDEPLOYED_SUFFIX,
  DOCKHAND_STACKS_PATH, DOCKHAND_STACKS_QUERY, DOCKHAND_STACK_ALIAS_MAP,
  DOCKHAND_STATUS_FIELD, DOCKHAND_RUNNING_VALUES, DOCKHAND_RUNNING_STATES
                          read directly by the sync script. The stacks-related ones control down-stack
                          detection: a deployed repo is 🟢 unless Dockhand's live /api/stacks?env=1
                          shows one of its containers isn't 'running', in which case it's 🟡 - see the
                          script's own docstring for how repos are matched to stacks and the defaults.
  LEGEND_STYLE, LEGEND_SUFFIX, BOOKMARKS_FILE, LEGEND_BOOKMARK_GROUP, STALE_GROUPS
                          read directly by the sync script. LEGEND_STYLE defaults to 'bookmarks'
                          (small horizontal chips written to bookmarks.yaml, not the GitHub group
                          itself); pass STALE_GROUPS (comma-separated exact group names) once via
                          EXTRA_ARGS to clean up leftover groups from an earlier HOMEPAGE_GROUP value.

Endpoints (wired up in server.py)
  GET  /github/stats                {deployed, undeployed, total, updated_at} for a Homepage customapi widget
  POST /webhook  (or /github/webhook)   GitHub App webhook; triggers an immediate sync
"""
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

log = logging.getLogger("github-sync")
script_log = logging.getLogger("github-sync.script")

GITHUB_USER = os.environ.get("GITHUB_USER", "")
HOMEPAGE_CONFIG = os.environ.get("HOMEPAGE_CONFIG", "")
HOMEPAGE_GROUP = os.environ.get("HOMEPAGE_GROUP", "")
DOCKHAND_URL = os.environ.get("DOCKHAND_URL", "")
SYNC_INTERVAL_SECS = int(os.environ.get("SYNC_INTERVAL_SECS", "3600"))
EXTRA_ARGS = os.environ.get("EXTRA_ARGS", "--show-stars --sort updated --include-private").split()
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
STATS_FILE = os.environ.get("STATS_FILE") or (
    os.path.join(os.path.dirname(HOMEPAGE_CONFIG), "repo-stats.json") if HOMEPAGE_CONFIG else ""
)
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "github_repos_to_homepage.py")
MAX_BODY_BYTES = 1_000_000

# Repository events that mean "the repo list changed, re-sync"
REPO_LIST_CHANGING_ACTIONS = {
    "created", "deleted", "renamed", "transferred", "archived", "unarchived",
    "privatized", "publicized",
}

_wake = threading.Event()
_sync_lock = threading.Lock()
_wake_reason = "startup"


def missing_env():
    required = {"GITHUB_USER": GITHUB_USER, "HOMEPAGE_CONFIG": HOMEPAGE_CONFIG,
                "HOMEPAGE_GROUP": HOMEPAGE_GROUP, "DOCKHAND_URL": DOCKHAND_URL}
    return [k for k, v in required.items() if not v]


def describe():
    token_set = bool(os.environ.get("GITHUB_TOKEN"))
    visibility = "private+public (token set)" if token_set else "public only (no GITHUB_TOKEN)"
    return (f"user={GITHUB_USER} group='{HOMEPAGE_GROUP}' config={HOMEPAGE_CONFIG} "
            f"interval={SYNC_INTERVAL_SECS}s webhook_secret={'set' if WEBHOOK_SECRET else 'NOT SET'} "
            f"repos={visibility}")


def check_config_dir():
    """Warn early about the most common deployment mistake: not being able to write services.yaml."""
    cfg_dir = os.path.dirname(HOMEPAGE_CONFIG) or "."
    if not os.path.isdir(cfg_dir):
        log.error("config directory %s does not exist - mount Homepage's config folder there "
                  "(volumes: /volume1/docker/homepage:/config)", cfg_dir)
    elif not os.access(cfg_dir, os.W_OK):
        log.error("config directory %s is not writable by uid=%d gid=%d - run the container as "
                  "your Homepage user (compose: user: \"1026:100\")", cfg_dir, os.getuid(), os.getgid())


# ---- sync ------------------------------------------------------------------
_TS_PREFIX = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC\]\s*")  # script stamps its own UTC time


_in_traceback = False


def _relay(line):
    """Forward one line of the sync script's output through our logger (one consistent format/timezone).
    Python tracebacks from the script are logged at ERROR so a crash can't hide at INFO level."""
    global _in_traceback
    line = _TS_PREFIX.sub("", line.rstrip())
    if not line:
        return
    if line.startswith("Traceback (most recent call last)"):
        _in_traceback = True
        return script_log.error(line)
    if _in_traceback:
        if not line[0].isspace():  # the final "SomeError: message" line ends the traceback
            _in_traceback = False
        return script_log.error(line)
    if line.startswith("ERROR"):
        script_log.error(line)
    elif line.startswith("Warning"):
        script_log.warning(line)
    else:
        script_log.info(line)


def run_sync():
    if not _sync_lock.acquire(blocking=False):
        log.info("sync already in progress, skipping duplicate trigger")
        return
    started = time.monotonic()
    try:
        log.info("sync starting (trigger: %s)", _wake_reason)
        cmd = [sys.executable, SCRIPT, "--user", GITHUB_USER, "--config", HOMEPAGE_CONFIG,
               "--group", HOMEPAGE_GROUP, *EXTRA_ARGS]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        for line in proc.stdout:
            _relay(line)
        rc = proc.wait()
        elapsed = time.monotonic() - started
        if rc == 0:
            log.info("sync OK in %.1fs", elapsed)
        else:
            log.error("sync FAILED (exit %d, %.1fs) - see script output above", rc, elapsed)
    except Exception:
        log.exception("sync crashed")
    finally:
        _sync_lock.release()


def _sync_loop():
    global _wake_reason
    while True:
        run_sync()
        log.info("next sync in up to %ds (or sooner on a webhook)", SYNC_INTERVAL_SECS)
        woken = _wake.wait(timeout=SYNC_INTERVAL_SECS)
        _wake.clear()
        _wake_reason = "webhook" if woken else "schedule"


def start():
    threading.Thread(target=_sync_loop, daemon=True, name="github-sync").start()


# ---- HTTP handlers ---------------------------------------------------------
def get_stats():
    """Contents of the stats file; zeros until the first sync has completed."""
    try:
        with open(STATS_FILE, "rb") as f:
            return json.loads(f.read())
    except FileNotFoundError:
        return {"deployed": 0, "deployed_down": 0, "undeployed": 0, "total": 0, "updated_at": None}


def handle_webhook(headers, body):
    """Process a GitHub webhook delivery. Returns (http_status, response_text)."""
    if WEBHOOK_SECRET:
        signature = headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            log.warning("webhook: bad signature, rejecting request")
            return 401, "bad signature"
    else:
        log.warning("webhook: GITHUB_WEBHOOK_SECRET not set, accepting unverified request")

    event = headers.get("X-GitHub-Event", "")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        payload = {}

    if event == "ping":
        log.info("webhook: received GitHub ping - setup looks good")
        return 200, "pong"

    action = payload.get("action")
    if event == "repository" and action in REPO_LIST_CHANGING_ACTIONS:
        repo = payload.get("repository", {}).get("full_name", "unknown")
        log.info("webhook: repository '%s' %s - triggering immediate sync", repo, action)
        _wake.set()  # the sync loop runs it (and re-runs once more if a sync was already in progress)
        return 200, "sync triggered"

    log.info("webhook: ignoring event='%s' action='%s'", event, action)
    return 200, "ignored"