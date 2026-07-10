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
ticket instructing the operator (Orchestrator) to paste the composed brief into the
running codex tmux session (``codex-session``, per
``tools/codex_modal_monitor.py``'s ``CODEX_PANE`` default) and later save
Codex's returned findings to a known path; ``--land-findings`` parses that
findings file and files the per-finding tickets. ``--auto-dispatch`` exists
as an explicit flag that raises a clear not-implemented error (mirrors
``CodexHarnessAdapter.resume_session``'s own honesty pattern) rather than
silently no-op-ing, so the gap stays visible at the exact surface a future
implementer will touch once codex inbound wiring lands.

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
        Prod mode. File ONE target=orchestrator dispatch ticket per due (or named)
        slot, embedding the brief + supervised hand-off instructions.
        --auto-dispatch raises a clear not-implemented error instead of running.

    python3 tools/s3star_audit_runner.py --land-findings --slot SLOT --findings-file PATH
        Prod mode. Parse a findings file Codex/the operator wrote, file one
        ticket per finding to the domain owner (never target=codex).

State file: ``~/.local/state/pantheon/s3star_audit/state.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.watch_runner import _invoke_ticket_file  # noqa: E402 — reuse the proven retry/subprocess kernel.

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

# Per tools/codex_modal_monitor.py's CODEX_PANE default — the confirmed live
# tmux session name for the codex daemon on this VPS.
CODEX_TMUX_SESSION = "codex-session"

# ---------------------------------------------------------------------------
# S3* audit isolation profile (T-1598, Phase 1 of the T-1598 design draft) —
# a dedicated, permission-enforced Codex profile so the read-only audit
# contract is structurally enforced, not prose-enforced. Overlay file
# /home/codex/.codex/s3star-audit.config.toml sets sandbox_mode=workspace-write
# with writable_roots=[] (the launch cwd below is the ONLY writable path),
# network_access=false, and disables the telegram MCP server.
#
# AUDIT_WORKSPACE_ROOT is owned the-ancestor:veneration, setgid (2770).
# Codex (uid in group veneration) writes here as its one writable root under
# the profile; the runner (claude, also in group veneration — confirmed via
# `id`) both writes the runtime snapshot here directly and reads back
# whatever codex wrote, with no cross-account ACL grant needed in either
# direction. This deliberately replaces the T-1599-era convention of putting
# findings/snapshot artifacts under STATE_DIR (~/.local/state/pantheon/...,
# home-owned 750 by claude) -- that path is NOT traversable by codex at all,
# which would have made a live dispatch's find-the-findings-file step fail
# structurally. STATE_DIR remains exactly where it was for state.json + the
# runner's own log; only the per-cycle findings/snapshot artifacts move.
AUDIT_WORKSPACE_ROOT = Path("/home/the-ancestor/s3star-workspace")

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
CODEX_LAUNCH_CMD = f"cd {AUDIT_WORKSPACE_ROOT} && codex --profile s3star-audit"

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

# Mirrors tools/mailbox_mcp.py's MAILBOX_HUB / tools/mailbox_wake.py's hub path
# convention. Defined locally rather than imported: importing mailbox_mcp would
# pull in the `mcp` SDK dependency for a bare path string.
_SNAPSHOT_MAILBOX_HUB = Path("/home/the-ancestor/mailbox")
_SNAPSHOT_MAILBOX_AGENTS = ("codex", "orchestrator", "specialist", "recovery", "the-ancestor")

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
test -w /home/the-ancestor/mailbox/orchestrator
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
        ),
        _SnapshotCommand(
            "systemctl --user list-units --all --no-pager",
            ["systemctl", "--user", "list-units", "--all", "--no-pager"],
        ),
        _SnapshotCommand(
            "ps -eo user,pid,ppid,stat,comm,args",
            ["ps", "-eo", "user,pid,ppid,stat,comm,args"],
        ),
        _SnapshotCommand("ss -lntup", ["ss", "-lntup"]),
        _SnapshotCommand(
            "ls -ld -- mailbox hub + per-agent roots (named ids)",
            ["ls", "-ld", *roots],
        ),
        _SnapshotCommand(
            "ls -ldn -- mailbox hub + per-agent roots (numeric ids)",
            ["ls", "-ldn", *roots],
        ),
        _SnapshotCommand("getent group veneration", ["getent", "group", "veneration"]),
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


def capture_runtime_snapshot(
    snapshot_path: Path,
    *,
    generated_at: str | None = None,
    run=subprocess.run,
) -> Path:
    """Capture runner-side host runtime evidence and write it to `snapshot_path`.

    Warn-not-fail per command (see `_run_snapshot_command`); the file write
    itself is NOT wrapped -- a disk-full/permission fault here is a real
    runner failure, and a brief that references a snapshot path with nothing
    behind it is worse than a loud failure at dispatch time.
    """
    ts = generated_at if generated_at is not None else _utc_iso_now()
    sections = [
        f"## {cmd.label}\n\n```\n{_run_snapshot_command(cmd, run=run)}\n```"
        for cmd in _snapshot_commands()
    ]
    text = (
        f"---\ngenerated_at: {ts}\nkind: s3star-runtime-snapshot\n---\n\n"
        f"# S3* runner-captured host runtime evidence snapshot\n\n"
        f"{_SNAPSHOT_CAVEAT}\n\n"
        f"Generated: {ts} (UTC)\n\n"
        + "\n\n".join(sections)
        + "\n"
    )
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(text, encoding="utf-8")
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


def compose_audit_brief(
    slot: AuditSlot,
    today: date,
    findings_path: Path,
    *,
    proposal_text: str | None = None,
    snapshot_path: Path | None = None,
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
"""

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
    ask = (
        f"S3* audit dispatch due: {slot.slug} ({slot.cadence}) for {today.isoformat()}. "
        f"SUPERVISED FIRST-FIRE (ORG-038 pilot): in the running codex tmux session "
        f"(`{CODEX_TMUX_SESSION}`), launch the audit session under the isolation "
        f"profile with `{CODEX_LAUNCH_CMD}`, then paste the brief below. Observe "
        f"the run, and save Codex's returned findings to `{findings_path}`. Then "
        f"file the per-finding tickets by running: `python3 tools/s3star_audit_runner.py "
        f"--land-findings --slot {slot.slug} --findings-file {findings_path}`."
    )
    context = (
        f"{_dispatch_marker(slot.slug, today)}\n\n"
        f"--- BRIEF (paste verbatim into tmux session `{CODEX_TMUX_SESSION}`) ---\n\n"
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
    if snapshot_path is None:
        snapshot_path = AUDIT_WORKSPACE_ROOT / f"{today.isoformat()}-{slot.slug}-runtime-snapshot.md"
    capture_runtime_snapshot(snapshot_path)
    brief = compose_audit_brief(slot, today, findings_path, snapshot_path=snapshot_path)
    ask, context = build_dispatch_ticket_bodies(slot, today, brief, findings_path)
    if env is None:
        env = os.environ.copy() | {"PANTHEON_ACTOR": "orchestrator"}
    argv = [
        sys.executable,
        str(ticket_bin),
        "file",
        "--requester", "orchestrator",
        "--target", "orchestrator",
        "--title", f"S3* audit dispatch — {slot.slug} {today.isoformat()}",
        "--labels", "s3star-audit,audit-dispatch",
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
    and `date:` keys, plus at least one `## Finding` heading in the body.
    Mirrors the *pattern* tools/watch_runner.py's `_report_is_structured` uses
    (frontmatter + heading check) — a new function because the finding-artifact
    frontmatter schema (slot/date/auditor) differs from the watch-report schema
    (surface/headline).
    """
    m = re.match(r"^---\n(.*?)\n---", text, flags=re.DOTALL)
    if not m:
        return False
    fm_lines = m.group(1).splitlines()
    has_slot = any(re.match(r"\s*slot\s*:\s*\S", line) for line in fm_lines)
    has_date = any(re.match(r"\s*date\s*:\s*\S", line) for line in fm_lines)
    if not (has_slot and has_date):
        return False
    return bool(re.search(r"^## Finding\b", text, flags=re.MULTILINE))


def findings_landed_ok(path: Path) -> bool:
    """Structural completion check: exists, at least FINDINGS_MIN_BYTES, and
    has the required frontmatter + `## Finding` heading structure.
    """
    if not path.exists():
        return False
    if path.stat().st_size < FINDINGS_MIN_BYTES:
        return False
    return _findings_file_is_structured(path.read_text(encoding="utf-8"))


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
    headers = re.findall(r"^## (Finding\b[^\n]*)$", text, flags=re.MULTILINE)
    blocks = re.split(r"^## Finding\b[^\n]*$", text, flags=re.MULTILINE)[1:]

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
    "orchestrator": "orchestrator",
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
    "specialist": "specialist",
    "research": "specialist",
    "watch": "specialist",
    # Recovery's domain (ORG-027 §1: recovery + ticket-ops).
    "recovery": "recovery",
    "recovery": "recovery",
    "ticket-ops": "recovery",
    "tickets": "recovery",
    "restart": "recovery",
    # Constitutional.
    "operator": "operator",
    # Codex never owns remediation of its own audit findings.
    "codex": "orchestrator",
}
_DEFAULT_OWNER = "orchestrator"


def resolve_owner(owner_text: str) -> str:
    return _OWNER_ALIASES.get(owner_text.strip().lower(), _DEFAULT_OWNER)


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
    return f"s3star-audit-finding: {slot_slug}:{today.isoformat()}:{finding.finding_id}"


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
        "--requester", "orchestrator",
        "--target", owner,
        "--title", f"S3* audit finding {finding.finding_id}: {finding.title}"[:120],
        "--labels", ",".join(labels),
        "--ask", ask,
        "--context", context,
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


def land_findings(
    slot_slug: str,
    findings_path: Path,
    today: date,
    *,
    cwd: Path = REPO_ROOT,
) -> int:
    """Parse `findings_path`, file one ticket per finding. Returns 1 if the
    findings artifact fails the structural check or any filing errors, else 0.
    """
    if not findings_landed_ok(findings_path):
        msg = f"findings file not landed-ok: {findings_path}"
        print(msg, file=sys.stderr)
        _append_runner_log(f"[s3star] land-findings: {msg}")
        return 1

    text = findings_path.read_text(encoding="utf-8")
    findings = parse_findings(text)
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
    return 1 if errored else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ORG-038 S3* audit pilot scheduler. Default-safe: pass exactly "
                    "one of --emit-brief / --file-dispatch-ticket / --land-findings.",
    )
    p.add_argument("--slot", choices=sorted(VALID_SLOTS), default=None)
    p.add_argument("--force", action="store_true", help="treat the named/all slots as due regardless of cadence state")
    p.add_argument("--emit-brief", action="store_true", help="safe mode: print composed brief(s) as JSON, no side effects")
    p.add_argument("--file-dispatch-ticket", action="store_true", help="prod mode: file a supervised dispatch ticket for due slot(s)")
    p.add_argument("--auto-dispatch", action="store_true", help="raise a not-implemented error instead of dispatching (see module docstring)")
    p.add_argument("--land-findings", action="store_true", help="prod mode: parse a findings file and file per-finding tickets")
    p.add_argument("--findings-file", default=None, help="path to the findings artifact (required with --land-findings)")
    return p.parse_args(argv)


def _enforce_default_safe(args: argparse.Namespace) -> int | None:
    modes = [
        name for name, on in (
            ("--emit-brief", args.emit_brief),
            ("--file-dispatch-ticket", args.file_dispatch_ticket),
            ("--land-findings", args.land_findings),
        ) if on
    ]
    if len(modes) != 1:
        print(
            "ERROR: pass exactly one of --emit-brief / --file-dispatch-ticket / "
            "--land-findings.",
            file=sys.stderr,
        )
        return 64
    if args.land_findings and (args.slot is None or args.findings_file is None):
        print("ERROR: --land-findings requires --slot and --findings-file.", file=sys.stderr)
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
            findings_path = AUDIT_WORKSPACE_ROOT / f"{today.isoformat()}-{slot.slug}-findings.md"
            snapshot_path = AUDIT_WORKSPACE_ROOT / f"{today.isoformat()}-{slot.slug}-runtime-snapshot.md"
            records.append({
                "slot": slot.slug,
                "cadence": slot.cadence,
                "brief": compose_audit_brief(slot, today, findings_path, snapshot_path=snapshot_path),
                "findings_path": str(findings_path),
                "snapshot_path": str(snapshot_path),
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
            findings_path = AUDIT_WORKSPACE_ROOT / f"{today.isoformat()}-{slot.slug}-findings.md"
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

    print("ERROR: no live mode selected (gate drift?)", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
