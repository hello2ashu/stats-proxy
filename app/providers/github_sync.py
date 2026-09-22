diff --git a/app/providers/github_sync.py b/app/providers/github_sync.py
index cdc719d..3276f29 100644
--- a/app/providers/github_sync.py
+++ b/app/providers/github_sync.py
@@ -11,14 +11,27 @@ Required env (the provider is skipped, with an error in the log, if any are miss
   DOCKHAND_URL        Dockhand base URL (repos are split into Deployed / Undeployed)
 
 Optional env
-  GITHUB_TOKEN            GitHub PAT for the sync script
+  GITHUB_TOKEN            GitHub PAT for the sync script. Strongly recommended: without one, only
+                          public repos can be seen at all (see EXTRA_ARGS below), and requests are
+                          capped at 60/hour. A classic PAT needs the 'repo' scope (or a fine-grained
+                          PAT with Contents: Read-only) to include private repos.
   DOCKHAND_TOKEN          Dockhand API token
   SYNC_INTERVAL_SECS      seconds between scheduled syncs (default 3600)
   GITHUB_WEBHOOK_SECRET   shared secret of the GitHub App webhook (strongly recommended)
-  EXTRA_ARGS              extra flags for the sync script (default "--show-stars --sort updated")
+  EXTRA_ARGS              extra flags for the sync script (default "--show-stars --sort updated
+                          --include-private"). --include-private is a no-op without GITHUB_TOKEN,
+                          so both public and private repos are read whenever a token is set, and
+                          only public repos otherwise. Every synced repo (private or public) is
+                          written with its own colored visibility dot - see EXTRA_ARGS'
+                          --hide-visibility to turn that off.
   STATS_FILE              stats JSON written by the script (default: repo-stats.json next to HOMEPAGE_CONFIG)
-  DOCKHAND_REPOS_PATH, DOCKHAND_URL_ALIAS_MAP, DEPLOYED_SUFFIX, UNDEPLOYED_SUFFIX
-                          read directly by the sync script
+  DOCKHAND_REPOS_PATH, DOCKHAND_URL_ALIAS_MAP, DEPLOYED_SUFFIX, UNDEPLOYED_SUFFIX,
+  DOCKHAND_STACKS_PATH, DOCKHAND_STACKS_QUERY, DOCKHAND_STACK_ALIAS_MAP,
+  DOCKHAND_STATUS_FIELD, DOCKHAND_RUNNING_VALUES, DOCKHAND_RUNNING_STATES
+                          read directly by the sync script. The stacks-related ones control down-stack
+                          detection: a deployed repo is 🟢 unless Dockhand's live /api/stacks?env=1
+                          shows one of its containers isn't 'running', in which case it's 🟡 - see the
+                          script's own docstring for how repos are matched to stacks and the defaults.
 
 Endpoints (wired up in server.py)
   GET  /github/stats                {deployed, undeployed, total, updated_at} for a Homepage customapi widget
@@ -43,7 +56,7 @@ HOMEPAGE_CONFIG = os.environ.get("HOMEPAGE_CONFIG", "")
 HOMEPAGE_GROUP = os.environ.get("HOMEPAGE_GROUP", "")
 DOCKHAND_URL = os.environ.get("DOCKHAND_URL", "")
 SYNC_INTERVAL_SECS = int(os.environ.get("SYNC_INTERVAL_SECS", "3600"))
-EXTRA_ARGS = os.environ.get("EXTRA_ARGS", "--show-stars --sort updated").split()
+EXTRA_ARGS = os.environ.get("EXTRA_ARGS", "--show-stars --sort updated --include-private").split()
 WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
 STATS_FILE = os.environ.get("STATS_FILE") or (
     os.path.join(os.path.dirname(HOMEPAGE_CONFIG), "repo-stats.json") if HOMEPAGE_CONFIG else ""
@@ -70,8 +83,11 @@ def missing_env():
 
 
 def describe():
+    token_set = bool(os.environ.get("GITHUB_TOKEN"))
+    visibility = "private+public (token set)" if token_set else "public only (no GITHUB_TOKEN)"
     return (f"user={GITHUB_USER} group='{HOMEPAGE_GROUP}' config={HOMEPAGE_CONFIG} "
-            f"interval={SYNC_INTERVAL_SECS}s webhook_secret={'set' if WEBHOOK_SECRET else 'NOT SET'}")
+            f"interval={SYNC_INTERVAL_SECS}s webhook_secret={'set' if WEBHOOK_SECRET else 'NOT SET'} "
+            f"repos={visibility}")
 
 
 def check_config_dir():
@@ -160,7 +176,7 @@ def get_stats():
         with open(STATS_FILE, "rb") as f:
             return json.loads(f.read())
     except FileNotFoundError:
-        return {"deployed": 0, "undeployed": 0, "total": 0, "updated_at": None}
+        return {"deployed": 0, "deployed_down": 0, "undeployed": 0, "total": 0, "updated_at": None}
 
 
 def handle_webhook(headers, body):
diff --git a/app/scripts/github_repos_to_homepage.py b/app/scripts/github_repos_to_homepage.py
index 9583eb1..69cc8ab 100644
--- a/app/scripts/github_repos_to_homepage.py
+++ b/app/scripts/github_repos_to_homepage.py
@@ -33,6 +33,35 @@ hit if the script runs on a cron and you also browse the API elsewhere.
 A token bumps that to 5,000/hour. A classic token with no scopes checked
 (public read-only) is enough for public repos; add `repo` scope if you
 want private repos included too (and pass --include-private).
+
+VISIBILITY (private vs. public)
+--------------------------------
+Every entry gets a small colored-dot prefix in its description showing
+whether the repo is private or public, in addition to the deployed status
+dot described below: 🟤 = private, 🔵 = public. This is separate from
+--include-private, which controls whether private repos are fetched from
+the API at all - once fetched, both private and public repos always show
+up with the right colored dot. Pass --hide-visibility to turn the dot off
+if you'd rather not surface repo visibility on the dashboard.
+
+DEPLOYED STACK HEALTH (green vs. yellow)
+-----------------------------------------
+A repo registered in Dockhand gets 🟢 by default, but if any container in
+its Dockhand stack isn't in the "running" state (created, exited,
+restarting, dead, ...), it gets 🟡 instead - "deployed" no longer means
+"actually up". An undeployed repo is still always 🔴, unaffected by any of
+this.
+
+This works by calling Dockhand's own stacks endpoint (default:
+--dockhand-url + /api/stacks?env=1) and matching each deployed repo to a
+stack by name - stack names are docker-compose project/folder names,
+which don't always match the repo name 1:1 (e.g. repo "bookorbit"
+deployed as stack "bookorbit-deploy"), so beyond an exact match this also
+tries a loose match against "-"/"_"-separated words in the stack name,
+then falls back to --dockhand-stack-alias-map for anything still
+unmatched. If a repo has no matching stack at all, or the stacks endpoint
+can't be reached, it's simply left green rather than guessed at - see
+--skip-stack-status to turn this feature off entirely.
 """
 
 import argparse
@@ -41,6 +70,7 @@ import os
 import re
 import sys
 from datetime import datetime, timezone
+from urllib.parse import parse_qsl
 
 import requests
 from ruamel.yaml import YAML
@@ -48,6 +78,45 @@ from ruamel.yaml.comments import CommentedMap, CommentedSeq
 
 API_ROOT = "https://api.github.com"
 
+# Colored-dot prefix for each repo's visibility, prepended to the
+# description alongside the deployed-status dot (see build_entry).
+# Deliberately a different color pair from any of the deployed-status dots
+# below so the two pieces of information never get confused at a glance.
+# Disable with --hide-visibility (or show_visibility=False) if you don't
+# want it.
+VISIBILITY_LABELS = {
+    True: "\U0001F7E4",   # 🟤 brown = private
+    False: "\U0001F535",  # 🔵 blue  = public
+}
+
+# Deployed-status dots. Undeployed is always red. A deployed repo is green
+# unless Dockhand's own data says its stack isn't currently running, in
+# which case it's yellow instead - see dockhand_stack_status().
+STATUS_DEPLOYED_RUNNING = "\U0001F7E2"  # 🟢 deployed, stack running (or status unknown - see above)
+STATUS_DEPLOYED_DOWN = "\U0001F7E1"     # 🟡 deployed, but Dockhand reports the stack isn't running
+STATUS_UNDEPLOYED = "\U0001F534"        # 🔴 not registered as a git-backed stack in Dockhand at all
+
+# Field names checked, in order, on each Dockhand /api/git/repositories
+# entry for its stack/container status - first one present wins. This is
+# the fallback layer used only if a repo has no match in /api/stacks (see
+# DOCKHAND_STACKS_PATH below) or if that lookup is disabled. Override via
+# --dockhand-status-field if your Dockhand build calls it something else.
+DOCKHAND_STATUS_FIELDS = ("status", "state", "stack_status", "container_status")
+
+# Status values (case-insensitive) that count as "running" for the fields
+# above. Anything else non-empty is treated as "down". Override via
+# --dockhand-running-values for a Dockhand build that phrases this
+# differently (e.g. "ok", "deployed").
+DEFAULT_DOCKHAND_RUNNING_VALUES = {"running", "up", "healthy", "active", "started", "ok"}
+
+# Per-container Docker states (the "state" field on each entry in a stack's
+# containerDetails, per /api/stacks - see fetch_dockhand_stacks) that count
+# as "running". Docker's other states - created, restarting, exited, dead -
+# are exactly what "any container status other than running" means, so a
+# stack counts as down the moment even one container isn't in one of these
+# states. Override via --dockhand-running-states if needed (unlikely).
+DEFAULT_DOCKHAND_RUNNING_STATES = {"running"}
+
 # Simple brand-color map for a few common languages so repo icons aren't
 # all identical. Falls back to plain GitHub icon when language is unknown
 # or not in this table. Extend as you like.
@@ -313,11 +382,13 @@ def normalize_repo_url(url):
 
 def fetch_dockhand_git_repo_urls(dockhand_url, token, repos_path, url_alias_map=None):
     """Fetch Dockhand's registered git repositories (/api/git/repositories)
-    and return the set of normalized source URLs - one per repo Dockhand
-    knows about as a git-backed stack, regardless of whether that stack is
-    currently running. `url_alias_map` lets a repo be matched via a URL
-    override (e.g. a renamed repo) instead of - or in addition to - its own
-    html_url; see --dockhand-url-alias-map."""
+    and return {normalized_source_url: raw_entry_dict} - one entry per repo
+    Dockhand knows about as a git-backed stack, regardless of whether that
+    stack is currently running. The raw entry is kept (not just the URL) so
+    callers can also read its container/stack status - see
+    dockhand_stack_status(). `url_alias_map` lets a repo be matched via a
+    URL override (e.g. a renamed repo) instead of - or in addition to - its
+    own html_url; see --dockhand-url-alias-map."""
     headers = {"Accept": "application/json"}
     if token:
         headers["Authorization"] = f"Bearer {token}"
@@ -350,11 +421,11 @@ def fetch_dockhand_git_repo_urls(dockhand_url, token, repos_path, url_alias_map=
         )
         sys.exit(1)
 
-    urls = set()
+    entries_by_url = {}
     skipped = 0
     for entry in entries:
         if isinstance(entry, dict) and isinstance(entry.get("url"), str) and entry["url"]:
-            urls.add(normalize_repo_url(entry["url"]))
+            entries_by_url[normalize_repo_url(entry["url"])] = entry
         else:
             skipped += 1
 
@@ -365,14 +436,173 @@ def fetch_dockhand_git_repo_urls(dockhand_url, token, repos_path, url_alias_map=
             file=sys.stderr,
         )
 
-    return urls
+    return entries_by_url
+
+
+def find_dockhand_entry(repo, dockhand_entries, url_alias_map=None):
+    """Return the raw Dockhand entry matching this repo (by its own GitHub
+    URL, or via --dockhand-url-alias-map), or None if it isn't deployed.
+    Mirrors repo_is_deployed's matching rules exactly, but also hands back
+    the entry itself so its status can be inspected."""
+    own = normalize_repo_url(repo["html_url"])
+    if own in dockhand_entries:
+        return dockhand_entries[own]
+    if url_alias_map:
+        alias = url_alias_map.get(repo["name"].lower())
+        if alias:
+            aliased = normalize_repo_url(alias)
+            if aliased in dockhand_entries:
+                return dockhand_entries[aliased]
+    return None
+
+
+def dockhand_stack_status(entry, status_fields=DOCKHAND_STATUS_FIELDS, running_values=None, running_states=None):
+    """Best-effort read of whether a Dockhand stack is currently "up" -
+    every one of its containers actually running, not just registered.
+
+    Returns True (running), False (confirmed down - at least one container
+    isn't in a "running" state), or None (nothing usable on this entry).
+    None matters just as much as False here: a repo must never get painted
+    "down" just because this script couldn't find a field it recognized -
+    see how main() only uses the yellow dot when this returns False, never
+    on None.
+
+    Checked in order, most precise first:
+      1. entry["containerDetails"][*]["state"] - the real per-container
+         Docker state (running/created/exited/restarting/dead/...), as
+         returned by Dockhand's /api/stacks?env=1. A stack is down the
+         moment any one container isn't "running" - see
+         DEFAULT_DOCKHAND_RUNNING_STATES.
+      2. A single status/state string field directly on the entry (one of
+         `status_fields`) - a fallback for entries that don't carry
+         containerDetails (e.g. if only /api/git/repositories happened to
+         expose a status field itself).
+      3. A `{"containers": {"running": N, "total": M}}`-style count
+         breakdown, for Dockhand builds that summarize differently.
+    """
+    if running_states is None:
+        running_states = DEFAULT_DOCKHAND_RUNNING_STATES
+    if running_values is None:
+        running_values = DEFAULT_DOCKHAND_RUNNING_VALUES
+    if not isinstance(entry, dict):
+        return None
+
+    details = entry.get("containerDetails")
+    if isinstance(details, list) and details:
+        states = [c.get("state") for c in details if isinstance(c, dict)]
+        if states and all(isinstance(s, str) and s.strip() for s in states):
+            return all(s.strip().lower() in running_states for s in states)
+
+    for field in status_fields:
+        val = entry.get(field)
+        if isinstance(val, str) and val.strip():
+            return val.strip().lower() in running_values
+        if isinstance(val, bool):
+            return val
+
+    containers = entry.get("containers")
+    if isinstance(containers, dict):
+        total = containers.get("total")
+        running = containers.get("running")
+        if isinstance(total, int) and isinstance(running, int) and total > 0:
+            return running >= total
+
+    return None
+
+
+def fetch_dockhand_stacks(dockhand_url, token, stacks_path, query, timeout=DOCKHAND_TIMEOUT):
+    """Fetch Dockhand's live stacks (default: /api/stacks?env=1) and return
+    {normalized_stack_name: raw_entry}, used purely to look up each
+    deployed repo's actual container status - see dockhand_stack_status()
+    and find_dockhand_stack(). Unlike fetch_dockhand_git_repo_urls, this is
+    an enhancement, not a hard requirement: on any failure this prints a
+    warning and returns {} so the sync still completes, just with every
+    deployed repo left at the default green dot."""
+    headers = {"Accept": "application/json"}
+    if token:
+        headers["Authorization"] = f"Bearer {token}"
+
+    url = dockhand_url.rstrip("/") + stacks_path
+    params = dict(parse_qsl(query)) if query else {}
+    try:
+        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
+        resp.raise_for_status()
+        payload = resp.json()
+    except requests.RequestException as e:
+        print(f"Warning: couldn't reach Dockhand's stacks endpoint at {url} ({e}) - "
+              f"deployed repos will all show the default green dot instead of reflecting "
+              f"their real container status. Pass --skip-stack-status to silence this.",
+              file=sys.stderr)
+        return {}
+    except ValueError:
+        print(f"Warning: Dockhand's stacks endpoint at {url} didn't return valid JSON - "
+              f"skipping down-stack detection for this run.", file=sys.stderr)
+        return {}
+
+    entries = payload
+    if isinstance(entries, dict):
+        for key in ("stacks", "data", "items", "results"):
+            if isinstance(entries.get(key), list):
+                entries = entries[key]
+                break
+
+    if not isinstance(entries, list):
+        print(f"Warning: unexpected response shape from {url} - expected a list of stacks. "
+              f"Got: {type(payload).__name__}. Skipping down-stack detection for this run.",
+              file=sys.stderr)
+        return {}
+
+    stacks_by_name = {}
+    skipped = 0
+    for entry in entries:
+        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"].strip():
+            stacks_by_name[entry["name"].strip().lower()] = entry
+        else:
+            skipped += 1
+    if skipped:
+        print(f"Note: {skipped} Dockhand stack entr{'y' if skipped == 1 else 'ies'} had no 'name' "
+              f"field and were skipped.", file=sys.stderr)
+
+    return stacks_by_name
+
+
+def find_dockhand_stack(repo, stacks_by_name, stack_alias_map=None):
+    """Match a repo to its Dockhand stack by name, for status lookup only
+    (deployed/undeployed itself is still decided by URL - see
+    repo_is_deployed). Dockhand stack names are docker-compose project
+    names (folder names), which often don't match the repo name 1:1 (e.g.
+    repo 'bookorbit' deployed as stack 'bookorbit-deploy', or two repos
+    sharing one stack like 'linkwarden-karakeep-deploy') - so beyond an
+    exact match, this also tries --dockhand-stack-alias-map, then a loose
+    match against '-'/'_'-separated words in the stack name. Returns the
+    raw stack entry, or None if nothing matched."""
+    name = repo["name"].strip().lower()
+
+    if name in stacks_by_name:
+        return stacks_by_name[name]
+
+    if stack_alias_map:
+        alias = stack_alias_map.get(name)
+        if alias:
+            alias = alias.strip().lower()
+            if alias in stacks_by_name:
+                return stacks_by_name[alias]
+
+    for stack_name in sorted(stacks_by_name):
+        words = re.split(r"[-_]+", stack_name)
+        if name in words:
+            return stacks_by_name[stack_name]
+
+    return None
 
 
 def repo_is_deployed(repo, deployed_urls, url_alias_map=None):
     """A repo counts as deployed if its own GitHub URL is registered in
     Dockhand, or - for repos whose Dockhand-registered URL differs from
     their current GitHub URL (e.g. a rename) - if its --dockhand-url-alias-map
-    override is."""
+    override is. `deployed_urls` may be a set of URLs or a
+    {url: entry} dict (e.g. from fetch_dockhand_git_repo_urls) - only key
+    membership is used."""
     if normalize_repo_url(repo["html_url"]) in deployed_urls:
         return True
     if url_alias_map:
@@ -456,13 +686,24 @@ def resolve_icon(repo_name, language, icon_overrides, skip_verification=False):
     return github_fallback_icon(language)
 
 
-def build_entry(repo, show_stars, icon_overrides=None, skip_icon_verification=False, status_label=None):
+def build_entry(repo, show_stars, icon_overrides=None, skip_icon_verification=False, status_label=None,
+                 show_visibility=True):
     description = repo.get("description") or "No description"
     if show_stars:
         stars = repo.get("stargazers_count", 0)
         description = f"{description} · \u2605 {stars}"
+
+    # Two independent colored-dot prefixes, so the two axes never get mixed
+    # up at a glance: deployment status (green/red, set by the caller via
+    # status_label) and repo visibility (purple/blue, set here from the
+    # GitHub API's own "private" field).
+    prefix_parts = []
     if status_label:
-        description = f"{status_label} {description}"
+        prefix_parts.append(status_label)
+    if show_visibility:
+        prefix_parts.append(VISIBILITY_LABELS[bool(repo.get("private"))])
+    if prefix_parts:
+        description = f"{' '.join(prefix_parts)} {description}"
 
     icon = resolve_icon(repo["name"], repo.get("language"), icon_overrides, skip_icon_verification)
 
@@ -568,6 +809,8 @@ def main():
     p.add_argument("--include-forks", action="store_true", help="Include forked repos (excluded by default)")
     p.add_argument("--include-archived", action="store_true", help="Include archived repos (excluded by default)")
     p.add_argument("--include-private", action="store_true", help="Include private repos (requires a token with 'repo' scope)")
+    p.add_argument("--hide-visibility", action="store_true",
+                    help="Don't prefix each repo's description with a private/public colored dot")
     p.add_argument("--show-stars", action="store_true", help="Append star count to each repo's description")
     p.add_argument("--show-total", action="store_true",
                     help="Add a summary entry at the top of the group showing the total repo count "
@@ -595,6 +838,35 @@ def main():
                     help="Path to a JSON or YAML file of {repo_name: git_url} overrides, for repos whose "
                          "current GitHub URL doesn't match what's registered in Dockhand (e.g. after a "
                          "repo rename). Repo name matching is case-insensitive.")
+    p.add_argument("--dockhand-status-field", default=os.environ.get("DOCKHAND_STATUS_FIELD"),
+                    help="Comma-separated field name(s), checked in order, that report a Dockhand "
+                         "git-repository entry's own status - fallback layer, only used if a repo has "
+                         "no match in --dockhand-stacks-path (default: "
+                         "status,state,stack_status,container_status).")
+    p.add_argument("--dockhand-running-values", default=os.environ.get("DOCKHAND_RUNNING_VALUES"),
+                    help="Comma-separated, case-insensitive values that count as 'running' for "
+                         "--dockhand-status-field (default: running,up,healthy,active,started,ok).")
+    p.add_argument("--dockhand-stacks-path", default=os.environ.get("DOCKHAND_STACKS_PATH", "/api/stacks"),
+                    help="API path appended to --dockhand-url for live stack/container status, used to "
+                         "give a deployed-but-down repo a yellow dot instead of green (default: "
+                         "/api/stacks). Each stack's containerDetails[].state is checked; a stack "
+                         "counts as down the moment any one container isn't 'running'.")
+    p.add_argument("--dockhand-stacks-query", default=os.environ.get("DOCKHAND_STACKS_QUERY", "env=1"),
+                    help="Query string appended to --dockhand-stacks-path (default: 'env=1')")
+    p.add_argument("--dockhand-stack-alias-map", default=os.environ.get("DOCKHAND_STACK_ALIAS_MAP"),
+                    help="Path to a JSON or YAML file of {repo_name: dockhand_stack_name} overrides, "
+                         "for repos whose name doesn't match their Dockhand stack's name (a "
+                         "docker-compose project/folder name, e.g. repo 'bookorbit' deployed as stack "
+                         "'bookorbit-deploy'). Repo name matching is case-insensitive. Exact and "
+                         "word-boundary matches (against '-'/'_' in the stack name) are tried first, "
+                         "so this is usually only needed for a genuinely unrelated stack name.")
+    p.add_argument("--dockhand-running-states", default=os.environ.get("DOCKHAND_RUNNING_STATES"),
+                    help="Comma-separated, case-insensitive per-container Docker states that count as "
+                         "'running' (default: running). Every container in a stack must be in one of "
+                         "these states for that stack to be shown as up.")
+    p.add_argument("--skip-stack-status", action="store_true",
+                    help="Don't query --dockhand-stacks-path at all - every deployed repo stays green "
+                         "regardless of its actual container status.")
     p.add_argument("--deployed-suffix", default=os.environ.get("DEPLOYED_SUFFIX", " - Deployed"),
                     help="Suffix appended to --group for the deployed-repos group (default: ' - Deployed')")
     p.add_argument("--undeployed-suffix", default=os.environ.get("UNDEPLOYED_SUFFIX", " - Undeployed"),
@@ -615,10 +887,16 @@ def main():
         sys.exit(1)
 
     if args.include_private and not args.token:
-        print("Warning: --include-private has no effect without --token", file=sys.stderr)
+        print(
+            "Warning: --include-private has no effect without --token (or GITHUB_TOKEN/GH_TOKEN env var) - "
+            "the GitHub API only returns public repos for unauthenticated requests. Use a token with the "
+            "'repo' scope (classic PAT) or Contents: Read-only (fine-grained PAT) to see private repos too.",
+            file=sys.stderr,
+        )
 
     icon_overrides = load_name_map(args.icon_map, "icon-map", "icon")
     url_alias_map = load_name_map(args.dockhand_url_alias_map, "dockhand-url-alias-map", "git_url")
+    stack_alias_map = load_name_map(args.dockhand_stack_alias_map, "dockhand-stack-alias-map", "stack_name")
 
     repos = fetch_all_repos(
         owner=args.user,
@@ -646,14 +924,14 @@ def main():
         )
         sys.exit(1)
 
-    deployed_urls = fetch_dockhand_git_repo_urls(args.dockhand_url, args.dockhand_token, args.dockhand_repos_path)
+    dockhand_entries = fetch_dockhand_git_repo_urls(args.dockhand_url, args.dockhand_token, args.dockhand_repos_path)
 
     deployed_repos, undeployed_repos = [], []
     for r in repos:
-        bucket = deployed_repos if repo_is_deployed(r, deployed_urls, url_alias_map) else undeployed_repos
+        bucket = deployed_repos if repo_is_deployed(r, dockhand_entries, url_alias_map) else undeployed_repos
         bucket.append(r)
 
-    unmatched = deployed_urls - {normalize_repo_url(r["html_url"]) for r in deployed_repos}
+    unmatched = set(dockhand_entries) - {normalize_repo_url(r["html_url"]) for r in deployed_repos}
     if unmatched:
         print(
             f"Note: {len(unmatched)} Dockhand git repositor{'y' if len(unmatched) == 1 else 'ies'} "
@@ -662,12 +940,51 @@ def main():
             file=sys.stderr,
         )
 
-    deployed_entries = [
-        build_entry(r, args.show_stars, icon_overrides, args.skip_icon_verification, status_label="\U0001F7E2")
-        for r in deployed_repos
-    ]
+    stacks_by_name = ({} if args.skip_stack_status else
+                       fetch_dockhand_stacks(args.dockhand_url, args.dockhand_token,
+                                             args.dockhand_stacks_path, args.dockhand_stacks_query))
+
+    status_fields = (tuple(f.strip() for f in args.dockhand_status_field.split(",") if f.strip())
+                      or DOCKHAND_STATUS_FIELDS) if args.dockhand_status_field else DOCKHAND_STATUS_FIELDS
+    running_values = ({v.strip().lower() for v in args.dockhand_running_values.split(",") if v.strip()}
+                       or DEFAULT_DOCKHAND_RUNNING_VALUES) if args.dockhand_running_values else DEFAULT_DOCKHAND_RUNNING_VALUES
+    running_states = ({v.strip().lower() for v in args.dockhand_running_states.split(",") if v.strip()}
+                       or DEFAULT_DOCKHAND_RUNNING_STATES) if args.dockhand_running_states else DEFAULT_DOCKHAND_RUNNING_STATES
+
+    show_visibility = not args.hide_visibility
+    deployed_entries = []
+    down_count = 0
+    unmatched_stacks = 0
+    for r in deployed_repos:
+        # Prefer the real, name-matched Dockhand stack (has per-container
+        # state - see fetch_dockhand_stacks); fall back to whatever the
+        # URL-matched git-repositories entry itself might expose.
+        stack_entry = find_dockhand_stack(r, stacks_by_name, stack_alias_map) if stacks_by_name else None
+        if stacks_by_name and stack_entry is None:
+            unmatched_stacks += 1
+        status = dockhand_stack_status(stack_entry, status_fields, running_values, running_states)
+        if status is None:
+            git_entry = find_dockhand_entry(r, dockhand_entries, url_alias_map)
+            status = dockhand_stack_status(git_entry, status_fields, running_values, running_states)
+        is_down = status is False
+        if is_down:
+            down_count += 1
+        deployed_entries.append(build_entry(
+            r, args.show_stars, icon_overrides, args.skip_icon_verification,
+            status_label=STATUS_DEPLOYED_DOWN if is_down else STATUS_DEPLOYED_RUNNING,
+            show_visibility=show_visibility,
+        ))
+    if down_count:
+        print(f"Note: {down_count} deployed repo(s) have at least one container not running - "
+              f"marked with a yellow dot instead of green.", file=sys.stderr)
+    if unmatched_stacks:
+        print(f"Note: {unmatched_stacks} deployed repo(s) had no matching Dockhand stack in "
+              f"{args.dockhand_stacks_path} - left as the default green dot. If one of these is "
+              f"actually down, add it to --dockhand-stack-alias-map.", file=sys.stderr)
+
     undeployed_entries = [
-        build_entry(r, args.show_stars, icon_overrides, args.skip_icon_verification, status_label="\U0001F534")
+        build_entry(r, args.show_stars, icon_overrides, args.skip_icon_verification,
+                    status_label=STATUS_UNDEPLOYED, show_visibility=show_visibility)
         for r in undeployed_repos
     ]
 
@@ -730,6 +1047,7 @@ def main():
     stats_path = args.stats_file or os.path.join(config_dir, "repo-stats.json")
     stats = {
         "deployed": len(deployed_repos),
+        "deployed_down": down_count,
         "undeployed": len(undeployed_repos),
         "total": total_count,
         "updated_at": ts,
