#!/usr/bin/env python3
"""deploy_hook — deploy a pantheon-config hook to accounts WITHOUT writing through a symlink.

Exists because the procedure it replaces lived in a memory, and a memory is the form we
established today does not protect its author (T-2195/T-2198 thread). On 2026-07-28 a
blanket `cp` over `~/.claude/hooks/verify-hook-integrity.sh` followed the symlink into
claude-research's git worktree, left it dirty, and blocked EVERY ticket.py write in that
clone for nine minutes — so the remediation path and the reporting path failed together
and the agent best placed to report it was the one silenced (T-2204).

SPECIALIST'S CORRECTION, which is why this file exists rather than a stricter habit:
push-canonical-before-deploy does NOT fix write-through. It fixes the false ALARM — the
deployed bytes then match origin/main's blob so the integrity check stays silent — but the
peer's tree is still dirty, because dirtiness is deployed-vs-THEIR-HEAD and their clone is
behind until they pull. Same cp, same ` M`, same block. The only thing that prevents it is
checking `[ -L ]` per file per account and NOT cp-ing when the target is a symlink.

DEPLOYMENT IS MIXED PER FILE **AND** PER ACCOUNT. Enumerate; never generalise. Measured
2026-07-28 — and note the third row, which I got wrong twice by generalising from the first:

    claude           symlinks: backlog-surface.sh, ensure-hooks-active.sh, verify-hook-integrity.sh
    claude-research  symlinks: ensure-hooks-active.sh, verify-hook-integrity.sh
    claude-fallback  symlinks: ensure-hooks-active.sh, verify-hook-integrity.sh
    (handoff-auto-read.sh is a regular COPY on all three; backlog-surface.sh exists only on claude)

So the correct action differs per (file, account) pair:
    SYMLINK into that account's clone -> `git pull` the clone. NEVER cp: it writes through.
    REGULAR FILE (or absent)          -> cp, then chmod.

Both paths finish by comparing the DEPLOYED bytes against origin/main's blob, because the
deploy step succeeding is not evidence the right bytes arrived.

T-2261, TWO DEFECTS ONE LAYER BELOW THE ACCOUNT LIST — both silent by construction:

1. FIRST DEPLOY TO A VIRGIN SEAT FAILED. The COPY branch ran a bare `cp` with no
   `mkdir -p`, so an account that had never received a hook — which has no
   ~/.claude/hooks/ at all — could not receive its first one. Measured on suborch2
   2026-07-28: `cp /dev/null ~/.claude/hooks/probe` -> "No such file or directory";
   the real run printed `suborch2 COPY deployed=? MISMATCH` / exit 1; after a manual
   `mkdir -p` the rerun printed `COPIED deployed=7afdb7ef VERIFIED` / exit 0.
   THE COMPOUNDING SHAPE: the accounts that were missing from DEFAULT_ACCOUNTS are
   exactly the accounts that never received a hook, and therefore exactly the accounts
   with no hooks dir. Fixing the enumeration made the new seats VISIBLE and simultaneously
   guaranteed they FAIL on first deploy. Two lists, same axis, one layer apart.
   CREDIT WHERE DUE AND KEEP IT: the tool FAILED CLOSED. The post-deploy md5 verify is
   what turned a silent no-op into a visible MISMATCH. Do not weaken that step.

2. DEPLOYED IS NOT ACTIVE, AND NOTHING MEASURED ACTIVE. Bytes on disk do not make a hook
   run; Claude Code only invokes it if that account's ~/.claude/settings.json registers it
   under a SessionStart entry. Metis's words, exact and worth keeping:
   "A DEPLOY THAT REPORTS VERIFIED AGAINST THE FILE'S MD5 IS MEASURING A PROXY FOR
   'THE HOOK RUNS.'"
   Measured twice: metis's seat and then atlas's seat both had handoff-auto-read.sh
   byte-perfect, mode 755, VERIFIED — and INERT, because settings.json registered only
   ensure-hooks-active.sh. Both had to be wired by hand. This is the same failure shape
   ensure-hooks-active.sh itself polices for git hooks (present but not running) — same
   class, different registry, and it is an Amendment-H2 instance: a check that cannot
   distinguish healthy from broken is not an assertion. The old verify printed VERIFIED
   for a hook that would run and for one that never could.

   SO THE ACTIVATION AXIS IS REPORTED SEPARATELY FROM THE BYTES AXIS. VERIFIED still
   means and only means "the right bytes are on disk". The second column answers "will
   they run": ACTIVE / INERT / ELSEWHERE / UNKNOWN (see the STATUS_* constants).

   REPORT, DO NOT WRITE — ruled FINAL by the HO 2026-07-28, not provisional. This tool
   detects and reports registration; it does NOT edit another account's settings.json.
   Editing a peer seat's config from an unattended deploy script is a materially heavier
   and less reversible intervention than reporting it (D3 autonomy bar). The decisive
   reason, though, is not caution about the write — it is that STAGED-BEFORE-ENABLED IS A
   LEGITIMATE STATE. Absence of registration is therefore not necessarily a defect, and a
   tool that auto-"fixed" it would be overriding a deliberate operator choice it cannot
   distinguish from an oversight. Hence INERT is VISIBLE and NON-FATAL: it does not change
   the exit code, it changes what the operator is told.

PRIVILEGE MODEL — WHO CAN ACTUALLY RUN THIS, AND HOW FAR.

This tool is HO/Recovery-tier for FLEET-WIDE use. That is a statement about the privilege
reality on the VPS, not a defect and not a TODO.

`_as_user()` short-circuits when the target IS the current user and otherwise goes through
`sudo -n -u <account>`. `-n` is non-interactive by construction — it must be, because this
runs unattended — so every cross-account operation depends on the calling account holding
passwordless sudo to that target.

Today that is recovery (claude-fallback), confirmed by its own earlier deploys — and orchestrator
(claude) ONLY via a root-wrapped invocation, NOT by bare peer sudo. CORRECTED 2026-07-29
(T-2256): this line previously read "orchestrator (claude) and recovery (claude-fallback)" as if both
held peer sudo. The orchestrator half was false. MEASURED by orchestrator during the fleet run at
~00:2xZ that night: `sudo -n -u claude-fallback` from the orchestrator seat returned "a password
is required". The fleet deploy that did succeed went through a ROOT (password-
authenticated) invocation, and needed an EXPLICIT ssh identity for the freshness fetch —
ssh resolves its config from the EFFECTIVE UID's passwd entry, not from $HOME, which took
three failed shapes before one worked. Budget for that, or the fetch fails in a way that
looks like a network problem.

That correction is the whole point of this section existing. A privilege model that names
the wrong seats is worse than no note at all: it sends the next reader down a path that
does not work, while reading as measured fact. Same class as everything else in this
cluster — a confident statement about a set nobody re-measured.

MEASURED 2026-07-28 from the suborch2 seat: `sudo -n -u <acct>` returned
"sudo: a password is required" for claude, claude-research, claude-fallback, exocortex and
suborch1 — every peer, without exception. Only suborch2 itself worked, and only because
_as_user never reached sudo at all.

    A seat WITHOUT cross-account sudo can still run this tool, but only against ITSELF:
        python3 tools/deploy_hook.py --hook <h> --accounts $(whoami)
    Every other --accounts entry will fail its probe, land as REFUSE (fail-closed, by
    design — see plan_step), and deploy nothing.

The right response to that is NOT to widen the seats' sudo. Granting a sub-orchestrator
fleet-wide peer sudo so that a deploy tool can be wet-run from its seat would expand a
seat's blast radius to fit a test, which is backwards: the blast radius is the thing being
constrained, and the test is the thing that should bend. So the constraint is documented
here instead of removed — the alternative is every new seat rediscovering it by watching a
deploy silently REFUSE, which is precisely the class of silent-by-construction failure this
file already exists to end.

CONSEQUENCE FOR TESTING, and it is why every function in this module takes `run=`: a seat
that cannot sudo cannot wet-run cross-account AT ALL. The injected `run=` callable is the
intended and only seam for asserting cross-account behaviour off-privilege
(tools/test_deploy_hook.py). Fleet-wide wet-runs belong to whoever holds the privilege.

Usage:
    python3 tools/deploy_hook.py --hook verify-hook-integrity.sh --dry-run
    python3 tools/deploy_hook.py --hook handoff-auto-read.sh --accounts claude,claude-research
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import getpass
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CANON_DIR = REPO / "tools" / "hooks"
# Every Claude Code persona account on the VPS, per README.md §Topology (six as of
# ORG-042 + ORG-043) — NOT just the original three.
#
# WHY THIS LIST IS DANGEROUS TO GET WRONG, and it was wrong from 2026-07-20 to 2026-07-28:
# it read ("claude", "claude-research", "claude-fallback") — the pre-ORG-042 topology.
# Mnemosyne (exocortex), Metis (suborch1) and Atlas (suborch2) were simply absent, so a
# deploy of ANY hook reported success while silently covering three of six accounts. The
# missing entries cannot announce themselves: a skipped account produces no error, no
# warning, and no row in the plan — its absence is indistinguishable from it being fine.
# Same defect class as the hook this was first used to deploy: a check whose blind spot
# reads as a pass. Verified 2026-07-28: `id exocortex/suborch1/suborch2` all resolve.
#
# When a persona is added, THIS LIST IS PART OF BRINGUP. An account absent here is an
# account that silently stops receiving hook updates.
DEFAULT_ACCOUNTS = (
    "claude",            # Orchestrator — HO
    "claude-research",   # Specialist — specialist
    "claude-fallback",   # Recovery — recovery
    "exocortex",         # Mnemosyne — second head (ORG-042)
    "suborch1",          # Metis — sub-orchestrator seat (ORG-043)
    "suborch2",          # Atlas — sub-orchestrator seat (ORG-043)
)

ACTION_PULL = "pull"      # symlinked deployment — cp would write through
ACTION_COPY = "copy"      # regular-file deployment — cp is correct
ACTION_REFUSE = "refuse"  # a precondition failed; do nothing

# ---------------------------------------------------------------- activation axis
# T-2261 defect 2. ORTHOGONAL to VERIFIED/MISMATCH, which is only ever about bytes.
# Deliberately four states, not two, because "not ACTIVE" has three causes that call for
# three different operator actions and collapsing them would rebuild the defect:
STATUS_ACTIVE = "ACTIVE"        # a SessionStart entry invokes THE DEPLOYED PATH. It runs.
STATUS_INERT = "INERT"          # readable config, nothing registers this hook. It never runs.
STATUS_ELSEWHERE = "ELSEWHERE"  # registered by this basename but at a DIFFERENT path — so
                                # something runs, but not the bytes we just verified. This
                                # is the one that would otherwise hide inside "ACTIVE".
STATUS_UNKNOWN = "UNKNOWN"      # could not read or could not parse the config.

# FAIL-SAFE, and the point of having UNKNOWN at all: a settings.json we cannot read or
# cannot parse is UNKNOWN, never ACTIVE. Reporting ACTIVE off an unreadable config would be
# a fresh instance of the exact defect class this ticket exists to close — a check whose
# blind spot reads as a pass.
_SETTINGS_ABSENT = "__PANTHEON_SETTINGS_ABSENT__"
_SETTINGS_UNREADABLE = "__PANTHEON_SETTINGS_UNREADABLE__"


@dataclass(frozen=True)
class Step:
    account: str
    action: str
    reason: str

    def to_dict(self) -> dict:
        return {"account": self.account, "action": self.action, "reason": self.reason}


def _current_user() -> str:
    """getpass.getuser() falls back through pwd, so it works when $USER is unset."""
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "")


def origin_is_fresh(*, run=subprocess.run) -> tuple[bool, str]:
    """Fetch, then PROVE the local origin/main ref equals the real remote tip.

    T-2227 finding 1 (specialist, external read). Both the precondition
    (canonical_is_pushed) and the verification target (_origin_blob_digest) read
    `origin/main` — a LOCAL CACHED ref. Nothing fetched. So they derive from one
    snapshot and CANNOT disagree: if the cache is stale, the precondition passes
    against stale bytes and `want` is computed from those same stale bytes, so the
    tool deploys the wrong file and prints VERIFIED for every account.

    Measured, not theorised: reproduced in a throwaway clone (worktree 'v1', true
    remote 'v2', canonical_is_pushed() -> True), and my own clone's origin/main was
    genuinely stale at 78ef1350 vs a true remote of 73d8e362 while I ran the check.

    The fetch alone is not enough to assert, so this also compares the post-fetch
    ref against `git ls-remote` — an assertion that cannot distinguish the failure
    state from the healthy one is a reminder wearing a mechanism's clothes. REFUSES
    on any failure rather than falling back to the cache; a stale-but-usable cache
    is exactly the thing being guarded against.
    """
    try:
        f = run(["git", "-C", str(REPO), "fetch", "--quiet", "origin", "main"],
                capture_output=True, timeout=60)
        if f.returncode != 0:
            return False, "git fetch origin main failed — refusing to trust a cached ref"
        local = run(["git", "-C", str(REPO), "rev-parse", "origin/main"],
                    capture_output=True, text=True, timeout=20)
        remote = run(["git", "ls-remote", "origin", "main"],
                     capture_output=True, text=True, timeout=60, cwd=str(REPO))
        if local.returncode != 0 or remote.returncode != 0:
            return False, "could not resolve origin/main against the remote"
        lsha = (local.stdout or "").strip()
        rsha = ((remote.stdout or "").split() or [""])[0].strip()
        if not lsha or not rsha:
            return False, "empty ref resolving origin/main"
        if lsha != rsha:
            return False, (f"origin/main is STALE after fetch (local {lsha[:9]} != "
                           f"remote {rsha[:9]}) — refusing")
        return True, lsha[:9]
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"origin freshness check errored: {type(e).__name__}"


def stage_canonical(hook: str, *, source: Path | None = None) -> str:
    """Write the canonical bytes to a staging file that CANNOT be redirected.

    T-2227 finding 2 (specialist): the old staging step was
    `Path(f"/tmp/recovery-deploy-{hook}").write_bytes(...)` — a PREDICTABLE path in a
    shared sticky directory, opened O_WRONLY|O_CREAT|O_TRUNC. A pre-planted symlink
    there redirects the write to any file the caller can reach. That is the exact
    defect class this tool exists to prevent, one level up, inside its own staging.
    It also raised an uncaught PermissionError when the path already existed owned
    by someone else.

    mkstemp opens O_CREAT|O_EXCL|O_NOFOLLOW with an unpredictable name, so it can
    neither follow nor clobber. 0644 because the target accounts read it as
    themselves; the unpredictable name, not the mode, is what carries the safety.
    """
    src = source if source is not None else (CANON_DIR / hook)
    fd, stage = tempfile.mkstemp(prefix=f"recovery-deploy-{hook}-", dir="/tmp")
    with os.fdopen(fd, "wb") as fh:
        fh.write(src.read_bytes())
    os.chmod(stage, 0o644)
    return stage


def canonical_is_pushed(hook: str, *, run=subprocess.run) -> bool:
    """Does the repo's copy match origin/main's blob? Precondition for deploying.

    Not because it prevents write-through (it does not — see the module docstring), but
    because deploying bytes that are not yet canonical makes every peer's integrity check
    fire on a difference nobody can resolve by pulling. Ordering discipline, enforced
    instead of remembered.
    """
    try:
        blob = run(["git", "-C", str(REPO), "show", f"origin/main:tools/hooks/{hook}"],
                   capture_output=True, timeout=20)
        if blob.returncode != 0:
            return False
        return blob.stdout == (CANON_DIR / hook).read_bytes()
    except (OSError, subprocess.SubprocessError):
        return False


def probe_is_symlink(account: str, path, *, run=subprocess.run) -> bool | None:
    """Is `path` a symlink, ON THAT ACCOUNT? True / False / None (could not determine).

    MUST shell out as the account rather than use Path.is_symlink() locally: peer homes
    are mode 700, so pathlib RAISES PermissionError rather than returning False. That is
    my own pinned memory (T-1867 killed a grooming run the same way, silently) — and I
    wrote the first version of this file using pathlib anyway, in the tool that exists
    because remembered procedures do not protect their author. A live dry-run caught it
    in about four seconds, which is the argument for this file in miniature.
    """
    return probe_symlink_state(account, path, run=run)[0]


# Why a probe produced no answer. UNREACHABLE and UNPARSEABLE both fail closed, but
# they are DIFFERENT FACTS with different remedies and must not share a message.
PROBE_SYMLINK = "symlink"
PROBE_REGULAR = "regular-or-absent"
PROBE_UNREACHABLE = "unreachable"      # the probe never ran (sudo denied, timeout)
PROBE_UNPARSEABLE = "unparseable"      # it ran and said something we don't understand


def probe_symlink_state(account: str, path, *, run=subprocess.run):
    """(is_symlink, why) — the three-state answer PLUS why there isn't one.

    T-2317, raised by orchestrator from the claude seat: it hit REFUSE on both peers with
    "could NOT determine whether the deployed path is a symlink". That message is
    true and it MISDIRECTS — it reads as a statement about the deployed FILE, so
    the operator goes and inspects the file. Nothing was wrong with the file. The
    probe shells out via `sudo -n`, which from a seat without NOPASSWD returns
    "a password is required", so the probe never executed: the file's state was
    never in question because the QUESTION WAS NEVER ASKED.

    "Could not determine whether X" and "could not reach the account to ask about
    X" point at different repairs — inspect the file, versus fix your credentials.
    Collapsing them is the blind-predicate defect from
    bugs/shell-sentinel-masks-exit-code-blind-predicate.md: a probe that cannot run
    reporting a conclusion about the thing it was going to probe. Same class as
    T-2359, and the same rule as "cannot tell must never stand in for no" — here
    applied to the DIAGNOSIS rather than the verdict.

    The fail-closed verdict is unchanged: both no-answer cases still REFUSE.
    """
    r = _as_user(account, f'[ -L "{path}" ] && echo SYMLINK || '
                          f'{{ [ -e "{path}" ] && echo REGULAR || echo ABSENT; }}', run=run)
    out = (getattr(r, "stdout", "") or "").strip()
    if out == "SYMLINK":
        return True, PROBE_SYMLINK
    if out in ("REGULAR", "ABSENT"):
        return False, PROBE_REGULAR
    # No usable stdout. A non-zero rc with sudo's complaint on stderr means the
    # command never reached the account; anything else means it ran and surprised us.
    err = (getattr(r, "stderr", "") or "").lower()
    rc = getattr(r, "returncode", 1)
    if rc != 0 and ("password is required" in err or "not allowed to execute" in err
                    or "no passwd entry" in err or "sudo:" in err or not out):
        return None, PROBE_UNREACHABLE
    return None, PROBE_UNPARSEABLE


def _hook_command_tokens(command: str) -> list[str]:
    """Split a settings.json command string the way a shell would, best-effort.

    A registration is not always a bare path: `bash ~/.claude/hooks/x.sh --flag` and
    `"$HOME/.claude/hooks/x.sh"` are both real shapes. Matching the whole string against a
    path would miss them; matching by substring would false-positive on a comment or a
    different hook whose name contains this one. Tokenise, then compare per token.
    """
    try:
        return shlex.split(command)
    except ValueError:          # unbalanced quotes — still better than nothing
        return command.split()


def _expand_home(token: str, home: str) -> str:
    """Resolve ~ and $HOME against THAT ACCOUNT's home, not ours."""
    t = token.strip().strip("'\"")
    if t == "~":
        t = home
    elif t.startswith("~/"):
        t = home + t[1:]
    t = t.replace("${HOME}", home).replace("$HOME", home)
    return os.path.normpath(t) if "/" in t else t


def _session_start_commands(session_start) -> list[str]:
    """Every command string under a SessionStart list, across the shapes it takes.

    Canonical shape is a list of matcher-groups each holding a `hooks` list of
    {"type": "command", "command": "..."} — but a group with a top-level `command`, and a
    bare string, both appear in the wild. Anything unrecognised is SKIPPED rather than
    guessed at, which is safe here: a command we fail to see makes the result INERT (loud),
    never ACTIVE (silent).
    """
    cmds: list[str] = []
    for group in session_start:
        if isinstance(group, str):
            cmds.append(group)
            continue
        if not isinstance(group, dict):
            continue
        if isinstance(group.get("command"), str):
            cmds.append(group["command"])
        inner = group.get("hooks")
        if isinstance(inner, list):
            for h in inner:
                if isinstance(h, dict) and isinstance(h.get("command"), str):
                    cmds.append(h["command"])
    return cmds


def parse_registration(settings_text: str | None, hook: str, account: str) -> tuple[str, str]:
    """Is `hook` registered under ANY event in this settings.json? PURE. (T-2320)

    Returns (STATUS_*, human detail). Split out from the probe so the whole decision table
    is testable without a subprocess and without a peer account — which matters, because
    this seat has no passwordless sudo to any peer and the alternative to a pure function
    here is an untestable one.

    THE DECISION TABLE, and why each malformed case lands where it does:
        unreadable / sudo denied   -> UNKNOWN  we cannot tell; must not read as a pass
        not valid JSON             -> UNKNOWN  ditto — an unparseable config is not evidence
        settings.json ABSENT       -> INERT    NOT unknown: a file that is not there cannot
                                               register anything. This is a positive
                                               measurement, and it is the virgin-seat case.
        no "hooks" key             -> INERT    same reasoning, one level in
        no "SessionStart"          -> INERT    same
        SessionStart, other hooks  -> INERT    THE MEASURED CASE: metis and atlas both had
                                               only ensure-hooks-active.sh registered while
                                               handoff-auto-read.sh sat on disk, VERIFIED
                                               and never invoked
        this basename, other path  -> ELSEWHERE something runs; not the bytes we verified
        this basename, deployed path -> ACTIVE
    """
    home = f"/home/{account}"
    deployed = f"{home}/.claude/hooks/{hook}"
    if settings_text is None:
        return STATUS_UNKNOWN, "could not read that account's ~/.claude/settings.json"
    body = settings_text.strip()
    if body == _SETTINGS_UNREADABLE:
        return STATUS_UNKNOWN, "~/.claude/settings.json exists but is not readable"
    if body == _SETTINGS_ABSENT:
        return STATUS_INERT, "no ~/.claude/settings.json — nothing can register this hook"
    if not body:
        return STATUS_UNKNOWN, "~/.claude/settings.json read back empty"
    try:
        data = json.loads(body)
    except ValueError:
        return STATUS_UNKNOWN, ("~/.claude/settings.json is not valid JSON — cannot tell "
                                "registered from unregistered, so NOT calling it active")
    if not isinstance(data, dict):
        return STATUS_UNKNOWN, "~/.claude/settings.json is not a JSON object"
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return STATUS_INERT, "settings.json has no 'hooks' object — no hook is registered"
    # T-2320: scan EVERY event, not just SessionStart.
    #
    # This used to read hooks.get("SessionStart") and return INERT when it was
    # missing — so a PreToolUse hook that was registered, executable and firing
    # reported INERT. That is a false negative shaped exactly like a failed
    # deploy, produced by the one tool built to stop presence being mistaken for
    # activation, and it folded a FIFTH cause (wrong-event-for-this-probe)
    # silently into INERT, defeating the point of the four-state design.
    #
    # Widening is the honest fix rather than the expensive one, because the match
    # was never event-aware: it compares the BASENAME of each registered command's
    # tokens against the hook. Nothing per-event is re-derived — only the set of
    # groups fed in changes. The event that matched is now named in the detail,
    # so the answer carries which surface it came from.
    events = {k: v for k, v in hooks.items() if isinstance(v, list) and v}
    if not events:
        return STATUS_INERT, "settings.json registers no hooks under any event"
    matched: list[tuple[str, str]] = []
    total = 0
    for event in sorted(events):
        commands = _session_start_commands(events[event])
        total += len(commands)
        for c in commands:
            for t in _hook_command_tokens(c):
                expanded = _expand_home(t, home)
                if os.path.basename(expanded) == hook:
                    matched.append((event, expanded))
    if not matched:
        return STATUS_INERT, (f"{total} command(s) registered across {len(events)} event(s) "
                              f"({', '.join(sorted(events))}), none of them {hook} — the "
                              f"deployed bytes will never be invoked")
    for event, path in matched:
        if path == deployed:
            return STATUS_ACTIVE, f"{event} invokes {deployed}"
    event, path = matched[0]
    return STATUS_ELSEWHERE, (f"{event} invokes {path}, NOT the deployed "
                              f"{deployed} — something runs, but not the bytes just verified")


def probe_registration(account: str, hook: str, *, run=subprocess.run) -> tuple[str, str]:
    """Read that account's settings.json AS THE ACCOUNT and decide activation status.

    Shells out for the same reason probe_is_symlink does: peer homes are mode 700, so a
    local open() raises PermissionError rather than returning a useful answer. The three
    outcomes are distinguished IN THE SHELL (readable / exists-but-not-readable / absent)
    because `cat` failing cannot tell "no such file" from "permission denied", and those
    two are INERT and UNKNOWN respectively — collapsing them would either invent a
    measurement or discard one.
    """
    r = _as_user(account,
                 'f="$HOME/.claude/settings.json"; '
                 'if [ -r "$f" ]; then cat "$f"; '
                 f'elif [ -e "$f" ]; then echo {_SETTINGS_UNREADABLE}; '
                 f'else echo {_SETTINGS_ABSENT}; fi', run=run)
    if getattr(r, "returncode", 1) != 0:
        return STATUS_UNKNOWN, ("could not read that account's settings.json "
                                "(sudo denied or shell failed)")
    return parse_registration(getattr(r, "stdout", None), hook, account)


def plan_step(account: str, deployed: Path, *, is_symlink: bool | None = None,
              why: str | None = None) -> Step:
    """Decide the action for ONE (hook, account) pair. Pure: no filesystem writes.

    `is_symlink` is three-state and supplied by the caller (probe_is_symlink):
        True  -> PULL. A cp would write through into that account's worktree.
        False -> COPY. Regular file or absent; there is nothing to write through.
        None  -> REFUSE. FAIL CLOSED: if we cannot tell whether it is a symlink, a cp
                 might write through, and the whole point of this tool is not doing that
                 on a guess. Unknown is not "probably fine".
    """
    if is_symlink is None:
        if why == PROBE_UNREACHABLE:
            # Name the ACTUAL failure. The old text sent orchestrator to inspect a file
            # that was never in question (T-2317): the probe had not run at all.
            return Step(account, ACTION_REFUSE,
                        f"could not REACH {account} to ask whether the deployed path "
                        f"is a symlink — the probe never ran (sudo -n denied or timed "
                        f"out), so nothing is known about the file either way. This is "
                        f"a CREDENTIALS problem on the calling seat, not a problem with "
                        f"the deployed file: run from a seat with NOPASSWD sudo to "
                        f"{account}, or have that account deploy its own")
        return Step(account, ACTION_REFUSE,
                    "the symlink probe RAN but returned output we cannot interpret — "
                    "refusing rather than guessing, because a cp onto an unknown target "
                    "may write through into that account's git worktree. Inspect the "
                    "deployed path directly")
    if is_symlink:
        return Step(account, ACTION_PULL,
                    "deployed path is a SYMLINK into this account's clone — cp would write "
                    "THROUGH it into the worktree and dirty the tree; pull instead")
    # T-2261: `False` collapses absent-FILE and absent-PARENT-DIR into one bucket, and only
    # the second needed the mkdir. We deliberately do NOT split the probe's three-state
    # contract to tell them apart — after the fix the COPY branch runs `mkdir -p && cp`
    # unconditionally, so BOTH sub-cases take the identical action and an operator reading
    # the plan would get no different decision from the distinction. The wording is
    # corrected instead, so the plan line names the command that will actually run.
    return Step(account, ACTION_COPY,
                "deployed path is a regular file, or absent — mkdir -p + cp is correct "
                "(the parent dir may not exist yet on a seat that never received a hook)")


def plan(hook: str, accounts=DEFAULT_ACCOUNTS, *, symlink_map: dict | None = None,
         pushed: bool | None = None) -> list[Step]:
    """Full deployment plan. Refuses wholesale if the canonical is not pushed."""
    if pushed is None:
        pushed = canonical_is_pushed(hook)
    if not pushed:
        return [Step(a, ACTION_REFUSE,
                     f"tools/hooks/{hook} differs from origin/main's blob — push the "
                     "canonical BEFORE deploying, or peers' integrity checks fire on a "
                     "difference a pull cannot resolve")
                for a in accounts]
    steps = []
    for a in accounts:
        path = Path(f"/home/{a}/.claude/hooks/{hook}")
        if symlink_map is not None:
            state = symlink_map.get(a)
            why = None  # caller-supplied map carries no probe diagnosis
        else:
            state, why = probe_symlink_state(a, path)
        steps.append(plan_step(a, path, is_symlink=state, why=why))
    return steps


# ------------------------------------------------------------------ execution
def _as_user(account: str, cmd: str, *, run=subprocess.run):
    # getpass.getuser() not os.environ["USER"] (T-2227 finding 3): USER is commonly
    # UNSET under systemd and cron — which is exactly where this runs unattended —
    # so the env read never matches and the tool sudo's to its own account.
    if account == _current_user():
        return run(["bash", "-c", cmd], capture_output=True, text=True, timeout=120)
    return run(["sudo", "-n", "-u", account, "bash", "-c", cmd],
               capture_output=True, text=True, timeout=120)


def _origin_blob_digest(hook: str, *, run=subprocess.run) -> str | None:
    try:
        r = run(["git", "-C", str(REPO), "show", f"origin/main:tools/hooks/{hook}"],
                capture_output=True, timeout=20)
        if r.returncode != 0:
            return None
        raw = r.stdout if isinstance(r.stdout, bytes) else str(r.stdout).encode()
        return hashlib.md5(raw).hexdigest()
    except (OSError, subprocess.SubprocessError):
        return None


def execute(hook: str, steps: list[Step], *, dry_run: bool, stage: str | None = None,
            run=subprocess.run) -> int:
    """Run the plan and VERIFY per account. Returns the count of accounts NOT verified.

    Also prints a READY-TO-PASTE report block (T-2208) — see _print_report_block.

    The return value is the BYTES axis only (md5 mismatch or refusal) and therefore the
    exit code is too. Activation is reported per row and summarised, but does not fail the
    run — per T-2261, absence of registration is sometimes deliberate staging, and a deploy
    tool that refuses to exit 0 until it has edited a peer's config is a tool that invites
    somebody to make it edit the peer's config.
    """
    want = _origin_blob_digest(hook, run=run)
    failures = 0
    not_active = 0
    report_rows: list = []
    for step in steps:
        if step.action == ACTION_REFUSE:
            print(f"  {step.account:<16} REFUSE — {step.reason}")
            failures += 1
            continue
        if dry_run:
            # The activation probe is READ-ONLY, so it runs in dry-run too. An operator
            # planning a deploy needs to know the hook is unregistered BEFORE spending the
            # deploy, not after being told VERIFIED — that ordering is the whole complaint.
            act, act_detail = probe_registration(step.account, hook, run=run)
            print(f"  {step.account:<16} would {step.action.upper()} — {step.reason}")
            print(f"  {'':<16}   registration: {act} — {act_detail}")
            if act != STATUS_ACTIVE:
                not_active += 1
            continue
        if step.action == ACTION_PULL:
            r = _as_user(step.account,
                         'cd ~/pantheon-config && '
                         '[ -z "$(git status --porcelain)" ] && git pull --ff-only -q && echo PULLED '
                         '|| echo DIRTY-OR-FAILED', run=run)
            note = (r.stdout or "").strip()
        else:
            # T-2261 defect 1: `mkdir -p` is NOT decoration. A seat that has never received
            # a hook has no ~/.claude/hooks/ at all, so the bare cp exited 1 and the FIRST
            # deploy to every new persona failed — measured on suborch2, and predicted for
            # exocortex. -p so a seat that already has the dir is unaffected.
            r = _as_user(step.account,
                         f"mkdir -p ~/.claude/hooks && cp {stage} ~/.claude/hooks/{hook} "
                         f"&& chmod 755 ~/.claude/hooks/{hook} "
                         f"&& echo COPIED", run=run)
            note = (r.stdout or "").strip()
        # VERIFY: the deploy step succeeding is not evidence the right bytes arrived.
        # UNCHANGED AND UNWEAKENED — this md5 compare is what turned defect 1 from a silent
        # no-op into a visible MISMATCH, and it is the only reason the bug was found.
        v = _as_user(step.account, f"md5sum ~/.claude/hooks/{hook} | cut -d' ' -f1", run=run)
        got = (v.stdout or "").strip()
        ok = want is not None and got == want
        # ...but VERIFIED is a statement about BYTES only. Ask the second question too.
        act, act_detail = probe_registration(step.account, hook, run=run)
        print(f"  {step.account:<16} {step.action.upper():<5} {note:<18} "
              f"deployed={got[:8] or '?'} {'VERIFIED' if ok else 'MISMATCH'}  {act}")
        if act != STATUS_ACTIVE:
            print(f"  {'':<16}   {act}: {act_detail}")
            not_active += 1
        if not ok:
            failures += 1
        # T-2208: keep the FULL identifiers, not the 8-char display form. A
        # truncated md5 cannot be checked against anything, which is half of why
        # the reports it appeared in were uncheckable.
        report_rows.append((step.account, step.action, got or "", act))
    if not_active:
        # Visible and non-fatal: it does not move the exit code, it moves what the operator
        # is told. Writing a peer's settings.json from here is above this tool's bar.
        #
        # THIS LINE ASSERTS NOTHING ABOUT THE BYTES. It used to open "correct bytes on disk,
        # but nothing proven to invoke them" — printed unconditionally, including under a row
        # that had just failed its md5 check, so the summary claimed byte-correctness for a
        # MISMATCH. Small, and nobody is misled next to a visible MISMATCH and a nonzero exit
        # — but it is this ticket's own defect shape (a word reading as a claim about
        # something it did not measure) reproduced inside the fix for it. Reworded rather
        # than made conditional: with several accounts the axes mix freely (VERIFIED+INERT
        # beside MISMATCH+UNKNOWN) and one conditional summary line cannot speak for all of
        # them without re-enumerating rows that are already printed. Saying nothing about
        # bytes is correct in every combination; saying the right thing about bytes requires
        # knowing which row you mean.
        print(f"  NOTE: {not_active} account(s) are NOT confirmed ACTIVE — see the rows above. "
              f"This line is about the ACTIVATION axis ONLY; the bytes axis is the "
              f"VERIFIED/MISMATCH column, counted separately on the line below. Bytes on disk "
              f"do not make a hook run — it must be registered in that account's "
              f"~/.claude/settings.json under SessionStart.")
    if not dry_run:
        _print_report_block(hook, report_rows, run=run)
    return failures



def _print_report_block(hook: str, rows: list, *, run=subprocess.run) -> None:
    """Print the identifiers this deploy ACTUALLY used, ready to paste into a report.

    ⚠️ WHY THIS EXISTS (T-2208, specialist): two consecutive deploy reports cited shas
    that resolve to NOTHING — `git log -1 <sha>` -> unknown revision — while the
    prose around them was entirely correct and the content really was in origin.
    One also cited an md5 matching no file anywhere. The substance was reliable;
    the identifiers were not, and identifiers are precisely the part a reader uses
    to VERIFY. A report whose sha does not resolve cannot be checked by the person
    it was sent to, which makes it a claim rather than evidence.

    A sha and an md5 are the canonical class of fact that CANNOT be recalled and
    must be retrieved. So the tool that has them prints them, rather than leaving a
    reporting agent to reproduce them from memory. Everything here is captured at
    deploy time from the objects actually used — never reconstructed afterwards.
    """
    def _git(*a):
        r = run(["git", "-C", str(REPO), *a], capture_output=True, text=True)
        return (r.stdout or "").strip() if getattr(r, "returncode", 1) == 0 else "?"

    canon = CANON_DIR / hook
    try:
        canon_md5 = hashlib.md5(canon.read_bytes()).hexdigest()
    except OSError:
        canon_md5 = "?"

    print()
    print("--- PASTE-READY IDENTIFIERS (captured at deploy time — do not retype) ---")
    print(f"  hook            : {hook}")
    print(f"  HEAD            : {_git('rev-parse', 'HEAD')}")
    print(f"  origin/main     : {_git('rev-parse', 'origin/main')}")
    print(f"  canonical blob  : {_git('rev-parse', f'origin/main:tools/hooks/{hook}')}")
    print(f"  canonical md5   : {canon_md5}   ({canon})")
    for account, action, md5, activation in rows:
        # FULL md5, and it is stated as "deployed md5" rather than as a verdict:
        # the VERIFIED/MISMATCH judgement is the rows above, this is the datum.
        print(f"  {account:<16} action={action:<7} deployed_md5={md5 or '<none>'} "
              f"activation={activation}")
    print("--- end ---")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deploy a hook without writing through symlinks.")
    ap.add_argument("--hook", required=True, help="basename under tools/hooks/")
    ap.add_argument("--accounts", default=",".join(DEFAULT_ACCOUNTS))
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = ap.parse_args(argv)

    if not (CANON_DIR / args.hook).is_file():
        print(f"no canonical at {CANON_DIR / args.hook}", file=sys.stderr)
        return 64
    # T-2227 finding 1: prove the cached ref matches the real remote BEFORE anything
    # reads origin/main. Refuse rather than fall back to the cache.
    fresh, detail = origin_is_fresh()
    if not fresh:
        print(f"REFUSING — {detail}", file=sys.stderr)
        return 65
    print(f"origin/main verified against remote @ {detail}")

    accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]
    steps = plan(args.hook, accounts)
    print(f"deploy {args.hook} -> {', '.join(accounts)}")

    # T-2227 finding 2: the staging write must not follow a symlink. The old
    # `/tmp/recovery-deploy-<hook>` was a PREDICTABLE path in a shared sticky dir opened
    # O_WRONLY|O_CREAT|O_TRUNC, so a pre-planted symlink there redirected the write —
    # the exact defect class this tool exists to prevent, one level up in its own
    # staging step. mkstemp is O_CREAT|O_EXCL with an unpredictable name, so it can
    # neither follow nor clobber. chmod 644 because the target accounts read it as
    # themselves.
    stage = None
    if not args.dry_run and any(s.action == ACTION_COPY for s in steps):
        stage = stage_canonical(args.hook)
    failures = execute(args.hook, steps, dry_run=args.dry_run, stage=stage)
    if stage:
        try:
            os.unlink(stage)
        except OSError:
            pass
    print(f"  {failures} account(s) not verified")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
