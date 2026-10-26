#!/usr/bin/env python3
"""git_reconciler — ff-only two-way sync between GitHub and Forgejo (ORG-101 §3, T-4690).

The ONLY thing that moves refs between the two hosts. For each manifest repo that has a
`primary` (GitHub) and a `mirror` (Forgejo) remote, and for each synced branch:

  equal                        -> OK
  Forgejo behind (ancestor)    -> fast-forward Forgejo to GitHub  (carries PR merges)
  GitHub behind (ancestor)     -> fast-forward GitHub to Forgejo  (replays outage-time pushes)
  neither is an ancestor       -> DIVERGED: alert, change NOTHING
  one side lacks the branch    -> create it on that side (a create is a fast-forward from nothing)

It never forces. Every push is a plain `<sha>:refs/heads/<branch>` push with no `+` and no
--force, so the git CLIENT refuses a non-fast-forward even if the ancestry check were wrong.
(The client does that check, not the server: do not read this as proof that Forgejo itself
blocks force pushes. Server-side force-blocking is ORG-101 §5's parity test.)

FORGEJO PULL-MIRRORS (pre-conversion, ORG-101 §4). A pull-mirror refuses pushes. While a repo's
Forgejo copy is still a mirror, the Forgejo-recovery leg triggers the forge's own mirror-sync instead
of pushing, and a GitHub-behind state cannot happen in normal operation (nothing can push to a
mirror), so it is reported as DIVERGED. Conversion happens only once this reconciler covers the
repo, so a repo never stops tracking GitHub.

GITHUB WRITE CREDENTIAL. Replay to GitHub uses the PAT at ~/.github-reconciler.token (the operator TG
11387; the reconciler is in the bypass set, ORG-101 §5). Without it, a GitHub-behind state is
REPLAY_PENDING and alerted, never pushed with some other identity.

TRUST BOUNDARY. A token is attached only to a remote whose EXACT host (remote_resolve._host_of)
is the expected one AND which passes the manifest's allowed_hosts binding
(remote_resolve._check_host_binding). A substring match would let `git.your-self-hosted-git.example.evil.com`
receive the Forgejo token.

SECRETS. Tokens ride an http.extraHeader in GIT_CONFIG_* env, never argv. Inherited git
trace/verbose variables are stripped from that env, and every message that reaches the
journal or the mailbox goes through _redact().

FAILING LOUD. This exists to make drift loud, so its own failures must be too:
  - a hung or crashing git call becomes a per-repo ERROR; it never aborts the run;
  - a missing token or an unloadable manifest exits 2, even with --findings-exit-zero;
  - an alert that cannot be delivered exits 3, even with --findings-exit-zero, so the unit's
    OnFailure (which has a Telegram fallback) carries it.

TARGETS (ORG-101 §6, every repo). Two sources, manifest first:
  - the pinned manifest (repos.yaml) entries with a GitHub primary and a Forgejo mirror;
  - INVENTORY: every repo GITHUB_OWNER owns on GitHub (forks and archived included), mapped 1:1
    to git.your-self-hosted-git.example/FORGEJO_ORG/<same name>. Owner, org and hosts are pinned in code; only the
    repo NAME comes from the API, and it must be a plain path segment. repos.yaml stays the
    seat-clone authority and is not turned into a catalogue.
  Archived repos reconcile Forgejo-recovery only (GitHub refuses pushes). Empty repos read
  "absent on both" (OK) until a first commit appears. A repo with no Forgejo copy reads NO_COPY.

MANIFEST FAILURE IS NOT A STOP (deliberate; orchestrator ruling on review note 4). If the pinned
manifest cannot load, the run exits 2 and mails, but the INVENTORY still reconciles every
repo, pantheon-config included. Safe because the inventory's trust boundary is the code
constants (owner, org, hosts), not the manifest, and every move is still fast-forward-only.
The reconciler must keep converging (ORG-101 §3).

BOUNDED RUN. A wall-clock budget (RUN_BUDGET_SECONDS) ends the repo loop early, and every
repo not reached is reported as an ERROR, so alerts and state are still written before
systemd's TimeoutStartSec. The next run starts where this one stopped. A host failing
CIRCUIT_AFTER times in one run is skipped for the rest of the run.

BRANCH SCOPE (Phase A): each repo's GitHub DEFAULT branch, overridable with --branch. Syncing
every branch would also need deletion semantics: a merged-and-deleted PR branch on GitHub
would be recreated from Forgejo. That is deliberately out of scope until it is designed.

Exit codes: 0 = all OK/healed (or findings mailed, with --findings-exit-zero); 1 = findings;
2 = config failure (token / manifest); 3 = alert delivery failed.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GITHUB_HOST = "github.com"
FORGEJO_HOST = "git.your-self-hosted-git.example"
FORGEJO_API = f"https://{FORGEJO_HOST}/api/v1"
FORGEJO_USER = "pantheon-mirror"  # repo-admin on github-mirror/* (can push once converted, can mirror-sync)
FORGEJO_TOKEN_FILE = Path(os.environ.get(
    "PANTHEON_RECONCILER_FORGEJO_TOKEN_FILE", str(Path.home() / ".forgejo-mirror.token")))
GITHUB_TOKEN_FILE = Path(os.environ.get(
    "PANTHEON_RECONCILER_GITHUB_TOKEN_FILE", str(Path.home() / ".github-reconciler.token")))
USER_AGENT = "pantheon-git-reconciler/1"  # forgejo's bot filter 403s urllib's default UA
GIT_TIMEOUT = 120
ERROR_WINDOW, ERROR_WINDOW_MIN = 6, 3  # an ERROR mails once it is 3 of the last 6 runs
MANIFEST_RETRY_SECONDS = 20  # one retry absorbs the re-pin / worktree fast-forward race
CIRCUIT_AFTER = 3  # a host whose ls-remote fails for 3 repos in a row is skipped for the run
GITHUB_OWNER = "example-org"     # inventory mode: hard-pinned, never read from the API
FORGEJO_ORG = "github-mirror"   # inventory mode: hard-pinned
MAX_PAGES = 50  # x100 repos per page
RUN_BUDGET_SECONDS = 300  # checked between repos; + one repo's worst case (~390s) < TimeoutStartSec=15min
# Conservative refname check for a branch name that came from the API (it flows into argv and
# alert keys): plain segments, no '..', no leading '-', no '.lock' suffix, no trailing '/'.
_SAFE_REF = re.compile(r"^(?!-)(?!.*\.\.)(?!.*\.lock$)(?!.*/$)[A-Za-z0-9._/-]{1,200}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

OK, HEALED, DIVERGED, REPLAY_PENDING, ERROR = "OK", "HEALED", "DIVERGED", "REPLAY_PENDING", "ERROR"
SKIPPED = "SKIPPED"  # deliberately not reconciled (e.g. archived); not a finding
NO_COPY = "NO_COPY"  # the repo has no Forgejo copy yet (forgejo_repo_rollout creates it)
BAD = (DIVERGED, REPLAY_PENDING, ERROR, NO_COPY)
EXIT_FINDINGS, EXIT_CONFIG, EXIT_ALERT = 1, 2, 3

# Inherited debug settings that would print the Authorization header (or the whole wire).
_LEAKY_ENV = ("GIT_TRACE", "GIT_TRACE_CURL", "GIT_TRACE_PACKET", "GIT_CURL_VERBOSE",
              "GIT_TRACE_CURL_NO_DATA", "GIT_TRACE2", "GIT_TRACE2_EVENT", "GIT_TRACE2_PERF")
_SECRETS: set[str] = set()


def _redact(text: str) -> str:
    """Strip anything credential-shaped before it reaches the journal or a mail."""
    for s in _SECRETS:
        text = text.replace(s, "<redacted>")
    text = re.sub(r"(?i)(authorization:\s*)(basic|bearer|token)\s+\S+", r"\1\2 <redacted>", text)
    text = re.sub(r"(https?://)[^/@\s]+@", r"\1<redacted>@", text)
    return text


def _log(msg: str) -> None:
    print(f"[git_reconciler ts={int(time.time())}] {_redact(msg)}", flush=True)


def _state_dir() -> Path:
    env = os.environ.get("PANTHEON_STATE_DIR")
    return (Path(env) if env else Path.home() / ".local" / "state" / "pantheon") / "git_reconciler"


_KEY_VALUE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(.+)$")


def _read_token(path: Path) -> str | None:
    """First line of the file. A `NAME=value` line yields value; a raw token is returned whole,
    even if it contains '=' (base64 padding), because only an identifier-shaped prefix counts."""
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    first = raw.splitlines()[0].strip() if raw else ""
    m = _KEY_VALUE.match(first)
    if m and m.group(1).strip("="):  # 'tok==' is base64 padding, not NAME=value
        first = m.group(1).strip()
    if first:
        _SECRETS.add(first)
    return first or None


def _auth_env(user: str, token: str | None) -> dict[str, str]:
    """git env carrying an Authorization header without putting the secret in argv."""
    env = {k: v for k, v in os.environ.items() if k not in _LEAKY_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        basic = base64.b64encode(f"{user}:{token}".encode()).decode()
        _SECRETS.add(basic)
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
    return env


GitRunner = Callable[[list, Path, dict], subprocess.CompletedProcess]


def _default_git(argv: list, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    """Never raises: a hang (today's outage shape) or an exec failure becomes a failed result,
    which the callers turn into a per-repo ERROR instead of aborting the whole run."""
    try:
        return subprocess.run(["git", *argv], cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"git {argv[0]} timed out after {GIT_TIMEOUT}s")
    except OSError as e:
        return subprocess.CompletedProcess(argv, 127, "", f"git {argv[0]} could not run: {e}")


@dataclass
class Side:
    """One host's view of a repo: where to fetch/push and with which credentials."""
    name: str          # "github" | "forgejo"
    url: str           # credential-free url
    env: dict
    can_push: bool
    push_blocker: str = ""  # why can_push is False (shown in the result)


@dataclass
class Result:
    repo: str
    branch: str
    status: str
    detail: str
    github_sha: str | None = None
    forgejo_sha: str | None = None
    immediate: bool = False  # config failures mail on the first run, not after an ERROR streak


def _strip_userinfo(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    if p.scheme in ("http", "https") and "@" in p.netloc:
        return urllib.parse.urlunsplit(p._replace(netloc=p.netloc.split("@", 1)[1]))
    return url


def forgejo_full_name(url: str) -> str:
    """'https://u@git.your-self-hosted-git.example/github-mirror/pantheon-config.git' -> 'github-mirror/pantheon-config'."""
    path = urllib.parse.urlsplit(url).path.strip("/")
    return path[:-4] if path.endswith(".git") else path


def github_https(url: str) -> str:
    """git@github.com:Owner/Repo.git -> https://github.com/Owner/Repo.git (for token auth)."""
    if url.startswith(f"git@{GITHUB_HOST}:"):
        return f"https://{GITHUB_HOST}/" + url.split(":", 1)[1]
    return url


def forgejo_api(method: str, path: str, token: str, *, timeout: int = 30):
    req = urllib.request.Request(FORGEJO_API + path, method=method, headers={
        "Authorization": f"token {token}", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else None


def _remote_sha(git: GitRunner, cache: Path, side: Side, branch: str) -> str | None:
    """ls-remote the branch. None = branch absent. Raises RuntimeError when the host can't answer:
    an unreadable host is UNKNOWN, never 'absent' (that would trigger a create)."""
    r = git(["ls-remote", "--heads", side.url, f"refs/heads/{branch}"], cache, side.env)
    if r.returncode != 0:
        raise RuntimeError(f"{side.name} ls-remote failed: {r.stderr.strip()[-300:]}")
    for line in r.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}":
            return sha
    return None


def _fetch(git: GitRunner, cache: Path, side: Side, branch: str) -> None:
    r = git(["fetch", "--no-tags", "--quiet", side.url,
             f"+refs/heads/{branch}:refs/{side.name}/{branch}"], cache, side.env)
    if r.returncode != 0:
        raise RuntimeError(f"{side.name} fetch failed: {r.stderr.strip()[-300:]}")


def _is_ancestor(git: GitRunner, cache: Path, a: str, b: str) -> bool:
    r = git(["merge-base", "--is-ancestor", a, b], cache, _auth_env("", None))
    if r.returncode not in (0, 1):
        raise RuntimeError(f"merge-base failed: {r.stderr.strip()[-300:]}")
    return r.returncode == 0


def _push(git: GitRunner, cache: Path, side: Side, sha: str, branch: str) -> None:
    # No --force, no '+': the git client refuses a non-ff even if the ancestry check were wrong.
    r = git(["push", "--quiet", side.url, f"{sha}:refs/heads/{branch}"], cache, side.env)
    if r.returncode != 0:
        raise RuntimeError(f"push to {side.name} refused: {r.stderr.strip()[-300:]}")


def reconcile_branch(
    *, repo: str, branch: str, github: Side, forgejo: Side, cache: Path, git: GitRunner,
    forgejo_is_mirror: bool, mirror_sync: Callable[[], None], dry_run: bool,
    github_archived: bool = False,
) -> Result:
    """Classify one repo/branch and heal it if a fast-forward does it. Never raises.
    An ARCHIVED GitHub repo refuses pushes: it is reconciled Forgejo-recovery only, and a GitHub-behind
    state is SKIPPED, never an ERROR."""
    def res(status: str, detail: str, g=None, f=None) -> Result:
        return Result(repo, branch, status, _redact(detail), g, f)

    try:
        g = _remote_sha(git, cache, github, branch)
        f = _remote_sha(git, cache, forgejo, branch)
    except Exception as e:  # noqa: BLE001 — any failure here is an UNKNOWN, reported per repo
        return res(ERROR, str(e))
    if g == f:
        return res(OK, "in sync" if g else "absent on both", g, f)

    try:
        if g:
            _fetch(git, cache, github, branch)
        if f:
            _fetch(git, cache, forgejo, branch)
        if g and f:
            forgejo_behind = _is_ancestor(git, cache, f, g)
            github_behind = not forgejo_behind and _is_ancestor(git, cache, g, f)
        else:
            forgejo_behind, github_behind = f is None, g is None
    except Exception as e:  # noqa: BLE001
        return res(ERROR, str(e), g, f)

    if forgejo_behind:
        if forgejo_is_mirror:
            if dry_run:
                return res(HEALED, f"[dry-run] would trigger mirror-sync ({_s(f)} -> {_s(g)})", g, f)
            try:
                mirror_sync()
            except Exception as e:  # noqa: BLE001 — any API failure is an ERROR result, not a crash
                return res(ERROR, f"mirror-sync failed: {e!r}", g, f)
            return res(HEALED, f"pull-mirror behind; mirror-sync triggered ({_s(f)} -> {_s(g)})", g, f)
        return _ff(git, cache, forgejo, g, branch, dry_run, res, g, f)

    if github_behind:
        if github_archived:
            return res(SKIPPED, f"github is archived (read-only); forgejo ahead ({_s(g)} -> {_s(f)}) "
                                "is not replayed", g, f)
        if forgejo_is_mirror:
            return res(DIVERGED, "forgejo pull-mirror is AHEAD of github: impossible in normal "
                                 "operation, investigate", g, f)
        if not github.can_push:
            return res(REPLAY_PENDING, f"github behind forgejo ({_s(g)} -> {_s(f)}); "
                                       f"{github.push_blocker}", g, f)
        return _ff(git, cache, github, f, branch, dry_run, res, g, f)

    return res(DIVERGED, f"neither side is an ancestor (github {_s(g)}, forgejo {_s(f)}); "
                         "nothing changed", g, f)


def _ff(git, cache, side: Side, sha: str, branch: str, dry_run: bool, res, g, f):
    if not side.can_push:
        return res(ERROR, f"{side.name} behind but not pushable: {side.push_blocker}", g, f)
    if dry_run:
        return res(HEALED, f"[dry-run] would fast-forward {side.name} to {_s(sha)}", g, f)
    try:
        _push(git, cache, side, sha, branch)
    except Exception as e:  # noqa: BLE001
        return res(ERROR, str(e), g, f)
    return res(HEALED, f"fast-forwarded {side.name} to {_s(sha)}", g, f)


def _s(sha: str | None) -> str:
    return sha[:10] if sha else "(absent)"


# ---------------------------------------------------------------------------
# Alerting: one mail per distinct bad state, re-armed when it clears
# ---------------------------------------------------------------------------

def _alert_key(r: Result) -> str:
    return f"{r.repo}:{r.branch}"


def _is_run_scoped(key: str) -> bool:
    """Keys that are not a repo:branch, e.g. '(config):-', '(manifest):-' and a host-binding
    '<repo>:-'. Healthy runs never produce them, so they must be cleared by absence."""
    return key.startswith("(") or key.endswith(":-") or key.endswith(":(branch)")


def alert_new_problems(results: list[Result], state_path: Path,
                       send: Callable[[str, str], None]) -> list[Result]:
    """Mail the problems not already mailed. Raises if `send` fails (the caller exits 3).

    - Only repo:branch keys checked in THIS run are rewritten, so a manual `--repo X` run never
      re-arms or forgets other repos' state.
    - Run-scoped keys ((config), (manifest), host-binding) are dropped on any run that does not
      produce them, so a recurrence after a fix mails again (review follow-up B).
    - An ERROR mails once ERROR_WINDOW_MIN of the last ERROR_WINDOW runs were ERROR, so one blip
      does not page but a host failing every other run still does (review follow-up A).
    - DIVERGED / REPLAY_PENDING and config errors (immediate) mail on first sight."""
    try:
        state = json.loads(state_path.read_text())
        if not isinstance(state, dict):
            state = {}
    except (OSError, json.JSONDecodeError):
        state = {}
    produced = {_alert_key(r) for r in results}
    for key in [k for k in state if _is_run_scoped(k) and k not in produced]:
        del state[key]
    fresh = []
    for r in results:
        key = _alert_key(r)
        prev = state.get(key) if isinstance(state.get(key), dict) else {}
        hist = (list(prev.get("hist", [])) + [r.status])[-ERROR_WINDOW:]
        if r.status not in BAD:
            if ERROR in hist:
                # keep the ERROR window alive, and keep what was mailed: re-arm only once the
                # window holds no ERROR at all, so a flapping host mails once, not every other run
                state[key] = {"hist": hist, "mailed_sig": prev.get("mailed_sig")}
            else:
                state.pop(key, None)
            continue
        sig = f"{r.status}|{r.github_sha}|{r.forgejo_sha}"
        mailed = prev.get("mailed_sig")
        due = sig != mailed and (r.status != ERROR or r.immediate
                                 or hist.count(ERROR) >= ERROR_WINDOW_MIN)
        state[key] = {"hist": hist, "mailed_sig": sig if due else mailed}
        if due:
            fresh.append(r)
    if fresh:
        body = "git reconciler (ORG-101 §3) needs attention:\n" + "\n".join(
            f"- {r.repo}@{r.branch}: {r.status} — {r.detail}" for r in fresh)
        body += ("\nNothing was forced. DIVERGED = resolve by hand (replay the one-sided commits "
                 "through the gated push path, then let the reconciler fast-forward).")
        send(f"git-reconciler: {len(fresh)} repo/branch need attention", _redact(body))
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=1))
    except OSError as e:
        _log(f"could not persist alert state (non-fatal): {e}")
    return fresh


def _mail_recovery(subject: str, body: str) -> None:
    from tools import mailbox
    from tools.agent_role_map import mailbox_hub
    mailbox.send(f"{mailbox_hub()}/recovery", body, "git_reconciler", subject=subject)


# ---------------------------------------------------------------------------
# Targets: the pinned manifest first, then the GitHub inventory (ORG-101 §6)
# ---------------------------------------------------------------------------

class UnverifiedManifest(RuntimeError):
    """The manifest failed its pin check; nothing from it may be used."""


@dataclass
class Target:
    name: str
    github_url: str
    forgejo_url: str
    default_branch: str = "main"
    archived: bool = False
    source: str = "manifest"  # "manifest" | "inventory"


def repos_from_manifest(only: list[str] | None, manifest=None):
    """(name, github_url, forgejo_url) for every manifest repo with a GitHub primary and a Forgejo
    mirror whose EXACT hosts are the expected ones and pass the manifest's host binding. A repo
    that fails either check is skipped (no token is ever attached to it) and reported."""
    from tools.remote_resolve import (HostBindingViolation, _check_host_binding, _host_of,
                                      load_production_manifest)
    if manifest is None:
        manifest = load_production_manifest()
    if not getattr(manifest, "signature_verified", False):
        # load_manifest() never raises on a pin mismatch: it records the verdict and leaves the
        # refusal to the consumer (remote_resolve docstring). This IS a consumer, so refuse here.
        # A tampered repos.yaml could otherwise repoint a repo to another path on github.com and
        # have it fast-forwarded into Forgejo. The caller retries once, then reports exit 2.
        raise UnverifiedManifest("repos.yaml does not verify against the manifest pin "
                                 "(refresh with tools/manifest_pin.py refresh after an authorized change)")
    out, refused = [], []
    for name, entry in sorted(manifest.repos.items()):
        if only and name not in only:
            continue
        prim = [r for r in entry.remotes if r.role == "primary"]
        mirr = [r for r in entry.remotes if r.role == "mirror" and _host_of(r.url) == FORGEJO_HOST]
        if not (prim and mirr) or _host_of(prim[0].url) != GITHUB_HOST:
            continue
        try:
            _check_host_binding(manifest, prim[0])
            _check_host_binding(manifest, mirr[0])
        except HostBindingViolation as e:
            refused.append((name, str(e), _repo_name_from_url(prim[0].url) or name))
            continue
        out.append((name, prim[0].url, _strip_userinfo(mirr[0].url)))
    return out, refused


def _load_manifest_targets(only, manifest, sleep):
    """repos_from_manifest with one retry. A repos.yaml change can briefly mismatch the pin: the
    consumer re-pins from the fetched origin blob just before fast-forwarding the worktree, and a
    manual `git pull` skips the re-pin entirely. One delayed retry absorbs the first; a real
    mismatch still fails on the second attempt and is reported as a config error."""
    try:
        return repos_from_manifest(only, manifest)
    except Exception:  # noqa: BLE001 — retried once, then propagated to the caller
        sleep(MANIFEST_RETRY_SECONDS)
        return repos_from_manifest(only, manifest)


def github_inventory(gh_token: str, *, opener=urllib.request.urlopen) -> list[dict]:
    """Every repo GITHUB_OWNER owns (forks and archived included), via the authenticated API."""
    out, page = [], 1
    while True:
        if page > MAX_PAGES:  # a server that ignores `page` must not loop us forever
            raise RuntimeError(f"GitHub inventory exceeded {MAX_PAGES} pages; refusing to continue")
        req = urllib.request.Request(
            f"https://api.github.com/user/repos?affiliation=owner&per_page=100&page={page}",
            headers={"Authorization": f"Bearer {gh_token}", "User-Agent": USER_AGENT,
                     "Accept": "application/vnd.github+json"})
        with opener(req, timeout=60) as r:
            batch = json.loads(r.read())
        if not batch:
            return out
        out += [b for b in batch if (b.get("owner") or {}).get("login") == GITHUB_OWNER]
        page += 1


def inventory_targets(repos: list[dict]) -> list[Target]:
    """Map each owned GitHub repo 1:1 to github-mirror/<same name>. Owner, org and both hosts are
    pinned here in code; the only untrusted input is the NAME, and a name that is not a plain
    path segment is dropped, so nothing from the API can steer a token to another host or path."""
    out = []
    for r in repos:
        name = r.get("name") or ""
        if not _SAFE_NAME.match(name) or name in (".", ".."):
            continue
        out.append(Target(
            name=name,
            github_url=f"https://{GITHUB_HOST}/{GITHUB_OWNER}/{name}.git",
            forgejo_url=f"https://{FORGEJO_HOST}/{FORGEJO_ORG}/{name}.git",
            default_branch=r.get("default_branch") or "main",
            archived=bool(r.get("archived")),
            source="inventory"))
    return out


def _repo_name_from_url(url: str) -> str | None:
    """'git@github.com:O/name.git' or 'https://github.com/O/name.git' -> 'name'."""
    tail = url.rstrip("/").replace(":", "/").rsplit("/", 1)[-1]
    tail = tail[:-4] if tail.endswith(".git") else tail
    return tail if _SAFE_NAME.match(tail or "") else None


# A HOST-level failure (the host is down or unreachable), as opposed to a per-repo answer such as
# "Repository not found", an auth refusal or a permission error. Only these feed the breaker:
# a deleted, renamed or unreadable repo must not skip the healthy repos after it (REBOUND #2).
_HOST_DOWN = re.compile(
    r"timed out|could not run|Could not resolve host|Temporary failure in name resolution|"
    r"Failed to connect|Connection (?:refused|reset|timed out)|Network is unreachable|"
    r"returned error: 5\d\d|HTTP ?Error 5\d\d|RPC failed|URLError|TimeoutError|socket\.timeout|"
    r"SSL|TLS|unexpected disconnect|early EOF", re.I)
# URLs are removed before matching: a repo NAME is not evidence of a host failure
# ("ssl-playground" matched SSL, so its per-repo failure tripped the github breaker).
# Quoted text is kept, because exception reprs quote the message itself.
_URLS = re.compile(r"\S+://\S+|\S+@[\w.-]+:\S+")


def _failed_host(r: Result) -> str | None:
    """Which host is DOWN according to this result, for the per-host breaker; None for a
    per-repo failure (not found, auth, permission), which stays a plain ERROR for that repo."""
    if r.status != ERROR or not _HOST_DOWN.search(_URLS.sub(" ", r.detail)):
        return None
    for host in ("github", "forgejo"):
        if r.detail.startswith((f"{host} ls-remote failed", f"{host} fetch failed")):
            return host
    if r.detail.startswith("forgejo repo lookup failed"):
        return "forgejo"
    return None


def _rotate(items: list, start_name: str | None) -> list:
    for i, t in enumerate(items):
        if t.name == start_name:
            return items[i:] + items[:i]
    return items


def _load_next_start(state: Path) -> str | None:
    try:
        return json.loads((state / "next_start.json").read_text()).get("name")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _save_next_start(state: Path, name: str | None) -> None:
    try:
        state.mkdir(parents=True, exist_ok=True)
        (state / "next_start.json").write_text(json.dumps({"name": name}))
    except OSError as e:
        _log(f"could not persist next_start (non-fatal): {e}")


def run(*, only: list[str] | None, branches: list[str] | None, dry_run: bool, alert: bool,
        git: GitRunner = _default_git, send: Callable[[str, str], None] = _mail_recovery,
        manifest=None, forgejo_api_fn=forgejo_api, inventory_fn=None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic) -> int:
    started = clock()
    state = _state_dir()
    results: list[Result] = []
    hard = 0
    fj_token = _read_token(FORGEJO_TOKEN_FILE)
    gh_token = _read_token(GITHUB_TOKEN_FILE)

    targets: dict[str, Target] = {}
    inventory_meta: dict[str, Target] = {}
    if not gh_token:
        _log(f"inventory: off (no GitHub token at {GITHUB_TOKEN_FILE}); manifest repos only")
    else:
        try:
            raw = (inventory_fn or github_inventory)(gh_token)
            inventory_meta = {t.name: t for t in inventory_targets(raw)}
            targets.update({n: t for n, t in inventory_meta.items() if not only or n in only})
        except Exception as e:  # noqa: BLE001 — manifest repos still reconcile; this is loud
            results.append(Result("(inventory)", "-", ERROR,
                                  _redact(f"GitHub inventory failed: {e!r}"), immediate=True))
    try:
        repos, refused = _load_manifest_targets(only, manifest, sleep)
    except Exception as e:  # noqa: BLE001 — the inventory still reconciles; this is a config failure
        results.append(Result("(manifest)", "-", ERROR, _redact(f"manifest load failed: {e!r}"),
                              immediate=True))
        repos, refused, hard = [], [], EXIT_CONFIG
    for name, why, gh_name in refused:
        results.append(Result(name, "-", ERROR, _redact(f"host binding refused, skipped: {why}"),
                              immediate=True))
        # a refused manifest entry must not fall back to the inventory: drop it by the GitHub
        # repo it names (the manifest KEY could differ from the repo's real name)
        targets.pop(gh_name, None)
        targets.pop(name, None)
    for name, gh_url, fj_url in repos:  # manifest entries take precedence
        gh_name = _repo_name_from_url(gh_url) or name
        meta = inventory_meta.get(gh_name)
        targets.pop(gh_name, None)
        targets[name] = Target(name, gh_url, fj_url,
                               default_branch=meta.default_branch if meta else "main",
                               archived=meta.archived if meta else False, source="manifest")
    if not fj_token:
        results.append(Result("(config)", "-", ERROR, f"forgejo token unreadable at {FORGEJO_TOKEN_FILE}",
                              immediate=True))
        targets, hard = {}, EXIT_CONFIG

    # Bounded run (review REBOUND): a wall-clock budget, a per-host breaker that counts EVERY
    # host failure this run (not just consecutive ones), and a rotating start, so a dead host can
    # neither push the unit past TimeoutStartSec nor starve the same tail of repos every fire.
    ordered = _rotate(sorted(targets.values(), key=lambda t: t.name.lower()), _load_next_start(state))
    host_failures = {"github": 0, "forgejo": 0}
    next_start = None
    for i, t in enumerate(ordered):
        want = branches or [t.default_branch]
        if clock() - started > RUN_BUDGET_SECONDS:
            next_start = t.name
            for u in ordered[i:]:
                results += [Result(u.name, b, ERROR, f"skipped: run budget of {RUN_BUDGET_SECONDS}s "
                                                     "exhausted; next run starts here")
                            for b in (branches or [u.default_branch])]
            break
        bad_ref = [b for b in want if not _SAFE_REF.match(b)]
        if bad_ref:
            results += [Result(t.name, "(branch)", ERROR,
                               f"unsafe branch name from the API, not used: {bad_ref[0][:60]!r}",
                               immediate=True)]
            continue
        open_host = next((h for h, n in host_failures.items() if n >= CIRCUIT_AFTER), None)
        if open_host:
            if next_start is None:
                next_start = t.name  # resume here next run, as the budget path does
            results += [Result(t.name, b, ERROR, f"skipped: circuit open, {open_host} failed "
                                                 f"{CIRCUIT_AFTER} times this run")
                        for b in want]
            continue
        try:
            got = _reconcile_repo(t, want, fj_token, gh_token, state, git, dry_run, forgejo_api_fn)
        except Exception as e:  # noqa: BLE001 — one bad repo never stops the others
            got = [Result(t.name, b, ERROR, _redact(f"unexpected: {e!r}")) for b in want]
        results += got
        for host in host_failures:
            if any(_failed_host(r) == host for r in got):
                host_failures[host] += 1
    if not dry_run and not only:  # a dry-run or a filtered run must not move the rotation
        _save_next_start(state, next_start)

    for r in results:
        if r.status not in (OK,) or not alert:
            _log(f"{r.repo}@{r.branch} {r.status}: {r.detail}")
    if alert and not dry_run:
        try:
            alert_new_problems(results, state / "alert_state.json", send)
        except Exception as e:  # noqa: BLE001 — undeliverable alert must not look like success
            _log(f"ALERT DELIVERY FAILED: {e!r}")
            return EXIT_ALERT
    bad = [r for r in results if r.status in BAD]
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    _log(f"done: {len(results)} checked ({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}), "
         f"{len(bad)} need attention")
    if hard:
        return hard
    return EXIT_FINDINGS if bad else 0


def _reconcile_repo(t: Target, branches, fj_token, gh_token, state, git, dry_run,
                    forgejo_api_fn) -> list[Result]:
    full = forgejo_full_name(t.forgejo_url)
    try:
        meta = forgejo_api_fn("GET", f"/repos/{full}", fj_token)
        is_mirror = bool(meta.get("mirror"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return [Result(t.name, b, NO_COPY, f"no Forgejo copy of {full}; create it with "
                                               "tools/forgejo_repo_rollout.py") for b in branches]
        return [Result(t.name, b, ERROR, _redact(f"forgejo repo lookup failed: {e!r}")) for b in branches]
    except Exception as e:  # noqa: BLE001
        return [Result(t.name, b, ERROR, _redact(f"forgejo repo lookup failed: {e!r}")) for b in branches]
    cache = state / "cache" / f"{t.name}.git"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        r = git(["init", "--quiet", "--bare", str(cache)], cache.parent, _auth_env("", None))
        if r.returncode != 0:
            return [Result(t.name, b, ERROR, f"cache init failed: {r.stderr.strip()[-200:]}") for b in branches]
    gh_blocker = ("" if gh_token else f"no GitHub write credential at {GITHUB_TOKEN_FILE}")
    github = Side("github", github_https(t.github_url) if gh_token else t.github_url,
                  _auth_env("x-access-token", gh_token), can_push=bool(gh_token) and not t.archived,
                  push_blocker="archived on GitHub" if t.archived else gh_blocker)
    forgejo = Side("forgejo", t.forgejo_url, _auth_env(FORGEJO_USER, fj_token), can_push=not is_mirror,
                   push_blocker="still a pull-mirror" if is_mirror else "")
    return [reconcile_branch(
        repo=t.name, branch=b, github=github, forgejo=forgejo, cache=cache, git=git,
        forgejo_is_mirror=is_mirror,
        mirror_sync=lambda: forgejo_api_fn("POST", f"/repos/{full}/mirror-sync", fj_token),
        dry_run=dry_run, github_archived=t.archived) for b in branches]


def main(argv: list[str] | None = None, **run_kw) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", action="append", help="limit to this manifest repo (repeatable)")
    ap.add_argument("--branch", action="append",
                    help="branch to sync (default: each repo's GitHub default branch)")
    ap.add_argument("--dry-run", action="store_true", help="classify only; no push, sync or alert")
    ap.add_argument("--no-alert", action="store_true", help="never mail (manual runs)")
    ap.add_argument("--findings-exit-zero", action="store_true",
                    help="exit 0 on FINDINGS once they are mailed (deduped), so the unit's "
                         "OnFailure does not re-page a standing divergence every fire. Config "
                         "failures (2) and undeliverable alerts (3) still exit non-zero.")
    a = ap.parse_args(argv)
    lock = _state_dir() / "run.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _log("another run holds the lock; skipping")
            return 0
        rc = run(only=a.repo, branches=a.branch, dry_run=a.dry_run,
                 alert=not a.no_alert, **run_kw)
    mailed = not a.no_alert and not a.dry_run
    if rc == EXIT_FINDINGS and a.findings_exit_zero and mailed:
        return 0
    return rc


if __name__ == "__main__":
    sys.exit(main())
