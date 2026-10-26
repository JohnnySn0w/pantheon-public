#!/usr/bin/env python3
"""forgejo_mirror_verify — prove the github-mirror repos actually hold what they claim (T-2217).

THE FAILURE THIS EXISTS FOR, in the ticket's own words: "an empty mirror repo and a quiet
healthy mirror look IDENTICAL from the repo list: both show a name and no recent activity."
Creating a mirror is a PROXY. The defining surfaces are REFS and the LAST-SYNC TIMESTAMP.
A forge full of repos that were created and never pulled passes every cheap check and holds
nothing — the same shape as T-2216 (mailbox.send provisions an empty maildir at any path and
reports success).

THREE TIERS (tier 0 added T-2881; tiers 1–2 are the original T-2217 checks):

  VISIBILITY-PARITY (tier 0, T-2881 — only meaningful for PUBLIC mirrors). Forgejo
  pull-mirrors sync CONTENT, not VISIBILITY. A repo flipped private on GitHub leaves
  its mirror PUBLIC = a silent, anon-viewable exposure the ref/sync checks cannot
  see. Compare the mirror's `private` flag to the upstream GitHub `private` and flag
  ONLY the exposure direction (public-on-forge + private-on-github). Defense-in-depth
  alongside T-2009's REQUIRE_SIGNIN_VIEW=TRUE, not a replacement; fail-safe (never
  flags when upstream visibility is unreadable).

The original two tiers, because the second is not always available:

  SYNC-LIVENESS (always checkable, no GitHub credential needed). Forgejo exposes
  `mirror_updated` — when the pull ACTUALLY RAN, not when it was configured. Stale beyond a
  multiple of the repo's own `mirror_interval` means the mirror is configured and dead. This
  tier works for PRIVATE mirrors too, which matters because this runs as recovery and recovery holds
  no GitHub credential.

  REF-PARITY (only when the upstream is readable). Compare the full ref set, not just HEAD:
  a mirror can share a HEAD and be missing every tag.

DESIGN NOTES EARNED THE HARD WAY TODAY:

  - EMPTY UPSTREAM MIRRORS AS EMPTY, AND THAT IS A PASS. `data-mapping` has zero refs on
    GitHub; my first verifier called it VERIFY FAILED. A check that permanently fails on a
    legitimately-empty repo is one somebody eventually disables — and then there is no check.
    The assertion is ref-set EQUALITY, never "mirror is non-empty".
  - UNREADABLE UPSTREAM IS *UNKNOWN*, NOT FAILURE. Once private mirrors exist, recovery cannot
    ls-remote them. Reporting DRIFT there would be a false alarm on 40 repos at once.
  - The staleness threshold is DERIVED from each repo's own `mirror_interval`, not hardcoded,
    so changing the interval cannot silently invalidate the check.
  - TRANSIENT NEW-REF LAG IS A WARN, NOT AN ALERT (T-2879). A repo under active dev pushes a
    ref between the mirror's scheduled pulls; for one cycle the mirror is missing it. That is
    benign lag the next sync fixes, but the tool exits 1 on ANY missing ref, so a fast-churning
    repo daily-alerted on it. So: a mirror syncing ON SCHEDULE (age <= its own interval) that
    shows active churn (retained upstream-deleted refs) and is missing only a SMALL number of
    refs → WARN (printed, not exit-1). A QUIET repo (no churn), a STALE mirror, or a large drift
    still DRIFTs. Verified self-resolving: echo-prismatic's 2026-08-11 drift was OK by 2026-08-24.
  - PURE-ADDITION LAG NEEDS STATE, NOT A CHURN PROXY (T-3114). T-2879's grace is stateless and
    keys off retained upstream-deleted refs — a repo that only ADDS branches (never deletes) has
    no such signal, so transient new-ref lag still DRIFTed (the codex-config case, 2026-08-24).
    The general fix persists per-repo {mirror_updated, missing-refs} across runs (main(), not
    classify() — which stays pure): a missing ref that SURVIVED a sync (mirror_updated advanced
    yet the ref is still absent) is a real gap → DRIFT; one not yet through a sync is transient →
    WARN. Handles addition + deletion churn uniformly. COST (ticket-sanctioned): a quiet repo's
    real gap is delayed ONE sync — first seen as WARN, escalated to DRIFT once a sync leaves it
    still missing. State fail-opens: a lost/corrupt state file just resets everyone to a fresh
    1-cycle grace, never crashes the run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

FORGE = "https://git.your-self-hosted-git.example"
ORG = "github-mirror"
# T-4690: name the identity in the url. Since T-4051 ~/.git-credentials-forgejo holds two
# git.your-self-hosted-git.example identities and git-credential-store answers with the FIRST match, which is
# pantheon-fleet (reads pantheon-config only). Every other mirror then read "not found",
# so this verifier reported UNKNOWN=50 every night from at least 2026-10-02, with exit 0.
MIRROR_USER = "pantheon-mirror"
PAGE = 50  # Forgejo's maximum page size
MAX_PAGES = 40  # 2000 repos; a pager that never returns a short page must not loop forever
UPSTREAM = "https://github.com/example-org/{repo}.git"
TOKEN_FILE = "~/.forgejo-mirror.token"
# T-2694: the least-privilege Contents:read GitHub PAT (key=value form). Used ONLY
# to ls-remote PRIVATE upstreams so the private mirrors get ref-parity, not just
# sync-liveness. Absent → private repos stay sync-liveness-only (graceful).
GITHUB_PAT_FILE = "~/forgejo.tok"
# T-2881: the upstream owner + REST endpoint for the visibility cross-check. Same
# owner the mirrors pull from (UPSTREAM above); the metadata GET reuses read_github_pat.
GITHUB_OWNER = "example-org"
GITHUB_API_REPO = "https://api.github.com/repos/{owner}/{repo}"
STALE_MULTIPLE = 2.5   # allow 2.5 intervals before calling a mirror dead
# T-2879: a mirror that IS syncing on-schedule but is MISSING a small number of
# upstream refs, on a repo showing active branch churn, is almost always transient
# new-ref lag (the upstream pushed a ref after the mirror's last pull; the next
# sync gets it) — not a real gap. That ONE case is downgraded DRIFT→WARN so a
# fast-churning repo mid-dev stops crying wolf daily. A quiet repo (no churn), a
# stale mirror, or a large drift still alerts. This is the tunable ceiling on
# "small" — a drift larger than this always DRIFTs even under active churn.
TRANSIENT_LAG_MAX_MISSING = 2

OK, DRIFT, STALE, HOLLOW, UNKNOWN, EXPOSED, WARN = (
    "OK", "DRIFT", "STALE", "HOLLOW", "UNKNOWN", "EXPOSED", "WARN")


def read_github_pat(path: str = GITHUB_PAT_FILE) -> str | None:
    """Return the Contents:read GitHub PAT (`key=value`; split on '='), or None.

    None is a graceful outcome, NOT an error: a private upstream then reads as
    UNREADABLE and classify() falls back to sync-liveness-only — exactly the
    pre-T-2694 behaviour. So a missing/rotated PAT degrades coverage, never breaks
    the daily verify run."""
    try:
        raw = open(os.path.expanduser(path)).read().strip()
    except OSError:
        return None
    val = raw.split("=", 1)[1].strip() if "=" in raw else raw.strip()
    return val or None


def _api(path: str, token: str, *, opener=urllib.request.urlopen):
    # An explicit User-Agent is REQUIRED, not cosmetic: the forge rejects urllib's default
    # `Python-urllib/3.x` with 403 (bot filter, in Forgejo or the Caddy front). Measured —
    # the identical request differs only by this header: without it 403, with it 200. The
    # failure looks exactly like a missing token scope, which is the wrong thing to go fix.
    req = urllib.request.Request(
        f"{FORGE}/api/v1{path}",
        headers={"Authorization": f"token {token}",
                 "User-Agent": "pantheon-recovery-mirror-verify/1.0"})
    with opener(req, timeout=30) as r:
        return json.loads(r.read().decode())


def github_repo_private(name: str, pat: str | None,
                        *, opener=urllib.request.urlopen) -> bool | None:
    """Return the upstream GitHub repo's `private` flag, or None if UNREADABLE.

    T-2881 visibility cross-check. None (no PAT / 404 / network / parse error) is a
    GRACEFUL 'unknown' the caller must NOT treat as a finding — never flag on an
    unknown (fail-safe, same posture as the UNKNOWN ref fallback). Read-only GET,
    reusing the T-2089 answer's cross-check shape: Bearer PAT + `Accept:
    application/vnd.github+json` against api.github.com/repos/<owner>/<name>.

    KNOWN LIMITATION (why this is defense-in-depth, not a sole guard — per the
    ticket + the operator's T-2009 REQUIRE_SIGNIN_VIEW ruling): a repo the PAT cannot
    see returns 404 → None → not flagged. In practice the read_github_pat PAT DOES
    see the private upstreams (it already ls-remotes them for ref-parity, T-2694),
    so a flip-to-private is visible here; but a PAT that lost access would silently
    downgrade this check to no-op rather than false-alarm."""
    if not pat:
        return None
    req = urllib.request.Request(
        GITHUB_API_REPO.format(owner=GITHUB_OWNER, repo=name),
        headers={"Authorization": f"Bearer {pat}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "pantheon-recovery-mirror-verify/1.0",
                 "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with opener(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    val = data.get("private")
    return val if isinstance(val, bool) else None


def parse_interval(text: str) -> int:
    """'24h0m0s' -> seconds. Returns 0 when unparseable (caller treats as unknown)."""
    total, num = 0, ""
    for ch in (text or ""):
        if ch.isdigit():
            num += ch
        elif ch in "hms" and num:
            total += int(num) * {"h": 3600, "m": 60, "s": 1}[ch]
            num = ""
        else:
            num = ""
    return total


_CRED_USER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MAX_CRED_FILE_BYTES = 64 * 1024


def store_password(path: str, host: str, user: str) -> str | None:
    """The password for `user`@`host` in a git-credentials store file, or None.

    T-5067: the store helper answers with the FIRST line matching the host once git drops the
    URL's username after a 401, and `git credential approve` reorders the file. So reading
    through the helper made the identity depend on line order. Here the line is picked by
    host AND username, whatever its position."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_MAX_CRED_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_CRED_FILE_BYTES:
        return None
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            parts = urllib.parse.urlsplit(line.strip())
        except ValueError:
            continue
        if (parts.hostname == host and parts.username is not None and parts.password
                and urllib.parse.unquote(parts.username) == user):
            return urllib.parse.unquote(parts.password)
    return None


def ref_set(url: str, *, token: str | None = None, pat: str | None = None,
            run=subprocess.run) -> set[str] | None:
    """Full ref set via ls-remote. None means UNREADABLE — never an empty set.

    `token` is a git-credentials FILE path (the forge mirror uses it). `pat` (T-2694)
    is a raw GitHub PAT for a PRIVATE upstream — supplied to an inline, github.com-
    SCOPED credential helper via ENV so it never lands on argv (ps-visible) or on
    disk. `ls-remote --heads --tags` already excludes GitHub's synthetic refs/pull/*
    PR refs, so the comparison against the forge mirror stays like-for-like."""
    cmd = ["git"]
    env = None
    if token:
        # T-5067: name the credential explicitly instead of asking the store helper, whose
        # answer depends on line order. Reset every inherited helper (the global list and
        # ~/.gitconfig's host-scoped credential.https://git.your-self-hosted-git.example.helper), then set a
        # host-scoped one that echoes the URL's user and reads the password from env (never argv).
        parts = urllib.parse.urlsplit(url)
        user = urllib.parse.unquote(parts.username or "")
        if not parts.hostname or not _CRED_USER_RE.fullmatch(user):
            return None
        password = store_password(token, parts.hostname, user)
        if password is None:
            return None
        scope = f"credential.{parts.scheme}://{parts.hostname}.helper"
        env = {**os.environ, "PANTHEON_FORGE_PW": password, "GIT_TERMINAL_PROMPT": "0"}
        cmd += ["-c", "credential.helper=", "-c", f"{scope}=",
                "-c", f'{scope}=!f() {{ echo username={user}; echo "password=$PANTHEON_FORGE_PW"; }}; f']
    if pat:
        # Reset any inherited helper for this host, then set our env-reading one.
        env = {**(env or os.environ), "PANTHEON_GH_PAT": pat}
        cmd += [
            "-c", "credential.https://github.com.helper=",
            "-c", ("credential.https://github.com.helper=!f() { echo username=x-access-token; "
                   'echo "password=$PANTHEON_GH_PAT"; }; f'),
        ]
    cmd += ["ls-remote", "--heads", "--tags", url]
    try:
        r = run(cmd, capture_output=True, text=True, timeout=90, cwd="/tmp", env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out = set()
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            # T-2694: compare by ref NAME, NOT sha+name. A pull-mirror on a 24h
            # interval is legitimately a few commits behind an actively-committed
            # upstream between syncs; sha-parity would flag that as DRIFT every day
            # (1 missing + 1 extra for the moved branch) and cry wolf on exactly the
            # busiest repos — the failure this tool's docstring exists to avoid. The
            # "mirror stopped syncing" case is already caught by the tier-1 STALE
            # check (mirror_updated age), and a branch/tag genuinely added or removed
            # upstream but not on the mirror IS a name difference and still DRIFTs.
            # This matches the T-2217 scratch verifier that proved 40/40 by name.
            out.add(parts[1])
    return out


def classify(repo: dict, upstream: set[str] | None, mirrored: set[str] | None,
             *, now: datetime, upstream_private: bool | None = None) -> tuple[str, str]:
    """Return (verdict, detail). Pure — all I/O is done by the caller, so this is testable.

    `upstream_private` (T-2881) is the GitHub `private` flag from github_repo_private(),
    or None when unreadable. It powers the tier-0 visibility-parity check below."""
    name = repo.get("name", "?")
    if not repo.get("mirror"):
        return UNKNOWN, f"{name}: not a mirror"

    # --- tier 0: visibility parity (T-2881) — the EXPOSURE direction only ---
    # Forgejo pull-mirrors sync CONTENT, not VISIBILITY: a repo flipped private on
    # GitHub leaves an anon-viewable PUBLIC mirror behind. That is the one drift
    # direction that is a security exposure, so it outranks ref/sync health for the
    # same repo (fix the exposure first; the ref checks resume next run). The reverse
    # — a PRIVATE mirror of a PUBLIC upstream — is benign over-privacy and is NOT
    # flagged. upstream_private is None when GitHub visibility is unreadable (no PAT /
    # API error) → do NOT flag (fail-safe, never cry wolf on an unknown).
    if upstream_private is True and not repo.get("private"):
        return EXPOSED, (f"{name}: PUBLIC on forge but PRIVATE on github — visibility "
                         f"drift = anon-viewable exposure; make the mirror private")

    # --- tier 1: did the pull actually RUN? ---
    stamp = repo.get("mirror_updated")
    interval = parse_interval(repo.get("mirror_interval", ""))
    age = None  # seconds since the pull last RAN; stays None when unparseable
    if stamp and interval:
        try:
            last = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            age = (now - last).total_seconds()
            if age > interval * STALE_MULTIPLE:
                return STALE, (f"{name}: last sync {int(age//3600)}h ago, interval "
                               f"{interval//3600}h — configured but not syncing")
        except ValueError:
            age = None

    # --- tier 2: ref parity, only when the upstream is readable ---
    if upstream is None:
        return UNKNOWN, f"{name}: upstream unreadable (no credential?) — sync-liveness only"
    if mirrored is None:
        return UNKNOWN, f"{name}: mirror unreadable"
    if upstream == mirrored:
        # Equality covers the empty-upstream case: both empty is a PASS, not a failure.
        return OK, f"{name}: {len(upstream)} refs match"
    if upstream and not mirrored:
        return HOLLOW, (f"{name}: upstream has {len(upstream)} refs, mirror has NONE — "
                        f"created and never pulled")
    missing = upstream - mirrored
    extra = mirrored - upstream
    if not missing:
        # The mirror holds EVERY upstream ref, plus {extra} it kept after upstream
        # deleted them — Forgejo pull-mirrors do not prune deleted refs by default
        # (T-2694: e.g. echo-prismatic retained a merged agent branch + a closed-PR
        # canary branch). A backup mirror holding MORE than upstream is not a failure
        # — only holding LESS (missing upstream data) is. Alerting on retained refs
        # would DRIFT every repo as branches churn — the cry-wolf failure this tool
        # exists to avoid. So this is OK, with the retained count noted.
        return OK, f"{name}: all {len(upstream)} upstream refs present (+{len(extra)} retained upstream-deleted)"
    tail = f" (+{len(extra)} extra retained)" if extra else ""
    # T-2879: transient new-ref lag vs a real gap. When the mirror is syncing ON
    # SCHEDULE (age <= its OWN interval — a STRICTER bar than the tier-1 2.5x STALE
    # threshold), the repo shows active branch churn (extra upstream-deleted refs
    # retained = branches being created/deleted), and only a SMALL number of refs
    # are missing, this is almost certainly the upstream having pushed a ref since
    # the mirror's last pull — the next sync resolves it. Downgrade THAT case to
    # WARN (surfaced in the output, but not a bad/exit-1 alert) so a fast-churning
    # repo mid-dev stops crying wolf daily. Everything else still DRIFTs: a STALE
    # mirror (caught at tier 1, untouched), a QUIET repo with no churn (extra is
    # empty), or a drift larger than the ceiling. The extra-ref-retention accounting
    # (the +N tail) and the sync-staleness check are unchanged.
    sync_on_schedule = bool(interval) and age is not None and age <= interval
    if sync_on_schedule and extra and len(missing) <= TRANSIENT_LAG_MAX_MISSING:
        return WARN, (f"{name}: {len(missing)} upstream ref(s) not yet mirrored{tail} "
                      f"— likely transient new-ref lag (synced {int(age//3600)}h ago "
                      f"< {interval//3600}h interval, active churn); self-resolves next sync")
    # Real divergence: the mirror is MISSING refs the upstream has.
    return DRIFT, f"{name}: {len(missing)} upstream ref(s) MISSING from mirror{tail}"


# --- T-3114: state-based 1-cycle grace for PURE-ADDITION transient new-ref lag ---
# T-2879's grace is STATELESS: it needs a churn signal (retained upstream-deleted
# refs) to tell transient lag from a real gap. A PURE-ADDITION repo — one that only
# pushes NEW branches and never deletes — has NO retained-extra signal, so a ref
# pushed since the mirror's last pull conservatively reads as DRIFT (the live
# codex-config case, 2026-08-24). The complete, churn-shape-independent fix persists
# per-repo state across runs: a missing ref that SURVIVED a sync (mirror_updated
# advanced yet the ref is STILL absent) is a real gap → DRIFT; a missing ref not yet
# through a sync is transient → WARN (a 1-cycle grace). This handles pure-addition AND
# deletion churn uniformly with no fragile activity proxy. DOCUMENTED COST (ticket-
# sanctioned): a QUIET repo's real gap is delayed one sync — its first observation is
# WARN, and it escalates to DRIFT once a sync runs with the ref still missing.

STATE_FILE = "forgejo_mirror_verify_state.json"


def _state_path() -> str:
    """Per-run state file: $PANTHEON_STATE_DIR else ~/.local/state/pantheon/."""
    base = os.environ.get("PANTHEON_STATE_DIR") or os.path.expanduser(
        "~/.local/state/pantheon")
    return os.path.join(base, STATE_FILE)


def load_state(path: str) -> dict:
    """{repo: {"mirror_updated": <stamp|None>, "missing": [refs]}}. Fail-open to {}
    on ANY fault (missing/unreadable/malformed) — a lost state file must degrade to
    'no grace history' (everything gets a fresh 1-cycle grace), never crash the run."""
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: str, state: dict) -> None:
    """Atomically persist state (tmp + os.replace). Best-effort: a write failure is
    logged to stderr but never fails the verify run (the grace just resets next run)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        print(f"warning: could not persist mirror-verify state to {path}: {e}", file=sys.stderr)


def apply_transient_grace(name: str, verdict: str, detail: str, *, missing: set[str],
                          mirror_updated: str | None, prev: dict | None) -> tuple[str, str, dict]:
    """Wrap a classify() result with the T-3114 state-based 1-cycle grace and return
    (verdict, detail, new_state_entry). Only DRIFT is reconsidered — every other verdict
    passes through unchanged (its current missing-set is still recorded for next run).

    A missing ref is a REAL gap iff it was ALSO missing last run AND a sync has run since
    (mirror_updated advanced) — i.e. it SURVIVED a sync while still absent. Otherwise it is
    within its grace cycle (newly missing, or no sync yet) → WARN. Without a mirror_updated
    stamp we cannot reason about 'survived a sync', so we do NOT grant grace (keep DRIFT) —
    the safe direction for a backup mirror is to alert, never to silence indefinitely."""
    new_entry = {"mirror_updated": mirror_updated, "missing": sorted(missing)}
    if verdict != DRIFT:
        return verdict, detail, new_entry
    if not mirror_updated:
        return verdict, detail, new_entry  # no sync stamp → cannot confirm grace; keep DRIFT
    prev_missing = set(prev.get("missing", [])) if isinstance(prev, dict) else set()
    prev_stamp = prev.get("mirror_updated") if isinstance(prev, dict) else None
    synced_since = mirror_updated != prev_stamp
    real = (missing & prev_missing) if synced_since else set()
    if real:
        return DRIFT, (f"{name}: {len(real)} upstream ref(s) MISSING from mirror — "
                       f"survived a sync (real gap)"), new_entry
    return WARN, (f"{name}: {len(missing)} upstream ref(s) not yet mirrored — within the "
                  f"1-cycle grace (not yet through a sync); self-resolves or escalates next run"), new_entry


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verify github-mirror repos by refs + sync time.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet-ok", action="store_true", help="only print non-OK verdicts")
    args = ap.parse_args(argv)

    import os
    tf = os.path.expanduser(TOKEN_FILE)
    cred = os.path.expanduser("~/.git-credentials-forgejo")
    try:
        token = open(tf).read().strip()
    except OSError as e:
        print(f"cannot read {tf}: {e}", file=sys.stderr)
        return 64

    try:
        # T-4690: Forgejo caps a page at 50 whatever `limit` asks for, so `limit=200` silently
        # saw only the first 50 repos. Page until a short (or empty) page.
        repos, page = [], 1
        while True:
            batch = _api(f"/orgs/{ORG}/repos?limit={PAGE}&page={page}", token) or []
            repos += batch
            if len(batch) < PAGE:
                break
            if page >= MAX_PAGES:
                raise ValueError(f"org pager passed {MAX_PAGES} pages without a short page")
            page += 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"forge API unreachable: {e}", file=sys.stderr)
        return 65

    # T-2694: the Contents:read PAT lets us ls-remote PRIVATE upstreams for
    # ref-parity. Absent → private repos degrade to sync-liveness-only (graceful).
    gh_pat = read_github_pat()

    # T-3114: load the per-repo grace state (missing-ref sets + last mirror_updated).
    state_path = _state_path()
    prev_state = load_state(state_path)
    new_state: dict = {}

    now = datetime.now(timezone.utc)
    results, bad = [], 0
    for repo in repos:
        name = repo["name"]
        # Public upstreams ls-remote anonymously; private ones need the PAT.
        up = ref_set(UPSTREAM.format(repo=name),
                     pat=gh_pat if repo.get("private") else None)
        mir = ref_set(f"https://{MIRROR_USER}@{FORGE.split('://', 1)[1]}/{ORG}/{name}.git", token=cred)
        # T-2881: only a PUBLIC mirror can be an exposure — skip the GitHub
        # metadata GET for private mirrors (a private mirror is safe regardless of
        # upstream visibility, and it saves an API call per private repo).
        upstream_private = github_repo_private(name, gh_pat) if not repo.get("private") else None
        verdict, detail = classify(repo, up, mir, now=now, upstream_private=upstream_private)
        # T-3114: state-based 1-cycle grace — only when we have a real ref comparison
        # (both sides readable). On an UNREADABLE run, carry the prior grace state
        # forward untouched so a transient outage doesn't reset the grace history.
        if up is not None and mir is not None:
            verdict, detail, entry = apply_transient_grace(
                name, verdict, detail, missing=up - mir,
                mirror_updated=repo.get("mirror_updated"), prev=prev_state.get(name))
            new_state[name] = entry
        elif name in prev_state:
            new_state[name] = prev_state[name]
        results.append({"repo": name, "verdict": verdict, "detail": detail})
        if verdict in (DRIFT, STALE, HOLLOW, EXPOSED) or detail.endswith(": mirror unreadable"):
            # T-4690: an unreadable MIRROR is our own credential/forge failing, so the check is
            # blind: loud, not UNKNOWN-and-exit-0. (An unreadable UPSTREAM stays benign UNKNOWN.)
            bad += 1
        if not (args.quiet_ok and verdict == OK):
            print(f"  {verdict:<7} {detail}")

    # Persist the refreshed grace state (repos absent this run are pruned).
    save_state(state_path, new_state)

    if args.json:
        print(json.dumps(results, indent=1))
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(f"-- {len(results)} mirror(s): " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
