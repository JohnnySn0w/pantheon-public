#!/usr/bin/env python3
"""tools/s3star_audit_runner.py — HO-side scheduler for the ORG-038 System-3*
assurance-auditor pilot (T-1583).

Per ORG-038 Refinement 1, the standing audit cadence lives HO-side (this
script), NOT as a standing autonomous Codex arc: this script composes a
bounded, read-only audit brief on the pilot cadence, hands it to Codex for a
SUPERVISED dispatch (see "Codex-dispatch-mechanics" below), and files one
ticket per returned finding to the domain owner. It never files anything to
`target=codex` — Codex never self-closes.

CODEX-DISPATCH-MECHANICS (the load-bearing gap this script works around,
T-1583 plan-tier finding): there is no unattended, HO-side-scriptable way to
hand Codex a task on the Plus-quota path and collect a result today.
``tools.harness_adapter.codex.CodexHarnessAdapter.inject_inbound`` is a
bridge surface that raises ``NotImplementedError`` at skeleton tier (codex
inbound is poll-and-inject via tmux send-keys, not wired); headless
``codex exec`` billing is unresolved (ORG-031 open question) and ORG-038
explicitly constrains the pilot away from it. This mirrors the exact shape
``tools/watch_runner.py`` hit for Claude's own watch loop (ORG-025 retired
headless dispatch in favor of file-a-ticket -> supervised/in-session dispatch
-> land-from-disk) -- codex's situation is strictly worse (no in-session
Agent-tool equivalent HO can drive), so the same resolution applies.

Consequently this is a SUPERVISED v1: ``--file-dispatch-ticket`` files a
ticket instructing the operator (Orchestrator) to run ``--launch-audit`` (T-1604),
which spawns a DEDICATED ephemeral ``codex-audit-<slot>-<date>`` tmux session
(never the supervisor-managed standing worker session, per
``tools/codex_modal_monitor.py``'s ``CODEX_PANE`` default -- that session is
kept alive UNPROFILED by ``codex-supervise.sh`` and cannot host the profiled
auditor), writes a fresh brief to the run's workspace subdir, and launches
``codex --profile s3star-audit-p2`` reading it. The operator then observes the
run, saves Codex's returned findings to the known path, runs
``--land-findings`` to file per-finding tickets, then ``--teardown-audit`` to
kill the dedicated session. ``--auto-dispatch`` exists as an explicit flag
that raises a clear not-implemented error (mirrors
``CodexHarnessAdapter.resume_session``'s own honesty pattern) rather than
silently no-op-ing, so the gap stays visible at the exact surface a future
implementer will touch once codex inbound wiring lands. (T-1604: an earlier
version of this docstring described pasting the brief into the standing
``codex-session`` directly -- the T-1603 first-fire proved that unrealizable.)

No repo-committed audit-trail and no ledger auto-commit in v1 (T-1583 plan,
approved by HO): the filed ticket bodies ARE the durable record (embed the
full brief / finding text); scheduler state (``state.json``) and this
module's error log live under ``~/.local/state/pantheon/s3star_audit/``
(outside the repo, regenerable — same convention as ``tools/watch_runner.py``'s
state file). Per-cycle findings/runtime-snapshot artifacts live under the
shared ``AUDIT_WORKSPACE_ROOT`` instead (T-1598 — the audit runs under a
locked-down Codex profile that can only write to that one shared,
group-readable workspace; ``~/.local/state`` is not codex-traversable at
all). HO's existing ledger-append discipline (memory: append-before-groom)
lifts material findings manually. Both are named fast-follows, not v1 gaps.

Modes (default-safe contract, mirrors ``tools/watch_runner.py``):

    python3 tools/s3star_audit_runner.py --emit-brief [--slot SLOT] [--force]
        Safe mode. Print due (or --slot-named) slots' composed briefs as JSON.
        No side effects.

    python3 tools/s3star_audit_runner.py --file-dispatch-ticket [--slot SLOT] [--force] [--auto-dispatch]
        Prod mode. File ONE target=role:engineering_orchestrator dispatch ticket per due (or named)
        slot, embedding the brief + supervised hand-off instructions.
        --auto-dispatch raises a clear not-implemented error instead of running.

    python3 tools/s3star_audit_runner.py --land-findings --slot SLOT --findings-file PATH
        Prod mode. Parse a findings file Codex/the operator wrote, file one
        ticket per finding to the domain owner (never target=codex). T-4314: the
        file must end with the closing sentinel `s3star-audit-end: findings=N`
        (N = its `## Finding` sections); a missing/mismatched sentinel is rc 1
        with nothing filed, so a truncated run cannot land as a clean audit.

    python3 tools/s3star_audit_runner.py --launch-audit --slot SLOT [--date YYYY-MM-DD]
        Prod mode (T-1604). HO-run: spawn the dedicated codex-audit session,
        write a fresh brief + runtime snapshot, and launch the profiled
        auditor reading it. Returns immediately -- does not poll, land, or
        tear down (no unattended spawn-through-land path; ORG-038 stays
        supervised).

    python3 tools/s3star_audit_runner.py --teardown-audit --slot SLOT [--date YYYY-MM-DD]
        Prod mode (T-1604). HO-run: kill the dedicated codex-audit session
        for SLOT/date. Idempotent if already gone.

    python3 tools/s3star_audit_runner.py --await-audit --slot SLOT [--date YYYY-MM-DD] [--stable-seconds N] [--timeout-seconds T] [--poll-seconds P]
        Safe, READ-ONLY mode (T-5666). The executable "safe to land" gate: polls
        until the profiled codex process is gone, OR findings.md is sealed and
        findings.md / verdicts.jsonl / the run dir have been unchanged for N
        seconds (interactive codex idles at its prompt and never exits). rc 0
        cleared, 1 timeout, 2 bad args / missing run dir. Never kills, lands, or
        tears down.

State file: ``~/.local/state/pantheon/s3star_audit/state.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.watch_runner import _invoke_ticket_file  # noqa: E402 — reuse the proven retry/subprocess kernel.
from tools._md_table import _md_row  # noqa: E402  — leaf module, re-only (T-1936)
from tools.agent_role_map import (  # noqa: E402 — T-2405: identity from the manifest, never copied
    account_persona_map,
    fleet_accounts,
    mailbox_group,
    mailbox_hub,
)
from tools import ticket as _ticket_mod  # noqa: E402 — T-2613: reuse ticket.py's own
# frontmatter mutation primitives as a library (same reuse tier as
# _invoke_ticket_file above) for record_retest_verdict's in-place frontmatter
# edit. Imported as a module object (not `from tools.ticket import X`) so test
# patches on `tools.ticket.<name>` are visible through this reference too.
from tools._git_utils import git_mutation_lock  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PROPOSAL_PATH = (
    REPO_ROOT
    / "knowledge"
    / "episodic"
    / "2026-06-29-codex-s3star-auditor-proposal"
    / "proposal.md"
)

STATE_DIR = Path.home() / ".local" / "state" / "pantheon" / "s3star_audit"
STATE_FILE = STATE_DIR / "state.json"
RUNNER_LOG = Path.home() / ".local" / "log" / "pantheon" / "s3star_audit_runner.log"
TICKET_BIN = REPO_ROOT / "tools" / "ticket.py"

# T-2801: recipient for the claude-tier verification queue push (see
# push_claude_tier_queue below). Same constant name/shape as
# tools/fleet_clone_currency_check.py's ORCHESTRATOR_INBOX -- both name the same
# physical mailbox, and both are env-overridable so tests never touch the
# real hub. orchestrator is the primary claude-tier verifier and the sole owner of
# this alert (see push_claude_tier_queue's docstring for why not also recovery).
ORCHESTRATOR_INBOX = os.environ.get("PANTHEON_ORCHESTRATOR_INBOX", f"{mailbox_hub()}/orchestrator")

# STANDING_WORKER_SESSION (per tools/codex_modal_monitor.py's CODEX_PANE
# default) is the supervisor-managed ORG-031 codex worker — codex-supervise.sh
# relaunches it UNPROFILED whenever it dies. NEVER spawn/attach/send-keys into
# it for an audit dispatch (T-1604): the profile requires a dedicated session
# (see codex_audit_session_name / launch_audit_session below). Kept as a named
# constant only so dispatch-ticket ask text can name the anti-pattern
# explicitly, not because anything here launches into it.
STANDING_WORKER_SESSION = "codex-session"

# Cross-account: the codex-audit session must run AS the codex unix user (it
# needs codex's own ~/.codex credentials/profile config), mirroring
# tools/codex_modal_monitor.py's `sudo -u codex bash -lc "tmux ..."` pattern —
# the only other cross-account tmux precedent in this repo.
CODEX_AUDIT_USER = "codex"

# T-1884: sudoers grants NOPASSWD on exactly this one fixed, root-owned path
# (see tools/sudoers.d/claude-codex-audit) — never anything under this repo.
# The env override exists only so tests can point at the repo copy of
# tools/sudoers.d/pantheon-s3star-tmux without installing anything; it has
# no effect on the real cross-account path (the grant matches the installed
# path literally).
WRAPPER_PATH = os.environ.get("S3STAR_TMUX_WRAPPER", "/usr/local/bin/pantheon-s3star-tmux")

# ---------------------------------------------------------------------------
# S3* audit isolation profile (T-1598) — a dedicated, permission-enforced
# Codex profile so the read-only audit contract is structurally enforced, not
# prose-enforced. Phase 2 (2026-07-14): the launch profile is the beta
# permissions overlay /home/codex/.codex/s3star-audit-p2.config.toml —
# default_permissions extends ":workspace" with deny-READ on the four
# secret-bearing paths (.ssh, .codex, .config/codex-telegram, .gnupg),
# writes scoped to the workspace roots, mailbox writes denied, network off.
# Probe-verified 2026-07-14 (sandbox layer, codex 0.142.5): secret cat/ls
# denied under the profile + readable at baseline; mailbox write exit 2;
# workspace write exit 0; curl exit 7 (no network); and the overlay's
# default_permissions BEATS the base config.toml's legacy sandbox_mode —
# the docs' "any loaded sandbox_mode wins" claim is empirically false in
# this layering direction. First TUI fire is supervised (audit dispatches
# are supervised by design). Phase-1 fallback stays installed at
# /home/codex/.codex/s3star-audit.config.toml (legacy sandbox_mode=
# workspace-write, writable_roots=[]) — relaunch with that name if the P2
# profile misbehaves in the TUI.
AUDIT_PROFILE = "s3star-audit-p2"
#
# AUDIT_WORKSPACE_ROOT is claude:pantheon-mailbox, setgid (2770), under the shared
# /var/lib/pantheon (T-5514 moved it off /home/the-ancestor before that account is
# retired; earlier comments named the-ancestor:veneration). Codex (in group
# pantheon-mailbox) writes here as its one writable root under the profile; the
# runner (claude, also in pantheon-mailbox) both writes the runtime snapshot here directly and reads back
# whatever codex wrote, with no cross-account ACL grant needed in either
# direction. This deliberately replaces the T-1599-era convention of putting
# findings/snapshot artifacts under STATE_DIR (~/.local/state/pantheon/...,
# home-owned 750 by claude) -- that path is NOT traversable by codex at all,
# which would have made a live dispatch's find-the-findings-file step fail
# structurally. STATE_DIR remains exactly where it was for state.json + the
# runner's own log; only the per-cycle findings/snapshot artifacts move.
AUDIT_WORKSPACE_ROOT = Path("/var/lib/pantheon/s3star-workspace")
# T-4928: Recovery-captured per-seat evidence (tools/s3star_seat_evidence.py). The audit
# session runs as codex, which lost its cross-account sudo (TG 11733), so other
# accounts' user managers and transcript inventories reach the auditor only here.
SEAT_EVIDENCE_LATEST = AUDIT_WORKSPACE_ROOT / "seat-evidence" / "latest"

# The exact launch line dispatch instructions must name: cd into the shared
# workspace (so relative "your findings output file" language in the brief
# resolves there), then launch under the profile overlay so the TUI honors
# the profile's sandbox_mode/network_access/writable_roots. Single source of
# truth referenced by both the dispatch-ticket ask text and (if ever needed)
# the composed brief, so the two can't drift apart.
#
# Caveat for re-testers (not relevant to this TUI launch line, but a known
# trap if probing the profile further): the `codex sandbox` SUBCOMMAND
# ignores a profile's sandbox_mode and needs an explicit
# `-c sandbox_mode="workspace-write"` override forced on the command line.
#
# T-1604: the runner used to generate a "paste this into codex-session"
# instruction built around a CODEX_LAUNCH_CMD constant — the T-1603 first-fire
# proved that unrealizable (codex-session is the supervisor-managed standing
# worker, kept unprofiled). See run_workspace_dir / launch_audit_session below
# for the proven dedicated-session mechanism that replaces it.

# ---------------------------------------------------------------------------
# Per-run workspace + dedicated-session dispatch mechanism (T-1604).
#
# D4: each (slot, date) cycle gets its own subdir under AUDIT_WORKSPACE_ROOT
# grouping brief.md / findings.md / runtime-snapshot*.md + whatever scratch
# (.git, .agents) codex creates in its cwd -- replacing the flat
# "<date>-<slot>-<artifact>.md" naming that left cross-cycle scratch dirs
# (.git/.agents) polluting the shared workspace root (T-1603 workspace-
# lifecycle note).
# ---------------------------------------------------------------------------


def run_workspace_dir(slot_slug: str, today: date) -> Path:
    """The per-run subdir for `slot_slug`/`today`. A pure function of its
    inputs -- both `file_dispatch_ticket` (ticket-time) and
    `launch_audit_session` (actual-launch-time) must agree on this path
    without any shared state, since they may run minutes or days apart.
    """
    return AUDIT_WORKSPACE_ROOT / f"{today.isoformat()}-{slot_slug}"


def ensure_run_workspace_dir(slot_slug: str, today: date) -> Path:
    """Create (idempotently) and return the per-run workspace subdir.

    Explicitly chmods 2770 rather than trusting umask-inherited permissions:
    a fresh mkdir under AUDIT_WORKSPACE_ROOT's setgid bit inherits GROUP
    ownership (veneration) automatically, but NOT necessarily group-write --
    umask could silently strip it, and the codex-account auditor structurally
    needs to write its findings file + scratch into this exact directory.
    """
    run_dir = run_workspace_dir(slot_slug, today)
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o2770)
    return run_dir


def codex_audit_session_name(slot_slug: str, today: date) -> str:
    """D1: deterministic dedicated-session name, never STANDING_WORKER_SESSION.
    Both launch and teardown recompute this from (slot, date) -- no session-
    name bookkeeping for the operator to copy/paste between the two calls.
    """
    return f"codex-audit-{slot_slug}-{today.isoformat()}"


def _sudo_codex_argv(verb: str, *args: str) -> list[str]:
    """Cross-account exec shape (T-1884): `sudo -n -u codex WRAPPER_PATH
    <verb> <args...>`. `-n` (non-interactive) makes a missing/misconfigured
    NOPASSWD grant fail instantly with a clear stderr instead of hanging on
    a password prompt that can never be answered from a tty-less Claude Code
    Bash call -- the exact failure mode T-1884 exists to fix.

    Replaces the earlier `sudo -u codex bash -lc "<free-form tmux string>"`
    shape: sudoers could then only grant NOPASSWD via a wildcard match over
    the whole quoted command string, which is unverifiable before install
    and brittle (P1: architecture over policy). The wrapper
    (tools/sudoers.d/pantheon-s3star-tmux) is a fixed, root-owned executable
    that does its own verb/session/path validation in bash (plain-unittest-
    testable); the paired sudoers grant (tools/sudoers.d/claude-codex-audit)
    becomes one line matching a fixed PATH, no quoted-string semantics to
    reason about. The wrapper's header also now owns the login-shell/env
    resolution the old `-lc` provided -- `args` here are passed through as
    literal argv elements with no shell re-parsing between this call and
    the wrapper (see QuotingTests / WrapperScriptTests for the injection-
    safety proof of that claim).
    """
    return ["sudo", "-n", "-u", CODEX_AUDIT_USER, WRAPPER_PATH, verb, *args]


def _codex_launch_command(read_instruction: str) -> str:
    """D2: the literal text sent into the codex-audit pane via the wrapper's
    `send` verb (-> `tmux send-keys ... "<this>" Enter`), launching codex
    with the read-instruction as its positional PROMPT arg (codex --help:
    "[PROMPT] Optional user prompt to start the session").

    T-1884: this is now the ONLY remaining shell-parsing layer in the whole
    chain -- sudo, the wrapper, and `tmux send-keys` all pass this string
    through as one literal argv element (no re-parsing in between); only
    the codex-audit PANE's own interactive bash, which receives it as if a
    human had typed it, ever re-parses it. `shlex.quote` here is for that
    one pass (round-trip-verified in QuotingTests, not just by inspection).
    """
    return f"codex --profile {AUDIT_PROFILE} {shlex.quote(read_instruction)}"


_SUDO_NOPASSWD_REFUSAL_MARKER = "a password is required"


def _missing_nopasswd_grant_hint(stderr: str | None) -> str | None:
    """T-1884: `sudo -n`'s refusal stderr when the NOPASSWD grant is missing
    or misconfigured contains this marker. Loud-and-specific rather than
    leaving an operator to archaeology: names both committed source files
    plus the install step. Returns None for any other failure shape (e.g. a
    real tmux error) so the hint stays targeted, not blanket noise appended
    to every failure.
    """
    if _SUDO_NOPASSWD_REFUSAL_MARKER not in (stderr or "").lower():
        return None
    return (
        "missing/misconfigured sudo NOPASSWD grant for claude -> codex "
        "(T-1884): install tools/sudoers.d/claude-codex-audit to "
        "/etc/sudoers.d/ (visudo -cf first) AND tools/sudoers.d/"
        "pantheon-s3star-tmux to /usr/local/bin/ (root:root 0755) -- see "
        "each file's header for exact install + verify commands."
    )


def _loud_sudo_failure(prefix: str, result) -> str:
    """Compose a RuntimeError message: `prefix` + rc/stderr, plus (if the
    failure matches sudo's `-n` refusal shape) the NOPASSWD-grant install
    hint from `_missing_nopasswd_grant_hint`.
    """
    msg = f"{prefix}: rc={result.returncode} stderr={result.stderr!r}"
    hint = _missing_nopasswd_grant_hint(result.stderr)
    if hint:
        msg += f" | {hint}"
    return msg


_LAUNCH_READ_INSTRUCTION = (
    "Read ./brief.md in your current directory and execute the audit brief "
    "it contains. Begin."
)

_SESSION_STEP_TIMEOUT_SECONDS = 15


def launch_audit_session(
    slot: AuditSlot,
    today: date,
    *,
    run=subprocess.run,
    read_instruction: str = _LAUNCH_READ_INSTRUCTION,
) -> dict[str, Any]:
    """D1-D3: spawn the dedicated codex-audit session for `slot`/`today`,
    write a FRESH brief + FRESH runtime snapshot into its per-run subdir, and
    launch the profiled auditor reading it.

    Deliberately recomposes its own brief/snapshot rather than reusing
    whatever `file_dispatch_ticket` embedded in the dispatch ticket at
    due-notice time -- HO may run this minutes, hours, or days after the
    ticket was filed, and fresher runtime evidence is strictly better. The
    snapshot is written to a SIBLING file (runtime-snapshot-at-launch.md),
    never overwriting `file_dispatch_ticket`'s runtime-snapshot.md: that
    ticket-time file is only REFERENCED (by path) from the filed ticket, not
    embedded, so overwriting it would silently destroy evidence the ticket
    still points at (T-1604 directive 1).

    Returns without polling, landing, or tearing down -- HO observes the
    live session, saves findings, runs --land-findings, then --teardown-audit
    as separate deliberate steps. No --poll-and-land: ORG-038 supervised-not-
    unattended draws the line at the HO-triggered launch + observation, and
    building an unattended spawn-through-land path would cross it.

    Raises RuntimeError (loud, uncaught) if either the session-spawn or the
    codex-launch step exits nonzero -- mirrors this module's existing
    loud-failure convention (_raise_auto_dispatch_not_implemented) rather
    than silently leaving a half-spawned session behind.
    """
    run_dir = ensure_run_workspace_dir(slot.slug, today)
    findings_path = run_dir / "findings.md"
    snapshot_path = run_dir / "runtime-snapshot-at-launch.md"
    capture_runtime_snapshot(snapshot_path)
    brief = compose_audit_brief(
        slot, today, findings_path, snapshot_path=snapshot_path,
        tickets_root=REPO_ROOT / "tickets",
    )
    brief_path = run_dir / "brief.md"
    _write_immutable_evidence(brief_path, brief)

    session = codex_audit_session_name(slot.slug, today)

    spawn = run(
        _sudo_codex_argv("new", session, str(run_dir)),
        capture_output=True, text=True, timeout=_SESSION_STEP_TIMEOUT_SECONDS, check=False,
    )
    if spawn.returncode != 0:
        raise RuntimeError(_loud_sudo_failure(
            f"launch_audit_session: failed to spawn session {session!r}", spawn,
        ))

    launch = run(
        _sudo_codex_argv("send", session, _codex_launch_command(read_instruction)),
        capture_output=True, text=True, timeout=_SESSION_STEP_TIMEOUT_SECONDS, check=False,
    )
    if launch.returncode != 0:
        raise RuntimeError(_loud_sudo_failure(
            f"launch_audit_session: session {session!r} spawned, but launching codex in "
            f"it failed",
            launch,
        ))

    return {
        "session": session,
        "run_dir": run_dir,
        "brief_path": brief_path,
        "findings_path": findings_path,
        "snapshot_path": snapshot_path,
    }


def teardown_audit_session(slot_slug: str, today: date, *, run=subprocess.run) -> bool:
    """Kill the dedicated session for `slot_slug`/`today` (D1: teardown after
    landing). Idempotent: a not-found session (already torn down, or never
    spawned) counts as success; any other nonzero exit is a loud RuntimeError.
    """
    session = codex_audit_session_name(slot_slug, today)
    result = run(
        _sudo_codex_argv("kill", session),
        capture_output=True, text=True, timeout=_SESSION_STEP_TIMEOUT_SECONDS, check=False,
    )
    if result.returncode == 0:
        return True
    stderr = (result.stderr or "").lower()
    if "can't find session" in stderr or "no such" in stderr:
        return True
    raise RuntimeError(_loud_sudo_failure(
        f"teardown_audit_session: failed to tear down session {session!r}", result,
    ))


# ---------------------------------------------------------------------------
# Audit slots — the pilot's two cadences (proposal.md §Audit program #1:
# "one narrow audit weekly and one integrated audit monthly"). Change-triggered
# (#3) and incident-triggered (#4) audits are event-driven, not cadence-based,
# and are explicitly OUT of this scheduler's scope (fast-follow candidate).
# ---------------------------------------------------------------------------

AUDIT_SLOTS: dict[str, dict[str, str]] = {
    "weekly-narrow": {
        "cadence": "weekly",
        "scoping_policy": (
            "This is the WEEKLY NARROW audit cycle. Select 1-3 coverage areas from "
            "the checklist below for THIS cycle, rotating your selection so every "
            "area gets visited at least once across a month. State which areas you "
            "chose and why in your findings preamble."
        ),
    },
    "monthly-integrated": {
        "cadence": "monthly",
        "scoping_policy": (
            "This is the MONTHLY INTEGRATED audit cycle. Cover the full checklist "
            "below plus the policy-versus-reality sampling section this cycle."
        ),
    },
}
VALID_SLOTS = set(AUDIT_SLOTS)


@dataclass
class AuditSlot:
    slug: str
    cadence: str
    scoping_policy: str


def get_slot(slug: str) -> AuditSlot:
    if slug not in AUDIT_SLOTS:
        raise ValueError(f"unknown audit slot {slug!r}; expected one of {sorted(VALID_SLOTS)}")
    cfg = AUDIT_SLOTS[slug]
    return AuditSlot(slug=slug, cadence=cfg["cadence"], scoping_policy=cfg["scoping_policy"])


# ---------------------------------------------------------------------------
# Cadence / due-computation — same weekly/monthly boundary logic as
# tools/watch_runner.py's Topic.is_due, adapted for the two fixed slots.
# ---------------------------------------------------------------------------


def is_due(cadence: str, last_fire: date | None, today: date) -> bool:
    if last_fire is None:
        return True
    if cadence == "weekly":
        iso_today = today.isocalendar()
        iso_last = last_fire.isocalendar()
        return (iso_last.year, iso_last.week) < (iso_today.year, iso_today.week)
    if cadence == "monthly":
        return (last_fire.year, last_fire.month) < (today.year, today.month)
    return False


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"schema_version": 1, "slots": {}}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def last_fire_date(state: dict[str, Any], slug: str) -> date | None:
    entry = state.get("slots", {}).get(slug)
    if not entry or not entry.get("last_fire"):
        return None
    return date.fromisoformat(entry["last_fire"])


def update_state(state: dict[str, Any], slug: str, fired_on: date) -> None:
    state.setdefault("slots", {})[slug] = {"last_fire": fired_on.isoformat()}


# ---------------------------------------------------------------------------
# Runtime evidence snapshot (T-1599) — runner-captured host-side evidence so
# the sandboxed S3* auditor gets ground truth its own bwrap sandbox
# structurally cannot see: PID namespace hides host processes, uid/gid
# overflow-mapping falsifies ownership as 65534 nobody:nogroup, and the
# systemd --user bus is unreachable from inside the sandbox (S3STAR-W1-004 /
# T-1596; uid-mapping mechanism confirmed in T-1595's triage).
#
# Captured at REAL-dispatch brief-composition time only (file_dispatch_ticket
# below), never during --emit-brief preview -- that mode's documented
# "No side effects" contract stays exactly as-is; --emit-brief still shows
# the snapshot's WOULD-BE path in its JSON preview for parity with the
# existing findings_path convention, it just never runs the capture.
# ---------------------------------------------------------------------------

# T-2405: DERIVED from instance.yaml, not copied from it.
#
# These were three hardcoded literals -- five personas, five accounts, and the
# group name `veneration`. The fleet then grew by three accounts
# (suborch1/suborch2/exocortex) and the group was renamed to pantheon-mailbox,
# and this capture went on describing the old fleet. The 2026-08-01 audit found
# it from the inside: its own process artifact showed all three omitted
# accounts ACTIVE while the identity artifact did not cover them, and the group
# query exited 2. An evidence channel that silently narrows is worse than a
# missing one, because a gap in a bundle is read as absence-of-thing.
#
# The persona/account distinction the old comment made is REAL and preserved:
# mailbox dirs are keyed by PERSONA (orchestrator/specialist/recovery), `id` takes ACCOUNTS
# (claude/claude-research/claude-fallback) -- `id orchestrator` fails on this host.
# account_persona_map() is exactly that mapping, so both lists now come from
# one source that cannot drift apart.
_SNAPSHOT_MAILBOX_HUB = Path(mailbox_hub())
_SNAPSHOT_MAILBOX_AGENTS: tuple[str, ...] = tuple(sorted(account_persona_map().values()))
_SNAPSHOT_UNIX_USERS: tuple[str, ...] = tuple(fleet_accounts())
_SNAPSHOT_MAILBOX_GROUP = mailbox_group()

# T-2406: the goal mapping every ticket this module files carries.
#
# Machine-filed tickets are the one place the `serves` field is unambiguously
# free to set -- there is no author to interrupt and no judgment to make, since
# every ticket this module emits is ORG-038 assurance work on the org system
# itself. The 2026-08-01 audit filed T-2385 through this path with no mapping
# at all, then found its own dispatch unmapped.
#
# The finding also observed that `serves` is OPTIONAL in the schema and its lint
# is advisory. That is NOT, on its face, the violation it looks like:
# rulebook/policies/goal-mapping.md explicitly permits routine work to carry its
# mapping "in one clause of the ledger entry's justifies-via -- no separate
# artifact". Making the field mandatory is a policy-tier change with a
# 106-ticket backfill behind it, so it is named in T-2406's resolve note rather
# than smuggled in here.
_SERVES = "pantheon (org system) — ORG-038 S3* assurance"

_SNAPSHOT_TIMEOUT_SECONDS = 15

# ---------------------------------------------------------------------------
# Session isolation profile note (T-1598) — unconditional, unlike the
# optional snapshot section: every composed brief must tell the auditor it
# runs under a permission-enforced profile, not just a prose contract, and
# fold in the T-1593 re-test self-permission-check probes.
# ---------------------------------------------------------------------------

_ISOLATION_PROFILE_SECTION = f"""
## Session isolation profile
This session runs under a permission-enforced isolation profile (ORG-038 /
T-1598), not just a prose contract: writes are scoped to
`{AUDIT_WORKSPACE_ROOT}`, you hold no mailbox send/consume authority, and
network access is disabled. Findings still land via the workspace findings
file for runner-side `--land-findings` parsing -- write your findings file
INSIDE the workspace, at the exact path given below in Output, not anywhere
else (writes outside the workspace will fail under the profile).

Self-permission-check (part of your own audit -- run these and confirm all
three return false; each returning false is the T-1593 re-test PASS
criteria):
```
test -w /home/codex/pantheon-config
test -w /home/codex/codex-config
test -w {_SNAPSHOT_MAILBOX_HUB}/orchestrator
```
"""

_SNAPSHOT_CAVEAT = (
    "CAVEAT: this snapshot is runner-narrated evidence, captured host-side by "
    "s3star_audit_runner.py itself -- NOT the auditor's own direct observation. "
    "Per P4, runner-narration is structurally weaker than auditor-direct "
    "observation (single-narrator risk: nothing independently corroborates the "
    "capture step). Cite it as runner-narrated evidence; do not treat it as "
    "equivalent to a first-party auditor finding."
)


@dataclass
class _SnapshotCommand:
    label: str
    argv: list[str]
    slug: str


def _snapshot_commands() -> list[_SnapshotCommand]:
    """The fixed set of read-only, metadata/listing-only capture commands.

    No command here can read file contents or dump environment variables --
    that is a structural property of the command set, not a runtime filter,
    so nothing secret-bearing can enter the snapshot (T-1599 requirement).
    """
    hub = _SNAPSHOT_MAILBOX_HUB
    roots = [str(hub)] + [str(hub / agent) for agent in _SNAPSHOT_MAILBOX_AGENTS]
    return [
        _SnapshotCommand(
            "systemctl --user list-timers --all --no-pager",
            ["systemctl", "--user", "list-timers", "--all", "--no-pager"],
            slug="systemd-timers",
        ),
        _SnapshotCommand(
            "systemctl --user list-units --all --no-pager",
            ["systemctl", "--user", "list-units", "--all", "--no-pager"],
            slug="systemd-units",
        ),
        _SnapshotCommand(
            "ps -eo user,pid,ppid,stat,lstart,comm,args",
            ["ps", "-eo", "user,pid,ppid,stat,lstart,comm,args"],
            slug="processes",
        ),
        _SnapshotCommand("ss -lntup", ["ss", "-lntup"], slug="sockets"),
        _SnapshotCommand(
            "ls -ld -- mailbox hub + per-agent roots (named ids)",
            ["ls", "-ld", *roots],
            slug="ownership-named",
        ),
        _SnapshotCommand(
            "ls -ldn -- mailbox hub + per-agent roots (numeric ids)",
            ["ls", "-ldn", *roots],
            slug="ownership-numeric",
        ),
        _SnapshotCommand(
            f"getent group {_SNAPSHOT_MAILBOX_GROUP}",
            ["getent", "group", _SNAPSHOT_MAILBOX_GROUP],
            slug="identity-group",
        ),
        _SnapshotCommand(
            f"id -- {len(_SNAPSHOT_UNIX_USERS)} declared Pantheon unix accounts (T-1596/T-2405)",
            ["id", *_SNAPSHOT_UNIX_USERS],
            slug="identity-users",
        ),
    ]


def _run_snapshot_command(cmd: _SnapshotCommand, *, run) -> str:
    """Run one capture command; NEVER raises (warn-not-fail, T-1599).

    Returns the section body: captured stdout on success (with a WARNING
    prefix plus any stdout that did come back on a non-zero exit -- e.g. the
    real T-1596 "Failed to connect to bus" case), or a bare `WARNING: ...`
    line describing why the capture is missing/degraded.

    Scope: warn-not-fail covers command execution only, not the snapshot
    file-write in `capture_runtime_snapshot` -- a write failure is a real
    infra fault and is allowed to raise.
    """
    try:
        result = run(
            cmd.argv, capture_output=True, text=True,
            timeout=_SNAPSHOT_TIMEOUT_SECONDS, check=False,
        )
    except FileNotFoundError:
        return f"WARNING: command not found: {cmd.argv[0]}"
    except subprocess.TimeoutExpired:
        return f"WARNING: command timed out after {_SNAPSHOT_TIMEOUT_SECONDS}s: {cmd.label}"
    except OSError as exc:
        return f"WARNING: capture failed ({exc!r}): {cmd.label}"

    stdout = (result.stdout or "").rstrip("\n")
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        note = f"WARNING: exited {result.returncode}" + (f" -- {stderr}" if stderr else "")
        return f"{note}\n\n{stdout}".strip() if stdout else note
    return stdout if stdout else "(empty output)"


def _write_immutable_evidence(path: Path, text: str, *, mode: int = 0o444) -> None:
    """Write `text` to `path` and lock it read-only immediately after (T-1732 /
    S3STAR-2026-07-13-001): runner-captured evidence artifacts must be
    structurally immutable to the audit worker session, not prose-guarded.

    Unlinks any pre-existing file at `path` rather than chmod'ing it writable
    first: `capture_runtime_snapshot`'s and `launch_audit_session`'s callers can
    both re-target a path a prior cycle already locked down (a same-day re-run
    of `file_dispatch_ticket`, or a re-launch after `--teardown-audit`). Unlink
    always produces a fresh inode, so the artifact is never transiently
    writable again and a crash mid-rewrite can't leave a should-be-immutable
    file silently writable, unlike a chmod-back-then-rewrite sequence would.
    The parent workspace dir (2770) permits the unlink regardless of the
    target file's own mode.
    """
    if path.exists():
        path.unlink()
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


# ---------------------------------------------------------------------------
# Evidence bundle (T-1596) -- one immutable, sha256'd file per captured
# command plus a MANIFEST, alongside the composed snapshot markdown above.
# Built from the SAME command-execution pass `capture_runtime_snapshot`
# already does for the composed doc -- never re-runs the commands, so the
# two representations can never disagree with each other about point-in-time
# state (T-1596 plan-review requirement).
# ---------------------------------------------------------------------------


def manifest_path_for(snapshot_path: Path) -> Path:
    """The evidence-bundle manifest path for `snapshot_path`'s run dir.

    A pure function of its input, mirroring `run_workspace_dir`'s rationale
    (T-1604 D4): both `capture_runtime_snapshot` (capture time) and
    `compose_audit_brief`'s callers (preview / launch / dispatch time) need
    this path and must not drift apart.
    """
    return snapshot_path.parent / "evidence" / "MANIFEST.md"


def _captured_as() -> str:
    """The unix account running this capture. stdlib lookup, no subprocess --
    the runner already knows its own identity without shelling out, and a
    subprocess call here would be one more command that could fail for no
    reason (T-1596 HARD constraint: no new capture-failure surface).
    """
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        return os.environ.get("USER", "unknown")


# The account this capture runs as; every other DECLARED account's `--user`
# manager is out of reach from here.
_CAPTURING_ACCOUNT = "claude"

# T-2405: derived, not typed out. The hardcoded three-account version of the
# sentence below went stale the moment the fleet grew to eight, and a Limits
# section that UNDERSTATES the gap is the one failure this section cannot
# afford -- it is the thing a reader trusts to bound their conclusions.
_OTHER_ACCOUNTS_CLAUSE = (
    ", ".join(f"`{a}`" for a in _SNAPSHOT_UNIX_USERS if a != _CAPTURING_ACCOUNT)
    or "(no other accounts declared)"
)

_EVIDENCE_LIMITS_MD = f"""## Limits -- what this bundle cannot see

Absence of an item below is absence-of-channel, not absence-of-thing --
do not read a gap here as a negative finding.

- **Other accounts' `systemctl --user` managers.** This bundle captures the
  `{_CAPTURING_ACCOUNT}` account's own `--user` systemd manager only (covers the
  pantheon mailbox-wake, knowledge-sweep, and s3star-audit timers).
  {_OTHER_ACCOUNTS_CLAUSE} each run their OWN `--user` manager, only
  introspectable while running as that account -- there is no cross-account
  `systemctl --user` read path without sudo, and this channel deliberately
  never uses sudo.
- **Cross-account socket-to-process attribution.** `ss -lntup` runs as
  `claude`; sockets owned by other accounts show port/protocol only, with no
  PID/process name -- the kernel does not expose inode-to-process resolution
  for another uid's sockets to an unprivileged caller.
- **Point-in-time, not continuous.** Every artifact reflects state at
  `generated_at` in this manifest's frontmatter, not a live view.
"""


def _coverage_parity(results: list[tuple[_SnapshotCommand, str]]) -> tuple[bool, list[str]]:
    """T-2405: check the CAPTURED artifacts against what instance.yaml declares.

    Deriving the command list from the manifest (above) fixes the question being
    asked; it does not prove the answer came back. `id` can fail for one account,
    a mailbox root can be absent, `getent` can miss the group -- each of which
    leaves a bundle that looks complete and covers less than it claims. That is
    the shape the 2026-08-01 audit found, and the shape a reader cannot detect,
    so it is asserted here rather than left to the reader.

    Returns (ok, lines). `ok=False` means a DECLARED identity is missing from the
    capture -- a degraded control, not a finding about the host: the honest read
    is "this bundle cannot speak to that identity", never "that identity is gone".
    """
    bodies = {cmd.slug: body for cmd, body in results}
    gaps: list[str] = []

    users_body = bodies.get("identity-users", "")
    missing_accounts = [a for a in _SNAPSHOT_UNIX_USERS if f"({a})" not in users_body]
    if missing_accounts:
        gaps.append(
            f"- accounts declared in instance.yaml but absent from `identity-users.txt`: "
            f"{', '.join(missing_accounts)}"
        )

    owner_body = bodies.get("ownership-named", "")
    missing_roots = [
        p for p in _SNAPSHOT_MAILBOX_AGENTS
        if str(_SNAPSHOT_MAILBOX_HUB / p) not in owner_body
    ]
    if missing_roots:
        gaps.append(
            f"- mailbox roots for declared personas absent from `ownership-named.txt`: "
            f"{', '.join(missing_roots)}"
        )

    group_body = bodies.get("identity-group", "")
    if not group_body.startswith(f"{_SNAPSHOT_MAILBOX_GROUP}:"):
        gaps.append(
            f"- group `{_SNAPSHOT_MAILBOX_GROUP}` (instance.yaml "
            f"`persona_installer.mailbox.group`) did not resolve; "
            f"`identity-group.txt` holds: {(group_body.splitlines() or ['(empty)'])[0]}"
        )

    if gaps:
        return False, [
            "**DEGRADED — this bundle covers less than the manifest declares.**",
            "",
            "Treat every identity/isolation conclusion drawn from it as provisional.",
            "",
            *gaps,
        ]
    return True, [
        f"OK — all {len(_SNAPSHOT_UNIX_USERS)} declared accounts, all "
        f"{len(_SNAPSHOT_MAILBOX_AGENTS)} declared mailbox roots, and group "
        f"`{_SNAPSHOT_MAILBOX_GROUP}` are present in this bundle.",
    ]


def _write_evidence_bundle(
    evidence_dir: Path,
    results: list[tuple[_SnapshotCommand, str]],
    *,
    generated_at: str,
) -> None:
    """Write one immutable file per captured command plus MANIFEST.md.

    `results` is the SAME (cmd, body) list the caller already produced for
    the composed doc -- no re-execution here. Never raises on a degraded
    (WARNING-prefixed) body -- that is recorded as a MANIFEST row, not a
    failure (T-1596: capture-what-you-can, record failures loud, never abort
    dispatch for a missing/degraded snapshot).
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(evidence_dir, 0o2770)

    rows: list[str] = []
    notes: list[str] = []
    for cmd, body in results:
        ok = not body.startswith("WARNING")
        artifact_path = evidence_dir / f"{cmd.slug}.txt"
        _write_immutable_evidence(artifact_path, body + "\n")
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        status = "OK" if ok else "WARNING"
        rows.append(_md_row(f"`{artifact_path.name}`", cmd.label, status, f"`{digest}`"))
        if not ok:
            reason = body.splitlines()[0] if body else "(empty)"
            notes.append(f"- `{artifact_path.name}`: {reason}")

    notes_section = ""
    if notes:
        notes_section = "\n## Capture notes\n\n" + "\n".join(notes) + "\n"

    parity_ok, parity_lines = _coverage_parity(results)
    heading = "## Coverage parity vs instance.yaml" + ("" if parity_ok else " — DEGRADED")
    parity_section = f"\n{heading}\n\n" + "\n".join(parity_lines) + "\n"

    captured_as = _captured_as()
    manifest_text = (
        f"---\ngenerated_at: {generated_at}\ncaptured_as: {captured_as}\n"
        f"kind: s3star-evidence-manifest\n---\n\n"
        f"# S3* runner-captured evidence bundle manifest\n\n"
        f"{_SNAPSHOT_CAVEAT}\n\n"
        f"Generated: {generated_at} (UTC) by `{captured_as}`, outside any audit "
        f"sandbox.\n\n"
        f"## Artifacts\n\n"
        f"| file | command | status | sha256 |\n"
        f"|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n"
        + notes_section
        + parity_section
        + f"\n{_EVIDENCE_LIMITS_MD}"
    )
    _write_immutable_evidence(evidence_dir / "MANIFEST.md", manifest_text)


def capture_runtime_snapshot(
    snapshot_path: Path,
    *,
    generated_at: str | None = None,
    run=subprocess.run,
) -> Path:
    """Capture runner-side host runtime evidence and write it to `snapshot_path`,
    plus a per-artifact evidence bundle + MANIFEST alongside it (T-1596).

    Warn-not-fail per command (see `_run_snapshot_command`); the file writes
    themselves are NOT wrapped -- a disk-full/permission fault here is a real
    runner failure, and a brief that references paths with nothing behind
    them is worse than a loud failure at dispatch time.

    Runs each command exactly once and reuses the results for both the
    composed markdown below and the evidence bundle -- two separate capture
    passes could observe different point-in-time state and silently disagree
    with each other, which would be worse than either representation alone.
    """
    ts = generated_at if generated_at is not None else _utc_iso_now()
    results = [(cmd, _run_snapshot_command(cmd, run=run)) for cmd in _snapshot_commands()]
    sections = [f"## {cmd.label}\n\n```\n{body}\n```" for cmd, body in results]
    text = (
        f"---\ngenerated_at: {ts}\nkind: s3star-runtime-snapshot\n---\n\n"
        f"# S3* runner-captured host runtime evidence snapshot\n\n"
        f"{_SNAPSHOT_CAVEAT}\n\n"
        f"Generated: {ts} (UTC)\n\n"
        + "\n\n".join(sections)
        + "\n"
    )
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    _write_immutable_evidence(snapshot_path, text)
    _write_evidence_bundle(snapshot_path.parent / "evidence", results, generated_at=ts)
    return snapshot_path


# ---------------------------------------------------------------------------
# Brief composition — extracts the independence contract / audit authority /
# finding format / severity calibration / audit-program coverage sections
# VERBATIM from proposal.md at runtime (single source of truth; avoids a
# hand-copied duplicate drifting from the adopted-as-written proposal text).
# ---------------------------------------------------------------------------


def load_proposal_text(path: Path = PROPOSAL_PATH) -> str:
    return path.read_text(encoding="utf-8")


def _extract_markdown_section(text: str, header: str, *, level: str = "##") -> str:
    """Return the body of a `<level> <header>` markdown section verbatim, up to
    the next same-level heading or EOF. Raises ValueError if not found.
    """
    pattern = re.compile(rf"^{re.escape(level)} {re.escape(header)}\s*$", re.MULTILINE)
    m = pattern.search(text)
    if not m:
        raise ValueError(f"section {header!r} (level {level!r}) not found")
    start = m.end()
    next_m = re.search(rf"^{re.escape(level)} ", text[start:], re.MULTILINE)
    end = start + next_m.start() if next_m else len(text)
    return text[start:end].strip()


# ---------------------------------------------------------------------------
# Open re-tests (T-1898) — carry previously-filed findings' Re-test criteria
# into the next composed brief, so a remediated finding actually gets
# independently re-tested instead of silently bouncing on a not_before gate
# that outlives whatever cadence happens to fire next (T-1733/T-1734 gap:
# the daily dispatch-ticket timer only files a NEW audit dispatch when a
# slot's cadence is due -- it does not, by itself, carry forward what a
# PRIOR cycle's still-open findings need re-tested).
# ---------------------------------------------------------------------------

_FINDING_MARKER_PREFIX = "s3star-audit-finding:"
_RETEST_CRITERIA_RE = re.compile(r"^Re-test criteria:\s*(.+)$", re.MULTILINE)
_TICKET_ID_RE = re.compile(r"^id:\s*(\S+)", re.MULTILINE)
_TICKET_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_RETEST_NOT_EXTRACTABLE = "criteria not machine-extractable - read the ticket"

# T-3849 (remediates T-2795 Codex FAIL, defect 2): retest_status values that
# are a STRUCTURAL closure marker regardless of which state directory the
# ticket file happens to sit in. Shared between find_open_retest_tickets'
# uniform exclusion check below and (by cross-reference in its own
# docstring) record_retest_verdict's cross-invocation replay guard.
_CLOSED_RETEST_STATUSES = frozenset({"verified", "overridden"})


@dataclass
class OpenRetest:
    ticket_id: str
    title: str
    criteria: str | None  # None -> Re-test criteria line not found (tolerant fallback)
    # T-2796: whether the finding's own `authorship-flagged` label is set
    # (ORG-038 Refinement 2 -- the T-2613 class). Drives BOTH
    # render_open_retests_section's Codex-facing exclusion and
    # render_claude_tier_verification_queue's inclusion. Default False so
    # any (currently nonexistent) direct construction site stays valid.
    authorship_flagged: bool = False


def _extract_ticket_id(text: str, *, fallback: str) -> str:
    m = _TICKET_ID_RE.search(text)
    return m.group(1).strip() if m else fallback


def _extract_ticket_title(text: str) -> str:
    m = _TICKET_TITLE_RE.search(text)
    return m.group(1).strip() if m else "(untitled)"


def _extract_retest_criteria(text: str) -> str | None:
    """Pull the finding's own Re-test criteria line, or None if absent.

    Scans the WHOLE ticket text rather than slicing out just the ## Context
    section: `build_finding_ticket_bodies` is the only writer that emits a
    colon-anchored "Re-test criteria: ..." line, and free-form prose added
    later (e.g. an HO verification note reading "Re-test criteria are now
    satisfiable...") never matches the anchored prefix, so an unconfined
    scan is safe and avoids parsing markdown section boundaries.
    """
    m = _RETEST_CRITERIA_RE.search(text)
    return m.group(1).strip() if m else None


def find_open_retest_tickets(tickets_root: Path) -> list[OpenRetest]:
    """Scan tickets/{inbox,active,done}/ for s3star finding tickets and pull
    each one's Re-test criteria for the composed brief's "Open re-tests"
    section.

    Deliberately does NOT filter on `not_before`: ORG-019 block discipline
    keeps not_before-gated tickets in inbox/ (never a separate blocked/
    dir), and the scheduled audit dispatch this brief is FOR is itself the
    re-test the gate is deferring to -- excluding gated tickets here would
    recreate the exact gap this ticket fixes (a remediated finding silently
    not re-tested because its not_before hadn't lapsed on some other
    cadence's clock; see T-1733/T-1734).

    Matches both current-runner-filed tickets and pre-T-1898 legacy tickets
    (e.g. T-1733/T-1734/T-1885) on the same `_FINDING_MARKER_PREFIX`
    substring `_finding_marker` has always emitted -- verified byte-identical
    across all three real on-disk tickets, so no separate legacy-detection
    path is needed.

    T-2613: a finding ticket the domain owner has REMEDIATED moves to done/
    with `state: done` -- but "moved to done/" only means "no longer being
    worked", not "independently re-verified closed" (ORG-038's whole
    no-self-closure point). So done/ tickets are included too, gated on the
    structural `retest_status` frontmatter field:
      - inbox/ and active/: always included. A finding still there hasn't
        even been remediated yet, so it is unconditionally open -- no field
        needed to say so.
      - done/: included ONLY when `retest_status: pending`. A MISSING field
        means "pre-T-2613, already independently closed" (the field is only
        written going forward, at filing time) -- treating a missing field
        as open would silently re-surface every already-closed legacy
        finding (e.g. T-1733/T-1734/T-1885). `verified`/`overridden` mean a
        verdict already closed it.

    T-2799/T-2796: every matched ticket's frontmatter is now parsed (not
    just done/'s retest_status gate) to read its `authorship-flagged`
    label structurally -- `OpenRetest.authorship_flagged` feeds both
    `render_open_retests_section`'s Codex-facing exclusion and
    `render_claude_tier_verification_queue`'s inclusion. A ticket whose
    frontmatter fails to parse is excluded from the result ENTIRELY, for
    ALL three states -- fail CLOSED on that individual ticket's inclusion
    (we cannot structurally confirm it is NOT authorship-flagged, so
    defaulting it to "unflagged" could leak a flagged finding into
    Codex's brief). The scan itself still never raises or blanks the
    whole section -- one bad ticket file is simply dropped, same
    "unattended path must not break" contract as before.

    T-3849 (remediates T-2795 Codex FAIL, defect 2): a `retest_status` of
    `verified` or `overridden` (`_CLOSED_RETEST_STATUSES`) is a STRUCTURAL
    closure marker, and closure does not depend on which state directory
    holds the ticket file. T-2795/T-2796 themselves stayed in inbox/ past
    their PASS verdict for an unrelated governance-ambiguity reason
    (2026-09-13) -- the pre-fix code only ever checked `retest_status` for
    done/, so those two already-verified findings kept getting
    re-enumerated as "open" every cycle, forcing wasted Codex
    re-verifications. The closed-status check below now runs for
    inbox/active exactly as it already implicitly did for done/ (verified/
    overridden were never "pending" either, so done/'s pre-fix gate always
    excluded them too).

    The done/-only "missing field means legacy-closed" fallback (previous
    paragraph) stays a done/-only rule and is NOT folded into the uniform
    closed-status check: a MISSING field means the opposite thing in
    inbox/active. `file_finding`/`build_finding_ticket_bodies` never write
    `retest_status` at filing time, so an inbox/active finding with no
    field yet is simply un-verdicted -- not closed. Collapsing that into
    the done/-only fallback would silently drop every freshly-filed
    finding from its own first audit cycle.
    """
    retests: list[OpenRetest] = []
    for state, is_done in (("inbox", False), ("active", False), ("done", True)):
        state_dir = tickets_root / state
        if not state_dir.exists():
            continue
        for p in sorted(state_dir.glob("*.md")):
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            if _FINDING_MARKER_PREFIX not in text:
                continue
            try:
                fields, _body = _ticket_mod.parse_front_matter(text)
            except _ticket_mod.TicketError:
                continue
            status = fields.get("retest_status")
            if status in _CLOSED_RETEST_STATUSES:
                continue  # structural closure marker -- regardless of directory
            if is_done and status != "pending":
                continue  # done/-only: legacy missing-field-means-closed fallback
            flagged = "authorship-flagged" in (fields.get("labels") or [])
            retests.append(
                OpenRetest(
                    ticket_id=_extract_ticket_id(text, fallback=p.stem),
                    title=_extract_ticket_title(text),
                    criteria=_extract_retest_criteria(text),
                    authorship_flagged=flagged,
                )
            )
    return retests


def render_open_retests_section(retests: list[OpenRetest], verdicts_path: Path) -> str:
    """Render the "## Open re-tests" brief section, or "" if there are none
    (mirrors the optional-section-omission convention `snapshot_section` /
    the methodology section already use in `compose_audit_brief`).

    T-2799 (remediates T-2795): instructs the (isolated, read-only) auditor
    to append its verdict to the WORKSPACE ARTIFACT at `verdicts_path`
    instead of running `--record-retest-verdict` directly -- that command
    acquires a canonical git-mutation lock the isolation profile denies
    (T-2795's own failure). The trusted runner lands the artifact after
    the session via `--land-verdicts`.

    T-2796: authorship-flagged findings (ORG-038 Refinement 2 -- the
    T-2613 class) are EXCLUDED here, structurally -- Codex, the audience
    for this rendered section, must never be told to verify a finding
    that touches its own authored work. The filter lives in THIS function
    (not just at the `find_open_retest_tickets` call site) so the
    guarantee holds even if a future caller passes an unfiltered list;
    see `render_claude_tier_verification_queue` for where those findings
    route instead.
    """
    visible = [r for r in retests if not r.authorship_flagged]
    if not visible:
        return ""
    bullets = "\n".join(
        f"- **{r.ticket_id}** — {r.title}\n  Re-test criteria: {r.criteria or _RETEST_NOT_EXTRACTABLE}"
        for r in visible
    )
    return f"""
## Open re-tests
Findings already reported to the domain owner stay open until independently
re-verified -- including tickets gated with a future `not_before`, since THIS
scheduled audit dispatch is the re-test the gate is deferring to, not a
reason to skip it. For each finding below, verify its Re-test criteria in
this session, then record the verdict in a WORKSPACE ARTIFACT -- not just in
prose, and NOT by running `--record-retest-verdict` yourself (your isolation
profile denies the canonical git-mutation lock that command needs) -- by
appending one JSON line per re-tested finding to:

    {verdicts_path}

Each line: `{{"ticket": "<ID>", "verdict": "pass"|"fail"|"partial", "evidence_note": "<summary/evidence>"}}`.
The trusted runner reads this artifact after your session ends and lands
each verdict on its named finding. A `pass` verdict removes the finding
from this section on the next cycle; `fail` / `partial` keep it open, but
the verdict is still durably recorded on the ticket either way -- a
verdict that only lives in this session's chat output is exactly the gap
this artifact exists to close.

{bullets}
"""


def render_claude_tier_verification_queue(retests: list[OpenRetest]) -> str:
    """Render the authorship-flagged subset of open re-tests (T-2796) as
    its own section, or "" if none are flagged-and-open.

    NEVER embedded in the Codex-facing brief -- `render_open_retests_section`
    excludes exactly this subset. Callers needing this queue (Orchestrator/Recovery
    reviewing what still needs claude-tier closure verification) call this
    directly. Mirrors `render_open_retests_section`'s shape and
    omission convention.
    """
    flagged = [r for r in retests if r.authorship_flagged]
    if not flagged:
        return ""
    bullets = "\n".join(
        f"- **{r.ticket_id}** — {r.title}\n  Re-test criteria: {r.criteria or _RETEST_NOT_EXTRACTABLE}"
        for r in flagged
    )
    return f"""
## Claude-tier verification queue (ORG-038 Refinement 2)
These findings touch a codex-authored control or remediation -- Codex must
not verify them (independence contract). Route closure verification to
orchestrator or recovery: verify each Re-test criteria directly, then record the
verdict with:

    python3 tools/s3star_audit_runner.py --record-retest-verdict \\
        --ticket <ID> --verdict pass|fail|partial --note "<summary/evidence>"

{bullets}
"""


def _seat_evidence_section(latest: Path) -> str:
    """T-4928: point the auditor at Recovery's per-seat bundle, or say plainly that it is
    absent. Never silent: without it the auditor has no cross-account channel."""
    manifest = latest / "MANIFEST.md"
    try:
        head = manifest.read_text(encoding="utf-8")[:400]
    except OSError:
        return (
            "\n### Per-seat evidence (Recovery-captured): ABSENT this cycle\n"
            f"`{manifest}` is missing or unreadable. You have no cross-account channel "
            "this cycle (the codex account has no sudo): other seats' user units, timers "
            "and transcript inventories are absence-of-channel, not absence-of-thing. "
            "Say so in any finding that would need them.\n"
        )
    gen = next((ln.split(":", 1)[1].strip() for ln in head.splitlines()
                if ln.startswith("generated_at:")), "unknown")
    return (
        "\n### Per-seat evidence (Recovery-captured)\n"
        f"Manifest: `{manifest}` (generated_at {gen}). Each fleet account's "
        "`systemctl --user` units, timers and failed units, plus an INVENTORY (path, "
        "bytes, mtime; never content) of its session transcripts, captured by Recovery as "
        "that account. Cite it as Recovery-captured evidence; Recovery is an audited seat too. "
        "For a transcript excerpt, put the path and line range in a finding's "
        "reproduction and Recovery returns a reviewed excerpt.\n"
    )


def compose_audit_brief(
    slot: AuditSlot,
    today: date,
    findings_path: Path,
    *,
    proposal_text: str | None = None,
    snapshot_path: Path | None = None,
    tickets_root: Path | None = None,
) -> str:
    """Build the full dispatch-brief text for `slot` on `today`.

    `findings_path` is embedded so the brief tells Codex exactly where to
    write its findings (same discipline as watch_runner.build_brief's
    output_path — passing a relative path here would reintroduce the T-0660
    "wrote to the wrong cwd" variance).

    `snapshot_path` (T-1599, optional): when given, a "Runtime evidence
    snapshot" section is inserted referencing it, so the auditor knows to
    read the runner-captured host evidence. Omitted entirely when `None`
    (the default) -- zero behavior change for every caller that predates
    T-1599, including --emit-brief preview when it chooses not to pass one.

    `tickets_root` (T-1898, optional): when given, an "Open re-tests" section
    is inserted listing every open s3star finding ticket under
    `tickets_root/inbox/` plus its Re-test criteria. Omitted entirely when
    `None` (the default) -- zero behavior change for every existing caller,
    same optionality convention as `snapshot_path` above. The section's
    workspace verdicts-artifact path (T-2799) is derived internally as
    `run_workspace_dir(slot.slug, today) / "verdicts.jsonl"` -- a pure
    function of the two required params above, so no new parameter is
    needed here.
    """
    text = proposal_text if proposal_text is not None else load_proposal_text()

    independence = _extract_markdown_section(text, "Independence contract")
    authority = _extract_markdown_section(text, "Audit authority")
    finding_format = _extract_markdown_section(text, "Finding format")
    severity = _extract_markdown_section(text, "Severity calibration")
    audit_program = _extract_markdown_section(text, "Audit program")
    narrow_full = _extract_markdown_section(
        audit_program, "1. Recurring security-assurance audit", level="###"
    )
    sampling = _extract_markdown_section(
        audit_program, "2. Policy-versus-reality sampling", level="###"
    )

    snapshot_section = ""
    if snapshot_path is not None:
        manifest_path = manifest_path_for(snapshot_path)
        seat_section = _seat_evidence_section(SEAT_EVIDENCE_LATEST)
        snapshot_section = f"""
## Runtime evidence snapshot (runner-narrated)
{_SNAPSHOT_CAVEAT}

Path: `{snapshot_path}`

The runner captured this host-side snapshot at brief-composition time to give
you ground truth for host runtime state your own sandbox cannot directly
observe (PID namespace, uid/gid overflow-mapping to 65534 nobody:nogroup, an
unreachable user systemd bus). Read it, and cite it as runner-narrated
evidence -- not as your own direct observation -- per the independence
contract above.

Per-artifact evidence bundle + hashes: `{manifest_path}` (one immutable file
per capture, each sha256'd, plus a Limits section naming what this channel
structurally cannot see -- absence there means absence-of-channel, not
absence-of-thing).
{seat_section}
## Methodology — in-sandbox observations are namespace-relative, not host truth
Your own sandbox is a bwrap user namespace: uids/gids it cannot map render as
the kernel overflow ids 65534 (`nobody`/`nogroup`). In-sandbox ownership and
identity observations (`ls -l`, `id`, `stat`) reflect that namespace's view,
not the host's, and MUST NOT be reported as host truth.

Worked example (S3STAR-W1-003 / T-1595): an earlier audit cycle reported the
mailbox hub as owned `nobody:nogroup` and `codex` as only a `nogroup`
member -- both were bwrap overflow-id rendering artifacts, not real drift.
Host-truth evidence showed a real owner and a real group membership; the false
reading had already shipped as a High-severity finding.

Read the CURRENT owner and group off `evidence/ownership-named.txt` and
`evidence/identity-group.txt` in this cycle's bundle -- this paragraph
deliberately no longer names them. It used to say `the-ancestor:veneration`,
that group was renamed to `pantheon-mailbox`, and the correction fixture went
on asserting the retired name as host truth (T-2405). A worked example that
hardcodes the answer becomes a second source of the drift it exists to catch.

Any claim about host ownership, identity, timers, or processes must cite the
evidence snapshot / bundle above, not your own in-sandbox observation. If the
bundle does not cover something, say so explicitly -- absence-of-channel is
not absence-of-thing.
"""

    open_retests_section = ""
    if tickets_root is not None:
        verdicts_path = run_workspace_dir(slot.slug, today) / "verdicts.jsonl"
        open_retests_section = render_open_retests_section(
            find_open_retest_tickets(tickets_root), verdicts_path
        )

    return f"""You are Codex, dispatched as Pantheon's System-3* Assurance Auditor (ORG-038).

## Audit cycle
Slot: {slot.slug} ({slot.cadence})
Date: {today.isoformat()}

## Scoping policy for this cycle
{slot.scoping_policy}

## Independence contract — BINDING, read this first
{independence}

This is a READ-ONLY audit. Do not modify anything outside your own findings
output file. You do not close findings — closure requires the domain owner's
remediation plus your own independent re-verification pass.
{_ISOLATION_PROFILE_SECTION}
{snapshot_section}
## Audit authority
{authority}

## Coverage — recurring security-assurance audit
{narrow_full}

## Coverage — policy-versus-reality sampling
{sampling}
{open_retests_section}
## Finding format
Use exactly this shape for EVERY finding, as one `## Finding <n>: <short title>`
markdown section per finding, each containing ALL of the following as
`- **Field:** value` bullets (field names exactly as given, case-insensitive):

{finding_format}

Required bullet fields per finding: ID, Severity, Control objective, Observed
condition, Evidence, Impact, Reproduction, Owner, Recommended outcome,
Re-test criteria, Justification.

## Severity calibration
{severity}

## Output

Write your findings to: `{findings_path}`

Begin the findings file with a frontmatter block:
```
---
slot: {slot.slug}
date: {today.isoformat()}
auditor: codex
---
```
followed by one `## Finding <n>: <title>` section per finding (omit the
section entirely if you found nothing actionable this cycle — do not invent
findings to fill a quota, per the severity-inflation / checklist-theater
guardrails).

Use the word "finding" in a markdown heading
ONLY for the `## Finding <n>: <title>` sections. Any other heading containing
it (e.g. `## No findings this cycle`) is read as a malformed finding and fails
the run; for a clean cycle write a heading such as `## Result` followed by
plain prose.

End the file with a closing sentinel as the LAST line of the file. It is a plain
line of text, NOT inside a code fence: write it bare, with no code-fence line
before or after it (a closing fence after it counts as content after the
sentinel and fails the run). Exactly this shape, where N is the number of
`## Finding <n>: <title>` sections you wrote (0 for a clean cycle):

{_CLOSING_SENTINEL_TEMPLATE}

Write it exactly once, on its own line after a blank line, only after every
finding is complete, and do not quote it anywhere else in the file.
Nothing may follow it. The runner refuses a findings file that lacks the
sentinel, whose N does not match the number of `## Finding` sections, or that
has content after it, and files nothing from it -- so a run that is cut off
before this line can never be mistaken for a clean cycle.

Begin.
"""


# ---------------------------------------------------------------------------
# Dispatch-ticket filing (supervised hand-off) — mirrors watch_runner's
# file_watch_ticket idempotency pattern (marker scan in tickets/inbox/).
# ---------------------------------------------------------------------------


def _dispatch_marker(slug: str, today: date) -> str:
    return f"s3star-audit-dispatch-for: {slug}:{today.isoformat()}"


def dispatch_ticket_already_filed(slug: str, today: date, *, tickets_root: Path | None = None) -> bool:
    root = tickets_root if tickets_root is not None else REPO_ROOT / "tickets"
    marker = _dispatch_marker(slug, today)
    inbox = root / "inbox"
    if not inbox.exists():
        return False
    for p in inbox.glob("*.md"):
        try:
            if marker in p.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False


def build_dispatch_ticket_bodies(
    slot: AuditSlot, today: date, brief: str, findings_path: Path
) -> tuple[str, str]:
    session = codex_audit_session_name(slot.slug, today)
    verdicts_path = run_workspace_dir(slot.slug, today) / "verdicts.jsonl"
    ask = (
        f"S3* audit dispatch due: {slot.slug} ({slot.cadence}) for {today.isoformat()}. "
        f"SUPERVISED DISPATCH (T-1604 mechanism): run `python3 tools/s3star_audit_runner.py "
        f"--launch-audit --slot {slot.slug} --date {today.isoformat()}` -- this spawns a "
        f"dedicated `{session}` tmux session (never the supervisor-managed "
        f"`{STANDING_WORKER_SESSION}`), writes a fresh brief into the run workspace, and "
        f"launches the profiled auditor (`codex --profile {AUDIT_PROFILE}`) reading it. "
        f"Observe the run. Land findings and verdicts only after `python3 "
        f"tools/s3star_audit_runner.py --await-audit --slot {slot.slug} "
        f"--date {today.isoformat()}` returns 0, not when `{findings_path}` first appears: "
        f"Codex can rewrite its output files after first writing them (T-4307: on "
        f"2026-09-28 verdicts.jsonl was rewritten ~40s after it first appeared, so retest "
        f"verdicts landed twice). It is read-only and polls until the Codex process has "
        f"exited, or findings.md carries its closing sentinel and findings.md, "
        f"verdicts.jsonl and the run dir have been unchanged for 10 minutes (interactive "
        f"Codex idles at its prompt after finishing and may never exit). rc 1 is a "
        f"timeout and rc 2 is bad args or a missing run dir: in both cases do NOT land. "
        f"Do not gate on the tmux session either: it stays alive until --teardown-audit. "
        f"Once it returns 0, file the per-finding tickets by running: "
        f"`python3 tools/s3star_audit_runner.py --land-findings --slot {slot.slug} "
        f"--findings-file {findings_path}` (a complete audit -- one ending in its closing "
        f"sentinel -- with zero findings lands clean, rc 0, filed 0). "
        f"rc 1 means the cycle is NOT clean; read the printed problem lines. "
        f"A missing sentinel, or an N that does not match the number of "
        f"`## Finding` sections, means the run was truncated or crashed. "
        f"A duplicated sentinel, or content after it, means the file was "
        f"rewritten or appended to after sealing. "
        f"Nothing was filed in any of these cases. Re-run the audit. "
        f"A shape problem in a sealed file files the parseable findings and returns "
        f"rc 1 on every re-land by design. Hand-file the rest instead of looping. "
        f"Either way, do not close the dispatch ticket as a clean cycle. "
        f"Then, if the brief carries an Open re-tests section, land the "
        f"retest verdicts by running: `python3 tools/s3star_audit_runner.py "
        f"--land-verdicts --slot {slot.slug} --verdicts-file {verdicts_path}`. Finally tear "
        f"the session down with `python3 tools/s3star_audit_runner.py --teardown-audit "
        f"--slot {slot.slug} --date {today.isoformat()}`."
    )
    context = (
        f"{_dispatch_marker(slot.slug, today)}\n\n"
        f"--- BRIEF (filed-time snapshot; --launch-audit recomposes a fresh brief + "
        f"runtime snapshot at actual launch time, since HO may run it well after this "
        f"ticket is filed) ---\n\n"
        f"{brief}"
    )
    return ask, context


def _raise_auto_dispatch_not_implemented() -> None:
    raise NotImplementedError(
        "s3star_audit_runner --auto-dispatch: automated Codex dispatch is not "
        "implemented (T-1583/ORG-038). CodexHarnessAdapter.inject_inbound is a "
        "skeleton-tier bridge surface (NotImplementedError, T-0714/ORG-031) and "
        "headless `codex exec` billing is unresolved (ORG-031 open question) and "
        "explicitly out of scope for this pilot (ORG-038 §Cost/cadence). Use "
        "--file-dispatch-ticket for the supervised hand-off path instead."
    )


def file_dispatch_ticket(
    slot: AuditSlot,
    today: date,
    findings_path: Path,
    *,
    ticket_bin: Path = TICKET_BIN,
    env: dict[str, str] | None = None,
    snapshot_path: Path | None = None,
) -> tuple[str, str | None, str]:
    ensure_run_workspace_dir(slot.slug, today)
    if snapshot_path is None:
        snapshot_path = run_workspace_dir(slot.slug, today) / "runtime-snapshot.md"
    capture_runtime_snapshot(snapshot_path)
    brief = compose_audit_brief(
        slot, today, findings_path, snapshot_path=snapshot_path,
        tickets_root=REPO_ROOT / "tickets",
    )
    ask, context = build_dispatch_ticket_bodies(slot, today, brief, findings_path)
    if env is None:
        env = os.environ.copy() | {"PANTHEON_ACTOR": "orchestrator"}
    argv = [
        sys.executable,
        str(ticket_bin),
        "file",
        "--automated-filer", "s3star_audit_runner",  # T-5678: ORG-105 gate, non-interactive
        "--requester", "orchestrator",
        "--target", "role:engineering_orchestrator",
        "--title", f"S3* audit dispatch — {slot.slug} {today.isoformat()}",
        "--labels", "s3star-audit,audit-dispatch",
        "--project-id", "null",
        "--nearest-intent-ref", "maintenance:s3star-audit",
        "--serves", _SERVES,
        "--ask", ask,
        "--context", context,
    ]
    return _invoke_ticket_file(
        argv, f"dispatch {slot.slug}:{today.isoformat()}", env, log_error=_log_s3star_error
    )


# ---------------------------------------------------------------------------
# Findings landing — structural completion check (T-1581 precedent pattern,
# new function since the finding-artifact schema differs from watch reports)
# + per-finding parse + per-finding ticket filing.
# ---------------------------------------------------------------------------

FINDINGS_MIN_BYTES = 200


def _findings_file_is_structured(text: str) -> bool:
    """True iff `text` has a leading frontmatter block with non-empty `slot:`
    and `date:` keys. Mirrors the *pattern* tools/watch_runner.py's
    `_report_is_structured` uses — a new function because the finding-artifact
    frontmatter schema (slot/date/auditor) differs from the watch-report schema
    (surface/headline).

    T-4307: a `## Finding` heading is NOT required. `compose_audit_brief` tells
    the auditor to omit the finding sections entirely when nothing is
    actionable, so a valid zero-finding cycle is a legitimate, complete
    artifact ("the auditor found nothing"). Whether headings that ARE present
    actually parse is `land_findings`' concern, not this structural check's.
    """
    m = re.match(r"^---\n(.*?)\n---", text, flags=re.DOTALL)
    if not m:
        return False
    fm_lines = m.group(1).splitlines()
    has_slot = any(re.match(r"\s*slot\s*:\s*\S", line) for line in fm_lines)
    has_date = any(re.match(r"\s*date\s*:\s*\S", line) for line in fm_lines)
    return has_slot and has_date


def findings_landed_ok(path: Path) -> bool:
    """Structural completion check: exists, at least FINDINGS_MIN_BYTES, and
    has the required slot/date frontmatter. Zero `## Finding` sections is
    valid (T-4307: a clean audit cycle).
    """
    if not path.exists():
        return False
    if path.stat().st_size < FINDINGS_MIN_BYTES:
        return False
    return _findings_file_is_structured(path.read_text(encoding="utf-8"))


# T-4314: the closing sentinel. Without one, a Codex run that crashed or was cut
# off after the frontmatter and preamble is byte-for-byte indistinguishable from
# a genuinely clean zero-finding audit (T-4307 made that shape rc 0). The auditor
# is told (compose_audit_brief) to write `s3star-audit-end: findings=N` as the
# file's LAST line, N = the number of `## Finding` sections it wrote (0 = clean).
#
# Deliberately a plain line, NOT the `## End of findings (N findings)` heading
# the ticket first floated: any heading containing "finding" trips
# `_FINDING_NEAR_MISS_LINE_RE` (and contradicts the brief's "no such heading in
# a clean file" rule), so a heading sentinel would need an exemption in the very
# guard that protects against silent loss. `key=value` also makes a whole
# workspace auditable with one grep.
_CLOSING_SENTINEL_TOKEN = "s3star-audit-end:"
_CLOSING_SENTINEL_TEMPLATE = f"{_CLOSING_SENTINEL_TOKEN} findings=N"
# Matched against a whole stripped line. Horizontal whitespace around `:`/`=` is
# tolerated (formatting slop is not a crash); token, key and case are exact.
# T-4368: N is 1-6 ASCII digits. `[0-9]`, not `\d`: `\d` also matches Unicode
# digits, which `int()` then parses, so a fullwidth one would seal a
# one-finding file. The cap keeps `int()` below CPython's 4300-digit
# string-conversion limit: an oversized N is not a valid sentinel, so it lands
# in the ordinary "malformed sentinel" problem (rc 1, logged) instead of a
# ValueError traceback. 6 digits is far beyond the 30-finding cap.
_CLOSING_SENTINEL_RE = re.compile(
    re.escape(_CLOSING_SENTINEL_TOKEN) + r"[ \t]*findings[ \t]*=[ \t]*([0-9]{1,6})"
)
# T-4368: how much of a sentinel-like line the "malformed" problem quotes back.
_SENTINEL_QUOTE_MAX = 80


def _split_closing_sentinel(text: str) -> tuple[str, list[str]]:
    """Check `text`'s closing sentinel. Returns `(body, problems)`. Pure.

    `body` is `text` with every valid sentinel line removed -- what
    `parse_findings` and `_findings_shape_problems` must see, because a
    sentinel on the line right after the last `- **Field:**` bullet would
    otherwise be folded into that field's value and filed inside a ticket.

    `problems` is empty iff the file carries exactly ONE valid sentinel, as its
    last non-blank line, whose N equals the number of strict `## Finding`
    headings (`_FINDING_HEADING_RE` -- the parser's own split constant, so the
    sentinel and the parser cannot disagree about what a finding section is;
    malformed and over-cap blocks have their own guard line in
    `_findings_shape_problems` and are deliberately not double-reported here).
    Every non-empty result is a reason `land_findings` must file NOTHING:

      * none      -- the run was truncated or crashed before finishing;
      * >1        -- ambiguous which one closes the file (a concatenated or
                     re-appended rewrite, or a quoted example);
      * content after it -- uncounted content (the file was rewritten or
                     appended to after it "finished"); ignoring it would
                     silently drop findings;
      * N != headings -- cut off mid-way, or hand-edited.
    """
    lines = text.split("\n")
    valid = [i for i, ln in enumerate(lines) if _CLOSING_SENTINEL_RE.fullmatch(ln.strip())]
    drop = set(valid)
    body = "\n".join(ln for i, ln in enumerate(lines) if i not in drop)
    problems: list[str] = []

    if not valid:
        looks_like = [ln.strip() for ln in lines if _CLOSING_SENTINEL_TOKEN in ln]
        msg = (
            f"no closing sentinel '{_CLOSING_SENTINEL_TEMPLATE}' as the file's last line: "
            f"the audit run was most likely truncated or crashed before finishing, so "
            f"this file is not a complete audit (and is NOT a clean cycle). Files "
            f"written before T-4314 have no sentinel and are not accepted."
        )
        if looks_like:
            # Bounded per line: an oversized N would otherwise be echoed whole
            # into stderr and the runner log.
            shown = ", ".join(
                repr(ln if len(ln) <= _SENTINEL_QUOTE_MAX else ln[:_SENTINEL_QUOTE_MAX] + "...")
                for ln in looks_like[:3]
            )
            msg += f" Sentinel-like line(s) present but malformed: {shown}."
        problems.append(msg)
        return body, problems

    if len(valid) > 1:
        problems.append(
            f"{len(valid)} closing sentinels found; exactly one is required, as the "
            f"file's last line (a re-appended rewrite or a quoted example?)"
        )
        return body, problems

    idx = valid[0]
    trailing = [ln.strip() for ln in lines[idx + 1:] if ln.strip()]
    if trailing:
        problems.append(
            f"{len(trailing)} non-blank line(s) follow the closing sentinel (first: "
            f"{trailing[0][:80]!r}); the sentinel must be the file's last line -- "
            f"content after it is uncounted (file rewritten or appended to after the "
            f"run finished?)"
        )
    claimed = int(_CLOSING_SENTINEL_RE.fullmatch(lines[idx].strip()).group(1))
    actual = len(_FINDING_HEADING_RE.findall(body))
    if claimed != actual:
        problems.append(
            f"closing sentinel claims {claimed} finding(s) but the file has {actual} "
            f"'## Finding' heading(s) (run cut off mid-way, or file hand-edited)"
        )
    return body, problems


@dataclass
class Finding:
    finding_id: str
    title: str
    severity: str
    control_objective: str
    observed_condition: str
    evidence: str
    impact: str
    reproduction: str
    owner: str
    recommended_outcome: str
    retest_criteria: str
    justification: str


_FIELD_RE = re.compile(r"^-\s+\*\*([^:*]+):\*\*\s*(.*)$")

# The ONE strict finding-heading shape (`## Finding <n>: <title>`), shared by
# `parse_findings` (which splits on it) and `land_findings`' shape guards (which
# count it), so the parser and the guards cannot drift apart. One capture group:
# the heading text after `## `, so `.split()` yields
# [preamble, header1, block1, header2, block2, ...].
_FINDING_HEADING_RE = re.compile(r"^## (Finding\b[^\n]*)$", flags=re.MULTILINE)

# Maps the lowercased bullet label (as required by compose_audit_brief's
# instructions to Codex) to the Finding dataclass attribute name.
_REQUIRED_FIELDS: dict[str, str] = {
    "id": "finding_id",
    "severity": "severity",
    "control objective": "control_objective",
    "observed condition": "observed_condition",
    "evidence": "evidence",
    "impact": "impact",
    "reproduction": "reproduction",
    "owner": "owner",
    "recommended outcome": "recommended_outcome",
    "re-test criteria": "retest_criteria",
    "justification": "justification",
}

# Guards against a malformed findings file spamming the ticket queue.
_FINDING_SOFT_CAP = 30


def parse_findings(text: str) -> list[Finding]:
    """Extract Finding records from `## Finding <n>: <title>` sections.

    Malformed blocks (missing a required field) are skipped with a one-line
    stderr note — never crash (this runs unattended). Mirrors
    tools/watch_runner.py's parse_followups tolerance for malformed input.
    """
    parts = _FINDING_HEADING_RE.split(text)
    headers, blocks = parts[1::2], parts[2::2]

    findings: list[Finding] = []
    for header, block in zip(headers, blocks):
        title = header.split(":", 1)[1].strip() if ":" in header else header.strip()
        fields: dict[str, str] = {}
        current_key: str | None = None
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                current_key = None
                continue
            m = _FIELD_RE.match(stripped)
            if m:
                key = m.group(1).strip().lower()
                val = m.group(2).strip()
                if key in _REQUIRED_FIELDS:
                    fields[key] = val
                    current_key = key
                else:
                    current_key = None
                continue
            if current_key:
                fields[current_key] = f"{fields[current_key]} {stripped}".strip()

        missing = [k for k in _REQUIRED_FIELDS if not fields.get(k, "").strip()]
        if missing:
            print(
                f"[s3star] skip malformed finding block {header!r}: missing {missing}",
                file=sys.stderr,
            )
            continue

        kwargs = {attr: fields[label] for label, attr in _REQUIRED_FIELDS.items()}
        findings.append(Finding(title=title, **kwargs))

    if len(findings) > _FINDING_SOFT_CAP:
        print(
            f"[s3star] finding cap ({_FINDING_SOFT_CAP}) exceeded ({len(findings)} "
            f"parsed); filing first {_FINDING_SOFT_CAP} only",
            file=sys.stderr,
        )
        findings = findings[:_FINDING_SOFT_CAP]

    return findings


# ---------------------------------------------------------------------------
# Owner resolution — derived from ORG-027 §1's domain table (Orchestrator=org+
# delivery+infra head domain; Specialist=research+watch; Recovery=recovery+ticket-ops).
# Unmatched / "codex" always falls back to orchestrator: Codex must never be a
# finding's ticket target (structural no-self-closure, not just convention).
# ---------------------------------------------------------------------------

_OWNER_ALIASES: dict[str, str] = {
    # Orchestrator's domain (ORG-027 §1: org + delivery + infra — the head domain).
    "orchestrator": "orchestrator",
    "orchestrator": "orchestrator",             # orchestrator's OLD (pre-ORG-100) audit-id, kept for continuity
    "head_engineering_orchestrator": "orchestrator", # ORG-100 (T-3814) forward audit-id for orchestrator
    "ho": "orchestrator",
    "org": "orchestrator",
    "delivery": "orchestrator",
    "infra": "orchestrator",
    "security": "orchestrator",
    "credentials": "orchestrator",
    "secrets": "orchestrator",
    "permissions": "orchestrator",
    "worker-guard": "orchestrator",
    "ledger": "orchestrator",
    "audit-chain": "orchestrator",
    # Specialist's domain (ORG-027 §1: research + watch).
    "specialist": "specialist",
    "head_research_orchestrator": "specialist",   # T-3807 forward audit-id
    "specialist": "specialist",                  # legacy audit-id, retained (D1, P4 audit-continuity)
    "research": "specialist",
    "watch": "specialist",
    # Recovery's domain (ORG-027 §1: recovery + ticket-ops).
    "recovery": "recovery",
    "head_system_orchestrator": "recovery",       # ORG-100 (T-3814) forward audit-id for recovery
    "recovery": "recovery",
    "ticket-ops": "recovery",
    "tickets": "recovery",
    "restart": "recovery",
    # Constitutional.
    "operator": "operator",
    # Codex never owns remediation of its own audit findings.
    "codex": "orchestrator",
    # ORG-100 (T-3814): the new role-class audit-ids for codex + archivist resolve
    # to their OWNER the SAME WAY their persona names already do in THIS table — NOT
    # to the persona itself. head_auditor (codex's forward id) -> orchestrator, mirroring the
    # codex -> orchestrator no-self-closure invariant above (codex must never be routed
    # remediation of its own findings). head_memory_orchestrator (archivist's forward
    # id) -> orchestrator, mirroring resolve_owner("archivist") == _DEFAULT_OWNER (orchestrator):
    # archivist is not a remediation owner in the ORG-027 §1 domain table (mailbox-
    # first, not a ticket-ops seat), so remediation stays in Orchestrator's head domain.
    # (This is the deliberate exception to D-d's generic persona-arrow mapping, which
    # holds for ticket_lint's resolve_persona normalization but would break this
    # table's owner invariant.)
    "head_auditor": "orchestrator",
    "head_memory_orchestrator": "orchestrator",
}
_DEFAULT_OWNER = "orchestrator"


def resolve_owner(owner_text: str) -> str:
    return _OWNER_ALIASES.get(owner_text.strip().lower(), _DEFAULT_OWNER)


def owner_target(owner: str) -> str:
    """Ticket `--target` for a resolved domain owner (T-5239 / ORG-088: role-only).

    The owner TABLE stays persona-keyed (it is domain knowledge, not routing); only the
    value handed to `ticket.py file --target` is translated to the owner's pool role.
    `operator` passes through."""
    if owner == "operator":
        return owner
    from tools.agent_role_map import persona_pool_role
    role = persona_pool_role(owner)
    return f"role:{role}" if role else f"role:{persona_pool_role(_DEFAULT_OWNER)}"


# ---------------------------------------------------------------------------
# Authorship-aware routing (ORG-038 Refinement 2): if a finding's evidence or
# reproduction text references a path with a codex-authored commit in its git
# history, flag the ticket for claude-tier verification rather than letting
# closure quietly route back through Codex's own review.
# ---------------------------------------------------------------------------

_BACKTICK_PATH_RE = re.compile(r"`([^`\s]+/[^`\s]+\.\w+)`")


def _extract_paths(text: str) -> list[str]:
    return _BACKTICK_PATH_RE.findall(text)


def _is_codex_authored(path: str, *, cwd: Path = REPO_ROOT) -> bool:
    """True iff any commit touching `path` (per `git log`) has an author name
    or email matching a codex identity. Best-effort: any subprocess failure
    (path doesn't exist / not in a repo) is treated as "no match", never raises.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--format=%an <%ae>", "--", path],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    return bool(re.search(r"codex", result.stdout, re.IGNORECASE))


def authorship_flag(finding: Finding, *, cwd: Path = REPO_ROOT) -> bool:
    paths = _extract_paths(finding.evidence) + _extract_paths(finding.reproduction)
    return any(_is_codex_authored(p, cwd=cwd) for p in paths)


_AUTHORSHIP_NOTE_UNFLAGGED = (
    "Authorship-aware routing (ORG-038 Refinement 2): this finding's evidence/"
    "reproduction paths were checked against git history; no codex-authored "
    "commit was found on them. Standard verification applies."
)
_AUTHORSHIP_NOTE_FLAGGED = (
    "AUTHORSHIP FLAG (ORG-038 Refinement 2): this finding touches a path with a "
    "codex-authored commit in its history. Per the independence contract, Codex "
    "must not verify controls/remediations it authored as implementer — route "
    "closure verification to a claude-tier verifier (orchestrator/recovery), not back to codex."
)


def _finding_marker(slot_slug: str, today: date, finding: Finding) -> str:
    # Shares _FINDING_MARKER_PREFIX with find_open_retest_tickets' reader
    # side (T-1898) so the writer and reader can never drift apart.
    return f"{_FINDING_MARKER_PREFIX} {slot_slug}:{today.isoformat()}:{finding.finding_id}"


def finding_already_filed(marker: str, *, tickets_root: Path | None = None) -> bool:
    root = tickets_root if tickets_root is not None else REPO_ROOT / "tickets"
    for sub in ("inbox", "active", "done"):
        d = root / sub
        if not d.exists():
            continue
        for p in d.rglob("*.md"):
            try:
                if marker in p.read_text(encoding="utf-8"):
                    return True
            except (OSError, UnicodeDecodeError):
                continue
    return False


def build_finding_ticket_bodies(
    finding: Finding, slot_slug: str, today: date, *, flagged: bool
) -> tuple[str, str]:
    note = _AUTHORSHIP_NOTE_FLAGGED if flagged else _AUTHORSHIP_NOTE_UNFLAGGED
    ask = (
        f"S3* audit finding {finding.finding_id} ({finding.severity}) from the "
        f"{slot_slug} audit on {today.isoformat()}: {finding.control_objective}. "
        f"Observed: {finding.observed_condition}. Recommended outcome: "
        f"{finding.recommended_outcome}. Re-test criteria: {finding.retest_criteria}. "
        f"Remediate through the normal implementation/review path; Codex re-tests "
        f"independently to close (never self-closes)."
    )
    context = (
        f"{_finding_marker(slot_slug, today, finding)}\n\n"
        f"Title: {finding.title}\n"
        f"Severity: {finding.severity}\n\n"
        f"Control objective: {finding.control_objective}\n\n"
        f"Observed condition: {finding.observed_condition}\n\n"
        f"Evidence: {finding.evidence}\n\n"
        f"Impact: {finding.impact}\n\n"
        f"Reproduction: {finding.reproduction}\n\n"
        f"Recommended outcome: {finding.recommended_outcome}\n\n"
        f"Re-test criteria: {finding.retest_criteria}\n\n"
        f"Justification: {finding.justification}\n\n"
        f"{note}"
    )
    return ask, context


def file_finding(
    finding: Finding,
    slot_slug: str,
    today: date,
    *,
    ticket_bin: Path = TICKET_BIN,
    env: dict[str, str] | None = None,
    cwd: Path = REPO_ROOT,
) -> tuple[str, str | None, str]:
    owner = resolve_owner(finding.owner)
    flagged = authorship_flag(finding, cwd=cwd)
    ask, context = build_finding_ticket_bodies(finding, slot_slug, today, flagged=flagged)
    severity_label = f"severity-{finding.severity.strip().lower()}"
    labels = ["s3star-audit", "audit-finding", severity_label]
    if flagged:
        labels.append("authorship-flagged")
    if env is None:
        env = os.environ.copy() | {"PANTHEON_ACTOR": "orchestrator"}
    argv = [
        sys.executable,
        str(ticket_bin),
        "file",
        "--automated-filer", "s3star_audit_runner",  # T-5678: ORG-105 gate, non-interactive
        "--requester", "orchestrator",
        "--target", owner_target(owner),
        "--title", f"S3* audit finding {finding.finding_id}: {finding.title}"[:120],
        "--labels", ",".join(labels),
        "--project-id", "null",
        "--nearest-intent-ref", "maintenance:s3star-audit",
        "--serves", _SERVES,
        "--ask", ask,
        "--context", context,
        # T-2613 / ORG-038: every newly filed finding starts awaiting
        # independent re-verification -- structural, so a remediate-and-move-
        # to-done/ can never silently drop it from re-test routing.
        "--retest-status", "pending",
    ]
    return _invoke_ticket_file(
        argv, f"finding {slot_slug}:{finding.finding_id}", env, log_error=_log_s3star_error
    )


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log_s3star_error(log_tag: str, detail: str) -> None:
    """`log_error` callback passed to `watch_runner._invoke_ticket_file` (T-1583
    REBOUND finding): without this, the shared kernel's default error path logs
    to watch_runner's OWN `RUNNER_LOG` tagged `"[specialist] actuation: ERROR"` —
    misattributing every s3star failure to Specialist's watch loop and leaving this
    module's own `RUNNER_LOG` untouched. This callback keeps failures in THIS
    module's log, correctly tagged.
    """
    _append_runner_log(f"[orchestrator] s3star: ERROR {log_tag} → {detail}")


def _append_runner_log(line: str, *, log_path: Path | None = None) -> None:
    path = log_path if log_path is not None else RUNNER_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{_utc_iso_now()} {line}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Retest-verdict recording (T-1898/T-2613) — the structural closure half of
# the "Open re-tests" mechanism above: flips a finding ticket's retest_status
# frontmatter field (and appends a transition-log line) once an independent
# re-test has actually run. Reuses tools.ticket's own mutation primitives as
# a library (same reuse tier as _invoke_ticket_file), NOT a new ticket.py
# verb — the field is S3*-assurance-specific, and a generic ticket.py verb
# would invite use on tickets the field means nothing for.
# ---------------------------------------------------------------------------

_RETEST_VERDICT_CHOICES = ("pass", "fail", "partial")

# T-2796 (ORG-038 Refinement 2): the only principals eligible to record a
# retest_status verdict on an `authorship-flagged` finding. An ALLOWLIST,
# not a denylist on "codex" specifically — a future unrecognized principal
# refuses by default rather than being silently trusted.
_ELIGIBLE_AUTHORSHIP_VERIFIERS = {"orchestrator", "recovery"}


def record_retest_verdict(
    ticket_id: str,
    *,
    verdict: str | None = None,
    override: bool = False,
    note: str,
    no_push: bool = False,
    principal: str | None = None,
) -> str:
    """Flip a finding ticket's `retest_status` frontmatter field and append a
    transition-log entry recording the independent re-test verdict. Returns
    the resulting `retest_status` value.

    Exactly one of `verdict` or `override` must be given:
      - verdict="pass"           -> retest_status: verified (leaves routing)
      - verdict="fail"/"partial" -> retest_status: pending, written explicitly
        (stays in routing; re-set rather than left alone so a ticket that
        somehow reaches this call with a missing/stale field self-heals to
        the correct structural state instead of silently staying wrong)
      - override=True            -> retest_status: overridden (leaves
        routing; an explicit non-verdict closure)

    `note` is REQUIRED on every call, no exceptions — including a bare PASS.
    A PASS verdict REMOVES a finding from re-test routing, and a
    removal-from-routing decision with no reason on the durable record is
    exactly the class of untraceable closure ORG-038 exists to prevent (P4).
    It is appended to the transition log, never silently dropped.

    `principal` (T-2796): the verifying identity checked against the
    authorship-eligibility gate below. Defaults to
    `_ticket_mod._claiming_persona()` — i.e. whoever is actually running
    this call (`$PANTHEON_ACTOR` or OS user) — correct for the direct
    `--record-retest-verdict` CLI path used interactively by a claude-tier
    verifier (orchestrator/recovery). `land_verdicts` always passes
    `principal="codex"` explicitly instead: that landing mode exists ONLY
    to land the isolated Codex auditor's own workspace artifact, so
    eligibility must reflect who *produced* the verdict, not which
    account happens to run the HO-run landing CLI.

    Refuses (TicketError, BEFORE any mutation) if the ticket carries the
    `authorship-flagged` label (ORG-038 Refinement 2) and `principal` is
    not an eligible claude-tier verifier — the ticket's retest_status and
    transition log are left completely untouched (still pending, still
    routed for re-test — never silently dropped from routing just because
    the wrong principal attempted it). Applies uniformly to pass, fail,
    partial, and override — the check runs before any of those diverge
    into a write.

    Refuses (TicketError) if `ticket_id` doesn't carry the s3star finding
    marker — this records a verdict on a FINDING, not an arbitrary ticket.
    Does not move ticket state or restrict which state directory the ticket
    is in (an override can legitimately close a finding before it reaches
    done/, e.g. a false positive) — only ticket.py claim/resolve move
    directories.

    T-3849 (remediates T-2795 Codex FAIL, defect 1): refuses (TicketError,
    BEFORE any mutation) a CROSS-INVOCATION REPLAY against a ticket that
    already carries a structural `retest_status` closure marker
    (`_CLOSED_RETEST_STATUSES`) -- this is the guard `land_verdicts`'
    per-entry open-set check and duplicate-in-artifact check don't cover,
    because both of those only look WITHIN a single invocation's own
    artifact; nothing previously stopped two SEPARATE invocations (e.g.
    the same workspace verdicts.jsonl landed twice) from each independently
    flipping the same ticket, as T-2795's own 24-second-apart double-PASS
    landing showed. Two cases:
      - already `overridden` -> refuses ANY new call (pass/fail/partial/
        override alike). An override is a terminal administrative closure
        (P4 traceability) -- nothing after it silently re-writes the
        record, including a second override.
      - already `verified` AND the new call is verdict="pass" -> refuses
        (a verified-to-verified re-write with no new evidence is exactly
        the untraceable-duplicate-closure shape ORG-038 exists to prevent).
        A `fail`/`partial` re-verdict on an already-`verified` ticket stays
        LEGAL and lands normally, downgrading retest_status back to
        `pending` -- that downgrade path is the entire point of
        independent re-test: a next-cycle FAIL must be able to re-open a
        prior PASS. `override=True` on an already-`verified` ticket also
        stays legal (out of scope for this guard -- only the pass-replay
        case is blocked; overriding a verified finding is a distinct,
        unblocked administrative action).
    This check runs before the authorship-flagged eligibility check below:
    a ticket's own state-legitimacy is checked first, independent of which
    principal is attempting the call.
    """
    has_verdict = verdict is not None
    if has_verdict == override:
        raise _ticket_mod.TicketError(
            "record_retest_verdict: pass exactly one of verdict= or override=True"
        )
    if has_verdict and verdict not in _RETEST_VERDICT_CHOICES:
        raise _ticket_mod.TicketError(
            f"record_retest_verdict: verdict {verdict!r} not in {_RETEST_VERDICT_CHOICES}"
        )
    if not (isinstance(note, str) and note.strip()):
        raise _ticket_mod.TicketError(
            "record_retest_verdict: --note is required on every call (P4 "
            "traceability — even a PASS that removes routing must cite its reason)"
        )
    note = note.strip()

    if override:
        new_status = "overridden"
        verdict_label = "override"
    elif verdict == "pass":
        new_status = "verified"
        verdict_label = "pass"
    else:
        new_status = "pending"
        verdict_label = verdict

    path = _ticket_mod._find_ticket(ticket_id)
    text = path.read_text(encoding="utf-8")
    if _FINDING_MARKER_PREFIX not in text:
        raise _ticket_mod.TicketError(
            f"record_retest_verdict: {ticket_id} is not an s3star finding ticket "
            f"(missing {_FINDING_MARKER_PREFIX!r} marker)"
        )
    fields, body = _ticket_mod.parse_front_matter(text)
    old_fields = _ticket_mod._fm_snapshot(fields)  # T-4760: explicit old for the registry check

    current_status = fields.get("retest_status")
    if current_status == "overridden":
        raise _ticket_mod.TicketError(
            f"record_retest_verdict: {ticket_id} already retest_status: "
            f"overridden -- refusing further verdict (P4 traceability: "
            f"terminal administrative closure, never silently re-written "
            f"by a later call); ticket left untouched"
        )
    if current_status == "verified" and has_verdict and verdict == "pass":
        raise _ticket_mod.TicketError(
            f"record_retest_verdict: {ticket_id} already retest_status: "
            f"verified -- refusing duplicate PASS replay (P4 traceability: "
            f"cross-invocation replay guard, T-3849); ticket left "
            f"untouched. A fail/partial re-verdict can still legitimately "
            f"reopen this finding for re-test (retest_status -> pending)."
        )

    if "authorship-flagged" in (fields.get("labels") or []):
        resolved_principal = (
            principal if principal is not None else _ticket_mod._claiming_persona()
        )
        if resolved_principal.strip().lower() not in _ELIGIBLE_AUTHORSHIP_VERIFIERS:
            raise _ticket_mod.TicketError(
                f"record_retest_verdict: {ticket_id} is authorship-flagged "
                f"(ORG-038 Refinement 2) -- {resolved_principal!r} is not an "
                f"eligible claude-tier verifier "
                f"({sorted(_ELIGIBLE_AUTHORSHIP_VERIFIERS)}); refused, no "
                f"mutation, finding stays routed for re-test"
            )

    fields["retest_status"] = new_status
    actor = _ticket_mod._actor()
    detail = f"{verdict_label}: {note}"
    body = _ticket_mod.append_transition(body, actor, "retest-verdict-recorded", detail)
    new_text = _ticket_mod._build_ticket_text(fields, body, old_fields=old_fields)

    with git_mutation_lock(label=f"s3star record-retest-verdict {ticket_id}"):
        # Mirrors ticket.py cmd_block's contract: frontmatter edit + transition
        # note, no ID allocation, no state move — pull-then-write is safe at
        # the default strict=True (T-1770 Part b).
        _ticket_mod._pull_if_needed(no_push)
        path.write_text(new_text, encoding="utf-8")
        subject = f"tickets: {ticket_id} retest-verdict — {verdict_label}"
        short = note if len(note) <= 60 else note[:57] + "..."
        commit_message = f"{subject}: {short}\n"
        if len(note) > 60:
            commit_message += f"\n{note}\n"
        _ticket_mod._commit_and_push([path], commit_message, no_push=no_push)

    return new_status


def push_claude_tier_queue(*, cwd: Path = REPO_ROOT) -> str | None:
    """T-2801: the structural delivery half of
    `render_claude_tier_verification_queue` -- reads the currently-open
    authorship-flagged retests straight off disk and mailbox.send()s the
    rendered queue to `ORCHESTRATOR_INBOX`, or does nothing if none are open.

    Called from `land_findings` (see below) so this fires every audit cycle
    by construction, instead of relying on HO remembering to call
    `render_claude_tier_verification_queue()` by hand (the anti-P1 gap
    T-2801 names).

    Recipient is orchestrator ONLY -- not also recovery. Every existing periodic
    mailbox alert in this repo (fleet_clone_currency_check -> orchestrator,
    unit_failure_alert/credential_expiry_monitor -> recovery) has exactly one
    owning recipient; fanning this one out to both eligible
    `_ELIGIBLE_AUTHORSHIP_VERIFIERS` principals would let orchestrator and recovery
    race to verify the same finding with no coordination. recovery stays
    reachable through the existing escalation ladder if orchestrator goes silent.

    No-spam: an empty render is a no-op (returns None, nothing sent).
    Duplicate suppression across repeated cycles is NOT reimplemented here
    -- `mailbox.send`'s own `dedupe_pending=True` default (never overridden
    below) already collapses a byte-identical resend while the prior copy
    sits unconsumed in orchestrator's inbox; once orchestrator reads it, an identical
    resend is a deliberate fresh nudge that the queue is still open. This
    only holds if the rendered payload is byte-stable across calls when the
    open-flagged set is unchanged -- true today because
    `find_open_retest_tickets` walks its three states in a fixed order and
    globs each with `sorted()`.

    A `mailbox.send` failure is logged and swallowed, never raised: this is
    a side-channel notification, not the source of truth (the ticket store
    is), so a mailbox hiccup must not undo the finding tickets `land_findings`
    just filed. The still-open queue is picked up again the next time
    `land_findings` runs -- self-healing, not a dropped notification.
    """
    retests = find_open_retest_tickets(cwd / "tickets")
    rendered = render_claude_tier_verification_queue(retests)
    if not rendered:
        return None
    flagged_count = sum(1 for r in retests if r.authorship_flagged)
    from tools import mailbox
    try:
        msg_id = mailbox.send(
            ORCHESTRATOR_INBOX, rendered, "s3star_audit_runner",
            subject=f"s3star claude-tier verification queue: {flagged_count} finding(s)",
        )
    except Exception as e:
        _append_runner_log(f"[s3star] claude-tier queue push failed: {e}")
        return None
    _append_runner_log(
        f"[s3star] claude-tier queue pushed: {msg_id} ({flagged_count} flagged)"
    )
    return msg_id


# T-4307: shape guards that keep a real finding from vanishing into a "clean"
# rc 0 once the structural check stopped requiring a `## Finding` heading.
# Both are deliberately broader than `_FINDING_HEADING_RE` (loud false positive
# > silent loss) and change only land_findings' return code + message, never
# what gets filed.
#
# Any markdown heading line (1-6 '#', optional leading indent) whose text
# mentions "finding", any case -- minus the strict headings -- is a near-miss:
# `### Finding 1`, `# Finding 1`, `## finding 1`, `## Findings`, `## Finding1`,
# `##Finding 1`, ` ## Finding 1`, `## 1. Finding: t`.
_FINDING_NEAR_MISS_LINE_RE = re.compile(
    r"^[ \t]*#{1,6}[^\n]*finding[^\n]*$", flags=re.MULTILINE | re.IGNORECASE
)
# The one required bullet every real finding block carries; catches shapes with
# no heading line to match at all (`**Finding 1: t**`, `## F-1: t`).
_SEVERITY_BULLET_RE = re.compile(
    r"^[ \t]*-[ \t]+\*\*severity:\*\*", flags=re.MULTILINE | re.IGNORECASE
)


def _findings_shape_problems(text: str, parsed: int) -> list[str]:
    """One human-readable line per way `text` looks like it carries findings
    that `parse_findings` (which returned `parsed` records) did NOT turn into
    tickets. Empty list = the file is consistent: either a genuinely clean
    cycle or every finding parsed. Pure function.
    """
    problems: list[str] = []
    strict = len(_FINDING_HEADING_RE.findall(text))
    if strict and not parsed:
        problems.append(
            f"{strict} '## Finding' heading(s) but none parsed "
            f"(malformed auditor output, not a clean cycle)"
        )
    elif strict > parsed:
        problems.append(
            f"only {parsed} of {strict} '## Finding' block(s) parsed "
            f"(the rest were malformed or over the {_FINDING_SOFT_CAP}-finding cap); "
            f"parseable ones were still filed"
        )
    near = [
        ln.strip() for ln in _FINDING_NEAR_MISS_LINE_RE.findall(text)
        if not _FINDING_HEADING_RE.match(ln)
    ]
    if near:
        shown = ", ".join(repr(ln) for ln in near[:3])
        problems.append(
            f"{len(near)} near-miss finding heading(s) not recognised as "
            f"'## Finding <n>: <title>' ({shown}); a real finding may not have been filed"
        )
    severity_bullets = len(_SEVERITY_BULLET_RE.findall(text))
    # T-4314 (T-4307 round-2 scope item 5): only when the bullets outnumber the
    # strict headings too. Bullets that merely match the headings are already
    # explained by the "only P of S parsed" line above (one cause, one line --
    # an over-cap file used to emit both).
    if severity_bullets > max(parsed, strict):
        problems.append(
            f"{severity_bullets} '- **Severity:**' bullet(s) but only {parsed} finding(s) "
            f"parsed; a real finding may not have been filed"
        )
    return problems


def land_findings(
    slot_slug: str,
    findings_path: Path,
    today: date,
    *,
    cwd: Path = REPO_ROOT,
) -> int:
    """Parse `findings_path`, file one ticket per finding. Returns 1 if the
    findings artifact fails the structural check, any filing errors, or the
    shape guards (`_findings_shape_problems`) say a real finding may not have
    been filed; else 0.

    T-4307: a structurally valid file that carries NO finding-shaped content is
    a clean audit cycle -- it files nothing, logs "filed 0, skipped 0, errored
    0", and returns 0. That is deliberately distinct from a file that looks
    like it carries findings the parser did not turn into tickets ("the audit
    broke"): headings present but none parsed, only some blocks parsed (the
    parseable ones are still filed), a near-miss heading such as `### Finding`,
    or more `- **Severity:**` bullets than parsed findings. Those all return 1
    rather than passing silently as if the auditor had found nothing. The
    guards change only the return code and a stderr/runner-log line -- never
    what gets filed.

    T-4314: "no finding-shaped content" is only a CLEAN cycle when the file is
    also SEALED -- its last line is the closing sentinel `s3star-audit-end:
    findings=N` with N matching the file's `## Finding` headings (see
    `_split_closing_sentinel`). Otherwise a run cut off after the frontmatter
    and preamble would land as clean. A sentinel problem (missing, N mismatch,
    content after it, more than one) is fail-closed: rc 1, NOTHING filed, no
    queue push -- distinct from the T-4307 shape problems above, which apply
    only inside a correctly sealed file. There is deliberately no legacy path:
    files from before T-4314 carry no sentinel and are rejected (they were all
    landed under the older rule; re-landing one today would also duplicate its
    tickets, since markers key on the landing date).

    T-2801: also pushes the claude-tier verification queue (see
    `push_claude_tier_queue`) after filing -- this is the one prod-mode step
    every supervised audit cycle runs (per the module docstring's dispatch
    -> launch -> observe -> land-findings -> teardown flow), so the push
    rides it rather than needing a new CLI mode of its own. Push
    success/failure never changes this function's return value -- only
    finding-filing errors do (see `push_claude_tier_queue`'s docstring for
    why).
    """
    if not findings_landed_ok(findings_path):
        msg = f"findings file not landed-ok: {findings_path}"
        print(msg, file=sys.stderr)
        _append_runner_log(f"[s3star] land-findings: {msg}")
        return 1

    text = findings_path.read_text(encoding="utf-8")

    # T-4314: closing-sentinel check runs BEFORE any parsing or filing and
    # short-circuits. FAIL-CLOSED (Orchestrator ruling on the T-4314 plan, Q2): a
    # sentinel problem means the run is incomplete or untrustworthy as a WHOLE
    # (a truncated file's last "parseable" finding may itself be cut short), and
    # partial filing followed by a re-run that lands on a later day would file
    # duplicates, since finding markers are keyed on the landing date. Like the
    # not-landed-ok return above, nothing else runs -- no push either.
    body, sentinel_problems = _split_closing_sentinel(text)
    if sentinel_problems:
        tag = f"[s3star] land-findings {slot_slug}:{today.isoformat()}"
        for problem in sentinel_problems:
            msg = f"{findings_path}: {problem}"
            print(msg, file=sys.stderr)
            _append_runner_log(f"{tag}: {msg}")
        summary = (
            f"{findings_path}: closing-sentinel check failed -- nothing filed "
            f"(run incomplete or untrustworthy; re-run the audit)"
        )
        print(summary, file=sys.stderr)
        _append_runner_log(f"{tag}: {summary}")
        return 1

    findings = parse_findings(body)
    problems = _findings_shape_problems(body, len(findings))
    filed = 0
    skipped = 0
    errored = 0
    for finding in findings:
        marker = _finding_marker(slot_slug, today, finding)
        if finding_already_filed(marker, tickets_root=cwd / "tickets"):
            skipped += 1
            continue
        outcome, tid, detail = file_finding(finding, slot_slug, today, cwd=cwd)
        if outcome == "filed":
            filed += 1
        else:
            errored += 1

    _append_runner_log(
        f"[s3star] land-findings {slot_slug}:{today.isoformat()}: "
        f"filed {filed}, skipped {skipped}, errored {errored}"
    )
    for problem in problems:
        msg = f"{findings_path}: {problem}"
        print(msg, file=sys.stderr)
        _append_runner_log(f"[s3star] land-findings {slot_slug}:{today.isoformat()}: {msg}")

    push_claude_tier_queue(cwd=cwd)

    return 1 if (errored or problems) else 0


# ---------------------------------------------------------------------------
# Verdict landing (T-2799, remediates T-2795) — the runner-side (HO-run)
# half of the workspace-artifact verdict channel: reads the isolated
# auditor's `verdicts.jsonl` workspace artifact (written per
# render_open_retests_section's instructions, since the auditor's
# isolation profile denies the canonical git-mutation lock
# --record-retest-verdict needs directly) and lands each valid entry via
# record_retest_verdict. Mirrors land_findings' parse -> validate -> land
# shape, but lands PER-FINDING retest_status flips instead of filing new
# tickets.
# ---------------------------------------------------------------------------


@dataclass
class VerdictEntry:
    ticket_id: str
    verdict: str  # one of _RETEST_VERDICT_CHOICES
    evidence_note: str


def parse_verdicts_artifact(text: str) -> list[VerdictEntry]:
    """Parse a workspace verdicts.jsonl artifact: one JSON object per
    non-blank line, `{"ticket": "T-NNNN", "verdict": "pass"|"fail"|
    "partial", "evidence_note": "..."}`.

    Malformed lines (bad JSON, not an object, missing/blank required key,
    verdict not in `_RETEST_VERDICT_CHOICES`) are skipped with a one-line
    stderr note — never crash (mirrors `parse_findings`' tolerance for
    malformed input; this runs inside an unattended landing path). Does
    NOT accept an "override" verdict — the artifact schema is limited to
    pass/fail/partial; override stays an HO-only direct action via
    `--record-retest-verdict --override`.
    """
    entries: list[VerdictEntry] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"[s3star] skip malformed verdict line {lineno}: {e}", file=sys.stderr)
            continue
        if not isinstance(obj, dict):
            print(
                f"[s3star] skip malformed verdict line {lineno}: not a JSON object",
                file=sys.stderr,
            )
            continue
        ticket_id = str(obj.get("ticket", "")).strip()
        verdict = str(obj.get("verdict", "")).strip().lower()
        evidence_note = str(obj.get("evidence_note", "")).strip()
        missing = []
        if not ticket_id:
            missing.append("ticket")
        if verdict not in _RETEST_VERDICT_CHOICES:
            missing.append("verdict")
        if not evidence_note:
            missing.append("evidence_note")
        if missing:
            print(
                f"[s3star] skip malformed verdict line {lineno}: invalid/missing {missing}",
                file=sys.stderr,
            )
            continue
        entries.append(
            VerdictEntry(ticket_id=ticket_id, verdict=verdict, evidence_note=evidence_note)
        )
    return entries


def land_verdicts(
    slot_slug: str,
    verdicts_path: Path,
    today: date,
    *,
    cwd: Path = REPO_ROOT,
) -> int:
    """Runner-side (HO-run) landing for the isolated auditor's workspace
    verdicts artifact. Returns 1 if the artifact is missing/empty or any
    entry was refused, else 0.

    Validates each entry against the audit's OWN enumerated open-retest
    set (`find_open_retest_tickets`) before landing — a ticket id absent
    from that set is treated as forged and refused, never landed.
    Duplicate ticket ids within the same artifact are ALL refused (an
    artifact naming the same finding twice cannot be trusted as to which
    entry is real).

    Every entry is landed with `principal="codex"` — this landing mode
    exists specifically to land the ISOLATED CODEX AUDITOR's own
    workspace artifact (by construction nothing else legitimately writes
    this file), so `record_retest_verdict`'s authorship-eligibility gate
    (T-2796) is always evaluated as Codex's own verdict, regardless of
    which account happens to run this HO-run CLI. An ineligible-principal
    refusal on an authorship-flagged finding is NOT a hard stop for the
    rest of the batch — it's logged and that entry is skipped, exactly
    like every other per-entry refusal here, and the finding's
    retest_status is left untouched (still pending, still routed — never
    dropped from re-test routing).
    """
    if not verdicts_path.exists():
        msg = f"verdicts file not found: {verdicts_path}"
        print(msg, file=sys.stderr)
        _append_runner_log(f"[s3star] land-verdicts: {msg}")
        return 1

    text = verdicts_path.read_text(encoding="utf-8")
    entries = parse_verdicts_artifact(text)
    if not entries:
        msg = f"no valid verdict entries parsed from {verdicts_path}"
        print(msg, file=sys.stderr)
        _append_runner_log(f"[s3star] land-verdicts: {msg}")
        return 1

    open_ids = {r.ticket_id for r in find_open_retest_tickets(cwd / "tickets")}

    counts: dict[str, int] = {}
    for e in entries:
        counts[e.ticket_id] = counts.get(e.ticket_id, 0) + 1
    duplicate_ids = {tid for tid, n in counts.items() if n > 1}

    landed = 0
    refused = 0
    for entry in entries:
        if entry.ticket_id in duplicate_ids:
            _append_runner_log(
                f"[s3star] land-verdicts: refused {entry.ticket_id} "
                f"(duplicate entry in artifact)"
            )
            refused += 1
            continue
        if entry.ticket_id not in open_ids:
            _append_runner_log(
                f"[s3star] land-verdicts: refused {entry.ticket_id} "
                f"(not in enumerated open-retest set -- forged or already closed)"
            )
            refused += 1
            continue
        try:
            record_retest_verdict(
                entry.ticket_id, verdict=entry.verdict, note=entry.evidence_note,
                principal="codex",
            )
        except _ticket_mod.TicketError as e:
            _append_runner_log(f"[s3star] land-verdicts: refused {entry.ticket_id}: {e}")
            refused += 1
            continue
        landed += 1

    _append_runner_log(
        f"[s3star] land-verdicts {slot_slug}:{today.isoformat()}: "
        f"landed {landed}, refused {refused}"
    )
    return 1 if refused else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T-5666: --await-audit -- the executable "safe to land" gate (read-only).
# ---------------------------------------------------------------------------

_AWAIT_STABLE_DEFAULT = 600.0
_AWAIT_TIMEOUT_DEFAULT = 7200.0
_AWAIT_POLL_DEFAULT = 30.0


def _audit_process_running(run) -> bool | None:
    """True/False if a `codex --profile <AUDIT_PROFILE>` process exists for the
    audit user; None if the probe itself failed (callers fail closed)."""
    try:
        result = run(
            ["pgrep", "-u", CODEX_AUDIT_USER, "-af", f"codex --profile {AUDIT_PROFILE}"],
            capture_output=True, text=True, timeout=_SESSION_STEP_TIMEOUT_SECONDS, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None  # hung or missing pgrep: unknown, fail closed, keep polling
    if result.returncode == 0:
        return bool((result.stdout or "").strip())
    if result.returncode == 1:
        return False
    return None


def _findings_sealed(path: Path) -> bool:
    """Sealed = `_split_closing_sentinel` reports no problems (one valid
    sentinel, last, N matching the headings): the same check land_findings uses."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return not _split_closing_sentinel(text)[1]


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def await_audit(
    slot_slug: str,
    today: date,
    *,
    run_dir: Path | None = None,
    stable_seconds: float = _AWAIT_STABLE_DEFAULT,
    timeout_seconds: float = _AWAIT_TIMEOUT_DEFAULT,
    poll_seconds: float = _AWAIT_POLL_DEFAULT,
    run=subprocess.run,
    now=time.time,
    sleep=time.sleep,
    out=print,
) -> int:
    """Block until it is safe to land the audit's outputs. READ-ONLY: the only
    subprocess is the pgrep probe; it never kills, lands, or tears down.

    Clears (rc 0) when EITHER the profiled codex process is gone, OR findings.md
    is sealed AND findings.md, verdicts.jsonl (if present) and the run dir have
    all been unmodified for >= stable_seconds. rc 1 = timeout (a probe failure
    persisting to timeout also lands here: fail closed); rc 2 = missing run dir.
    """
    run_dir = run_dir if run_dir is not None else run_workspace_dir(slot_slug, today)
    if not run_dir.is_dir():
        out(f"ERROR: run dir {run_dir} does not exist")
        return 2
    findings = run_dir / "findings.md"
    verdicts = run_dir / "verdicts.jsonl"
    start = now()
    while True:
        t = now()
        mt = {n: _mtime(p) for n, p in (
            ("findings.md", findings), ("verdicts.jsonl", verdicts), ("run_dir", run_dir))}
        mtimes = ", ".join(
            f"{n}={'absent' if v is None else datetime.fromtimestamp(v, timezone.utc).strftime('%H:%M:%SZ')}"
            for n, v in mt.items()
        )
        running = _audit_process_running(run)
        if running is False:
            out(f"await-audit: CLEARED (process exited); {mtimes}")
            return 0
        if running is None:
            out("await-audit: process probe failed; treating as still running")
        known = [v for v in mt.values() if v is not None]
        if known and _findings_sealed(findings):
            age = t - max(known)
            if age >= stable_seconds:
                out(f"await-audit: CLEARED (findings sealed, unchanged {age:.0f}s >= "
                    f"{stable_seconds:.0f}s); {mtimes}")
                return 0
        if t - start >= timeout_seconds:
            out(f"await-audit: TIMEOUT after {t - start:.0f}s; do not land; {mtimes}")
            return 1
        sleep(poll_seconds)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ORG-038 S3* audit pilot scheduler. Default-safe: pass exactly "
                    "one of --emit-brief / --file-dispatch-ticket / --land-findings / "
                    "--land-verdicts / --launch-audit / --teardown-audit.",
    )
    p.add_argument("--slot", choices=sorted(VALID_SLOTS), default=None)
    p.add_argument("--force", action="store_true", help="treat the named/all slots as due regardless of cadence state")
    p.add_argument("--emit-brief", action="store_true", help="safe mode: print composed brief(s) as JSON, no side effects")
    p.add_argument("--file-dispatch-ticket", action="store_true", help="prod mode: file a supervised dispatch ticket for due slot(s)")
    p.add_argument("--auto-dispatch", action="store_true", help="raise a not-implemented error instead of dispatching (see module docstring)")
    p.add_argument("--land-findings", action="store_true", help="prod mode: parse a findings file and file per-finding tickets")
    p.add_argument("--findings-file", default=None, help="path to the findings artifact (required with --land-findings)")
    p.add_argument(
        "--land-verdicts", action="store_true",
        help="T-2799 (HO-run, canonical write): read a workspace verdicts.jsonl "
             "artifact and land each valid entry via record_retest_verdict. "
             "Requires --slot and --verdicts-file.",
    )
    p.add_argument(
        "--verdicts-file", default=None,
        help="path to the workspace verdicts artifact (required with --land-verdicts)",
    )
    p.add_argument(
        "--launch-audit", action="store_true",
        help="HO-run (T-1604): spawn the dedicated codex-audit session, write a fresh "
             "brief, and launch the profiled auditor reading it. Requires --slot.",
    )
    p.add_argument(
        "--teardown-audit", action="store_true",
        help="HO-run (T-1604): tear down the dedicated codex-audit session for "
             "--slot/--date. Requires --slot.",
    )
    p.add_argument(
        "--date", default=None,
        help="YYYY-MM-DD; defaults to today. Used with --launch-audit/--teardown-audit "
             "so a session started on one date can be torn down correctly on another.",
    )
    p.add_argument(
        "--await-audit", action="store_true",
        help="T-5666 (read-only): poll until it is safe to land -- codex process gone, "
             "or findings.md sealed and all outputs unchanged for --stable-seconds. "
             "Requires --slot. rc 0 cleared, 1 timeout, 2 bad args/missing run dir.",
    )
    p.add_argument("--stable-seconds", type=float, default=_AWAIT_STABLE_DEFAULT,
                   help="--await-audit: required unchanged window (default 600)")
    p.add_argument("--timeout-seconds", type=float, default=_AWAIT_TIMEOUT_DEFAULT,
                   help="--await-audit: give up after this long (default 7200)")
    p.add_argument("--poll-seconds", type=float, default=_AWAIT_POLL_DEFAULT,
                   help="--await-audit: poll interval (default 30)")
    p.add_argument(
        "--record-retest-verdict", action="store_true",
        help="T-2613: record an independent re-test verdict on a finding ticket "
             "(structural close/reopen of retest_status). Requires --ticket and "
             "--note, plus exactly one of --verdict / --override.",
    )
    p.add_argument("--ticket", default=None, help="T-NNNN; required with --record-retest-verdict")
    p.add_argument(
        "--verdict", choices=_RETEST_VERDICT_CHOICES, default=None,
        help="independent re-test verdict for --record-retest-verdict; mutually "
             "exclusive with --override.",
    )
    p.add_argument(
        "--override", action="store_true",
        help="--record-retest-verdict: explicit non-verdict closure, mutually "
             "exclusive with --verdict.",
    )
    p.add_argument(
        "--note", default=None,
        help="reason/evidence for --record-retest-verdict; required.",
    )
    p.add_argument(
        "--no-push", dest="no_push", action="store_true", default=False,
        help="--record-retest-verdict: commit locally but skip git push.",
    )
    return p.parse_args(argv)


def _enforce_default_safe(args: argparse.Namespace) -> int | None:
    modes = [
        name for name, on in (
            ("--emit-brief", args.emit_brief),
            ("--file-dispatch-ticket", args.file_dispatch_ticket),
            ("--land-findings", args.land_findings),
            ("--land-verdicts", args.land_verdicts),
            ("--launch-audit", args.launch_audit),
            ("--teardown-audit", args.teardown_audit),
            ("--await-audit", args.await_audit),
            ("--record-retest-verdict", args.record_retest_verdict),
        ) if on
    ]
    if len(modes) != 1:
        print(
            "ERROR: pass exactly one of --emit-brief / --file-dispatch-ticket / "
            "--land-findings / --land-verdicts / --launch-audit / "
            "--teardown-audit / --await-audit / --record-retest-verdict.",
            file=sys.stderr,
        )
        return 64
    if args.await_audit:
        problem = None
        if args.slot is None:
            problem = "--await-audit requires --slot."
        elif args.stable_seconds < 0:
            problem = "--stable-seconds must be >= 0."
        elif args.timeout_seconds <= 0 or args.poll_seconds <= 0:
            problem = "--timeout-seconds and --poll-seconds must be > 0."
        elif args.date is not None:
            try:
                date.fromisoformat(args.date)
            except ValueError:
                problem = f"--date must be YYYY-MM-DD, got {args.date!r}."
        if problem:
            print(f"ERROR: {problem}", file=sys.stderr)
            return 2
    if args.land_findings and (args.slot is None or args.findings_file is None):
        print("ERROR: --land-findings requires --slot and --findings-file.", file=sys.stderr)
        return 64
    if args.land_verdicts and (args.slot is None or args.verdicts_file is None):
        print("ERROR: --land-verdicts requires --slot and --verdicts-file.", file=sys.stderr)
        return 64
    if args.launch_audit and args.slot is None:
        print("ERROR: --launch-audit requires --slot.", file=sys.stderr)
        return 64
    if args.teardown_audit and args.slot is None:
        print("ERROR: --teardown-audit requires --slot.", file=sys.stderr)
        return 64
    if args.record_retest_verdict:
        if not args.ticket:
            print("ERROR: --record-retest-verdict requires --ticket.", file=sys.stderr)
            return 64
        if not args.note:
            print("ERROR: --record-retest-verdict requires --note.", file=sys.stderr)
            return 64
        if bool(args.verdict) == bool(args.override):
            print(
                "ERROR: --record-retest-verdict requires exactly one of "
                "--verdict / --override.",
                file=sys.stderr,
            )
            return 64
    if args.date is not None:
        try:
            date.fromisoformat(args.date)
        except ValueError:
            print(f"ERROR: --date must be YYYY-MM-DD, got {args.date!r}.", file=sys.stderr)
            return 64
    return None


def _due_slots(args: argparse.Namespace, state: dict[str, Any], today: date) -> list[AuditSlot]:
    slugs = [args.slot] if args.slot else sorted(VALID_SLOTS)
    due: list[AuditSlot] = []
    for slug in slugs:
        slot = get_slot(slug)
        if args.slot or args.force:
            due.append(slot)
            continue
        if is_due(slot.cadence, last_fire_date(state, slug), today):
            due.append(slot)
    return due


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    refusal = _enforce_default_safe(args)
    if refusal is not None:
        return refusal

    today = datetime.now(timezone.utc).date()

    if args.emit_brief:
        state = load_state()
        records = []
        for slot in _due_slots(args, state, today):
            findings_path = run_workspace_dir(slot.slug, today) / "findings.md"
            snapshot_path = run_workspace_dir(slot.slug, today) / "runtime-snapshot.md"
            verdicts_path = run_workspace_dir(slot.slug, today) / "verdicts.jsonl"
            records.append({
                "slot": slot.slug,
                "cadence": slot.cadence,
                "brief": compose_audit_brief(
                    slot, today, findings_path, snapshot_path=snapshot_path,
                    tickets_root=REPO_ROOT / "tickets",
                ),
                "findings_path": str(findings_path),
                "snapshot_path": str(snapshot_path),
                "manifest_path": str(manifest_path_for(snapshot_path)),
                "verdicts_path": str(verdicts_path),
            })
        print(json.dumps(records, indent=2))
        return 0

    if args.file_dispatch_ticket:
        if args.auto_dispatch:
            _raise_auto_dispatch_not_implemented()
        state = load_state()
        due = _due_slots(args, state, today)
        if not due:
            print(f"[{today.isoformat()}] no audit slots due")
            return 0
        failures = 0
        for slot in due:
            if dispatch_ticket_already_filed(slot.slug, today):
                print(f"[{today.isoformat()}] {slot.slug}: dispatch ticket already filed, skipping")
                continue
            findings_path = run_workspace_dir(slot.slug, today) / "findings.md"
            outcome, tid, detail = file_dispatch_ticket(slot, today, findings_path)
            if outcome == "filed":
                print(f"[{today.isoformat()}] {slot.slug}: filed dispatch ticket {tid or '(id?)'}")
                update_state(state, slot.slug, today)
            else:
                msg = f"{slot.slug}: FAILED to file dispatch ticket: {detail}"
                print(f"[{today.isoformat()}] {msg}", file=sys.stderr)
                # Durable record at the main() boundary — stderr alone is not
                # durable (T-1583 REBOUND finding). This is in addition to,
                # not a substitute for, the log_error callback wired into
                # file_dispatch_ticket's _invoke_ticket_file call above.
                _append_runner_log(f"[s3star] file-dispatch-ticket: {msg}")
                failures += 1
        save_state(state)
        return 1 if failures else 0

    if args.land_findings:
        findings_path = Path(args.findings_file)
        return land_findings(args.slot, findings_path, today)

    if args.land_verdicts:
        verdicts_path = Path(args.verdicts_file)
        return land_verdicts(args.slot, verdicts_path, today)

    if args.launch_audit:
        slot = get_slot(args.slot)
        run_date = date.fromisoformat(args.date) if args.date else today
        result = launch_audit_session(slot, run_date)
        print(
            f"[{run_date.isoformat()}] {slot.slug}: launched {result['session']} -- "
            f"brief {result['brief_path']}, findings expected at {result['findings_path']}"
        )
        return 0

    if args.teardown_audit:
        run_date = date.fromisoformat(args.date) if args.date else today
        teardown_audit_session(args.slot, run_date)
        print(
            f"[{run_date.isoformat()}] {args.slot}: torn down "
            f"{codex_audit_session_name(args.slot, run_date)}"
        )
        return 0

    if args.await_audit:
        run_date = date.fromisoformat(args.date) if args.date else today
        return await_audit(
            args.slot, run_date,
            stable_seconds=args.stable_seconds,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )

    if args.record_retest_verdict:
        new_status = record_retest_verdict(
            args.ticket,
            verdict=args.verdict,
            override=args.override,
            note=args.note,
            no_push=args.no_push,
        )
        print(f"record-retest-verdict {args.ticket}: retest_status -> {new_status}")
        return 0

    print("ERROR: no live mode selected (gate drift?)", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
