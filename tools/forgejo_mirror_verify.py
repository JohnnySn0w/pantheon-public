#!/usr/bin/env python3
"""forgejo_mirror_verify — prove the github-mirror repos actually hold what they claim (T-2217).

THE FAILURE THIS EXISTS FOR, in the ticket's own words: "an empty mirror repo and a quiet
healthy mirror look IDENTICAL from the repo list: both show a name and no recent activity."
Creating a mirror is a PROXY. The defining surfaces are REFS and the LAST-SYNC TIMESTAMP.
A forge full of repos that were created and never pulled passes every cheap check and holds
nothing — the same shape as T-2216 (mailbox.send provisions an empty maildir at any path and
reports success).

TWO TIERS, because the second is not always available:

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
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

FORGE = "https://git.snowis.me"
ORG = "github-mirror"
UPSTREAM = "https://github.com/example-org/{repo}.git"
TOKEN_FILE = "~/.forgejo-mirror.token"
# T-2694: the least-privilege Contents:read GitHub PAT (key=value form). Used ONLY
# to ls-remote PRIVATE upstreams so the private mirrors get ref-parity, not just
# sync-liveness. Absent → private repos stay sync-liveness-only (graceful).
GITHUB_PAT_FILE = "~/forgejo.tok"
STALE_MULTIPLE = 2.5   # allow 2.5 intervals before calling a mirror dead

OK, DRIFT, STALE, HOLLOW, UNKNOWN = "OK", "DRIFT", "STALE", "HOLLOW", "UNKNOWN"


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
        cmd += ["-c", f"credential.helper=store --file={token}"]
    if pat:
        # Reset any inherited helper for this host, then set our env-reading one.
        env = {**os.environ, "PANTHEON_GH_PAT": pat}
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
             *, now: datetime) -> tuple[str, str]:
    """Return (verdict, detail). Pure — all I/O is done by the caller, so this is testable."""
    name = repo.get("name", "?")
    if not repo.get("mirror"):
        return UNKNOWN, f"{name}: not a mirror"

    # --- tier 1: did the pull actually RUN? ---
    stamp = repo.get("mirror_updated")
    interval = parse_interval(repo.get("mirror_interval", ""))
    if stamp and interval:
        try:
            last = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            age = (now - last).total_seconds()
            if age > interval * STALE_MULTIPLE:
                return STALE, (f"{name}: last sync {int(age//3600)}h ago, interval "
                               f"{interval//3600}h — configured but not syncing")
        except ValueError:
            pass

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
    # Real divergence: the mirror is MISSING refs the upstream has.
    tail = f" (+{len(extra)} extra retained)" if extra else ""
    return DRIFT, f"{name}: {len(missing)} upstream ref(s) MISSING from mirror{tail}"


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
        repos = _api(f"/orgs/{ORG}/repos?limit=200", token)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"forge API unreachable: {e}", file=sys.stderr)
        return 65

    # T-2694: the Contents:read PAT lets us ls-remote PRIVATE upstreams for
    # ref-parity. Absent → private repos degrade to sync-liveness-only (graceful).
    gh_pat = read_github_pat()

    now = datetime.now(timezone.utc)
    results, bad = [], 0
    for repo in repos:
        name = repo["name"]
        # Public upstreams ls-remote anonymously; private ones need the PAT.
        up = ref_set(UPSTREAM.format(repo=name),
                     pat=gh_pat if repo.get("private") else None)
        mir = ref_set(f"{FORGE}/{ORG}/{name}.git", token=cred)
        verdict, detail = classify(repo, up, mir, now=now)
        results.append({"repo": name, "verdict": verdict, "detail": detail})
        if verdict in (DRIFT, STALE, HOLLOW):
            bad += 1
        if not (args.quiet_ok and verdict == OK):
            print(f"  {verdict:<7} {detail}")

    if args.json:
        print(json.dumps(results, indent=1))
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(f"-- {len(results)} mirror(s): " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
