#!/usr/bin/env python3
"""tools/fleet_clone_currency_check.py — fleet clone-currency check (T-2798, T-2794 option-b).

Consume controllers run tools/persona_bringup.py from each SEAT'S OWN
pantheon-config clone, so a stale clone runs stale tooling. T-2743 found
suborch1/mason 332 behind origin/main and suborch2/artisan 156 behind, with NO
auto-pull and NO discovery mechanism — the drift sat unnoticed for weeks. The
T-2794 design annotation picked option (b): a READ-ONLY currency CHECK with a
drift alert, not auto-pull and not pull-on-fire — remediation (a supervised
ff-pull, the T-2743 pattern) stays a human/HO act; this tool only closes the
DISCOVERY gap.

Per (seat, repo), read-only: resolve HEAD (own account direct; every other
account via `sudo -S -k -u <account>`, password piped from /home/claude/root.env
— orchestrator has no NOPASSWD cross-account sudo, unlike recovery's monitors, which is why
this port of the T-2743/persona_cutover.sh recon pattern needs the password leg
at all) and compute commits-behind against the RUNNER's OWN clone of that repo
(origin/<default-branch>; never a second command against the seat's clone).
NEVER mutates a seat clone — the only command that ever reaches one is
`git rev-parse HEAD`.

T-4061 (2026-09-28): generalized from the single pantheon-config clone to EVERY
repos.yaml `always_available` repo (pantheon-config + pantheon-inventory today),
reporting per (seat, repo). A seat's clone of repo R lives at ~/{R} by
convention (both current repos follow it — no clone_path field is added to
repos.yaml). The runner's own sibling clone of R (REPO_ROOT.parent / R, an
injectable path for tests) supplies the compare ref; its origin default branch
is resolved via `symbolic-ref refs/remotes/origin/HEAD` (pantheon-config is
main; pantheon-inventory is Forgejo and may differ), falling back to "main".

T-4116 (2026-09-28): a new EMPTY_CLONE verdict is added to the vocabulary
(CURRENT/DRIFTED/NO_CLONE/UNKNOWN/PENDING_PUSH/SKIPPED/EMPTY_CLONE) for the
UNBORN-clone case — see below.

T-4143 (2026-09-28): branch-aware. The single seat command is now
`rev-parse HEAD --abbrev-ref HEAD` (still one read-only rev-parse), and a clone
checked out on anything but the repo's default branch (or detached) is
  OFF_MAIN — alerted regardless of commits-behind: the seat's units run that
             branch's tooling. Never auto-switched (the persona's working clone).
Flagged rows (OFF_MAIN/DRIFTED) name the HEAD commit's age, read in the
runner's clone. Alerts now also reach Recovery (a copy) and each flagged seat's own
persona inbox (only its rows); those copies are best-effort. Origin: consultant's
clone sat on a month-old agent branch while its mailbox-wake ran that branch's
submit_verify. This tool saw it (7811 behind), but a skip-list entry claiming
"not a live-pull clone" hid it. That entry is retired.

Verdicts, not a boolean (ci_fleet_account_reconcile / ci_audit_liveness
house style — a monitor whose broken state reads as healthy reproduces the
exact T-2743 discovery gap this tool exists to close):
  CURRENT  — within the drift threshold.
  DRIFTED  — behind-count exceeds the threshold (default 20).
  NO_CLONE — the account has no pantheon-config clone at all (expected for
             seats outside the clone-bearing set, e.g. the mailbox hub).
  EMPTY_CLONE — the clone exists (git-init'd, remote + credentials present) but
             its HEAD is UNBORN: no commits yet (e.g. provisioned before the
             repo had content, with no `git pull` since — T-4051/T-4052/T-4116).
             `git rev-parse HEAD` fails ("Needed a single revision" / "unknown
             revision"), which pre-T-4116 conflated it with UNKNOWN; it is now
             disambiguated (a real git repo whose HEAD is a symref to an unborn
             branch) and carries an actionable pull remedy. Alerted like DRIFTED
             — a seat with an empty clone is not trustworthy — but DISTINCT from
             an un-diagnosable read failure.
  UNKNOWN  — could not be verified (sudo/auth failure, timeout, unresolvable
             sha, corruption). Alerted exactly like DRIFTED — an unverifiable
             seat is the same discovery-gap risk as a confirmed-stale one.
             Kept for GENUINE read failures only; an unborn clone is EMPTY_CLONE.

Seats are DERIVED from agent_role_map.fleet_accounts() — every bound account,
not a hand-carried list — so a new seat is covered automatically the moment
it's bound in instance.yaml; an account with no clone simply reports NO_CLONE
and is never alerted on.

Alerts land in orchestrator's own ORG-026 mailbox (the supervised-pull remediator);
pulls stay supervised, per T-2794's option-(b) decision.

T-3425 (2026-09-05, from T-3009 5-obs threshold hit): per-account
dispositioned-mitigation skip-list. A seat can be listed at
fleet-clone-currency-skip-list.yaml with a disposition_ticket + expected_state
(inbox/active/done); while the ticket sits in that state, DRIFTED/UNKNOWN for
that seat is downgraded to SKIPPED and dropped from the alert. Fail-open at
EVERY step (missing file, parse error, missing/moved disposition ticket, state
mismatch, malformed entry — every failure yields "no skip applied", so a
broken skip-list can never suppress an alert). Skipped seats are still
visible in --dry-run/--json output and in a footer section on any real alert.

T-4061: the skip-list is now repo-aware, backward-compatibly. A skip entry MAY
carry an optional `repo` field: an entry WITH `repo` applies only to that
(account, repo) pair; an entry WITHOUT `repo` applies to that account across
ALL repos (so every pre-T-4061 account-only entry keeps working unchanged). A
repo-specific entry takes precedence over an account-wide entry for its repo.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import agent_role_map as arm  # noqa: E402
from tools.seat_exec import is_own_account, seat_argv  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# The bare-line sudo password for the claude account (T-2743/persona_cutover.sh
# pattern). Env-overridable so tests never touch the real file.
ROOT_ENV_PATH = Path(os.environ.get("PANTHEON_ROOT_ENV_PATH", "/home/claude/root.env"))

# Env-overridable so tests never depend on a live mailbox hub path.
ORCHESTRATOR_INBOX = os.environ.get("PANTHEON_ORCHESTRATOR_INBOX", f"{arm.mailbox_hub()}/orchestrator")

# Drift threshold, commits-behind. "env/flag" per the ticket: env sets the
# default, --threshold overrides per-invocation.
DRIFT_THRESHOLD_DEFAULT = int(os.environ.get("PANTHEON_FLEET_CLONE_DRIFT_THRESHOLD", "20"))

# T-4143: alerts also go to Recovery and to each flagged seat's OWN persona inbox
# (the persona owns the clone; orchestrator stays the supervised-pull remediator).
RECOVERY_INBOX = os.environ.get("PANTHEON_RECOVERY_INBOX", f"{arm.mailbox_hub()}/recovery")

CURRENT, DRIFTED, NO_CLONE, UNKNOWN, SKIPPED, PENDING_PUSH, EMPTY_CLONE, OFF_MAIN = (
    "CURRENT", "DRIFTED", "NO_CLONE", "UNKNOWN", "SKIPPED", "PENDING_PUSH", "EMPTY_CLONE",
    "OFF_MAIN",
)
# Log-column width: the longest verdict label, so a wide verdict (EMPTY_CLONE,
# PENDING_PUSH) never overflows and un-aligns the --check log lines. Derived so
# a future verdict widens the column automatically.
_VERDICT_COL = max(len(v) for v in
                   (CURRENT, DRIFTED, NO_CLONE, UNKNOWN, SKIPPED, PENDING_PUSH, EMPTY_CLONE,
                    OFF_MAIN))
# Verdicts that page (needs_alert) and that a disposition skip may suppress.
_ALERT_VERDICTS = (DRIFTED, UNKNOWN, EMPTY_CLONE, OFF_MAIN)

# git rev-list stderr shapes that indicate "the SHA is unresolvable in the
# runner clone" — i.e. the seat has a local unpushed commit whose object we
# have never fetched. Distinct from every other rev-list failure (e.g. a
# corrupted repo), which stays UNKNOWN.
#
# The real-seat case (a 40-hex-char SHA that we haven't fetched) surfaces as
# `fatal: Invalid revision range <sha>..origin/main` — verified 2026-09-14
# by simulating clone-B-commits-locally-clone-A-runs-rev-list. The other
# markers cover related non-hex-shape inputs git treats the same way.
_UNRESOLVED_SHA_MARKERS = (
    "invalid revision range",
    "unknown revision",
    "not a valid object name",
    "bad revision",
)

# T-3425: per-account dispositioned-mitigation skip-list. Fail-open (missing
# file / parse error / unresolvable expiry-condition = zero skips applied, so
# a broken skip-list can never SUPPRESS an alert — only ever fail to add one).
SKIP_LIST_PATH_DEFAULT = REPO_ROOT / "fleet-clone-currency-skip-list.yaml"
SKIP_LIST_PATH = Path(os.environ.get("PANTHEON_FLEET_CLONE_SKIP_LIST", str(SKIP_LIST_PATH_DEFAULT)))
_TICKET_STATE_DIRS = ("inbox", "active", "done")

# git stderr shapes that mean "this account has no pantheon-config clone at
# all" (path absent, or present but not a repo) — distinct from every other
# failure, which is UNKNOWN (could not verify, not "nothing to verify").
_NO_CLONE_MARKERS = ("no such file or directory", "not a git repository", "cannot change to")


@dataclass(frozen=True)
class Seat:
    persona: str
    account: str
    clone_path: str


def discover_seats() -> list[Seat]:
    """Every fleet seat's (persona, account, clone_path) — derived from
    agent_role_map.fleet_accounts(), never hand-listed. A future account bound
    in instance.yaml is covered the next time this runs, with no code change;
    an account with no pantheon-config clone self-classifies NO_CLONE at
    check time rather than needing an exclusion list here."""
    seats = []
    for account in arm.fleet_accounts():
        persona = arm.account_persona(account) or account
        seats.append(Seat(persona=persona, account=account, clone_path=f"/home/{account}/pantheon-config"))
    return seats


def discover_repos(*, repo_root: Path = REPO_ROOT) -> list[str]:
    """Every repo the fleet-clone-currency check covers — the repos.yaml
    `always_available` set (pantheon-config + pantheon-inventory today), read
    via the shared repo_registry loader so a repo added there is covered the
    next time this runs with no code change.

    Fail-open: if the registry is missing/unreadable/unparseable, or lists no
    always_available repos, this falls back to ["pantheon-config"] — the
    historical single-repo behaviour. A registry we cannot read must never
    silently DROP the pantheon-config check that predates this generalization.
    """
    from tools import repo_registry  # lazy + guarded (yaml import inside)
    registry = repo_registry.load_repo_registry(repo_root)
    if not registry:
        return ["pantheon-config"]
    repos: list[str] = []
    for entry in registry.get("always_available") or []:
        if isinstance(entry, dict) and isinstance(entry.get("repo"), str):
            repos.append(entry["repo"])
    return repos or ["pantheon-config"]


def _read_sudo_password() -> str:
    """The claude account's sudo password, as a bare line (T-2743 convention:
    `tr -d '\\n' < /home/claude/root.env`). Never logged, never returned in an
    error message, and the only caller pipes it via subprocess `input=`."""
    return ROOT_ENV_PATH.read_text().strip()


def _run_seat_git(seat: Seat, git_args: list[str], *,
                  run=subprocess.run) -> subprocess.CompletedProcess | None:
    """Run ONE read-only `git -C <clone> <git_args...>` as the seat user and
    return the CompletedProcess, or None if the sudo password could not be read
    or the exec raised (timeout/OSError). Own account direct; every other
    account via `sudo -S -k -u <account>`, password on stdin (never argv, never
    logged). Read-only by contract — callers pass only inspection verbs
    (`rev-parse` / `symbolic-ref` / `show-ref`); the NoMutationGuarantee test
    enforces that no mutating verb ever reaches a seat clone."""
    argv = seat_argv(seat.account, ["git", "-C", seat.clone_path, *git_args],
                     sudo=("sudo", "-S", "-k", "-u"))
    kwargs: dict = {}
    if not is_own_account(seat.account):
        try:
            password = _read_sudo_password()
        except OSError:
            return None
        kwargs["input"] = password + "\n"
    try:
        return run(argv, capture_output=True, text=True, timeout=20, **kwargs)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _is_unborn_clone(seat: Seat, *, run=subprocess.run) -> bool:
    """True iff the seat clone is an UNBORN repo — a real git repo whose HEAD is
    a symref to a branch that has no commit yet (git-init'd / freshly cloned
    from an empty remote, no `git pull` since — the T-4116 forensic). Only
    called AFTER `rev-parse HEAD` has already failed with a non-NO_CLONE error,
    to disambiguate the unborn case from a genuine read failure. Read-only, two
    inspection probes run as the seat user:

      1. `symbolic-ref -q HEAD` must SUCCEED and point at refs/heads/<b>. A
         POPULATED repo would have made `rev-parse HEAD` succeed (never reaching
         here); a DETACHED HEAD or an unreadable/corrupt .git/HEAD (and a
         sudo/timeout failure) all make this probe fail -> NOT unborn (UNKNOWN).
      2. that branch ref must NOT resolve -- `show-ref --verify --quiet <ref>`
         is non-zero (the branch is unborn, no commit). This is the ticket's
         "refs/heads/<default> vs an empty HEAD" check: it guards against a
         corrupt-but-non-empty repo (a ref present/pointing at a live object)
         being mis-read as empty. Note a valid-sha/missing-object corruption
         leaves `rev-parse HEAD` SUCCEEDING, so it never reaches this probe.

    Any exec/probe failure (None / non-zero symref) is conservatively NOT unborn
    (stays UNKNOWN) — P1: never misclassify a real read failure as EMPTY_CLONE."""
    sr = _run_seat_git(seat, ["symbolic-ref", "-q", "HEAD"], run=run)
    if sr is None or sr.returncode != 0:
        return False
    ref = (sr.stdout or "").strip()
    if not ref.startswith("refs/heads/"):
        return False
    xr = _run_seat_git(seat, ["show-ref", "--verify", "--quiet", ref], run=run)
    if xr is None:
        return False
    # ref does NOT resolve (rc != 0) => the branch is unborn => EMPTY_CLONE.
    return xr.returncode != 0


def _seat_head_ref(seat: Seat, *, run=subprocess.run) -> tuple[str, str | None, str | None]:
    """Read-only `git rev-parse HEAD --abbrev-ref HEAD` for one seat. Returns
    (verdict, head_sha, branch): verdict is "" on success, else NO_CLONE /
    EMPTY_CLONE / UNKNOWN (sha and branch None on failure). The ONE rev-parse
    prints the sha, then the checked-out branch (T-4143): the literal "HEAD" when
    detached, or None if git printed no second line. That single rev-parse is
    the ONLY command sent to a seat clone on the success path; on the FAILURE
    path, EMPTY_CLONE disambiguation adds the two read-only inspection probes in
    `_is_unborn_clone`. Own account direct, every other account via
    `sudo -S -k`, password on stdin (never argv, never logged)."""
    r = _run_seat_git(seat, ["rev-parse", "HEAD", "--abbrev-ref", "HEAD"], run=run)
    if r is None:
        return UNKNOWN, None, None
    if r.returncode == 0:
        lines = (r.stdout or "").split()
        head = lines[0] if lines else ""
        branch = lines[1] if len(lines) > 1 else None
        return ("", head, branch) if head else (UNKNOWN, None, None)
    stderr_lower = (r.stderr or "").lower()
    if any(marker in stderr_lower for marker in _NO_CLONE_MARKERS):
        return NO_CLONE, None, None
    # rev-parse HEAD failed and it is NOT a NO_CLONE marker: disambiguate an
    # UNBORN clone (git-init'd, no commits yet — trivially fixable by a pull)
    # from a genuine read failure (auth/sudo/timeout/corruption — UNKNOWN).
    if _is_unborn_clone(seat, run=run):
        return EMPTY_CLONE, None, None
    return UNKNOWN, None, None


def _seat_head(seat: Seat, *, run=subprocess.run) -> tuple[str, str | None]:
    """(verdict, head_sha) view of `_seat_head_ref`, for callers that don't
    need the branch."""
    verdict, head, _branch = _seat_head_ref(seat, run=run)
    return verdict, head


def _head_age_days(head_sha: str, *, repo_root: Path = REPO_ROOT,
                   run=subprocess.run, now: float | None = None) -> float | None:
    """Days since the seat's HEAD commit, read in the RUNNER's clone (never the
    seat's). None when the sha isn't in the runner clone (an unpushed seat
    commit) or on any failure: the age is detail for a flagged seat, never a
    verdict input."""
    try:
        r = run(["git", "-C", str(repo_root), "show", "-s", "--format=%ct", head_sha],
                capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        committed = int((r.stdout or "").strip())
    except ValueError:
        return None
    return max(0.0, ((time.time() if now is None else now) - committed) / 86400)


def _age_suffix(age_days: float | None) -> str:
    return "" if age_days is None else f"; HEAD commit is {age_days:.1f}d old"


def _resolve_compare_branch(runner_repo_clone: Path, *, run=subprocess.run) -> str:
    """Resolve the RUNNER clone's origin default branch — the ref we compute
    commits-behind against. `git symbolic-ref refs/remotes/origin/HEAD` yields
    e.g. `refs/remotes/origin/main`; strip the `refs/remotes/origin/` prefix.

    Fall back to "main" on ANY failure (no origin/HEAD set, timeout, exec
    error, unexpected shape). pantheon-config is main; pantheon-inventory
    (Forgejo) may differ — so this is resolved, never hardcoded — but "main"
    is the safe default for the historical single repo. Read-only."""
    try:
        r = run(["git", "-C", str(runner_repo_clone), "symbolic-ref", "refs/remotes/origin/HEAD"],
                capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return "main"
    if r.returncode != 0:
        return "main"
    ref = (r.stdout or "").strip()
    prefix = "refs/remotes/origin/"
    if ref.startswith(prefix):
        return ref[len(prefix):] or "main"
    return "main"


def _fetch_origin_main(*, repo_root: Path = REPO_ROOT, branch: str = "main", run=subprocess.run) -> bool:
    """Refresh the RUNNER's own origin/<branch> remote-tracking ref. Read-only
    w.r.t. working state (a fetch never touches HEAD or the working tree) and
    only ever runs against `repo_root` (one of the runner's own clones) —
    never a seat's clone. `branch` defaults to "main" (the historical
    pantheon-config case); T-4061 passes each repo's resolved default branch."""
    try:
        r = run(["git", "-C", str(repo_root), "fetch", "origin", branch],
                capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _ref_exists(ref: str, *, repo_root: Path = REPO_ROOT, run=subprocess.run) -> bool:
    """Does `ref` resolve to a commit in `repo_root`? `rev-parse --verify
    --quiet` returns 0 iff it does, non-zero (quietly) otherwise. Read-only.
    Any exec/timeout failure is treated as "does not resolve" (False)."""
    try:
        r = run(["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet", ref],
                capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def _behind_count(head_sha: str, *, repo_root: Path = REPO_ROOT, branch: str = "main",
                  run=subprocess.run) -> tuple[str, int | None]:
    """`git rev-list --count <head_sha>..origin/<branch>`, run entirely in the
    RUNNER's own clone (repo_root) — the seat's HEAD is just a sha string
    handed to a local command; no second command ever touches the seat's
    clone. `branch` defaults to "main"; T-4061 passes each repo's resolved
    default branch. Returns (verdict, count): verdict is "" on success,
    PENDING_PUSH when the seat's HEAD sha is not in the runner clone (seat has
    a local unpushed commit — self-resolves after the seat pushes + runner
    fetches), else UNKNOWN for every other failure class.

    F2 (T-4061 rebound): a rev-list revision-range error is ambiguous — it can
    mean the HEAD sha is unresolvable (PENDING_PUSH, self-resolving) OR that the
    COMPARE ref origin/<branch> itself is missing (a fetch/branch-resolution
    problem — e.g. _resolve_compare_branch fell back to a wrong "main"). The
    latter must NOT be silently swallowed as PENDING_PUSH (not alerted); it is a
    verify-failure → UNKNOWN (alerted). Disambiguate by checking whether
    origin/<branch> resolves: present ⇒ the HEAD sha is the culprit
    (PENDING_PUSH); absent ⇒ the compare ref is the culprit (UNKNOWN)."""
    try:
        r = run(["git", "-C", str(repo_root), "rev-list", "--count", f"{head_sha}..origin/{branch}"],
                capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return UNKNOWN, None
    if r.returncode != 0:
        stderr_lower = (r.stderr or "").lower()
        if any(marker in stderr_lower for marker in _UNRESOLVED_SHA_MARKERS):
            if not _ref_exists(f"origin/{branch}", repo_root=repo_root, run=run):
                return UNKNOWN, None
            return PENDING_PUSH, None
        return UNKNOWN, None
    try:
        return "", int((r.stdout or "").strip())
    except ValueError:
        return UNKNOWN, None


def check_seat(seat: Seat, *, repo: str = "pantheon-config", threshold: int,
               runner_repo_clone: Path | None = None, branch: str = "main",
               fetch_ok: bool = True,
               head_run=subprocess.run, count_run=subprocess.run,
               age_run=subprocess.run) -> dict:
    """Classify one (seat, repo). Pure decision over the two read-only legs
    above. The seat's clone of `repo` is at ~/{repo} by convention; the
    behind-count is computed in `runner_repo_clone` (the runner's own sibling
    clone of that repo, default REPO_ROOT — the historical single-repo case)
    against origin/`branch`. Every result carries a `repo` field (T-4061).

    F1 (T-4061 rebound): `fetch_ok` reports whether main() successfully
    refreshed the runner clone's origin/<branch> for THIS repo. If the fetch
    FAILED, the runner's local origin/<branch> ref is stale, so a behind-count
    against it would UNDER-report (a genuinely-behind seat could read behind==0
    → false CURRENT — the exact silent-drift miss this tool exists to catch).
    A stale-ref repo therefore classifies UNKNOWN (which needs_alert alerts) —
    mirroring the global fetch-gate's skip semantics PER repo, without skipping
    the whole sweep (the other repo may be fine). NO_CLONE/UNKNOWN from the
    HEAD read still take precedence: those are honest, fetch-independent."""
    seat_clone = f"/home/{seat.account}/{repo}"
    # A per-repo view of the seat: _seat_head reads clone_path + account, so
    # this is the one place the ~/{repo} convention is applied. (discover_seats
    # keeps clone_path=~/pantheon-config for backward compat; the per-repo path
    # is derived here, never stored back onto the discovered Seat.)
    repo_seat = Seat(persona=seat.persona, account=seat.account, clone_path=seat_clone)
    runner_clone = runner_repo_clone if runner_repo_clone is not None else REPO_ROOT

    base = {"persona": seat.persona, "account": seat.account, "repo": repo}
    verdict, head, seat_branch = _seat_head_ref(repo_seat, run=head_run)
    if verdict == EMPTY_CLONE:
        # The clone exists but HEAD is unborn (no commits yet). Distinct,
        # trivially-fixable state — carry the actionable pull remedy inline
        # (substituting this repo's resolved default branch for <default>).
        return {**base, "verdict": verdict,
                "detail": f"clone exists but HEAD is unborn (no commits yet) — "
                          f"remedy: git pull --ff-only origin {branch} as the seat user"}
    if verdict:
        return {**base, "verdict": verdict,
                "detail": f"could not read HEAD ({verdict.lower()})"}
    if seat_branch is not None and seat_branch != branch:
        # T-4143: the seat's units exec tools from whatever this clone has
        # checked out, so a clone parked on a feature branch (or detached) runs
        # that branch's tooling however few commits it trails. Fetch-independent,
        # so it outranks the fetch/behind legs. Behind-count is best-effort detail.
        on = "a detached HEAD" if seat_branch == "HEAD" else f"branch {seat_branch}"
        behind_note = ""
        if fetch_ok:
            bverdict, behind = _behind_count(head, repo_root=runner_clone, branch=branch,
                                             run=count_run)
            behind_note = (f", {behind} commit(s) behind origin/{branch}" if not bverdict
                           else f", behind-count unavailable ({bverdict.lower()})")
        age = _head_age_days(head, repo_root=runner_clone, run=age_run)
        return {**base, "verdict": OFF_MAIN, "head": head, "branch": seat_branch,
                "age_days": age,
                "detail": f"on {on}, not {branch}: HEAD={head[:10]}{behind_note}{_age_suffix(age)}"}
    if not fetch_ok:
        # HEAD read fine, but the runner's compare ref for this repo is stale
        # (its fetch failed) — a behind-count would under-report. Fail loud.
        return {**base, "verdict": UNKNOWN, "head": head,
                "detail": f"HEAD={head[:10]} but runner clone of {repo} could not be refreshed "
                          f"(fetch of origin/{branch} failed) — behind-count against a stale ref "
                          f"would under-report drift; treated as UNKNOWN"}
    verdict, behind = _behind_count(head, repo_root=runner_clone, branch=branch, run=count_run)
    if verdict == PENDING_PUSH:
        return {**base, "verdict": verdict, "head": head,
                "detail": f"HEAD={head[:10]} not in runner clone — seat has a local unpushed commit (self-resolves next cycle after push+fetch)"}
    if verdict:
        return {**base, "verdict": verdict,
                "detail": f"HEAD={head[:10]} but behind-count unavailable"}
    status = DRIFTED if behind > threshold else CURRENT
    detail = f"HEAD={head[:10]}, {behind} commit(s) behind origin/{branch}"
    if status == DRIFTED:
        # Age only for a flagged seat (T-4143 names it); CURRENT seats skip the call.
        age = _head_age_days(head, repo_root=runner_clone, run=age_run)
        return {**base, "verdict": status, "head": head, "behind": behind,
                "age_days": age, "detail": detail + _age_suffix(age)}
    return {**base, "verdict": status, "head": head, "behind": behind, "detail": detail}


def sweep(seats: list[Seat], repo_targets: dict[str, dict] | None = None, *,
          threshold: int, head_run=subprocess.run, count_run=subprocess.run,
          age_run=subprocess.run) -> list[dict]:
    """Classify every (seat, repo). `repo_targets` maps repo name -> a dict
    with `runner_clone` (Path), `branch` (str), and `fetch_ok` (bool: did
    main() refresh that runner clone's compare ref this cycle) — built by
    main() from _resolve_repo_targets() + the per-repo fetch loop. It defaults
    to the historical single-repo case (pantheon-config @ REPO_ROOT, branch
    main, fetch_ok True) so pre-T-4061 callers/tests that pass only seats keep
    working. Returns one result dict per (seat, repo)."""
    if repo_targets is None:
        repo_targets = {"pantheon-config": {"runner_clone": REPO_ROOT, "branch": "main"}}
    results: list[dict] = []
    for repo, tgt in repo_targets.items():
        for s in seats:
            results.append(check_seat(
                s, repo=repo, threshold=threshold,
                runner_repo_clone=tgt.get("runner_clone"), branch=tgt.get("branch", "main"),
                fetch_ok=tgt.get("fetch_ok", True),
                head_run=head_run, count_run=count_run, age_run=age_run))
    return results


def _resolve_repo_targets(repos: list[str], *, runner_clone_base: Path | None = None,
                          branch_run=subprocess.run) -> dict[str, dict]:
    """For each repo, resolve the runner's own sibling clone path and its
    origin default branch. `runner_clone_base` defaults to REPO_ROOT.parent
    (in production the runner's home, where ~/pantheon-config and its siblings
    live) — INJECTABLE so tests can point at a fixture tree, exactly like the
    existing repo_root injection. Read-only."""
    base = runner_clone_base if runner_clone_base is not None else REPO_ROOT.parent
    targets: dict[str, dict] = {}
    for repo in repos:
        clone = Path(base) / repo
        branch = _resolve_compare_branch(clone, run=branch_run)
        targets[repo] = {"runner_clone": clone, "branch": branch}
    return targets


def needs_alert(results: list[dict]) -> list[dict]:
    """DRIFTED (over threshold), UNKNOWN (could not verify) and EMPTY_CLONE (an
    unborn clone) all need attention — an unverifiable OR empty seat is exactly
    the trustworthiness gap this tool exists to catch; NO_CLONE is never a
    problem; SKIPPED is a dispositioned-mitigation carve-out (T-3425) that was
    verified to still apply at check time (fail-open: expired disposition =
    seat re-alerts). PENDING_PUSH is NOT alerted — it self-resolves next cycle
    once the seat pushes and the runner fetches; alerting on a self-resolving
    transient is noise (per T-3709 same-shape FP-reduction reasoning).
    EMPTY_CLONE is DISTINCT from UNKNOWN (T-4116): it carries an actionable pull
    remedy rather than an un-diagnosable read failure, so its per-seat line in
    the alert names the verdict + remedy distinctly. OFF_MAIN (T-4143): the
    clone is parked on another branch or detached, so the seat's units run that
    branch's tooling; alerted regardless of commits-behind."""
    return [r for r in results if r["verdict"] in _ALERT_VERDICTS]


def load_skip_list(path: Path | None = None) -> list[dict]:
    """Load the per-account dispositioned-mitigation skip-list (T-3425).

    Fail-open at every step: a missing file, an unreadable file, a parse
    error, or a malformed root shape all yield []. A skip-list that we
    cannot understand must never silently suppress alerts — the whole
    point of this check is to catch silent drift, so an unreadable
    skip-list defaults to "no skips" (alert normally), never "skip
    everything I can't parse" (suppression by malformation).
    """
    p = path or SKIP_LIST_PATH
    try:
        raw = p.read_text()
    except OSError:
        return []
    try:
        import yaml
        data = yaml.safe_load(raw) or {}
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("skips")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def is_skip_active(entry: dict, *, repo_root: Path = REPO_ROOT) -> tuple[bool, str]:
    """Check whether a skip entry's expiry-condition still holds.

    An entry names a disposition ticket + expected state; the entry is
    "active" iff the ticket file currently lives in the expected state
    directory (tickets/<state>/<id>.md). Any other outcome — missing
    ticket, missing state, mismatched state, malformed entry — is fail-open
    ("expired", so the seat re-alerts). The reason string is included
    verbatim in render_alert's SKIPPED footer for auditability.
    """
    account = entry.get("account")
    ticket_id = entry.get("disposition_ticket")
    expected = entry.get("expected_state")
    reason = entry.get("reason", "(no reason given)")
    if not (account and ticket_id and expected):
        return False, f"malformed skip entry (account/disposition_ticket/expected_state required); ignored"
    if expected not in _TICKET_STATE_DIRS:
        return False, f"skip for {account}: expected_state '{expected}' is not one of {_TICKET_STATE_DIRS}; ignored"
    actual = None
    for state in _TICKET_STATE_DIRS:
        if (repo_root / "tickets" / state / f"{ticket_id}.md").exists():
            actual = state
            break
    if actual is None:
        return False, f"skip for {account} EXPIRED: disposition ticket {ticket_id} not found in tickets/{{inbox,active,done}}/"
    if actual != expected:
        return False, f"skip for {account} EXPIRED: {ticket_id} now in tickets/{actual}/ (was expected in tickets/{expected}/)"
    return True, f"skip active per {ticket_id} (state={actual}): {reason}"


def apply_skip_list(results: list[dict], skip_list: list[dict],
                    *, repo_root: Path = REPO_ROOT) -> list[dict]:
    """Suppress DRIFTED/UNKNOWN/EMPTY_CLONE verdicts to SKIPPED for (account,
    repo) pairs whose disposition-skip is currently active. Returns a NEW list;
    never mutates the input results. CURRENT, NO_CLONE and PENDING_PUSH are
    never suppressed (nothing to suppress). EMPTY_CLONE is suppressible (T-4116)
    for parity with the other needs-alert verdicts — a known/accepted unborn
    clone can be dispositioned exactly like a known drift. The SKIPPED result
    carries the original verdict
    under `original_verdict` and the reason under `skip_reason` for the
    dry-run/json output and the alert footer.

    T-4061 repo-awareness (backward-compatible): an entry WITH a `repo` field
    applies only to that (account, repo); an entry WITHOUT `repo` applies to
    the account across ALL repos (so pre-T-4061 account-only entries keep
    working). A repo-specific entry takes precedence over an account-wide one
    for its repo.
    """
    by_account_repo: dict[tuple[str, str], dict] = {}
    by_account: dict[str, dict] = {}
    for entry in skip_list:
        acct = entry.get("account")
        if not isinstance(acct, str):
            continue
        repo = entry.get("repo")
        if isinstance(repo, str):
            by_account_repo[(acct, repo)] = entry
        else:
            by_account[acct] = entry
    out: list[dict] = []
    for r in results:
        acct = r.get("account", "")
        repo = r.get("repo")
        entry = by_account_repo.get((acct, repo)) if isinstance(repo, str) else None
        if entry is None:
            entry = by_account.get(acct)
        if entry is None or r["verdict"] not in _ALERT_VERDICTS:
            out.append(r)
            continue
        active, reason = is_skip_active(entry, repo_root=repo_root)
        if active:
            out.append({**r, "verdict": SKIPPED, "original_verdict": r["verdict"],
                        "skip_reason": reason,
                        "detail": f"{r.get('detail', '')} — SKIPPED: {reason}"})
        else:
            # Expired / malformed skip: keep the original verdict so the seat
            # still alerts. Annotate the detail so an operator can see WHY
            # the skip didn't apply on this cycle.
            out.append({**r, "detail": f"{r.get('detail', '')} — skip attempted but not applied: {reason}"})
    return out


def post_pull_verification(persona: str) -> str:
    """How to confirm a seat runs the pulled tooling, from its instance.yaml binding (T-5149).

    Only a `supervisor: control-plane` seat declares a persona-consume runtime, so only
    there is a consume fire the right check. Every other seat has no enabled
    pantheon-persona-consume@ timer, and firing that unit by hand starts a runtime the seat
    does not run: on 2026-10-18 a manual fire on the codex peer daemon exhausted runtime
    viability (RuntimeViabilityExhausted). Those seats are verified on the daemons they do
    run, from their next timer-driven cycle, never by a hand fire."""
    binding = arm.instance_binding(persona)
    if binding.get("supervisor") == "control-plane":
        return (f"wait for the next timer fire of {arm.control_plane_consume_service(persona)} "
                f"and confirm it exited clean (Result=success).")
    units = [f"pantheon-inbox-consumer@{persona}.service", f"pantheon-mailbox-wake@{persona}.service"]
    if binding.get("restart_service"):
        head = f"confirm {binding['restart_service']} is active, then "
    else:
        head = "confirm the seat's live session/daemon is up, then "
    return (head + f"confirm the next timer-driven cycle of {' and '.join(units)} exited clean "
            f"(Result=success). Do NOT fire pantheon-persona-consume@{persona} by hand: "
            f"this seat declares no consume runtime.")


def render_alert(flagged: list[dict], *, skipped: list[dict] | None = None) -> str:
    lines = [f"Fleet clone-currency check: {len(flagged)} seat(s) need attention.", ""]
    for r in flagged:
        repo_tag = f" [{r['repo']}]" if r.get("repo") else ""
        lines.append(f"- {r['persona']}/{r['account']}{repo_tag}: {r['verdict']} — {r['detail']}")
    lines += [
        "",
        "Remedy: supervised ff-pull each flagged seat/repo (T-2743 pattern) — confirm the "
        "clone is clean, then ff-only pull to its origin default branch. Then verify, per seat:",
    ]
    for persona in sorted({r["persona"] for r in flagged}):
        lines.append(f"- {persona}: {post_pull_verification(persona)}")
    if any(r["verdict"] == OFF_MAIN for r in flagged):
        lines.append(
            "OFF_MAIN: never auto-switch (it is the persona's working clone, T-4143). "
            "The owning persona moves its work to a worktree and checks out the default "
            "branch again. Until then its units run the parked branch's tooling.")
    if skipped:
        lines += [
            "",
            f"Skipped (dispositioned-mitigation carve-outs still active, T-3425): "
            f"{len(skipped)} seat(s).",
        ]
        for r in skipped:
            repo_tag = f" [{r['repo']}]" if r.get("repo") else ""
            lines.append(
                f"- {r['persona']}/{r['account']}{repo_tag}: "
                f"was {r.get('original_verdict', 'DRIFTED')} → SKIPPED. "
                f"{r.get('skip_reason', '')}"
            )
    return "\n".join(lines)


def render_persona_notice(persona: str, rows: list[dict]) -> str:
    lines = [f"{persona}: your clone(s) failed the daily fleet clone-currency check.", ""]
    for r in rows:
        lines.append(f"- /home/{r['account']}/{r.get('repo', 'pantheon-config')}: "
                     f"{r['verdict']} — {r['detail']}")
    lines += [
        "",
        "Your systemd units (mailbox-wake, inbox-consumer, persona-consume, ...) exec tools "
        "from this clone, so they run whatever it has checked out. Nothing was changed for "
        "you. If a clone is OFF_MAIN, move your work to a worktree and check out the default "
        "branch again. If it is DRIFTED, ff-pull it once it is clean. orchestrator and recovery got the "
        "fleet-wide copy.",
    ]
    return "\n".join(lines)


def alert(flagged: list[dict], *, skipped: list[dict] | None = None) -> list[str]:
    """Mail the fleet-wide alert to orchestrator (the supervised-pull remediator), a
    copy to Recovery, and each flagged persona its own rows (T-4143). Returns the
    recipients reached. orchestrator's send stays unguarded (its failure fails the unit,
    as before). The copies are best-effort: an absent inbox is logged and never
    blocks the others."""
    from tools import mailbox
    subject = f"fleet clone-currency: {len(flagged)} seat(s) need attention"
    body = render_alert(flagged, skipped=skipped)
    mailbox.send(ORCHESTRATOR_INBOX, body, "fleet_clone_currency_check", subject=subject)
    reached = ["orchestrator"]
    copies: list[tuple[str, str, str, str]] = [("recovery", RECOVERY_INBOX, body, subject)]
    by_persona: dict[str, list[dict]] = {}
    for r in flagged:
        by_persona.setdefault(r["persona"], []).append(r)
    for persona, rows in by_persona.items():
        if persona in {"orchestrator", "recovery"}:
            continue  # already holds the fleet-wide copy
        copies.append((persona, f"{arm.mailbox_hub()}/{persona}",
                       render_persona_notice(persona, rows),
                       f"your pantheon clone needs attention: "
                       f"{', '.join(sorted({r['verdict'] for r in rows}))}"))
    for name, inbox, text, subj in copies:
        try:
            mailbox.send(inbox, text, "fleet_clone_currency_check", subject=subj)
            reached.append(name)
        except Exception as exc:  # best-effort copy; never blocks the rest
            _log(f"could not mail {name} ({inbox}): {type(exc).__name__}: {exc}")
    return reached


def _log(msg: str) -> None:
    print(f"[fleet_clone_currency_check ts={int(time.time())}] {msg}", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--check", action="store_true", help="sweep the fleet + alert on drift (cron path)")
    p.add_argument("--dry-run", action="store_true", help="sweep + print, send no alert")
    p.add_argument("--threshold", type=int, default=DRIFT_THRESHOLD_DEFAULT,
                    help=f"commits-behind alert threshold (default {DRIFT_THRESHOLD_DEFAULT})")
    p.add_argument("--skip-list", type=Path, default=SKIP_LIST_PATH,
                    help=f"per-account dispositioned-mitigation skip-list YAML "
                         f"(default {SKIP_LIST_PATH}; T-3425)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if not (args.check or args.dry_run):
        print(
            "ERROR: refusing to run with no mode flag. Pass --check (cron path) or "
            "--dry-run to preview without alerting.",
            file=sys.stderr,
        )
        return 64

    seats = discover_seats()
    # Connectivity gate on the runner's OWN primary clone (pantheon-config @
    # main): if we cannot even fetch our own origin/main, the whole cycle is
    # skipped (a stale ref would under-report drift, which is worse than
    # skipping). Preserved from T-2798 — the runner's own clone is the canary.
    if not _fetch_origin_main():
        _log("could not fetch origin/main — skipping this cycle's sweep (stale ref would under-report drift)")
        if not args.dry_run:
            from tools import mailbox
            mailbox.send(
                ORCHESTRATOR_INBOX,
                "Fleet clone-currency check: could not fetch origin/main this cycle — "
                "the sweep was SKIPPED (a stale ref would under-report drift, which is "
                "worse than skipping). Check the runner's own clone/network.",
                "fleet_clone_currency_check",
                subject="fleet clone-currency: fetch failed, sweep skipped",
            )
        return 0

    # Per-repo: resolve each always_available repo's runner clone + default
    # branch, then refresh that runner clone's compare ref. F1 (T-4061 rebound):
    # CAPTURE each fetch's success into the target — the global gate above only
    # covers pantheon-config@main (GitHub); pantheon-inventory is a DIFFERENT
    # host (Forgejo), so a Forgejo fetch blip passes the global gate yet leaves a
    # stale local origin/<branch>, against which a behind-count under-reports
    # (false CURRENT). A repo whose fetch fails has fetch_ok=False → its seats
    # classify UNKNOWN (alerted), mirroring the global gate PER repo, without
    # skipping the whole sweep (the other repo may be fine).
    repos = discover_repos()
    repo_targets = _resolve_repo_targets(repos)
    for repo, tgt in repo_targets.items():
        tgt["fetch_ok"] = _fetch_origin_main(repo_root=tgt["runner_clone"], branch=tgt["branch"])
        if not tgt["fetch_ok"]:
            _log(f"could not fetch origin/{tgt['branch']} for {repo} — that repo's seats "
                 f"will report UNKNOWN this cycle (stale ref would under-report drift)")

    raw_results = sweep(seats, repo_targets, threshold=args.threshold)
    skip_list = load_skip_list(args.skip_list)
    results = apply_skip_list(raw_results, skip_list)
    flagged = needs_alert(results)
    skipped = [r for r in results if r["verdict"] == SKIPPED]

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            _log(f"[{r['verdict']:<{_VERDICT_COL}}] {r['persona']:10} {r['account']:12} "
                 f"{r.get('repo', ''):18} {r['detail']}")

    if flagged and not args.dry_run:
        alert(flagged, skipped=skipped)
        _log(f"alerted orchestrator (+ recovery, owning personas): {len(flagged)} seat(s) need attention "
             f"({len(skipped)} dispositioned-skipped)")
    elif flagged:
        _log(f"[dry-run] would alert: {len(flagged)} seat(s) need attention "
             f"({len(skipped)} dispositioned-skipped)")
    else:
        _log(f"all seats current; no alert ({len(skipped)} dispositioned-skipped)")
    # Exit 0 always: a monitor must not fail its own timer.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
