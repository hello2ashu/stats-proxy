#!/usr/bin/env python3
"""
github_repos_to_homepage.py

Fetches a GitHub user's (or org's) repositories and writes them into a
gethomepage.dev `services.yaml` file as a group of bookmarks, one entry
per repo. Designed to be run on a schedule (cron) so the dashboard stays
in sync with your GitHub account automatically.

It only touches the ONE group you point it at (--group). Every other
group in services.yaml is left completely untouched, formatting and
comments included (it uses ruamel.yaml round-trip mode).

USAGE
-----
    python3 github_repos_to_homepage.py \\
        --user hello2ashu \\
        --config /path/to/services.yaml \\
        --group "GitHub Repos" \\
        --token "$GITHUB_TOKEN" \\
        --show-total

    # dry run - print the YAML that WOULD be written, don't touch the file
    python3 github_repos_to_homepage.py --user hello2ashu --dry-run

FIRST TIME SETUP
----------------
    pip install requests ruamel.yaml

A GitHub token is optional for public repos but strongly recommended:
unauthenticated requests are capped at 60/hour per IP, which is easy to
hit if the script runs on a cron and you also browse the API elsewhere.
A token bumps that to 5,000/hour. A classic token with no scopes checked
(public read-only) is enough for public repos; add `repo` scope if you
want private repos included too (and pass --include-private).

VISIBILITY (private vs. public)
--------------------------------
Every entry gets a small colored-dot prefix in its description showing
whether the repo is private or public, in addition to the deployed status
dot described below: 🟤 = private, 🔵 = public. This is separate from
--include-private, which controls whether private repos are fetched from
the API at all - once fetched, both private and public repos always show
up with the right colored dot. Pass --hide-visibility to turn the dot off
if you'd rather not surface repo visibility on the dashboard.

DEPLOYED STACK HEALTH (green vs. yellow)
-----------------------------------------
A repo registered in Dockhand gets 🟢 by default, but if any container in
its Dockhand stack isn't in the "running" state (created, exited,
restarting, dead, ...), it gets 🟡 instead - "deployed" no longer means
"actually up". An undeployed repo is still always 🔴, unaffected by any of
this.

This works by calling Dockhand's own stacks endpoint (default:
--dockhand-url + /api/stacks?env=1) and matching each deployed repo to a
stack by name - stack names are docker-compose project/folder names,
which don't always match the repo name 1:1 (e.g. repo "bookorbit"
deployed as stack "bookorbit-deploy"), so beyond an exact match this also
tries a loose match against "-"/"_"-separated words in the stack name,
then falls back to --dockhand-stack-alias-map for anything still
unmatched. If a repo has no matching stack at all, or the stacks endpoint
can't be reached, it's simply left green rather than guessed at - see
--skip-stack-status to turn this feature off entirely.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import requests
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

API_ROOT = "https://api.github.com"

# Colored-dot prefix for each repo's visibility, prepended to the
# description alongside the deployed-status dot (see build_entry).
# Deliberately a different color pair from any of the deployed-status dots
# below so the two pieces of information never get confused at a glance.
# Disable with --hide-visibility (or show_visibility=False) if you don't
# want it.
VISIBILITY_LABELS = {
    True: "\U0001F7E4",   # 🟤 brown = private
    False: "\U0001F535",  # 🔵 blue  = public
}

# Deployed-status dots. Undeployed is always red. A deployed repo is green
# unless Dockhand's own data says its stack isn't currently running, in
# which case it's yellow instead - see dockhand_stack_status().
STATUS_DEPLOYED_RUNNING = "\U0001F7E2"  # 🟢 deployed, stack running (or status unknown - see above)
STATUS_DEPLOYED_DOWN = "\U0001F7E1"     # 🟡 deployed, but Dockhand reports the stack isn't running
STATUS_UNDEPLOYED = "\U0001F534"        # 🔴 not registered as a git-backed stack in Dockhand at all

# Field names checked, in order, on each Dockhand /api/git/repositories
# entry for its stack/container status - first one present wins. This is
# the fallback layer used only if a repo has no match in /api/stacks (see
# DOCKHAND_STACKS_PATH below) or if that lookup is disabled. Override via
# --dockhand-status-field if your Dockhand build calls it something else.
DOCKHAND_STATUS_FIELDS = ("status", "state", "stack_status", "container_status")

# Status values (case-insensitive) that count as "running" for the fields
# above. Anything else non-empty is treated as "down". Override via
# --dockhand-running-values for a Dockhand build that phrases this
# differently (e.g. "ok", "deployed").
DEFAULT_DOCKHAND_RUNNING_VALUES = {"running", "up", "healthy", "active", "started", "ok"}

# Per-container Docker states (the "state" field on each entry in a stack's
# containerDetails, per /api/stacks - see fetch_dockhand_stacks) that count
# as "running". Docker's other states - created, restarting, exited, dead -
# are exactly what "any container status other than running" means, so a
# stack counts as down the moment even one container isn't in one of these
# states. Override via --dockhand-running-states if needed (unlikely).
DEFAULT_DOCKHAND_RUNNING_STATES = {"running"}

# Simple brand-color map for a few common languages so repo icons aren't
# all identical. Falls back to plain GitHub icon when language is unknown
# or not in this table. Extend as you like.
LANGUAGE_COLORS = {
    "Python": "3776AB",
    "JavaScript": "F7DF1E",
    "TypeScript": "3178C6",
    "Go": "00ADD8",
    "Rust": "000000",
    "Java": "007396",
    "C++": "00599C",
    "C": "A8B9CC",
    "C#": "239120",
    "Shell": "89E051",
    "HTML": "E34F26",
    "CSS": "1572B6",
    "PHP": "777BB4",
    "Ruby": "CC342D",
    "Swift": "F05138",
    "Kotlin": "7F52FF",
}

# Repo names that correspond to actual self-hosted apps get that app's own
# icon (matching homepage's bundled icon set / dashboard-icons naming, i.e.
# just "<name>.png") instead of a plain GitHub icon - much more useful at a
# glance since you likely already recognize these icons from elsewhere on
# your dashboard. Repo name matching is case-insensitive. Override or add
# to this via --icon-map for anything not covered here.
#
# NOTE: this is a static guess at what exists in the Dashboard Icons
# project. Names there do change (renames, removals, new additions), so
# every candidate pulled from this map (or from --icon-map) is verified
# against the live CDN before use - see verify_or_fallback() below. If
# verification fails for any reason, it falls back to the colored GitHub
# icon rather than risking a broken image on the dashboard.
KNOWN_APP_ICONS = {
    "dawarich": "dawarich.png",
    "karakeep": "karakeep.png",
    "linkwarden": "linkwarden.png",
    "bookorbit": "bookorbit.png",
    "airtrail": "air-trail.png",  # Dashboard Icons uses the hyphenated slug, not "airtrail"
    "trek": "trek.png",  # not yet in Dashboard Icons as of writing - verification will fall back until it's added
    "trillium": "trilium.png",
    "ntop": "ntopng.png",
    "ntopng": "ntopng.png",
    "qbit": "qbittorrent.png",
    "qbittorrent": "qbittorrent.png",
    "bentopdf": "bentopdf.png",
    "convertx": "convertx.png",
    "synology": "synology.png",
    "dockhand": "dockhand.png",
    "syncthing": "syncthing.png",
}

# gethomepage.dev resolves a bare icon name against Dashboard Icons, and
# recognizes three prefixed icon libraries on top of that - see
# https://gethomepage.dev/configs/services/#icons. Each has its own CDN and
# its own naming convention, so each gets its own existence check below
# rather than being trusted blindly. A candidate that isn't in any of
# these forms (a full URL, or a local /icons/... path) can't be verified
# from here and is used as-is.
DASHBOARD_ICON_CDN_BASES = [
    "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons",  # current
    "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons",  # legacy mirror, in case of a lookup lag
]
MDI_ICON_URL = "https://cdn.jsdelivr.net/npm/@mdi/svg@latest/svg/{name}.svg"
SIMPLE_ICONS_URL = "https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/{name}.svg"
SELFHST_ICON_CDN_BASE = "https://cdn.jsdelivr.net/gh/selfhst/icons"

ICON_CHECK_TIMEOUT = 5
DASHBOARD_ICON_EXTS = ("png", "svg", "webp")  # gethomepage.dev defaults to png when none is given
SELFHST_ICON_EXTS = ("png", "svg", "webp")  # selfh.st also defaults to png when none is given

# mdi-XX and si-XX accept a "-#hexcolor" color-override suffix; strip it
# before checking the icon name itself against its source library.
_COLOR_SUFFIX_RE = re.compile(r"-#[0-9a-fA-F]{3,8}$")

_icon_exists_cache = {}
_icon_session = requests.Session()

# Running tally so main() can report, at the end of a run, how many icons
# were actually swapped for the fallback vs. how many merely couldn't be
# checked - the two have very different implications and were previously
# indistinguishable (see _check_url's docstring for why that mattered).
icon_verification_stats = {"confirmed_missing": 0, "unverifiable": 0}


def _check_url(url):
    """Returns True (confirmed 200), False (confirmed 404), or None
    (inconclusive: timeout, connection error, or any other status code
    such as a 403/429 from an over-eager CDN).

    The None case matters: this check runs on whatever machine generates
    the YAML (often a NAS or a locked-down home server on a cron job),
    which may have very different outbound network access than the
    browser that actually renders the dashboard. Collapsing "couldn't
    reach the CDN from here" into "the icon doesn't exist" was the bug
    that made perfectly good icons disappear in restricted environments -
    see verify_or_fallback, which now only swaps an icon out for the
    fallback on a real, confirmed 404.
    """
    try:
        resp = _icon_session.head(url, timeout=ICON_CHECK_TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    return None


def _cached(cache_key, check_fn):
    if cache_key in _icon_exists_cache:
        return _icon_exists_cache[cache_key]
    result = check_fn()
    _icon_exists_cache[cache_key] = result
    return result


def _split_ext(name, valid_exts, default_ext):
    """Split a trailing '.ext' off `name` if it's one of `valid_exts`;
    otherwise return `name` unchanged along with `default_ext` (matching
    what the dashboard would actually fetch when no extension is given)."""
    lower = name.lower()
    for ext in valid_exts:
        suffix = "." + ext
        if lower.endswith(suffix):
            return name[: -len(suffix)], ext
    return name, default_ext


def _combine(results):
    """Combine several tri-state checks (e.g. two CDN mirrors) into one:
    any confirmed hit wins; only call it missing if every check came back
    a confirmed 404; otherwise it's inconclusive."""
    if True in results:
        return True
    if results and all(r is False for r in results):
        return False
    return None


def dashboard_icon_status(candidate):
    bare_name, ext = _split_ext(candidate, DASHBOARD_ICON_EXTS, "png")
    check = lambda: _combine([_check_url(f"{base}/{ext}/{bare_name}.{ext}") for base in DASHBOARD_ICON_CDN_BASES])
    return _cached(("dashboard", bare_name, ext), check)


def mdi_icon_status(name):
    name = _COLOR_SUFFIX_RE.sub("", name)
    return _cached(("mdi", name), lambda: _check_url(MDI_ICON_URL.format(name=name)))


def simple_icon_status(name):
    name = _COLOR_SUFFIX_RE.sub("", name)
    return _cached(("si", name), lambda: _check_url(SIMPLE_ICONS_URL.format(name=name)))


def selfhst_icon_status(candidate):
    bare_name, ext = _split_ext(candidate, SELFHST_ICON_EXTS, "png")
    return _cached(("sh", bare_name, ext), lambda: _check_url(f"{SELFHST_ICON_CDN_BASE}/{ext}/{bare_name}.{ext}"))


def github_fallback_icon(language):
    """The always-valid fallback: GitHub's own simple-icons logo, tinted by
    the repo's primary language so repos are still visually distinguishable
    even without an app-specific icon."""
    color = LANGUAGE_COLORS.get(language, "181717")  # 181717 = GitHub's own black
    return f"si-github-#{color}"


def _log_confirmed_missing(candidate, source, language):
    icon_verification_stats["confirmed_missing"] += 1
    print(f"Icon '{candidate}' not found in {source} - using the GitHub icon instead.", file=sys.stderr)
    return github_fallback_icon(language)


def _log_unverifiable(candidate, source):
    icon_verification_stats["unverifiable"] += 1
    print(f"Warning: couldn't verify icon '{candidate}' against {source} (network issue?) - using it as configured.",
          file=sys.stderr)
    return candidate


def verify_or_fallback(candidate, language, skip_verification=False):
    if skip_verification or candidate.startswith(("http://", "https://", "/icons/")):
        return candidate  # trusted as configured: verification off, or it's a URL/local icon we can't check

    # An explicit mdi-/si-/sh- pick is a deliberate choice of library, not a
    # guess - it's checked in place and either kept or replaced with the
    # generic icon, but never silently moved to a *different* library.
    if candidate.startswith("mdi-"):
        status = mdi_icon_status(candidate[len("mdi-"):])
        source = "Material Design Icons"
    elif candidate.startswith("si-"):
        status = simple_icon_status(candidate[len("si-"):])
        source = "Simple Icons"
    elif candidate.startswith("sh-"):
        status = selfhst_icon_status(candidate[len("sh-"):])
        source = "selfh.st/icons"
    else:
        # A bare name (from KNOWN_APP_ICONS or --icon-map) is just a guess
        # at the Dashboard Icons filename, so a miss there doesn't mean the
        # app has no icon anywhere - keep searching the other libraries for
        # the same name before giving up.
        return search_icon_libraries(candidate, language)

    if status is True:
        return candidate
    if status is False:
        return _log_confirmed_missing(candidate, source, language)
    return _log_unverifiable(candidate, source)  # status is None: inconclusive, keep as configured


def search_icon_libraries(candidate, language):
    """Look for `candidate`'s app across Dashboard Icons, then Material
    Design Icons, Simple Icons, and selfh.st/icons in turn, stopping at the
    first confirmed match. A confirmed miss moves on to the next library; an
    inconclusive result (network issue) stops the search and keeps the
    originally configured candidate rather than guessing further."""
    bare_name, _ext = _split_ext(candidate, DASHBOARD_ICON_EXTS, "png")

    library_chain = [
        ("Dashboard Icons", lambda: dashboard_icon_status(candidate), lambda: candidate),
        ("Material Design Icons", lambda: mdi_icon_status(bare_name), lambda: f"mdi-{bare_name}"),
        ("Simple Icons", lambda: simple_icon_status(bare_name), lambda: f"si-{bare_name}"),
        ("selfh.st/icons", lambda: selfhst_icon_status(bare_name), lambda: f"sh-{bare_name}"),
    ]

    tried_sources = []
    for source, get_status, get_result in library_chain:
        status = get_status()
        if status is True:
            return get_result()
        if status is None:
            return _log_unverifiable(candidate, source)
        tried_sources.append(source)

    icon_verification_stats["confirmed_missing"] += 1
    print(f"Icon '{candidate}' not found in any of {', '.join(tried_sources)} - using the GitHub icon instead.",
          file=sys.stderr)
    return github_fallback_icon(language)


DOCKHAND_TIMEOUT = 10


def normalize_repo_url(url):
    """Canonicalize a git URL for comparison: strip protocol, 'www.',
    trailing slash, and a trailing '.git' suffix, and lowercase - so
    'https://github.com/x/Y.git' and 'github.com/x/y/' compare equal."""
    if not url:
        return None
    u = url.strip().lower()
    u = re.sub(r"^git@([^:]+):", r"\1/", u)
    u = re.sub(r"^https?://(www\.)?", "", u)
    u = u.rstrip("/")
    if u.endswith(".git"):
        u = u[: -len(".git")]
    return u


def fetch_dockhand_git_repo_urls(dockhand_url, token, repos_path, url_alias_map=None):
    """Fetch Dockhand's registered git repositories (/api/git/repositories)
    and return {normalized_source_url: raw_entry_dict} - one entry per repo
    Dockhand knows about as a git-backed stack, regardless of whether that
    stack is currently running. The raw entry is kept (not just the URL) so
    callers can also read its container/stack status - see
    dockhand_stack_status(). `url_alias_map` lets a repo be matched via a
    URL override (e.g. a renamed repo) instead of - or in addition to - its
    own html_url; see --dockhand-url-alias-map."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = dockhand_url.rstrip("/") + repos_path
    try:
        resp = requests.get(url, headers=headers, timeout=DOCKHAND_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as e:
        print(f"ERROR: couldn't reach Dockhand at {url}: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError:
        print(f"ERROR: Dockhand response from {url} wasn't valid JSON.", file=sys.stderr)
        sys.exit(1)

    entries = payload
    if isinstance(entries, dict):
        # some APIs wrap the list, e.g. {"repositories": [...]} or {"data": [...]}
        for key in ("repositories", "data", "items", "results"):
            if isinstance(entries.get(key), list):
                entries = entries[key]
                break

    if not isinstance(entries, list):
        print(
            f"ERROR: unexpected response shape from {url} - expected a list of git repositories. "
            f"Got: {type(payload).__name__}.",
            file=sys.stderr,
        )
        sys.exit(1)

    entries_by_url = {}
    skipped = 0
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("url"), str) and entry["url"]:
            entries_by_url[normalize_repo_url(entry["url"])] = entry
        else:
            skipped += 1

    if skipped:
        print(
            f"Note: {skipped} Dockhand git-repository entr{'y' if skipped == 1 else 'ies'} had no "
            f"'url' field and were skipped.",
            file=sys.stderr,
        )

    return entries_by_url


def find_dockhand_entry(repo, dockhand_entries, url_alias_map=None):
    """Return the raw Dockhand entry matching this repo (by its own GitHub
    URL, or via --dockhand-url-alias-map), or None if it isn't deployed.
    Mirrors repo_is_deployed's matching rules exactly, but also hands back
    the entry itself so its status can be inspected."""
    own = normalize_repo_url(repo["html_url"])
    if own in dockhand_entries:
        return dockhand_entries[own]
    if url_alias_map:
        alias = url_alias_map.get(repo["name"].lower())
        if alias:
            aliased = normalize_repo_url(alias)
            if aliased in dockhand_entries:
                return dockhand_entries[aliased]
    return None


def dockhand_stack_status(entry, status_fields=DOCKHAND_STATUS_FIELDS, running_values=None, running_states=None):
    """Best-effort read of whether a Dockhand stack is currently "up" -
    every one of its containers actually running, not just registered.

    Returns True (running), False (confirmed down - at least one container
    isn't in a "running" state), or None (nothing usable on this entry).
    None matters just as much as False here: a repo must never get painted
    "down" just because this script couldn't find a field it recognized -
    see how main() only uses the yellow dot when this returns False, never
    on None.

    Checked in order, most precise first:
      1. entry["containerDetails"][*]["state"] - the real per-container
         Docker state (running/created/exited/restarting/dead/...), as
         returned by Dockhand's /api/stacks?env=1. A stack is down the
         moment any one container isn't "running" - see
         DEFAULT_DOCKHAND_RUNNING_STATES.
      2. A single status/state string field directly on the entry (one of
         `status_fields`) - a fallback for entries that don't carry
         containerDetails (e.g. if only /api/git/repositories happened to
         expose a status field itself).
      3. A `{"containers": {"running": N, "total": M}}`-style count
         breakdown, for Dockhand builds that summarize differently.
    """
    if running_states is None:
        running_states = DEFAULT_DOCKHAND_RUNNING_STATES
    if running_values is None:
        running_values = DEFAULT_DOCKHAND_RUNNING_VALUES
    if not isinstance(entry, dict):
        return None

    details = entry.get("containerDetails")
    if isinstance(details, list) and details:
        states = [c.get("state") for c in details if isinstance(c, dict)]
        if states and all(isinstance(s, str) and s.strip() for s in states):
            return all(s.strip().lower() in running_states for s in states)

    for field in status_fields:
        val = entry.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip().lower() in running_values
        if isinstance(val, bool):
            return val

    containers = entry.get("containers")
    if isinstance(containers, dict):
        total = containers.get("total")
        running = containers.get("running")
        if isinstance(total, int) and isinstance(running, int) and total > 0:
            return running >= total

    return None


def fetch_dockhand_stacks(dockhand_url, token, stacks_path, query, timeout=DOCKHAND_TIMEOUT):
    """Fetch Dockhand's live stacks (default: /api/stacks?env=1) and return
    {normalized_stack_name: raw_entry}, used purely to look up each
    deployed repo's actual container status - see dockhand_stack_status()
    and find_dockhand_stack(). Unlike fetch_dockhand_git_repo_urls, this is
    an enhancement, not a hard requirement: on any failure this prints a
    warning and returns {} so the sync still completes, just with every
    deployed repo left at the default green dot."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = dockhand_url.rstrip("/") + stacks_path
    params = dict(parse_qsl(query)) if query else {}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as e:
        print(f"Warning: couldn't reach Dockhand's stacks endpoint at {url} ({e}) - "
              f"deployed repos will all show the default green dot instead of reflecting "
              f"their real container status. Pass --skip-stack-status to silence this.",
              file=sys.stderr)
        return {}
    except ValueError:
        print(f"Warning: Dockhand's stacks endpoint at {url} didn't return valid JSON - "
              f"skipping down-stack detection for this run.", file=sys.stderr)
        return {}

    entries = payload
    if isinstance(entries, dict):
        for key in ("stacks", "data", "items", "results"):
            if isinstance(entries.get(key), list):
                entries = entries[key]
                break

    if not isinstance(entries, list):
        print(f"Warning: unexpected response shape from {url} - expected a list of stacks. "
              f"Got: {type(payload).__name__}. Skipping down-stack detection for this run.",
              file=sys.stderr)
        return {}

    stacks_by_name = {}
    skipped = 0
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"].strip():
            stacks_by_name[entry["name"].strip().lower()] = entry
        else:
            skipped += 1
    if skipped:
        print(f"Note: {skipped} Dockhand stack entr{'y' if skipped == 1 else 'ies'} had no 'name' "
              f"field and were skipped.", file=sys.stderr)

    return stacks_by_name


def find_dockhand_stack(repo, stacks_by_name, stack_alias_map=None):
    """Match a repo to its Dockhand stack by name, for status lookup only
    (deployed/undeployed itself is still decided by URL - see
    repo_is_deployed). Dockhand stack names are docker-compose project
    names (folder names), which often don't match the repo name 1:1 (e.g.
    repo 'bookorbit' deployed as stack 'bookorbit-deploy', or two repos
    sharing one stack like 'linkwarden-karakeep-deploy') - so beyond an
    exact match, this also tries --dockhand-stack-alias-map, then a loose
    match against '-'/'_'-separated words in the stack name. Returns the
    raw stack entry, or None if nothing matched."""
    name = repo["name"].strip().lower()

    if name in stacks_by_name:
        return stacks_by_name[name]

    if stack_alias_map:
        alias = stack_alias_map.get(name)
        if alias:
            alias = alias.strip().lower()
            if alias in stacks_by_name:
                return stacks_by_name[alias]

    for stack_name in sorted(stacks_by_name):
        words = re.split(r"[-_]+", stack_name)
        if name in words:
            return stacks_by_name[stack_name]

    return None


def repo_is_deployed(repo, deployed_urls, url_alias_map=None):
    """A repo counts as deployed if its own GitHub URL is registered in
    Dockhand, or - for repos whose Dockhand-registered URL differs from
    their current GitHub URL (e.g. a rename) - if its --dockhand-url-alias-map
    override is. `deployed_urls` may be a set of URLs or a
    {url: entry} dict (e.g. from fetch_dockhand_git_repo_urls) - only key
    membership is used."""
    if normalize_repo_url(repo["html_url"]) in deployed_urls:
        return True
    if url_alias_map:
        alias = url_alias_map.get(repo["name"].lower())
        if alias and normalize_repo_url(alias) in deployed_urls:
            return True
    return False



def fetch_all_repos(owner, token, is_org, include_forks, include_archived, include_private):
    """Paginate through the GitHub API and return the full repo list."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if is_org:
        base_url = f"{API_ROOT}/orgs/{owner}/repos"
        params = {"per_page": 100, "type": "all" if include_private else "public"}
    else:
        if token:
            # /user/repos is the authenticated user and can include private
            # repos if include_private is set; /users/{owner}/repos is
            # public-only regardless of token.
            base_url = f"{API_ROOT}/user/repos" if include_private else f"{API_ROOT}/users/{owner}/repos"
            params = {"per_page": 100, "affiliation": "owner"} if include_private else {"per_page": 100}
        else:
            base_url = f"{API_ROOT}/users/{owner}/repos"
            params = {"per_page": 100}

    repos = []
    page = 1
    while True:
        params["page"] = page
        resp = requests.get(base_url, headers=headers, params=params, timeout=30)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            print("GitHub API rate limit hit. Pass --token to raise the limit.", file=sys.stderr)
            sys.exit(1)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    if not include_forks:
        repos = [r for r in repos if not r.get("fork")]
    if not include_archived:
        repos = [r for r in repos if not r.get("archived")]
    if not include_private:
        repos = [r for r in repos if not r.get("private")]

    return repos


def sort_repos(repos, sort_by):
    if sort_by == "stars":
        repos.sort(key=lambda r: r.get("stargazers_count", 0), reverse=True)
    elif sort_by == "name":
        repos.sort(key=lambda r: r["name"].lower())
    else:  # updated (default) - GitHub already returns most-recent-first for /repos,
        repos.sort(key=lambda r: r.get("pushed_at") or "", reverse=True)
    return repos


def resolve_icon(repo_name, language, icon_overrides, skip_verification=False):
    """Icon resolution order: user's --icon-map override > known self-hosted
    app icon (matched by repo name) > language-colored GitHub icon. Any
    candidate pulled from the first two is verified against its source's
    live CDN before use (see verify_or_fallback)."""
    name_lower = repo_name.lower()

    if icon_overrides and name_lower in icon_overrides:
        return verify_or_fallback(icon_overrides[name_lower], language, skip_verification)

    if name_lower in KNOWN_APP_ICONS:
        return verify_or_fallback(KNOWN_APP_ICONS[name_lower], language, skip_verification)

    return github_fallback_icon(language)


def build_entry(repo, show_stars, icon_overrides=None, skip_icon_verification=False, status_label=None,
                 show_visibility=True):
    description = repo.get("description") or "No description"
    if show_stars:
        stars = repo.get("stargazers_count", 0)
        description = f"{description} · \u2605 {stars}"

    # Two independent colored-dot prefixes, so the two axes never get mixed
    # up at a glance: deployment status (green/red, set by the caller via
    # status_label) and repo visibility (purple/blue, set here from the
    # GitHub API's own "private" field).
    prefix_parts = []
    if status_label:
        prefix_parts.append(status_label)
    if show_visibility:
        prefix_parts.append(VISIBILITY_LABELS[bool(repo.get("private"))])
    if prefix_parts:
        description = f"{' '.join(prefix_parts)} {description}"

    icon = resolve_icon(repo["name"], repo.get("language"), icon_overrides, skip_icon_verification)

    attrs = CommentedMap()
    attrs["href"] = repo["html_url"]
    attrs["description"] = description
    attrs["icon"] = icon
    return {repo["name"]: attrs}


def build_total_entry(user, total_count, shown_count):
    """A non-clickable-in-spirit summary entry placed at the top of the
    group so the total repo count is visible on the dashboard at a glance."""
    if shown_count < total_count:
        label = f"\U0001F4E6 Showing {shown_count} of {total_count} Repos"
    else:
        label = f"\U0001F4E6 {total_count} Repos"

    attrs = CommentedMap()
    attrs["href"] = f"https://github.com/{user}"
    attrs["description"] = "Total repositories synced from GitHub"
    attrs["icon"] = "si-github-#181717"
    return {label: attrs}


def build_legend_entries():
    """Static reference entries explaining every colored dot used across
    the Deployed/Undeployed groups, as full-size services-group entries -
    used only when --legend-style=services. See build_legend_bookmarks for
    the default, compact style."""
    def entry(label, description):
        attrs = CommentedMap()
        attrs["href"] = "#"
        attrs["description"] = description
        return {label: attrs}

    return [
        entry(f"{STATUS_DEPLOYED_RUNNING} Deployed & running",
              "In Dockhand, every container in the stack is running"),
        entry(f"{STATUS_DEPLOYED_DOWN} Deployed but down",
              "In Dockhand, but at least one container in the stack isn't running"),
        entry(f"{STATUS_UNDEPLOYED} Not deployed",
              "No matching Dockhand stack was found for this repo"),
        entry(f"{VISIBILITY_LABELS[True]} Private repo", "Visible only to you / your org on GitHub"),
        entry(f"{VISIBILITY_LABELS[False]} Public repo", "Visible to anyone on GitHub"),
    ]


def build_legend_bookmarks():
    """The same five legend explanations as build_legend_entries, but
    shaped for bookmarks.yaml instead of services.yaml (Homepage renders
    bookmarks as small horizontal chips at the top of the page, rather
    than full-size cards) - this is the --legend-style=bookmarks default.
    Each chip's `abbr` is the colored dot itself, so the color is what
    catches the eye; the name/description carry the explanation."""
    def entry(name, dot, description):
        item = CommentedMap()
        item["abbr"] = dot
        item["description"] = description
        return {name: [item]}

    return [
        entry("Running", STATUS_DEPLOYED_RUNNING, "Deployed - every container in the stack is running"),
        entry("Down", STATUS_DEPLOYED_DOWN, "Deployed - at least one container in the stack isn't running"),
        entry("Undeployed", STATUS_UNDEPLOYED, "No matching Dockhand stack was found for this repo"),
        entry("Private", VISIBILITY_LABELS[True], "Private GitHub repo"),
        entry("Public", VISIBILITY_LABELS[False], "Public GitHub repo"),
    ]


def load_or_create_config(path):
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=2, offset=0)
    # Long URLs / API keys/tokens must never be soft-wrapped onto a second
    # line - a wrapped plain scalar gets a newline folded into a space by
    # the YAML spec, which silently corrupts the value. A very large width
    # disables wrapping in practice.
    yaml.width = 100000
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.load(f)
        if data is None:
            data = CommentedSeq()
    else:
        data = CommentedSeq()
    return yaml, data


def upsert_group(data, group_name, entries, insert_at=None):
    """Replace the list under `group_name` if it exists, else insert a new
    top-level group - at `insert_at` if given, else appended at the end.
    `data` is the top-level CommentedSeq of {group: [...]}. An existing
    group's position is always left alone (`insert_at` only matters the
    first time a group is created), so a person's manual reordering of
    groups on the dashboard survives future syncs."""
    new_seq = CommentedSeq()
    new_seq.extend(entries)

    for item in data:
        if isinstance(item, dict) and group_name in item:
            item[group_name] = new_seq
            return data

    if insert_at is not None:
        data.insert(insert_at, {group_name: new_seq})
    else:
        data.append({group_name: new_seq})
    return data


def remove_group(data, group_name):
    """Remove a top-level {group_name: [...]} entry if present. Used to
    clean up the old flat group left over from before repos were split into
    Deployed/Undeployed groups."""
    for i, item in enumerate(data):
        if isinstance(item, dict) and group_name in item:
            del data[i]
            return data, True
    return data, False


def load_name_map(path, flag_name, value_desc):
    """Load a flat {key: value} JSON/YAML mapping file, lowercasing keys.
    Used for both --icon-map (repo -> icon) and --stack-alias-map
    (repo -> Dockhand stack name), which share the same simple shape."""
    if not path:
        return {}
    if not os.path.exists(path):
        print(f"ERROR: --{flag_name} file not found: {path}", file=sys.stderr)
        sys.exit(1)

    yaml = YAML(typ="safe")
    with open(path, encoding="utf-8") as f:
        raw = yaml.load(f) or {}

    if not isinstance(raw, dict):
        print(f"ERROR: --{flag_name} file must contain a flat mapping of repo_name: {value_desc}, "
              f"got {type(raw).__name__}", file=sys.stderr)
        sys.exit(1)

    return {str(k).lower(): str(v) for k, v in raw.items()}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", required=True, help="GitHub username or org name")
    p.add_argument("--org", action="store_true", help="Treat --user as an organization, not a personal account")
    p.add_argument("--config", default="services.yaml", help="Path to homepage's services.yaml (default: ./services.yaml)")
    p.add_argument("--group", default="GitHub Repos", help="Group/section name to write repos under (default: 'GitHub Repos')")
    p.add_argument("--token", default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
                    help="GitHub personal access token (or set GITHUB_TOKEN / GH_TOKEN env var)")
    p.add_argument("--sort", choices=["updated", "stars", "name"], default="updated", help="Sort order (default: updated)")
    p.add_argument("--limit", type=int, default=0, help="Max number of repos to include (0 = no limit)")
    p.add_argument("--include-forks", action="store_true", help="Include forked repos (excluded by default)")
    p.add_argument("--include-archived", action="store_true", help="Include archived repos (excluded by default)")
    p.add_argument("--include-private", action="store_true", help="Include private repos (requires a token with 'repo' scope)")
    p.add_argument("--hide-visibility", action="store_true",
                    help="Don't prefix each repo's description with a private/public colored dot")
    p.add_argument("--show-stars", action="store_true", help="Append star count to each repo's description")
    p.add_argument("--show-total", action="store_true",
                    help="Add a summary entry at the top of the group showing the total repo count "
                         "(and how many are shown, if --limit truncated the list)")
    p.add_argument("--icon-map", default=None,
                    help="Path to a JSON or YAML file of {repo_name: icon} overrides, applied on top of the "
                         "built-in known-app icons. Repo name matching is case-insensitive.")
    p.add_argument("--skip-icon-verification", action="store_true",
                    help="Don't check known-app / --icon-map icons against their source CDN before use "
                         "(Dashboard Icons, Material Design Icons, Simple Icons, selfh.st/icons). Use this if "
                         "the machine running this script can't reach those CDNs but your browser can - "
                         "otherwise every icon will look 'missing' here even though it would render fine.")
    p.add_argument("--dry-run", action="store_true", help="Print the resulting YAML instead of writing to --config")
    p.add_argument("--dockhand-url", default=os.environ.get("DOCKHAND_URL"),
                    help="Base URL of your Dockhand instance, e.g. https://dockhand.example.com "
                         "(or set DOCKHAND_URL env var). Required: repos are always split into two "
                         "groups - '{group} - Deployed' for repos registered as a git repository in "
                         "Dockhand, '{group} - Undeployed' for repos that aren't.")
    p.add_argument("--dockhand-token", default=os.environ.get("DOCKHAND_TOKEN"),
                    help="Dockhand API token (or set DOCKHAND_TOKEN env var)")
    p.add_argument("--dockhand-repos-path", default=os.environ.get("DOCKHAND_REPOS_PATH", "/api/git/repositories"),
                    help="API path appended to --dockhand-url that returns Dockhand's registered git "
                         "repositories, each with a 'url' field (default: /api/git/repositories)")
    p.add_argument("--dockhand-url-alias-map", default=os.environ.get("DOCKHAND_URL_ALIAS_MAP"),
                    help="Path to a JSON or YAML file of {repo_name: git_url} overrides, for repos whose "
                         "current GitHub URL doesn't match what's registered in Dockhand (e.g. after a "
                         "repo rename). Repo name matching is case-insensitive.")
    p.add_argument("--dockhand-status-field", default=os.environ.get("DOCKHAND_STATUS_FIELD"),
                    help="Comma-separated field name(s), checked in order, that report a Dockhand "
                         "git-repository entry's own status - fallback layer, only used if a repo has "
                         "no match in --dockhand-stacks-path (default: "
                         "status,state,stack_status,container_status).")
    p.add_argument("--dockhand-running-values", default=os.environ.get("DOCKHAND_RUNNING_VALUES"),
                    help="Comma-separated, case-insensitive values that count as 'running' for "
                         "--dockhand-status-field (default: running,up,healthy,active,started,ok).")
    p.add_argument("--dockhand-stacks-path", default=os.environ.get("DOCKHAND_STACKS_PATH", "/api/stacks"),
                    help="API path appended to --dockhand-url for live stack/container status, used to "
                         "give a deployed-but-down repo a yellow dot instead of green (default: "
                         "/api/stacks). Each stack's containerDetails[].state is checked; a stack "
                         "counts as down the moment any one container isn't 'running'.")
    p.add_argument("--dockhand-stacks-query", default=os.environ.get("DOCKHAND_STACKS_QUERY", "env=1"),
                    help="Query string appended to --dockhand-stacks-path (default: 'env=1')")
    p.add_argument("--dockhand-stack-alias-map", default=os.environ.get("DOCKHAND_STACK_ALIAS_MAP"),
                    help="Path to a JSON or YAML file of {repo_name: dockhand_stack_name} overrides, "
                         "for repos whose name doesn't match their Dockhand stack's name (a "
                         "docker-compose project/folder name, e.g. repo 'bookorbit' deployed as stack "
                         "'bookorbit-deploy'). Repo name matching is case-insensitive. Exact and "
                         "word-boundary matches (against '-'/'_' in the stack name) are tried first, "
                         "so this is usually only needed for a genuinely unrelated stack name.")
    p.add_argument("--dockhand-running-states", default=os.environ.get("DOCKHAND_RUNNING_STATES"),
                    help="Comma-separated, case-insensitive per-container Docker states that count as "
                         "'running' (default: running). Every container in a stack must be in one of "
                         "these states for that stack to be shown as up.")
    p.add_argument("--skip-stack-status", action="store_true",
                    help="Don't query --dockhand-stacks-path at all - every deployed repo stays green "
                         "regardless of its actual container status.")
    p.add_argument("--deployed-suffix", default=os.environ.get("DEPLOYED_SUFFIX", " - Deployed"),
                    help="Suffix appended to --group for the deployed-repos group (default: ' - Deployed')")
    p.add_argument("--undeployed-suffix", default=os.environ.get("UNDEPLOYED_SUFFIX", " - Undeployed"),
                    help="Suffix appended to --group for the undeployed-repos group (default: ' - Undeployed')")
    p.add_argument("--legend-style", choices=("bookmarks", "services", "none"),
                    default=os.environ.get("LEGEND_STYLE", "bookmarks"),
                    help="How to show the color legend (default: bookmarks). 'bookmarks' writes small, "
                         "horizontal chips to --bookmarks-file (Homepage's compact bookmarks-bar style, "
                         "shown at the top of every page rather than inside the GitHub group). "
                         "'services' writes it as its own full-size services group instead (see "
                         "--legend-suffix), same visual size as a repo card. 'none' skips it entirely.")
    p.add_argument("--legend-suffix", default=os.environ.get("LEGEND_SUFFIX", " - Legend"),
                    help="Suffix appended to --group for the color-legend group when "
                         "--legend-style=services (default: ' - Legend')")
    p.add_argument("--bookmarks-file", default=os.environ.get("BOOKMARKS_FILE"),
                    help="Path to Homepage's bookmarks.yaml, used when --legend-style=bookmarks "
                         "(default: bookmarks.yaml next to --config)")
    p.add_argument("--legend-bookmark-group", default=os.environ.get("LEGEND_BOOKMARK_GROUP", "Legend"),
                    help="Group name for the legend chips in bookmarks.yaml (default: 'Legend')")
    p.add_argument("--stale-groups", default=os.environ.get("STALE_GROUPS"),
                    help="Comma-separated, exact services.yaml group names to delete every run. Use this "
                         "once (or leave it in permanently - deleting an already-gone group is a no-op) "
                         "to clean up leftovers after renaming --group or --legend-suffix, e.g. "
                         "'Repo List - Deployed,Repo List - Undeployed,Repo List - Legend'.")
    p.add_argument("--stats-file", default=os.environ.get("STATS_FILE"),
                    help="Path to write a small {deployed, undeployed, total, updated_at} JSON stats file "
                         "(default: repo-stats.json next to --config). Served by webhook_server.py's "
                         "GET /stats for a Homepage customapi widget.")
    args = p.parse_args()

    if os.sep in args.group or args.group.lower().endswith((".yaml", ".yml")):
        print(
            f"ERROR: --group value '{args.group}' looks like a file path, not a display name.\n"
            f"This usually means --config's value got passed to --group by mistake "
            f"(or vice versa). Expected something like 'GitHub Repos' or 'Repo List'.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.include_private and not args.token:
        print(
            "Warning: --include-private has no effect without --token (or GITHUB_TOKEN/GH_TOKEN env var) - "
            "the GitHub API only returns public repos for unauthenticated requests. Use a token with the "
            "'repo' scope (classic PAT) or Contents: Read-only (fine-grained PAT) to see private repos too.",
            file=sys.stderr,
        )

    icon_overrides = load_name_map(args.icon_map, "icon-map", "icon")
    url_alias_map = load_name_map(args.dockhand_url_alias_map, "dockhand-url-alias-map", "git_url")
    stack_alias_map = load_name_map(args.dockhand_stack_alias_map, "dockhand-stack-alias-map", "stack_name")

    repos = fetch_all_repos(
        owner=args.user,
        token=args.token,
        is_org=args.org,
        include_forks=args.include_forks,
        include_archived=args.include_archived,
        include_private=args.include_private,
    )
    repos = sort_repos(repos, args.sort)

    total_count = len(repos)  # before --limit clips the visible list
    if args.limit:
        repos = repos[: args.limit]

    if not repos:
        print("No repositories found matching the given filters — leaving config untouched.", file=sys.stderr)
        sys.exit(1)

    if not args.dockhand_url:
        print(
            "ERROR: --dockhand-url (or DOCKHAND_URL env var) is required. Repos are always split into "
            "Deployed/Undeployed groups based on Dockhand, so there's no flat-list mode anymore.",
            file=sys.stderr,
        )
        sys.exit(1)

    dockhand_entries = fetch_dockhand_git_repo_urls(args.dockhand_url, args.dockhand_token, args.dockhand_repos_path)

    deployed_repos, undeployed_repos = [], []
    for r in repos:
        bucket = deployed_repos if repo_is_deployed(r, dockhand_entries, url_alias_map) else undeployed_repos
        bucket.append(r)

    unmatched = set(dockhand_entries) - {normalize_repo_url(r["html_url"]) for r in deployed_repos}
    if unmatched:
        print(
            f"Note: {len(unmatched)} Dockhand git repositor{'y' if len(unmatched) == 1 else 'ies'} "
            f"didn't match any of your GitHub repos ({', '.join(sorted(unmatched))}) - likely a "
            f"private/forked/renamed repo not visible with the current --user/--token/--include-* flags.",
            file=sys.stderr,
        )

    stacks_by_name = ({} if args.skip_stack_status else
                       fetch_dockhand_stacks(args.dockhand_url, args.dockhand_token,
                                             args.dockhand_stacks_path, args.dockhand_stacks_query))

    status_fields = (tuple(f.strip() for f in args.dockhand_status_field.split(",") if f.strip())
                      or DOCKHAND_STATUS_FIELDS) if args.dockhand_status_field else DOCKHAND_STATUS_FIELDS
    running_values = ({v.strip().lower() for v in args.dockhand_running_values.split(",") if v.strip()}
                       or DEFAULT_DOCKHAND_RUNNING_VALUES) if args.dockhand_running_values else DEFAULT_DOCKHAND_RUNNING_VALUES
    running_states = ({v.strip().lower() for v in args.dockhand_running_states.split(",") if v.strip()}
                       or DEFAULT_DOCKHAND_RUNNING_STATES) if args.dockhand_running_states else DEFAULT_DOCKHAND_RUNNING_STATES

    show_visibility = not args.hide_visibility
    deployed_entries = []
    down_count = 0
    unmatched_stacks = 0
    for r in deployed_repos:
        # Prefer the real, name-matched Dockhand stack (has per-container
        # state - see fetch_dockhand_stacks); fall back to whatever the
        # URL-matched git-repositories entry itself might expose.
        stack_entry = find_dockhand_stack(r, stacks_by_name, stack_alias_map) if stacks_by_name else None
        if stacks_by_name and stack_entry is None:
            unmatched_stacks += 1
        status = dockhand_stack_status(stack_entry, status_fields, running_values, running_states)
        if status is None:
            git_entry = find_dockhand_entry(r, dockhand_entries, url_alias_map)
            status = dockhand_stack_status(git_entry, status_fields, running_values, running_states)
        is_down = status is False
        if is_down:
            down_count += 1
        deployed_entries.append(build_entry(
            r, args.show_stars, icon_overrides, args.skip_icon_verification,
            status_label=STATUS_DEPLOYED_DOWN if is_down else STATUS_DEPLOYED_RUNNING,
            show_visibility=show_visibility,
        ))
    if down_count:
        print(f"Note: {down_count} deployed repo(s) have at least one container not running - "
              f"marked with a yellow dot instead of green.", file=sys.stderr)
    if unmatched_stacks:
        print(f"Note: {unmatched_stacks} deployed repo(s) had no matching Dockhand stack in "
              f"{args.dockhand_stacks_path} - left as the default green dot. If one of these is "
              f"actually down, add it to --dockhand-stack-alias-map.", file=sys.stderr)

    undeployed_entries = [
        build_entry(r, args.show_stars, icon_overrides, args.skip_icon_verification,
                    status_label=STATUS_UNDEPLOYED, show_visibility=show_visibility)
        for r in undeployed_repos
    ]

    if args.show_total:
        deployed_entries.insert(0, build_total_entry(args.user, len(deployed_repos), len(deployed_repos)))
        undeployed_entries.insert(0, build_total_entry(args.user, len(undeployed_repos), len(undeployed_repos)))

    if not args.skip_icon_verification:
        missing = icon_verification_stats["confirmed_missing"]
        unverifiable = icon_verification_stats["unverifiable"]
        if missing:
            print(f"{missing} icon(s) confirmed missing from their source and replaced with the GitHub icon "
                  f"(see warnings above).", file=sys.stderr)
        if unverifiable:
            print(f"{unverifiable} icon(s) couldn't be verified due to a network issue and were used as "
                  f"configured (see warnings above). If this happens every run, this machine likely can't "
                  f"reach the icon CDNs — try --skip-icon-verification.", file=sys.stderr)

    config_dir = os.path.dirname(os.path.abspath(args.config)) or "."
    if not os.path.isdir(config_dir):
        print(
            f"ERROR: the folder '{config_dir}' doesn't exist, so '{args.config}' can't be written.\n"
            f"Double-check --config points at the real path to your homepage services.yaml "
            f"(e.g. it might be /volume1/docker/homepage/services.yaml rather than a 'config' subfolder).",
            file=sys.stderr,
        )
        sys.exit(1)

    yaml, data = load_or_create_config(args.config)

    deployed_group = args.group + args.deployed_suffix
    undeployed_group = args.group + args.undeployed_suffix
    legend_group = args.group + args.legend_suffix

    # Written first so a brand-new config puts the legend above the repo
    # groups (see upsert_group's insert_at) - on later runs its position
    # is left alone if the person has moved it. Only relevant for
    # --legend-style=services; the 'bookmarks' default writes to a
    # separate file entirely (see below), and 'none' skips it.
    if args.legend_style == "services":
        data = upsert_group(data, legend_group, build_legend_entries(), insert_at=0)
    else:
        data, _ = remove_group(data, legend_group)

    data = upsert_group(data, deployed_group, deployed_entries)
    data = upsert_group(data, undeployed_group, undeployed_entries)

    # Clean up the old flat group (plain --group, e.g. "Repo List") left
    # over from before repos were split into Deployed/Undeployed groups.
    data, removed_stale = remove_group(data, args.group)

    # Clean up any other exact group names named via --stale-groups - e.g.
    # leftovers from a previous --group/--legend-suffix value that's since
    # been changed. This is a no-op for anything already gone.
    stale_removed = []
    if args.stale_groups:
        for name in (n.strip() for n in args.stale_groups.split(",")):
            if not name:
                continue
            data, removed = remove_group(data, name)
            if removed:
                stale_removed.append(name)

    if args.dry_run:
        yaml.dump(data, sys.stdout)
    else:
        # Write atomically: build in a temp file, then replace, so a crashed
        # run never leaves services.yaml half-written.
        tmp_path = args.config + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)
        os.replace(tmp_path, args.config)

    if args.legend_style == "bookmarks":
        bookmarks_path = args.bookmarks_file or os.path.join(config_dir, "bookmarks.yaml")
        b_yaml, b_data = load_or_create_config(bookmarks_path)
        b_data = upsert_group(b_data, args.legend_bookmark_group, build_legend_bookmarks(), insert_at=0)
        if args.dry_run:
            print(f"\n# --- {bookmarks_path} ---")
            b_yaml.dump(b_data, sys.stdout)
        else:
            b_tmp_path = bookmarks_path + ".tmp"
            with open(b_tmp_path, "w", encoding="utf-8") as f:
                b_yaml.dump(b_data, f)
            os.replace(b_tmp_path, bookmarks_path)

    if args.dry_run:
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    stale_note = f" (removed stale flat group '{args.group}')" if removed_stale else ""
    if stale_removed:
        stale_note += f" (removed stale group(s): {', '.join(stale_removed)})"
    if args.legend_style == "services":
        legend_note = f", legend in '{legend_group}'"
    elif args.legend_style == "bookmarks":
        legend_note = f", legend chips in '{bookmarks_path}'"
    else:
        legend_note = ""
    print(f"[{ts}] Wrote {len(deployed_entries)} deployed / {len(undeployed_entries)} undeployed "
          f"repo(s) ({total_count} repo(s) total) to groups '{deployed_group}' / '{undeployed_group}' "
          f"in {args.config}{legend_note}{stale_note}")


    # Also drop a tiny stats file alongside services.yaml so a Homepage
    # customapi widget can show live deployed/undeployed counts (see
    # webhook_server.py's GET /stats, which just serves this file).
    stats_path = args.stats_file or os.path.join(config_dir, "repo-stats.json")
    stats = {
        "deployed": len(deployed_repos),
        "deployed_down": down_count,
        "undeployed": len(undeployed_repos),
        "total": total_count,
        "updated_at": ts,
    }
    stats_tmp = stats_path + ".tmp"
    with open(stats_tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f)
    os.replace(stats_tmp, stats_path)


if __name__ == "__main__":
    main()