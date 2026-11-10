#!/usr/bin/env python3
"""Pantheon ticket lifecycle CLI.

Implements the ticket system ratified in `adr/ORG-003-ticket-system.md`
(amended by ORG-019: `blocked/` state retired; blocks expressed via
`not_before` or `depends_on` frontmatter only).

States: inbox → active → done  (3-state machine, no blocked/).

Subcommands:
    ticket.py file --requester X --target Y --title "..." [--labels A,B] [--depends-on T-NNNN,...] [--stale-after 4h] [--ask "..."] [--context "..."]
    ticket.py claim <T-NNNN>
    ticket.py block <T-NNNN> (--not-before YYYY-MM-DD | --depends-on T-NNNN) [repeatable --depends-on]
    ticket.py resolve <T-NNNN> [--with "summary"]
    ticket.py cascade --tid <T-NNNN> [--no-push]  (ORG-013 cascade-unblock pass
        for an already-resolved tid; T-3289 PR-merge trigger — the tid never
        moves, only its dependants' depends_on frontmatter changes)
    ticket.py list [--state inbox|active|done] [--target X] [--label X]

`list` is read-only. Every other subcommand performs exactly one
filesystem-change-plus-commit-plus-push, gated on the same auto-commit
discipline as `tools/watch_runner.py` (HERMES guard, narrow scope, ff-only
pull, plain push, no force, no --no-verify).

`block` is a frontmatter-edit command (no state change / no dir move). It
sets `not_before` and/or `depends_on` on the ticket in place. The ticket
stays in its current state directory (inbox/ or active/).

Actor identity for transition-log entries comes from `$PANTHEON_ACTOR` env
var if set, else the OS username (`getpass.getuser()`).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make `from tools._git_utils import ...` work both as a script
# (`python3 tools/ticket.py ...` per the docstring's documented usage) and
# as a package import (`python3 -m unittest tools.test_ticket`). When
# invoked as a script, sys.path prepends `tools/` rather than the repo root,
# so the `tools` package isn't reachable; this bootstrap injects the repo
# root in that case. In package mode `__package__ == "tools"`, no-op.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import (  # noqa: E402  — after sys.path bootstrap.
    PULL_FETCH_FAILED_PREFIX,
    PULL_NOT_AHEAD_PREFIX,
    PULL_REBASE_CONFLICT_PREFIX,
    _git,
    _hermes_scan_file,
    _hermes_scan_message,
    _mid_operation_kind,
    git_mutation_lock as _git_mutation_lock_raw,
    pull_or_rebase_strict,
    push_with_retry,
)
from tools._md_table import _md_row  # noqa: E402  — leaf module, re-only (T-1936)
from tools import _frontmatter_block  # noqa: E402  — leaf module, re-only (T-5689)
from tools.agent_role_map import (  # noqa: E402  — after sys.path bootstrap.
    account_persona_map as _account_persona_map,
)
# T-3020 BT3 / ORG-078 §2-3: priority-list parse — leaf module (only stdlib +
# yaml), no cycle risk importing it at module scope (unlike ticket_lint,
# which imports FROM this module).
from tools import priority_list_parse  # noqa: E402
from tools import _security_gate  # noqa: E402  — T-5678, pure leaf module

# T-0761 c4: clone-on-demand repo provisioning (called after a successful claim).
# Guarded top-level import: module may be absent in isolated worktrees used by
# tests that spawn ticket.py as a subprocess (e.g. TestRetargetDetachedHeadPush).
# When absent, provision_repos is set to None and cmd_claim skips provisioning
# gracefully. Module-level name is required so test patches resolve cleanly.
try:
    from tools.provision_repos import provision_repos  # noqa: E402
except ImportError:
    provision_repos = None  # type: ignore[assignment]

# T-4751 S1: ORG-021's machine-read frontmatter field registry. Every write
# funnels through _build_ticket_text, which checks the fields it serializes
# against the registry (WARN by default; refuse mode for S2). Guarded like
# provision_repos: subprocess fixtures copy a minimal tools/ tree without it,
# and a missing checker must fail OPEN (ORG-021 §registry), never block a write.
try:
    from tools import frontmatter_registry as _fm_registry  # noqa: E402
except ImportError:
    _fm_registry = None  # type: ignore[assignment]

try:
    from tools import decision_lines as _decision_lines  # noqa: E402
except ImportError:
    _decision_lines = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
TICKETS_ROOT = REPO_ROOT / "tickets"

STATES = ("inbox", "active", "done")  # ORG-019: blocked/ retired

# T-3168 migration leg: malformed ref syntax is always refused, while missing
# or dangling graph state is warning-only.  The literal is intentionally
# auditable; flipping it to ``enforce`` is a separately approved activation.
TICKET_PURPOSE_ENFORCEMENT = "warn"

# T-3020 BT3 / ORG-078 §4: failure-mode + cadence-observation telemetry sink
# for `cmd_list`'s priority-sort path. ADR-literal path (matches BT1's
# priority_list_parse.py docstring) — `.local/log/` is this tree's existing
# home for append-only diagnostic logs (e.g. inbox_consumer.log); `.jsonl` here is a deliberate ADR-directed exception
# to the `.local/state/` convention most OTHER `.jsonl` trend files use.
PRIORITY_LIST_HEALTH_PATH = Path.home() / ".local" / "log" / "pantheon" / "priority_list_health.jsonl"

# T-3080 / ORG-078 §Priority-list-drift-hygiene: `ticket.py priority-list
# validate` heuristic thresholds. Named module constants keep them readable
# + change-in-one-place; two knobs don't justify a config file.
#
# NOTE: semantic-drift heuristic DEFERRED (T-3424 follow-up). ORG-078's
# "label suddenly appearing on many tickets outside its original arc"
# wording implies a temporal / rate-of-change signal; the first-cut
# static parent_ticket-root-fanout implementation false-fired on 6 of 8
# real priority-list labels (legitimate cross-cutting design-layer labels
# like org-065 / ticket-flow / harness-controller trip a static count).
# Deferred pending a proper design pass on the drift signal shape.
PRIORITY_LIST_HIGH_VOLUME_THRESHOLD = 30  # label carried by > N tickets -> "may be over-broad"
PRIORITY_LIST_SAMPLE_SIZE = 5  # sample matching (ticket_id, title) pairs shown per label

# MUST stay in sync with sibling at pantheon-pwa/src/server/ticketRoutes.ts
RATIFIABLE_REPO_ALLOWLIST = ("pantheon-config", "revenue-project", "echo-prismatic", "pantheon-pwa",
                             "pantheon-public")  # T-1964: seed ratify surfaces live here (T-1933 arc)

FRONT_MATTER_FIELD_ORDER = (
    "id",
    "created",
    "requester",
    "target",
    # ORG-078 / T-3020 BT2: when `target` last changed via `ticket.py retarget`.
    # Written transactionally by cmd_retarget alongside the target field itself
    # (same file write, same commit) — feeds ORG-078 §3's effective_queue_ts
    # sort key. Absent on tickets never retargeted; readers fall back to
    # `created` for those (see ORG-078 §3).
    "retargeted_at",
    "auto_declined_by",  # T-5049: seats that were offered + left unclaimed; retarget clears
    "state",
    # T-3168 / ORG-054: stable project membership and nearest typed purpose.
    # Explicit null is meaningful for project/goal roots and maintenance work.
    "project_id",
    "nearest_intent_ref",
    "parent_ticket",
    "depends_on",
    # T-2841 / ORG-065 BT1: typed-edge fields (warn-only model layer). Absorb
    # depends_on/parent_ticket/ratifiable_files as-is (no parallel fields) —
    # these four are the NEW edges: implements/verifies -> a ticket or an ADR
    # obligation (`ORG-NNN#E1[.C1]`); supersedes -> the node this replaces;
    # informs -> advisory notify only, NEVER gates, acyclicity-EXEMPT (see
    # _GATING_EDGE_FIELDS in ticket_lint.py). All optional; unknown target is
    # a corpus-wide lint WARN, never a refusal here (resolved-decision 1).
    "implements",
    "verifies",
    "supersedes",
    "informs",
    # T-3476 / ORG-065 Call 1: a distinct edge type from `implements` — a
    # ticket that RATIFIES an ADR obligation (the operator's TG 6734 ruling) is
    # graph-distinguishable from one that IMPLEMENTS it. The load-bearing
    # invariant: a `ratifies` edge alone must never satisfy an implements-
    # obligation (see ticket_lint.detect_unverified_elements). Same target
    # shape as the four fields above (ticket id or ADR-NNN#E<k>[.C<m>]),
    # including the reserved whole-ADR sentinel `#E0` (T-3205 policy).
    "ratifies",
    # T-3328: the file-time lineage-guard escape-hatch justification. Written
    # only when `--orphan "<justification>"` was used to file a ticket with NO
    # lineage edge (parent_ticket/depends_on/serves/implements/verifies/
    # supersedes/informs) — a genuine root-tier ticket. Registered adjacent to
    # the edge fields it explains the absence of. NOT the same concept as
    # ticket_lint.py's `orphan_ratifications` (a ratifiable ticket with no open
    # ratify ticket) — that's a report-only detector, unrelated frontmatter.
    "orphan_reason",
    # T-5014: the file-time attachment-gate escape-hatch justification. Written
    # only when `--no-file "<reason>"` was used to file (or retarget) a
    # target=operator ticket that genuinely has no file to attach. Never stored
    # for non-operator targets. Audited by ticket_lint's unattached-ask warning.
    "no_file_reason",
    # T-2842 / ORG-065 BT2: the closure-gate evidence field. Deliberately
    # separate from `verifies` (which stays exactly as BT1 built it — a
    # coarse declarative edge) — `receipts` carries the actual evidence a
    # `verifies`/`implements` obligation edge needs to CLOSE (§3.3/§3.5).
    # Entries are encoded `<clause>=<receipt-id>=<provenance>` strings (see
    # _parse_receipt_entries / ticket_lint.decode_receipt) so this reuses the
    # existing flow-list serializer unchanged — no new frontmatter shape.
    "receipts",
    "labels",
    # T-5715 / ORG-078 Amendment 1: optional intra-tier ordering. Lower
    # integers sort first, but only after the label/dependency-derived tier.
    "tier_rank",
    "stale_after",
    "not_before",
    # T-2833: the mandatory justification for a not_before date gate. Registered
    # adjacent to not_before so the pair renders together; a date gate must carry
    # a `operator:<ref>` anchor (see _validate_not_before_anchor). Scalar, not a list.
    "not_before_anchor",
    # T-2209: the symmetric inverse of not_before. `not_before` says "not yet";
    # `decide_by` says "not later than". Registered here rather than left to the
    # unknown-keys round-trip fallback so it renders in a stable position next
    # to its sibling — a field that survives only as an unrecognised leftover is
    # one schema tidy-up away from being silently dropped.
    "decide_by",
    "ratifiable_files",
    "intent",
    "operator_intent",  # T-5015: intent preserved across retarget away from operator
    "serves",
    "retest_status",
    # T-3440 / ORG-089: appetite circuit-breaker + DROPPED substate schema.
    # `appetite` is the machine-readable bet-sizing field (int 1-10, ORG-089
    # §Decision.4 / D-E); `bet_ref` optionally names the bet this ticket serves;
    # `substate` carries the two ORG-021-frontmatter substates this ADR adds
    # (`appetite-overrun` enforcing gate, `dropped` terminal disposition);
    # `dropped_reason` is REQUIRED when substate==dropped (ORG-021 §substates);
    # `dropped_from_state` records the open dir the drop came from (inbox|active).
    "appetite",
    "bet_ref",
    "substate",
    "dropped_reason",
    "dropped_from_state",
    # T-4119: a work-complete ticket's PR/branch pointer while `substate:
    # awaiting-merge` holds its WIP slot open without unclaiming (see
    # sop/standard/ticket-flow.md). Free-text — e.g. `echo-prismatic#365` or
    # `pantheon-config:agent/T-4061` — set/cleared by `ticket.py await-merge`,
    # stripped on resolve. Registered adjacent to `substate` (the field it
    # always accompanies) rather than up with `bet_ref`/`appetite`, since it
    # is T-4119's own field, not part of the ORG-089 appetite schema above.
    "awaiting_merge_ref",
    # T-4707 / ORG-076 BT1 / RFC-004 §3(a): runtime repair-ticket attributes, set ONLY
    # through `file --repair-fields-json` (see REPAIR_FIELD_SPEC). Flat scalars, NOT
    # ORG-065 typed edges. `evidence_snapshot` renders as a YAML `|` block scalar.
    "fault_id",
    "episode_id",
    "domain_key",
    "invariant_list_id",
    "invariant_list_digest",
    "subsystem",
    "detector",
    "verdict",
    "action",
    "evidence_deferred",
    "repair_route",
    "attempts_budget",
    "recovered_before_repair",
    "evidence_snapshot",
)
# ORG-019: blocked_by / unblock_when / unblock_check removed from schema.
# Legacy tickets may still carry these fields; serialize_front_matter emits
# them via the "unknown keys" fallback at the end so existing data round-trips
# without data loss until Phase 3 migration strips them.
REQUIRED_FRONT_MATTER = ("id", "created", "requester", "target", "state")
OPTIONAL_FRONT_MATTER = (
    "project_id",  # T-3168 / ORG-054; explicit null differs from absence
    "nearest_intent_ref",
    "parent_ticket",
    "retargeted_at",  # T-3020 BT2 / ORG-078
    "auto_declined_by",  # T-5049
    "depends_on",
    "implements",  # T-2841 / ORG-065 BT1
    "verifies",
    "supersedes",
    "informs",
    "ratifies",  # T-3476 / ORG-065 Call 1
    "orphan_reason",  # T-3328
    "no_file_reason",  # T-5014
    "receipts",  # T-2842 / ORG-065 BT2
    "labels",
    "tier_rank",  # T-5715 / ORG-078 Amendment 1
    "stale_after",
    "not_before",
    "not_before_anchor",  # T-2833
    "decide_by",
    "ratifiable_files",
    "intent",
    "operator_intent",  # T-5015
    # T-5678 / ORG-105 s4: the security-ticket gate. security_gate is pending|accepted|
    # rejected; pending is skipped by every pull path (tools/_security_gate.is_pending).
    "security_gate",
    "security_rank",
    "threat_unclassified",  # T-5678 r1: automated filer, no chain yet; accept supplies it
    "security_filer",
    "threat_foothold",
    "threat_impact",
    "threat_path",
    "threat_justification",
    "not_security_reason",
    "serves",
    "retest_status",
    "appetite",  # T-3440 / ORG-089
    "bet_ref",
    "substate",
    "dropped_reason",
    "dropped_from_state",
    "awaiting_merge_ref",  # T-4119
)
# T-4707 / RFC-004 §3(a): the closed allowlist of runtime repair-ticket attributes
# accepted by `file --repair-fields-json`. kind: str (single-line scalar), enum, bool,
# int, block (multi-line `|` scalar). Required keys must be present.
REPAIR_FIELD_SPEC: dict = {
    "fault_id": ("str", True),
    "episode_id": ("str", True),
    "domain_key": ("str", True),
    "invariant_list_id": ("str", False),
    "invariant_list_digest": ("str", False),
    "subsystem": ("str", True),
    "detector": ("str", True),
    "verdict": ("enum:broken,unknown", True),
    "action": ("str", False),
    "evidence_snapshot": ("block", False),
    "evidence_deferred": ("bool", False),
    "repair_route": ("str", True),
    "attempts_budget": ("int", False),
    "recovered_before_repair": ("bool", False),
}
REPAIR_ATTEMPTS_BUDGET_DEFAULT = 2
# evidence_snapshot (T-4707) + the T-5678 free-text gate fields (threat_path,
# threat_justification, not_security_reason). Single source in the leaf module
# (T-5689) so ticket_purpose's parser applies the same legacy-indent rule.
BLOCK_SCALAR_FIELDS = _frontmatter_block.BLOCK_SCALAR_FIELDS

LIST_FIELDS = (
    "depends_on", "implements", "verifies", "supersedes", "informs",
    "ratifies", "receipts", "labels", "ratifiable_files",
)

# T-0465: `intent` frontmatter disambiguates target=operator tickets.
# action  — "the operator, do X" (operator action request; default for target=operator)
# ratify  — target=operator with ratifiable_files (per ORG-015)
# noodle  — "the operator's eye welcome; no urgent action" (design-noodle, parking-the-question)
# Not set for target != operator (the field is target=operator-specific surface).
INTENT_VOCAB = ("action", "ratify", "noodle")

# T-2613: `retest_status` — ORG-038 S3* independent-re-test-to-close structural
# gate. Only s3star_audit_runner.py's finding filer sets this today (via
# `file --retest-status`), but the vocabulary is owned by the schema, not by
# whichever caller happens to set it — same reasoning as INTENT_VOCAB above.
# pending    — awaiting independent re-verification (routes into re-test brief)
# verified   — an independent re-test recorded a PASS verdict (closed)
# overridden — an explicit non-verdict closure (closed; reason lives in the
#              transition log, not this field)
# A ticket with NO retest_status field is treated as pre-existing/closed —
# see tools/s3star_audit_runner.py find_open_retest_tickets for the read side.
RETEST_STATUS_VOCAB = ("pending", "verified", "overridden")

# T-3215: id space is allocated sequentially and crosses into five digits in
# roughly a year at the current rate — 4 was never a promise, so this widens
# to "4 or more". Filename-shaped (adds `.md$` + a capture group); the bare
# id-shape sibling is _TICKET_ID_SHAPE below, kept separate on purpose since
# a filename check and a bare-id check are not the same regex.
TICKET_FILENAME_RE = re.compile(r"T-(\d{4,})\.md$")

# ORG-088 Plane-A (T-3761): a `role:<R>` pool-routing target. SHAPE detector only —
# mirrors tools.ticket_purpose.ROLE_TARGET_RE byte-for-byte but is defined LOCALLY so
# the file/retarget WRITE path and the list/ready/prime READ path need not import
# ticket_purpose (which pulls in project_catalogue): the lint auto-retarget shells out
# to a minimal tools/ copy that carries neither. This regex decides only whether a
# target IS a role target; the role VOCABULARY authority is agent_role_map.known_role_ids
# (the single L3-derived source), never duplicated here.
_ROLE_TARGET_RE = re.compile(r"^role:[a-z][a-z0-9_]*$")

# ---------------------------------------------------------------------------
# Allocation lock (T-0786, amended T-0823)
# ---------------------------------------------------------------------------
# A blocking LOCK_EX flock serializes all concurrent writers on this machine
# (claude, claude-fallback, kino, claude-research) through the
# allocate→write phase of ticket creation.
# Single-machine assumption: fcntl.flock semantics are only reliable on a
# local filesystem; NFS is NOT supported.
#
# The lockfile lives in /tmp (sticky, world-writable dir) with mode 0o666.
# Under the host's fs.protected_regular=2 kernel hardening, an O_CREAT open
# of a regular file the opener does NOT own in a sticky world-writable dir is
# refused (EPERM).  To avoid this, the primary open path uses plain O_WRONLY
# (no O_CREAT) so non-owner uids can open the existing lockfile safely.
# O_CREAT is used only when the file is genuinely absent (ENOENT), wrapped in
# umask(0) so the created file lands at 0o666.  A create-race fallback handles
# the TOCTOU between the ENOENT return and the O_CREAT attempt: if another uid
# wins the race and creates the file first, the O_CREAT call triggers the same
# EPERM; we catch PermissionError (and FileExistsError) and fall back to a
# plain O_WRONLY open of the now-existing file.
#
# Env-overridable via PANTHEON_TICKET_ALLOC_LOCKFILE for test isolation.

_DEFAULT_ALLOC_LOCKFILE = "/tmp/pantheon_ticket_alloc.lock"


def _alloc_lockfile() -> str:
    """Return the flock path for allocation serialization.

    Env-overridable via PANTHEON_TICKET_ALLOC_LOCKFILE so tests can use
    isolated paths without clobbering the production lockfile.
    Default: /tmp/pantheon_ticket_alloc.lock.
    """
    return os.environ.get("PANTHEON_TICKET_ALLOC_LOCKFILE") or _DEFAULT_ALLOC_LOCKFILE


def _open_world_writable(path: str):  # type: ignore[return]
    """Open (or create) path with mode 0o666, bypassing the process umask.

    Primary path: open WITHOUT O_CREAT so that, under fs.protected_regular=2,
    a uid that does not own the file can still open it for write when it
    already exists.

    O_CREAT is used only when the file is genuinely absent (FileNotFoundError
    on the no-O_CREAT open).  The O_CREAT open is wrapped in umask(0) so the
    created file lands at mode 0o666 (world-writable) regardless of the
    process umask.

    Create-race fallback: if the file is absent when we try O_WRONLY but
    another uid creates it before our O_CREAT reaches the kernel, the O_CREAT
    call hits the same protected_regular=2 refusal (PermissionError) or a
    FileExistsError on some kernels.  Both are caught; we fall back to a plain
    O_WRONLY open of the now-existing file.  This fallback is still inside the
    umask(0) / umask-restore finally block — the umask is irrelevant for a
    no-create open but keeping it inside the finally is simpler than a
    separate try/finally.

    Returns an open file object.  Caller is responsible for closing it.
    """
    try:
        fd = os.open(path, os.O_WRONLY)          # existing → protected_regular-safe
    except FileNotFoundError:
        old_umask = os.umask(0)
        try:
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o666)
        except (FileExistsError, PermissionError):
            fd = os.open(path, os.O_WRONLY)      # lost create race → file exists now
        finally:
            os.umask(old_umask)
    return os.fdopen(fd, "w")


@contextlib.contextmanager
def _allocation_lock():
    """Context manager: hold a blocking LOCK_EX flock for the allocate→write phase.

    Callers must wrap both _next_ticket_id() AND new_path.write_text() inside
    this context so no other writer can scan the same max between our scan and
    our write.  The lock is released on exit.  T-5406: cmd_file takes it INSIDE
    git_mutation_lock (order is always git then alloc; never the reverse, which
    would be ABBA against close-noodle/close-action spawns).

    Usage::

        with _allocation_lock():
            tid = _next_ticket_id()
            ...build fields / text...
            new_path.write_text(text, encoding="utf-8")
        # alloc lock released here

    LOCK_EX is blocking (not LOCK_NB): writers queue for their turn rather
    than failing.  Single-machine assumption: fcntl.flock; NFS not supported.
    """
    lockfile = _alloc_lockfile()
    lock_fd = _open_world_writable(lockfile)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


# Process exit codes (T-4496). 0 = ok; 2 = argparse usage error (stdlib, before
# main()'s try-block); 1 and 3 are the two TicketError classes below. A wrapper
# (kino's "File new ticket") keys on the CODE, never on stderr text.
EXIT_FAULT = 1
EXIT_REFUSAL = 3


class TicketError(Exception):
    """A FAULT: the tool or its environment failed, not the caller's request.

    Git/IO failures, scope/HERMES-README guards, a non-main checkout, an
    unreadable vocabulary file, a ticket/README file the request did not author,
    or an internal invariant. Exits EXIT_FAULT (1) — a wrapper shows a 500.

    Raise `TicketRefusal` instead when the caller's own request is invalid (see
    below). Classification test: would the identical request, re-issued against
    a healthy tool and the CURRENT shared ticket state, refuse deterministically?
    Yes -> TicketRefusal. If the outcome depends on git, the filesystem, locks,
    the checkout branch, or file state the request did not author -> TicketError.
    tools/test_ticket_refusal_exit_codes.py pins the set of bare-TicketError
    raise sites, so a new site cannot silently default to the fault class.
    """

    exit_code = EXIT_FAULT


class TicketRefusal(TicketError):
    """A REFUSAL: the caller's request is invalid, so ticket.py declined it
    (T-4496). Exits EXIT_REFUSAL (3) — a wrapper shows a 400.

    Covers bad flag shapes/values, an unknown target/requester/ticket, a lineage
    or appetite gate, a state-mismatch, and a stale-view CONFLICT (another writer
    moved the ticket first: the remedy is the caller's — re-read, retry). Subclasses
    TicketError so every existing `except TicketError` still catches it, and the
    `error: ...` stderr line is identical for both classes."""

    exit_code = EXIT_REFUSAL


class AppetiteRequired(TicketRefusal):
    """T-3440 / ORG-089 §Decision.4e: `file` refused because an applicable bet
    (bet_ref present OR non-vault-ADR parent OR non-hygiene serves) was filed
    without an `appetite:` score. A TicketRefusal: main() surfaces it as the
    caller-refusal exit code."""


class AppetiteOverrunNotResolvable(TicketRefusal):
    """T-3440 / ORG-089 §Decision.4: `resolve` refused because the ticket is in
    `substate: appetite-overrun`. A re-decision verb (re-earn / drop /
    adjust-appetite) must run first — resolve stays terminal, never overloaded
    (D-P)."""


class SopStubNotFilled(TicketRefusal):
    """T-4680: `resolve` refused because the ticket is labelled `sop:<stem>`
    while sop/standard/<stem>.md is still a STUB that did not change in the
    ticket's arc (writing-sops-from-first-use.md step 3). Remedy: fill the
    stub's First-run section, cite the filling commit with --sop-updated, or
    waive with --sop-no-change '<why>'."""


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _actor() -> str:
    """Identity to record in transition-log entries.

    Resolution order: `$PANTHEON_ACTOR` env var, then `getpass.getuser()`.
    """
    explicit = os.environ.get("PANTHEON_ACTOR", "").strip()
    if explicit:
        return explicit
    return getpass.getuser()


# OS account → persona, for the one-active WIP cap (T-0877). A clone's OS user
# identifies its persona even when PANTHEON_ACTOR is unset.
#
# DERIVED from instance.yaml (T-2256). This literal was COMPLETE when it was
# repointed — T-2255 had just added the entry it was missing — and it is
# repointed anyway, because the defect this ticket closes is not "the list is
# wrong", it is "there are twenty lists". This one is the ticket's own worked
# example: `exocortex` was absent while the LATER ORG-043 seats were present, so
# `_claiming_persona()` fell through to `_resolve_persona()`, which hands back
# "exocortex" unchanged while that seat's tickets carry "archivist". The cap
# keyed on a value no ticket could ever match, one seat ran uncapped for eight
# days, and nothing surfaced it — a fail-open guard is silent by construction.
# Leaving a correct copy standing just means the next persona gets added by
# whoever happens to be standing in front of one of the other nineteen.
_OS_USER_PERSONA: dict[str, str] = _account_persona_map()


def _claiming_persona() -> str:
    """Resolve the claiming agent's canonical persona for the WIP cap.

    Maps the actor (`$PANTHEON_ACTOR` or OS user) → persona: OS account first
    (claude/claude-research/claude-fallback), else role-id/persona via
    `_resolve_persona()`. An unrecognized identity passes through unchanged,
    so the guard fails OPEN — it never blocks a claim it cannot attribute.
    """
    raw = _actor()
    if raw in _OS_USER_PERSONA:
        return _OS_USER_PERSONA[raw]
    return _resolve_persona(raw)


def _closer_display_name() -> str:
    """Who ran a close verb, for the prose of the ticket it spawns (T-4326).

    The persona behind the actor, capitalized: "the operator" when kino closes
    (it runs with PANTHEON_ACTOR=operator), "Orchestrator" from the claude account.
    An unrecognized identity is shown as-is, never attributed to the operator.
    """
    return _claiming_persona().capitalize()


def _now_iso() -> str:
    """UTC ISO 8601 timestamp, second precision, Z-suffixed."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_not_before(raw: str) -> str:
    """Validate and normalize a not_before value to full ISO 8601 UTC form.

    Accepts:
      - Bare date ``YYYY-MM-DD`` → normalized to ``YYYY-MM-DDT00:00:00Z``
      - Full datetime ``YYYY-MM-DDTHH:MM:SSZ`` → stored as-is

    Raises TicketError on anything else.
    """
    # Bare date: YYYY-MM-DD
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            datetime.fromisoformat(raw)
        except ValueError:
            raise TicketRefusal(f"not_before: invalid date: {raw!r}")
        return raw + "T00:00:00Z"
    # Full ISO 8601 UTC: YYYY-MM-DDTHH:MM:SSZ
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", raw):
        try:
            # Normalize the trailing 'Z' → '+00:00': datetime.fromisoformat only
            # accepts a bare 'Z' on Python 3.11+, but the documented bring-up
            # baseline is >=3.10 (T-1383 / ORG-A4). The regex above already
            # guarantees a valid shape; this keeps the validation portable.
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise TicketRefusal(f"not_before: invalid datetime: {raw!r}")
        return raw
    raise TicketRefusal(
        f"not_before: expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, got: {raw!r}"
    )


def _validate_ratifiable_path_part(path: str) -> str:
    """Validate the path portion of a ratifiable_files entry.

    Rules (P3 structural-invariant guard against directory traversal):
      - Must be non-empty.
      - Must be relative (no leading ``/``).
      - Must not start with ``./``.
      - Must not contain ``..`` segment.
      - Must not contain empty segments (double slashes or trailing slash).
    """
    if not path:
        raise TicketRefusal("ratifiable_files: path part must be non-empty")
    if path.startswith("/") or path.startswith("./"):
        raise TicketRefusal(
            f"ratifiable_files: path must be a clean relative path (no leading "
            f"'/' or './'), got: {path!r}"
        )
    parts = path.split("/")
    if any(p == ".." for p in parts):
        raise TicketRefusal(
            f"ratifiable_files: path must not contain '..' segments (no traversal), "
            f"got: {path!r}"
        )
    if any(p == "" for p in parts):
        raise TicketRefusal(
            f"ratifiable_files: path must not contain empty segments / double slashes, "
            f"got: {path!r}"
        )
    return path


NO_FILE_MIN_CHARS = 20  # T-5014: mirrors the --orphan justification minimum.


def _operator_attachment_gate(
    target: str,
    ratifiable_files: list[str],
    no_file: object,
    *,
    fix_prefix: str,
    existing_files: bool = False,
    existing_reason: bool = False,
) -> str | None:
    """T-5014 forced choice: a target=operator ticket (ANY intent) carries a file
    (``--ratifiable-file <repo>:<path>``) or an explicit ``--no-file "<reason>"``.

    Returns the stripped no-file reason to persist (None when files were given, or
    for non-operator targets, where --no-file is accepted and ignored so static
    callers with a dynamic --target keep working). Raises TicketRefusal otherwise.
    `existing_*` let cmd_retarget count attachments already in the frontmatter.
    `fix_prefix` is the verb-specific command stem for the refusal's fix line.
    """
    if target != "operator":
        return None
    reason = no_file.strip() if isinstance(no_file, str) and no_file.strip() else None
    if ratifiable_files and reason:
        raise TicketRefusal(
            "--ratifiable-file and --no-file are mutually exclusive: attach the "
            "file OR assert there is none, not both (T-5014)"
        )
    if reason is not None:
        if len(reason) < NO_FILE_MIN_CHARS:
            raise TicketRefusal(
                f"--no-file justification too short ({len(reason)} chars, "
                f"minimum {NO_FILE_MIN_CHARS}): {reason!r}"
            )
        return reason
    if ratifiable_files or existing_files or existing_reason:
        return None
    raise TicketRefusal(
        "refusing a target=operator ticket with no attachment: the operator cannot open "
        "a prose-only ask. Fix: commit the file it is about into a tracked repo, "
        f"then `{fix_prefix} --ratifiable-file <repo>:<path>` (repeatable); or, if "
        f"the ask genuinely has no file, `{fix_prefix} --no-file \"<reason, "
        f">={NO_FILE_MIN_CHARS} chars>\"` (T-5014)"
    )


def _check_ratifiable_resolve(entries: list[str]) -> None:
    """T-5014: each ratifiable_files entry must exist in its repo at file time.

    Reuses ticket_lint.ratifiable_path_status (lazy-imported: ticket_lint imports
    FROM this module). False (confirmed absent) refuses; None (foreign repo not
    cloned / unverifiable) warns and allows (fail-open, the T-0549 property).
    """
    from tools.ticket_lint import ratifiable_path_status

    for entry in entries:
        repo, _sep, rel_path = entry.partition(":")
        status = ratifiable_path_status(repo, rel_path, REPO_ROOT)
        if status is False:
            raise TicketRefusal(
                f"ratifiable_files entry {entry!r} does not exist in {repo!r}: "
                "the operator can only open a file that is committed in a tracked repo. "
                "Fix: commit the draft into the repo first (a path under /home or "
                "/tmp is not attachable), then re-run with the real <repo>:<path> "
                "(T-5014)"
            )
        if status is None:
            print(
                f"warning: could not verify ratifiable_files entry {entry!r} "
                f"(repo {repo!r} not resolvable locally); allowing (T-5014)",
                file=sys.stderr,
            )


# T-6071 / ORG-015 exclusion list: document classes that MUST NOT be a NEW
# ratifiable_files target. Enforced ONLY where a target is created or added
# (cmd_file, cmd_retarget, cmd_attach) via _refuse_excluded_ratifiable_class.
# _parse_ratifiable_files stays syntax-only and stored values are never
# re-checked, so the grandfathered tickets still transition; ticket_lint WARNs
# on them via the same classifier (single source, no drift).
_ORG015_EXCLUDED_CLASSES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("notes/", re.compile(r"^notes/")),
    ("ledger (jsonl)", re.compile(r"^ledger/.*\.jsonl$")),
    ("tickets/", re.compile(r"^tickets/(?:inbox|active|blocked|done)/T-\d+\.md$")),
    ("incidents/", re.compile(r"^incidents/")),
    ("bugs/", re.compile(r"^bugs/.+\.md$")),
    ("lessons/", re.compile(r"^lessons/.+\.md$")),
    ("knowledge/watch/reports/", re.compile(r"^knowledge/watch/reports/")),
    ("knowledge/episodic/", re.compile(r"^knowledge/episodic/")),
    ("README.md", re.compile(r"(?:^|/)README\.md$")),
    ("source code", re.compile(
        r"\.(?:py|ts|tsx|js|jsx|mjs|rs|go|sh|java|c|h|cpp|rb)$")),
)


def ratifiable_excluded_class(entry: str) -> str | None:
    """Return the ORG-015 excluded class name for a ``<repo>:<path>`` entry, or None."""
    path = entry.split(":", 1)[1] if ":" in entry else entry
    for name, rx in _ORG015_EXCLUDED_CLASSES:
        if rx.search(path):
            return name
    return None


def _refuse_excluded_ratifiable_class(entries: list[str]) -> None:
    """T-6071: refuse a NEW ratifiable_files target in an ORG-015 excluded class."""
    for entry in entries:
        cls = ratifiable_excluded_class(entry)
        if cls is not None:
            raise TicketRefusal(
                f"ratifiable_files entry {entry!r} is in an excluded class "
                f"({cls}): ORG-015 says this class is not a valid ratification "
                "target. Fix: promote the content to an allowed class (an ADR "
                "under architecture/adr/, rulebook/policies/, sop/standard/, "
                "sop/remediation/, or rulebook principles/directives) and attach "
                "that path instead (T-6071)"
            )


def _parse_ratifiable_files(entries: list[str]) -> list[str]:
    """Validate a ratifiable_files list of ``<repo>:<path>`` strings.

    Each entry must:
      - Contain exactly one ``:`` separator.
      - Have a repo prefix present in RATIFIABLE_REPO_ALLOWLIST.
      - Have a path part passing _validate_ratifiable_path_part rules.

    Raises TicketError on any rule violation.
    """
    validated: list[str] = []
    for entry in entries:
        if ":" not in entry:
            raise TicketRefusal(
                f"ratifiable_files: each entry must be '<repo>:<path>', "
                f"missing ':' in: {entry!r}"
            )
        repo, path = entry.split(":", 1)
        if repo not in RATIFIABLE_REPO_ALLOWLIST:
            raise TicketRefusal(
                f"ratifiable_files: repo {repo!r} not in allowlist "
                f"{RATIFIABLE_REPO_ALLOWLIST}"
            )
        _validate_ratifiable_path_part(path)
        validated.append(entry)
    return validated


# T-2841 / ORG-065 BT1: typed-edge target shape — a ticket id (T-NNNN) or an
# ADR obligation/clause id (ADR-NNN#E<k> / ORG-NNN#E<k>.C<m>, matching the
# `_ADR_ID_RE`-style prefix already used elsewhere in the repo, e.g.
# ticket_grooming.py's `_ADR_ID_RE = r"^(ADR|ORG)-(\d+)"`). SHAPE only — this
# is enforced at every insertion point (cmd_file); whether the target actually
# RESOLVES in the corpus is a separate, WARN-tier, lint-only check
# (ticket_lint.detect_typed_graph_issues) per resolved-decision 1: an unknown
# target is a corpus-wide WARN, never a refusal here.
#
# KNOWN BT1 SIMPLIFICATION for `verifies`: the ADR's strict model (§3.2,
# §3.5.2) has `verifies` point commit/test/evidence -> obligation, and §3.5.2
# is explicit that "a verifies edge must point at an actual test/run
# RECEIPT... NOT merely a commit link". This shape accepts a plain ticket id
# (T-NNNN) as a `verifies` target too — same as `implements` — because BT1
# has no commit/test/evidence NODE IDENTITY yet (NODE_KINDS in ticket_lint.py
# registers those kinds but nothing constructs them; that's downstream work).
# A ticket-id `verifies` target today means "this ticket [stands in for /
# tracks] the evidence", not a real receipt — BT2's closure validation (§3.3)
# MUST NOT treat a bare ticket-id `verifies` edge as satisfying the
# receipt/evidence-authority requirements (§3.5.2/§3.5.4) without a real
# commit/test/evidence node behind it. Do not inherit this as settled
# semantics.
_EDGE_TARGET_RE = re.compile(r"^(?:T-\d{4,}|(?:ADR|ORG)-\d+#E\d+(?:\.C\d+)?)$")  # T-3215


def _parse_edge_targets(field_name: str, entries: list[str]) -> list[str]:
    """Validate a typed-edge field's (implements/verifies/supersedes/informs)
    list of target strings. Each entry must match _EDGE_TARGET_RE. Raises
    TicketError on a malformed entry; never checks existence (see above)."""
    validated: list[str] = []
    for entry in entries:
        e = entry.strip()
        if not e:
            raise TicketRefusal(f"{field_name}: empty target entry")
        if not _EDGE_TARGET_RE.fullmatch(e):
            raise TicketRefusal(
                f"{field_name}: {e!r} is not a valid edge target — expected a "
                f"ticket id (T-NNNN) or an ADR obligation/clause id "
                f"(ADR-NNN#E<k> / ORG-NNN#E<k>.C<m>)"
            )
        validated.append(e)
    return validated


# T-2842 / ORG-065 BT2: shape validation for the `receipts:` field's
# `<clause>=<receipt-id>=<provenance>` entries. Delegates the actual shape
# check to tools.ticket_lint.decode_receipt (lazy-imported, same
# ticket<->ticket_lint cycle-avoidance pattern as _reject_if_cycle below) so
# the clause-id / receipt-id / provenance shapes have exactly one definition
# each, not two independently-maintained regexes for the WHOLE entry (the
# _CLAUSE_ID_RE half is still independently duplicated in ticket_lint.py —
# see that module's note — but the receipt-id/provenance shapes are NOT
# duplicated here; this function reuses decode_receipt wholesale instead of
# re-deriving those two).
def _parse_receipt_entries(field_name: str, entries: list[str]) -> list[str]:
    """Validate a `receipts:`-shaped field's list of
    `<clause>=<receipt-id>=<provenance>` strings (T-2842 / ORG-065 BT2).
    Raises TicketError on a malformed entry; returns entries unchanged
    (encoded strings — same round-trip-as-is shape as _parse_edge_targets)."""
    from tools.ticket_lint import decode_receipt

    validated: list[str] = []
    for entry in entries:
        e = entry.strip()
        if not e:
            raise TicketRefusal(f"{field_name}: empty receipt entry")
        if decode_receipt(e) is None:
            raise TicketRefusal(
                f"{field_name}: {e!r} is not a valid receipt — expected "
                f"<clause>=<receipt-id>=<provenance>, e.g. "
                f"ADR-2842#E1.C1=ci-run:12345=ci (provenance one of "
                f"ci/reviewer/self; receipt-id namespaced "
                f"ci-run:/test-result:/observable:)"
            )
        validated.append(e)
    return validated


# ---------------------------------------------------------------------------
# T-3440 / ORG-089 §Decision.4e: file-time appetite applicability.
# ---------------------------------------------------------------------------

# ORG-089 landing date — the legacy grandfather boundary (D-H / DECISION-10).
# Tickets `created` before this are legacy (medium-default, never refused). At
# file-time `created` is always "now", so this branch effectively fires only
# for pre-landing corpus reads, not genuinely new files (noted, DECISION-4/Q2).
_ORG089_LANDING = datetime(2026, 9, 6, tzinfo=timezone.utc)


def _created_is_legacy(created_iso: str | None) -> bool:
    if not created_iso:
        return False
    try:
        dt = datetime.fromisoformat(str(created_iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False  # unparseable → not legacy (fail toward applicability)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < _ORG089_LANDING


def _is_hygiene(fields: dict) -> bool:
    """Hygiene / goal-neutral-maintenance class — routed to the medium-default,
    never refused (D-H). Three equivalent fleet signals of goal-neutral work:
      - a hygiene label (hygiene/chore/cleanup/noodle/noop),
      - the `goal-neutral-maintenance` serves marker, OR
      - a `maintenance:` nearest_intent_ref kind (ORG-054 typed purpose).

    The maintenance-kind intent ref is the machine-readable form of the same
    goal-neutral category (a maintenance ref points at a logical/path asset, not
    a goal/bet). Every AUTOMATED_FILER_EXPECTATIONS filer files with a
    `maintenance:<...>` ref (knowledge_sweep, watch_runner, adr_reeval_walk,
    mirror_drift_sweep, s3star_audit_runner, soak_filer, weekly_ticket_summary,
    grand_project_program) and a descriptive (non-`goal-neutral-maintenance`)
    serves string, so this arm is what keeps automated filing in the
    medium-default branch without threading appetite into the filers
    (T-3440 Q3-guard; DECISION-12)."""
    from tools.ticket_purpose import (
        HYGIENE_LABELS, GOAL_NEUTRAL_SERVES,
        parse_intent_ref, PurposeError, PurposeRefKind,
    )

    labels = set(fields.get("labels") or [])
    serves = (fields.get("serves") or "").strip()
    if bool(labels & HYGIENE_LABELS) or serves == GOAL_NEUTRAL_SERVES:
        return True
    nir = fields.get("nearest_intent_ref")
    if isinstance(nir, str) and nir.strip():
        try:
            if parse_intent_ref(nir).kind is PurposeRefKind.MAINTENANCE:
                return True
        except PurposeError:
            pass  # unparseable ref → not a hygiene signal (fail toward applicability)
    return False


def _references_non_vault_adr(fields: dict) -> bool:
    """True iff any lineage edge names a NON-VAULT (agent-authored) ADR — the
    '§4e non-vault-ADR parent' trigger. Scans nearest_intent_ref + the typed
    obligation edges for an ORG-NNN / ADR-NNN token."""
    from tools.ticket_purpose import ADR_ID_RE, is_vault_adr

    candidates: list[str] = []
    nir = fields.get("nearest_intent_ref")
    if isinstance(nir, str):
        candidates.append(nir)
    # T-3476: `ratifies` is a real ORG-089 §Decision.4e "bet" trigger too — a
    # ratify-class ticket citing a non-vault ADR is exactly as consequential
    # as one citing implements/verifies/supersedes.
    for key in ("implements", "verifies", "supersedes", "ratifies"):
        val = fields.get(key)
        if isinstance(val, list):
            candidates.extend(str(v) for v in val)
        elif isinstance(val, str):
            candidates.append(val)
    for cand in candidates:
        for adr_id in ADR_ID_RE.findall(cand):
            if not is_vault_adr(adr_id):
                return True
    return False


def _appetite_applicable(fields: dict, created_iso: str | None) -> bool:
    """§Decision.4e applicability: True (appetite REQUIRED) iff this is a 'bet'
    ticket AND it is neither hygiene nor legacy.

    bet := bet_ref present OR non-vault-ADR parent OR non-hygiene serves.
    (Hygiene + legacy are excluded first, so 'non-hygiene serves' reduces to
    'serves is set' here.)"""
    if _is_hygiene(fields) or _created_is_legacy(created_iso):
        return False
    serves = (fields.get("serves") or "").strip()
    return (
        bool((fields.get("bet_ref") or "").strip())
        or _references_non_vault_adr(fields)
        or bool(serves)
    )


def _apply_appetite_at_file_time(fields: dict, raw_appetite, created_iso: str | None) -> None:
    """Resolve the appetite field at file-time (T-3440 Scope A).

    - appetite supplied → normalize to int and set.
    - absent + hygiene/legacy → stamp the medium default (5) so the field is
      present going forward (D-H / D-O / DECISION-4).
    - absent + applicable bet → raise AppetiteRequired.
    - absent + below-the-line non-bet → leave absent.
    Fail-fast (raises before any mutation)."""
    from tools.ticket_purpose import (
        normalize_appetite,
        AppetiteError,
        APPETITE_MEDIUM_DEFAULT,
    )

    try:
        normalized = normalize_appetite(raw_appetite)
    except AppetiteError as exc:
        raise TicketRefusal(str(exc)) from exc
    if normalized is not None:
        fields["appetite"] = normalized
        return
    if _is_hygiene(fields) or _created_is_legacy(created_iso):
        fields["appetite"] = APPETITE_MEDIUM_DEFAULT
        return
    if _appetite_applicable(fields, created_iso):
        raise AppetiteRequired(
            "appetite required: this bet (bet_ref / non-vault-ADR parent / "
            "non-hygiene serves) must carry an appetite score. Pass "
            "--appetite <1-10> (per ORG-089 §Decision.4 / RECORD-mode). Any "
            "--serves other than goal-neutral-maintenance — 'operator-ad-hoc' "
            "included — is a bet. Exempt (medium-default 5 is stamped): "
            "--serves goal-neutral-maintenance, a hygiene label, or "
            "--nearest-intent-ref maintenance:<asset>."
        )


# T-2842 / ORG-065 BT2 (§3.3): closure-validation gate mode. warn (default)
# NAMES violations without refusing the transition; gate REFUSES. Mirrors the
# existing PANTHEON_MAX_ACTIVE_TICKETS env-override precedent (test
# isolation without a permanent production behavior flip). An unset or
# unrecognized value fails SAFE to "warn" — a typo must never silently start
# gating production closures.
_CLOSURE_GATE_MODE_ENV = "PANTHEON_CLOSURE_GATE_MODE"
_CLOSURE_GATE_MODES = ("warn", "gate")


def _closure_gate_mode() -> str:
    raw = (os.environ.get(_CLOSURE_GATE_MODE_ENV) or "gate").strip().lower()
    return raw if raw in _CLOSURE_GATE_MODES else "warn"


def _gate_echo_adr_dir() -> Path:
    """The echo-prismatic ADR directory this actor's own clone resolves to
    (T-3578 / ORG-065 closure-gate cross-repo fix). ACTOR-RELATIVE by design:
    the closure-gate runs inside the resolving process, so it must read that
    process's OWN echo clone —
      - artisan/suborch2 -> /home/suborch2/echo-prismatic/adr (readable, carries
        the merged echo ADRs incl. ADR-216#E4);
      - orchestrator/claude   -> /home/claude/echo-prismatic/adr (same path the old
        hardcode `_ECHO_PRISMATIC_ADR_DIR` named).
    The `ECHO_PRISMATIC_ADR_DIR` env var overrides (used by the T-3578 tests to
    point at a fixture echo-ADR dir); otherwise defaults to
    `$HOME/echo-prismatic/adr`.

    Distinct from `ticket_lint._ECHO_PRISMATIC_ADR_DIR` (which stays hardcoded
    under /home/claude for the recovery-side echo_corpus_lint.py, run as recovery with
    native /home/claude read access) — do NOT collapse the two (T-3578).
    """
    override = os.environ.get("ECHO_PRISMATIC_ADR_DIR")
    if override:
        return Path(override)
    return Path.home() / "echo-prismatic" / "adr"


def _check_closure_gate(tid: str, fields: dict) -> None:
    """Run the ORG-065 §3.3 closure-validation gate against `fields` (the
    ticket's frontmatter as it will be at the moment of closure — see
    _do_transition's call site for the pre-mv timing). No-op when there are
    no findings (the overwhelming majority of tickets, which never declared
    an ADR obligation via `implements`).

    gate mode: raises TicketError naming every finding (refuses the
    transition; caller must invoke this BEFORE any mutation so the refusal
    leaves no trace, mirroring _reject_if_cycle's fail-fast shape).

    warn mode (default): prints one stderr line per finding in a stable,
    greppable format — `[closure-gate] <tid> <kind>: <clause-id> ... —
    '<element text>'` (each ClosureFinding.detail already embeds the clause/
    obligation id and its element/clause TEXT — see evaluate_closure_fields
    — so coverage is checkable against the obligation body, not a label,
    per §3.3's own framing). The corpus migration walk and the flip-leg
    live-fire smoke both parse the `[closure-gate] <tid> <kind>:` prefix;
    do not change it without updating both.
    """
    from tools.ticket_lint import (
        _CLAUSE_ID_RE,
        _adr_dir_scannable,
        evaluate_closure_fields,
    )

    # T-3578 / ORG-065: resolve obligations against BOTH the pantheon ORG-*
    # corpus AND this actor's echo-prismatic clone (actor-relative, so a
    # cross-repo echo obligation like ADR-216#E4 resolves — see
    # _gate_echo_adr_dir). The echo dir is unioned only when it is scannable by
    # this actor; a clone-less actor degrades to a warn-fallback below rather
    # than spuriously blocking on an echo obligation it structurally cannot
    # validate.
    adr_dirs = [REPO_ROOT / "architecture" / "adr"]
    echo_dir = _gate_echo_adr_dir()
    echo_scannable = _adr_dir_scannable(echo_dir)
    if echo_scannable:
        adr_dirs.append(echo_dir)

    findings = evaluate_closure_fields(fields, adr_dirs)
    if not findings:
        return

    # WARN-FALLBACK (T-3578): when this actor has no readable echo clone AND the
    # ticket carries an echo-namespace obligation (ADR-*), the echo obligation(s)
    # are UNVALIDATED this resolve — we cannot distinguish a genuinely-dangling
    # echo id from one that lives in the unreadable clone. Degrade to a LOUD warn
    # (do not raise) so a clone-less actor is not permanently blocked, while
    # PANTHEON (ORG-*) obligations continue to gate fully: if the findings
    # include ANY non-echo (ORG-*) obligation defect, we still fall through to
    # the gate-mode raise below. Only when EVERY finding is echo-namespace do we
    # warn-and-return. For artisan/orchestrator (readable clones) echo_scannable is True,
    # so this branch never triggers — echo obligations validate normally.
    if not echo_scannable:
        implements = fields.get("implements") or []
        echo_obligations = [
            str(t) for t in implements
            if _CLAUSE_ID_RE.fullmatch(str(t)) and str(t).startswith("ADR-")
        ]
        if echo_obligations:
            def _finding_is_echo(f) -> bool:
                # An unreadable echo clone can only SPURIOUSLY produce a
                # `dangling_obligation` (the id resolves in NO scannable corpus).
                # A resolved-but-unsatisfied finding (`unsatisfied_clause` /
                # `evidence_authority_denied`) means the obligation DID resolve —
                # necessarily in the scannable pantheon corpus, since the echo
                # dir is unreadable here — so it is a REAL pantheon finding that
                # MUST keep gating, even for an ADR-NNN id. (pantheon-config's
                # own corpus uses ADR-NNN ids too, e.g. ADR-2842 — the id prefix
                # alone does NOT mean echo-resident; only a dangle on such an id,
                # with echo unreadable, is plausibly an echo obligation we could
                # not see.) `malformed_receipt` has no id prefix and keeps
                # gating. This is the T-3578 rebound fix: keying on the prefix
                # ALONE wrongly demoted resolved pantheon ADR-NNN obligations to
                # warn on a clone-less actor (CI runner).
                return (
                    f.kind == "dangling_obligation"
                    and f.detail.startswith("ADR-")
                )

            non_echo_findings = [f for f in findings if not _finding_is_echo(f)]
            if not non_echo_findings:
                print(
                    f"[closure-gate] {tid} echo_adr_unreadable: echo ADR dir "
                    f"{echo_dir} is UNREADABLE by this actor "
                    f"({getpass.getuser()}); cross-repo echo obligation(s) "
                    f"{', '.join(echo_obligations)} are UNVALIDATED this resolve "
                    f"— resolve from an account/clone that can read "
                    f"echo-prismatic (ref T-3578).",
                    file=sys.stderr,
                )
                return
            # Mixed ORG-* + echo obligations: keep only the non-echo findings so
            # the gate blocks on the pantheon defect (integrity preserved) while
            # not spuriously blocking on the unvalidatable echo obligation.
            findings = non_echo_findings

    if _closure_gate_mode() == "gate":
        lines = [f"  - {f.kind}: {f.detail}" for f in findings]
        raise TicketRefusal(
            f"closure-gate (ORG-065 §3.3): {tid} has unsatisfied obligation(s):\n"
            + "\n".join(lines)
        )

    for f in findings:
        print(f"[closure-gate] {tid} {f.kind}: {f.detail}", file=sys.stderr)


# T-2841 / ORG-065 BT1 (§3.6): the gating/structural edge set whose acyclicity
# is a BINDING invariant, even in this warn-only phase. `informs` is
# deliberately excluded here — advisory-only, exempt from the DAG check.
# `parent_ticket` is scalar (not a list) so callers handle it separately from
# the list-typed fields in this tuple. T-3476: `ratifies` joins this list —
# same unioned acyclicity domain as implements/verifies/supersedes (see
# ticket_lint.ACYCLICITY_DOMAINS; no falsifying wet-run evidence exists for
# giving it its own exempt domain the way parent_of has).
_GATING_LIST_FIELDS = ("depends_on", "implements", "verifies", "supersedes", "ratifies")


def _reject_if_cycle(tid: str, candidate_edges: dict[str, list[str]]) -> None:
    """Raise TicketError, naming the offending path, if any of
    `candidate_edges` (field_name -> raw target strings this ticket would
    newly point at) would close a cycle against the EXISTING on-disk corpus
    graph — checked BEFORE the edit lands, so the graph never accrues a bad
    edge (ORG-065 §3.6: "reject a cycle-creating edge at insertion and name
    the offending path"). Callers must only pass gating/structural fields
    (depends_on, parent_ticket, implements, verifies, supersedes, ratifies) —
    `informs` is acyclicity-exempt and must never be checked here.

    Checked PER ACYCLICITY DOMAIN (ticket_lint.ACYCLICITY_DOMAINS), never on
    one graph unioning every edge type — a live corpus wet-run found that
    union false-positives on a widespread, legitimate idiom (a parent ticket
    depends_on its own child). See the module note atop ticket_lint.py's
    typed-edge-graph section for the full rationale.

    Lazy-imports tools.ticket_lint (the generalized graph-builder) to avoid
    the ticket<->ticket_lint import cycle — same pattern cmd_ready already
    uses for its dep-graph deadlock lookup.
    """
    from tools.ticket_lint import ACYCLICITY_DOMAINS, build_typed_edge_graph, edge_node_id, find_path

    outbound: list[tuple[str, str]] = [
        (field_name, raw)
        for field_name, targets in candidate_edges.items()
        for raw in targets
    ]
    if not outbound:
        return

    def _domain_for(field_name: str) -> tuple[str, ...]:
        key = "parent_of" if field_name == "parent_ticket" else field_name
        for domain in ACYCLICITY_DOMAINS:
            if key in domain:
                return domain
        # Unreachable for the fields this function's callers pass, but a
        # safe fallback (a self-contained singleton domain) rather than a
        # KeyError if a future caller adds a field this dict doesn't know.
        return (key,)

    src = ("ticket", tid)
    graph_cache: dict[tuple[str, ...], dict] = {}
    for field_name, raw in outbound:
        domain = _domain_for(field_name)
        if domain not in graph_cache:
            graph_cache[domain] = build_typed_edge_graph(TICKETS_ROOT, domain)
        graph = graph_cache[domain]
        tgt = edge_node_id(raw)
        if tgt == src:
            raise TicketRefusal(
                f"{field_name}: {raw!r} would create a self-referential edge on "
                f"{tid} — the graph must stay acyclic (ORG-065 §3.6)"
            )
        path = find_path(graph, tgt, src)
        if path is not None:
            chain = " -> ".join(f"{kind}:{ident}" for kind, ident in path)
            raise TicketRefusal(
                f"{field_name}: {tid} -> {raw} would close a cycle in domain "
                f"{domain} — the graph must stay acyclic (ORG-065 §3.6): "
                f"ticket:{tid} -> {chain} -> ticket:{tid}"
            )


# ---------------------------------------------------------------------------
# Front-matter parser / serializer.
# ---------------------------------------------------------------------------


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split a ticket file's text into (front_matter_fields, body).

    Front matter is delimited by leading `---\\n` and a closing `---\\n`. Each
    line within is `key: value`. Values are scalars, `null`, single-line
    flow-style lists `[a, b, c]`, or (T-4707/T-5689) a YAML `|` / `>` block
    scalar on any key, read by tools/_frontmatter_block. No nested mappings.
    """
    if not text.startswith("---\n"):
        raise TicketError("front matter: missing opening `---` delimiter")
    rest = text[4:]
    end = rest.find("\n---\n")
    if end == -1:
        # Allow trailing `---` with no newline — rare hand-edit case.
        if rest.endswith("\n---"):
            fm_block = rest[: -len("\n---")]
            body = ""
        else:
            raise TicketError("front matter: missing closing `---` delimiter")
    else:
        fm_block = rest[:end]
        body = rest[end + len("\n---\n") :]

    fields: dict = {}
    # T-5694: LF is the ticket format's only source-line delimiter.  Python's
    # splitlines() also consumes CR, VT, FF, the record separators, NEL, LS and
    # PS; those are legitimate value bytes and must remain inside a scalar.
    fm_lines = fm_block.split("\n")
    idx = 0
    while idx < len(fm_lines):
        raw_line = fm_lines[idx]
        idx += 1
        line = raw_line.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^([a-z_][a-z0-9_]*):\s*(.*)$", line)
        if not m:
            raise TicketError(f"front matter: cannot parse line: {raw_line!r}")
        key = m.group(1)
        raw_val = m.group(2).strip()
        if _frontmatter_block.parse_block_header(raw_val) is not None:
            # T-4707 / T-5689: a YAML `|` / `>` block scalar (any key, any
            # chomping/indentation indicator) -- tools/_frontmatter_block is the
            # ONE reader, shared with tools/ticket_purpose.py.
            value, idx = _frontmatter_block.consume_block(fm_lines, idx, raw_val, key=key)
            if value is not None:
                fields[key] = value
                continue
            if key in BLOCK_SCALAR_FIELDS and raw_val == "|":
                # T-4707 back-compat: an empty `|` block on a block field is "".
                fields[key] = ""
                continue
            # Back-compat: a bare `serves: |` with no indented content stays the
            # plain scalar "|" (pinned by test_ticket_repair_fields).
        fields[key] = _parse_value(key, raw_val)

    for req in REQUIRED_FRONT_MATTER:
        if req not in fields:
            raise TicketError(f"front matter: missing required field `{req}`")

    # Body must lead with a blank line; strip exactly one if present so callers
    # work against the body content directly.
    if body.startswith("\n"):
        body = body[1:]
    # T-4760: no process-wide snapshot is recorded here any more. A writer that
    # will mutate `fields` takes `_fm_snapshot(fields)` itself and hands it to
    # _build_ticket_text(old_fields=...) — see _check_frontmatter_registry.
    return fields, body


def _parse_value(key: str, raw: str):
    if raw == "" or raw.lower() == "null":
        return None
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if inner == "":
            return []
        items = _tokenize_flow_list(inner)
        # Reject empty tokens (e.g. `[a, ,b]`) — clearer to refuse than coerce.
        if any(item == "" for item in items):
            raise TicketError(f"front matter: empty list element in `{key}`")
        return items
    return raw


def _tokenize_flow_list(inner: str) -> list[str]:
    """Split a YAML flow-list interior on commas, respecting double-quoted spans.

    Items may be bare or double-quoted. Inside a double-quoted span, commas
    are not treated as separators. Backslash-escaped characters (``\\"`` and
    ``\\\\``) are handled so that ``\\"`` does not close the quoted span.
    After splitting, quoted tokens have their surrounding quotes stripped and
    ``\\"`` / ``\\\\`` escape sequences unescaped. Bare tokens are stripped of
    surrounding whitespace only.
    """
    tokens: list[str] = []
    current: list[str] = []
    in_quoted = False
    i = 0
    while i < len(inner):
        ch = inner[i]
        if in_quoted:
            if ch == "\\":
                # Consume the backslash and the next character as a unit.
                current.append(ch)
                i += 1
                if i < len(inner):
                    current.append(inner[i])
                    i += 1
                continue
            elif ch == '"':
                # Closing double-quote — end the quoted span.
                current.append(ch)
                in_quoted = False
                i += 1
                continue
            else:
                current.append(ch)
                i += 1
                continue
        else:
            if ch == '"':
                # Opening double-quote — begin a quoted span.
                current.append(ch)
                in_quoted = True
                i += 1
                continue
            elif ch == ",":
                tokens.append("".join(current))
                current = []
                i += 1
                continue
            else:
                current.append(ch)
                i += 1
                continue
    tokens.append("".join(current))

    result: list[str] = []
    for token in tokens:
        token = token.strip()
        if token.startswith('"') and token.endswith('"') and len(token) >= 2:
            # Strip surrounding quotes and unescape.
            unquoted = token[1:-1]
            unquoted = unquoted.replace('\\"', '"').replace("\\\\", "\\")
            result.append(unquoted)
        else:
            result.append(token)
    return result


def serialize_front_matter(fields: dict) -> str:
    """Emit front matter in `FRONT_MATTER_FIELD_ORDER`.

    Required fields always emit; optional fields emit only when present in
    `fields`. None for optional present fields renders as `null`. Lists render
    as flow-style `[a, b, c]`.
    """
    for req in REQUIRED_FRONT_MATTER:
        if req not in fields:
            raise TicketError(f"front matter: missing required field `{req}` for serialization")

    lines: list[str] = ["---"]
    for key in FRONT_MATTER_FIELD_ORDER:
        if key not in fields:
            continue
        lines.append(f"{key}: {_render_value(key, fields[key])}")
    # Any unknown keys (forward-compat) — emit at end in insertion order.
    for key in fields:
        if key in FRONT_MATTER_FIELD_ORDER:
            continue
        lines.append(f"{key}: {_render_value(key, fields[key])}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _render_value(key: str, val) -> str:
    if val is None:
        return "null"
    if key in BLOCK_SCALAR_FIELDS and isinstance(val, str):
        if _frontmatter_block.plain_scalar_safe(val):
            # T-5689 (orchestrator): a single-line value is written as a plain scalar,
            # the same unquoted form as every other single-line field, so a seat
            # still on pre-T-5689 readers (which choke on `threat_path: |`) can
            # parse every --threat-chain filing.
            return val
        # T-4707: YAML `|` block scalar -- each content line indented two spaces,
        # blank lines kept blank, always clip. Parsed back by parse_front_matter.
        return _frontmatter_block.render_block(val, always_clip=True)
    if isinstance(val, str) and val.splitlines() != [val]:
        # T-5689/T-5694: any other value containing a source-line separator
        # re-renders as a `|` block with the chomping that reads back to the
        # same value.  splitlines() is deliberately the detector here: besides
        # LF it catches CR, VT, FF, the record separators, NEL, LS and PS.  The
        # readers themselves split only on LF, so the other bytes stay data.
        return _frontmatter_block.render_block(val)
    if isinstance(val, list):
        return "[" + ", ".join(_quote_flow_item(str(item)) for item in val) + "]"
    return str(val)


# YAML flow-style special characters that require an item to be double-quoted.
# Comma is the primary trigger (it is the item separator). The bracket and
# brace chars are YAML flow-collection delimiters. Double-quote itself must
# be quoted so the escape sequence renders correctly. Bare colons (without a
# trailing space) are valid in YAML plain scalars and are NOT included here —
# `repo:path` values in ratifiable_files must NOT be force-quoted on colons
# alone, as that would churn every existing ticket.
_FLOW_SPECIAL = frozenset(',[]{}\"')


def _quote_flow_item(s: str) -> str:
    """Return *s* as a YAML flow-style scalar token.

    If *s* contains any YAML-flow-special character or has leading/trailing
    whitespace, it is wrapped in double quotes with internal backslashes and
    double-quotes escaped. Otherwise it is returned bare (byte-identical to
    the pre-fix output for items that never needed quoting).
    """
    needs_quoting = s != s.strip() or any(ch in _FLOW_SPECIAL for ch in s)
    if not needs_quoting:
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# Ticket type + locator.
# ---------------------------------------------------------------------------


@dataclass
class Ticket:
    path: Path
    fields: dict
    body: str
    title: str = ""
    state_dir: str = field(init=False)

    def __post_init__(self) -> None:
        self.state_dir = self.path.parent.name


def parse_ticket(path: Path) -> Ticket:
    text = path.read_text(encoding="utf-8")
    fields, body = parse_front_matter(text)
    title = _extract_title(body)
    return Ticket(path=path, fields=fields, body=body, title=title)


def _extract_title(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _write_new_ticket_exclusive(path: Path, text: str) -> None:
    """Create *path* with *text*, refusing to touch an existing file (T-4885).

    A freshly allocated ticket id names a path that must not exist. If one does
    (an upstream same-id file landed between allocation and write), a plain
    overwrite silently destroys another agent's ticket and the commit becomes a
    MODIFICATION that lands as a fast-forward, bypassing the T-0786 re-allocation.
    ``open(path, "x")`` is O_CREAT|O_EXCL: it raises FileExistsError and leaves
    the foreign file's bytes untouched; the caller re-allocates.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as fh:
        fh.write(text)


def _next_ticket_id() -> str:
    """Return the next available ticket ID as T-NNNN.

    MUST be called from within an ``_allocation_lock()`` context so the
    scan and the subsequent file write are atomic across concurrent writers.
    This function does NOT acquire the lock itself — see _allocation_lock().

    Side effect: runs `git fetch origin main` to refresh the remote-tracking
    ref before scanning. Raises TicketError if fetch fails — proceeding with
    a stale local view recreates the duplicate-ID race (T-0068). The fetch
    here is redundant with _commit_and_push's pre-push pull, but the
    discipline window is at allocation time, not push time.

    Scans both the local working tree (tickets/) AND origin/main:tickets/ via
    `git ls-tree -r --name-only`, takes max(local_max, origin_max), returns
    that + 1.
    """
    # Step 1: fetch to refresh remote-tracking ref. Fail-loud on error.
    res = _git("fetch", "origin", "main")
    if res.returncode != 0:
        raise TicketError(
            f"git fetch failed (cannot safely allocate ticket ID): "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )

    # Step 2: scan local working tree.
    local_max = 0
    for state in STATES:
        d = TICKETS_ROOT / state
        if not d.exists():
            continue
        for entry in d.iterdir():
            if not entry.is_file():
                continue
            m = TICKET_FILENAME_RE.fullmatch(entry.name)
            if not m:
                continue
            n = int(m.group(1))
            if n > local_max:
                local_max = n

    # Step 3: scan origin/main:tickets/ via ls-tree. Non-zero return (e.g.
    # fresh clone where origin/main has no commits yet) is silently treated
    # as origin_max=0; local_max wins, and T-0001 is correct for that case.
    origin_max = 0
    res = _git("ls-tree", "-r", "--name-only", "origin/main", "tickets/")
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            # Each line is a repo-relative path: "tickets/<state>/T-NNNN.md"
            fname = line.split("/")[-1]
            m = TICKET_FILENAME_RE.fullmatch(fname)
            if not m:
                continue
            n = int(m.group(1))
            if n > origin_max:
                origin_max = n

    return f"T-{max(local_max, origin_max) + 1:04d}"


# T-3215: canonical bare-ticket-id SHAPE — the single source for every place
# in this module that validates "is this string a well-formed ticket id"
# (existence is a separate question — see _ticket_exists). Four digits was
# never a promise; ids allocate sequentially and the id space crosses into
# five digits in roughly a year at the current rate, so every site sharing
# this shape widens together instead of drifting independently. Sibling of
# TICKET_FILENAME_RE (above), which is FILENAME-shaped (adds `.md$` + a
# capture group) and stays separate on purpose — not the same regex despite
# sharing this digit shape.
_TICKET_ID_SHAPE = r"T-\d{4,}"
_TICKET_ID_FULL_RE = re.compile(_TICKET_ID_SHAPE)


def _require_ticket_id_shape(flag: str, value: str) -> None:
    """Refuse a caller-supplied ticket id that is not shaped T-NNNN (T-3215 shape).

    Shape only — existence is a separate question. Shared by `file`'s
    --parent-ticket and each --depends-on element (T-4499): a gate on a value
    that can never resolve ('banana', '-x') would sit forever, since the ORG-013
    cascade only fires when a REAL ticket resolves."""
    if not _TICKET_ID_FULL_RE.fullmatch(value):
        raise TicketRefusal(f"{flag} value {value!r} does not match the T-NNNN format")


def _find_ticket(tid: str) -> Path:
    if not _TICKET_ID_FULL_RE.fullmatch(tid):
        raise TicketRefusal(f"invalid ticket id: {tid!r} (expected T-NNNN)")
    fname = f"{tid}.md"
    for state in STATES:
        candidate = TICKETS_ROOT / state / fname
        if candidate.exists():
            return candidate
    raise TicketRefusal(f"ticket not found: {tid}")


def _ticket_exists(tid: str) -> bool:
    """Non-raising existence check across all states (inbox/active/done).
    The boolean sibling of _find_ticket, for anchor resolution (T-2833)."""
    if not _TICKET_ID_FULL_RE.fullmatch(tid):
        return False
    fname = f"{tid}.md"
    return any((TICKETS_ROOT / state / fname).exists() for state in STATES)


# T-2833: the legal forms of a not_before date-gate anchor. A date gate is
# anchored to the operator OR to a real-world blocker (the operator's directive, TG
# 5641/5651) — never to a free-text reason. Two forms only:
#   operator:<T-NNNN|tg:MSGID> — the operator picked this date: a ticket recording his
#     decision (must resolve) or a Telegram message id (specific + auditable).
#   event:<KIND>:<param>      — a genuine real-world trigger, KIND from a CLOSED
#     enum of real deadline/cadence types. The closed set is the whole point:
#     "fresh_session" is not a KIND, so the free-text bypass has no home, while a
#     real cert/key deadline or a maintenance cadence can stay a simple dated
#     gate. The param is descriptive (e.g. event:cert-expiry:your-domain.example), required.
# An arbitrary reason ("fresh session", "be careful", "not urgent") fits NEITHER
# form — it is a pickup note in the body, or the real blocker is a --depends-on
# dependency. Enforced everywhere a not_before is SET; pre-existing unanchored
# gates are grandfathered in the files and surfaced by ticket_lint.
# T-3215: composed from _TICKET_ID_SHAPE (above) so the ticket-id half of this
# anchor widens in lockstep with the rest of the module instead of drifting.
_OPERATOR_ANCHOR_RE = re.compile(rf"^operator:({_TICKET_ID_SHAPE}|tg:\d+)$")
_EVENT_ANCHOR_KINDS = ("cert-expiry", "key-expiry", "cadence", "window")
_EVENT_ANCHOR_RE = re.compile(r"^event:(?:" + "|".join(_EVENT_ANCHOR_KINDS) + r"):.+$")

# T-3103: the event:<kind>:<param> form is anchor-VALID but the param is still
# gameable — `window` especially takes an open-ended param, and past Recovery
# sessions laundered arbitrary defers of ACTIONABLE work through pseudo-windows
# like `event:window:passport-hardening-focused-session` (3rd recurrence:
# 2026-08-11/20/23; 13 tickets cleared 08-23). A real anchor names a WHEN
# (a date, a deadline, a cadence); these HOW-words name a way-of-working
# ("do it in a focused session / a careful pass / when I'm unhurried") — a
# pickup note, not a gate. Rejected at SET time in every event param. Genuine
# params (dates, hostnames, weekly/quarterly, cert names) contain none of them.
# See feedback_no_arbitrary_date_gates_on_actionable_work.
_PSEUDO_WINDOW_TOKENS = (
    "focused", "dedicated", "robustness", "careful-pass", "careful",
    "session", "batch", "unhurried",
)


def _validate_not_before_anchor(anchor: str | None) -> str:
    """Return the normalized not_before anchor, or raise TicketError. Enforced at
    every point a not_before date is SET (ticket.py block/file); pre-existing
    unanchored gates are grandfathered in the files and surfaced by ticket_lint."""
    _forms = ("operator:<T-NNNN|tg:MSGID> (the operator picked the date) OR event:<"
              + "|".join(_EVENT_ANCHOR_KINDS) + ">:<param> (a real-world "
              "deadline/cadence)")
    if not anchor or not anchor.strip():
        raise TicketRefusal(
            f"a not_before date gate requires --not-before-anchor {_forms}. A "
            "'fresh session / be careful / not urgent' reason is a pickup note in "
            "the body, never a gate; a ticket dependency is --depends-on. [T-2833]"
        )
    a = anchor.strip()
    m = _OPERATOR_ANCHOR_RE.match(a)
    if m:
        ref = m.group(1)
        if ref.startswith("T-") and not _ticket_exists(ref):
            raise TicketRefusal(
                f"--not-before-anchor cites {ref}, which does not exist in "
                "inbox/active/done — an operator-anchor ticket ref must resolve. [T-2833]"
            )
        return a
    if _EVENT_ANCHOR_RE.match(a):
        # T-3103: close the pseudo-window bypass — the param must name a WHEN,
        # not a HOW. `event:window:<self-invented>-focused-session` matched the
        # regex above yet named a way-of-working, laundering an arbitrary defer
        # of actionable work (3rd recurrence 2026-08-23). Reject any event param
        # carrying a HOW-word; genuine date/deadline/cadence params carry none.
        param = a.split(":", 2)[2].lower()
        hit = next((w for w in _PSEUDO_WINDOW_TOKENS if w in param), None)
        if hit:
            raise TicketRefusal(
                f"invalid --not-before-anchor {a!r}: the event param names a HOW "
                f"(matched {hit!r}), not a real-world WHEN. "
                "'event:window:<self-invented>-focused-session' is the gameable "
                "bypass this rule exists to close (3rd recurrence 2026-08-23). Use "
                "a real deadline/cadence (event:cert-expiry:<host>, "
                "event:cadence:weekly, a dated maintenance window), a "
                "operator:<T-NNNN|tg:MSGID>, a --depends-on dependency, or put the "
                "pickup guidance in the ticket body — not a date gate. [T-3103]"
            )
        return a
    raise TicketRefusal(
        f"invalid --not-before-anchor {a!r}: must be {_forms}. No free-text "
        "anchors — an arbitrary reason belongs in --depends-on or a body "
        "pickup-note, not a date gate. [T-2833]"
    )


# ---------------------------------------------------------------------------
# Transition-log helpers.
# ---------------------------------------------------------------------------


_TRANSITION_HEADER = "## Transition log"

# Map lifecycle verb (driving CLI subcommand + commit subject) to the
# past-tense form recorded in the in-file transition log. Per ADR-003
# §"Transition-log format": verbs are past-tense in the log.
# T-3440 / ORG-089: sentinel for _do_transition field_updates that DELETES a
# frontmatter key (distinct from setting it to None, which renders as `null`).
_DELETE_FIELD = object()

_LOG_VERB = {
    "claim": "claimed",
    "unclaim": "unclaimed",
    "block": "blocked",  # past-tense; still used for frontmatter-gate log entry
    "resolve": "resolved",
    "retarget": "retargeted",
    "attach": "attached",  # T-5027
    # T-0653: noodle/action lifecycle close verbs for target=operator queue.
    "close-noodle": "noodle-closed",
    "close-action": "action-closed",
}


def append_transition(body: str, actor: str, verb: str, detail: str | None) -> str:
    """Append one bullet line to the body's `## Transition log` section.

    Refuses if the section header is absent (clearer than auto-creating;
    grooming agent's job to repair malformed files).
    """
    if _TRANSITION_HEADER not in body:
        raise TicketError(
            f"body missing required section header `{_TRANSITION_HEADER}`"
        )
    entry = _format_transition_entry(_now_iso(), actor, verb, detail)
    # Ensure body ends with a newline before appending.
    if not body.endswith("\n"):
        body = body + "\n"
    return body + entry + "\n"


def _warn_decision_line_shape(text: str | None, *, surface: str) -> None:
    """ORG-102 step 2 WARN-only gate for newly written decision prose.

    This must never raise: ticket.py runs fleet-wide and a malformed parser is
    an observability bug, not a ticket-system outage.
    """
    if not text or _decision_lines is None:
        return
    try:
        if not _decision_lines.has_decision_marker(text):
            return
        scan = _decision_lines.scan_decisions(text)
        for problem in scan.problems:
            loc = f" line {problem.line_no}" if problem.line_no is not None else ""
            print(
                f"WARN ORG-102 DECISION line: {surface}{loc}: {problem.message}",
                file=sys.stderr,
            )
    except BaseException as exc:
        print(
            f"WARN ORG-102 DECISION line: {surface}: parser crashed "
            f"({type(exc).__name__}); warning-only gate skipped",
            file=sys.stderr,
        )


def _published_parent_sha() -> str | None:
    """Short sha of the last PUBLISHED parent — `merge-base HEAD origin/main`.

    T-2203 introduced this stamp against local HEAD. T-2360 replaced that,
    because HEAD is the one commit guaranteed to be rewritten: this repo's
    pre-push hook REBASES onto origin/main, and a resolve is normally made
    while the branch is ahead (commit the code, then resolve the ticket). So
    the stamp named a commit the very next push destroyed. Measured: 22 of 33
    unreachable citations in the repo came from here, regenerating one per
    ticket operation — which is why T-2272's lint could never converge.

    The merge-base is durable BY CONSTRUCTION: it is already on origin/main, so
    nothing can rewrite it, and a reader can actually fetch and check it, which
    is the whole point of "checkable". When HEAD is already on origin/main it
    returns HEAD, so the previously-correct case is unchanged.

    It reads the LOCAL origin/main ref, so a long-unfetched clone stamps an
    older commit. That is fine and deliberate: older-but-real beats
    newer-but-rewritten. Every value this returns is a commit that exists on
    the remote.

    FAIL-OPEN, unchanged from T-2203 — no sha rather than a wrong one, and
    never block a transition on provenance. `_git` swallows non-zero exits, but
    a missing binary or unreadable repo raises OSError, so the whole thing is
    wrapped. The regex checks are not paranoia: an empty stdout would otherwise
    stamp a blank sha and produce a line that looks authoritative and says
    nothing.

    NO HEAD FALLBACK, deliberately. When origin/main is absent (fresh clone, no
    remote, test fixture) this returns None and the line degrades to the
    pre-T-2203 shape. Falling back to HEAD would silently reintroduce the exact
    rewritable stamp this replaces, in precisely the situation nobody inspects.
    """
    try:
        base = _git("merge-base", "HEAD", "origin/main")
        if base.returncode != 0:
            return None
        full = (base.stdout or "").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", full):
            return None
        res = _git("rev-parse", "--short", full)
        if res.returncode != 0:
            return None
        sha = (res.stdout or "").strip()
        return sha if re.fullmatch(r"[0-9a-f]{7,40}", sha) else None
    except Exception:  # noqa: BLE001 — fail-open is the whole point
        return None


def _format_transition_entry(ts: str, actor: str, verb: str, detail: str | None) -> str:
    """Render one transition-log bullet, stamped with the last PUBLISHED parent
    (T-2203; the source of the sha revised by T-2360 — see
    `_published_parent_sha`, which explains why local HEAD was the wrong parent
    to name even though the INTENT below was always right).

    WHAT THE SHA MEANS, precisely, because the obvious reading is wrong: it is
    the PARENT commit — the repo state this note was written AGAINST — not the
    commit this transition is about to create. The latter is unobtainable by
    construction: the sha of a commit depends on the file contents, and the
    file contains this line, so a note can never carry its own landed sha.

    The parent sha is the more useful of the two anyway, because it is what
    makes staleness CHECKABLE, which is the actual ask. Worked example from
    the same night this landed: T-2194 was filed at 13:32Z describing a bug
    that T-2172 had fixed at 12:37Z. Reconstructing that took reading the
    code, then `git log -S`, then comparing timestamps. A HEAD stamp on the
    filing turns it into one `merge-base --is-ancestor`.

    PLACEMENT IS LOAD-BEARING: the stamp goes AFTER the verb. The consumer
    weekly_ticket_summary.py parses these lines with the verb anchored to the
    actor bracket — `\\[(?P<actor>[^\\]]+)\\]\\s+(?P<action>resolved|...)`.
    Inserting the sha before the verb silently breaks it (it would match
    nothing and report zero resolves, which reads as a quiet week rather than
    as a bug).
    """
    sha = _published_parent_sha()
    stamp = f" @{sha}" if sha else ""
    if detail:
        return f"- {ts} [{actor}] {verb}{stamp}; {detail}"
    return f"- {ts} [{actor}] {verb}{stamp}"


# ---------------------------------------------------------------------------
# Ticket file builders.
# ---------------------------------------------------------------------------


_FM_REGISTRY_MISSING_WARNED = False
_FM_REGISTRY_ERROR_WARNED = False


def _fm_snapshot(fields: dict) -> dict:
    """T-4760: an independent copy of parsed frontmatter, to pass as
    ``_build_ticket_text(old_fields=...)``. Take it right after the parse and
    BEFORE mutating ``fields`` (lists are copied, so in-place list edits on the
    working dict do not move "old")."""
    return {k: (list(v) if isinstance(v, list) else v) for k, v in fields.items()}


def _check_frontmatter_registry(fields: dict, old_fields: dict | None) -> None:
    """T-4751 S1 / T-4760: validate a frontmatter write against ORG-021's registry.

    The single choke point: every ticket-file writer (ticket.py verbs,
    inbox_consumer stamps/clears, ticket_grooming, ticket_lint, s3star, the
    repair filer via `file`) builds its text through _build_ticket_text, and
    every call site builds BEFORE its file write and git step.

    T-4760: "old" is EXPLICIT — the fields this writer itself parsed from the
    file it is about to overwrite (``_fm_snapshot`` taken before mutating), or
    None for a brand-new ticket. There is no process-wide store: a parse of a
    same-id ticket elsewhere (another worktree / path) cannot leak in, a file
    changed on disk by another writer after this writer's read is judged
    against what THIS writer read (so its unchanged values pass and its real
    changes are caught), and a build that is refused or never written leaves
    nothing behind to poison a later check. This function is pure apart from
    stderr.

    WARN mode prints and proceeds; refuse mode raises TicketRefusal here,
    before any side effect. Any OTHER error from the registry module fails
    open: one WARN per process, the write proceeds.
    """
    global _FM_REGISTRY_MISSING_WARNED, _FM_REGISTRY_ERROR_WARNED
    if _fm_registry is None:
        if not _FM_REGISTRY_MISSING_WARNED:
            _FM_REGISTRY_MISSING_WARNED = True
            print("[frontmatter-registry] WARN registry unavailable: "
                  "tools/frontmatter_registry.py not importable — validation skipped",
                  file=sys.stderr)
        return
    tid = fields.get("id")
    try:
        _fm_registry.enforce_write(old_fields, fields,
                                   ticket_id=tid if isinstance(tid, str) else None)
    except _fm_registry.RegistryRefusal as exc:
        raise TicketRefusal(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — T-4751 r1 NIT-1: the checker must fail OPEN
        if not _FM_REGISTRY_ERROR_WARNED:
            _FM_REGISTRY_ERROR_WARNED = True
            detail = " ".join(f"{type(exc).__name__}: {exc}".split())
            print(f"[frontmatter-registry] WARN registry check failed unexpectedly: "
                  f"{detail} — validation skipped", file=sys.stderr)


def _build_ticket_text(fields: dict, body: str, *, old_fields: dict | None) -> str:
    """Serialize a ticket file, after the ORG-021 registry check.

    ``old_fields`` is REQUIRED (keyword-only, no default — T-4760): the
    ``_fm_snapshot`` of what the caller parsed from the file it overwrites, or
    None for a new ticket. A forgotten argument is a TypeError at test time,
    never a silent fall-back to "everything is new"."""
    _check_frontmatter_registry(fields, old_fields)
    return serialize_front_matter(fields) + "\n" + body


def recovered_before_repair_mutation(text: str, tid: str, actor: str, note: str) -> "tuple[str, str] | None":
    """ORG-076 BT3b (RFC-004 section 2b): the pure text half of the healed-under-ticket flag.

    Returns ``(new_ticket_text, commit_message)``, or ``None`` when the ticket already carries
    ``recovered_before_repair: true`` (idempotent). Built from this module's OWN helpers -- the
    annotate path's ``parse_front_matter`` / ``append_transition`` / ``_build_ticket_text`` /
    ``_build_commit_message`` -- so the transition-log line and the commit subject have exactly the
    shape of ``ticket.py annotate``. Front matter round-trips byte-for-byte (including the
    ``evidence_snapshot: |`` block scalar); only the flag line and ONE appended log entry change.
    Refuses a ticket with no ``recovered_before_repair`` field (not a runtime repair ticket) and a
    done/ ticket is the CALLER's concern (annotate refuses those too). No I/O."""
    fields, body = parse_front_matter(text)
    if "recovered_before_repair" not in fields:
        raise TicketRefusal(f"{tid}: not a runtime repair ticket (no recovered_before_repair field)")
    if str(fields["recovered_before_repair"]).strip().lower() == "true":
        return None
    old_fields = _fm_snapshot(fields)
    fields["recovered_before_repair"] = "true"
    title = _extract_title(body)
    body = append_transition(body, actor, "note", note)
    return (_build_ticket_text(fields, body, old_fields=old_fields),
            _build_commit_message("annotate", tid, title, note))


def _build_initial_body(title: str, ask: str, context: str, actor: str) -> str:
    ask_text = ask.strip() + "\n" if ask else ""
    ctx_text = context.strip() + "\n" if context else ""
    # Initial filing entry is rendered as "filed in inbox" (no semicolon),
    # per the ADR's example. Subsequent transitions use past-tense verbs
    # like "claimed; <detail>".
    initial_log = f"- {_now_iso()} [{actor}] filed in inbox"
    parts = [
        f"# {title}",
        "",
        "## Ask",
        "",
        ask_text,
        "## Context",
        "",
        ctx_text,
        "## Transition log",
        "",
        initial_log,
        "",
    ]
    # Join, then collapse only the explicit empty placeholders we left for
    # missing ask/context. Trailing single newline after the last log entry.
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Commit-message templates.
# ---------------------------------------------------------------------------


def _build_commit_message(verb: str, tid: str, title: str | None, detail: str | None) -> str:
    if verb == "file":
        subject = f"tickets: file {tid} — {title}"
    elif verb == "claim":
        subject = f"tickets: claim {tid}"
    elif verb == "unclaim":
        subject = f"tickets: unclaim {tid}"
    elif verb == "block":
        subject = f"tickets: block {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: block {tid} — {short}"
    elif verb == "resolve":
        subject = f"tickets: resolve {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: resolve {tid} — {short}"
    elif verb == "retarget":
        subject = f"tickets: retarget {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: retarget {tid} — {short}"
    elif verb == "attach":
        # T-5027.
        subject = f"tickets: attach {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: attach {tid} — {short}"
    elif verb == "annotate":
        subject = f"tickets: annotate {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: annotate {tid} — {short}"
    elif verb == "supersede-ask":
        # T-2247. Distinct subject from `annotate`: this one CHANGES WHAT THE
        # TICKET INSTRUCTS, which a log-scanner reading subjects must be able to
        # tell apart from a free-form note that changes nothing.
        subject = f"tickets: supersede-ask {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: supersede-ask {tid} — {short}"
    elif verb == "addendum":
        # T-2205. Distinct subject from `annotate` so the post-resolution
        # correction is legible in `git log` without opening the ticket.
        subject = f"tickets: addendum {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: addendum {tid} — {short}"
    elif verb == "close-noodle":
        subject = f"tickets: close-noodle {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: close-noodle {tid} — {short}"
    elif verb == "close-action":
        subject = f"tickets: close-action {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: close-action {tid} — {short}"
    elif verb == "label-add":
        subject = f"tickets: label add {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: label add {tid} — {short}"
    elif verb == "label-remove":
        subject = f"tickets: label remove {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: label remove {tid} — {short}"
    elif verb in ("re-earn", "drop", "adjust-appetite", "reopen", "tier-rank"):
        # Rationale-bearing in-place decisions. Subject names the verb; full
        # rationale/reason lands in the body when truncated (see below).
        subject = f"tickets: {verb} {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: {verb} {tid} — {short}"
    elif verb in ("security-accept", "security-reject"):
        # T-5678: subject names the verb; the note rides in the body (never truncated).
        subject = f"tickets: {verb} {tid}"
    elif verb == "clear-operator-intent":
        # T-5522: subject carries only the verb; the full decided-by/ref/why
        # detail goes in the body so the audit chain is never truncated.
        subject = f"tickets: clear-operator-intent {tid}"
    elif verb == "await-merge":
        # T-4119.
        subject = f"tickets: await-merge {tid}"
        if detail:
            short = detail if len(detail) <= 60 else detail[:57] + "..."
            subject = f"tickets: await-merge {tid} — {short}"
    else:
        raise TicketError(f"unknown verb for commit message: {verb!r}")

    body_parts: list[str] = []
    # Include the full detail in the body when it was truncated in the subject,
    # so the audit chain retains the unabbreviated reason / summary.
    if verb in ("block", "resolve", "retarget", "attach", "close-noodle", "close-action",
                "label-add", "label-remove",
                "re-earn", "drop", "adjust-appetite", "reopen", "tier-rank") \
            and detail and len(detail) > 60:
        body_parts.append(detail)
    if verb in ("clear-operator-intent", "security-accept", "security-reject") and detail:
        body_parts.append(detail)

    if not body_parts:
        return subject + "\n"
    return subject + "\n\n" + "\n".join(body_parts) + "\n"


# ---------------------------------------------------------------------------
# README table rebuild.
# ---------------------------------------------------------------------------

# Number of most-recent done tickets (by ID descending) to keep as full rows.
# Older done tickets are collapsed to per-created-month digest lines.
# Tunable: raising K keeps more detail; lowering K compresses the index further.
# Current value chosen to cover ~5 weeks of typical throughput while keeping
# the boot-loaded index well under 100 rows. justifies-via D10 (bound the index).
README_DONE_ROW_FLOOR = 40


def _partition_done_rows(
    done_tickets: list["Ticket"],
) -> tuple[list[str], list[str]]:
    """Split done tickets into full table rows and monthly digest lines.

    Args:
        done_tickets: Done-state tickets already sorted by ID descending
                      (highest ID first).  The caller owns this sort.

    Returns:
        A 2-tuple ``(full_rows, digest_lines)`` where:

        - ``full_rows`` is a list of formatted ``| col | … |`` row strings,
          one per ticket, for the top ``README_DONE_ROW_FLOOR`` tickets by ID.
        - ``digest_lines`` is a list of formatted digest-row strings, one per
          YYYY-MM bucket, for any tickets beyond the floor, sorted by month
          descending (newest month first).

        If ``len(done_tickets) <= README_DONE_ROW_FLOOR`` then ``digest_lines``
        is empty.  If ``done_tickets`` is empty both lists are empty.

        The digest line for a boundary month (where some tickets of that month
        appear in ``full_rows`` and others are collapsed) reflects ONLY the
        collapsed subset — its count and T-lo/T-hi span the collapsed tickets
        alone, not the whole month.
    """
    if not done_tickets:
        return [], []

    # Split into the kept full-row set and the to-be-collapsed set.
    # done_tickets is sorted descending by ID; the top K stay as full rows.
    kept = done_tickets[:README_DONE_ROW_FLOOR]
    collapsed = done_tickets[README_DONE_ROW_FLOOR:]

    # Build full row strings for kept tickets.
    full_rows: list[str] = []
    for t in kept:
        tid = t.fields.get("id", "?")
        state = t.fields.get("state", "?")
        title = t.title or "(no title)"
        requester = t.fields.get("requester", "?")
        target = t.fields.get("target", "?")
        created_iso = t.fields.get("created", "")
        filed = created_iso[:10] if len(created_iso) >= 10 else "?"
        full_rows.append(
            _md_row(tid, state, title, f"{requester} → {target}", filed)
        )

    if not collapsed:
        return full_rows, []

    # Group collapsed tickets by YYYY-MM bucket (from 'created' frontmatter).
    # Tickets within each bucket are still in ID-descending order (inherited
    # from the input sort), so min/max ID are first and last in each group.
    buckets: dict[str, list[str]] = {}
    for t in collapsed:
        created_iso = t.fields.get("created", "")
        month = created_iso[:7] if len(created_iso) >= 7 else "???-??"
        tid = t.fields.get("id", "?")
        buckets.setdefault(month, []).append(tid)

    # Build digest lines, one per month, sorted month descending.
    digest_lines: list[str] = []
    for month in sorted(buckets.keys(), reverse=True):
        tids = buckets[month]  # already descending (highest first)
        count = len(tids)
        hi = tids[0]   # highest ID in the collapsed subset for this month
        lo = tids[-1]  # lowest  ID in the collapsed subset for this month
        digest_lines.append(
            _md_row(
                month,
                "done (digest)",
                f"{count} done · {lo}–{hi} · grep tickets/done/",
                "—",
                "—",
            )
        )

    return full_rows, digest_lines


def _rebuild_readme_table(tickets_root: Path, readme_path: Path) -> bool:
    """Regenerate the 'Current state' table in tickets/README.md from filesystem.

    Reads all ticket files from tickets/{inbox,active,done}/*.md,
    parses frontmatter, and replaces the table in README.md with a freshly
    generated one sorted by ticket ID descending.

    Returns True if README content changed (and was written), False if the
    rebuilt content matches what was already on disk. The caller uses this
    signal to decide whether to stage README in the auto-commit pipeline —
    post-ORG-019 block-subcommand edits frontmatter in place without moving
    the ticket between dirs, which means the table is often unchanged.

    Idempotent: running twice on the same state produces the same output.
    """
    # Collect all tickets
    tickets: list[Ticket] = []
    for state in STATES:
        state_dir = tickets_root / state
        if not state_dir.exists():
            continue
        for entry in sorted(state_dir.iterdir()):
            if not entry.is_file():
                continue
            if not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            try:
                tickets.append(parse_ticket(entry))
            except Exception:
                # Skip malformed tickets rather than blocking the whole rebuild
                continue

    # Sort by ID descending (T-0017 before T-0001)
    tickets.sort(key=lambda t: t.fields.get("id", ""), reverse=True)

    # Separate by state so decay applies only to done tickets.
    live_tickets = [t for t in tickets if t.fields.get("state") != "done"]
    done_tickets = [t for t in tickets if t.fields.get("state") == "done"]

    # Build full rows for live (inbox + active) tickets — never decayed.
    live_rows: list[str] = []
    for t in live_tickets:
        tid = t.fields.get("id", "?")
        state = t.fields.get("state", "?")
        title = t.title or "(no title)"
        requester = t.fields.get("requester", "?")
        target = t.fields.get("target", "?")
        created_iso = t.fields.get("created", "")
        filed = created_iso[:10] if len(created_iso) >= 10 else "?"
        live_rows.append(
            _md_row(tid, state, title, f"{requester} → {target}", filed)
        )

    # Apply done-row decay: top README_DONE_ROW_FLOOR by ID stay as full rows;
    # older done tickets collapse to per-month digest lines.
    done_full_rows, digest_lines = _partition_done_rows(done_tickets)

    # Assemble table: header, live rows, done full rows, digest lines.
    table_lines = [
        "| ID | State | Title | Requester → Target | Filed |",
        "|---|---|---|---|---|",
        *live_rows,
        *done_full_rows,
        *digest_lines,
    ]

    # Read existing README
    readme_text = readme_path.read_text(encoding="utf-8")

    # Find the "## Current state" section
    marker = "## Current state"
    if marker not in readme_text:
        # If the marker doesn't exist, we can't rebuild — refuse rather than
        # corrupting the file.
        raise TicketError(f"README.md missing required section header `{marker}`")

    # Split at the marker
    before_marker = readme_text[: readme_text.index(marker)]
    after_marker = readme_text[readme_text.index(marker) + len(marker) :]

    # Find the end of the table: first line after marker that doesn't start with |
    # and isn't blank (preserves the blank line after the table)
    after_lines = after_marker.splitlines(keepends=True)
    table_end_idx = 0
    for i, line in enumerate(after_lines):
        stripped = line.lstrip()
        # Skip blank lines and table lines
        if stripped == "" or stripped.startswith("|"):
            table_end_idx = i + 1
        else:
            break

    # Reconstruct README
    new_content = (
        before_marker
        + marker
        + "\n\n"
        + "\n".join(table_lines)
        + "\n"
        + "".join(after_lines[table_end_idx:])
    )

    changed = new_content != readme_text
    if changed:
        try:
            readme_path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            # T-1003: cross-owner README (kino PWA uid creates 0644 file; claude-user
            # ticket.py then hits PermissionError on write). Also covers ENOSPC/EROFS.
            # The README rebuild is best-effort — a perms hiccup must never abort a
            # ticket op. Degrade gracefully: warn to stderr and return False so the
            # caller skips staging README (no phantom git-add of an unwritten file).
            # stderr not stdout: ticket.py stdout may be parsed by callers.
            # justifies-via P1 (structural: ticket op completes; README self-heals on
            # next successful write by any actor who owns the file).
            print(
                f"ticket.py: WARNING: README rebuild skipped for {readme_path}: "
                f"permission/OS error ({exc}); ticket op itself still completed.",
                file=sys.stderr,
            )
            return False
    # T-0695: keep tickets/README.md group-writable (0664). The PWA runs this
    # via ticket.py as uid=kino; a kino-created/recreated README (git pipeline
    # or umask-022 creation) can land 0644 (group read-only), which then blocks
    # subsequent claude-user ticket.py writes (PermissionError on this rebuild).
    # Best-effort: only the file's owner can chmod, so a cross-owner call raises
    # EPERM — harmless, since in that case the file is already group-writable.
    # justifies-via P1 (structural fix at the write path, caller-agnostic).
    try:
        os.chmod(readme_path, 0o664)
    except OSError:
        pass
    return changed


# ---------------------------------------------------------------------------
# Auto-commit-and-push pipeline.
# ---------------------------------------------------------------------------


# T-4380: append-only warm-up ledger shards are NOT regenerable from the tree (an
# event log, not derived state), so a rebase conflict on one is resolved by a
# line UNION rather than by regeneration.
_APPETITE_SHARD_RE = re.compile(r"^ledger/appetite/[^/]+\.jsonl$")


def _dedupe_added_lines(base: list[str], merged: list[str]) -> list[str]:
    """Drop repeat copies of lines the union ADDED over `base`.

    A line that already exists in `base` is history and is always kept as-is (even
    if `base` itself repeats it). A line NOT in `base` is kept at its first
    position only, so a replayed append that both sides made (or that a union of
    overlapping appends duplicated) is counted once. Order is otherwise preserved.
    """
    base_set = set(base)
    seen: set[str] = set()
    out: list[str] = []
    for line in merged:
        if line in base_set:
            out.append(line)
        elif line not in seen:
            seen.add(line)
            out.append(line)
    return out


def _is_pure_append(base: str, ours: str, theirs: str) -> bool:
    """True iff BOTH sides only appended: `base`'s lines are a prefix of `ours`' lines
    and of `theirs`' lines, so neither side rewrote or deleted history. Compared per
    line with terminators stripped, so a base whose last line lacks a newline still
    counts as a prefix of a side that appended after it."""
    base_lines = base.splitlines()
    n = len(base_lines)
    return ours.splitlines()[:n] == base_lines and theirs.splitlines()[:n] == base_lines


def _union_merge_conflicted_file(rel_path: str, cwd: Path) -> bool:
    """Resolve a rebase-conflicted APPEND-ONLY file by line union; stage it.

    Reads the three index stages (1=base, 2=ours, 3=theirs) and runs `git
    merge-file --union`, then dedupes lines the union added (`_dedupe_added_lines`).
    ORDER: during a rebase "ours" is the branch being rebased ONTO (what already
    landed on origin) and "theirs" is the commit being replayed, so upstream's
    lines come first and the replayed commit's second — the order they would have
    had if the replaying writer had pushed second on a clean base. A missing stage
    1 (both sides created the file) means an empty base. DECLINES (False) unless
    the conflict is a PURE APPEND: a missing stage 2 or 3 (delete/modify), or a
    side that rewrote or deleted any base line (`_is_pure_append`), is not an append
    race — a union would resurrect the deleted file or keep both variants of an
    edited line side by side.
    """
    import tempfile

    stages: dict[int, str | None] = {}
    for n in (1, 2, 3):
        r = _git("show", f":{n}:{rel_path}", cwd=cwd)
        stages[n] = r.stdout if r.returncode == 0 else None
    if stages[2] is None or stages[3] is None:
        return False
    base_text = stages[1] or ""
    if not _is_pure_append(base_text, stages[2], stages[3]):
        return False
    with tempfile.TemporaryDirectory() as td:
        files = {}
        for name, text in (("base", base_text), ("ours", stages[2]), ("theirs", stages[3])):
            f = Path(td) / name
            f.write_text(text, encoding="utf-8")
            files[name] = str(f)
        mf = _git(
            "merge-file", "--union", "-p", files["ours"], files["base"], files["theirs"], cwd=cwd
        )
    if mf.returncode != 0:
        return False
    merged = _dedupe_added_lines(base_text.splitlines(keepends=True), mf.stdout.splitlines(keepends=True))
    out = "".join(merged)
    if out and not out.endswith("\n"):
        out += "\n"
    (Path(cwd) / rel_path).write_text(out, encoding="utf-8")
    return _git("add", "--", rel_path, cwd=cwd).returncode == 0


def _board_conflict_resolver(unmerged_paths: list[str], cwd: Path) -> bool:
    """push_with_retry rebase-conflict resolver (T-0846, extended T-4380).

    Resolve a rebase conflict limited to GENERATED / append-only ticket-op
    artifacts: the `tickets/README` board (regenerated from the rebased ticket
    files) and `ledger/appetite/*.jsonl` shards (union-merged, and only when the
    conflict is a pure append: an append-only event log cannot be regenerated, and
    an edit or delete of history is a real conflict). ANY other conflicted path, or
    a shard conflict that is not a pure append -> False, so a real
    ticket-file conflict still surfaces / aborts. The board is a pure function of
    the filesystem, so concurrent ticket ops only ever "conflict" on it
    cosmetically; regenerate-from-state is the correct merge. `cwd` is the repo the
    rebase runs in (== REPO_ROOT for ticket.py); we operate via the
    REPO_ROOT-defaulted `_git`.
    """
    paths = set(unmerged_paths)
    shards = {p for p in paths if _APPETITE_SHARD_RE.match(p)}
    if not paths or not paths <= ({"tickets/README.md"} | shards):
        return False
    for shard in sorted(shards):
        if not _union_merge_conflicted_file(shard, cwd):
            return False
    if "tickets/README.md" not in paths:
        return True
    readme_path = REPO_ROOT / "tickets" / "README.md"
    # Clear the conflict markers by checking out one whole side, then regenerate
    # the table from the (already-merged) ticket files on disk. WHICH side is
    # arbitrary — the table is fully rebuilt either way; we only need a
    # marker-free base. (`--theirs` during a REBASE is the local-commit side,
    # inverted vs merge, but it doesn't matter here.) Assumes the README's
    # non-table prose (static header/footer) isn't being concurrently edited in
    # a way that races a ticket op — that prose changes only via deliberate doc
    # commits, which don't collide with the board.
    co = _git("checkout", "--theirs", "--", "tickets/README.md", cwd=cwd)
    if co.returncode != 0:
        return False
    try:
        _rebuild_readme_table(TICKETS_ROOT, readme_path)
    except Exception:
        return False
    return _git("add", "--", "tickets/README.md", cwd=cwd).returncode == 0


# T-2333: _pull_if_needed's pre-mutation pull (`pull_or_rebase_strict`) has TWO
# distinct failure shapes with OPPOSITE truth about local state, and one hint
# text used to be printed for both:
#   - ff refused, clone not ahead (usually a dirty tree): the pull genuinely
#     ran before any write, so "nothing has been written yet" holds.
#   - ff refused, clone ahead, rebase-onto-origin then hit a real conflict
#     (T-0846 self-heal path, reachable only when a PRIOR op already committed
#     and its push was lost/failed): a local commit demonstrably EXISTS. The
#     old single hint asserted "nothing written" here too — false, and it
#     steered the reader toward re-running, which on the `file` op allocates a
#     SECOND ticket id for content already committed under the first (T-2333).
# Selection is on the RETURN-VALUE PREFIX `pull_or_rebase_strict` itself
# emits (_git_utils.PULL_NOT_AHEAD_PREFIX / PULL_REBASE_CONFLICT_PREFIX),
# never on git's own free text — git's wording is locale/version-dependent
# and was never a safe discriminator (T-2204's docstring already says so).

_PULL_FAILURE_HINT_FF = (
    "ticket.py runs an ff-only pull BEFORE touching any file (T-0244), so a "
    "failure here means nothing has been written yet. The usual cause is a "
    "DIRTY WORKING TREE in this clone — git's own message ('Please commit your "
    "changes or stash them before you merge') never mentions that ticket.py "
    "pulled, or why. Fix: commit or stash the unrelated changes "
    "(`git -C {repo} status`), then re-run."
).format(repo=REPO_ROOT)

_PULL_FAILURE_HINT_REBASE_CONFLICT = (
    "ticket.py's pre-write pull found this clone AHEAD of origin/main and "
    "tried to rebase those local unpushed commits onto the fresh tip (T-0846) "
    "— the self-heal for a STRANDED COMMIT left by a PRIOR ticket op (e.g. its "
    "push lost a race or failed after the commit). That rebase hit a real "
    "conflict, so it was ABORTED and this clone was restored to its "
    "pre-rebase state: your local commit(s) STILL EXIST on this branch — run "
    "`git -C {repo} log --oneline origin/main..HEAD` to see them. "
    "DO NOT RE-RUN this operation: on the `file` op, re-running allocates a "
    "SECOND ticket id for content already committed under the first. Inspect "
    "the conflicting path(s) named above and reconcile by hand (or ask a "
    "human) before retrying."
).format(repo=REPO_ROOT)

_PULL_FAILURE_HINT_FETCH = (
    "ticket.py's pre-write pull could not reach origin at all — the fetch "
    "failed before any merge or rebase was attempted, so NOTHING HAS BEEN "
    "WRITTEN and no local commit was created. This is a NETWORK or "
    "REMOTE-AUTH problem, not a state problem in this clone: check "
    "connectivity and credentials (`git -C {repo} fetch origin main` will "
    "reproduce it directly). RE-RUNNING IS SAFE once the remote is reachable "
    "— unlike the rebase-conflict case, no id was allocated and no state was "
    "moved, so there is nothing to double up."
).format(repo=REPO_ROOT)

_PULL_FAILURE_HINT_UNKNOWN = (
    "ticket.py's pre-write pull failed for a reason this hint cannot "
    "classify — it can assert NEITHER that nothing has been written NOR that "
    "a local commit exists. Check `git -C {repo} status` and `git -C {repo} "
    "log --oneline origin/main..HEAD` before deciding whether it is safe to "
    "re-run."
).format(repo=REPO_ROOT)


def _pull_failure_hint(msg: str) -> str:
    """Select the T-2333-correct hint for a `pull_or_rebase_strict` failure `msg`.

    Discriminates on `_git_utils`'s own return-value prefixes (see the module
    comment above) so an unrecognised failure — one that matches neither
    prefix — falls back to a hint that claims neither invariant, rather than
    silently inheriting the (possibly false) ff-path wording.
    """
    if msg.startswith(PULL_REBASE_CONFLICT_PREFIX):
        return _PULL_FAILURE_HINT_REBASE_CONFLICT
    if msg.startswith(PULL_NOT_AHEAD_PREFIX):
        return _PULL_FAILURE_HINT_FF
    if msg.startswith(PULL_FETCH_FAILED_PREFIX):
        return _PULL_FAILURE_HINT_FETCH
    return _PULL_FAILURE_HINT_UNKNOWN


def _pull_if_needed(no_push: bool, *, strict: bool = True) -> None:
    """Pre-mutation pull. Called by callers BEFORE filesystem changes (T-0244).

    strict=False (T-2204) downgrades a pull failure to a WARNING and proceeds.

    ONLY legitimate for operations that neither allocate an ID nor move state
    — in practice `annotate`, which appends a note to a ticket that already
    exists at a path already known. Being a few commits behind cannot make
    that note wrong; the append is additive and the commit is scoped to the
    one file.

    It must NOT be relaxed for file/claim/resolve/block/retarget: those
    allocate the next ID or move a ticket between directories, and a stale
    view of origin makes them allocate a colliding ID or transition from a
    state that is no longer real.

    The failure this fixes: a dirty tree aborted the internal pull, which
    failed EVERY write in the clone — including annotate. So the moment the
    tree was dirty, the tool used to REPORT the problem died the same way as
    the tool used to fix it, and the remediation path and the reporting path
    failed together. Keeping annotate alive means an agent can always leave a
    record of what it hit, even in a clone it cannot currently transition.

    Failure modes for git pull (network down, ff-not-possible, no remote in
    hermetic e2e) used to leave the working tree mid-transition with files
    moved/edited but no commit. Hoisting the pull to a pre-mutation check
    means pull-failure raises BEFORE any filesystem change happens.

    Skipped when no_push=True: --no-push semantically means "local-only
    operation; don't network." The next pushing op (or a manual push) carries
    the local-ahead state to origin. (The T-0211 60s sync timer that formerly
    auto-reconciled was retired by T-0365 — 2026-06-09; callers that need
    propagation either push themselves, e.g. ticket_lint's unified
    push_with_retry, or run a normal pushing lifecycle op next.) This is what
    makes --no-push usable in hermetic / offline contexts (T-0243: Playwright
    e2e fixtures with no git remote).
    """
    if no_push:
        return
    # ff-only pull, but self-heal a diverged clone by rebasing local (unpushed)
    # commits onto origin/main rather than hard-failing (T-0846). A prior ticket
    # op can leave a stranded local commit (push failed after commit); without
    # this, the NEXT op's ff-only pull raises and the clone stays wedged. Rebase
    # is the reconcile strategy (the operator's call 2026-06-05). The board resolver
    # handles a purely-cosmetic tickets/README conflict; real ticket conflicts
    # still abort + fail loud inside pull_or_rebase_strict.
    ok, msg = pull_or_rebase_strict(conflict_resolver=_board_conflict_resolver)
    if not ok:
        # T-2204: name the cause and the fix in BOTH paths. git's own text
        # ("Please commit your changes or stash them before you merge.
        # Aborting") never says that ticket.py pulled, or why, so the operator
        # sees a merge error for a merge they did not run. T-2333: which cause
        # gets named must match which failure this actually was — see
        # `_pull_failure_hint`.
        hint = _pull_failure_hint(msg)
        if strict:
            raise TicketError(f"{msg}\n\n{hint}")
        print(
            f"ticket.py: WARNING: pre-write pull failed; continuing anyway "
            f"(this operation neither allocates an ID nor moves state, so a "
            f"stale view cannot make it wrong).\n{msg}\n\n{hint}",
            file=sys.stderr,
        )


def _refuse_if_mid_operation(repo_root: Path) -> None:
    """T-4422: refuse a ticket write while a rebase / merge / cherry-pick / revert is in
    progress in `repo_root`. Committing there would land the ticket change on a
    detached, half-rebased (or half-merged) HEAD, and the push could then report an
    up-to-date no-op as landed or publish the partial state."""
    kind = _mid_operation_kind(repo_root)
    if kind is not None:
        raise TicketError(
            f"refusing to write: a {kind} is in progress in {repo_root}. Nothing was "
            f"staged, moved or committed. Finish it (e.g. `git -C {repo_root} "
            f"{'rebase' if kind.startswith('rebase') else kind} --continue`) or abandon "
            f"it (`... --abort`) by hand, then re-run this command."
        )


@contextlib.contextmanager
def git_mutation_lock(**kwargs):
    """The shared git mutation lock, plus the T-4422 refusal: every ticket write
    takes this lock before its first `git mv` / file edit / commit, so checking
    right after acquiring it stops the op before it touches anything."""
    with _git_mutation_lock_raw(**kwargs):
        _refuse_if_mid_operation(REPO_ROOT)
        yield


def _stage_and_commit_scoped(
    affected_paths: list[Path],
    commit_message: str,
    *,
    repo_root: Path | None = None,
    require_added: list[Path] | None = None,
) -> None:
    """Stage exactly *affected_paths* (those that exist on disk), assert the
    staged set equals the full scope, then commit — steps 4-6 of the transactional
    ticket commit, extracted (ORG-088 T3 Q1) so BOTH _commit_and_push and the
    role-assignment executor's atomic rebind / receipt sidecar commit through the
    SAME audited scope-guarded path instead of a duplicated one.

    BEHAVIOR-PRESERVING for _commit_and_push: this is a pure move of the former
    inline body. `repo_root` defaults to None and is resolved to the MODULE-GLOBAL
    REPO_ROOT at CALL time (not bound as a default arg) so the existing tests that
    monkeypatch ticket.REPO_ROOT to a temp dir keep working unchanged. `repo_root`
    is the one additive extension — it lets the executor run this against an
    isolated repo (its own live checkout, or a hermetic test repo) rather than the
    module-global. Callers own HERMES-guarding, README rebuild, and pushing; this
    does staging + scope-assert + commit only.

    require_added (T-4885): paths that MUST stage as a new-file ADD. The `file`
    verb passes its new ticket path(s): a brand-new ticket that stages as anything
    but `A` means it overwrote an existing file, so refuse (TicketRefusal) and
    reset the index. Default None leaves every other verb unchanged.

    Raises TicketError on any failure; resets the index on a scope-guard refusal
    so a rejected commit never leaves a partially-staged index behind.
    """
    if repo_root is None:
        repo_root = REPO_ROOT
    else:
        # T-4422: an explicit repo_root (the role-assignment executor's isolated repo)
        # is not the REPO_ROOT the lock wrapper checked, so check it here. The default
        # path is always under ticket.py's git_mutation_lock, which already refused.
        _refuse_if_mid_operation(repo_root)
    # Stage paths that exist in the working tree, in repo-relative form so the
    # diff-cached scope compare below is directly comparable. Paths previously
    # moved via `git mv` are already staged as deletions and must NOT be re-added
    # (`git add -- <missing>` fails "pathspec did not match any files" — T-0004);
    # the strict-set assertion still validates the full expected set.
    rel_paths: list[str] = []
    for p in affected_paths:
        try:
            rel_paths.append(str(p.relative_to(repo_root)))
        except ValueError:
            raise TicketError(f"path outside repo: {p}")
    to_add = [rp for rp in rel_paths if (repo_root / rp).exists()]
    if to_add:
        res = _git("add", "--", *to_add, cwd=repo_root)
        if res.returncode != 0:
            raise TicketError(
                f"git add failed: {res.stderr.strip() or res.stdout.strip()}"
            )

    # `--no-renames` disables git's rename-detection coalescing — without it a
    # `git mv old new` reports only the destination, failing the [old, new]
    # expected-set compare (T-0004 fix companion).
    res = _git("diff", "--cached", "--name-only", "--no-renames", cwd=repo_root)
    if res.returncode != 0:
        raise TicketError(
            f"git diff --cached failed: {res.stderr.strip() or res.stdout.strip()}"
        )
    staged = sorted(p for p in res.stdout.split("\n") if p.strip())
    expected = sorted(rel_paths)
    if staged != expected:
        _git("reset", "HEAD", cwd=repo_root)
        raise TicketError(
            f"scope guard refused: staged set {staged!r} != expected {expected!r}"
        )

    if require_added:
        res = _git("diff", "--cached", "--name-status", "--no-renames", cwd=repo_root)
        if res.returncode != 0:
            _git("reset", "HEAD", cwd=repo_root)
            raise TicketError(
                f"git diff --cached --name-status failed: "
                f"{res.stderr.strip() or res.stdout.strip()}"
            )
        status_by_path: dict[str, str] = {}
        for line in res.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                status_by_path[parts[1]] = parts[0].strip()
        for p_ in require_added:
            rel = str(p_.relative_to(repo_root))
            if status_by_path.get(rel) != "A":
                _git("reset", "HEAD", cwd=repo_root)
                raise TicketRefusal(
                    f"refusing to commit {rel}: staged as "
                    f"{status_by_path.get(rel) or 'no change'!r}, expected a new-file "
                    f"ADD ('A'). A new ticket must never overwrite an existing file "
                    f"(T-4885)."
                )

    res = _git("commit", "-m", commit_message, cwd=repo_root)
    if res.returncode != 0:
        raise TicketError(
            f"git commit failed: {res.stderr.strip() or res.stdout.strip()}"
        )


def _dirty_snapshot() -> dict[str, tuple[str, bytes | None]]:
    """T-5511: {repo-relative path: (porcelain XY, bytes or None)} for every path git
    sees as dirty right now (tracked changes + untracked, renames split). Taken before
    a transition's first write so a rollback can restore a path the transition wrote
    that was ALREADY dirty (uncommitted edits, a staged change) to what it was."""
    res = _git("status", "--porcelain", "-z", "--no-renames", "--untracked-files=all")
    out: dict[str, tuple[str, bytes | None]] = {}
    if res.returncode != 0:
        return out
    for entry in res.stdout.split("\0"):
        if len(entry) > 3:
            rel = entry[3:]
            f = REPO_ROOT / rel
            out[rel] = (entry[:2], f.read_bytes() if f.is_file() else None)
    return out


def _rollback_transition(
    baseline: dict[str, tuple[str, bytes | None]], head_before: str, written: list[str]
) -> None:
    """T-5511: undo a transition that failed after its first write but before its
    commit landed (scope guard, HERMES guard, cascade/lift/appetite failure, ...).

    Touches ONLY *written* (repo-relative paths the transition itself wrote or
    moved) — never a path another writer (editor, other seat, the user) changed
    meanwhile; the mutation lock does not cover those. For each written path the
    index entry is reset to HEAD, the bytes are restored (baseline bytes if the path
    was already dirty, else HEAD's version, else deleted), and a pre-existing staged
    state is re-applied, so index and tree both match the pre-transition state.
    No-op if HEAD moved (a commit landed; a later push failure must never revert
    committed work). Best-effort per step: never masks the original error."""
    try:
        if _git("rev-parse", "HEAD").stdout.strip() != head_before:
            return
        for rel in written:
            try:
                _git("reset", "-q", "HEAD", "--", rel)
                f = REPO_ROOT / rel
                if rel in baseline:
                    data = baseline[rel][1]
                    if data is None:
                        f.unlink(missing_ok=True)
                    else:
                        f.parent.mkdir(parents=True, exist_ok=True)
                        f.write_bytes(data)
                elif _git("checkout", "HEAD", "--", rel).returncode != 0:
                    f.unlink(missing_ok=True)
                x = baseline[rel][0][0] if rel in baseline else " "
                if x in "MARC" and (REPO_ROOT / rel).exists():
                    _git("add", "--", rel)
                elif x == "D":
                    _git("rm", "-q", "--cached", "--", rel)
            except Exception:  # noqa: BLE001 - one bad path must not strand the rest
                pass
    except Exception:  # noqa: BLE001 - never mask the original error
        pass


def _commit_and_push(
    affected_paths: list[Path],
    commit_message: str,
    no_push: bool = False,
    rebase_on_nonff: bool = True,
    require_added: list[Path] | None = None,
) -> None:
    """Run the auto-commit pipeline for one ticket transition.

    require_added (T-4885): see _stage_and_commit_scoped; the `file` verb passes
    its new ticket path(s) so an overwrite-as-modification is refused pre-commit.

    T-1088 CONVERGENCE EXCEPTION (do NOT migrate to `_git_utils.commit_scoped`):
    the T-1088 P1 convergence folded the hand-rolled scope-narrowed-commit
    pattern across ~9 tools onto `commit_scoped`, but this caller is a
    documented exception (the second, alongside `watch_runner.git_commit_and_push`).
    Forcing it onto the atomic primitive would require THREE caller-specific
    extensions plus a guard-semantics weakening, on the highest-blast-radius
    caller (every ticket op + the restart/handoff path commits through here):
      1. Add-list != scope. A transition does `git mv old new`; `git add -- old`
         then fails fatal-128 ("pathspec did not match any files") because the
         worktree file is gone (T-0004). So we stage only EXISTING paths but
         must keep `old` (pre-staged as a deletion by git mv) in the scope set.
         commit_scoped conflates its `paths` arg as both add-list and scope.
      2. FILE-path re-allocation. cmd_file passes rebase_on_nonff=False so a
         racing same-ID push surfaces as a non-ff FAILURE (push_attempts=1, no
         rebase) and the cmd_file loop re-allocates a fresh ID — rebasing an
         add/add collision would be wrong. commit_scoped's push is fixed at
         push_with_retry's default 3-attempt rebase-on-nonff.
      3. Push-side board conflict_resolver. commit_scoped threads its
         conflict_resolver only into the sync (pull) step, not push_with_retry.
      Plus: commit_scoped's leak-only guard runs WITHOUT `--no-renames`, so its
      correctness for a rename would hinge on git's similarity heuristic;
      this pipeline's strict-equality + `--no-renames` guard is a deliberate
      stronger invariant for the most critical commits in the system.
    Convergence value (dedup of the COMMON pattern) is captured by the 9
    conforming callers; this outlier stays on its proven inline impl.

    Order: HERMES guard on each affected file -> HERMES belt on commit
    message -> ff-only pull -> rebuild README table -> stage exact paths ->
    diff-cached scope-set assert -> commit -> push (skipped when no_push).
    Each step fails fast with TicketError.

    rebase_on_nonff: when True (resolve/block/retarget — EDIT ops on an existing
    ticket), a non-ff push is reconciled by fetch+rebase onto fresh origin/main
    and retried — this is the T-0846 fix that stops stranding ratification
    resolves. When False (the FILE path), a non-ff push is NOT rebased: for a
    brand-new ticket a non-ff means a racing agent grabbed the same ID, and the
    correct response is to RE-ALLOCATE a fresh ID (cmd_file's retry loop), not to
    rebase an add/add-conflicting commit. Rebasing the FILE path silently bypasses
    re-allocation in the no-conflict case and add/add-conflicts in the collision
    case; a single plain push that surfaces the non-ff lets the loop re-allocate.
    """
    # 1. HERMES guard on every affected file that exists on disk now.
    for p in affected_paths:
        ok, detail = _hermes_scan_file(p)
        if not ok:
            raise TicketRefusal(f"HERMES guard refused: {detail}")

    # 2. HERMES belt on commit message.
    if not _hermes_scan_message(commit_message):
        raise TicketRefusal("HERMES guard refused: commit message construction")

    # 3. (Pull-first removed in T-0244.) Pull is now done by callers BEFORE any
    #    filesystem changes (_do_transition, cmd_file, cmd_retarget), so a pull
    #    failure can't leave the working tree in an inconsistent state where the
    #    file has been moved/edited but no commit happened. See _pull_if_needed.

    # 3a. Rebuild README table from current filesystem state. Only add README
    #     to affected_paths if the rebuild actually changed content — post-
    #     ORG-019 block-subcommand edits frontmatter in place without moving
    #     the ticket between dirs, so the table is often byte-identical and
    #     staging README would fail the step-5 scope guard with a no-op delta.
    readme_path = REPO_ROOT / "tickets" / "README.md"
    readme_changed = _rebuild_readme_table(TICKETS_ROOT, readme_path)
    if readme_changed:
        # HERMES guard on the freshly written README
        ok, detail = _hermes_scan_file(readme_path)
        if not ok:
            raise TicketError(f"HERMES guard refused (README): {detail}")
        affected_paths.append(readme_path)

    # 4-6. Stage exactly the affected paths, assert the staged set is exactly our
    #      scope, and commit — the audited scope-guarded transactional commit,
    #      extracted to _stage_and_commit_scoped (ORG-088 T3 Q1) so the role-
    #      assignment executor's atomic rebind + receipt sidecar reuse this SAME
    #      path. BEHAVIOR-PRESERVING: the extraction is a pure move of the former
    #      inline steps 4-6; _commit_and_push still runs HERMES (steps 1-2),
    #      README rebuild (3a), and push (step 7) around it.
    _stage_and_commit_scoped(
        affected_paths, commit_message, require_added=require_added
    )

    # 7. Push with rebase-on-non-ff retry (skipped when --no-push; the next
    #    pushing op carries the local-ahead state — the 60s sync timer that
    #    formerly did this was retired T-0365). T-0846: a PLAIN push strands the commit
    #    when origin advanced between the pre-pull and the push (the
    #    PWA-ratification-stranding class — an interactive resolve commits, the
    #    push is rejected non-ff, and the commit sits local-only). push_with_retry
    #    fetches + rebases onto fresh origin/main and retries; the board resolver
    #    keeps the generated tickets/README from blocking that rebase.
    if no_push:
        return
    # rebase_on_nonff=False (FILE path) -> attempts=1: a single push, no rebase.
    # A non-ff then surfaces as a "git push failed" so cmd_file's loop re-allocates
    # a fresh ticket ID rather than rebasing a same-ID add/add collision.
    push_attempts = 3 if rebase_on_nonff else 1
    ok, detail = push_with_retry(
        REPO_ROOT,
        refspec="HEAD:main",
        attempts=push_attempts,
        conflict_resolver=_board_conflict_resolver if rebase_on_nonff else None,
    )
    if not ok:
        raise TicketError(f"git push failed: {detail}")


# ---------------------------------------------------------------------------
# Subcommand handlers.
# ---------------------------------------------------------------------------


def _parse_csv_list(raw: str | None) -> list[str]:
    if raw is None or raw.strip() == "":
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def _str_arg(args: argparse.Namespace, name: str) -> str | None:
    """Return args.<name> if it is a str, else None.

    Same isinstance-guard `getattr(args, "parent_ticket", None)` + `isinstance(
    ..., str)` already uses just below — factored out for the T-2841 typed-edge
    flags for the same reason that precedent guards `parent_ticket`: `_parse_
    csv_list`'s own contract is `raw: str | None`, and this is the one place
    that boundary is enforced before a non-str, non-None value can reach it.
    (Empirically, a MagicMock happens to survive `_parse_csv_list` unguarded —
    MagicMock auto-configures `__iter__` to `iter([])`, so `raw.split(",")` on
    an unset MagicMock attribute silently yields `[]` rather than raising. That
    is a MagicMock implementation detail, not a documented one, and it doesn't
    generalize — a real caller passing an int or a list would still hit
    `raw.strip()`/`raw.split(",")` and raise. The guard's job is enforcing the
    real type contract, not working around a mock quirk that happens not to
    bite today.)
    """
    raw = getattr(args, name, None)
    return raw if isinstance(raw, str) else None


_MAX_FILE_RETRIES = 3


_NEAR_DUPLICATE_WINDOW_MINUTES = 30
_NEAR_DUPLICATE_MIN_SHARED_LABELS = 2


def _parse_iso(raw: str) -> datetime:
    """Parse a ticket `created:` stamp. Trailing `Z` → `+00:00` (fromisoformat
    only learned to accept `Z` in 3.11, and these stamps are all `Z`-suffixed)."""
    return datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))


def _recent_neighbours(labels: list[str], now_iso: str, *,
                       window_minutes: int = _NEAR_DUPLICATE_WINDOW_MINUTES,
                       min_shared: int = _NEAR_DUPLICATE_MIN_SHARED_LABELS,
                       exclude: str | None = None,
                       ) -> list[tuple[str, str, list[str]]]:
    """Tickets filed in the last `window_minutes` sharing >= `min_shared` labels.

    T-2260. Twice in one night two agents filed the same finding four minutes
    apart, each having looked at the same surface and written up what they
    found rather than assuming the other would — which is the behaviour we
    want. The gap is that `file` writes into inbox/ with no awareness of what
    landed a moment ago, and neither author had reason to poll.

    A WARNING, NEVER A BLOCK, and that is a design constraint rather than
    caution: sibling tickets deliberately split from one arc are routine here
    and share labels by construction, so a blocking check would fire on the
    behaviour it is meant to protect. Label overlap plus a time window is an
    admitted PROXY for "same finding"; it is enough because the reader is an
    author who already knows what they just wrote and can dismiss it in a
    second. Deliberately no semantic similarity and no model call — a proxy
    that surfaces the neighbour beats a cleverer one that adjudicates.

    Returns (tid, title, shared_labels), newest first. Never raises: a
    scan failure must not cost a filing.
    """
    mine = {l.strip().lower() for l in labels if l.strip()}
    if len(mine) < min_shared:
        return []
    try:
        cutoff = _parse_iso(now_iso) - timedelta(minutes=window_minutes)
    except Exception:
        return []

    out: list[tuple[str, str, list[str], str]] = []
    for state in STATES:
        d = TICKETS_ROOT / state
        if not d.is_dir():
            continue
        for path in d.glob("T-*.md"):
            if exclude is not None and path.stem == exclude:
                continue
            try:
                fields, body = parse_front_matter(path.read_text(encoding="utf-8"))
                created = fields.get("created")
                if not isinstance(created, str):
                    continue
                if _parse_iso(created) < cutoff:
                    continue
                theirs = {str(l).strip().lower()
                          for l in (fields.get("labels") or []) if str(l).strip()}
                shared = sorted(mine & theirs)
                if len(shared) >= min_shared:
                    out.append((path.stem, _extract_title(body), shared, created))
            except Exception:
                continue
    out.sort(key=lambda r: r[3], reverse=True)
    return [(tid, title, shared) for tid, title, shared, _ in out]


def _warn_near_duplicates(labels: list[str], now_iso: str,
                          *, exclude: str | None = None) -> None:
    """Print recent label-overlapping neighbours to stderr. Never raises."""
    try:
        neighbours = _recent_neighbours(labels, now_iso, exclude=exclude)
    except Exception:
        return
    if not neighbours:
        return
    print(
        f"note: {len(neighbours)} ticket(s) filed in the last "
        f"{_NEAR_DUPLICATE_WINDOW_MINUTES} min share >= "
        f"{_NEAR_DUPLICATE_MIN_SHARED_LABELS} labels with this one. "
        f"Not a duplicate check — just putting the neighbours in front of you:",
        file=sys.stderr,
    )
    for tid, title, shared in neighbours:
        print(f"  {tid}  [{','.join(shared)}]  {title}", file=sys.stderr)


def _purpose_values_from_args(args: argparse.Namespace) -> tuple[bool, str | None, str | None]:
    """Return (project field present, normalized project id, typed intent ref).

    ``--project-id null`` is an explicit root declaration and is therefore
    distinct from an omitted option.  Typed-ref syntax is always fail-closed,
    even while corpus/existence migration remains warning-only.
    """
    raw_project = getattr(args, "project_id", None)
    project_present = isinstance(raw_project, str)
    project_id: str | None = None
    if project_present:
        normalized = raw_project.strip()
        if normalized.lower() != "null":
            from tools.project_catalogue import PROJECT_ID_RE

            if not PROJECT_ID_RE.fullmatch(normalized):
                raise TicketRefusal(f"--project-id has invalid stable id: {raw_project!r}")
            project_id = normalized
    raw_ref = getattr(args, "nearest_intent_ref", None)
    nearest = raw_ref.strip() if isinstance(raw_ref, str) and raw_ref.strip() else None
    if nearest is not None:
        from tools.ticket_purpose import PurposeError, parse_intent_ref

        try:
            parse_intent_ref(nearest)
        except PurposeError as exc:
            raise TicketRefusal(f"--nearest-intent-ref: {exc}") from exc
    return project_present, project_id, nearest


def _purpose_issues_for_fields(fields: dict, *, mode) -> tuple:
    """Validate candidate fields against the on-disk ticket/project graph."""
    from tools.goal_registry import registry_status
    from tools.project_catalogue import load_project_records
    from tools.ticket_purpose import (
        TicketPurpose,
        load_ticket_purpose_index,
        validate_new_ticket_purpose,
        with_goal_registry_reason,
    )
    tid = str(fields["id"])
    purposes = load_ticket_purpose_index(TICKETS_ROOT)
    candidate = TicketPurpose(
        ticket_id=tid,
        project_id=fields.get("project_id"),
        project_id_present="project_id" in fields,
        nearest_intent_ref=fields.get("nearest_intent_ref"),
        serves=fields.get("serves"),
        labels=frozenset(str(label) for label in (fields.get("labels") or [])),
        state=str(fields.get("state") or "inbox"),
    )
    purposes[tid] = candidate
    records = load_project_records(REPO_ROOT / "projects" / "records")
    # T-5679: the canonical goal keys come from the goals.md markers. A
    # missing/unparseable registry yields an empty set, and the reason is
    # carried onto any goal-registry-unavailable issue rather than dropped.
    goal_keys, registry_error = registry_status(REPO_ROOT)
    return with_goal_registry_reason(
        validate_new_ticket_purpose(candidate, purposes, records, goal_keys, mode),
        registry_error,
    )


def _report_purpose_warnings(issues: tuple) -> None:
    for issue in issues:
        print(
            f"[ticket-purpose] WARN {issue.ticket_id} {issue.code}: {issue.detail}",
            file=sys.stderr,
        )


def _apply_purpose_policy(issues: tuple, *, mode) -> None:
    if not issues:
        return
    if mode.value == "enforce":
        detail = "; ".join(f"{issue.code}: {issue.detail}" for issue in issues)
        raise TicketRefusal(f"structured ticket purpose refused: {detail}")
    _report_purpose_warnings(issues)


def _ensure_project_label_justifies_filing(
    project_id: str | None,
    labels: list[str],
    nearest_intent_ref: str | None,
    *,
    mode,
) -> list[str]:
    """T-4090: auto-add `project_id` as a label when it would not otherwise
    be justified (`tp.project_id_justified` -- no exact-label match, no
    `ticket:`-chain hop to a same-project ancestor). The filer already named
    the project; the label is how that choice gets recorded, not an
    inference. This is what closes T-4080's gap: `ticket.py file` used to
    accept an unjustified `project_id` silently, and only the live-corpus
    test (tools/test_ticket_purpose.py) caught it -- after main was already
    red. Shared by `cmd_file` and `cmd_purpose`, the two write paths that can
    set `project_id`.

    Nonexistent project record (no `projects/records/<id>.yaml`): follows
    the caller-supplied `mode` (derived from `TICKET_PURPOSE_ENFORCEMENT`)
    rather than a fixed policy of its own. WARN prints a loud stderr note and
    still falls through to the auto-add below (T-4090 plan-review: the live
    corpus already carries a project_id with no backing record, justified by
    label alone under warn mode -- an unconditional refusal here would be
    STRICTER than the repo's current migration-leg policy and would silently
    flip it). ENFORCE raises TicketError.

    Returns `labels` unchanged when `project_id` is None or already
    justified; otherwise the extended list. Pure aside from the
    projects/records + live ticket-purpose-index reads and the stderr notes.
    """
    if project_id is None:
        return labels
    from tools.project_catalogue import load_project_records
    from tools.ticket_purpose import (
        PurposeEnforcement,
        TicketPurpose,
        load_ticket_purpose_index,
        project_id_justified,
    )

    records = load_project_records(REPO_ROOT / "projects" / "records")
    if project_id not in records:
        detail = (
            f"--project-id {project_id!r} has no projects/records/"
            f"{project_id}.yaml -- create the project record, or pass "
            f"--project-id null"
        )
        if mode is PurposeEnforcement.ENFORCE:
            raise TicketRefusal(detail)
        print(f"[ticket-purpose] WARNING: {detail}", file=sys.stderr)

    candidate = TicketPurpose(
        ticket_id="<pending>",
        project_id=project_id,
        project_id_present=True,
        nearest_intent_ref=nearest_intent_ref,
        serves=None,
        labels=frozenset(labels),
        state="inbox",
    )
    purposes = load_ticket_purpose_index(TICKETS_ROOT)
    if project_id_justified(candidate, purposes):
        return labels
    print(
        f"[ticket-purpose] auto-added label {project_id!r} to justify "
        f"project_id={project_id!r} (T-4090: no exact-label match and no "
        f"ticket:-chain hop to a same-project ancestor)",
        file=sys.stderr,
    )
    return labels + [project_id]


def _load_repair_fields(path_arg: str) -> dict:
    """T-4707 / RFC-004 §3(a): load + validate `--repair-fields-json <path>`.

    Closed allowlist (REPAIR_FIELD_SPEC): unknown keys, wrong types, missing
    required keys, multi-line / list-looking scalars are all refused BEFORE any
    mutation. Returns frontmatter-ready values (bools as `true`/`false`,
    attempts_budget defaulted to 2). `evidence_snapshot` is a block scalar and is
    simply omitted when absent (the deferred case, RFC-004 §6.16)."""
    import json as _json

    try:
        raw = _json.loads(Path(path_arg).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TicketRefusal(f"--repair-fields-json unreadable/invalid JSON: {exc}")
    if not isinstance(raw, dict):
        raise TicketRefusal("--repair-fields-json must contain a JSON object")
    unknown = sorted(set(raw) - set(REPAIR_FIELD_SPEC))
    if unknown:
        raise TicketRefusal(f"--repair-fields-json unknown key(s): {unknown}")
    missing = [k for k, (_, req) in REPAIR_FIELD_SPEC.items() if req and k not in raw]
    if missing:
        raise TicketRefusal(f"--repair-fields-json missing required key(s): {missing}")
    out: dict = {}
    for key, (kind, _req) in REPAIR_FIELD_SPEC.items():
        if key not in raw:
            continue
        val = raw[key]
        if kind == "bool":
            if not isinstance(val, bool):
                raise TicketRefusal(f"--repair-fields-json {key} must be a boolean")
            out[key] = "true" if val else "false"
        elif kind == "int":
            if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                raise TicketRefusal(f"--repair-fields-json {key} must be a non-negative integer")
            out[key] = str(val)
        elif kind == "block":
            if not isinstance(val, str) or not val.strip():
                raise TicketRefusal(f"--repair-fields-json {key} must be a non-empty string (omit the key instead)")
            if any(c in val for c in ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")):
                raise TicketRefusal(f"--repair-fields-json {key} contains non-newline line separators")
            out[key] = val
        else:  # str / enum
            if not isinstance(val, str) or not val.strip():
                raise TicketRefusal(f"--repair-fields-json {key} must be a non-empty string")
            if "\n" in val or "\r" in val or val != val.strip() or val[0] in "[|{>'\"#&*!%@`":
                raise TicketRefusal(f"--repair-fields-json {key} must be a single-line plain scalar")
            if kind.startswith("enum:") and val not in kind[5:].split(","):
                raise TicketRefusal(f"--repair-fields-json {key} must be one of {kind[5:]}")
            out[key] = val
    out.setdefault("attempts_budget", str(REPAIR_ATTEMPTS_BUDGET_DEFAULT))
    return out


def _refuse_if_episode_committed(episode_id: str) -> None:
    """T-4707 / RFC-004 §2: episode_id uniqueness against the COMMITTED corpus (origin/main
    tip + local-ahead commits), fail-closed when the query cannot be answered. Only runs
    when `--repair-fields-json` is passed -- existing invocations pay no extra git cost."""
    from tools import _episode_corpus

    res = _episode_corpus.lookup_episode(episode_id, repo=REPO_ROOT, fetch=True)
    if res.status == _episode_corpus.FOUND:
        raise TicketRefusal(
            f"episode_id {episode_id!r} is already committed ({res.where}"
            f"{' ' + res.sha if res.sha else ''}); refusing to file a duplicate (RFC-004 section 2)"
        )
    if res.status == _episode_corpus.UNKNOWN:
        raise TicketRefusal(
            f"cannot verify episode_id {episode_id!r} is unique against the committed corpus "
            f"({res.detail}); refusing (fail-closed, RFC-004 section 2)"
        )


def _security_gate_fields(*, title, ask, labels, threat_chain=None,
                          path_justification=None, not_security=None,
                          automated_filer=None) -> dict:
    """T-5678: run the ORG-105 gate; GateRefusal -> TicketRefusal (exit 3)."""
    try:
        return _security_gate.evaluate(
            title=title,
            ask=ask if isinstance(ask, str) else None,
            labels=labels if isinstance(labels, list) else [],
            threat_chain=threat_chain if isinstance(threat_chain, str) else None,
            path_justification=path_justification if isinstance(path_justification, str) else None,
            not_security=not_security if isinstance(not_security, str) else None,
            automated_filer=automated_filer if isinstance(automated_filer, str) else None,
        )
    except _security_gate.GateRefusal as exc:
        raise TicketRefusal(str(exc)) from None


def cmd_file(args: argparse.Namespace) -> int:
    from tools.ticket_purpose import PurposeEnforcement

    title = args.title.strip()
    if not title:
        raise TicketRefusal("--title must not be empty")
    requester = args.requester.strip()
    target = args.target.strip()

    # T-0306: validate target against agent_role_map + operator. Same check as
    # cmd_retarget — prevents target=echo (or any unknown) from slipping
    # through and producing tickets with no consumer (precedent: T-0109 +
    # T-0110 sat 6 days because target=echo has no session).
    #
    # T-3761 / ORG-088 Plane-A: _resolve_or_reject_target also accepts a
    # `role:<R>` pool target iff R is a known functional role_id, refusing
    # fail-closed on an unknown role or an unverifiable membership vocabulary
    # (a write must never pool a ticket to an unresolvable role). A role target
    # is persisted unchanged; a plain persona/role-id is normalized as before.
    target = _resolve_or_reject_target(target)

    # T-0492: normalize role-ID inputs to persona names on write.
    # Persona names are canonical for ticket target/requester (msg_1661).
    # "orchestrator" → "orchestrator", "head_research_orchestrator" → "specialist" (legacy
    # "specialist" → "specialist" too); others pass through.
    # (requester is never a role: pool target — normalized directly.)
    # T-4499: requester is validated against the same persona/role vocabulary
    # (plus fleet OS-account names), so `--requester=-x` is refused, not stored.
    # T-4044: requester is provenance, not a delivery vector — so a bare
    # non-injective role-class id (e.g. engineering_orchestrator) is still
    # ACCEPTED here; _resolve_persona passes it through UNCHANGED (records the
    # honest role-class token) rather than silently misattributing to artisan. The
    # non-injective REFUSAL is target-only.
    requester = _resolve_requester(requester)

    # Fields that don't change across retry attempts.
    created = _now_iso()
    labels = _parse_csv_list(args.labels)
    depends_on = _parse_csv_list(args.depends_on)
    for dep in depends_on:  # T-4499: same shape check --parent-ticket gets, per element
        _require_ticket_id_shape("--depends-on", dep)
    # T-2841 / ORG-065 BT1: the four new typed-edge fields. Shape-validated
    # up front (same place depends_on/labels are parsed) — malformed shape is
    # a refusal; an unresolvable-but-well-formed target is NOT (see
    # _parse_edge_targets docstring).
    implements = _parse_edge_targets("implements", _parse_csv_list(_str_arg(args, "implements")))
    verifies = _parse_edge_targets("verifies", _parse_csv_list(_str_arg(args, "verifies")))
    supersedes = _parse_edge_targets("supersedes", _parse_csv_list(_str_arg(args, "supersedes")))
    informs = _parse_edge_targets("informs", _parse_csv_list(_str_arg(args, "informs")))
    # T-3476 / ORG-065 Call 1: fifth typed-edge field, same shape as the four
    # above. Distinct from `implements` — see FRONT_MATTER_FIELD_ORDER note.
    ratifies = _parse_edge_targets("ratifies", _parse_csv_list(_str_arg(args, "ratifies")))
    raw_parent = getattr(args, "parent_ticket", None)
    parent_ticket: str | None = None
    if isinstance(raw_parent, str) and raw_parent.strip():
        parent_ticket = raw_parent.strip()
        _require_ticket_id_shape("--parent-ticket", parent_ticket)  # T-3215
    actor = _actor()
    project_id_present, project_id, nearest_intent_ref = _purpose_values_from_args(args)

    # T-4090: auto-add the justifying project label (or, in enforce mode,
    # refuse a nonexistent --project-id) BEFORE the retry loop below — a
    # single, idempotent pass, same fail-fast-before-mutation shape as the
    # T-3328/T-0852 guards that follow it. `purpose_mode` is also reused by
    # the general purpose-graph check inside the loop.
    purpose_mode = PurposeEnforcement(TICKET_PURPOSE_ENFORCEMENT)
    labels = _ensure_project_label_justifies_filing(
        project_id, labels, nearest_intent_ref, mode=purpose_mode
    )

    # --- T-5678 / ORG-105 s4: security-ticket gate (resolved once, pre-loop) ---
    # Fail-fast before any mutation: a refusal leaves no trace. str-only reads so
    # MagicMock'd Namespaces in other suites stay inert.
    gate_fields = _security_gate_fields(
        title=title, ask=getattr(args, "ask", None), labels=labels,
        threat_chain=getattr(args, "threat_chain", None),
        path_justification=getattr(args, "path_justification", None),
        not_security=getattr(args, "not_security", None),
        automated_filer=getattr(args, "automated_filer", None),
    )

    # --- T-3328: file-time lineage-guard (resolved once, pre-loop) ---
    # A first-class ticket DAG needs every node connected — a new ticket must
    # carry >=1 lineage edge or explicitly assert (via --orphan) that it is a
    # genuine root. Checked here (after all 7 edge fields are known, before
    # any mutation) so a refusal leaves no trace — same fail-fast-before-
    # mutation shape as _reject_if_cycle below.
    #
    # Heterogeneous node model (T-3328, Specialist's 2026-08-28 13:51:39Z scope
    # refinement): the graph root is the Grand Goal; goals/ADRs/tickets are
    # all nodes on ONE graph. This guard does not add new fields for that —
    # the existing edges already reach every node type:
    #   serves      -> a goal/arc-intent (ORG-034; traces to rulebook/goals.md)
    #   implements/verifies/supersedes/informs -> a ticket OR an ADR
    #       obligation (ADR-NNN#E<k>[.C<m>] / ORG-NNN#E<k>[.C<m>]) — already
    #       accepted by _parse_edge_targets (T-2841/ORG-065 BT1), unchanged.
    #   parent_ticket/depends_on -> another ticket.
    # A genuine one-off with no goal alignment is still connected, not
    # orphaned: use --serves goal-neutral-maintenance (or a maintenance:<asset>
    # --nearest-intent-ref) for housekeeping, and --nearest-intent-ref
    # ticket:T-5677 plus a descriptive --serves (which requires --appetite,
    # per ORG-089: ad-hoc asks are bets) for the operator's ungoaled asks. T-5677 is the designated
    # operational-root ticket (T-3332 (b), ratified by orchestrator 2026-10-27; the
    # registry is the ORG-054 ref kinds plus tickets labelled operational-root).
    # The free-text 'operator-ad-hoc' serves value is legacy. Existence of a
    # referenced goal/ADR/ticket
    # is NOT validated at file-time — same warn-only-at-grooming precedent
    # already established for implements/verifies/supersedes/informs.
    #
    # NOTE: distinct from ticket_lint.py's `orphan_ratifications` (a
    # ratifiable ticket with no open ratify ticket) — unrelated concept,
    # shares only the English word.
    raw_serves_for_guard = getattr(args, "serves", None)
    _has_serves = isinstance(raw_serves_for_guard, str) and bool(raw_serves_for_guard.strip())
    raw_orphan = getattr(args, "orphan", None)
    orphan_reason: str | None = raw_orphan.strip() if isinstance(raw_orphan, str) else None

    has_lineage_edge = bool(
        parent_ticket
        or depends_on
        or _has_serves
        or nearest_intent_ref
        or project_id
        or implements
        or verifies
        or supersedes
        or informs
        or ratifies
    )
    if not has_lineage_edge:
        if orphan_reason is None:
            raise TicketRefusal(
                "refusing to file an orphan ticket: no lineage edge is set "
                "(T-3328 — every new ticket must connect to the DAG). Add "
                ">=1 of:\n"
                "  --parent-ticket T-NNNN     this ticket's parent\n"
                "  --depends-on T-NNNN[,...]  ticket(s) that gate this one\n"
                "  --serves \"<phrase>\"        the goal/arc-intent this ticket "
                "serves (ORG-034) — traces to a rulebook/goals.md north star; "
                "use 'goal-neutral-maintenance' for housekeeping; legacy "
                "'operator-ad-hoc' (operator-ad-hoc, like any serves other than "
                "goal-neutral-maintenance, also needs --appetite <1-10> — "
                "ORG-089)\n"
                "  --nearest-intent-ref ticket:T-5677  a genuine one-off with "
                "no goal (ad-hoc the operator ask): the operational-root ticket "
                "(T-3332), plus a descriptive --serves (which requires "
                "--appetite <1-10>, per ORG-089)\n"
                "  --implements <T-NNNN|ADR-NNN#E<k>>  the parent work or ADR "
                "obligation this ticket implements\n"
                "  --verifies <T-NNNN|ADR-NNN#E<k>>    the obligation this "
                "ticket's evidence verifies\n"
                "  --supersedes <T-NNNN|ADR-NNN#E<k>>  the node this ticket "
                "replaces\n"
                "  --informs <T-NNNN|ADR-NNN#E<k>>     advisory-only notify "
                "edge (never gates)\n"
                "  --ratifies <T-NNNN|ADR-NNN#E<k>>    the ADR obligation "
                "this ticket ratifies (distinct from implements — T-3476)\n"
                "Escape hatch: --orphan \"<justification, >=20 chars>\" asserts "
                "this is a genuine root with no natural connection — rare; "
                "recorded as orphan_reason: for audit."
            )
        if len(orphan_reason) < 20:
            raise TicketRefusal(
                f"--orphan justification too short ({len(orphan_reason)} chars, "
                f"minimum 20): {orphan_reason!r}"
            )

    # --- T-0852: ratify file-time impl-followup gate (resolved once, pre-loop) ---
    # A target=operator + intent=ratify file must not dead-end: it carries either a
    # spawned impl child (gated on this ratify, cascade-unblocked on the operator's
    # ratify) or an explicit --terminal assertion. Keyed on the EFFECTIVE intent so
    # the auto-default-to-ratify path (--ratifiable-file, no --intent) is also gated.
    spawn_impl = getattr(args, "spawn_impl", False)
    terminal = getattr(args, "terminal", False)
    impl_title = getattr(args, "impl_title", None)
    impl_ask = getattr(args, "impl_ask", None)
    impl_target = getattr(args, "impl_target", None)
    # T-5291: link an ALREADY-FILED impl ticket instead of spawning a duplicate.
    # str-only (MagicMock'd Namespaces in tests would otherwise read as truthy).
    _raw_impl_existing = getattr(args, "impl_existing", None)
    impl_existing: str | None = (
        _raw_impl_existing.strip()
        if isinstance(_raw_impl_existing, str) and _raw_impl_existing.strip()
        else None
    )

    _explicit_intent = (
        args.intent.strip()
        if isinstance(getattr(args, "intent", None), str) and args.intent.strip()
        else None
    )
    _has_ratifiable = bool(getattr(args, "ratifiable_file", None))
    # T-5014: target=operator forced choice (attach a file OR --no-file reason) and
    # attachment resolution. Pre-loop: pure validation, no writes, no lock held.
    _pre_files = _parse_ratifiable_files(
        [e.strip() for e in (getattr(args, "ratifiable_file", None) or [])]
    )
    _refuse_excluded_ratifiable_class(_pre_files)  # T-6071 / ORG-015
    no_file_reason = _operator_attachment_gate(
        target, _pre_files, getattr(args, "no_file", None),
        fix_prefix="ticket.py file",
    )
    if target == "operator" and _pre_files:
        _check_ratifiable_resolve(_pre_files)
    _effective_intent = (
        (_explicit_intent or ("ratify" if _has_ratifiable else "action"))
        if target == "operator"
        else _explicit_intent
    )
    gate_applies = target == "operator" and _effective_intent == "ratify"

    ask_text = args.ask or ""
    if gate_applies:
        _chosen = [
            flag for flag, on in (
                ("--spawn-impl", spawn_impl),
                ("--terminal", terminal),
                ("--impl-existing", impl_existing is not None),
            ) if on
        ]
        if len(_chosen) > 1:
            raise TicketRefusal(
                f"{' and '.join(_chosen)} are mutually exclusive: pass exactly one of "
                f"--spawn-impl / --terminal / --impl-existing (T-0852, T-5291)"
            )
        if not _chosen:
            raise TicketRefusal(
                "ratify ticket (target=operator, intent=ratify) must not dead-end: "
                "pass --spawn-impl (+ --impl-title) to atomically file an "
                "implementation ticket gated on this ratify, --impl-existing T-NNNN "
                "to gate an already-filed implementation ticket on it, or --terminal "
                "to assert no implementation follow-up is needed (T-0852, T-5291)"
            )
        if impl_existing is not None:
            _validate_impl_existing(impl_existing, args)
            if "ratify-impl-existing" not in labels:
                labels.append("ratify-impl-existing")
            ask_text = (
                f"{ask_text}\n\n[T-0852] implementation tracked by existing "
                f"{impl_existing} (gated on this ratify via depends_on; T-5291)."
            ).strip()
        if spawn_impl:
            if not (isinstance(impl_title, str) and impl_title.strip()):
                raise TicketRefusal("--spawn-impl requires --impl-title (T-0852)")
            impl_title = impl_title.strip()
            impl_target = (
                impl_target.strip()
                if isinstance(impl_target, str) and impl_target.strip()
                else requester
            )
            # T-5239: a defaulted impl target (the requester) is DERIVED -> its pool
            # role; an explicit --impl-target goes through the write-side refusal.
            if getattr(args, "impl_target", None) and str(args.impl_target).strip():
                impl_target = _resolve_or_reject_target(impl_target)
            else:
                impl_target = _derive_pool_target(impl_target)
        if terminal:
            if "ratify-terminal" not in labels:
                labels.append("ratify-terminal")
            ask_text = (
                f"{ask_text}\n\n[T-0852] TERMINAL ratify: filer asserts this "
                f"ratification needs no implementation follow-up."
            ).strip()
    elif spawn_impl or terminal or impl_existing is not None:
        raise TicketRefusal(
            "--spawn-impl/--terminal/--impl-existing are ratify-only "
            "(target=operator + intent=ratify); "
            f"refusing for target={target!r} intent={_effective_intent!r} (T-0852)"
        )

    body = _build_initial_body(title, ask_text, args.context or "", actor)

    # T-4707 / RFC-004 section 3(a): runtime repair-ticket attributes (validated and
    # uniqueness-checked before any mutation; None for every existing invocation).
    repair_fields: dict | None = None
    _repair_path = getattr(args, "repair_fields_json", None)
    if isinstance(_repair_path, str) and _repair_path:  # str-only: MagicMock'd Namespaces in tests
        repair_fields = _load_repair_fields(_repair_path)
        _refuse_if_episode_committed(repair_fields["episode_id"])

    last_push_err: str | None = None
    new_path: Path | None = None
    child_path: Path | None = None
    # T-5291: the --impl-existing ticket is MODIFIED (not added) in the parent's
    # commit; its pre-edit bytes are kept so a retry restores it before re-binding.
    existing_path: Path | None = None
    existing_orig: bytes | None = None
    committed = False  # T-5291 r2: once committed, the undo must never fire

    def _undo_impl_existing_link() -> None:
        """T-5291 r1: a NON-push failure after the link (HERMES file/message
        guard, scope guard, commit failure, anything unexpected) must not leave
        the existing impl ticket pointing at a never-committed tid. Unstage the
        attempt's paths, restore the existing ticket byte-exact, drop the parent.
        Best-effort per step: never mask the original error. No-op once the
        commit has landed: a post-commit failure (a print, the near-duplicate
        warning) must never revert a committed/pushed edit locally."""
        if committed or existing_path is None or existing_orig is None:
            return
        for p_ in (existing_path, new_path):
            try:
                _git("reset", "-q", "HEAD", "--", str(p_.relative_to(REPO_ROOT)))
            except Exception:  # noqa: BLE001 - never mask the original error
                pass
        try:
            existing_path.write_bytes(existing_orig)
        except Exception:  # noqa: BLE001
            pass
        try:
            if new_path is not None and new_path.exists():
                new_path.unlink()
        except Exception:  # noqa: BLE001
            pass

    def _rollback_uncommitted_attempt() -> None:
        """T-5316: roll back an attempt that failed WITHOUT committing (README
        HERMES guard, scope guard, a refused commit). Left alone, the regenerated
        tickets/README.md (a row for a ticket that never landed) and the staged
        new ticket file(s) make the next ticket op's scope guard refuse. The
        push-failure path already does this (T-2172).

        Only files this attempt STAGED AS ADDED are unstaged and deleted, so a
        same-id foreign file (T-4885 collision) or an author's untracked draft is
        never touched. The README is derived state, rebuilt on the next op.
        Best-effort; never masks the original error; no-op once committed."""
        if committed:
            return
        for p_ in (new_path, child_path):
            if p_ is None:
                continue
            rel = str(p_.relative_to(REPO_ROOT))
            try:
                staged_add = _git(
                    "diff", "--cached", "--name-only", "--diff-filter=A", "--", rel
                ).stdout.strip()
                if staged_add:
                    _git("reset", "-q", "HEAD", "--", rel)
                    p_.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001 - never mask the original error
                pass
        for cmd in (
            ("reset", "-q", "HEAD", "--", "tickets/README.md"),
            ("checkout", "--", "tickets/README.md"),
        ):
            try:
                _git(*cmd)
            except Exception:  # noqa: BLE001 - never mask the original error
                pass

    for attempt in range(_MAX_FILE_RETRIES):
        # T-5406: ONE git_mutation_lock span per attempt, outermost, covering the
        # pull, the allocate->write (+ the T-5291 tracked edit of an existing impl
        # ticket), the commit/publish and the failure undo. Previously the first
        # half ran under only _allocation_lock, leaving a tracked modification
        # between two spans that a concurrent `pull --rebase` could wedge on, and
        # the failure-path `reset HEAD~1` ran unlocked. LOCK ORDER is git then
        # alloc, matching close-noodle/close-action (_do_transition's lock ->
        # _spawn_child_ticket's _allocation_lock); alloc-then-git would be ABBA.
        # git_mutation_lock is NOT re-entrant: nothing inside may take it again.
        # The lock is released between attempts (a retry re-takes it).
        with git_mutation_lock(label="file"):
            # The _allocation_lock() context spans allocate→write so no other
            # writer can scan the same max between our _next_ticket_id() call
            # and the moment we land the file on disk (T-0786).  (T-5406: this
            # now nests inside the git lock above and is released first, at the
            # end of the allocate->write block.)  _next_ticket_id fetches origin on every attempt,
            # ensuring a fresh remote view even after a prior push failure
            # bumped origin's max.
            with _allocation_lock():
                # Pull-first (T-0244, reordered T-4885): pull BEFORE allocating so the
                # allocation scan sees whatever the pull brings in. Must succeed
                # before any write, so a pull failure leaves no orphaned file.
                # Skipped when no_push=True.
                no_push = getattr(args, "no_push", False)
                _pull_if_needed(no_push)

                tid = _next_ticket_id()
                fields: dict = {
                    "id": tid,
                    "created": created,
                    "requester": requester,
                    "target": target,
                    "state": "inbox",
                    "parent_ticket": parent_ticket,
                    "depends_on": depends_on,
                }
                if project_id_present:
                    fields["project_id"] = project_id
                if nearest_intent_ref is not None:
                    fields["nearest_intent_ref"] = nearest_intent_ref
                # T-2841 / ORG-065 BT1.
                if implements:
                    fields["implements"] = implements
                if verifies:
                    fields["verifies"] = verifies
                if supersedes:
                    fields["supersedes"] = supersedes
                if informs:
                    fields["informs"] = informs
                if ratifies:
                    fields["ratifies"] = ratifies
                # T-3328: record the escape-hatch justification whenever supplied
                # (even alongside a real edge — harmless, and useful audit either way).
                if orphan_reason:
                    fields["orphan_reason"] = orphan_reason
                if no_file_reason:
                    fields["no_file_reason"] = no_file_reason  # T-5014
                if labels:
                    fields["labels"] = labels
                fields.update(gate_fields)  # T-5678
                if args.stale_after:
                    fields["stale_after"] = args.stale_after.strip()
                if args.not_before:
                    # T-2833: a date gate at file-time must carry a resolvable
                    # operator:<ref> anchor, same rule as `block`.
                    fields["not_before"] = _parse_not_before(args.not_before.strip())
                    fields["not_before_anchor"] = _validate_not_before_anchor(
                        getattr(args, "not_before_anchor", None)
                    )
                raw_ratifiable = getattr(args, "ratifiable_file", None) or []
                if raw_ratifiable:
                    fields["ratifiable_files"] = _parse_ratifiable_files(
                        [e.strip() for e in raw_ratifiable]
                    )

                # ORG-022: ratifiable label ↔ ratifiable_files pairing enforcement.
                # Asymmetric: refuse label-without-files (malformed stub); auto-add
                # label-when-files-present (files are the concrete signal; preserves
                # writer ergonomics). Pre-ratified by the operator msg_1705.
                has_ratifiable_label = "ratification" in (labels or [])
                has_ratifiable_files = bool(fields.get("ratifiable_files"))
                if has_ratifiable_label and not has_ratifiable_files:
                    raise TicketRefusal(
                        "ratification label requires --ratifiable-file <repo>:<path> "
                        "(one or more); per ORG-022 file-tier pairing enforcement"
                    )
                if has_ratifiable_files and not has_ratifiable_label:
                    # Auto-add the canonical 'ratification' label (matches PWA
                    # frontend filter at web/src/components/TicketList.svelte +
                    # TicketDetail.svelte). Pre-ORG-022 convention was inconsistent
                    # ('ratifiable' vs 'ratification'); ORG-022 codifies 'ratification'.
                    if labels:
                        labels = labels + ["ratification"]
                    else:
                        labels = ["ratification"]
                    fields["labels"] = labels

                # T-0465: intent disambiguation for target=operator queue.
                raw_intent = getattr(args, "intent", None)
                if isinstance(raw_intent, str) and raw_intent.strip():
                    intent = raw_intent.strip()
                    if intent not in INTENT_VOCAB:
                        raise TicketRefusal(
                            f"--intent {intent!r} not in vocab {INTENT_VOCAB}; "
                            f"choose one of: {', '.join(INTENT_VOCAB)}"
                        )
                    if target != "operator":
                        raise TicketRefusal(
                            f"--intent is target=operator-specific; refusing for target={target!r}"
                        )
                    # T-1321: explicit intent=ratify requires at least one
                    # ratifiable_files entry — the PWA ratifies a committed diff,
                    # not prose.  _has_ratifiable is computed above (line ~1295)
                    # from the --ratifiable-file args; re-check fields in case a
                    # prior code path already populated ratifiable_files.
                    if intent == "ratify" and not (
                        _has_ratifiable or fields.get("ratifiable_files")
                    ):
                        raise TicketRefusal(
                            "intent=ratify requires at least one ratifiable file; "
                            "pass --ratifiable-file <repo>:<path> (per ORG-015)"
                        )
                    fields["intent"] = intent
                elif target == "operator":
                    # Default for target=operator without explicit --intent: 'ratify' if
                    # ratifiable_files present (matches ORG-015 ratification path),
                    # else 'action' (operator-action default per T-0465 body).
                    fields["intent"] = "ratify" if fields.get("ratifiable_files") else "action"

                # T-1103 / ORG-034: serves — arc-intent field (optional; free-text).
                raw_serves = getattr(args, "serves", None)
                if isinstance(raw_serves, str) and raw_serves.strip():
                    fields["serves"] = raw_serves.strip()
                if repair_fields:
                    fields.update(repair_fields)  # T-4707: RFC-004 section 3(a) runtime attributes

                # T-3440 / ORG-089 §Decision.4e: appetite applicability at file-time.
                # bet_ref is set first (it is one of the applicability triggers), then
                # the appetite is resolved: supplied → normalized int; hygiene/legacy →
                # medium-default 5; applicable bet without a score → AppetiteRequired;
                # below-the-line non-bet → left absent. Fail-fast before any mutation,
                # same posture as the lineage/cycle guards.
                raw_bet_ref = getattr(args, "bet_ref", None)
                if isinstance(raw_bet_ref, str) and raw_bet_ref.strip():
                    fields["bet_ref"] = raw_bet_ref.strip()
                _apply_appetite_at_file_time(
                    fields, getattr(args, "appetite", None), created
                )

                # T-3168: syntax is already fail-closed above.  During the approved
                # migration leg, graph/corpus findings are named but do not refuse
                # filing.  ENFORCE remains a tested API until a canonical goal-key
                # adapter exists and the activation gate is separately approved.
                purpose_mode = PurposeEnforcement(TICKET_PURPOSE_ENFORCEMENT)
                purpose_issues = _purpose_issues_for_fields(fields, mode=purpose_mode)
                _apply_purpose_policy(purpose_issues, mode=purpose_mode)

                # T-2613 / ORG-038: retest_status — vocab already enforced by argparse
                # `choices=RETEST_STATUS_VOCAB` on the flag itself.
                raw_retest_status = getattr(args, "retest_status", None)
                if isinstance(raw_retest_status, str) and raw_retest_status.strip():
                    fields["retest_status"] = raw_retest_status.strip()

                # T-2841 / ORG-065 §3.6: reject a cycle-creating edge at insertion,
                # BEFORE the pull/write below — a failed cycle check must leave no
                # trace (matches the fail-fast shape of every other file-time
                # validation above). Checked against every gating/structural field
                # this ticket carries; `informs` is excluded (acyclicity-exempt).
                _reject_if_cycle(tid, {
                    "depends_on": depends_on,
                    "parent_ticket": [parent_ticket] if parent_ticket else [],
                    "implements": implements,
                    "verifies": verifies,
                    "supersedes": supersedes,
                    "ratifies": ratifies,
                })

                text = _build_ticket_text(fields, body, old_fields=None)
                # T-4760 (c): the impl child is spawned AFTER the parent is on disk
                # (its id allocation must see the parent), so registry-check its
                # frontmatter now, before the parent write, so a refusal leaves no
                # orphan parent file behind.
                if gate_applies and spawn_impl:
                    _preflight_child_ticket(
                        parent_tid=tid, parent_fields=fields, target=impl_target,
                        intent=None, labels=["ratify-impl"], depends_on=[tid],
                    )
                new_path = TICKETS_ROOT / "inbox" / f"{tid}.md"
                # T-4885: exclusive create. On collision (an upstream same-id file
                # arrived after allocation) re-allocate as a counted retry; the
                # foreign file is never written, deleted, or reset over.
                try:
                    _write_new_ticket_exclusive(new_path, text)
                except FileExistsError:
                    last_push_err = (
                        f"git push failed: {tid} already exists on disk (same-id "
                        f"collision, T-4885)"
                    )
                    print(f"ticket.py: {tid} already exists; re-allocating", file=sys.stderr)
                    continue

                # T-0852: spawn the impl child INSIDE the lock, after the parent is on
                # disk, so the child's _next_ticket_id() sees it and allocates tid+1.
                # _locked=True avoids re-acquiring the (blocking) alloc lock we hold.
                # Re-run per attempt: on a push race the parent tid is re-allocated, so
                # the child's depends_on=[tid] must re-bind to the fresh tid.
                child_path = None
                if gate_applies and spawn_impl:
                    child_ask = impl_ask or (
                        f"Implement the change(s) ratified in {tid}. See the ratified "
                        f"artifact(s) on the parent ticket. Spawned at file-time per "
                        f"T-0852 so the ratification does not dead-end; this ticket is "
                        f"gated on {tid} and auto-unblocks when the operator ratifies it."
                    )
                    try:
                        child_path = _spawn_child_ticket(
                            parent_tid=tid,
                            target=impl_target,
                            intent=None,
                            labels=["ratify-impl"],
                            title=impl_title,
                            ask=child_ask,
                            depends_on=[tid],
                            _locked=True,
                        )
                    except FileExistsError:
                        # T-4885: child id collided with a foreign file. Remove ONLY
                        # the parent this attempt just created, then re-allocate both.
                        new_path.unlink()
                        last_push_err = (
                            "git push failed: spawned impl child id collided on "
                            "disk (T-4885)"
                        )
                        print("ticket.py: impl child id collided; re-allocating",
                              file=sys.stderr)
                        continue

                # T-5291: gate the already-filed impl ticket on this ratify. Same
                # place + same per-attempt re-binding as the spawned child above: on
                # a re-allocation the edit is restored first (retry path below), so
                # depends_on is re-bound to the FRESH tid, never a stale one.
                existing_path = None
                existing_orig = None
                if gate_applies and impl_existing is not None:
                    try:
                        existing_path, existing_orig = _link_existing_impl_to_ratify(
                            impl_existing, tid
                        )
                    except Exception:
                        new_path.unlink()  # leave no orphan parent behind
                        raise
            # allocation lock released here; the git lock is still held through commit/push

            commit_message = _build_commit_message("file", tid, title, None)

            # Subject-length warning: title is requester's editorial call; surface
            # but do not refuse if the subject grows past 100 chars.
            subject = commit_message.splitlines()[0]
            if len(subject) > 100:
                print(
                    f"warning: commit subject {len(subject)} chars (>100): {subject!r}",
                    file=sys.stderr,
                )

            no_push = getattr(args, "no_push", False)
            try:
                # FILE path: rebase_on_nonff=False so a racing same-ID push surfaces as
                # a non-ff failure and this loop re-allocates a fresh ID (T-0786 race
                # design), rather than push_with_retry rebasing an add/add collision.
                # T-0852: the impl child (if spawned) lands in the SAME atomic commit.
                # T-1770 Part b / T-5406: add+commit(+push) run under the SAME
                # git_mutation_lock span as the write above (taken at the top of
                # the attempt); do NOT re-take it here (non-reentrant).
                added = [new_path] if child_path is None else [new_path, child_path]
                # T-5291: the linked existing ticket is a MODIFICATION in the same
                # atomic commit -> in scope, but never in require_added (T-4885).
                affected = list(added) + ([existing_path] if existing_path is not None else [])
                _commit_and_push(
                    affected, commit_message, no_push=no_push, rebase_on_nonff=False,
                    require_added=list(added),
                )
                committed = True  # T-5291 r2
                if existing_path is not None:
                    print(
                        f"filed {tid} → tickets/inbox/{tid}.md "
                        f"(+ existing impl {impl_existing} now gated on {tid})"
                    )
                elif child_path is not None:
                    print(
                        f"filed {tid} → tickets/inbox/{tid}.md "
                        f"(+ impl {child_path.stem} → tickets/inbox/{child_path.name}, "
                        f"gated on {tid})"
                    )
                else:
                    print(f"filed {tid} → tickets/inbox/{tid}.md")
                # T-2260: AFTER the write, deliberately. The warning must never be
                # mistaken for a gate, and a filing that succeeded must read as
                # succeeded; the neighbours are for the author's next second of
                # judgement, not for this command's exit status. Excludes the
                # ticket just filed (its own `created` is `created`, but it is
                # matched out by tid) — see _recent_neighbours.
                _warn_near_duplicates(labels, created, exclude=tid)
                return 0
            except TicketError as exc:
                err = str(exc)
                if "git push failed" not in err:
                    # Non-push errors (HERMES guard, scope guard, pull failure,
                    # etc.) are not recoverable by retry — surface immediately.
                    _undo_impl_existing_link()  # T-5291 r1
                    _rollback_uncommitted_attempt()  # T-5316
                    raise
                last_push_err = err
                # _commit_and_push committed before the push. Undo that commit
                # (soft reset: moves HEAD back, un-stages changes, leaves working
                # tree intact) so the next attempt starts from a clean state.
                # Delete the ticket file(s) first so the working tree is clean post-
                # reset (not yet on origin, so no remote cleanup needed). T-0852: the
                # spawned impl child is re-created on the next attempt against the
                # re-allocated tid, so it must be removed here too.
                if new_path.exists():
                    new_path.unlink()
                if child_path is not None and child_path.exists():
                    child_path.unlink()
                _git("reset", "HEAD~1")
                # T-5291: restore the linked existing ticket to its pre-edit bytes
                # (== HEAD after the reset) so the next attempt re-binds its
                # depends_on to the re-allocated tid instead of stacking a stale one.
                if existing_path is not None and existing_orig is not None:
                    existing_path.write_bytes(existing_orig)
                # T-2172: the undone commit ALSO carried the regenerated
                # tickets/README.md table. A soft reset leaves that regeneration in
                # the working tree, so the clone is left dirty with a file no
                # subsequent op expects — and the scope guard (which requires the
                # staged set to equal exactly the ticket paths) then refuses EVERY
                # later ticket op in that clone until someone cleans it by hand.
                # A push race would silently wedge the whole board.
                #
                # Safe to discard rather than preserve: the table is derived state,
                # rebuilt from the filesystem by _rebuild_readme_table on the next
                # attempt, so nothing is lost. Best-effort — a failure here must not
                # mask the push error we are retrying.
                try:
                    _git("checkout", "--", "tickets/README.md")
                except Exception:  # noqa: BLE001 - never mask last_push_err
                    pass
            except Exception:
                _undo_impl_existing_link()  # T-5291 r1: unexpected non-TicketError
                _rollback_uncommitted_attempt()  # T-5316
                raise

    raise TicketError(
        f"git push failed after {_MAX_FILE_RETRIES} attempts: {last_push_err}"
    )


# ---------------------------------------------------------------------------
# T-5015 P1: the operator decision register — structural close-path gate.
#
# resolve / close-action / close-noodle on a ticket carrying an `intent:` field
# (only operator-asks have one) must record the ruling in
# notes/operator-decision-register.md, appended in the SAME auto-commit as the
# ticket move. Keyed on `intent:`, NOT target=operator: ratify carriers are
# often retargeted to orchestrator before resolve. The operator's own closes (kino sets
# PANTHEON_ACTOR=operator and passes no ruling flags) never fail; they write a
# PENDING row the throughline pass fills in later.
# ---------------------------------------------------------------------------

DECISION_REGISTER_REL = "notes/operator-decision-register.md"
REGISTER_CATEGORIES = (
    "access", "ratify", "product", "identity", "money", "physical", "irreversible",
)
REGISTER_DELEGABLE = ("yes", "no", "partial")
_REGISTER_TABLE_HEADER_PREFIX = "| Date | Ticket |"
_REGISTER_FALLBACK_SUMMARY_MAX = 200


def _register_path() -> Path:
    return REPO_ROOT / DECISION_REGISTER_REL


def _register_insert_index(lines: list[str]) -> int:
    """Index just after the last row of the register's main table."""
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(_REGISTER_TABLE_HEADER_PREFIX):
            start = i
            break
    if start is None:
        raise TicketError(
            f"decision register {DECISION_REGISTER_REL} has no table "
            f"(header starting {_REGISTER_TABLE_HEADER_PREFIX!r}); nothing changed"
        )
    end = start + 1
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    return end


def _validate_register() -> None:
    """Raise (before any mutation) when the register cannot take a row."""
    path = _register_path()
    if not path.is_file():
        raise TicketError(
            f"decision register {DECISION_REGISTER_REL} not found at {path}; "
            f"nothing changed"
        )
    _register_insert_index(path.read_text(encoding="utf-8").split("\n"))


def _register_cell(value: str) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def _add_successor_arg(sp: argparse.ArgumentParser) -> None:
    """T-5496: --successor on every verb that moves a ticket to done/."""
    sp.add_argument(
        "--successor", default=None, metavar="T-NNNN",
        help="T-5496: open ticket that takes over registry/systems.yaml gap cells citing this "
             "ticket (re-cited + system-coverage.md re-rendered in the same commit). Required "
             "when the registry still cites this ticket as an open gap's owner.",
    )


def _add_register_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--ruling", default=None,
                    help="T-5015: the operator's call, quoted/summarised, with the TG id "
                         "(required when the ticket has an `intent:` field)")
    sp.add_argument("--implies", default=None,
                    help="T-5015: the general rule the ruling implies")
    sp.add_argument("--delegable", default=None, choices=list(REGISTER_DELEGABLE),
                    help="T-5015: can the next such case be decided below the operator?")
    sp.add_argument("--category", default=None, choices=list(REGISTER_CATEGORIES),
                    help="T-5015: register category")
    sp.add_argument("--why", default=None,
                    help="T-5015: why it reached the operator (default: the ticket's intent)")


def _flag(args: argparse.Namespace, name: str) -> str | None:
    v = getattr(args, name, None)
    return v.strip() if isinstance(v, str) and v.strip() else None


def _register_gate(args: argparse.Namespace, verb: str) -> dict | None:
    """Gate + row builder for the three close verbs. None => ticket not gated."""
    try:
        text = _find_ticket(args.ticket).read_text(encoding="utf-8")
    except TicketError:
        return None  # the verb's own lookup raises the canonical error
    fields, body = parse_front_matter(text)
    intent = fields.get("intent") or fields.get("operator_intent")  # T-5015: retarget keeps provenance
    if not intent:
        return None
    _validate_register()  # up front: refuse cleanly before any mutation

    ruling = _flag(args, "ruling")
    implies = _flag(args, "implies")
    delegable = _flag(args, "delegable")
    category = _flag(args, "category")
    why = _flag(args, "why")
    operator = _claiming_persona() == "operator"

    missing = [f for f, v in (("--ruling", ruling), ("--implies", implies),
                              ("--delegable", delegable), ("--category", category)) if not v]
    if missing and not operator:
        raise TicketRefusal(
            f"{verb} refused: {args.ticket} has intent={intent!r}, so closing it "
            f"records the operator's ruling in the decision register (T-5015). Missing: "
            f"{', '.join(missing)}. Give --ruling \"<his call, with TG id>\" "
            f"--implies \"<the general rule>\" --delegable yes|no|partial "
            f"--category {'|'.join(REGISTER_CATEGORIES)}."
        )
    if category and category not in REGISTER_CATEGORIES:
        raise TicketRefusal(
            f"{verb} refused: --category {category!r} not one of {list(REGISTER_CATEGORIES)}"
        )
    if delegable and delegable not in REGISTER_DELEGABLE:
        raise TicketRefusal(
            f"{verb} refused: --delegable {delegable!r} not one of {list(REGISTER_DELEGABLE)}"
        )
    if missing:  # the operator's PWA path: never fail, leave a PENDING row
        summary = _flag(args, "with_summary")
        fallback = f"{verb} by the operator (PWA)"
        if summary:
            fallback += ": " + summary[:_REGISTER_FALLBACK_SUMMARY_MAX]
        ruling = ruling or fallback
        implies = implies or "PENDING"
        delegable = delegable or "unknown"
        category = category or "unclassified"  # fallback-only; never accepted from --category
    return {
        "date": _now_iso()[:10],
        "ticket": args.ticket,
        "ask": _extract_title(body) or args.ticket,
        "ruling": ruling,
        "category": category,
        "why": why or str(intent),
        "implies": implies,
        "delegable": delegable,
    }


def _append_register_row(row: dict) -> Path:
    path = _register_path()
    lines = path.read_text(encoding="utf-8").split("\n")
    idx = _register_insert_index(lines)
    cells = [row[k] for k in ("date", "ticket", "ask", "ruling", "category",
                              "why", "implies", "delegable")]
    lines.insert(idx, "| " + " | ".join(_register_cell(c) for c in cells) + " |")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _plan_registry_gate(tid: str, verb: str, successor: str | None):
    """T-5496: refuse a done-transition that registry/systems.yaml still depends on.

    Returns the planned registry edit (None when the ticket owns no gap). Raises
    TicketRefusal before any mutation: no successor given, or a successor that is
    the ticket itself, missing, or already done.
    """
    # Lazy: only a repo that carries a registry pays (or needs) the germinate import
    # (synthetic-repo fixtures copy a fixed tools/ file list that omits it).
    if not (REPO_ROOT / "registry" / "systems.yaml").exists():
        return None
    # Fail OPEN (same posture as _fm_registry / _decision_lines): a seat without PyYAML
    # or with a partial tools tree must not have every close crash. CI's
    # `registry validate` stays the backstop. A named --successor is different: it is
    # an explicit ask we cannot honor, so refuse rather than silently ignore it.
    try:
        import importlib

        ticket_registry_gate = importlib.import_module("tools.ticket_registry_gate")

        edit = ticket_registry_gate.plan_registry_edit(REPO_ROOT, tid, None)
    except ImportError:
        if successor is not None:
            raise TicketRefusal(
                f"{verb} refused: --successor needs the registry tooling (PyYAML / "
                f"tools.germinate) which is unavailable here; re-cite the cells in "
                f"registry/systems.yaml by hand, or run from a seat that has it."
            ) from None
        return None
    if edit is None:
        return None
    if successor is None:
        raise TicketRefusal(ticket_registry_gate.refusal_message(verb, tid, edit.cells))
    if successor == tid:
        raise TicketRefusal(f"{verb} refused: --successor cannot be {tid} itself")
    if _find_ticket(successor).parent.name == "done":
        raise TicketRefusal(
            f"{verb} refused: --successor {successor} is already done and cannot own the gap"
        )
    try:
        return ticket_registry_gate.plan_registry_edit(REPO_ROOT, tid, successor)
    except ticket_registry_gate.RecitationError:
        raise TicketRefusal(
            f"{verb} refused: could not re-cite safely; edit registry/systems.yaml by hand"
        ) from None


def _do_transition(
    tid: str,
    expected_state: str,
    new_state: str,
    verb: str,
    detail: str | None,
    no_push: bool = False,
    cascade_callback=None,
    receipts_to_attach: list[str] | None = None,
    strip_auto_delivered: bool = False,
    field_updates: dict | None = None,
    skip_closure_gate: bool = False,
    appetite_outcome: str | None = None,
    register_row: dict | None = None,
    successor: str | None = None,
) -> int:
    """Shared implementation for claim / block / unblock / resolve / drop / reopen.

    field_updates (optional, T-3440 / ORG-089): a dict of frontmatter fields to
    set (value) or delete (value is the sentinel `_DELETE_FIELD`) on the ticket
    as part of THIS transition's atomic write. Used by `drop` (set substate:
    dropped + dropped_reason + dropped_from_state) and `reopen` (clear those +
    set appetite). Applied after `state` is set, before the transition-log
    append, so the whole change lands in one commit.

    skip_closure_gate (optional, T-3440): bypass the ORG-065 §3.3 closure gate
    when transitioning INTO done. `drop` sets this — a dropped bet is ABANDONED,
    not satisfied, so the obligation-satisfaction gate is semantically wrong for
    it (D-N: dropped does not satisfy obligations).

    cascade_callback (optional): callable(parent_tid: str) -> list[Path].
    Called AFTER the parent's git mv + frontmatter edit but BEFORE the
    commit, so any side-effect file moves it performs land in the same
    atomic commit as the parent transition. Used by cmd_resolve for
    auto-unblock cascade per T-0328.

    receipts_to_attach (optional, T-2842 / ORG-065 BT2): pre-validated
    `<clause>=<receipt-id>=<provenance>` entries (see _parse_receipt_entries)
    to merge into the ticket's `receipts:` list as part of THIS transition —
    threaded from cmd_resolve's `--receipt` flag. Merged in (deduped) before
    the closure gate runs, so freshly-supplied evidence is what the gate
    evaluates. Ignored when new_state != "done" (only resolve wires this
    today; claim/unclaim never reach `done` and never pass it).

    Closure-validation gate (ORG-065 §3.3, T-2842 BT2): whenever this
    transition moves a ticket INTO `done` (resolve / close-noodle /
    close-action — the only three `_do_transition` callers that ever pass
    new_state="done"; claim/unclaim do not), `_check_closure_gate` runs
    against the ticket's fields BEFORE the git mv, so a gate-mode refusal
    leaves no trace (mirrors _reject_if_cycle's fail-fast-before-mutation
    shape). This is a single, verb-agnostic hook — closure applies to any
    node reaching `done`, not to `resolve` specifically (§3.3's own "a node
    cannot close" framing), so close-noodle/close-action tickets carrying
    `implements` obligations are gated too, even though only `resolve`
    exposes `--receipt` in this ticket's scope.

    successor (optional, T-5496): when new_state == "done" and registry/systems.yaml
    cites this ticket as the owner of an open gap, the close is REFUSED unless a
    successor (open ticket) is named; the cited cells are then re-cited to it and
    registry/system-coverage.md re-rendered, both folded into this commit.

    appetite_outcome (optional, T-3451 / ORG-089): one of "shipped" / "dropped"
    / "reopened" — passed by cmd_resolve/cmd_drop/cmd_reopen, the only three
    verbs that fire a terminal appetite event (T-3440 Scope C). When set,
    `_record_appetite_outcome` runs INSIDE this critical section, after the
    ticket's own mv + frontmatter/transition-log edit (so its active-spend
    read sees the same final snapshot the pre-T-3451 post-transition call
    used) and BEFORE the commit-message build, and a successful write's shard
    Path is folded into this transition's `affected_paths` — so the shard
    lands in the SAME commit as the transition instead of as an untracked
    side effect. Fail-safe throughout: a write failure yields None and the
    shard is simply absent from this commit, never blocking the transition.
    """
    old_path = _find_ticket(tid)
    if old_path.parent.name != expected_state:
        raise TicketRefusal(
            f"state-mismatch: {tid} is in `{old_path.parent.name}`, "
            f"{verb} requires `{expected_state}`"
        )

    new_dir = TICKETS_ROOT / new_state
    new_dir.mkdir(parents=True, exist_ok=True)
    new_path = new_dir / old_path.name

    # T-1770 Part b: the ENTIRE mutate → stage → commit → push span for this
    # transition is one atomic critical section against other writers on the
    # same shared checkout (bugs/ticket-py-shared-checkout-interleaved-writer-
    # race.md). The lock must start BEFORE the pull/git-mv (both touch the
    # shared index/working tree) and extend through `_commit_and_push`, not
    # just wrap the commit call — a `git mv` staged-but-uncommitted outside
    # the lock is exactly what a concurrent writer's unrelated `git commit`
    # (no pathspec — commits the WHOLE index) can silently sweep up.
    with git_mutation_lock(label=f"{verb} {tid}"):
        # 0. Pull-first (T-0244). Must succeed BEFORE any filesystem mutation so
        #    a pull failure leaves the working tree clean. Skipped when
        #    no_push=True.
        _pull_if_needed(no_push)

        # 0b. T-2140: RE-VERIFY STATE INSIDE THE LOCK, AFTER THE PULL.
        #
        # The state check above ran OUTSIDE this critical section, so it proves
        # nothing by the time we get here — two processes can both pass it and
        # both arrive. The pull makes it worse rather than better: it can itself
        # bring in another writer's transition of this very ticket, so even a
        # single-writer race is real. Acting on the pre-lock paths would `git mv`
        # a path that has moved, or double-transition a ticket someone else
        # already advanced.
        #
        # This is the check-then-act half of T-2140 finding (1) ("the WIP cap is
        # not a lock — it is evaluated per invocation, not held for the life of
        # the work"). It does not make the cap a true lock across a whole
        # claim→resolve span, which is a bigger design question; it does make a
        # single transition atomic with respect to its own precondition.
        current = _find_ticket(tid)
        if current.parent.name != expected_state:
            raise TicketRefusal(
                f"concurrent-transition conflict: {tid} was in `{expected_state}` "
                f"when checked but is in `{current.parent.name}` now — another "
                f"writer transitioned it while this {verb} was waiting for the "
                f"lock. Nothing was changed. Re-read the ticket and retry "
                f"(T-2140)."
            )
        old_path = current
        new_path = new_dir / old_path.name

        # 0b'. T-5015: re-check the decision register INSIDE the lock (the pull
        # above can change it); still before any mutation, so nothing to unwind.
        if register_row is not None:
            _validate_register()

        # 0b''. T-5496: a close must not leave registry/systems.yaml citing a done
        # ticket as an open gap's owner (`registry validate` would go red on main).
        registry_edit = None
        if new_state == "done":
            registry_edit = _plan_registry_gate(tid, verb, successor)

        # 0c. Closure-validation gate + receipt attach (T-2842 / ORG-065 BT2,
        # §3.3) — BEFORE the git mv, so a gate-mode refusal leaves no trace
        # (same fail-fast-before-mutation shape as _reject_if_cycle). Reads
        # from old_path since the mv hasn't happened yet. Only evaluated when
        # transitioning INTO done — see this function's docstring.
        merged_receipts: list[str] | None = None
        if new_state == "done":
            pre_text = old_path.read_text(encoding="utf-8")
            pre_fields, _pre_body = parse_front_matter(pre_text)
            if receipts_to_attach:
                merged_receipts = list(pre_fields.get("receipts") or [])
                for r in receipts_to_attach:
                    if r not in merged_receipts:
                        merged_receipts.append(r)
                pre_fields["receipts"] = merged_receipts
            # T-3440 / ORG-089: `drop` skips the closure gate — a dropped bet is
            # abandoned, not satisfied, so the obligation-satisfaction gate does
            # not apply (D-N).
            if not skip_closure_gate:
                _check_closure_gate(tid, pre_fields)

        # 1. Edit front matter + append transition log, computed from old_path
        #    BEFORE the git mv (T-4751: _build_ticket_text runs the ORG-021
        #    frontmatter-registry check, and a refuse-mode refusal must leave
        #    no trace — same fail-fast-before-mutation shape as step 0c). The
        #    mv does not change content, so reading old_path is equivalent.
        text = old_path.read_text(encoding="utf-8")
        fields, body = parse_front_matter(text)
        old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
        # T-2842 / ORG-065 BT2: splice in the receipts merged by step 0c (if
        # any) so freshly-attached evidence lands in the same atomic write —
        # re-reading fresh from disk above would otherwise lose it.
        if merged_receipts is not None:
            fields["receipts"] = merged_receipts
        fields["state"] = new_state
        # T-3440 / ORG-089: apply drop/reopen field mutations in the same atomic
        # write (substate/dropped_reason/dropped_from_state/appetite). A value of
        # the _DELETE_FIELD sentinel removes the key (reopen clears the dropped
        # disposition); any other value sets it.
        if field_updates:
            for key, value in field_updates.items():
                if value is _DELETE_FIELD:
                    fields.pop(key, None)
                else:
                    fields[key] = value
        # T-3081: strip auto_delivered / auto_delivered_at when the transition
        # is a re-shelve/defer (e.g. cmd_unclaim: active→inbox). Same rationale
        # as the cmd_block strip + T-0971 arm-A (cascade-unblock) + T-2350
        # (retarget): a delivered-but-parked ticket keeps the stamp; when it
        # later becomes un-gated + 8h-stale, check_dispatch_locked's backstop
        # force-releases the lock + operator-alerts. Only strip when the caller
        # explicitly asks (default False preserves existing claim / resolve
        # behavior).
        stripped_ad = None
        stripped_ad_at = None
        if strip_auto_delivered:
            stripped_ad = fields.pop("auto_delivered", None)
            stripped_ad_at = fields.pop("auto_delivered_at", None)
            if (stripped_ad is not None or stripped_ad_at is not None):
                detail = (
                    f"{detail}; auto_delivered stamp cleared"
                    if detail else "auto_delivered stamp cleared"
                )
            if _release_pool_holder(fields, in_inbox=new_state == "inbox"):
                detail = f"{detail}; delivered_to cleared" if detail else "delivered_to cleared"
        actor = _actor()
        log_verb = _LOG_VERB.get(verb, verb)
        body = append_transition(body, actor, log_verb, detail)
        new_text = _build_ticket_text(fields, body, old_fields=old_fields)

        # T-5511: everything from the first write to the commit is one unit. A refusal
        # in between (the commit scope guard, HERMES guard, a cascade failure) used to
        # leave the ticket moved, the registry re-cited, README/appetite/dependants
        # edited. Snapshot first, restore on any failure that precedes the commit.
        rollback_baseline = _dirty_snapshot()
        rollback_head = _git("rev-parse", "HEAD").stdout.strip()
        written: list[str] = []  # repo-relative paths THIS transition writes/moves

        def _note(*paths: Path) -> None:
            for p_ in paths:
                try:
                    rel_ = str(p_.relative_to(REPO_ROOT))
                except ValueError:
                    continue
                if rel_ not in written:
                    written.append(rel_)

        try:
            # 2. git mv (renames index entry + working-tree file in one step), then
            #    write the edited text at the new path.
            rel_old = str(old_path.relative_to(REPO_ROOT))
            rel_new = str(new_path.relative_to(REPO_ROOT))
            # README is rebuilt inside _commit_and_push (derived state); noted up
            # front so a failure between its write and the scope guard unwinds it.
            _note(old_path, new_path, REPO_ROOT / "tickets" / "README.md")
            res = _git("mv", rel_old, rel_new)
            if res.returncode != 0:
                raise TicketError(
                    f"git mv failed: {res.stderr.strip() or res.stdout.strip()}"
                )
            new_path.write_text(new_text, encoding="utf-8")

            # 2a'. T-5496: re-cite the registry cells + re-render, folded into this commit.
            registry_paths: list[Path] = []
            if registry_edit is not None and registry_edit.new_registry_text is not None:
                # T-5511: a path joins the commit scope only if its bytes change. The
                # re-cited cell may sit in a block the coverage page does not render
                # (adoption_gaps, a shadowed legacy leg), making the re-render a no-op;
                # expecting an unchanged file in the staged set refused the commit.
                for path_, text_ in (
                    (registry_edit.registry_path, registry_edit.new_registry_text),
                    (registry_edit.rendered_path, registry_edit.new_rendered_text),
                ):
                    if not path_.exists() or path_.read_text(encoding="utf-8") != text_:
                        _note(path_)
                        path_.write_text(text_, encoding="utf-8")
                        registry_paths.append(path_)

            # 2b. Cascade callback (T-0328): runs after parent mv+edit, returns
            # additional paths to fold into the commit. Callback is responsible
            # for its own git mv + frontmatter edits. Failure raises TicketError
            # which propagates and aborts before commit.
            cascade_paths: list[Path] = []
            if cascade_callback is not None:
                # A callback that raises midway returns nothing, so note the
                # dependants it is about to edit before it runs (T-5511).
                try:
                    _note(*_find_dependants(tid))
                except Exception:  # noqa: BLE001 - best-effort pre-note
                    pass
                cascade_paths = cascade_callback(tid)
                _note(*cascade_paths)

            # 2c. Ratify-lift (T-0491): when resolving a ratify-intent ticket that has
            # ratifiable_files, rewrite each same-repo doc's first **Status:** line to
            # "Ratified <YYYY-MM-DD> by the operator." in the same atomic commit. Cross-repo
            # entries and missing Status lines emit warnings but never block the resolve.
            # This is a best-effort convenience lift — HO can hand-tune edge cases.
            # Gate: verb==resolve AND intent==ratify AND ratifiable_files non-empty
            #       AND resolution-with-summary does NOT start with "Rejected:".
            # The "Rejected:" carve-out (T-0641): pantheon-pwa's /reject route also
            # shells `ticket.py resolve --with "Rejected: ..."` for the artifact-
            # rejection verb (T-0342). Without this guard the lift fires on rejected
            # ratify tickets too, rewriting the artifact's Status to "Ratified by
            # the operator" even though the operator rejected. Caught in T-0637 smoke (2026-05-27).
            # lift_paths is kept SEPARATE from cascade_paths so the cascade commit-message
            # tail and stdout print are gated on real cascade dependants only (T-0491 fix).
            lift_paths: list[Path] = []
            if verb == "resolve":
                ratify_intent = fields.get("intent") == "ratify"
                ratifiable_files_list = fields.get("ratifiable_files") or []
                is_reject = _is_reject_note(detail)
                if ratify_intent and ratifiable_files_list and not is_reject:
                    lift_warnings: list[str] = []
                    lift_paths = _apply_ratify_lift(
                        ratifiable_files_list, REPO_ROOT, lift_warnings
                    )
                    _note(*lift_paths)
                    for w in lift_warnings:
                        print(f"warning: ratify-lift: {w}", file=sys.stderr)

            # 2d. T-3451 / ORG-089: appetite outcome-log fold-into-commit. Runs
            # only when the caller passed `appetite_outcome` — the three verbs
            # that fire a terminal appetite event (resolve->shipped, drop->dropped,
            # reopen->reopened; T-3440 Scope C). Must run HERE: after the ticket's
            # own mv + frontmatter/transition-log edit (steps 1-2, so the shard's
            # active-spend read sees the SAME final ticket snapshot the pre-T-3451
            # post-transition call used) and BEFORE the commit-message build /
            # scope-guard (steps 3/5 below), so the shard lands in THIS
            # transition's affected_paths and is staged+committed atomically
            # instead of as an untracked side effect (the T-3439/T-3440 bug this
            # ticket fixes). The narrow "current-week-only" admission is
            # structural, not a separate allowlist check: the shard path is
            # always `_iso_week_shard_name(now)` for the actual current week, so
            # no other-week/other-tree path can ever ride along. Fail-safe
            # throughout: `_record_appetite_outcome` never raises; a write
            # failure yields None and the shard is simply absent from this
            # commit — never blocks the ticket transition itself. Locking this
            # write inside git_mutation_lock (it previously ran AFTER this whole
            # function returned, outside the lock) is an incidental concurrent-
            # write-safety improvement on the shared weekly shard file.
            outcome_paths: list[Path] = []
            if appetite_outcome is not None:
                shard_path = _record_appetite_outcome(tid, appetite_outcome)
                if shard_path is not None:
                    outcome_paths.append(shard_path)
                    _note(shard_path)

            # 2e. T-5015: decision-register row, folded into THIS commit.
            register_paths: list[Path] = []
            if register_row is not None:
                register_paths.append(_append_register_row(register_row))
                _note(*register_paths)

            # 3. Build commit message + auto-commit pipeline. Both old and new
            #    paths must be staged so the rename is captured atomically.
            title = _extract_title(body)
            commit_message = _build_commit_message(verb, tid, title, detail)
            if cascade_paths:
                # Only real cascade-unblock results go here — NOT lift_paths.
                cascade_count = len(cascade_paths)
                commit_message = (
                    commit_message + f"\n\nAuto-unblock cascade per ORG-013 (orig. T-0328): "
                    f"{cascade_count} dependant"
                    f"{'s' if cascade_count != 1 else ''} had depends_on updated in place."
                )
            subject = commit_message.splitlines()[0]
            if len(subject) > 100:
                print(
                    f"warning: commit subject {len(subject)} chars (>100): {subject!r}",
                    file=sys.stderr,
                )

            _commit_and_push(
                [old_path, new_path] + cascade_paths + lift_paths + outcome_paths + register_paths + registry_paths,
                commit_message,
                no_push=no_push,
            )
        except BaseException:
            _rollback_transition(rollback_baseline, rollback_head, written)
            raise
    print(f"{verb} {tid} → tickets/{new_state}/{tid}.md")
    if cascade_paths:
        # Only print cascade line for real cascade-unblock results.
        cascade_count = len(cascade_paths)
        print(f"  auto-unblock cascade: {cascade_count} dependant"
              f"{'s' if cascade_count != 1 else ''} had depends_on updated in place")
    return 0


def _origin_state_of_ticket(tid: str) -> str | None:
    """Best-effort check of ticket's state on origin/main.

    Returns the state directory name ('inbox' | 'active' | 'done') if the
    ticket file exists at origin/main:tickets/<state>/<tid>.md for any state;
    None if not found (either truly not on origin or fetch failure).

    Used by cmd_claim's pre-claim guard (T-0463) to refuse a local claim if
    origin shows the ticket has already moved past inbox (typical sign:
    another clone claimed it concurrently and pushed before this clone did).
    """
    # Best-effort fetch; fail-open. Network outage / offline VPS shouldn't
    # block local claim attempts. Caller falls back to the stale local ref.
    _git("fetch", "origin", "main")
    for state in STATES:
        res = _git(
            "show", f"origin/main:tickets/{state}/{tid}.md",
        )
        if res.returncode == 0:
            return state
    return None


def ticket_holder(fields: dict) -> str | None:
    """T-4695: the persona that HOLDS a ticket (for the one-active WIP cap and the
    inbox_consumer dispatch mutex). Shared so both sides use one predicate.

    A `role:<R>` pool ticket is held by whoever took it (`delivered_to`, stamped on
    consumer delivery and on `claim`), or by nobody when not yet taken. Any other
    target is held by the persona it resolves to. Never rewrites `target`
    (ORG-088 rev-2 D-4)."""
    target = str(fields.get("target", "") or "")
    if target.startswith("role:"):
        taker = str(fields.get("delivered_to", "") or "").strip()
        return taker or None
    return _resolve_persona(target)


def _release_pool_holder(fields: dict, *, in_inbox: bool) -> bool:
    """T-4698: drop a pool ticket's taker (`delivered_to`) alongside the auto_delivered
    strip, but ONLY when the ticket ends up in inbox/. In active/ the taker is the
    claimant still holding live work (WIP cap + dispatch mutex via `ticket_holder`),
    so it stays. Returns True if a taker was dropped (callers log it visibly, like
    the stamp strip)."""
    if not in_inbox:
        return False
    return fields.pop("delivered_to", None) is not None


def _strip_dispatch_stamps_on_gate_add(fields: dict, *, in_inbox: bool) -> list[str]:
    """T-3081 / T-4698: when a gate is ADDED to a ticket, drop its
    auto_delivered / auto_delivered_at stamp (and, in inbox/, its pool taker) so a
    later un-gate does not inherit a stale dispatch-mutex hold. Returns the
    transition-log notes for whatever was cleared (cleared VISIBLY, never
    silently). Shared by cmd_block's add path and the T-5291 --impl-existing link.
    """
    notes: list[str] = []
    had_ad = fields.pop("auto_delivered", None)
    had_ad_at = fields.pop("auto_delivered_at", None)
    if had_ad is not None or had_ad_at is not None:
        notes.append("auto_delivered stamp cleared")
    if _release_pool_holder(fields, in_inbox=in_inbox):
        notes.append("delivered_to cleared")
    return notes


def _validate_impl_existing(impl_tid: str, args: argparse.Namespace) -> Path:
    """T-5291 pre-loop validation (no writes, no lock) for ``file --impl-existing``.

    The id must be well-formed, name an existing ticket, and that ticket must be
    OPEN (inbox/ or active/ — done/ holds resolved, dropped and superseded
    tickets alike; same refusal surface as cmd_block). The spawn-only flags
    (--impl-title/--impl-ask/--impl-target) are refused: they would be silently
    ignored otherwise. Returns the ticket's current path.
    """
    for flag, dest in (("--impl-title", "impl_title"), ("--impl-ask", "impl_ask"),
                       ("--impl-target", "impl_target")):
        val = getattr(args, dest, None)
        if isinstance(val, str) and val.strip():
            raise TicketRefusal(
                f"{flag} only applies to --spawn-impl; refusing it with "
                f"--impl-existing {impl_tid} (the existing ticket keeps its own "
                f"title/ask/target) (T-5291)"
            )
    if not _TICKET_ID_FULL_RE.fullmatch(impl_tid):
        raise TicketRefusal(
            f"--impl-existing value {impl_tid!r} does not match the T-NNNN format (T-5291)"
        )
    try:
        path = _find_ticket(impl_tid)
    except TicketRefusal:
        raise TicketRefusal(
            f"--impl-existing {impl_tid}: ticket not found locally (pull and "
            f"retry?) (T-5291)"
        ) from None
    if path.parent.name == "done":
        raise TicketRefusal(
            f"--impl-existing {impl_tid} is in `done` (resolved/dropped/superseded); "
            f"a closed ticket cannot carry the ratify's implementation — use "
            f"--spawn-impl, or reopen it first (T-5291)"
        )
    return path


def _link_existing_impl_to_ratify(impl_tid: str, ratify_tid: str) -> tuple[Path, bytes]:
    """T-5291: gate an already-filed impl ticket on a freshly written ratify.

    Called INSIDE cmd_file's allocation lock, after the ratify parent is on disk
    with its final tid for this attempt. Adds *ratify_tid* to the impl ticket's
    depends_on (no duplicate), strips dispatch stamps like any gate-add, appends a
    transition-log line, and writes it in place (the file stays in its state
    dir). Returns (path, pre-edit bytes) so the caller can stage the MODIFIED
    file into the parent's atomic commit and restore it on a push-race retry.
    Re-checks openness: a pull since the pre-loop check may have closed it.
    """
    path = _find_ticket(impl_tid)
    if path.parent.name == "done":
        raise TicketRefusal(
            f"--impl-existing {impl_tid} moved to `done` before the ratify could be "
            f"linked (T-5291)"
        )
    # ORG-065 §3.6: the new edge impl -> ratify must not close a cycle (e.g. the
    # ratify itself was filed --depends-on the impl ticket).
    _reject_if_cycle(impl_tid, {"depends_on": [ratify_tid]})
    orig = path.read_bytes()
    fields, body = parse_front_matter(orig.decode("utf-8"))
    old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
    deps: list[str] = list(fields.get("depends_on") or [])
    if ratify_tid not in deps:
        deps.append(ratify_tid)
    fields["depends_on"] = deps
    notes = [f"depends_on: [{ratify_tid}]"]
    notes.extend(
        _strip_dispatch_stamps_on_gate_add(fields, in_inbox=path.parent.name == "inbox")
    )
    detail = (
        f"gated on ratify {ratify_tid} via file --impl-existing (T-0852 no-dead-end, "
        f"T-5291); " + "; ".join(notes)
    )
    body = append_transition(body, _actor(), "blocked", detail)
    path.write_text(_build_ticket_text(fields, body, old_fields=old_fields), encoding="utf-8")
    return path, orig


def _role_claim_field_updates(tid: str, persona: str) -> dict | None:
    """T-4695: `{delivered_to: <claimant>}` when *tid* is role-targeted, else None.
    Fail-safe: a read error returns None (the claim itself must not be blocked)."""
    try:
        fields = parse_ticket(_find_ticket(tid)).fields
    except Exception:
        return None
    if str(fields.get("target", "") or "").startswith("role:"):
        return {"delivered_to": persona}
    return None


_WIP_CAP_ENV = "PANTHEON_MAX_ACTIVE_TICKETS"
_WIP_CAP_MAX_HORIZON = timedelta(days=7)
_WIP_CAP_FORM = f"{_WIP_CAP_ENV}=<N>@<ISO-UTC expiry>, e.g. {_WIP_CAP_ENV}=5@2026-10-14T17:00:00Z"


def active_holdings(tickets) -> dict[str, list[str]]:
    """T-4881: {holder: [ticket ids]} of active tickets that count against the
    one-active WIP cap (T-0877). Shared by `cmd_claim` and the grooming
    drift check so there is ONE predicate: state active, holder via
    `ticket_holder()` (T-4695), not `substate: awaiting-merge` (T-4119),
    and `operator` exempt. Input order is preserved per holder."""
    out: dict[str, list[str]] = {}
    for t in tickets:
        if t.fields.get("state") != "active":
            continue
        if t.fields.get("substate") == "awaiting-merge":
            continue
        holder = ticket_holder(t.fields)
        if not holder or holder == "operator":
            continue
        out.setdefault(holder, []).append(t.fields.get("id", "?"))
    return out


def _resolve_wip_cap(now: datetime | None = None) -> int:
    """T-4881: the effective one-active cap. Unset (or bare `1`) -> 1. An
    override must be `N@<ISO-UTC>` with an expiry in the future and at most 7
    days out; anything else is REFUSED (fail closed -- never silently 1, never
    silently granted). Every refusal says why, the current time and the form."""
    now = now or datetime.now(timezone.utc)
    now_s = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = os.environ.get(_WIP_CAP_ENV)
    if raw is None:
        return 1

    def refuse(why: str) -> "TicketRefusal":
        return TicketRefusal(
            f"{_WIP_CAP_ENV}={raw!r} refused: {why}. Current time {now_s}. "
            f"Expected form: {_WIP_CAP_FORM} (expiry must be in the future and "
            f"at most 7 days out). Unset it to return to the default cap of 1."
        )

    text = raw.strip()
    num_s, sep, exp_s = text.partition("@")
    try:
        n = int(num_s)
    except ValueError:
        raise refuse("not an integer cap") from None
    if n < 1:
        raise refuse("cap must be >= 1")
    if not sep:
        if n == 1:
            return 1
        raise refuse("an override above 1 must carry its own expiry")
    exp = None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            exp = datetime.strptime(exp_s.strip(), fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if exp is None:
        raise refuse("expiry is not an ISO-UTC timestamp")
    if exp <= now:
        raise refuse(f"override expired at {exp_s.strip()}")
    if exp - now > _WIP_CAP_MAX_HORIZON:
        raise refuse("expiry is more than 7 days away (limit: 7 days)")
    return n


def _refuse_if_security_pending(tid: str) -> None:
    """T-5678: claim must not pull a ticket awaiting HO's ORG-105 security accept."""
    try:
        fields = parse_front_matter(_find_ticket(tid).read_text(encoding="utf-8"))[0]
    except TicketRefusal:
        return  # not found: the normal claim path reports it
    if _security_gate.is_pending(fields):
        raise TicketRefusal(
            f"claim refused: {tid} is security_gate: pending (ORG-105 s4); HO must run "
            f"`ticket.py security-accept {tid} --rank primary|secondary --note ...` "
            f"(or security-reject) before it can be pulled."
        )


def cmd_claim(args: argparse.Namespace) -> int:
    # T-0463: pre-claim origin-side state check. Refuses if origin shows the
    # ticket already past inbox (concurrent-claim divergence pre-empt).
    # Caveat: TOCTOU window between this check and the local claim landing on
    # origin — bounds the worst case but doesn't eliminate it. Pair with
    # PWA-tier optimistic-lock UI (T-0367 follow-on) for fuller coverage.
    _refuse_if_security_pending(args.ticket)  # T-5678
    origin_state = _origin_state_of_ticket(args.ticket)
    if origin_state is not None and origin_state != "inbox":
        raise TicketRefusal(
            f"concurrent-claim conflict: {args.ticket} is in `{origin_state}` "
            f"on origin/main (likely claimed by another clone). Pull origin/main "
            f"and re-evaluate before re-attempting claim."
        )

    # One-active WIP cap (T-0877; the operator directive 2026-06-06 msg 2662): an
    # agent holds at most one active ticket at a time. Refuse a manual claim
    # if the claiming persona already holds active work — finish (resolve),
    # retarget, or unclaim it first. This extends the T-0717 auto-dispatch
    # mutex to MANUAL claims, so the 1-active discipline can't be bypassed by
    # claiming directly. operator is exempt (claims his own decision tickets).
    # Cap is overridable via PANTHEON_MAX_ACTIVE_TICKETS (default 1) for test
    # isolation / deliberate exceptions; unset in production = enforce 1.
    persona = _claiming_persona()
    if persona != "operator":
        # T-4881: the override must carry its own expiry (`N@<ISO-UTC>`); a bare
        # N>1, an expired or >7d-out expiry is refused (fail closed).
        max_active = _resolve_wip_cap()
        # T-4695: ticket_holder() so a stamped role:<R> pool ticket counts for
        # its taker only. T-4119: `substate: awaiting-merge` is work-complete
        # and does not count. One predicate shared with the grooming drift
        # check (active_holdings) so the two can't diverge.
        held = active_holdings(_iter_all_tickets()).get(persona, [])
        if len(held) >= max_active:
            raise TicketRefusal(
                f"WIP cap ({max_active} active per agent): {persona} already "
                f"holds active {', '.join(held)}. Finish (resolve), retarget, or "
                f"unclaim it before claiming {args.ticket}. "
                f"See sop/standard/ticket-flow.md."
            )

    # T-4119 REBOUND-1 (reviewer finding #4): defensively strip a stale
    # `substate: awaiting-merge` pair on the ticket BEING claimed. Claiming
    # means the work is live again — the substate no longer describes reality
    # — and without this a leaked pair (from a hand-edit, or any transition
    # this arc didn't fully close) would survive the claim and permanently
    # exempt the ticket from the WIP cap under its new activity, forever
    # (`t.fields.get("substate") != "awaiting-merge"` above would keep
    # excluding it). This is on the CLAIMED ticket only — it is still in
    # inbox/ here, so it was never part of the `held` count above regardless.
    field_updates = _resolve_field_updates_for_awaiting_merge(args.ticket)
    # T-4695 / ORG-088 rev-2: a role:<R> ticket stays role-targeted for life (no
    # target rewrite). Record WHO took it in `delivered_to` so the WIP cap and
    # the consumer's dispatch mutex bind the ticket to the claimant alone.
    claim_updates = _role_claim_field_updates(args.ticket, persona)
    if claim_updates:
        field_updates = {**(field_updates or {}), **claim_updates}
    rc = _do_transition(
        tid=args.ticket,
        expected_state="inbox",
        new_state="active",
        verb="claim",
        detail="moving to active",
        no_push=getattr(args, "no_push", False),
        field_updates=field_updates,
    )

    # T-0761 c4: clone-on-demand provisioning. Runs ONLY on a successful claim
    # (rc == 0) AND when provision_repos is available (None when module absent
    # in isolated subprocess worktrees). Best-effort: any failure (network,
    # missing deploy key, bad repo) warns to stderr and continues — claim
    # return code is NEVER altered.
    # Mirrors the README-rebuild OSError graceful-degradation pattern above.
    # Grant validation is intentionally omitted here: ticket_lint's gate already
    # validated grants before the ticket could carry repos: entries. No re-impl
    # of persona→role mapping needed (no live instance.yaml reader exists yet).
    if rc == 0 and provision_repos is not None:
        try:
            ticket_path = _find_ticket(args.ticket)
            text = ticket_path.read_text(encoding="utf-8")
            fields, _ = parse_front_matter(text)
            warnings = provision_repos(fields, repo_root=REPO_ROOT)
            for w in warnings:
                print(
                    f"ticket.py: provision_repos: {w}",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort; never blocks claim
            print(
                f"ticket.py: WARNING: repo provisioning skipped ({exc}); "
                f"claim already completed.",
                file=sys.stderr,
            )

    return rc


def cmd_unclaim(args: argparse.Namespace) -> int:
    """Move a ticket from active/ back to inbox/. Inverse of cmd_claim.

    T-0663: needed when a claim was premature (deps not clear, ticket parked,
    bandwidth not available). No origin-state pre-check — the contested
    concurrent-race surface is inbox→active (claim); the inverse demotion
    has no concurrent race surface.

    T-4119 REBOUND-1 (reviewer finding #1): `substate: awaiting-merge` is only
    meaningful while the ticket sits in active/ — bare-unclaiming a
    work-complete ticket back to inbox/ without stripping it would leak the
    marker into a state the WIP-cap exclusion and the inbox auto-consumer
    never expect, reproducing the exact false-redelivery hole this substate
    exists to close. Reuses the same delete-dict helper `cmd_resolve` already
    uses, applied in the SAME atomic write as the state-mv. Any OTHER
    substate (e.g. appetite-overrun) is left untouched — not this verb's to
    touch.
    """
    field_updates = _resolve_field_updates_for_awaiting_merge(args.ticket)
    return _do_transition(
        tid=args.ticket,
        expected_state="active",
        new_state="inbox",
        verb="unclaim",
        detail="moving to inbox",
        no_push=getattr(args, "no_push", False),
        strip_auto_delivered=True,  # T-3081: re-shelve clears the dispatch-mutex stamp
        field_updates=field_updates,
    )


def cmd_block(args: argparse.Namespace) -> int:
    """Set not_before and/or depends_on on a ticket (frontmatter edit; no state change).

    Per ORG-019: blocking is expressed via two structural frontmatter fields only:
      --not-before <ISO ts>   calendar gate (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)
      --depends-on T-NNNN     ticket-dep gate (repeatable for multiple deps)

    T-0663 extension:
      --clear-not-before      remove the not_before calendar gate (mutually exclusive
                              with --not-before and --depends-on). No-op + no commit
                              if not_before is already absent.

    T-1975 extension (the dep-clearing inverse of --depends-on, for a wrong gate):
      --clear-dep T-NNNN      remove a specific dep (repeatable). CLEAN ERROR if the
                              named dep is absent — it names a target, so a miss is
                              surfaced, not silently ignored.
      --clear-deps            remove ALL deps. Idempotent no-op + no commit if empty
                              (names no target, so nothing to surface).
    A clear op is a removal: it cannot combine with an add (--not-before/--depends-on),
    and only one clear op runs per call. These are the MANUAL path for a wrong gate;
    ORG-013 cascade-unblock remains the AUTOMATIC path when a parent resolves.

    The ticket stays in its current state directory (inbox/ or active/).
    At least one gate operation must be supplied. Refuses on done/ tickets.
    """
    not_before_raw: str | None = getattr(args, "not_before", None)
    not_before_anchor_raw: str | None = getattr(args, "not_before_anchor", None)
    depends_on_list: list[str] = getattr(args, "depends_on", None) or []
    clear_not_before: bool = getattr(args, "clear_not_before", False)
    clear_dep_list: list[str] = getattr(args, "clear_dep", None) or []
    clear_deps_all: bool = getattr(args, "clear_deps", False)
    no_push: bool = getattr(args, "no_push", False)

    # T-2833: --not-before-anchor only makes sense when SETTING a not_before date.
    if not_before_anchor_raw and not not_before_raw:
        raise TicketRefusal(
            "block: --not-before-anchor requires --not-before (an anchor justifies "
            "a date gate; there is no date to anchor without --not-before)"
        )

    # --clear-not-before is mutually exclusive with --not-before and --depends-on.
    if clear_not_before and (not_before_raw or not_before_anchor_raw):
        raise TicketRefusal(
            "block: --clear-not-before is mutually exclusive with --not-before"
        )
    if clear_not_before and depends_on_list:
        raise TicketRefusal(
            "block: --clear-not-before is mutually exclusive with --depends-on"
        )

    # T-1975: the dep-clearing inverse of --depends-on. --clear-dep removes named
    # dep(s); --clear-deps removes all. A clear op is a REMOVAL, so it cannot
    # combine with an add (--not-before/--depends-on) in the same call, and only
    # one clear op runs per call (removing and adding, or two different removals,
    # in one call would be ambiguous). This is the MANUAL path for a wrong gate;
    # ORG-013 cascade-unblock stays the AUTOMATIC path that removes a dep when its
    # parent resolves — no interplay beyond coexistence.
    if (clear_dep_list or clear_deps_all) and (not_before_raw or depends_on_list):
        raise TicketRefusal(
            "block: --clear-dep/--clear-deps cannot combine with --not-before/--depends-on "
            "(remove and add in separate calls)"
        )
    if sum([clear_not_before, bool(clear_dep_list), clear_deps_all]) > 1:
        raise TicketRefusal(
            "block: the clear flags (--clear-not-before / --clear-dep / --clear-deps) "
            "are mutually exclusive; one clear operation per call"
        )

    # Must supply at least one gate operation.
    if not (not_before_raw or depends_on_list or clear_not_before
            or clear_dep_list or clear_deps_all):
        raise TicketRefusal(
            "block: must specify at least one of --not-before, --depends-on, "
            "--clear-not-before, --clear-dep, or --clear-deps"
        )

    # --clear-not-before branch: lift calendar gate, stay in current lane.
    if clear_not_before:
        path = _find_ticket(args.ticket)
        if path.parent.name == "done":
            raise TicketRefusal(
                f"state-mismatch: {args.ticket} is in `done`; cannot clear block gate on closed ticket"
            )
        text = path.read_text(encoding="utf-8")
        fields, body = parse_front_matter(text)
        old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
        if "not_before" not in fields:
            # Already absent — idempotent no-op; skip commit to preserve audit signal.
            print(
                f"block {args.ticket}: not_before already absent; no change"
            )
            return 0
        del fields["not_before"]
        # T-2833: a cleared gate must not leave a dangling anchor behind.
        fields.pop("not_before_anchor", None)
        # T-3112: strip auto_delivered / auto_delivered_at on the DE-gate op.
        # T-3081 stripped on the ADD (park) direction; this is the missing INVERSE.
        # De-gating re-enters the ticket into the workable set, so a lingering
        # stamp becomes a live in-flight dispatch that holds the dispatch mutex +
        # fires the 8h backstop (the 8 spurious lock-release notifs Recovery diagnosed
        # 2026-08-24: a --clear-not-before batch left stale stamps that flipped
        # cosmetic→active). Mirrors T-0971 arm-A (cascade-unblock strip); cleared
        # VISIBLY in the transition log for the same audit-trail reason.
        had_ad = fields.pop("auto_delivered", None)
        had_ad_at = fields.pop("auto_delivered_at", None)
        actor = _actor()
        clear_note = "not_before gate cleared"
        if had_ad is not None or had_ad_at is not None:
            clear_note += "; auto_delivered stamp cleared"
        if _release_pool_holder(fields, in_inbox=path.parent.name == "inbox"):
            clear_note += "; delivered_to cleared"
        body = append_transition(body, actor, clear_note, None)
        new_text = _build_ticket_text(fields, body, old_fields=old_fields)
        # T-1770 Part b: pull → write → commit is one atomic critical section.
        with git_mutation_lock(label=f"block --clear-not-before {args.ticket}"):
            _pull_if_needed(no_push)
            path.write_text(new_text, encoding="utf-8")
            commit_message = f"tickets: {args.ticket} not_before cleared\n"
            _commit_and_push([path], commit_message, no_push=no_push)
        state_dir = path.parent.name
        print(
            f"block {args.ticket} --clear-not-before → tickets/{state_dir}/{args.ticket}.md "
            f"(not_before removed; file stays in place)"
        )
        return 0

    # T-1975: --clear-dep / --clear-deps branch — remove ticket-dep gate(s).
    if clear_dep_list or clear_deps_all:
        path = _find_ticket(args.ticket)
        if path.parent.name == "done":
            raise TicketRefusal(
                f"state-mismatch: {args.ticket} is in `done`; cannot clear block gate on closed ticket"
            )
        text = path.read_text(encoding="utf-8")
        fields, body = parse_front_matter(text)
        old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
        current_deps: list[str] = list(fields.get("depends_on") or [])

        if clear_deps_all:
            if not current_deps:
                # Idempotent no-op — matches --clear-not-before; skip commit.
                print(f"block {args.ticket}: depends_on already empty; no change")
                return 0
            new_deps: list[str] = []
            note = "all depends_on gates cleared"
            summary = "all deps cleared"
        else:
            # --clear-dep names SPECIFIC targets. A named dep that isn't present is
            # a clean ERROR, not a silent no-op: the caller asked to remove a
            # particular thing that isn't there, and surfacing that beats pretending
            # it worked. (This is the PRINCIPLED asymmetry with --clear-deps /
            # --clear-not-before, which are idempotent because they name no target.)
            missing = [d for d in clear_dep_list if d not in current_deps]
            if missing:
                raise TicketRefusal(
                    f"block --clear-dep: {args.ticket} has no dep(s) {missing}; "
                    f"current depends_on = {current_deps or '[]'}"
                )
            new_deps = [d for d in current_deps if d not in clear_dep_list]
            note = f"depends_on gate(s) cleared: {', '.join(clear_dep_list)}"
            summary = f"deps cleared: {', '.join(clear_dep_list)}"

        fields["depends_on"] = new_deps
        # T-3112: strip auto_delivered / auto_delivered_at on the DE-gate op —
        # same missing-inverse of T-3081 as the --clear-not-before branch above.
        # Removing a dep-gate re-enters the ticket into the workable set, so a
        # lingering stamp would hold the dispatch mutex + fire the 8h backstop.
        # Mirrors T-0971 arm-A (cascade-unblock), which strips on the AUTOMATIC
        # dep-removal path; this covers the MANUAL --clear-dep/--clear-deps path.
        had_ad = fields.pop("auto_delivered", None)
        had_ad_at = fields.pop("auto_delivered_at", None)
        if had_ad is not None or had_ad_at is not None:
            note += "; auto_delivered stamp cleared"
        if _release_pool_holder(fields, in_inbox=path.parent.name == "inbox"):
            note += "; delivered_to cleared"
        actor = _actor()
        body = append_transition(body, actor, note, None)
        new_text = _build_ticket_text(fields, body, old_fields=old_fields)
        # T-1770 Part b: pull → write → commit is one atomic critical section.
        with git_mutation_lock(label=f"block --clear-dep {args.ticket}"):
            _pull_if_needed(no_push)
            path.write_text(new_text, encoding="utf-8")
            commit_message = f"tickets: {args.ticket} {summary}\n"
            _commit_and_push([path], commit_message, no_push=no_push)
        state_dir = path.parent.name
        print(
            f"block {args.ticket} → tickets/{state_dir}/{args.ticket}.md "
            f"({summary}; file stays in place)"
        )
        return 0

    # Validate and normalize not_before if provided. T-2833: a date gate MUST
    # carry a resolvable operator:<ref> anchor — validated up front so we refuse
    # before touching the ticket / git.
    not_before: str | None = None
    not_before_anchor: str | None = None
    if not_before_raw:
        not_before = _parse_not_before(not_before_raw.strip())
        not_before_anchor = _validate_not_before_anchor(not_before_anchor_raw)

    # Find the ticket (any non-done state).
    path = _find_ticket(args.ticket)
    if path.parent.name == "done":
        raise TicketRefusal(
            f"state-mismatch: {args.ticket} is in `done`; cannot set block gate on closed ticket"
        )

    # T-1770 Part b: pull → edit → commit is one atomic critical section
    # against other writers on the shared checkout (no git-mv here — the
    # file stays in place — but the frontmatter edit must not be exposed to
    # an interleaved concurrent commit before it lands in this one).
    with git_mutation_lock(label=f"block {args.ticket}"):
        # Pull-first (T-0244).
        _pull_if_needed(no_push)

        # Edit frontmatter in-place.
        text = path.read_text(encoding="utf-8")
        fields, body = parse_front_matter(text)
        old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check

        # Merge depends_on: union of existing + new deps (preserves any already set).
        existing_deps: list[str] = fields.get("depends_on") or []
        stale_done_deps: list[str] = []
        live_deps_added: list[str] = []
        if depends_on_list:
            for dep in depends_on_list:
                if not _TICKET_ID_FULL_RE.fullmatch(dep):  # T-3215
                    raise TicketRefusal(f"--depends-on value {dep!r} does not match T-NNNN format")
                # T-3830: refuse to back-populate a stale done-ref. ORG-013 cascade
                # is resolve-time-triggered on the PARENT, so a depends_on added
                # AFTER the target is already done never auto-clears — this
                # write-time gate closes that hole. Skip-and-record: keep the
                # unrelated adds valid + surface the stale dep in the transition
                # log so the caller sees what wasn't recorded.
                dep_path = (TICKETS_ROOT / "done" / f"{dep}.md")
                if dep_path.exists():
                    if dep not in stale_done_deps:
                        stale_done_deps.append(dep)
                    continue
                if dep not in live_deps_added:
                    live_deps_added.append(dep)
            # T-3830: refuse the whole call if ALL deps are stale-done AND no
            # not_before was passed — the caller asked to block on nothing live,
            # and silent no-op would hide the mistake. A partial (some live, some
            # stale) proceeds with the live subset + logs the skipped stale set.
            if stale_done_deps and not live_deps_added and not not_before:
                raise TicketRefusal(
                    f"block: all --depends-on target(s) are already in done/ "
                    f"({', '.join(stale_done_deps)}); nothing to gate on. "
                    f"ORG-013 cascade only fires at parent-resolve time, so a "
                    f"back-populated done-ref would never auto-clear."
                )
            if live_deps_added:
                # T-2841 / ORG-065 §3.6: reject cycle-creating edges at insertion,
                # BEFORE the merge lands in `fields` — "depends_on via block" is
                # explicitly one of the two insertion points named in the ADR.
                _reject_if_cycle(args.ticket, {"depends_on": live_deps_added})
                merged_deps = list(existing_deps)
                for dep in live_deps_added:
                    if dep not in merged_deps:
                        merged_deps.append(dep)
                fields["depends_on"] = merged_deps

        if not_before:
            fields["not_before"] = not_before
            fields["not_before_anchor"] = not_before_anchor  # T-2833 (validated above)

        # T-3081: strip auto_delivered / auto_delivered_at when ADDING a gate.
        # A ticket that was auto_delivered then PARKED keeps the stamp; when the
        # gate later clears + it becomes un-gated + 8h-stale,
        # `check_dispatch_locked`'s backstop force-releases the lock + alerts
        # the operator — the source of alert-noise Recovery diagnosed 2026-08-23. Mirrors
        # T-0971 arm-A (cascade-unblock strip) + T-2350 retarget strip; keeps
        # all three paths in step. Cleared VISIBLY in the transition log for
        # the same audit-trail reason as retarget.
        strip_notes = _strip_dispatch_stamps_on_gate_add(
            fields, in_inbox=path.parent.name == "inbox"
        )

        # Build log detail line.
        detail_parts: list[str] = []
        if not_before:
            detail_parts.append(f"not_before: {not_before} (anchor: {not_before_anchor})")
        if live_deps_added:
            detail_parts.append(f"depends_on: [{', '.join(live_deps_added)}]")
        if stale_done_deps:
            detail_parts.append(
                f"stale-done-deps skipped (T-3830): [{', '.join(stale_done_deps)}]"
            )
        detail_parts.extend(strip_notes)
        detail = "; ".join(detail_parts)

        actor = _actor()
        body = append_transition(body, actor, "blocked", detail)
        new_text = _build_ticket_text(fields, body, old_fields=old_fields)
        path.write_text(new_text, encoding="utf-8")

        commit_message = _build_commit_message("block", args.ticket, _extract_title(body), detail)
        _commit_and_push([path], commit_message, no_push=no_push)
    state_dir = path.parent.name
    print(f"block {args.ticket} → tickets/{state_dir}/{args.ticket}.md (gate set; file stays in place)")
    return 0


def _warn_unmet_deps(tid: str, verb: str) -> None:
    """WARN (never block) when a ticket is closed with an unmet depends_on (T-2220).

    MEASURED instance: T-2113 sits in done/ with `depends_on: [T-2154]` while
    T-2154 is still in inbox/. The work completed with its stated gate unmet and
    nothing objected at any point, so the record says "gated" and the history says
    otherwise — a false record nobody can see.

    ⚠️ DELIBERATELY A WARNING, NOT A BLOCK, and the ticket says so explicitly.
    Overtaking a stale or mis-specified gate is legitimate and common — T-2113 did
    real work and the GATE was wrong, not the work. Blocking would add a new way
    to wedge the queue in exchange for a record-keeping problem, which is a bad
    trade. The point is that the inconsistency stops being silent, not that it
    stops being possible.

    Fail-soft in every direction: any read error and we say nothing. A close must
    never fail because its advisory could not be computed.
    """
    try:
        deps = list(parse_ticket(_find_ticket(tid)).fields.get("depends_on") or [])
        if not deps:
            return
        unmet = []
        for dep in deps:
            try:
                dep_state = parse_ticket(_find_ticket(dep)).fields.get("state")
            except Exception:
                dep_state = None  # unknown id — cannot call it met
            if dep_state != "done":
                unmet.append(f"{dep} ({dep_state or 'not found'})")
        if unmet:
            print(f"warning: {verb} {tid} with UNMET depends_on: {', '.join(unmet)}. "
                  f"Not blocking — overtaking a stale gate is legitimate — but the "
                  f"ticket's own record will say it was gated on work that is not "
                  f"done. Clear or correct the gate if it was wrong (T-2220).")
    except Exception:
        return


def _refuse_resolve_if_overrun(tid: str) -> None:
    """T-3440 / ORG-089 §Decision.4: resolve is refused on a ticket in
    `substate: appetite-overrun` — a re-decision verb (re-earn / drop /
    adjust-appetite) must run first. Fail-fast BEFORE the lock/git-mv (same
    posture as _warn_unmet_deps / receipt-parse). Fail-safe: any read error is
    swallowed (never block a legitimate resolve on a corrupt/missing field —
    the breaker's job is surfacing overrun, not blocking work)."""
    try:
        fields = parse_ticket(_find_ticket(tid)).fields
    except Exception:
        return
    if fields.get("substate") == "appetite-overrun":
        raise AppetiteOverrunNotResolvable(
            f"resolve refused: {tid} is in substate: appetite-overrun "
            f"(ORG-089 §Decision.4). Run a re-decision first: "
            f"`re-earn --appetite <N> --rationale ...` (keep working), "
            f"`adjust-appetite --appetite <N> --rationale ...` (original score "
            f"was wrong), or `drop --dropped-reason ...` (abandon the bet)."
        )


# ---------------------------------------------------------------------------
# T-4680: first-use SOP capture gate (writing-sops-from-first-use.md step 3).
# A ticket labelled `sop:<stem>` performs an operation whose SOP may still be a
# STUB. Resolve is refused while sop/standard/<stem>.md is a STUB that did not
# change in the ticket's arc, unless --sop-updated <sha> names a landed commit
# that touched it or --sop-no-change '<why>' waives it (recorded in the note).
# ---------------------------------------------------------------------------

SOP_LABEL_PREFIX = "sop:"
SOP_STEM_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# `**Status:** X` or `**Status**: X` in the body; `status: X` in YAML frontmatter.
SOP_STATUS_RE = re.compile(r"^\*\*Status(?::\*\*|\*\*:)\s*(.*)$", re.IGNORECASE)
SOP_FM_STATUS_RE = re.compile(r"^status:\s*(.*)$", re.IGNORECASE)
SOP_STUB_VALUE_RE = re.compile(r"^[\s*]*stub\b", re.IGNORECASE)
SOP_FIRST_RUN_PREFIX = "## First run"
REJECT_NOTE_PREFIX = "Rejected:"


def _is_reject_note(detail: str | None) -> bool:
    """T-0641: a resolve note starting with "Rejected:" is pantheon-pwa's
    /reject route (artifact rejection), not a completed piece of work. Shared
    by the ratify-lift carve-out and the T-4680 SOP gate exemption."""
    return (detail or "").lstrip().startswith(REJECT_NOTE_PREFIX)


def _sop_git(*args: str) -> "subprocess.CompletedProcess | None":
    """Run a read-only git query in REPO_ROOT. None on OSError/timeout, so a
    git failure reads as "no evidence" (the gate then refuses, fail-closed,
    with the waiver as the way out)."""
    import subprocess
    try:
        return subprocess.run(
            ["git", *args], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _sop_labels(fields: dict) -> list[str]:
    labels = fields.get("labels") or []
    if isinstance(labels, str):
        labels = [x.strip() for x in labels.split(",")]
    out = []
    for lab in labels:
        lab = str(lab).strip()
        if lab.startswith(SOP_LABEL_PREFIX):
            out.append(lab[len(SOP_LABEL_PREFIX):].strip())
    return out


def _sop_stub_status(text: str) -> tuple[bool, int | None]:
    """(is_stub, first_run_line).

    The FIRST status line outside ``` code fences wins: a YAML frontmatter
    `status: X`, or a body `**Status:** X` / `**Status**: X`. It is a STUB
    when X starts with the word STUB, case-insensitively, after optional
    `*`/whitespace (step 4: "the status line starts with STUB"); a mid-line
    STUB (a partly-stubbed ratified SOP) does not count, and a file with no
    status line is not a STUB. first_run is the line of the first
    `## First run` heading outside fences, or None."""
    lines = text.splitlines()
    status: str | None = None
    first_run = None
    in_fence = False
    in_fm = bool(lines) and lines[0].strip() == "---"
    for n, line in enumerate(lines, start=1):
        if in_fm:
            if n > 1 and line.strip() in ("---", "..."):
                in_fm = False
                continue
            m = SOP_FM_STATUS_RE.match(line.strip())
            if status is None and m:
                status = m.group(1).strip().strip("'\"")
            continue
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if status is None:
            m = SOP_STATUS_RE.match(line.strip())
            if m:
                status = m.group(1)
        if first_run is None and line.startswith(SOP_FIRST_RUN_PREFIX):
            first_run = n
    is_stub = status is not None and SOP_STUB_VALUE_RE.match(status) is not None
    return is_stub, first_run


def _ticket_id_in(tid: str, message: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(tid)}(?![\w-])", message) is not None


def _sop_changed_in_arc(tid: str, rel: str) -> bool:
    """True when a commit that MODIFIED `rel` (--diff-filter=M: the commit
    that creates the stub is not a fill, review MED-1) names `tid` as a whole
    token in its message (subject OR body: a squash merge may carry the id
    only in its PR body)."""
    res = _sop_git("log", "--diff-filter=M", "--format=%H%x00%B%x1e", "--", rel)
    if res is None or res.returncode != 0:
        return False
    for rec in res.stdout.split("\x1e"):
        _, _, msg = rec.partition("\x00")
        if _ticket_id_in(tid, msg):
            return True
    return False


def _check_sop_updated_sha(tid: str, sha: str, rel: str) -> str:
    """Validate --sop-updated: a real commit, on HEAD, that MODIFIED `rel`
    (a commit that only adds it is the stub's creation, not a fill).
    Returns the short sha; raises SopStubNotFilled otherwise."""
    res = _sop_git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if res is None or res.returncode != 0 or not res.stdout.strip():
        raise SopStubNotFilled(
            f"resolve refused: {tid} --sop-updated {sha!r} is not a commit in this "
            f"repo (T-4680)."
        )
    full = res.stdout.strip()
    anc = _sop_git("merge-base", "--is-ancestor", full, "HEAD")
    if anc is None or anc.returncode != 0:
        raise SopStubNotFilled(
            f"resolve refused: {tid} --sop-updated {sha} is not on HEAD, so the SOP "
            f"fill has not landed. Resolve after the merge lands; the squash sha is "
            f"the one to cite (T-4680)."
        )
    res = _sop_git("diff-tree", "--root", "--no-commit-id", "--name-status", "-r", full)
    rows = res.stdout.splitlines() if res is not None and res.returncode == 0 else []
    if not rows:
        raise SopStubNotFilled(
            f"resolve refused: {tid} --sop-updated {sha} lists no changed files (a "
            f"merge commit?). Cite the squash commit, or the underlying commit that "
            f"filled {rel} (T-4680)."
        )
    statuses = {parts[-1]: parts[0] for parts in (r.split("\t") for r in rows) if len(parts) >= 2}
    st = statuses.get(rel)
    if st is None:
        raise SopStubNotFilled(
            f"resolve refused: {tid} --sop-updated {sha} does not touch {rel}. Cite "
            f"the commit that filled the SOP's First-run section (T-4680)."
        )
    if not st.startswith("M"):
        raise SopStubNotFilled(
            f"resolve refused: {tid} --sop-updated {sha} only adds {rel} (status "
            f"{st}): that is the stub's creation, not a fill. Cite the commit that "
            f"filled its First-run section (T-4680)."
        )
    return full[:10]


def _refuse_resolve_if_sop_stub(
    tid: str, sop_updated: str | None, sop_no_change: str | None,
    detail: str | None = None,
) -> list[str]:
    """T-4680 gate. Returns resolve-note fragments to append to --with
    (waivers and cited shas); raises SopStubNotFilled on refusal.

    Fail-fast BEFORE the lock/git-mv, like _refuse_resolve_if_overrun. Cost on
    a ticket with no `sop:` label: one extra ticket read, and no git or
    subprocess call (pinned by test). An unreadable ticket is let through (not
    this gate's job, same posture as the overrun guard). A "Rejected:" resolve
    (T-0641, the pantheon-pwa /reject route) is exempt: a rejected run is not a
    completed first run with a procedure to record (review MED-2)."""
    waiver = None
    if sop_no_change is not None:
        waiver = sop_no_change.strip()
        if not waiver:
            raise SopStubNotFilled(
                f"resolve refused: {tid} --sop-no-change needs a non-empty reason "
                f"(say why the run taught nothing new; T-4680)."
            )
    if _is_reject_note(detail):
        return []
    try:
        fields = parse_ticket(_find_ticket(tid)).fields
    except Exception:
        return []
    stems = _sop_labels(fields)
    if not stems:
        return []

    notes: list[str] = []
    for stem in stems:
        if not SOP_STEM_RE.match(stem):
            raise SopStubNotFilled(
                f"resolve refused: {tid} label sop:{stem} is not a valid SOP file "
                f"stem (lowercase letters, digits, '.', '_', '-'). Fix the label "
                f"(T-4680)."
            )
        rel = f"sop/standard/{stem}.md"
        path = REPO_ROOT / rel
        if not path.is_file():
            if waiver is not None:
                notes.append(f"sop-no-change({stem}): {waiver}")
                continue
            raise SopStubNotFilled(
                f"resolve refused: {tid} is labelled sop:{stem} but {rel} does not "
                f"exist. Fix the label, or pass --sop-no-change '<why>' "
                f"(writing-sops-from-first-use.md step 2; T-4680)."
            )
        try:
            is_stub, first_run = _sop_stub_status(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            # Fail-closed (review LOW-3): an unreadable SOP cannot be shown to
            # be filled. The waiver stays the way out, as for a missing file.
            if waiver is not None:
                notes.append(f"sop-no-change({stem}): {waiver}")
                continue
            raise SopStubNotFilled(
                f"resolve refused: {tid} is labelled sop:{stem} but {rel} could not "
                f"be read as UTF-8 text ({type(exc).__name__}). Fix the file, or "
                f"pass --sop-no-change '<why>' (T-4680)."
            ) from exc
        if not is_stub:
            continue
        if sop_updated is not None:
            short = _check_sop_updated_sha(tid, sop_updated, rel)
            notes.append(f"sop-updated({stem}): {short}")
            continue
        if _sop_changed_in_arc(tid, rel):
            continue
        if waiver is not None:
            notes.append(f"sop-no-change({stem}): {waiver}")
            continue
        where = f"{rel}:{first_run}" if first_run else rel
        raise SopStubNotFilled(
            f"resolve refused: {tid} is labelled sop:{stem} but {rel} is still a "
            f"STUB and did not change in this ticket's arc (no commit mentioning "
            f"{tid} modifies it). Fill its \"{SOP_FIRST_RUN_PREFIX}\" section "
            f"({where}) with the steps actually taken, then resolve; or pass "
            f"--sop-updated <sha> for the landed commit that filled it, or "
            f"--sop-no-change '<why>' if the run taught nothing new "
            f"(writing-sops-from-first-use.md step 3; T-4680)."
        )
    return notes


def _resolve_field_updates_for_awaiting_merge(tid: str) -> dict | None:
    """T-4119: when resolving a ticket in `substate: awaiting-merge`, strip both
    the substate and its `awaiting_merge_ref` pointer in the SAME atomic write
    as the done-transition — the merge this substate was holding a WIP slot
    open for is realized by the resolve itself, so the pointer must not
    survive into done/.

    Returns None when there is nothing to strip. `appetite-overrun` can never
    reach this call (already refused earlier in cmd_resolve by
    _refuse_resolve_if_overrun), and `dropped` is only ever written
    atomically with the very done-transition that creates it (cmd_drop), so
    it can never be present on a ticket resolve is about to act on —
    `awaiting-merge` is the only substate value this function can observe in
    practice, but the check is still by VALUE (not "substate is present") so
    a future substate this function doesn't know about is left alone rather
    than silently stripped.

    Fail-safe: any read error returns None rather than blocking a legitimate
    resolve on a corrupt/missing field (same posture as
    _refuse_resolve_if_overrun / _warn_unmet_deps)."""
    try:
        fields = parse_ticket(_find_ticket(tid)).fields
    except Exception:
        return None
    if fields.get("substate") == "awaiting-merge":
        return {"substate": _DELETE_FIELD, "awaiting_merge_ref": _DELETE_FIELD}
    return None


def cmd_await_merge(args: argparse.Namespace) -> int:
    """T-4119: hold a WIP slot open for a work-complete ticket awaiting an
    external (HO-exclusive, T-1673) merge, without unclaiming it.

    Frontmatter-only edit (no git-mv, no _do_transition) — same shape as
    cmd_block, since the ticket stays in active/ throughout. Sets
    `substate: awaiting-merge` + `awaiting_merge_ref: <ref>` (`--ref`), or
    clears both (`--clear`). Only valid on an active ticket. Refuses to
    clobber a DIFFERENT pre-existing substate (e.g. appetite-overrun) on
    either the set or the clear path — that substate isn't this verb's to
    touch. Re-setting `--ref` while already `substate: awaiting-merge` is
    allowed (updates the ref in place, e.g. a PR reopened under a new
    number) rather than refused, per Orchestrator's plan-approval ratification.
    """
    ref = (getattr(args, "ref", None) or "").strip()
    clear = bool(getattr(args, "clear", False))
    no_push = getattr(args, "no_push", False)

    if clear and ref:
        raise TicketRefusal("await-merge: --ref and --clear are mutually exclusive")
    if not clear and not ref:
        raise TicketRefusal(
            "await-merge requires --ref <repo>#<pr>|<repo>:<branch> (or --clear "
            "to remove an existing awaiting-merge substate)"
        )

    # Fail-fast pre-lock check (avoid taking the lock for an obviously-bad ask).
    pre = _find_ticket(args.ticket)
    if pre.parent.name != "active":
        raise TicketRefusal(
            f"await-merge refused: {args.ticket} is in `{pre.parent.name}`, not "
            f"`active` — await-merge holds a WIP slot open for a ticket whose "
            f"OWN work is done but still active; it is only meaningful there."
        )

    with git_mutation_lock(label=f"await-merge {args.ticket}"):
        _pull_if_needed(no_push)
        path = _find_ticket(args.ticket)
        if path.parent.name != "active":
            raise TicketRefusal(
                f"await-merge refused: {args.ticket} moved out of `active` "
                f"before the lock; nothing changed."
            )
        fields, body = parse_front_matter(path.read_text(encoding="utf-8"))
        old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
        current_substate = fields.get("substate")

        if clear:
            if current_substate is None:
                # Idempotent no-op — matches block --clear-not-before's
                # convention; skip the commit to preserve audit signal.
                print(f"await-merge {args.ticket}: substate already absent; no change")
                return 0
            if current_substate != "awaiting-merge":
                raise TicketRefusal(
                    f"await-merge --clear refused: {args.ticket} substate is "
                    f"`{current_substate}`, not `awaiting-merge` — not this "
                    f"verb's to clear."
                )
            fields.pop("substate", None)
            fields.pop("awaiting_merge_ref", None)
            detail = "awaiting-merge cleared"
        else:
            if current_substate is not None and current_substate != "awaiting-merge":
                raise TicketRefusal(
                    f"await-merge refused: {args.ticket} is already in substate "
                    f"`{current_substate}` — not this verb's to clobber. Resolve "
                    f"that substate first."
                )
            fields["substate"] = "awaiting-merge"
            fields["awaiting_merge_ref"] = ref
            detail = f"awaiting merge: {ref}"

        actor = _actor()
        # T-4119 REBOUND-1 (reviewer finding #7): past-tense transition-log
        # verb, matching every sibling verb's convention in the log (e.g.
        # cmd_block logs "blocked", not "block"). The CLI subcommand name and
        # commit-message verb (`_build_commit_message`'s "await-merge" arm)
        # are unaffected — only the in-body log entry's verb text changes.
        log_verb = "awaiting-merge cleared" if clear else "awaiting-merge set"
        body = append_transition(body, actor, log_verb, detail)
        new_text = _build_ticket_text(fields, body, old_fields=old_fields)
        path.write_text(new_text, encoding="utf-8")

        title = _extract_title(body)
        commit_message = _build_commit_message("await-merge", args.ticket, title, detail)
        _commit_and_push([path], commit_message, no_push=no_push)

    print(f"await-merge {args.ticket}: {detail}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    detail = getattr(args, "with_summary", None)
    register_row = _register_gate(args, "resolve")  # T-5015: before any mutation
    _warn_decision_line_shape(detail, surface=f"{args.ticket} resolve --with")
    _warn_unmet_deps(args.ticket, "resolving")
    # T-3440 / ORG-089 §Decision.4: refuse resolve on appetite-overrun (typed).
    _refuse_resolve_if_overrun(args.ticket)
    # T-4680: first-use SOP capture gate. Waivers / cited shas ride the
    # resolve note (appended, so a leading "Rejected:" is preserved).
    sop_notes = _refuse_resolve_if_sop_stub(
        args.ticket,
        getattr(args, "sop_updated", None),
        getattr(args, "sop_no_change", None),
        detail,
    )
    if sop_notes:
        detail = "; ".join([detail] + sop_notes) if detail else "; ".join(sop_notes)
    # T-2842 / ORG-065 BT2: validate --receipt entries fail-fast, BEFORE
    # _do_transition (same "abort before touching the lock/git-mv" posture
    # as every other file-time/transition-time shape check).
    receipts_to_attach = _parse_receipt_entries(
        "receipts", getattr(args, "receipt", None) or []
    )
    # T-4119: strip substate: awaiting-merge + awaiting_merge_ref in the same
    # atomic write as the done-transition, when present.
    field_updates = _resolve_field_updates_for_awaiting_merge(args.ticket)
    rc = _do_transition(
        tid=args.ticket,
        expected_state="active",
        new_state="done",
        verb="resolve",
        detail=detail,
        no_push=getattr(args, "no_push", False),
        cascade_callback=_cascade_unblock_dependants,
        receipts_to_attach=receipts_to_attach,
        field_updates=field_updates,
        register_row=register_row,
        successor=getattr(args, "successor", None),
        # T-3440 / ORG-089: log the shipped outcome for the calibration
        # warm-up. T-3451: folded into _do_transition's own commit (was a
        # disconnected post-transition call that left the shard uncommitted).
        appetite_outcome="shipped",
    )
    # T-2273: AFTER the transition, and only on success. The warning asks a
    # question at the one moment the author is still present; it must never look
    # like a gate on the resolve, and a failed resolve has nothing to ask about.
    if rc == 0:
        _warn_recurrence_prediction(detail or "", args.ticket)
        _warn_leftover_worktree(args.ticket)
    return rc


def _leftover_worktrees(tid: str, root: Path | None = None) -> list[tuple[str, str]]:
    """(path, branch) for each worktree of `root` (default REPO_ROOT) still on
    agent/<tid> or agent/<tid>-<suffix>. Best-effort: any git error → []."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(root or REPO_ROOT), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    found, path = [], None
    want = f"refs/heads/agent/{tid}"
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch ") and path:
            ref = line[len("branch "):]
            if ref == want or ref.startswith(want + "-"):
                found.append((path, ref[len("refs/heads/"):]))
    return found


def _warn_leftover_worktree(tid: str) -> None:
    """T-4674: after a resolve, flag a worktree still on the ticket's agent branch. The
    merge SOP (step 8) asks for its removal, but nothing enforced it, and the sweeper
    alone cannot be relied on (it judges merged-ness, not the ticket). Never a gate."""
    try:
        for path, branch in _leftover_worktrees(tid):
            print(
                f"warning: {tid} resolved but worktree {path} is still on {branch}. Once "
                f"the merge is confirmed: git worktree remove {path} && git branch -D {branch} "
                f"(T-4674; merging-an-agent-branch-pr.md step 8)",
                file=sys.stderr,
            )
    except Exception:
        return


# ---------------------------------------------------------------------------
# T-3440 / ORG-089 §Decision.4: the four appetite re-decision verbs.
#   re-earn         — new appetite + rationale; clears overrun, stays active,
#                     restarts the overrun clock (D-P). Requires overrun.
#   adjust-appetite — original score was wrong; corrects appetite in place,
#                     clears overrun if present, does NOT restart the clock.
#   drop            — terminal; open → done/ with substate: dropped (skips the
#                     ORG-065 closure gate — a dropped bet is abandoned, not
#                     satisfied, D-N).
#   reopen          — dropped (done/) → inbox/; clears the dropped disposition,
#                     re-earns a new appetite, restarts the clock.
# Resolve stays terminal and is never overloaded.
# ---------------------------------------------------------------------------


def _record_appetite_outcome(tid: str, outcome: str) -> Path | None:
    """Best-effort append of a terminal appetite event to the warm-up ledger
    (ledger/appetite/<year>-W<NN>.jsonl) on resolve->shipped / drop->dropped /
    reopen->reopened. Reads the ticket's current appetite + computes active-spend
    from its transition log so the warm-up carries the calibration signal
    (appetite vs actual). Fail-safe: never raises into the verb — the outcome log
    is calibration data, not a gate (ORG-089 §Build plan). Co-located writer +
    spend machine in ticket_grooming, lazy-imported (no hard dep).

    Returns the shard Path on a successful write (T-3451: `_do_transition` folds
    this into the SAME commit as the transition it fired from), None on any
    failure/no-op."""
    try:
        from tools import ticket_grooming
        from tools.ticket_purpose import normalize_appetite, AppetiteError
        now = datetime.now(timezone.utc)
        t = parse_ticket(_find_ticket(tid))
        try:
            appetite = normalize_appetite(t.fields.get("appetite"))
        except AppetiteError:
            appetite = None
        spend_s = ticket_grooming.active_spend_from_transition_log(t, now=now)
        # Pass an explicit ledger_root derived from ticket.REPO_ROOT (the root
        # ticket lifecycle tests patch) rather than letting the writer fall back
        # to ticket_grooming.REPO_ROOT — keeps outcome logging isolated in tests
        # and byte-identical in production (both resolve to the same repo path).
        return ticket_grooming._appetite_outcome_log(
            tid=tid, appetite=appetite, outcome=outcome, now=now,
            active_spend_s=spend_s,
            ledger_root=REPO_ROOT / "ledger" / "appetite",
        )
    except Exception:  # noqa: BLE001 — outcome logging must never break a verb
        return None


def _inplace_appetite_edit(
    tid: str,
    *,
    verb: str,
    appetite: int,
    rationale: str,
    no_push: bool,
    require_overrun: bool,
    clear_overrun: bool,
) -> int:
    """Shared in-place (no dir move) frontmatter edit for re-earn / adjust-appetite.

    The authoritative read + edit + write happen INSIDE the git_mutation_lock
    after the pull, so a concurrent writer cannot interleave (T-1770 posture).
    """
    # Fail-fast pre-lock check (avoid taking the lock for an obviously-bad ask).
    pre = _find_ticket(tid)
    if pre.parent.name == "done":
        raise TicketRefusal(
            f"{verb} refused: {tid} is in done/ (terminal). Reopen it first "
            f"(`reopen --re-earn-appetite <N> --rationale ...`) to keep working."
        )

    detail = f"appetite -> {appetite}; {rationale}"
    commit_message = _build_commit_message(verb, tid, None, detail)
    with git_mutation_lock(label=f"{verb} {tid}"):
        _pull_if_needed(no_push)
        path = _find_ticket(tid)
        if path.parent.name == "done":
            raise TicketRefusal(
                f"{verb} refused: {tid} moved to done/ before the lock; nothing changed."
            )
        fields, body = parse_front_matter(path.read_text(encoding="utf-8"))
        old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
        if require_overrun and fields.get("substate") != "appetite-overrun":
            raise TicketRefusal(
                f"{verb} refused: {tid} is not in substate: appetite-overrun. "
                f"re-earn is a re-decision on an overrun bet; use adjust-appetite "
                f"to correct a score outside overrun."
            )
        fields["appetite"] = appetite
        if clear_overrun and fields.get("substate") == "appetite-overrun":
            fields.pop("substate", None)
        actor = _actor()
        body = append_transition(body, actor, verb, detail)
        path.write_text(_build_ticket_text(fields, body, old_fields=old_fields), encoding="utf-8")
        _commit_and_push([path], commit_message, no_push=no_push)
    print(f"{verb} {tid}: appetite -> {appetite} ({rationale})")
    return 0


def cmd_re_earn(args: argparse.Namespace) -> int:
    from tools.ticket_purpose import normalize_appetite, AppetiteError
    try:
        appetite = normalize_appetite(args.appetite)
    except AppetiteError as exc:
        raise TicketRefusal(str(exc)) from exc
    rationale = (args.rationale or "").strip()
    if not rationale:
        raise TicketRefusal("re-earn requires a non-empty --rationale")
    return _inplace_appetite_edit(
        args.ticket, verb="re-earn", appetite=appetite, rationale=rationale,
        no_push=getattr(args, "no_push", False),
        require_overrun=True, clear_overrun=True,
    )


def cmd_adjust_appetite(args: argparse.Namespace) -> int:
    from tools.ticket_purpose import normalize_appetite, AppetiteError
    try:
        appetite = normalize_appetite(args.appetite)
    except AppetiteError as exc:
        raise TicketRefusal(str(exc)) from exc
    rationale = (args.rationale or "").strip()
    if not rationale:
        raise TicketRefusal("adjust-appetite requires a non-empty --rationale")
    return _inplace_appetite_edit(
        args.ticket, verb="adjust-appetite", appetite=appetite, rationale=rationale,
        no_push=getattr(args, "no_push", False),
        require_overrun=False, clear_overrun=True,
    )


def cmd_tier_rank(args: argparse.Namespace) -> int:
    """Set or clear a ticket's optional intra-tier ordering rank (T-5715)."""
    reason = (getattr(args, "reason", None) or "").strip()
    if not reason:
        raise TicketRefusal("tier-rank requires a non-empty --reason")

    clear = bool(getattr(args, "clear", False))
    raw_rank = getattr(args, "rank", None)
    if clear == (raw_rank is not None):
        raise TicketRefusal("tier-rank requires exactly one of --rank INT or --clear")
    rank: int | None = None
    if raw_rank is not None:
        raw_text = str(raw_rank)
        if re.fullmatch(r"-?[0-9]+", raw_text) is None:
            raise TicketRefusal("tier-rank --rank must be an integer")
        rank = int(raw_text)

    pre = _find_ticket(args.ticket)
    if pre.parent.name == "done":
        raise TicketRefusal(
            f"tier-rank refused: {args.ticket} is in done/ (terminal); "
            "ordering only applies to open work"
        )

    with git_mutation_lock(label=f"tier-rank {args.ticket}"):
        _pull_if_needed(getattr(args, "no_push", False))
        path = _find_ticket(args.ticket)
        if path.parent.name == "done":
            raise TicketRefusal(
                f"tier-rank refused: {args.ticket} moved to done/ before the lock; "
                "nothing changed"
            )
        fields, body = parse_front_matter(path.read_text(encoding="utf-8"))
        old_fields = _fm_snapshot(fields)
        old_rank = _tier_rank(fields)

        if clear:
            if "tier_rank" not in fields:
                print(f"tier-rank {args.ticket}: already absent; no change")
                return 0
            fields.pop("tier_rank", None)
            old_rank_text = str(old_rank) if old_rank is not None else "invalid"
            detail = f"tier_rank {old_rank_text} removed; reason: {reason}"
            log_verb = "tier-rank-cleared"
        else:
            fields["tier_rank"] = rank
            detail = f"tier_rank -> {rank}; reason: {reason}"
            log_verb = "tier-ranked"

        body = append_transition(body, _actor(), log_verb, detail)
        path.write_text(
            _build_ticket_text(fields, body, old_fields=old_fields), encoding="utf-8"
        )
        commit_message = _build_commit_message(
            "tier-rank", args.ticket, None, detail
        )
        _commit_and_push(
            [path], commit_message, no_push=getattr(args, "no_push", False)
        )

    print(f"tier-rank {args.ticket}: {detail}")
    return 0


# ---------------------------------------------------------------------------
# T-5522: clear a STALE operator_intent (HO-only).
#
# T-5015 keeps `operator_intent` on a retarget away from operator so a real
# the operator carrier stays register-gated at close. When the ask was instead
# pulled to agent tier and decided there, that preserved field forces any
# closer to fabricate an operator ruling. This verb removes it, with one audited
# transition-log line, so the register only ever holds his real rulings.
# ---------------------------------------------------------------------------

CLEAR_OPERATOR_INTENT_ROLE = "head_engineering_orchestrator"


def _is_operator_name(value: str) -> bool:
    """True when *value* names the operator in any case or `role:` form (T-5522)."""
    v = value.strip().lower()
    if v.startswith("role:"):
        v = v[len("role:"):].strip()
    return _resolve_persona(v) == "operator"


def cmd_clear_operator_intent(args: argparse.Namespace) -> int:
    """Remove a stale `operator_intent` from an agent-tier ticket (T-5522).

    HO-only. The actor check is ADVISORY, same-user trust like PANTHEON_ACTOR
    everywhere else in ticket.py; it is not an auth boundary.
    """
    verb = "clear-operator-intent"
    tid = args.ticket
    # Lazy, but free: tools.agent_role_map is already imported at module scope.
    from tools.agent_role_map import resolve_role

    persona = _claiming_persona()
    if resolve_role(persona) != CLEAR_OPERATOR_INTENT_ROLE:
        raise TicketRefusal(
            f"{verb} refused: actor {_actor()!r} (persona {persona!r}) is not the "
            f"head engineering orchestrator. Clearing an item off the operator's desk is "
            f"an HO-operator boundary decision (T-5522): ask orchestrator to run it."
        )
    decided_by = (getattr(args, "decided_by", None) or "").strip()
    ref = (getattr(args, "ref", None) or "").strip()
    why = (getattr(args, "why", None) or "").strip()
    missing = [f for f, v in (("--decided-by", decided_by), ("--ref", ref),
                              ("--why", why)) if not v]
    if missing:
        raise TicketRefusal(f"{verb} refused: empty {', '.join(missing)}")
    if _is_operator_name(decided_by):
        raise TicketRefusal(
            f"{verb} refused: --decided-by {decided_by!r} names the operator. A real "
            f"the operator ruling belongs in his decision register, not erased by a "
            f"clear: use `ticket.py resolve {tid} --ruling \"<his call, TG id>\" "
            f"--implies ... --delegable ... --category ...` (T-5015)."
        )

    def _check(path: Path, fields: dict) -> None:
        if path.parent.name == "done":
            raise TicketRefusal(
                f"{verb} refused: {tid} is in done/ (closed provenance is not rewritten)."
            )
        if fields.get("target") == "operator":
            raise TicketRefusal(
                f"{verb} refused: {tid} has target: operator, so it is still the operator's. "
                f"Retarget it off the operator first, or resolve it with --ruling."
            )
        if fields.get("intent"):
            raise TicketRefusal(
                f"{verb} refused: {tid} carries a live intent: {fields.get('intent')!r} "
                f"(still the operator's ask)."
            )
        if not fields.get("operator_intent"):
            raise TicketRefusal(
                f"{verb} refused: {tid} has no operator_intent; nothing to clear."
            )

    pre = _find_ticket(tid)
    _check(pre, parse_front_matter(pre.read_text(encoding="utf-8"))[0])

    no_push = getattr(args, "no_push", False)
    with git_mutation_lock(label=f"{verb} {tid}"):
        _pull_if_needed(no_push)
        path = _find_ticket(tid)
        fields, body = parse_front_matter(path.read_text(encoding="utf-8"))
        _check(path, fields)  # re-verify inside the lock (T-1770 posture)
        old_fields = _fm_snapshot(fields)
        old_value = fields.pop("operator_intent")
        detail = (f"operator_intent={old_value} cleared; decided-by={decided_by}; "
                  f"ref={ref}; why={why}")
        body = append_transition(body, _actor(), verb, detail)
        path.write_text(_build_ticket_text(fields, body, old_fields=old_fields),
                        encoding="utf-8")
        _commit_and_push([path], _build_commit_message(verb, tid, None, detail),
                         no_push=no_push)
    print(f"{verb} {tid}: operator_intent={old_value} cleared (decided-by {decided_by})")
    return 0


def _require_ho_actor(verb: str) -> None:
    """HO-only actor check, same source of identity as clear-operator-intent (T-5522).

    ADVISORY: `_actor()` reads $PANTHEON_ACTOR else the OS user, and a subagent runs as
    its dispatcher's OS user, so this is same-user trust, not an auth boundary."""
    from tools.agent_role_map import resolve_role

    persona = _claiming_persona()
    if resolve_role(persona) != CLEAR_OPERATOR_INTENT_ROLE:
        raise TicketRefusal(
            f"{verb} refused: actor {_actor()!r} (persona {persona!r}) is not the "
            f"head engineering orchestrator. Accepting or rejecting a security-gated "
            f"ticket is an HO decision (T-5678, ORG-105 s4): ask orchestrator to run it."
        )


def _security_verb_check(verb: str, tid: str, path: Path, fields: dict) -> None:
    if path.parent.name == "done":
        raise TicketRefusal(f"{verb} refused: {tid} is in done/.")
    if not _security_gate.is_pending(fields):
        raise TicketRefusal(
            f"{verb} refused: {tid} is not security_gate: pending "
            f"(found {fields.get('security_gate')!r})."
        )


def cmd_security_accept(args: argparse.Namespace) -> int:
    """T-5678 (HO-only): pending -> accepted (pullable), or released as not-security.

    `--rank` accepts. `--not-security "<reason>"` releases instead (clears the gate and
    records the reason): the cheap exit for keyword false positives. A pending ticket
    that an automated filer left UNCLASSIFIED needs `--threat-chain` here when accepted
    (the HO supplies the chain at acceptance); a classified one refuses the flag."""
    verb = "security-accept"
    tid = args.ticket
    _require_ho_actor(verb)
    rank = (getattr(args, "rank", None) or "").strip()
    note = (getattr(args, "note", None) or "").strip()
    not_sec = (getattr(args, "not_security", None) or "").strip()
    chain = (getattr(args, "threat_chain", None) or "").strip()
    just = getattr(args, "path_justification", None)
    if bool(rank) == bool(not_sec):
        raise TicketRefusal(
            f"{verb} refused: give exactly one of --rank "
            f"{{{','.join(_security_gate.RANKS)}}} (accept) or --not-security '<reason>' (release)"
        )
    if rank and rank not in _security_gate.RANKS:
        raise TicketRefusal(f"{verb} refused: --rank must be one of {', '.join(_security_gate.RANKS)}")
    if not note:
        raise TicketRefusal(f"{verb} refused: empty --note")
    if not_sec and chain:
        raise TicketRefusal(f"{verb} refused: --not-security and --threat-chain are exclusive")

    def _gate(path: Path, fields: dict) -> dict:
        _security_verb_check(verb, tid, path, fields)
        unclassified = _security_gate.is_unclassified(fields)
        if rank and unclassified and not chain:
            raise TicketRefusal(
                f"{verb} refused: {tid} was filed by an automated filer with no chain. "
                f"Supply --threat-chain 'foothold=<F>;impact=<I>;path=<text>' "
                f"(F in {{{','.join(_security_gate.FOOTHOLDS)}}}, I in "
                f"{{{','.join(_security_gate.ASSETS)}}}), or release it with "
                f"--not-security '<reason>'."
            )
        if chain and not unclassified:
            raise TicketRefusal(
                f"{verb} refused: {tid} already carries a chain; --threat-chain applies "
                f"only to an unclassified ticket."
            )
        if chain:
            try:
                return _security_gate.chain_fields(chain, just)
            except _security_gate.GateRefusal as exc:
                raise TicketRefusal(f"{verb} refused: {exc}") from None
        return {}

    pre = _find_ticket(tid)
    _gate(pre, parse_front_matter(pre.read_text(encoding="utf-8"))[0])

    no_push = getattr(args, "no_push", False)
    with git_mutation_lock(label=f"{verb} {tid}"):
        _pull_if_needed(no_push)
        path = _find_ticket(tid)
        fields, body = parse_front_matter(path.read_text(encoding="utf-8"))
        chain_updates = _gate(path, fields)  # re-verify inside the lock (T-1770)
        old_fields = _fm_snapshot(fields)
        if not_sec:
            fields.pop("security_gate", None)
            fields.pop("threat_unclassified", None)
            fields["not_security_reason"] = not_sec
            detail = f"released as not-security: {not_sec}; {note}"
            result = "released (not security)"
        else:
            fields.update(chain_updates)
            fields.pop("threat_unclassified", None)
            fields["security_gate"] = "accepted"
            fields["security_rank"] = rank
            detail = f"rank={rank}; {note}" + ("; chain supplied at accept" if chain_updates else "")
            result = f"accepted ({rank})"
        body = append_transition(body, _actor(), verb, detail)
        path.write_text(_build_ticket_text(fields, body, old_fields=old_fields), encoding="utf-8")
        _commit_and_push([path], _build_commit_message(verb, tid, None, detail), no_push=no_push)
    print(f"{verb} {tid}: {result}")
    return 0


def cmd_security_reject(args: argparse.Namespace) -> int:
    """T-5678 (HO-only): pending -> dropped (done/, substate: dropped) with the note."""
    verb = "security-reject"
    tid = args.ticket
    _require_ho_actor(verb)
    note = (getattr(args, "note", None) or "").strip()
    if not note:
        raise TicketRefusal(f"{verb} refused: empty --note")
    path = _find_ticket(tid)
    fields = parse_front_matter(path.read_text(encoding="utf-8"))[0]
    _security_verb_check(verb, tid, path, fields)
    origin = path.parent.name
    return _do_transition(
        tid=tid,
        expected_state=origin,
        new_state="done",
        verb=verb,
        detail=note,
        no_push=getattr(args, "no_push", False),
        field_updates={
            "substate": "dropped",
            "dropped_reason": f"security-reject (ORG-105 s4): {note}",
            "dropped_from_state": origin,
            "security_gate": "rejected",
        },
        skip_closure_gate=True,  # dropped != satisfied
        appetite_outcome="dropped",
    )


def cmd_drop(args: argparse.Namespace) -> int:
    dropped_reason = (args.dropped_reason or "").strip()
    if not dropped_reason:
        raise TicketRefusal("drop requires a non-empty --dropped-reason")
    # Resolve the origin dir up front so _do_transition gets the right
    # expected_state; it re-verifies inside the lock (T-2140) so a concurrent
    # move is caught, not acted on.
    path = _find_ticket(args.ticket)
    origin = path.parent.name
    if origin == "done":
        raise TicketRefusal(
            f"drop refused: {args.ticket} is already in done/ "
            f"(if it is a dropped bet, it is already terminal)."
        )
    # T-4119 REBOUND-1 (reviewer finding #5): a ticket dropped directly out of
    # `substate: awaiting-merge` must not carry its `awaiting_merge_ref`
    # pointer into done/. The field_updates below unconditionally overwrite
    # `substate` to "dropped", but that overwrite does not touch
    # `awaiting_merge_ref` — left alone, it survives as a dangling ref to a
    # merge nobody is watching anymore.
    pre_fields, _pre_body = parse_front_matter(path.read_text(encoding="utf-8"))
    field_updates = {
        "substate": "dropped",
        "dropped_reason": dropped_reason,
        "dropped_from_state": origin,
    }
    if pre_fields.get("awaiting_merge_ref") is not None:
        field_updates["awaiting_merge_ref"] = _DELETE_FIELD
    rc = _do_transition(
        tid=args.ticket,
        expected_state=origin,
        new_state="done",
        verb="drop",
        detail=dropped_reason,
        no_push=getattr(args, "no_push", False),
        field_updates=field_updates,
        skip_closure_gate=True,  # dropped != satisfied (D-N)
        successor=getattr(args, "successor", None),
        # T-3451: folded into _do_transition's own commit — see cmd_resolve.
        appetite_outcome="dropped",
    )
    return rc


def cmd_reopen(args: argparse.Namespace) -> int:
    from tools.ticket_purpose import normalize_appetite, AppetiteError
    try:
        appetite = normalize_appetite(args.re_earn_appetite)
    except AppetiteError as exc:
        raise TicketRefusal(str(exc)) from exc
    if appetite is None:
        raise TicketRefusal("reopen requires --re-earn-appetite <1-10>")
    rationale = (args.rationale or "").strip()
    if not rationale:
        raise TicketRefusal("reopen requires a non-empty --rationale")
    # Must be a dropped bet in done/ (fail-fast; _do_transition re-verifies).
    path = _find_ticket(args.ticket)
    if path.parent.name != "done":
        raise TicketRefusal(
            f"reopen refused: {args.ticket} is in `{path.parent.name}`, not done/ "
            f"(reopen is for a DROPPED bet)."
        )
    fields, _body = parse_front_matter(path.read_text(encoding="utf-8"))
    if fields.get("substate") != "dropped":
        raise TicketRefusal(
            f"reopen refused: {args.ticket} is in done/ but not substate: dropped "
            f"(reopen clears a DROPPED disposition; a shipped ticket is not reopened here)."
        )
    field_updates = {
        "substate": _DELETE_FIELD,
        "dropped_reason": _DELETE_FIELD,
        "dropped_from_state": _DELETE_FIELD,
        "appetite": appetite,
    }
    rc = _do_transition(
        tid=args.ticket,
        expected_state="done",
        new_state="inbox",  # reopened dropped bet needs re-triage/re-claim (Q5)
        verb="reopen",
        detail=f"re-earn appetite {appetite}; {rationale}",
        no_push=getattr(args, "no_push", False),
        field_updates=field_updates,
        # T-3451: folded into _do_transition's own commit — see cmd_resolve.
        appetite_outcome="reopened",
    )
    return rc


# ---------------------------------------------------------------------------
# T-0653: noodle/action close lifecycle for target=operator queue.
#
# the operator's target=operator tickets carry intent: action|ratify|noodle. ratify
# closes via cmd_resolve (the existing ratify-lift path). action + noodle
# need their own close verbs:
#   - close-noodle: the operator acknowledges + closes; optional --spawn-to creates
#     a follow-up action ticket for an agent.
#   - close-action: the operator did the action; close + AUTO-spawn a review
#     ticket back to the original requester ("did this unblock you?").
#
# Both verbs cascade-unblock dependants (ORG-013) AND, when spawning, fold
# the child file into the same atomic commit so we never have a half-closed
# parent + missing child window.
# ---------------------------------------------------------------------------


def _spawn_child_ticket(
    *,
    parent_tid: str,
    target: str,
    intent: str | None,
    labels: list[str],
    title: str,
    ask: str,
    depends_on: list[str] | None = None,
    _locked: bool = False,
) -> Path:
    """Allocate next T-NNNN + write a child ticket file in tickets/inbox/.

    Used by close-noodle (--spawn-to), close-action (auto-review), and the
    T-0852 ratify file-time impl-followup spawn. The returned Path is folded
    into the parent's atomic commit (by the close path's cascade callback, or
    by cmd_file's affected-paths list) — both writes land together so a crash
    between them is impossible.

    The child's `requester` is the actor (or `recovery`). The parent_ticket field
    preserves lineage. `intent` is set only when target=operator (per T-0465).

    depends_on: child's depends_on list. For the ratify impl-spawn (T-0852)
        this is [parent_tid] so the impl ticket is gated on the ratify and
        ORG-013 cascade-unblocks it when the operator resolves the ratify. Default
        [] (close-noodle/close-action children have no gate).

    _locked: when True, the CALLER already holds _allocation_lock() — skip
        re-acquiring it. cmd_file spawns the impl child INSIDE its own
        allocation lock (so the child's _next_ticket_id sees the just-written
        parent on disk and gets parent_tid+1); re-acquiring the blocking
        LOCK_EX from the same process on a second fd would self-deadlock.
    """
    actor = _actor()
    body = _build_initial_body(title, ask, "", actor)
    # _allocation_lock() spans allocate→write so no concurrent writer can
    # claim the same T-NNNN between our scan and our file creation (T-0786).
    # _locked=True: caller holds it already (cmd_file's ratify-impl spawn).
    lock_cm = contextlib.nullcontext() if _locked else _allocation_lock()
    with lock_cm:
        tid = _next_ticket_id()
        parent_fields, _parent_body = parse_front_matter(
            _find_ticket(parent_tid).read_text(encoding="utf-8")
        )
        fields = _child_ticket_fields(
            tid=tid, parent_tid=parent_tid, parent_fields=parent_fields,
            actor=actor, target=target, intent=intent, labels=labels,
            depends_on=depends_on,
        )
        new_path = TICKETS_ROOT / "inbox" / f"{tid}.md"
        # T-4885: exclusive create; FileExistsError propagates to cmd_file, which
        # re-allocates the whole attempt without touching the foreign file.
        _write_new_ticket_exclusive(
            new_path, _build_ticket_text(fields, body, old_fields=None)
        )
    return new_path


def _child_ticket_fields(
    *,
    tid: str,
    parent_tid: str,
    parent_fields: dict,
    actor: str,
    target: str,
    intent: str | None,
    labels: list[str],
    depends_on: list[str] | None,
) -> dict:
    """Pure: the frontmatter of a spawned child ticket (see _spawn_child_ticket).

    Split out (T-4760) so the parent's verb can run the ORG-021 registry check
    on the child's fields BEFORE the parent is written (_preflight_child_ticket)."""
    fields: dict = {
        "id": tid,
        "created": _now_iso(),
        "requester": actor if actor in _valid_target_values() else "recovery",
        "target": target,
        "state": "inbox",
        "parent_ticket": parent_tid,
        "depends_on": list(depends_on) if depends_on else [],
        # T-3168: spawned children inherit membership and point to their
        # actual parent ticket.  This is the one child-filing path that
        # does not cross the public ``file`` parser.
        "project_id": parent_fields.get("project_id"),
        "nearest_intent_ref": f"ticket:{parent_tid}",
    }
    fields["serves"] = parent_fields.get("serves") or f"follow-up to {parent_tid}"
    if labels:
        fields["labels"] = labels
    if intent is not None and target == "operator":
        fields["intent"] = intent
    return fields


def _preflight_child_ticket(
    *,
    parent_tid: str,
    parent_fields: dict,
    target: str,
    intent: str | None,
    labels: list[str],
    depends_on: list[str] | None = None,
) -> None:
    """T-4760 (c): run the ORG-021 registry check on a to-be-spawned child's
    frontmatter BEFORE the parent write, so a refuse-mode refusal of the child
    can never land after the parent is already on disk / git-mv'd.

    The child's id is not allocated yet (allocation must see the parent on
    disk), so a placeholder id stands in; every other field is exactly what
    _spawn_child_ticket will write, built by the same _child_ticket_fields.
    `parent_fields` is the parent as read now: the fields the child inherits
    (project_id, serves) are not changed by the parent's own transition.
    The real spawn still runs the check (belt and braces).

    T-4762 (T-4760 r1 NIT-1): only refuse mode can block, so this is a no-op
    otherwise; warn-mode findings print once, from the real spawn's build."""
    if _fm_registry is None or _fm_registry.get_mode() != "refuse":
        return
    _check_frontmatter_registry(
        _child_ticket_fields(
            tid=f"{parent_tid}-child", parent_tid=parent_tid,
            parent_fields=parent_fields, actor=_actor(), target=target,
            intent=intent, labels=labels, depends_on=depends_on,
        ),
        None,
    )


def _close_lifecycle_cascade(spawn_fn):
    """Compose the standard ORG-013 cascade-unblock with an optional
    child-ticket spawn. Both side effects land in the parent's atomic
    commit via _do_transition's cascade_callback hook.
    """
    def cb(parent_tid):
        paths = _cascade_unblock_dependants(parent_tid)
        if spawn_fn is not None:
            child = spawn_fn(parent_tid)
            if child is not None:
                paths.append(child)
        return paths
    return cb


def _validate_operator_close(tid: str, expected_intent: str) -> tuple[Path, dict, str]:
    """Find the ticket, validate target=operator + intent matches the verb,
    return (path, fields, body_text). Refuses with TicketError otherwise.
    """
    path = _find_ticket(tid)
    text = path.read_text(encoding="utf-8")
    fields, body = parse_front_matter(text)
    if fields.get("target") != "operator":
        raise TicketRefusal(
            f"close-{expected_intent}: {tid} has target={fields.get('target')!r}; "
            f"only target=operator tickets close via this verb"
        )
    if fields.get("intent") != expected_intent:
        raise TicketRefusal(
            f"close-{expected_intent}: {tid} has intent={fields.get('intent')!r}; "
            f"only intent={expected_intent} closes via this verb "
            f"(use 'resolve' for ratify, 'close-noodle'/'close-action' for the others)"
        )
    return path, fields, body


def cmd_close_noodle(args: argparse.Namespace) -> int:
    detail = getattr(args, "with_summary", None)
    path, fields, body = _validate_operator_close(args.ticket, "noodle")
    register_row = _register_gate(args, "close-noodle")  # T-5015

    spawn_to = getattr(args, "spawn_to", None)
    as_action = getattr(args, "as_action", False)

    spawn_fn = None
    if spawn_to:
        if not as_action:
            raise TicketRefusal(
                "close-noodle --spawn-to requires --as-action "
                "(spawn-as-noodle not supported; file manually if you want that shape)"
            )
        # T-5239: --spawn-to is a caller-supplied target -> role-only (typed refusal).
        spawn_to_canon = _resolve_or_reject_target(spawn_to)
        parent_title = _extract_title(body) or args.ticket
        closer = _closer_display_name()
        child_title = f"[spawned from {args.ticket}] {parent_title}"
        child_ask = (
            f"Spawned from noodle {args.ticket} via {closer}'s close-noodle "
            f"--spawn-to {spawn_to_canon} --as-action.\n\n"
            f"Read the parent ticket ({args.ticket}, now in done/) for the full "
            f"discussion + {closer}'s close comment; execute the action it points to.\n\n"
            f"{closer}'s close comment: {detail or '(no comment)'}"
        )

        def make_child(parent_tid: str) -> Path:
            return _spawn_child_ticket(
                parent_tid=parent_tid,
                target=spawn_to_canon,
                intent=None,  # only set for target=operator
                labels=["spawned-from-noodle"],
                title=child_title,
                ask=child_ask,
            )

        spawn_fn = make_child
        # T-4760 (c): registry-check the child BEFORE the parent transition.
        _preflight_child_ticket(
            parent_tid=args.ticket, parent_fields=fields, target=spawn_to_canon,
            intent=None, labels=["spawned-from-noodle"],
        )

    return _do_transition(
        tid=args.ticket,
        expected_state=path.parent.name,
        new_state="done",
        verb="close-noodle",
        detail=detail,
        register_row=register_row,
        no_push=getattr(args, "no_push", False),
        cascade_callback=_close_lifecycle_cascade(spawn_fn),
        successor=getattr(args, "successor", None),
    )


def cmd_close_action(args: argparse.Namespace) -> int:
    detail = getattr(args, "with_summary", None)
    path, fields, body = _validate_operator_close(args.ticket, "action")
    register_row = _register_gate(args, "close-action")  # T-5015

    original_requester = fields.get("requester")
    if not isinstance(original_requester, str) or not original_requester.strip():
        raise TicketError(
            f"close-action: {args.ticket} has no requester field; "
            f"cannot route the auto-spawned review ticket"
        )
    # T-5239: requester is provenance (a persona); the review ticket routes by ROLE.
    original_requester = _derive_pool_target(original_requester)
    parent_title = _extract_title(body) or args.ticket
    closer = _closer_display_name()
    # intent only matters when target=operator (rare for review back to the
    # original requester, but supported for self-routed cases).
    review_intent = "action" if original_requester == "operator" else None
    review_labels = ["review", "follow-up", "auto-spawned-from-action-close"]

    def make_review(parent_tid: str) -> Path:
        review_title = f"Review: did closing {parent_tid} unblock the downstream work?"
        review_ask = (
            f"{closer} closed {parent_tid} ('{parent_title}') as completed.\n\n"
            f"{closer}'s close comment: {detail or '(no comment)'}\n\n"
            f"Verify the closure actually unblocked the downstream work this "
            f"action was meant to enable. If yes, resolve this review with "
            f"'verified'. If the action did NOT in fact unblock, file a "
            f"corrective ticket pointing at whoever owns the next step, with "
            f"this review as parent_ticket so the chain stays traceable."
        )
        return _spawn_child_ticket(
            parent_tid=parent_tid,
            target=original_requester,
            intent=review_intent,
            labels=review_labels,
            title=review_title,
            ask=review_ask,
        )

    # T-4760 (c): registry-check the review child BEFORE the parent transition.
    _preflight_child_ticket(
        parent_tid=args.ticket, parent_fields=fields, target=original_requester,
        intent=review_intent, labels=review_labels,
    )
    return _do_transition(
        tid=args.ticket,
        expected_state=path.parent.name,
        new_state="done",
        verb="close-action",
        detail=detail,
        register_row=register_row,
        no_push=getattr(args, "no_push", False),
        cascade_callback=_close_lifecycle_cascade(make_review),
        successor=getattr(args, "successor", None),
    )


# ---------------------------------------------------------------------------
# Auto-unblock cascade (ORG-013, orig. T-0328): when a parent ticket resolves,
# scan for inbox/+active/ tickets that depend on it; if all their other deps
# are also done, remove parent_tid from the dependant's depends_on frontmatter.
# File stays in place — no git mv. Runs as cascade_callback inside
# _do_transition so the parent resolve + cascade edits land in one atomic commit.
#
# T-3933 arm — strip stale-done refs eagerly; unblock gate unchanged. parent_tid
# (and any other independently-shipped sibling) is now pruned from a dependant's
# depends_on on EVERY cascade fire, even when the dependant stays gated on a
# still-open sibling — closes the middle-state accumulation gap where a stale
# done-dep reference survived until either every sibling shipped or a hand/sweep
# cleanup ran. The all-or-nothing UNBLOCK decision itself (the operator msg_1293) is
# untouched: a dependant only becomes deliverable, and only then loses its
# auto_delivered stamp, when every OTHER dep is SHIPPED too.
#
# Post-ORG-019 semantics: no blocked/ directory. The cascade operates on
# inbox/ and active/ only. The file is not moved; only frontmatter is edited.
# ---------------------------------------------------------------------------


def _find_dependants(parent_tid: str) -> list[Path]:
    """Return ticket paths in inbox/+active/ whose depends_on includes parent_tid."""
    dependants: list[Path] = []
    for state in ("inbox", "active"):
        d = TICKETS_ROOT / state
        if not d.exists():
            continue
        for entry in d.iterdir():
            if not entry.is_file() or not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            try:
                t = parse_ticket(entry)
            except TicketError:
                continue  # malformed; skip silently — grooming surfaces these separately
            deps = t.fields.get("depends_on") or []
            if parent_tid in deps:
                dependants.append(entry)
    return dependants


def _is_shipped(ticket: "Ticket") -> bool:
    """ORG-089 typed completion predicate (T-3440 Scope B).

    Shipped iff the ticket is in `done/` AND its substate is not `dropped`.
    This is the single source of truth ORG-021 (§substates) names: a DROPPED
    bet lives in done/ but does NOT satisfy dependencies or ORG-065 obligations
    (D-N). Every done-as-resolved consumer routes through this (or the id-keyed
    `_dep_is_shipped` below) so `dropped != resolved` holds everywhere.
    """
    return ticket.state_dir == "done" and ticket.fields.get("substate") != "dropped"


def _dep_is_shipped(dep: str) -> bool:
    """True iff `dep` is a well-formed ticket id currently SHIPPED (in done/ and
    not `substate: dropped`).

    Replaces `_dep_is_done` (T-3440 Scope B). Fail-CLOSED: a malformed id, a
    dep not in done/, an unparseable done/ file, or a `substate: dropped` file
    all return False — the cascade must never auto-unblock (or prune a dep) on
    uncertainty (same T-2177 posture as `_all_other_deps_shipped`), and a
    dropped dep is deliberately NOT satisfied (falsifier 2 / D-N).
    """
    if not _TICKET_ID_FULL_RE.fullmatch(dep):  # T-3215
        return False
    path = TICKETS_ROOT / "done" / f"{dep}.md"
    if not path.exists():
        return False
    try:
        fields, _body = parse_front_matter(path.read_text(encoding="utf-8"))
    except (TicketError, OSError):
        # Unparseable/unreadable done ticket — fail closed (do not treat as
        # shipped; grooming surfaces malformed files separately).
        return False
    return fields.get("substate") != "dropped"


def _all_other_deps_shipped(deps: list[str], just_resolved_tid: str) -> bool:
    """True iff every dep besides just_resolved_tid is currently SHIPPED.

    Replaces `_all_other_deps_done` (T-3440 Scope B). Load-bearing for the
    ORG-013 cascade guard: a still-open OR dropped sibling dep must keep the
    dependant gated (falsifier 2). Fail-closed on a malformed dep id (unchanged
    T-2177 posture); a dropped-in-done sibling returns False via `_dep_is_shipped`.
    """
    for dep in deps:
        if dep == just_resolved_tid:
            continue
        if not _TICKET_ID_FULL_RE.fullmatch(dep):  # T-3215
            # Malformed dep id — fail closed (don't auto-unblock on uncertainty)
            return False
        if not _dep_is_shipped(dep):
            return False
    return True


def _cascade_unblock_dependants(
    parent_tid: str,
    stats: dict[str, int] | None = None,
) -> list[Path]:
    """Remove parent_tid from depends_on on inbox/+active/ tickets, pruning any
    other independently-shipped sibling dep at the same time.

    Optional `stats` dict (T-3941): if provided, populated with per-leg fire
    counts under keys `cascade` (partial-strip) and `auto-unblock` (full-unblock).
    The default None keeps every existing caller's behavior byte-identical.
    Only `cmd_cascade` opts in — it builds its commit message from these counts
    so `git log` distinguishes a partial-strip-only fire from a full-unblock fire.

    Two legs, per T-3933 (which REFINES — does not reverse — the operator's msg_1293
    ratification of T-0328's all-or-nothing UNBLOCK call):

    - PARTIAL-STRIP (always fires, unconditionally, for every dependant
      `_find_dependants` returns): parent_tid — plus any sibling dep that is
      independently `_dep_is_shipped` — is pruned from depends_on right away.
      This leg does not unblock the ticket and does not touch auto_delivered
      stamps; it only keeps depends_on faithful to actual open-dep state,
      closing the middle-state gap where a stale done-dep ref survived until
      every sibling shipped (T-3182: artisan hand-cleared 2 of 4 dependants;
      post-cascade hygiene sweep @34332a9f independently found 20 more).
      Logged under actor `cascade` / verb `stale-done dep <parent_tid>
      stripped` — deliberately NOT the word "resolved", so the downstream
      `resolved`-transition consumer (`weekly_ticket_summary.py`'s
      `RESOLVE_LOG_RE`) never misparses a partial prune as an actual ticket
      resolution. That consumer additionally only scans `tickets/done/` — a
      dependant edited here lives in inbox/+active/, so it would not even
      see the line — but the wording is kept
      resolve-safe regardless, since consumer scope is not this function's
      contract to preserve.
    - FULL-UNBLOCK (fires only when `_all_other_deps_shipped` is True — same
      fail-closed posture as before, unchanged, including on a malformed
      sibling id): on top of the same prune, strips auto_delivered /
      auto_delivered_at stamps (T-0971 arm-A) — the dependant is now actually
      deliverable. Logged under actor `auto-unblock` / verb `dep resolved`
      (unchanged wording — this IS the historical unblock event).

    T-3933 decision (i): partial-strip is NOT gated by the full-unblock fail-
    closed posture. A malformed SIBLING dep id means the guard can't tell
    whether the DEPENDANT is unblockable — it says nothing about whether
    parent_tid itself (already confirmed shipped, by construction, before this
    function is ever called for it) should still be listed as a live gate.
    This falls out of the shared `remaining_deps` computation below without
    any special-casing: `d != parent_tid` drops parent_tid unconditionally;
    a malformed sibling fails `_dep_is_shipped`'s own regex check and is kept
    on its own account, independent of whatever else is in the list. The same
    reasoning generalizes past "malformed" to ANY not-currently-shipped
    sibling — including a DROPPED one (T-3440/ORG-089 `_is_shipped`
    substate:dropped != resolved): `test_appetite_circuit_breaker.py`'s
    FALSIFIER 2 confirms A stays correctly un-unblocked (the dropped dep still
    gates) even though the genuinely-shipped sibling now gets pruned as a
    stale ref on the same pass.

    Post-ORG-019 semantics: no file moves. The cascade edits depends_on
    frontmatter in-place and appends a transition log entry. Returns list of
    modified paths for the parent's atomic commit.
    """
    affected: list[Path] = []
    for dep_path in _find_dependants(parent_tid):
        try:
            t = parse_ticket(dep_path)
        except TicketError:
            continue
        fields = t.fields
        old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
        deps = fields.get("depends_on") or []
        full_unblock = _all_other_deps_shipped(deps, parent_tid)

        # Remove parent_tid AND every independently-shipped sibling dep (T-2177).
        # This computation is shared by BOTH legs — see the class docstring above
        # for why a malformed sibling gates full_unblock but not this prune.
        #
        # Historical note (still true): a precondition evaluated over the WHOLE
        # set must not have its action limited to a single member. For
        # depends_on [A, B], A resolving used to skip entirely, then B resolving
        # fired and removed only B — stranding A, a DONE ticket, as a live gate
        # with no remaining cascade opportunity to clear it.
        remaining_deps = [
            d for d in deps
            if d != parent_tid and not _dep_is_shipped(d)
        ]
        fields["depends_on"] = remaining_deps

        if full_unblock:
            # T-0971 arm-A: strip any auto_delivered / auto_delivered_at stamp
            # unconditionally — but ONLY on full unblock. A stamp on a ticket
            # that stays gated after a partial-strip fire is still accurate:
            # the ticket genuinely isn't deliverable yet (T-0768 rearm concern).
            had_ad = fields.pop("auto_delivered", None)
            had_ad_at = fields.pop("auto_delivered_at", None)
            had_stamp = bool(had_ad or had_ad_at)

            detail = f"dep {parent_tid} resolved; removed from depends_on"
            if had_stamp:
                detail += "; auto_delivered stamp cleared"
            if _release_pool_holder(fields, in_inbox=dep_path.parent.name == "inbox"):
                detail += "; delivered_to cleared"
            body = append_transition(t.body, "auto-unblock", "dep resolved", detail)
            if stats is not None:
                stats["auto-unblock"] = stats.get("auto-unblock", 0) + 1
        else:
            stripped_siblings = [
                d for d in deps if d != parent_tid and d not in remaining_deps
            ]
            detail = f"dep {parent_tid} resolved; stale-done ref stripped from depends_on"
            if stripped_siblings:
                detail += (
                    "; also pruned already-shipped sibling(s) "
                    + ", ".join(stripped_siblings)
                )
            if remaining_deps:
                detail += f"; still gated on {', '.join(remaining_deps)}"
            body = append_transition(
                t.body, "cascade", f"stale-done dep {parent_tid} stripped", detail
            )
            if stats is not None:
                stats["cascade"] = stats.get("cascade", 0) + 1

        # T-4760 (c): this write runs AFTER the parent's write (cascade_callback),
        # so it must never be able to produce a registry finding. It cannot: it
        # only REMOVES keys (auto_delivered / auto_delivered_at / delivered_to)
        # and narrows `depends_on` to a subset, and the check validates only
        # added/changed keys against the dependant's own parsed old_fields.
        # Enforce that shape here so a future edit that adds or changes a key
        # fails loudly in tests instead of refusing mid-cascade.
        _assert_cascade_write_shape(old_fields, fields)
        new_text = _build_ticket_text(fields, body, old_fields=old_fields)
        dep_path.write_text(new_text, encoding="utf-8")

        affected.append(dep_path)
    return affected


def _assert_cascade_write_shape(old_fields: dict, new_fields: dict) -> None:
    """T-4760 (c): a cascade-unblock write may only remove keys or shrink
    `depends_on` to a subset — the one shape that can never yield an ORG-021
    registry finding (validate_write checks only added/changed keys; removal
    is always allowed). `depends_on` itself is a registered list field."""
    added = set(new_fields) - set(old_fields) - {"depends_on"}
    changed = sorted(k for k in new_fields
                     if k in old_fields and k != "depends_on"
                     and new_fields[k] != old_fields[k])
    old_deps = list(old_fields.get("depends_on") or [])
    new_deps = list(new_fields.get("depends_on") or [])
    if added or changed or not set(new_deps) <= set(old_deps):
        raise TicketError(
            f"internal: cascade write for {old_fields.get('id')} is not remove-only "
            f"(added={sorted(added)}, changed={changed}, "
            f"depends_on {old_deps} -> {new_deps}) (T-4760)"
        )


def _refuse_cascade_unless_shipped(tid: str) -> None:
    """T-4186 / ORG-089: only a SHIPPED tid (done/, not `substate: dropped`) may be cascaded.

    `_dep_is_shipped` is the single typed predicate; it also fails closed on an unreadable
    done/ file. The `drop` verb itself never cascades (cmd_drop passes no cascade_callback),
    so this guards the one remaining route: `ticket.py cascade --tid <dropped-tid>`."""
    if not _dep_is_shipped(tid):
        raise TicketRefusal(
            f"cascade refused: {tid} is in `done` but not shipped (`substate: dropped`, or its "
            f"done/ file is unreadable) — ORG-089: a dropped bet does not satisfy dependencies, "
            f"so its dependants must keep their gate"
        )


def cmd_cascade(args: argparse.Namespace) -> int:
    """ORG-013 cascade-unblock pass for an already-resolved tid (T-3289).

    Closes the T-3288 PR-squash-merge gap: when a ticket lands in done/ via
    `gh pr merge --squash` (or an admin-merge) rather than `ticket.py
    resolve`, `_do_transition`'s cascade_callback hook never runs, so
    dependants keep stale `depends_on:` refs indefinitely. This subcommand is
    the standalone trigger. T-4186: it is now driven by `tools/cascade_sweep.py`
    (level-triggered, seat-side timer) for every stale tid it finds; the
    diff-walk Action that used to invoke it per PR-merge could never push
    (read-only GITHUB_TOKEN, protect-main bypass is deploy-key/user only) and
    was retired.

    Reuses `_cascade_unblock_dependants` VERBATIM (T-3288's diagnosis: the
    cascade LOGIC is correct — the guard semantics tested extensively in
    TestComputeUnblocks / the resolve-time path are unchanged — only the
    PR-merge TRIGGER was missing). Also reuses the same lock/pull/re-verify
    shape and the same `_commit_and_push` primitive that `_do_transition`
    itself calls for the parent-transition case (single atomic commit,
    HERMES guard, README rebuild, push-with-retry).

    Deliberately does NOT call `_do_transition`: that function's contract is
    a `git mv` state transition (`expected_state` -> `new_state`) of the
    PASSED tid. Here `--tid` is already `done` and never moves — only its
    DEPENDANTS' depends_on frontmatter changes. Forcing this through
    `_do_transition` would require a fake no-op transition on a ticket that
    isn't transitioning, which doesn't fit the function's shape.

    Fail-closed (raises TicketError -> non-zero exit via main()'s catch):
    tid is malformed, not found in any state, found but not currently in
    `done/`, or in `done/` but NOT SHIPPED (`substate: dropped` — ORG-089:
    dropped != resolved; T-4186. `_cascade_unblock_dependants` removes the
    parent from every dependant unconditionally, so without this refusal a
    dropped bet would be silently stripped as if it had shipped). Idempotent: a tid with no live dependants makes no filesystem
    change and creates no commit ("no cascade updates" — same idempotent-
    no-op-skips-commit shape as `block --clear-dep` / `--clear-not-before`).
    Per T-3933, "no live dependants" now means every dependant already had
    `tid` removed from its depends_on (via a prior partial-strip OR full-
    unblock fire) — `_cascade_unblock_dependants`'s partial-strip leg means a
    dependant with a still-unshipped SIBLING dep is no longer left untouched:
    it gets `tid` stripped on THIS call (and produces a commit), it just
    doesn't get fully unblocked. See `_cascade_unblock_dependants` for the
    full/partial split.
    """
    tid = args.tid
    no_push = getattr(args, "no_push", False)

    # Fail-fast BEFORE the lock/pull — same shape as _do_transition's initial
    # expected-state check. A request for a tid that isn't (yet) done/ is a
    # caller error a pull can't fix.
    path = _find_ticket(tid)  # raises TicketError: malformed id or not found anywhere
    if path.parent.name != "done":
        raise TicketRefusal(
            f"cascade refused: {tid} is in `{path.parent.name}`, not `done` — "
            f"cascade only fires for an already-resolved (done) tid"
        )
    _refuse_cascade_unless_shipped(tid)

    # T-1770 Part b: the mutate -> stage -> commit -> push span is one atomic
    # critical section against other writers on the shared checkout. The lock
    # starts before the pull (which can itself bring in a concurrent writer's
    # transition of this very tid) and extends through _commit_and_push.
    with git_mutation_lock(label=f"cascade {tid}"):
        _pull_if_needed(no_push)

        # T-2140-style re-verify inside the lock, after the pull: prove the
        # state again rather than trust the pre-lock check, which proves
        # nothing by the time the lock is actually held.
        current = _find_ticket(tid)
        if current.parent.name != "done":
            raise TicketRefusal(
                f"concurrent-transition conflict: {tid} was in `done` when "
                f"checked but is now in `{current.parent.name}` — another "
                f"writer moved it while this cascade was waiting for the "
                f"lock. Nothing was changed. Re-read and retry."
            )
        _refuse_cascade_unless_shipped(tid)

        stats: dict[str, int] = {}
        affected = _cascade_unblock_dependants(tid, stats=stats)
        if not affected:
            print(f"cascade {tid}: no cascade updates")
            return 0

        cascade_count = len(affected)
        # T-3941: distinguish the two ORG-013 legs T-3933 landed. Both fire
        # under this callback, but they mean different things in git-log:
        # - `cascade` (partial-strip): stale-done ref pruned; dependant stays gated.
        # - `auto-unblock` (full-unblock): every dep shipped, dependant is now deliverable.
        # Prior wording ("Auto-unblock cascade per ORG-013") claimed unblock even
        # when only partial-strip fired — cosmetically misleading in git-log.
        n_prune = stats.get("cascade", 0)
        n_unblock = stats.get("auto-unblock", 0)
        if n_unblock and n_prune:
            leg_summary = (
                f"{n_prune} partial-strip (stale-done ref pruned; still gated) + "
                f"{n_unblock} auto-unblock (fully deliverable now)"
            )
        elif n_unblock:
            leg_summary = f"{n_unblock} auto-unblock (fully deliverable now)"
        else:
            leg_summary = f"{n_prune} partial-strip (stale-done ref pruned; still gated)"
        # Distinct subject from resolve's own "tickets: resolve <tid> — ..."
        # (which carries its cascade line in the BODY) so `git log` can tell
        # a PR-merge-triggered cascade apart from a resolve-time one.
        commit_message = (
            f"cascade: {tid} → {cascade_count} dependant"
            f"{'s' if cascade_count != 1 else ''} updated\n\n"
            f"ORG-013 cascade (T-3933 two-leg): {leg_summary}. "
            f"Invoked via `ticket.py cascade` (ORG-013 cascade)."
        )
        _commit_and_push(affected, commit_message, no_push=no_push)

    print(
        f"cascade {tid}: {cascade_count} dependant"
        f"{'s' if cascade_count != 1 else ''} had depends_on updated in place"
    )
    return 0


# ---------------------------------------------------------------------------
# Ratify-lift helpers (T-0491)
# ---------------------------------------------------------------------------

# Repo name that ticket.py operates in (pantheon-config). Same-repo entries
# in ratifiable_files use this name; cross-repo entries are skipped (v1).
_PANTHEON_CONFIG_REPO_NAME = "pantheon-config"

# Tolerant regex for "awaiting … ratification" clauses to strip after the
# Ratified prefix is prepended.  Matches common phrasings — no holder, a bare
# holder ("the operator"), or a possessive holder ("the operator's", "the operator's") —
# with or without trailing period, with or without bold markers. The holder
# clause is bounded to exactly one optional word so it can never swallow
# unrelated multi-word prose (T-2847).
# Non-fatal if nothing matches — the prepended Ratified is authoritative.
_AWAITING_RATIFICATION_RE = re.compile(
    r"\*{0,2}"                        # optional bold-open **
    r"Awaiting"
    r"(?:\s+(?:the\s+)?\w+(?:'s)?)?"  # optional single-word holder clause:
                                       # "the operator", "the operator's", "the operator's"
    r"\s+ratification"
    r"\.?"                # optional trailing period
    r"\*{0,2}"             # optional bold-close **
    r"\s*",                # trailing whitespace
    re.IGNORECASE,
)

# Leading "Proposed" status marker (with its typical "(agent-authored,
# unratified)"-style qualifier) to strip before prepending the Ratified
# prefix. Anchored to the START of the rest-of-line and \b-bounded so it can
# only ever remove the actual leading marker — never an embedded/mid-sentence
# "Proposed", and never a false-positive prefix match (e.g. "Proposed" is
# NOT a prefix collision risk the way "DRAFT"/"Drafted" is, but the \b is
# kept for symmetry + defense) (T-2847).
_LEADING_PROPOSED_RE = re.compile(
    r"^\s*\*{0,2}\bProposed\b\*{0,2}"
    r"(?:\s*\([^)]*\))?"       # optional "(agent-authored, unratified)"-style qualifier
    r"\s*[—;:,\-]*\s*",        # optional connecting punctuation before the next clause
    re.IGNORECASE,
)

# Leading "DRAFT" status marker — same shape/rationale as _LEADING_PROPOSED_RE.
# \b is load-bearing here: without it, "DRAFT" matches the first 5 letters of
# "Drafted", corrupting genuine "Drafted <date>" lineage text (T-2847 design
# trace caught this before it shipped).
_LEADING_DRAFT_RE = re.compile(
    r"^\s*\*{0,2}\bDRAFT\b\*{0,2}"
    r"(?:\s*\([^)]*\))?"
    r"\s*[—;:,\-]*\s*",
    re.IGNORECASE,
)

# T-5373: "Draft <date>" at the start of the line is lineage, not a marker:
# becomes "Drafted <date>" so no orphan date is left behind. Must run BEFORE
# _LEADING_DRAFT_RE (which would strip only the word).
# T-5405: bold markers are captured and re-emitted in place so bold stays
# balanced ("**Draft 2026-10-22**" -> "**Drafted 2026-10-22**").
_LEADING_DRAFT_DATED_RE = re.compile(
    r"^(\s*)(\*{0,2})\bDraft\b(\*{0,2})\s+(\d{4}-\d{2}-\d{2})(\*{0,2})",
    re.IGNORECASE,
)

# T-5373: one sentence = text up to a terminal . ! ? (optionally followed by
# bold-close **) that is followed by whitespace/end, so "v1.2" does not split.
_SENTENCE_RE = re.compile(r".+?(?:[.!?]+\*{0,2}(?=\s|$)|$)\s*", re.DOTALL)

# T-5373: sentences carrying pre-ratification language, removed WHOLE.
# "ratification" alone is NOT a trigger ("gated on ratification" is lineage).
_PRERATIFY_SENTENCE_RE = re.compile(
    r"\bpending\b[^.!?]*\bratif"
    r"|\buntil\b[^.!?]{0,40}\bratified\b"
    r"|\bunratified\b"
    r"|\bnot\s+yet\s+ratified\b",
    re.IGNORECASE,
)

# T-5373: residual guard. Anything matching after cleaning means the line
# would still claim to be pending -> skip the write, ask for a hand flip.
# Bare "pending" is deliberately included (false positive costs one hand flip).
_RESIDUAL_PRERATIFY_RE = re.compile(
    r"\b(?:pending|unratified|not\s+yet\s+ratified)\b"
    r"|\buntil\b[^.!?]{0,40}\bratified\b"
    r"|\bawaiting\b[^.!?]{0,30}\bratif"
    r"|^\W*(?:draft|proposed)\b",
    re.IGNORECASE,
)


def _markup_balance(text: str) -> tuple[int, int]:
    """(paren depth delta, bold parity) - equal before/after = balanced-ness kept."""
    return (text.count("(") - text.count(")"), text.count("**") % 2)


def _awaiting_sub(m: "re.Match[str]") -> str:
    """Strip an awaiting clause unless that would unbalance bold (T-5405).

    "**Drafted, NOT yet awaiting ratification** - ..." has the bold-close but
    not the bold-open inside the match; stripping it mangles the line. Leave it
    so the residual guard trips and a human flips the doc.
    """
    if m.group(0).count("**") % 2:
        return m.group(0)
    return ""


# T-5405: a leading Draft/Proposed qualifier parenthetical that carries
# lineage (a date, or Authored/Drafted/Author) is kept; pure qualifiers such
# as "(agent-authored, unratified)" are still removed by the leading regexes.
_LEADING_MARKER_PAREN_RE = re.compile(
    r"^(\s*)\*{0,2}\b(Proposed|DRAFT)\b\*{0,2}\s*(?=\()", re.IGNORECASE
)
_LINEAGE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\b(?:Authored|Drafted|Author)\b")


def _keep_lineage_qualifier(text: str) -> str:
    m = _LEADING_MARKER_PAREN_RE.match(text)
    if m is None:
        return text
    depth, end = 0, None
    for i in range(m.end(), len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None or not _LINEAGE_RE.search(text[m.end():end]):
        return text
    head = "Drafted " if m.group(2).upper() == "DRAFT" else ""
    return m.group(1) + head + text[m.end():]


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_FLAT_PAREN_RE = re.compile(r"\([^()]*\)")
_PAREN_SEGMENT_SPLIT_RE = re.compile(r"(;\s*|(?<=[.!?])\s+)")


def _strip_pending_in_parens(text: str) -> str:
    """Drop only the pre-ratification segments inside flat parentheticals
    (T-5405), keeping the lineage head ("Drafted <date> (T-..; Author: ..)").
    Segments split on ';' and sentence boundaries; an emptied paren is removed.
    """

    def repl(m: "re.Match[str]") -> str:
        inner = m.group(0)[1:-1]
        if not _PRERATIFY_SENTENCE_RE.search(inner):
            return m.group(0)
        parts = _PAREN_SEGMENT_SPLIT_RE.split(inner)
        segs = list(zip(parts[0::2], parts[1::2] + [""]))
        kept = [(t, sep) for t, sep in segs if not _PRERATIFY_SENTENCE_RE.search(t)]
        if not kept:
            return ""
        out = "".join(t + sep for t, sep in kept[:-1]) + kept[-1][0]
        return "(" + out.rstrip(" .;") + ")"

    out = _FLAT_PAREN_RE.sub(repl, text)
    if out == text:
        return out
    out = re.sub(r"\s+([.,;])", r"\1", out)
    return re.sub(r"(?<=\S)  +", " ", out)


def _strip_preratification_sentences(text: str) -> str:
    """Drop whole sentences matching _PRERATIFY_SENTENCE_RE (T-5373)."""
    kept = [
        sent for sent in _SENTENCE_RE.findall(text)
        if not _PRERATIFY_SENTENCE_RE.search(sent)
    ]
    return "".join(kept)


# Flips a "**HITL: Proposed.**"-style tag to "**HITL: Ratified.**", matching
# the existing bold-wrap-whole-tag convention (rather than stripping it —
# HITL is a distinct field, not free prose to remove) (T-2847).
_HITL_PROPOSED_RE = re.compile(
    r"(\*{0,2}HITL:\s*)Proposed(\.?\*{0,2})",
    re.IGNORECASE,
)

# Regex that matches the FIRST **Status:** line (captures the rest-of-line).
_STATUS_LINE_RE = re.compile(r"^(\*\*Status:\*\*)[ \t]*(.*)", re.MULTILINE)

# Frontmatter block (between the leading "---" fence pair) and its `status:`
# key, used only to WARN (never rewrite — T-2847 part-3 option (b)) when the
# frontmatter status: still reads a pre-ratification marker after a body lift.
_FRONTMATTER_BLOCK_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
_FRONTMATTER_STATUS_LINE_RE = re.compile(r"^status:[ \t]*(.*)$", re.MULTILINE)


def _stale_frontmatter_status_value(content: str) -> str | None:
    """Return the frontmatter `status:` value if present and NOT already
    Ratified-prefixed, else None (no frontmatter, no status: key, empty
    value, or value already starting with "Ratified").
    """
    fm_match = _FRONTMATTER_BLOCK_RE.match(content)
    if fm_match is None:
        return None
    status_match = _FRONTMATTER_STATUS_LINE_RE.search(fm_match.group(1))
    if status_match is None:
        return None
    value = status_match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    if not value:
        return None
    if value.lower().startswith("ratified"):
        return None
    return value


def _lift_ratifiable_doc_status(
    doc_path: Path,
    repo_root: Path,
    warnings: list[str],
) -> str | None:
    """Rewrite the first **Status:** line of *doc_path* to the Ratified form.

    Returns the new file content as a string if a change was made, or None if
    the doc is already Ratified (idempotent) or has no Status line.

    Appends human-readable warning strings to *warnings* for:
      - no **Status:** line found
      - frontmatter `status:` still reads a pre-ratification marker after a
        successful body lift (T-2847 part-3 option (b) — body-only lift;
        frontmatter is warned-on-stale, never auto-rewritten)
    Does NOT raise on any of these — callers must not block on lift failure.

    T-5373: whole sentences carrying "Pending … ratification" / "Until …
    ratified" / "unratified" are removed, a leading "Draft <date>" becomes
    "Drafted <date>", and if pre-ratification vocabulary still remains the
    write is SKIPPED with a "HAND FLIP REQUIRED" warning (returns None).

    The Ratified prefix format is: **Ratified <YYYY-MM-DD> by the operator.**
    The pre-ratification marker family — a leading "Proposed" (with its
    typical "(agent-authored, unratified)" qualifier), a leading "DRAFT",
    and any "Awaiting … ratification" clause (tolerant regex, incl.
    possessive holders like "the operator's") — is stripped so the resulting
    line is internally consistent; a "**HITL: Proposed.**" tag is flipped to
    "**HITL: Ratified.**" rather than stripped (T-2847). Genuine lineage
    (Drafted/Authored/Amended <date> … history) is preserved — the strip
    regexes are anchored to the START of the rest-of-line and \\b-bounded so
    they can only remove the actual leading marker, never embedded prose.
    """
    try:
        content = doc_path.read_text(encoding="utf-8")
    except OSError as e:
        warnings.append(f"could not read {doc_path}: {e}")
        return None

    m = _STATUS_LINE_RE.search(content)
    if m is None:
        warnings.append(
            f"no **Status:** line found in {doc_path.relative_to(repo_root)!s}; "
            "Status lift skipped — resolve proceeds"
        )
        return None

    rest = m.group(2)

    # Idempotent: if the rest already starts with "**Ratified" or "Ratified", no-op.
    stripped_rest = rest.lstrip()
    if stripped_rest.lower().startswith("**ratified") or stripped_rest.lower().startswith("ratified"):
        return None

    # Strip the pre-ratification marker family (best-effort; non-fatal if
    # nothing matches). Order matters: leading "Proposed" first, then
    # leading "DRAFT" (handles "Proposed (...) — DRAFT ..." as well as a
    # bare "DRAFT ..." with no "Proposed"), then the awaiting-clause, then
    # flip any HITL:-tag in place.
    rest_cleaned = rest
    rest_cleaned = _keep_lineage_qualifier(rest_cleaned)
    rest_cleaned = _LEADING_PROPOSED_RE.sub("", rest_cleaned)
    rest_cleaned = _LEADING_DRAFT_DATED_RE.sub(r"\1\2Drafted\3 \4\5", rest_cleaned)
    rest_cleaned = _LEADING_DRAFT_RE.sub("", rest_cleaned)
    rest_cleaned = _AWAITING_RATIFICATION_RE.sub(_awaiting_sub, rest_cleaned)
    rest_cleaned = _HITL_PROPOSED_RE.sub(r"\1Ratified\2", rest_cleaned)
    rest_cleaned = _strip_pending_in_parens(rest_cleaned)
    rest_cleaned = _strip_preratification_sentences(rest_cleaned)
    rest_cleaned = rest_cleaned.strip()

    # T-5373 residual guard: never write a line that still says it's pending.
    residual = _RESIDUAL_PRERATIFY_RE.search(rest_cleaned)
    problem = residual.group(0) if residual is not None else None
    if problem is None and _markup_balance(rest_cleaned) != _markup_balance(rest):
        # T-5405: cleaning must never leave an unclosed paren / odd bold.
        problem = "markup imbalance"
    if problem is None:
        # T-5405: a dated lineage head must survive cleaning; if any date in
        # the input is gone the sentence strip ate it -> hand flip.
        lost = [d for d in _DATE_RE.findall(rest) if d not in rest_cleaned]
        if lost:
            problem = f"dated lineage lost: {lost[0]}"
    if problem is not None:
        warnings.append(
            f"{doc_path.relative_to(repo_root)!s}: Status line cannot be "
            f"cleanly lifted ({problem!r}); "
            "Status lift SKIPPED — HAND FLIP REQUIRED for this doc; "
            "resolve proceeds"
        )
        return None

    # Build the Ratified prefix.  Date is UTC per brief.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ratified_prefix = f"**Ratified {today} by the operator.**"

    # Compose the new Status line content.
    if rest_cleaned:
        new_rest = f"{ratified_prefix} {rest_cleaned}"
    else:
        new_rest = ratified_prefix

    new_status_line = f"**Status:** {new_rest}"
    new_content = content[: m.start()] + new_status_line + content[m.end():]

    doc_path.write_text(new_content, encoding="utf-8")

    # T-2847 part-3 option (b): body is authoritative and now lifted; the
    # frontmatter status: field is NOT rewritten (free-form prose corpus,
    # no schema — rewriting risks YAML/prose corruption for no structural
    # gain). Warn actionably if it still reads pre-ratification.
    stale_value = _stale_frontmatter_status_value(new_content)
    if stale_value is not None:
        warnings.append(
            f"{doc_path.relative_to(repo_root)!s}: body Status lifted to "
            "Ratified, but frontmatter status: still reads a "
            f"pre-ratification marker ({stale_value!r}); reconcile "
            "frontmatter manually."
        )

    return new_content


def _apply_ratify_lift(
    ratifiable_files: list[str],
    repo_root: Path,
    warnings: list[str],
) -> list[Path]:
    """Process each ratifiable_files entry; return list of modified same-repo paths.

    Same-repo entries (repo == pantheon-config): attempt the Status lift and
    return the path if it was modified (so it gets staged in the resolve commit).
    Cross-repo entries: skip + warn (v1 scope; D3 same-repo-only rationale).
    Missing docs: warn + skip.
    """
    lifted: list[Path] = []
    for entry in ratifiable_files:
        # Entry format validated at file-time: "<repo>:<rel-path>".
        if ":" not in entry:
            warnings.append(f"malformed ratifiable_files entry {entry!r}; skipping")
            continue

        repo_name, rel_path = entry.split(":", 1)

        if repo_name != _PANTHEON_CONFIG_REPO_NAME:
            # Cross-repo: v1 skip + warn.  The cross-repo lift is follow-on work.
            warnings.append(
                f"cross-repo entry {entry!r} (repo={repo_name!r}) — "
                "Status lift skipped for cross-repo docs in v1; "
                "resolve proceeds. HO can lift manually."
            )
            continue

        doc_path = repo_root / rel_path
        if not doc_path.exists():
            warnings.append(
                f"ratifiable doc {rel_path!r} not found in repo root; "
                "Status lift skipped — resolve proceeds"
            )
            continue

        result = _lift_ratifiable_doc_status(doc_path, repo_root, warnings)
        if result is not None:
            # File was modified — stage it.
            lifted.append(doc_path)

    return lifted


def _valid_target_values() -> set[str]:
    """Persona names + role IDs + persona aliases (e.g. consultant) + operator.

    Delegates to agent_role_map.valid_target_values() so the accepted-target set
    has ONE source of truth. Before T-3478 this unioned the map keys/values
    inline and so did not include persona aliases — a `requester: consultant` ticket
    then failed retarget validation with `unknown target 'consultant'`."""
    from tools.agent_role_map import valid_target_values
    return valid_target_values()


def _resolve_persona(value: str) -> str:
    """Normalize a ticket target/requester value to its canonical persona name.

    Wraps agent_role_map.resolve_persona(). Converts role IDs to persona names
    on write so ticket front-matter is always persona-canonical (T-0492/msg_1661).

    Examples: "orchestrator" -> "orchestrator", "head_research_orchestrator" -> "specialist"
    (legacy "specialist" -> "specialist" too), "orchestrator" -> "orchestrator" (passthrough),
    "operator" -> "operator" (passthrough).

    Only use for ticket `target:` and `requester:` fields. Ledger `agent:`
    fields remain role-ID-canonical — use resolve_role() there.
    """
    from tools.agent_role_map import resolve_persona
    return resolve_persona(value)


def _resolve_requester(requester: str) -> str:
    """Validate + normalize a ticket `requester:` for the `file` WRITE surface (T-4499).

    Accepts exactly what `_valid_target_values()` accepts (persona names, audit
    role-ids, persona aliases, `operator`) — the ONE vocabulary, not a second
    list — and normalizes it to the canonical persona (T-0492). A bare
    non-injective role-class id (engineering_orchestrator) is accepted and kept
    unchanged: requester is provenance, so only a TARGET must name a single seat
    (T-4044).

    Additionally accepts a fleet OS-ACCOUNT name (`claude-research`, `claude`,
    ...) and normalizes it to the persona that account is bound to in
    instance.yaml (`_OS_USER_PERSONA`, the same account->persona axis the WIP cap
    uses). A seat's own session files as its OS account (specialist did, as
    `claude-research`, on 2026-09-15); refusing that would fail a live filer
    silently (P7), whereas the account is a real, attributable filer identity.
    Accounts are NOT accepted as a `--target` (that is a delivery vector).

    Matching is case-insensitive and whitespace-tolerant (T-4499 follow-up):
    the value is folded with `.strip().lower()` before BOTH lookups, because the
    kino filing form is free text on a phone keyboard that auto-capitalizes
    ("the operator"). The fold is sound because every vocabulary name and OS-account
    key is lowercase (guarded by test_lowercasing_cannot_shadow_or_merge_...);
    the STORED value is still the canonical lowercase persona, and the refusal
    message echoes the caller's ORIGINAL text. `--target` gets the same fold in
    `_resolve_or_reject_target`, where OS accounts and role-classes are still refused.

    Anything else (`-x`, `test`, `role:<R>`, empty) is a TicketRefusal.
    """
    valid = _valid_target_values()
    key = requester.strip().lower()
    if key in valid:
        return _resolve_persona(key)
    if key in _OS_USER_PERSONA:
        return _resolve_persona(_OS_USER_PERSONA[key])
    raise TicketRefusal(
        f"unknown requester {requester!r}; expected one of {sorted(valid)} "
        f"(or a fleet OS account name, normalized to its persona)"
    )


def _resolve_or_reject_target(target: str) -> str:
    """Validate + normalize a ticket `target:` for a WRITE surface (file/retarget).

    ORG-088 Plane-A (T-3761). Two target kinds:

      - A `role:<R>` pool target: accepted iff R is a KNOWN functional role_id
        (agent_role_map.known_role_ids()). The value passes through UNCHANGED — a
        role target is canonical as-is, NOT a persona alias to normalize. Fail-CLOSED
        (the WRITE fail direction, deliberately asymmetric to the READ surfaces): an
        unknown role R, OR an unverifiable/malformed membership vocabulary, is a TYPED
        refusal (TicketError). A write must never pool a ticket to an unresolvable
        role — an unknown-R or unverified-vocab ticket could never resolve to eligible
        members.

      - Any other value: validated against _valid_target_values() and normalized to
        its canonical persona (role-id/alias -> persona), exactly as the pre-T-3761
        inline check did.

    Returns the target to persist. Contrast the READ surfaces (list/ready/prime),
    which DEGRADE-GRACEFUL on the same unverifiable-vocab condition (hide role
    tickets, keep the listing alive) — see _plane_a_eligible_roles.

    Case + surrounding whitespace are forgiven (T-4499 follow-up): the value is
    folded with `.strip().lower()` once, and every lookup / stored value uses that
    key — kino's File form and retarget box are free text on an auto-capitalizing
    phone keyboard ("Orchestrator", "Recovery"). The fold is sound because every persona,
    alias, role-id and role-pool id is lowercase (guarded by tests). NOTHING ELSE
    is loosened: an OS account is still not a target, a non-injective role-class
    still refuses (a target must resolve to exactly the seat/pool its lowercase
    form names), and a refusal echoes the caller's ORIGINAL text.
    """
    raw = target
    target = raw.strip().lower()
    if _ROLE_TARGET_RE.fullmatch(target):
        role = target.split(":", 1)[1]
        from tools.agent_role_map import known_role_ids, PersonaRoleMembershipError

        try:
            vocab = known_role_ids()
        except PersonaRoleMembershipError as exc:
            raise TicketError(
                f"cannot validate role target {raw!r}: the functional-role "
                f"membership vocabulary is unverifiable ({exc}); refusing to pool a "
                f"ticket to an unresolvable role (fail-closed)"
            ) from exc
        if role not in vocab:
            raise TicketRefusal(
                f"unknown functional role {raw.strip().split(':', 1)[1]!r} in "
                f"target {raw!r}; expected one of {sorted(vocab)}"
            )
        return target

    if target == "operator":
        return target

    # T-5239 / ORG-088 (the operator TG 12076/12077): tickets route by ROLE ONLY. A
    # persona, persona alias, or bare audit role-id is no longer a writable target:
    # name the pool role(s) that replace it. (`requester:` keeps personas — provenance.)
    valid = _valid_target_values()
    if target not in valid:
        raise TicketRefusal(
            f"unknown target {raw!r}; expected 'operator' or a role pool "
            f"'role:<R>' (one of {_known_role_pool_hint()})"
        )
    from tools.agent_role_map import (
        PersonaRoleMembershipError, known_role_ids, pool_roles_for,
    )
    try:
        roles = pool_roles_for(target)
        if target in known_role_ids():
            roles = [target] + [r for r in roles if r != target]
    except PersonaRoleMembershipError as exc:
        raise TicketError(
            f"cannot name the replacement role for {raw!r}: the functional-role "
            f"membership vocabulary is unverifiable ({exc})"
        ) from exc
    hint = " or ".join(f"'role:{r}'" for r in roles) or "a 'role:<R>' pool"
    if target in known_role_ids():
        what = "is a bare role id; a role target needs the 'role:' prefix"
    else:
        what = "is a seat, not a role"
    raise TicketRefusal(
        f"persona targeting is removed (ORG-088, T-5239): target {raw!r} {what}. "
        f"Route by role instead: use {hint}. "
        f"(target 'operator' is still accepted.)"
    )


def _known_role_pool_hint() -> str:
    from tools.agent_role_map import known_role_ids
    try:
        return ", ".join(f"role:{r}" for r in sorted(known_role_ids()))
    except Exception:  # noqa: BLE001 - hint only; the refusal itself still stands
        return "role:<R>"


def _derive_pool_target(value: str) -> str:
    """Translate a DERIVED persona/role-id/requester value into a ticket target.

    T-5239: for auto-spawn paths that build a target from a requester or an
    operator-supplied hand-off name rather than a caller-supplied `--target`.
    `operator` and `role:<R>` pass through; a persona/alias/role-id becomes its pool
    role via agent_role_map.persona_pool_role. A value with no declared role is a
    TicketError (never silently left as a persona)."""
    from tools.agent_role_map import PersonaRoleMembershipError, persona_pool_role
    v = value.strip().lower()
    if v == "operator" or _ROLE_TARGET_RE.fullmatch(v):
        return v
    try:
        role = persona_pool_role(v)
    except PersonaRoleMembershipError as exc:
        raise TicketError(
            f"cannot derive a role pool target from {value!r}: the functional-role "
            f"membership vocabulary is unverifiable ({exc})"
        ) from exc
    if not role:
        raise TicketRefusal(
            f"cannot derive a role pool target from {value!r}: no functional role "
            f"declared (tickets route by role only, ORG-088/T-5239)"
        )
    return f"role:{role}"


def cmd_retarget(args: argparse.Namespace) -> int:
    """Swap a ticket's `target` field + append transition log + commit.

    Default: cross-actor retargets ALSO reset state to inbox so the new
    target sees the ticket as fresh work (per the operator directive
    2026-05-14 msg_1494: "if tickets get re-routed to another member,
    we should reset status to inbox too"). The inbox/active
    state was meaningful to the prior target; the new target shouldn't
    inherit it.

    Pass --keep-state to preserve the existing state (rare: mid-work
    handoff where the new target picks up exactly where the prior left off).

    Refuses on done (closed; re-open is a separate concern).
    """
    new_target = args.to
    note = getattr(args, "note", None)
    keep_state = getattr(args, "keep_state", False)

    # T-3761 / ORG-088 Plane-A: validate + normalize in one step. A `role:<R>`
    # target re-pools the ticket to a functional role and is accepted iff R is a
    # known role_id (fail-closed on unknown role / unverifiable vocab); a plain
    # persona/role-id is validated against _valid_target_values() and normalized
    # to its canonical persona.
    #
    # T-0492: the role-ID → persona normalization happens BEFORE the no-op check
    # below so that `retarget --to orchestrator` on a `target: orchestrator` ticket
    # correctly no-ops rather than re-writing the same effective target with a
    # role-ID form. A role: target passes through unchanged, so a
    # role:R → role:R retarget also no-ops correctly.
    new_target = _resolve_or_reject_target(new_target)

    path = _find_ticket(args.ticket)
    if path.parent.name == "done":
        raise TicketRefusal(
            f"state-mismatch: {args.ticket} is in `done`, "
            f"retarget refuses on done (closed)"
        )

    text = path.read_text(encoding="utf-8")
    fields, body = parse_front_matter(text)
    old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
    old_target = fields.get("target", "?")
    old_state = path.parent.name

    if old_target == new_target:
        print(
            f"retarget {args.ticket} → {new_target}: no-op "
            f"(target already {new_target!r})",
            file=sys.stderr,
        )
        return 0

    # T-5014: a ticket becoming target=operator needs the same forced choice as
    # cmd_file — new --ratifiable-file / --no-file, or an attachment already in the
    # frontmatter. Pre-lock validation: pure, no writes.
    _rt_new_files = _parse_ratifiable_files(
        [e.strip() for e in (getattr(args, "ratifiable_file", None) or [])]
    )
    _refuse_excluded_ratifiable_class(_rt_new_files)  # T-6071 / ORG-015
    _rt_no_file = _operator_attachment_gate(
        new_target, _rt_new_files, getattr(args, "no_file", None),
        fix_prefix=f"ticket.py retarget {args.ticket} --to operator",
        existing_files=bool(fields.get("ratifiable_files")),
        existing_reason=bool(str(fields.get("no_file_reason") or "").strip()),
    )
    if new_target == "operator" and _rt_new_files:
        _check_ratifiable_resolve(_rt_new_files)

    # T-1770 Part b: pull → (optional git-mv) → edit → commit is one atomic
    # critical section against other writers on the shared checkout — the
    # lock must start before the pull (and any git-mv) and extend through
    # `_commit_and_push`.
    with git_mutation_lock(label=f"retarget {args.ticket}"):
        # Pull-first (T-0244): must succeed BEFORE overwriting the ticket file
        # so a pull failure doesn't leave an edited-but-uncommitted file on
        # disk. Skipped when no_push=True.
        _pull_if_needed(args.no_push)

        # Determine target state. Default: cross-actor retarget resets to inbox
        # unless --keep-state is passed or the ticket is already in inbox.
        new_state = old_state if (keep_state or old_state == "inbox") else "inbox"

        # T-4119 REBOUND-1 (reviewer finding #2): `substate: awaiting-merge` is
        # only meaningful while the ticket sits in active/ — the default
        # cross-actor reset path above is a MORE likely trigger for this leak
        # than a bare unclaim (retargeting a stalled arc to a different
        # orchestrator is ordinary use of this verb). Strip both fields
        # whenever the ticket is actually LEAVING active (new_state !=
        # "active"); --keep-state on an active ticket keeps the marker intact
        # — the target changes, the work-in-flight status doesn't.
        dropped_awaiting_merge = (
            new_state != "active" and fields.get("substate") == "awaiting-merge"
        )
        if dropped_awaiting_merge:
            fields.pop("substate", None)
            fields.pop("awaiting_merge_ref", None)

        fields["target"] = new_target

        # T-5014: persist the attachment choice made above. Files merge (dedup)
        # into any existing ratifiable_files; ORG-022 label pairing is preserved
        # by auto-adding the canonical 'ratification' label, as cmd_file does.
        if _rt_new_files:
            merged = list(fields.get("ratifiable_files") or [])
            merged += [e for e in _rt_new_files if e not in merged]
            fields["ratifiable_files"] = merged
            _rt_labels = list(fields.get("labels") or [])
            if "ratification" not in _rt_labels:
                fields["labels"] = _rt_labels + ["ratification"]
        if _rt_no_file:
            fields["no_file_reason"] = _rt_no_file

        # T-0465: intent disambiguation tracks target=operator status.
        raw_intent = getattr(args, "intent", None)
        dropped_intent: str | None = None
        if isinstance(raw_intent, str) and raw_intent.strip():
            intent = raw_intent.strip()
            if intent not in INTENT_VOCAB:
                raise TicketRefusal(
                    f"--intent {intent!r} not in vocab {INTENT_VOCAB}; "
                    f"choose one of: {', '.join(INTENT_VOCAB)}"
                )
            if new_target != "operator":
                raise TicketRefusal(
                    f"--intent is target=operator-specific; refusing for new target={new_target!r}"
                )
            # T-1321: explicit intent=ratify requires ratifiable_files to already
            # be present in the ticket's frontmatter.  cmd_retarget has no
            # --ratifiable-file argument, so the effective check is the existing
            # fields dict (already populated from parse_front_matter above).
            if intent == "ratify" and not fields.get("ratifiable_files"):
                raise TicketRefusal(
                    "intent=ratify requires ratifiable_files to already be set in "
                    "the ticket's frontmatter; add them before retargeting to "
                    "intent=ratify (per ORG-015)"
                )
            fields["intent"] = intent
        elif new_target == "operator" and "intent" not in fields:
            # Newly retargeted to operator without explicit --intent: auto-default
            # (ratify if ratifiable_files present, else action) per cmd_file pattern.
            # T-5015: a preserved operator_intent restores first.
            fields["intent"] = fields.get("operator_intent") or (
                "ratify" if fields.get("ratifiable_files") else "action")
        elif new_target != "operator" and "intent" in fields:
            # Retargeted away from operator: intent no longer applicable; drop it
            # (target=operator-specific surface per T-0465). NOT a loss of the
            # ticket's ratify nature: ratifiable_files survive the retarget, and
            # the elif above AUTO-RESTORES intent when the ticket is retargeted
            # back to operator (ratify if ratifiable_files present). The round-trip
            # is therefore self-healing — pinned by
            # test_cmd_retarget_to_operator_no_intent_autorestores_ratify.
            dropped_intent = fields.pop("intent")
            # T-5015: keep the provenance. The decision-register close gate
            # fires on `intent:` OR `operator_intent:`, so an operator carrier
            # retargeted to an agent is still gated when it closes.
            fields["operator_intent"] = dropped_intent

        if new_target == "operator":
            fields.pop("operator_intent", None)  # T-5015: back with the operator; intent is live again

        # T-1103 / ORG-034: serves — arc-intent field. When --serves is given,
        # overwrite (or set) the field. When absent, leave existing serves: intact.
        raw_serves = getattr(args, "serves", None)
        if isinstance(raw_serves, str) and raw_serves.strip():
            fields["serves"] = raw_serves.strip()

        # T-2350: drop the PREVIOUS target's delivery stamp. `auto_delivered` is
        # a dispatch mutex (T-0864 arm-B / the T-0768 incident pattern): while it
        # is set, inbox delivery for this ticket is suppressed. Handing the
        # ticket to a new target without clearing it means the new target can sit
        # undispatched behind a stamp recording a delivery that was made to
        # someone else — and nothing reports it, because the retarget itself
        # succeeds and the `target` field is correct.
        #
        # The cascade-unblock path already pops these unconditionally for exactly
        # this reason (T-0971 arm-A). This mirrors it rather than inventing a
        # second policy; keep the two in step.
        #
        # Cleared VISIBLY, in the transition log. A silent mutation of a
        # dispatch-affecting field is what made the original defect hard to find.
        had_ad = fields.pop("auto_delivered", None)
        had_ad_at = fields.pop("auto_delivered_at", None)
        # T-5049: a deliberate re-route re-offers the ticket to every seat.
        had_declined = fields.pop("auto_declined_by", None)

        actor = _actor()
        log_verb = _LOG_VERB["retarget"]
        detail = f"{old_target} → {new_target}"
        if note:
            detail = f"{detail}; {note}"
        if new_state != old_state:
            detail = f"{detail}; state {old_state} → {new_state} (cross-actor reset)"
        if had_ad is not None or had_ad_at is not None:
            detail = f"{detail}; auto_delivered stamp cleared"
        if had_declined is not None:
            detail = f"{detail}; auto_declined_by cleared"
        if _release_pool_holder(fields, in_inbox=new_state == "inbox"):
            detail = f"{detail}; delivered_to cleared"
        if dropped_awaiting_merge:
            detail = f"{detail}; awaiting-merge substate cleared"
        if dropped_intent is not None:
            detail = (
                f"{detail}; intent={dropped_intent} dropped "
                "(operator-specific; auto-restores on return to operator)"
            )
        body = append_transition(body, actor, log_verb, detail)

        # T-3020 BT2 / ORG-078: stamp when target last changed. Same write +
        # same commit as the target/state mutation below — free transactionality.
        if new_state != old_state:
            fields["state"] = new_state
        fields["retargeted_at"] = _now_iso()
        # T-4751: build (and so run the ORG-021 registry check) BEFORE the git
        # mv below, so a refuse-mode refusal leaves no trace.
        new_text = _build_ticket_text(fields, body, old_fields=old_fields)

        # If state changes, also git-mv the file to the new state directory.
        paths_to_stage: list[Path] = [path]
        if new_state != old_state:
            new_dir = TICKETS_ROOT / new_state
            new_dir.mkdir(parents=True, exist_ok=True)
            new_path = new_dir / path.name
            old_path_abs = path
            rel_old = str(path.relative_to(REPO_ROOT))
            rel_new = str(new_path.relative_to(REPO_ROOT))
            res = _git("mv", rel_old, rel_new)
            if res.returncode != 0:
                raise TicketError(
                    f"git mv failed: {res.stderr.strip() or res.stdout.strip()}"
                )
            path = new_path
            paths_to_stage = [old_path_abs, new_path]

        path.write_text(new_text, encoding="utf-8")

        title = _extract_title(body)
        commit_message = _build_commit_message(
            "retarget", args.ticket, title, detail
        )
        subject = commit_message.splitlines()[0]
        if len(subject) > 100:
            print(
                f"warning: commit subject {len(subject)} chars (>100): {subject!r}",
                file=sys.stderr,
            )

        _commit_and_push(paths_to_stage, commit_message, no_push=args.no_push)
    state_suffix = f" (state {old_state} → {new_state})" if new_state != old_state else ""
    print(f"retarget {args.ticket} → {new_target}{state_suffix}")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    """T-5027: add ratifiable_files / no_file_reason to an EXISTING ticket.

    Fixes tickets filed before the T-5014 forced-choice gate. Reuses its helpers.
    Exactly one of --ratifiable-file (repeatable; any target) or --no-file
    (target=operator only; refused if the ticket already has files) is required.
    Attaching a file clears a stale no_file_reason. Never changes target, state
    or intent. Refuses on done.
    """
    path = _find_ticket(args.ticket)
    if path.parent.name == "done":
        raise TicketRefusal(
            f"state-mismatch: {args.ticket} is in `done`, attach refuses on done (closed)"
        )
    new_files = _parse_ratifiable_files(
        [e.strip() for e in (getattr(args, "ratifiable_file", None) or [])]
    )
    _refuse_excluded_ratifiable_class(new_files)  # T-6071 / ORG-015
    raw_reason = getattr(args, "no_file", None)
    has_reason_arg = isinstance(raw_reason, str) and bool(raw_reason.strip())
    if not new_files and not has_reason_arg:
        raise TicketRefusal(
            "attach needs --ratifiable-file <repo>:<path> (repeatable) or "
            f'--no-file "<reason, >={NO_FILE_MIN_CHARS} chars>" (T-5027)'
        )
    # Reuse the T-5014 gate for mutual exclusion + reason length (always as
    # target=operator: the gate is operator-specific; the non-operator refusal of
    # --no-file is enforced below once the ticket's target is known).
    no_file = _operator_attachment_gate(
        "operator", new_files, raw_reason,
        fix_prefix=f"ticket.py attach {args.ticket}",
    )
    if new_files:
        _check_ratifiable_resolve(new_files)

    with git_mutation_lock(label=f"attach {args.ticket}"):
        _pull_if_needed(args.no_push)
        path = _find_ticket(args.ticket)  # state may have moved during the pull
        if path.parent.name == "done":
            raise TicketRefusal(
                f"state-mismatch: {args.ticket} is in `done`, attach refuses on done"
            )
        fields, body = parse_front_matter(path.read_text(encoding="utf-8"))
        old_fields = _fm_snapshot(fields)
        existing = list(fields.get("ratifiable_files") or [])
        had_reason = bool(str(fields.get("no_file_reason") or "").strip())
        if no_file:
            if fields.get("target") != "operator":
                raise TicketRefusal(
                    f"--no-file is meaningful only on target=operator tickets "
                    f"({args.ticket} target={fields.get('target')!r}); attach a "
                    "file instead (T-5027)"
                )
            if existing:
                raise TicketRefusal(
                    f"{args.ticket} already has ratifiable_files {existing}; "
                    "a no-file reason would contradict them (T-5027)"
                )
            fields["no_file_reason"] = no_file
            detail = f"no-file: {no_file}"
        else:
            added = [e for e in dict.fromkeys(new_files) if e not in existing]
            fields["ratifiable_files"] = existing + added
            labels = list(fields.get("labels") or [])
            if "ratification" not in labels:  # ORG-022 pairing, as cmd_file/retarget
                fields["labels"] = labels + ["ratification"]
            detail = "attached ratifiable_files: " + (", ".join(added) or "(none new)")
            if had_reason:
                fields.pop("no_file_reason", None)
                detail += "; no_file_reason cleared"
        body = append_transition(body, _actor(), _LOG_VERB["attach"], detail)
        new_text = _build_ticket_text(fields, body, old_fields=old_fields)
        path.write_text(new_text, encoding="utf-8")
        commit_message = _build_commit_message(
            "attach", args.ticket, _extract_title(body), detail
        )
        _commit_and_push([path], commit_message, no_push=args.no_push)
    print(f"attach {args.ticket}: {detail}")
    return 0


def cmd_purpose(args: argparse.Namespace) -> int:
    """Set structured purpose fields without changing lifecycle state."""
    from tools.ticket_purpose import PurposeEnforcement

    tid = args.ticket
    project_present, project_id, nearest = _purpose_values_from_args(args)
    if not project_present or nearest is None:  # argparse requires both; defensive API guard
        raise TicketRefusal("purpose requires --project-id and --nearest-intent-ref")
    raw_serves = getattr(args, "serves", None)
    serves = raw_serves.strip() if isinstance(raw_serves, str) and raw_serves.strip() else None

    _find_ticket(tid)  # fail before taking the mutation lock when plainly absent

    # T-4090 REBOUND-1 (finding 2): a nonexistent --project-id record must
    # refuse cleanly and EARLY, before `_ensure_project_label_justifies_
    # filing` below ever gets a chance to print its WARN-mode "WARNING" +
    # "auto-added label ... to justify" note -- those prints imply progress
    # this command does not actually deliver, because the pre-existing
    # downstream purpose-graph check a few lines further down hardcodes
    # `PurposeEnforcement.ENFORCE` regardless of `TICKET_PURPOSE_ENFORCEMENT`
    # and refuses on a dangling project_id anyway. This check is
    # deliberately unconditional on `TICKET_PURPOSE_ENFORCEMENT` to match
    # that unconditional-ENFORCE downstream check (left untouched here) --
    # `cmd_purpose` always fully validates, unlike `cmd_file`, which reuses
    # the real mode for its own downstream check and is untouched by this
    # fix (its WARN-mode auto-add-and-continue behavior is correct there).
    if project_id is not None:
        from tools.project_catalogue import load_project_records

        records = load_project_records(REPO_ROOT / "projects" / "records")
        if project_id not in records:
            raise TicketRefusal(
                f"purpose {tid}: no projects/records/{project_id}.yaml; "
                f"create the record first, or set project_id null"
            )

    purpose_mode = PurposeEnforcement(TICKET_PURPOSE_ENFORCEMENT)
    no_push = getattr(args, "no_push", False)
    with git_mutation_lock(label=f"purpose {tid}"):
        _pull_if_needed(no_push)
        path = _find_ticket(tid)
        fields, body = parse_front_matter(path.read_text(encoding="utf-8"))
        old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
        candidate = dict(fields)
        candidate["project_id"] = project_id
        candidate["nearest_intent_ref"] = nearest
        if serves is not None:
            candidate["serves"] = serves
        # T-4090: same gap as `file` -- a purpose-set that leaves project_id
        # unjustified (no exact-label match, no ticket:-chain hop) must not
        # land silently. `purpose` has no --labels flag, so the only labels
        # available are whatever the ticket already carries.
        candidate_labels = list(candidate.get("labels") or [])
        justified_labels = _ensure_project_label_justifies_filing(
            project_id, candidate_labels, nearest, mode=purpose_mode
        )
        if justified_labels != candidate_labels:
            candidate["labels"] = justified_labels
        if candidate == fields:
            raise TicketRefusal(
                f"purpose: {tid} already has exactly those values; refusing empty no-op"
            )
        issues = _purpose_issues_for_fields(
            candidate, mode=PurposeEnforcement.ENFORCE
        )
        if issues:
            detail = "; ".join(f"{issue.code}: {issue.detail}" for issue in issues)
            raise TicketRefusal(f"purpose validation refused {tid}: {detail}")
        fields = candidate
        body = append_transition(
            body,
            _actor(),
            "purpose-set",
            f"project_id={project_id if project_id is not None else 'null'}; "
            f"nearest_intent_ref={nearest}",
        )
        path.write_text(_build_ticket_text(fields, body, old_fields=old_fields), encoding="utf-8")
        _commit_and_push(
            [path], f"tickets: set structured purpose for {tid}", no_push=no_push
        )
    print(f"purpose set for {tid} (state preserved: {fields.get('state')})")
    return 0


# ---------------------------------------------------------------------------
# T-3020 BT5 / ORG-078 scope-addition: `ticket.py label` verb (add/remove/list)
# + backfill-audit. Motivated by Recovery's rollout-gap flag — ORG-078 sorts on
# arc-labels but tickets don't reliably carry them (T-3026 had zero labels).
# See T-3020 transition log for the full scope-addition note.
# ---------------------------------------------------------------------------


def _read_labels(tid: str) -> list[str]:
    """Read-only: current `labels` frontmatter for a ticket. No side effects."""
    path = _find_ticket(tid)
    text = path.read_text(encoding="utf-8")
    fields, _body = parse_front_matter(text)
    return list(fields.get("labels") or [])


def _labels_add_frontmatter(
    fields: dict, labels_to_add: list[str]
) -> tuple[dict, list[str], list[str]]:
    """Pure: add `labels_to_add` to `fields["labels"]`, deduping.

    Returns `(new_fields, added, already_present)`. Order of `labels_to_add`
    is preserved for both the `added` and `already_present` outputs.
    """
    current = list(fields.get("labels") or [])
    added: list[str] = []
    already_present: list[str] = []
    for label in labels_to_add:
        if label in current:
            already_present.append(label)
        else:
            current.append(label)
            added.append(label)
    new_fields = dict(fields)
    new_fields["labels"] = current
    return new_fields, added, already_present


def _labels_remove_frontmatter(
    fields: dict, labels_to_remove: list[str]
) -> tuple[dict, list[str], list[str]]:
    """Pure: remove `labels_to_remove` from `fields["labels"]`.

    Returns `(new_fields, removed, missing)`. Does not decide error-vs-no-op
    semantics for a partial miss — that's `cmd_label_remove`'s job (this stays
    a pure field-transform, mirroring `_labels_add_frontmatter`).
    """
    current = list(fields.get("labels") or [])
    missing = [label for label in labels_to_remove if label not in current]
    removed = [label for label in labels_to_remove if label in current]
    new_current = [label for label in current if label not in labels_to_remove]
    new_fields = dict(fields)
    new_fields["labels"] = new_current
    return new_fields, removed, missing


def _refuse_if_label_removal_unjustifies_project(tid: str, new_fields: dict) -> None:
    """T-4090: refuse a label removal that would leave a non-done ticket's
    `project_id` unjustified (no exact-label match, no `ticket:`-chain hop to
    a same-project ancestor). Mirrors the file-time guard from the other
    direction -- `ticket.py file` / `purpose` can't land an unjustified
    project_id anymore, and `label remove` can't strip the ONLY thing
    justifying one either. No-op when `project_id` is absent, or when the
    ticket's own `state` (the same frontmatter source this function already
    reads to build the `TicketPurpose` candidate below) is `done` -- matches
    the repo's established convention that historic closure remains readable
    and quiet (`ticket_purpose.validate_intent_link`'s done-state exemption,
    T-4090 REBOUND-1 finding 1).
    """
    project_id = new_fields.get("project_id")
    if project_id is None:
        return
    if str(new_fields.get("state") or "active") == "done":
        return
    from tools.ticket_purpose import (
        TicketPurpose,
        load_ticket_purpose_index,
        project_id_justified,
    )

    purposes = load_ticket_purpose_index(TICKETS_ROOT)
    candidate = TicketPurpose(
        ticket_id=tid,
        project_id=project_id,
        project_id_present="project_id" in new_fields,
        nearest_intent_ref=new_fields.get("nearest_intent_ref"),
        serves=new_fields.get("serves"),
        labels=frozenset(str(label) for label in (new_fields.get("labels") or [])),
        state=str(new_fields.get("state") or "active"),
    )
    purposes[tid] = candidate
    if not project_id_justified(candidate, purposes):
        raise TicketRefusal(
            f"label remove {tid}: would leave project_id={project_id!r} "
            f"unjustified (no exact-label match, no ticket:-chain hop to a "
            f"same-project ancestor); keep the label, set project_id: null "
            f"via `ticket.py purpose`, or repoint nearest_intent_ref first"
        )


def cmd_label_add(args: argparse.Namespace) -> int:
    """`ticket.py label add <T-NNNN> <label> [<label>...]` — idempotent add.

    Adding an already-present label is a no-op for that label (not an error);
    if EVERY named label is already present, the whole call is a no-op (exit 0,
    no commit). Refuses on done/ (closed; matches retarget/block convention).
    """
    tid: str = args.ticket
    labels_to_add: list[str] = args.labels
    no_push: bool = getattr(args, "no_push", False)

    path = _find_ticket(tid)
    if path.parent.name == "done":
        raise TicketRefusal(
            f"state-mismatch: {tid} is in `done`; label add refuses on done (closed)"
        )

    text = path.read_text(encoding="utf-8")
    fields, body = parse_front_matter(text)
    old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
    new_fields, added, already_present = _labels_add_frontmatter(fields, labels_to_add)

    if not added:
        print(
            f"label add {tid}: already-present: {', '.join(already_present)}; no change",
            file=sys.stderr,
        )
        return 0

    # T-5678: adding a security label is a security-shaped mutation: it needs the same
    # ORG-105 chain as `file`, unless the ticket already went through the gate.
    gate_note = ""
    if (_security_gate.label_hits(added)
            and not _security_gate.label_hits(fields.get("labels") or [])
            and not fields.get("security_gate")):
        if path.parent.name == "active":
            raise TicketRefusal(
                f"label add refused: {tid} is active, and a security label would make it "
                f"security_gate: pending under someone's hands. `unclaim` it first, or have "
                f"HO accept it directly (security-accept {tid})."
            )
        gate_fields = _security_gate_fields(
            title=_extract_title(body) or "", ask=None, labels=new_fields.get("labels") or [],
            threat_chain=getattr(args, "threat_chain", None),
            path_justification=getattr(args, "path_justification", None),
            not_security=None,
        )
        new_fields.update(gate_fields)
        gate_note = "; security_gate: pending (ORG-105 chain recorded)"

    actor = _actor()
    detail = f"labels added: {', '.join(added)}{gate_note}"
    if already_present:
        detail += f"; already-present (skipped): {', '.join(already_present)}"
    body = append_transition(body, actor, "labels added", detail)
    new_text = _build_ticket_text(new_fields, body, old_fields=old_fields)

    # T-1770 Part b: pull → write → commit is one atomic critical section.
    with git_mutation_lock(label=f"label add {tid}"):
        _pull_if_needed(no_push)
        path.write_text(new_text, encoding="utf-8")
        commit_message = _build_commit_message("label-add", tid, _extract_title(body), detail)
        _commit_and_push([path], commit_message, no_push=no_push)

    suffix = f"; already-present: [{', '.join(already_present)}]" if already_present else ""
    print(f"label add {tid}: added [{', '.join(added)}]{suffix}")
    return 0


def cmd_label_remove(args: argparse.Namespace) -> int:
    """`ticket.py label remove <T-NNNN> <label> [<label>...]`.

    ALL named labels absent -> idempotent no-op (exit 0, warning, no commit) —
    names no target that's actually there, same shape as `block --clear-deps`.
    SOME (not all) named labels absent -> CLEAN ERROR (exit 1) naming the miss
    — mirrors `block --clear-dep`'s asymmetry: a partial miss names a specific
    target that isn't there, so it's surfaced rather than silently dropped.
    Refuses on done/ (closed; matches retarget/block convention).
    """
    tid: str = args.ticket
    labels_to_remove: list[str] = args.labels
    no_push: bool = getattr(args, "no_push", False)

    path = _find_ticket(tid)
    if path.parent.name == "done":
        raise TicketRefusal(
            f"state-mismatch: {tid} is in `done`; label remove refuses on done (closed)"
        )

    text = path.read_text(encoding="utf-8")
    fields, body = parse_front_matter(text)
    old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
    new_fields, removed, missing = _labels_remove_frontmatter(fields, labels_to_remove)

    if missing and not removed:
        print(
            f"label remove {tid}: none of {labels_to_remove} present; no change",
            file=sys.stderr,
        )
        return 0
    if missing:
        current = fields.get("labels") or []
        raise TicketRefusal(
            f"label remove {tid}: label(s) not present: {missing}; "
            f"current labels = {current or '[]'}"
        )

    # T-4090: refuse before any mutation if this removal would leave a
    # non-done ticket's project_id unjustified.
    _refuse_if_label_removal_unjustifies_project(tid, new_fields)

    actor = _actor()
    detail = f"labels removed: {', '.join(removed)}"
    body = append_transition(body, actor, "labels removed", detail)
    new_text = _build_ticket_text(new_fields, body, old_fields=old_fields)

    # T-1770 Part b: pull → write → commit is one atomic critical section.
    with git_mutation_lock(label=f"label remove {tid}"):
        _pull_if_needed(no_push)
        path.write_text(new_text, encoding="utf-8")
        commit_message = _build_commit_message("label-remove", tid, _extract_title(body), detail)
        _commit_and_push([path], commit_message, no_push=no_push)

    print(f"label remove {tid}: removed [{', '.join(removed)}]")
    return 0


def cmd_label_list(args: argparse.Namespace) -> int:
    """`ticket.py label list <T-NNNN>` — print current labels, one per line.
    Read-only; no side effects."""
    for label in _read_labels(args.ticket):
        print(label)
    return 0


_LINT_FRONTMATTER_EXAMPLES = 3


def _lint_frontmatter_unavailable(args: argparse.Namespace, reason: str) -> int:
    """T-4760 (e): one line, never a traceback. Report mode fails OPEN (WARN,
    exit 0); --strict exits 1 with an explicit "registry unavailable"."""
    reason = " ".join(str(reason).split())
    if args.strict:
        print(f"lint-frontmatter: registry unavailable ({reason}) — --strict cannot "
              "vouch for the corpus; failing", file=sys.stderr)
        return 1
    print(f"[frontmatter-registry] WARN lint-frontmatter: registry unavailable "
          f"({reason}) — report skipped", file=sys.stderr)
    return 0


def cmd_lint_frontmatter(args: argparse.Namespace) -> int:
    """`ticket.py lint-frontmatter` (T-4751 S1): read-only corpus report of
    frontmatter that the ORG-021 field registry does not sanction — unregistered
    keys, out-of-enum values, legacy-field use — with counts + example ids.

    Scans tickets/{inbox,active,done}/T-*.md; files without frontmatter are
    skipped. Exit 0 always (it is a report); --strict exits 1 on an
    unknown-field, a bad-value, or an unavailable registry (the S2 CI gate).
    T-4762: legacy-field use on stored tickets is printed as info and never
    fails --strict (old tickets are immutable history). A hand-added legacy
    field is therefore NOT caught by this lint, only at write time (refuse
    mode via ticket.py).

    T-4760 (e): any UNEXPECTED error from the registry module is caught and
    handled like an unavailable registry (one WARN + exit 0; --strict: exit 1
    with "registry unavailable"), never a traceback. T-4760 (f): allowed off
    `main` (read-only; its parser sets off_main_ok=True, honoured in main()).
    """
    try:
        return _lint_frontmatter_impl(args)
    except Exception as exc:  # noqa: BLE001 — fail open on the read-only report
        return _lint_frontmatter_unavailable(args, f"{type(exc).__name__}: {exc}")


def _lint_frontmatter_impl(args: argparse.Namespace) -> int:
    if _fm_registry is None:
        return _lint_frontmatter_unavailable(
            args, "tools/frontmatter_registry.py not importable")
    registry = _fm_registry.load_registry()
    if registry is None:
        # load_registry already printed its one WARN with the reason; report
        # mode adds nothing (one WARN total), --strict adds its failure line.
        if args.strict:
            return _lint_frontmatter_unavailable(args, "see the WARN above")
        return 0
    groups: dict = {}
    scanned = 0
    unparseable: list[str] = []
    for state in STATES:
        d = TICKETS_ROOT / state
        if not d.is_dir():
            continue
        for path in sorted(d.glob("T-*.md")):
            if not TICKET_FILENAME_RE.fullmatch(path.name):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                unparseable.append(path.stem)
                continue
            if not text.startswith("---\n"):
                continue
            try:
                fields, _body = parse_front_matter(text)
            except TicketError:
                unparseable.append(path.stem)
                continue
            scanned += 1
            for f in _fm_registry.check_fields(registry, None, fields):
                value = f.value if f.kind == "bad-value" else None
                groups.setdefault((f.kind, f.field, value), []).append(path.stem)
    total = sum(len(v) for v in groups.values())
    print(f"lint-frontmatter: registry {registry.source.name} "
          f"(v{registry.version}, {len(registry.fields)} fields); "
          f"scanned {scanned} tickets; {total} finding(s) in {len(groups)} group(s)")
    by_kind: dict = {}
    for (kind, _f, _v), ids in groups.items():
        by_kind[kind] = by_kind.get(kind, 0) + len(ids)
    for kind in ("unknown-field", "bad-value", "legacy-write"):
        if kind in by_kind:
            label = "legacy-field use" if kind == "legacy-write" else kind
            print(f"  {label}: {by_kind[kind]}")
    for (kind, fname, value), ids in sorted(
            groups.items(), key=lambda kv: (kv[0][0], -len(kv[1]), kv[0][1])):
        label = "legacy-field use (info)" if kind == "legacy-write" else kind
        what = f"{fname}: {value}" if value is not None else fname
        ex = ", ".join(ids[:_LINT_FRONTMATTER_EXAMPLES])
        more = f" (+{len(ids) - _LINT_FRONTMATTER_EXAMPLES} more)" if len(ids) > _LINT_FRONTMATTER_EXAMPLES else ""
        print(f"  [{label}] {what} — {len(ids)} ticket(s): {ex}{more}")
    if unparseable:
        ex = ", ".join(unparseable[:_LINT_FRONTMATTER_EXAMPLES])
        print(f"  unparseable frontmatter (skipped): {len(unparseable)}: {ex}")
    if args.strict and (by_kind.get("unknown-field") or by_kind.get("bad-value")):
        return 1
    return 0


def _warn_recent_other_annotators(body: str, actor: str) -> None:
    """Warn if ANOTHER actor wrote to this ticket's transition log recently.

    T-2260's "one thing to check before building". Scoped to one ticket, so it
    is a measurement rather than the label-overlap proxy `file` has to use.
    Never raises; a scan failure must not cost an annotation.
    """
    try:
        cutoff = _parse_iso(_now_iso()) - timedelta(
            minutes=_NEAR_DUPLICATE_WINDOW_MINUTES)
        others: list[tuple[str, str]] = []
        for line in body.splitlines():
            m = re.match(r"^- (\S+) \[([^\]]+)\]", line.strip())
            if not m:
                continue
            stamp, who = m.group(1), m.group(2)
            if who == actor:
                continue
            try:
                if _parse_iso(stamp) >= cutoff:
                    others.append((stamp, who))
            except Exception:
                continue
        if not others:
            return
        print(
            f"note: {len(others)} entr(ies) from another actor in the last "
            f"{_NEAR_DUPLICATE_WINDOW_MINUTES} min on this ticket — check you are "
            f"not restating their finding:",
            file=sys.stderr,
        )
        for stamp, who in others[-3:]:
            print(f"  {stamp} [{who}]", file=sys.stderr)
    except Exception:
        return


def cmd_annotate(args: argparse.Namespace) -> int:
    """Append a dated transition-log note with NO state change (T-1972).

    Fills the gap where a note needs to land on a ticket's own record but
    nothing about its state changes: `retarget --to <same>` no-ops without
    writing the note, hand-editing the file is barred by the direct-edit
    convention, and a ledger entry splits the ticket's record across two
    surfaces. This is retarget's same-state path minus the target edit: append
    + commit + push, one file, no git-mv.

    Refuses on `done/` — appending to a closed ticket's resolve-evidence is a
    different concern (resolve owns that surface). Refuses on an empty/
    whitespace-only note — a no-op write is not worth a commit.
    """
    note = (getattr(args, "note", "") or "").strip()
    if not note:
        raise TicketRefusal(
            "annotate requires a non-empty --note (an empty note is a no-op; "
            "nothing to append)"
        )
    _warn_decision_line_shape(note, surface=f"{args.ticket} annotate --note")

    path = _find_ticket(args.ticket)
    if path.parent.name == "done":
        raise TicketRefusal(
            f"state-mismatch: {args.ticket} is in `done`; annotate refuses on "
            f"done (appending to a closed ticket's resolve-evidence is a "
            f"separate concern)"
        )

    # Same atomic critical section as retarget's same-state path (T-1770 Part b):
    # lock spans pull → edit → commit against other writers on the shared checkout.
    with git_mutation_lock(label=f"annotate {args.ticket}"):
        # T-2204: annotate is the ONE write that does not need to be current —
        # it appends to a ticket that already exists at a path already
        # resolved, and the commit is scoped to that single file. A dirty tree
        # used to abort the internal pull and take EVERY write down with it,
        # so the reporting path died exactly when the remediation path did.
        # Degrade to a warning here; every state-moving/ID-allocating caller
        # stays strict. --no-pull skips the pull outright for the same reason.
        _pull_if_needed(
            args.no_push or getattr(args, "no_pull", False), strict=False
        )

        text = path.read_text(encoding="utf-8")
        fields, body = parse_front_matter(text)
        old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
        actor = _actor()
        # verb="note" (not a state verb) so a scanner distinguishes a free
        # annotation from a lifecycle event like "claimed"/"resolved".
        body = append_transition(body, actor, "note", note)
        new_text = _build_ticket_text(fields, body, old_fields=old_fields)
        path.write_text(new_text, encoding="utf-8")

        title = _extract_title(body)
        commit_message = _build_commit_message("annotate", args.ticket, title, note)
        subject = commit_message.splitlines()[0]
        if len(subject) > 100:
            print(
                f"warning: commit subject {len(subject)} chars (>100): {subject!r}",
                file=sys.stderr,
            )
        _commit_and_push([path], commit_message, no_push=args.no_push)

    print(f"annotate {args.ticket}: note appended")
    # T-2260 asked whether the same warning belongs here. It does, but NOT the
    # same warning: the file-time signal is "a similar ticket exists", which
    # needs label overlap as a proxy for sameness. On annotate the target is
    # already one specific ticket, so the question collapses to "did someone
    # else write here in the last few minutes" — a precise check with no
    # false-positive class, rather than a proxy. The near-miss it catches is
    # the one the filing named: two agents annotating the same ticket with the
    # same finding produces no duplicate ID and therefore leaves no trace to
    # notice later.
    _warn_recent_other_annotators(body, actor)
    return 0


# T-2273: shapes an author uses when they have just predicted a recurrence.
# Deliberately SURFACE-SYNTAX, not semantics — the same admitted-proxy posture as
# the T-2260 near-duplicate warning, and for the same reason: the reader is the
# author, one second after writing the sentence, and they can answer in one
# second. A cleverer matcher that adjudicates would be worse than a crude one
# that asks.
_RECURRENCE_PREDICTION_PATTERNS = (
    r"\bwill hit the same\b",
    r"\bwill (?:also )?(?:hit|see|face|encounter) this\b",
    r"\bthe next \w+(?: \w+)? will\b",
    r"\brecurs? (?:when|if|on)\b",
    r"\bwill recur\b",
    r"\bwill happen again\b",
    r"\bsame thing will\b",
    r"\bwill trip (?:over )?(?:the|this) same\b",
    r"\bnext (?:new[- ])?\w+ deploy will\b",
    r"\banyone (?:who|doing) .{0,40}will\b",
    r"\bthis will bite\b",
    r"\bwill bite (?:the|whoever|anyone)\b",
)


def _recurrence_predictions(text: str) -> list[str]:
    """Return the matched fragments of `text` that predict a recurrence.

    T-2273. A finding recorded in a RESOLVED ticket's prose is on nobody's
    queue: a resolve note enters the audit chain and exits every queue in the
    same act. The worked case is Recovery's T-2188 note — correct diagnosis, correct
    root cause, an explicit prediction that the next new-agent deploy would hit
    it — followed within a day by two agents rediscovering it from scratch. The
    class definition is artisan's reviewer's: **"silently skipped" and "loudly
    recorded where nobody will look again" produce the same outcome.**

    Verified before building (T-1885 duplicate-instrument trap): knowledge_sweep
    covers lessons/ + bugs/ + incidents/ + notes/ + memory pins. `tickets/done/`
    is NOT in its corpus, so nothing installed catches this.

    Never raises; a scan failure must not cost a resolve.
    """
    found: list[str] = []
    for pat in _RECURRENCE_PREDICTION_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            s, e = max(0, m.start() - 40), min(len(text), m.end() + 40)
            found.append("…" + text[s:e].replace("\n", " ").strip() + "…")
    return found


def _warn_recurrence_prediction(text: str, tid: str) -> None:
    """Ask, at resolve time, whether a predicted recurrence needs a ticket."""
    try:
        hits = _recurrence_predictions(text or "")
    except Exception:
        return
    if not hits:
        return
    print(
        f"note: this resolve note predicts a recurrence. A finding in a resolved "
        f"ticket is on nobody's queue — {tid} is about to leave every queue it was "
        f"on. If the prediction needs an owner, file it now (T-2273):",
        file=sys.stderr,
    )
    for h in hits[:3]:
        print(f"  {h}", file=sys.stderr)


_SUPERSEDED_ASK_MARKER = "SUPERSEDED ASK"
# Deliberately BOLD TEXT, not a `###` subheading. inbox_consumer._ask_body()
# terminates its extraction on `\n##`, which `###` also matches — so a
# subheading here would truncate the auto-delivered prompt at exactly this
# line and the superseded original would never reach the acting agent. The
# block above it says "act on this, not on the text below": that sentence has
# to refer to text the reader can actually see, and the reader is often an
# injected prompt rather than a person.
_ORIGINAL_ASK_MARKER = "**Original Ask (superseded — kept for the record):**"


def _supersede_ask_section(body: str, correction: str, actor: str, stamp: str) -> str:
    """Return `body` with `correction` prepended INSIDE the `## Ask` section.

    T-2247. The Ask is the field most likely to be acted on and, until now, the
    only one with no correction path: `annotate` appends to the transition log,
    which on a long ticket puts the fix hundreds of lines below the instruction
    it fixes.

    WHY THE CORRECTION GOES INSIDE THE SECTION rather than anywhere else on the
    ticket, and this is the load-bearing reason rather than a formatting
    preference: `inbox_consumer._ask_body()` extracts EXACTLY this section and
    injects it as the auto-delivered prompt. For an auto-delivered ticket, text
    outside `## Ask` is not merely read late — **it is not delivered at all.**
    A correction in the transition log reaches a human scrolling down and
    reaches the acting agent never.

    WHY THE ORIGINAL IS KEPT rather than overwritten: what was asked is part of
    the record, and a ticket whose Ask can be silently rewritten is a ticket
    whose dispatch cannot be audited (P4). This is a prepend plus a demotion,
    not an edit — the original moves under a marked subheading, intact.

    Repeated supersedes stack newest-first, each with its own stamp, and the
    original is demoted only once. So the reading order is always: newest
    correction, older corrections, original.
    """
    m = re.search(r"(##\s+Ask\s*\n)(.*?)(?=\n##\s|\Z)", body, re.DOTALL)
    if m is None:
        raise TicketRefusal(
            "supersede-ask: could not locate a `## Ask` section in this ticket. "
            "The section heading is what inbox_consumer extracts for the "
            "auto-delivered prompt, so a ticket without one has no correctable "
            "Ask — use annotate."
        )
    heading, current = m.group(1), m.group(2)

    block = (
        f"> ⚠️ **{_SUPERSEDED_ASK_MARKER} — corrected {stamp} by {actor}.** "
        f"Act on this, not on the text below it.\n>\n"
        + "\n".join(f"> {line}" if line.strip() else ">"
                    for line in correction.strip().splitlines())
        + "\n"
    )

    if _ORIGINAL_ASK_MARKER in current:
        # Already superseded once: stack this correction above the previous
        # ones, leaving the single demotion marker where it is.
        new_section = "\n" + block + "\n" + current.lstrip("\n")
    else:
        new_section = (
            "\n" + block + "\n"
            + _ORIGINAL_ASK_MARKER + "\n\n"
            + current.strip("\n") + "\n"
        )

    # Repeated supersedes would otherwise accumulate a blank line per round.
    rebuilt = body[:m.start()] + heading + new_section + body[m.end():]
    return re.sub(r"\n{3,}", "\n\n", rebuilt)


def cmd_supersede_ask(args: argparse.Namespace) -> int:
    """Correct a stale `## Ask` in place, preserving the original (T-2247).

    Refuses on `done/` for the same reason `annotate` does: a closed ticket's
    Ask is history, and resolve owns that surface. Refuses on an empty
    correction.
    """
    correction = (getattr(args, "with_correction", "") or "").strip()
    if not correction:
        raise TicketRefusal(
            "supersede-ask requires a non-empty --with (an empty correction "
            "would demote the original Ask and replace it with nothing)"
        )

    path = _find_ticket(args.ticket)
    if path.parent.name == "done":
        raise TicketRefusal(
            f"state-mismatch: {args.ticket} is in `done`; supersede-ask refuses "
            f"on done (a closed ticket's Ask is history — use addendum)"
        )

    with git_mutation_lock(label=f"supersede-ask {args.ticket}"):
        _pull_if_needed(
            args.no_push or getattr(args, "no_pull", False), strict=False
        )
        text = path.read_text(encoding="utf-8")
        fields, body = parse_front_matter(text)
        old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
        actor = _actor()
        stamp = _now_iso()
        body = _supersede_ask_section(body, correction, actor, stamp)
        # The transition log still records it — the in-section block is for the
        # READER, the log line is for anyone scanning the ticket's history.
        body = append_transition(
            body, actor, "note",
            f"[{_SUPERSEDED_ASK_MARKER}] {correction}",
        )
        path.write_text(_build_ticket_text(fields, body, old_fields=old_fields), encoding="utf-8")

        title = _extract_title(body)
        commit_message = _build_commit_message(
            "supersede-ask", args.ticket, title, correction)
        subject = commit_message.splitlines()[0]
        if len(subject) > 100:
            print(
                f"warning: commit subject {len(subject)} chars (>100): {subject!r}",
                file=sys.stderr,
            )
        _commit_and_push([path], commit_message, no_push=args.no_push)

    print(f"supersede-ask {args.ticket}: Ask corrected in place")
    return 0


# Rendered verb for a post-resolution addendum (T-2205). Chosen so the line is
# self-describing to a human scanner AND inert to the consumer that parses
# these lines: weekly_ticket_summary.py matches `\[actor\]\s+(resolved|resolve|
# close-action|close-noodle)\b`. "post-resolution addendum" contains "resolution", never the
# token "resolved" in verb position, so an addendum cannot be miscounted as a
# second resolve — which would inflate every throughput digest.
_ADDENDUM_VERB = "post-resolution addendum"


def cmd_addendum(args: argparse.Namespace) -> int:
    """Append a post-resolution addendum to a `done/` ticket (T-2205).

    A resolved ticket's record was immutable with no correction path: annotate
    hard-refuses on done/, and there is no reopen/amend. So a resolve note that
    later turned out to be wrong — or that a peer's measurement superseded —
    could not be corrected on the surface a reader actually reads. The record
    stayed confidently wrong.

    Deliberately NOT implemented by relaxing annotate's refusal. That refusal
    protects a real property: resolve owns the outcome surface, and you should
    not be able to quietly rewrite how a closed ticket ended. This is a
    distinct operation with the opposite bias — maximum visibility instead of
    silence:

      (a) APPEND-ONLY. It calls append_transition, which only ever adds a
          bullet. No existing line is edited, so the original resolve evidence
          is untouched and remains readable exactly as written.
      (b) VISIBLY POST-RESOLUTION. The verb says so in words, and because the
          log is chronological the addendum necessarily renders BELOW the
          `resolved` line — a reader sees the original outcome and the later
          correction as two events, in order, rather than one revised claim.
      (c) VINTAGE LEGIBLE. It inherits the T-2203 HEAD stamp, so the addendum
          carries the repo state it was written against — which is the whole
          point when the thing being corrected is "this note was already stale
          when it was written."

    Mirror-image guard to annotate's: this REFUSES on anything not in done/,
    and points at annotate. Two operations, two disjoint domains, neither able
    to do the other's job by accident.
    """
    note = (getattr(args, "note", "") or "").strip()
    if not note:
        raise TicketRefusal(
            "addendum requires a non-empty --note (an empty addendum is a "
            "no-op; nothing to append)"
        )

    path = _find_ticket(args.ticket)
    if path.parent.name != "done":
        raise TicketRefusal(
            f"state-mismatch: {args.ticket} is in `{path.parent.name}`, not "
            f"`done`; addendum is the post-resolution correction path. Use "
            f"`annotate` for a ticket that is still open."
        )

    with git_mutation_lock(label=f"addendum {args.ticket}"):
        # Same reasoning as annotate (T-2204): appending to a ticket that
        # already exists at a path already resolved neither allocates an ID nor
        # moves state, so a stale view cannot make the addendum wrong.
        _pull_if_needed(
            args.no_push or getattr(args, "no_pull", False), strict=False
        )

        text = path.read_text(encoding="utf-8")
        fields, body = parse_front_matter(text)
        old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check
        actor = _actor()
        body = append_transition(body, actor, _ADDENDUM_VERB, note)
        new_text = _build_ticket_text(fields, body, old_fields=old_fields)

        # Build the commit message BEFORE writing (T-2205, found by running
        # this for real): _build_commit_message raises on an unrecognised verb,
        # and the original ordering wrote the file first — so the very first
        # live invocation left the ticket modified in the working tree with no
        # commit, which is the same write-then-abort shape T-0244 hoisted the
        # pull out of _commit_and_push to prevent. Everything that can refuse
        # now refuses before anything on disk changes.
        title = _extract_title(body)
        commit_message = _build_commit_message("addendum", args.ticket, title, note)

        path.write_text(new_text, encoding="utf-8")

        subject = commit_message.splitlines()[0]
        if len(subject) > 100:
            print(
                f"warning: commit subject {len(subject)} chars (>100): {subject!r}",
                file=sys.stderr,
            )
        _commit_and_push([path], commit_message, no_push=args.no_push)

    print(f"addendum {args.ticket}: post-resolution addendum appended")
    return 0


# ---------------------------------------------------------------------------
# `list` (read-only).
# ---------------------------------------------------------------------------


def _iter_all_tickets() -> list[Ticket]:
    out: list[Ticket] = []
    for state in STATES:
        d = TICKETS_ROOT / state
        if not d.exists():
            continue
        names = sorted(
            entry.name
            for entry in d.iterdir()
            if entry.is_file() and TICKET_FILENAME_RE.fullmatch(entry.name)
        )
        for name in names:
            out.append(parse_ticket(d / name))
    return out


def compute_unblocks(tickets: list[Ticket]) -> dict[str, list[str]]:
    """Reverse index of `depends_on` (T-2720): unblocks[T] = ids of tickets
    whose depends_on contains T.

    DERIVED-ON-READ, never persisted to frontmatter — depends_on stays the
    single source of truth; a stored reverse edge would need cascade upkeep
    and could desync (see ORG-013 cascade-unblock, which already maintains
    depends_on itself).

    Every id present in `tickets` gets a key, even with zero dependents
    (empty list) — callers never need to guard a KeyError. A dependent
    counts regardless of ITS OWN state (inbox/active/done); the target id
    of a depends_on edge does not need to be present in `tickets` (e.g. a
    partial/test corpus) — that id still gets a key via setdefault.
    """
    out: dict[str, list[str]] = {
        (t.fields.get("id") or t.path.stem): [] for t in tickets
    }
    for t in tickets:
        tid = t.fields.get("id") or t.path.stem
        for dep in t.fields.get("depends_on") or []:
            dep = str(dep).strip()
            if dep:
                out.setdefault(dep, []).append(tid)
    for deps in out.values():
        deps.sort()
    return out


# ---------------------------------------------------------------------------
# T-3020 BT5: `ticket.py label backfill-audit` — report-only suggestion tool.
# Never auto-applies; operator reviews the report and applies via a
# `ticket.py label add` loop (per T-3020's scope-addition note).
# ---------------------------------------------------------------------------

# Labels that name a whole arc/project and are expected to propagate across
# parent_ticket / depends_on chains. Pattern-shaped (not a static list)
# because the brief's own examples ARE patterns: t-XXXX-arc, phase-N-*,
# org-XXX. A label matching one of these AND not in `_PER_TICKET_LABELS`
# below is "inheritable".
_INHERITABLE_LABEL_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^t-\d+-arc$"),          # e.g. t-2953-arc, t-3071-arc
    re.compile(r"^[a-z][a-z0-9]*-arc$"), # generic <project>-arc
    re.compile(r"^phase-\d+(-.+)?$"),    # phase-N-*
    re.compile(r"^org-\d+$"),            # org-XXX
    re.compile(r"^harness-controller$"),
    re.compile(r"^concordance$"),
)

# Labels that describe a property of ONE ticket, not the arc it belongs to —
# never inherited even if they happen to match a pattern above. Exact-match
# denylist; wins over `_INHERITABLE_LABEL_PATTERNS`.
_PER_TICKET_LABELS: frozenset[str] = frozenset({
    "interactive-required", "ratify-impl", "cutover", "preventive",
    "re-eval", "quarterly", "production",
})


def _is_inheritable_label(label: str) -> bool:
    if label in _PER_TICKET_LABELS:
        return False
    return any(pattern.match(label) for pattern in _INHERITABLE_LABEL_PATTERNS)


def _parent_chain_suggestions(t: Ticket, by_id: dict[str, "Ticket"]) -> list[tuple[str, str]]:
    """Direct parent_ticket only (not transitive — ORG-065 graph closure is
    explicitly deferred per T-3020's scope-addition note, option (c))."""
    parent_id = t.fields.get("parent_ticket")
    if not parent_id:
        return []
    parent = by_id.get(str(parent_id).strip())
    if parent is None:
        return []
    current = set(t.fields.get("labels") or [])
    return [
        (label, "parent-chain")
        for label in (parent.fields.get("labels") or [])
        if _is_inheritable_label(label) and label not in current
    ]


def _dep_chain_suggestions(
    t: Ticket, tid: str, dependants_by_id: dict[str, list["Ticket"]]
) -> list[tuple[str, str]]:
    """Direction: dependant -> dependency. If ticket A `depends_on` t and A
    carries an inheritable label, t (the dependency) is probably the same
    arc and is suggested that label. Not the reverse (t's labels are not
    propagated onto A here) — only one direction is named in the brief, and
    doubling it would double the false-positive surface on a report-only
    tool with no clear signal the reverse is wanted."""
    current = set(t.fields.get("labels") or [])
    out: list[tuple[str, str]] = []
    for dependant in dependants_by_id.get(tid, []):
        for label in dependant.fields.get("labels") or []:
            if _is_inheritable_label(label) and label not in current:
                out.append((label, "dep-chain"))
    return out


def _body_pattern_suggestions(
    t: Ticket, known_labels: list[str], *, body_lines: int = 15
) -> list[tuple[str, str]]:
    """Substring-match `known_labels` (arc labels drawn from
    rulebook/priority-list.md) against the ticket title + first `body_lines`
    lines of body. Case-insensitive."""
    current = set(t.fields.get("labels") or [])
    haystack = " ".join([t.title, *t.body.splitlines()[:body_lines]]).lower()
    out: list[tuple[str, str]] = []
    for label in known_labels:
        if label in current or not _is_inheritable_label(label):
            continue
        if label.lower() in haystack:
            out.append((label, f"body-pattern:{label}"))
    return out


def _label_suggestions_for_ticket(
    t: Ticket,
    tid: str,
    by_id: dict[str, "Ticket"],
    dependants_by_id: dict[str, list["Ticket"]],
    known_labels: list[str],
) -> dict[str, set[str]]:
    """label -> set of source tags that suggested it."""
    merged: dict[str, set[str]] = {}
    for label, source in (
        _parent_chain_suggestions(t, by_id)
        + _dep_chain_suggestions(t, tid, dependants_by_id)
        + _body_pattern_suggestions(t, known_labels)
    ):
        merged.setdefault(label, set()).add(source)
    return merged


def _backfill_audit_report(scope: str | None) -> list[str]:
    """Pure (besides reading the ticket tree): one line per ticket-with-
    suggestions, `T-NNNN: missing [label-a, label-b] (source: ... / ...)`.
    `scope` is one of inbox/active/done/all, or None for the default
    inbox+active. Never raises — a priority-list parse failure just means
    `known_labels` degrades to `[]` (parent-chain/dep-chain suggestions are
    unaffected)."""
    if scope == "all":
        states: tuple[str, ...] = STATES
    elif scope:
        states = (scope,)
    else:
        states = ("inbox", "active")

    all_tickets = _iter_all_tickets()
    by_id = {(t.fields.get("id") or t.path.stem): t for t in all_tickets}
    dependants_by_id: dict[str, list[Ticket]] = {}
    for t in all_tickets:
        tid = t.fields.get("id") or t.path.stem
        for dep in t.fields.get("depends_on") or []:
            dependants_by_id.setdefault(str(dep).strip(), []).append(t)

    try:
        known_labels = list(priority_list_parse.parse_file().keys())
    except priority_list_parse.PriorityListParseError:
        known_labels = []

    lines: list[str] = []
    scoped = sorted(
        (t for t in all_tickets if t.state_dir in states),
        key=lambda t: t.fields.get("id") or t.path.stem,
    )
    for t in scoped:
        tid = t.fields.get("id") or t.path.stem
        suggestions = _label_suggestions_for_ticket(t, tid, by_id, dependants_by_id, known_labels)
        if not suggestions:
            continue
        labels_sorted = sorted(suggestions)
        sources_sorted = sorted({s for srcs in suggestions.values() for s in srcs})
        lines.append(
            f"{tid}: missing [{', '.join(labels_sorted)}] "
            f"(source: {' / '.join(sources_sorted)})"
        )
    return lines


_LINEAGE_STATE_CHOICES = ("open", "inbox", "active", "done", "all")
# T-3332 / T-5679: the goals file `lineage` reads its goal keys from, through
# tools/goal_registry.py (the canonical marker parser). Module-level so tests
# can point it at a fixture.
LINEAGE_GOALS_PATH = REPO_ROOT / "rulebook" / "goals.md"


def _lineage_states(raw: str) -> tuple[str, ...]:
    if raw == "open":
        return ("inbox", "active")
    if raw == "all":
        return STATES
    return (raw,)


def _lineage_edge_fields(raw: str | None) -> tuple[str, ...]:
    """--edges: 'default', 'all', or a comma list; a '+f' item adds to default."""
    from tools.ticket_lineage import ALL_UP_FIELDS, DEFAULT_UP_FIELDS

    if not raw or raw == "default":
        return DEFAULT_UP_FIELDS
    if raw == "all":
        return ALL_UP_FIELDS
    items = [item.strip() for item in raw.split(",") if item.strip()]
    base: list[str] = list(DEFAULT_UP_FIELDS) if all(i.startswith("+") for i in items) else []
    for item in items:
        name = item.lstrip("+")
        if name not in ALL_UP_FIELDS:
            raise TicketRefusal(
                f"--edges: unknown edge field {name!r} (choose from {', '.join(ALL_UP_FIELDS)})"
            )
        if name not in base:
            base.append(name)
    return tuple(base)


def cmd_lineage(args: argparse.Namespace) -> int:
    """T-3332 (a): read-only lineage-graph queries (up / down / roots).

    Never writes, commits, fetches or pushes: one corpus read pass through
    ticket_lint.iter_ticket_frontmatter, then in-memory walks. Logic lives in
    tools/ticket_lineage.py; this is only the CLI shell."""
    from tools import ticket_lineage as tl
    from tools.goal_registry import registry_status

    edge_fields = _lineage_edge_fields(getattr(args, "edges", None))
    # T-5679: a missing/unparseable registry is named on stderr, never a
    # silent empty set; every goal ref then reads as goal-ref-unregistered.
    goal_keys, registry_error = registry_status(REPO_ROOT, path=LINEAGE_GOALS_PATH)
    if registry_error is not None:
        print(f"[lineage] WARN goal-registry-unavailable: {registry_error}", file=sys.stderr)
    graph = tl.build_graph(TICKETS_ROOT, goal_keys, edge_fields)
    findings = {
        "unparseable": graph.unparseable,
        "malformed": graph.malformed,
    }

    if args.lineage_cmd == "roots":
        summary = tl.roots_summary(graph, _lineage_states(args.state))
        if args.cls:
            summary["members"] = {args.cls: summary["members"].get(args.cls, [])}
        if args.counts:
            summary.pop("members", None)
        elif args.limit is not None:
            summary["members"] = {k: v[: args.limit] for k, v in summary["members"].items()}
        if args.json:
            out = dict(summary, edge_fields=list(edge_fields),
                       goal_keys=sorted(goal_keys), findings=findings)
            print(json.dumps(out, indent=2, sort_keys=True))
            return 0
        print(f"lineage roots: {summary['total']} ticket(s) in {'/'.join(summary['states'])}; "
              f"edges {','.join(edge_fields)}")
        for cls in tl.PRECEDENCE:
            name = cls.value
            note = "   <- honesty metric: traces to no goal or named root" if cls is tl.Terminus.UNROOTED else ""
            print(f"  {name:<17}{summary['counts'][name]:>6}{note}")
            for code, n in sorted(summary["detail"][name].items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"      {code:<26}{n:>6}")
        if "members" in summary:
            for name, rows in summary["members"].items():
                if not rows:
                    continue
                print(f"\n{name}:")
                for row in rows:
                    via = "" if row["via"] == row["ticket"] else f" via {row['via']}"
                    print(f"  {row['ticket']} [{row['state']}] {row['code']}{via}")
        if graph.unparseable or graph.malformed:
            print(f"findings: {len(graph.unparseable)} unparseable, {len(graph.malformed)} malformed edge value(s)")
        return 0

    direction = args.lineage_cmd
    try:
        if direction == "up":
            node = args.node.strip()
            if node not in graph.nodes:
                raise tl.SeedError(f"unknown ticket {node}")
            seeds = [node]
        else:
            seeds = tl.resolve_down_seeds(graph, args.node)
    except tl.SeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result = tl.walk(graph, seeds, direction, args.depth)
    tl.load_titles(graph, TICKETS_ROOT, result["visited"])
    terminus = {}
    if direction == "up":
        cls, via = tl.ticket_terminus(graph, seeds[0])
        terminus = {"class": cls.cls.value, "code": cls.code, "detail": cls.text, "via": via}
    if args.json:
        out = {
            "query": {"direction": direction, "node": args.node, "seeds": seeds,
                      "depth": args.depth, "edge_fields": list(edge_fields)},
            "nodes": {n: tl.node_record(graph, n) for n in result["visited"]},
            "edges": result["edges"],
            "terminus": terminus or None,
            "findings": dict(findings, cycles=result["cycles"], missing=result["missing"]),
        }
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    for line in tl.render_tree(graph, seeds, direction, args.depth):
        print(line)
    if terminus:
        via = "" if terminus["via"] == seeds[0] else f" via {terminus['via']}"
        print(f"terminus: {terminus['class']} ({terminus['code']}: {terminus['detail']}){via}")
    else:
        print(f"{len(result['visited']) - len(seeds)} node(s) below {len(seeds)} seed(s)")
    if result["cycles"] or result["missing"]:
        print(f"findings: {len(result['cycles'])} cycle(s), {len(result['missing'])} missing ticket(s)")
    return 0


def cmd_label_backfill_audit(args: argparse.Namespace) -> int:
    """`ticket.py label backfill-audit [--scope inbox|active|done|all]`.
    Report-only — never mutates. Exit code is always 0 (informational)."""
    scope = getattr(args, "scope", None)
    lines = _backfill_audit_report(scope)
    if not lines:
        print("backfill-audit: no missing-label suggestions")
        return 0
    for line in lines:
        print(line)
    return 0


def _format_age(created_iso: str, now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        # `Z` suffix is not parsed by fromisoformat in older Python; normalize.
        normalized = created_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "0s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


@dataclass
class _PriorityInfo:
    """Per-ticket ORG-078 priority computation result (T-3020 BT3).

    `own_rank` — ceiling priority-rank across the ticket's OWN labels (§3);
    `math.inf` if none match `rank_map`. `matched_label` — the label that
    produced `own_rank` (None if own_rank is inf). `effective_rank` — after
    dependency-inheritance (§Priority-inversion):
    `min(own_rank, min(effective_rank of tickets whose depends_on contains
    this ticket))`. `elevated_by` — the direct-dependent ticket id that
    supplied `effective_rank`, set ONLY on a STRICT improvement
    (`effective_rank < own_rank`); None otherwise (own rank stands even on a
    tie — a tie doesn't need to borrow credit from a dependent). `tier_rank`
    is the optional per-ticket intra-tier key; it is never inherited and never
    participates before `effective_rank`.
    """

    own_rank: float
    matched_label: str | None
    effective_rank: float
    elevated_by: str | None
    tier_rank: int | None


def _own_priority_rank(
    labels: list[str] | None, rank_map: dict[str, int]
) -> tuple[float, str | None]:
    """Ceiling priority-rank (ORG-078 §1/§3): the MINIMUM rank across
    `labels` that appear in `rank_map` (lower number = higher priority).
    Multi-label ties (two labels sharing the lowest rank) resolve to
    whichever is encountered first in `labels` order — display-only choice
    of `matched_label`, doesn't affect the numeric rank. No label present in
    `rank_map` (or `labels` empty/None) -> `(math.inf, None)`, the FIFO
    tail."""
    best_rank: float = math.inf
    best_label: str | None = None
    for label in labels or []:
        rank = rank_map.get(label)
        if rank is not None and rank < best_rank:
            best_rank = rank
            best_label = label
    return best_rank, best_label


def _effective_queue_ts(fields: dict) -> str:
    """ORG-078 §3 secondary sort key: `fields['retargeted_at']` (T-3020 BT2)
    if present, else `fields['created']`. Both are `_now_iso()`-shaped
    (`%Y-%m-%dT%H:%M:%SZ`), so plain string comparison sorts correctly with
    no datetime parsing needed."""
    return str(fields.get("retargeted_at") or fields.get("created") or "")


def _tier_rank(fields: dict) -> int | None:
    """Return a valid optional intra-tier rank; malformed legacy data is inert."""
    raw = fields.get("tier_rank")
    if isinstance(raw, bool) or raw is None:
        return None
    text = str(raw)
    if re.fullmatch(r"-?[0-9]+", text) is None:
        return None
    try:
        return int(text)
    except ValueError:
        # Python bounds decimal-string conversion to protect against hostile
        # input. Optional rank metadata beyond that limit is malformed and
        # must stay inert rather than taking down a read-only queue listing.
        return None


def _build_depends_on_graph(tickets_root: Path) -> dict[str, list[str]]:
    """Ticket-id-keyed depends_on adjacency: `{tid: [prereq_tid, ...]}`,
    restricted to ticket-kind targets. ORG-078's §Priority-inversion
    inherits via `depends_on` ONLY — not the `implements`/`verifies`/
    `supersedes` union ORG-065's acyclicity check uses — so this passes a
    single-field `edge_fields` restriction, and unwraps
    `ticket_lint.build_typed_edge_graph`'s `("ticket"|"obligation", id)`
    NodeId shape down to bare ticket ids (an `obligation`-kind depends_on
    target, or one pointing off a ticket entirely, is dropped — dependency
    inheritance only makes sense between tickets).

    Lazy-imports `tools.ticket_lint` — same circular-import guard
    `_reject_if_cycle` and `cmd_ready`'s deadlock lookup already use
    (`ticket_lint` imports FROM this module)."""
    from tools.ticket_lint import build_typed_edge_graph

    raw_graph = build_typed_edge_graph(tickets_root, edge_fields=("depends_on",))
    out: dict[str, list[str]] = {}
    for (src_kind, src_id), targets in raw_graph.items():
        if src_kind != "ticket":
            continue
        out[src_id] = [tgt_id for (tgt_kind, tgt_id) in targets if tgt_kind == "ticket"]
    return out


def _compute_priority_info(
    tickets: list["Ticket"],
    rank_map: dict[str, int],
    depends_on_graph: dict[str, list[str]],
) -> dict[str, _PriorityInfo]:
    """ORG-078 §3 own-rank + §Priority-inversion dependency-inheritance, in
    one pass over `tickets` (a single graph snapshot per `list` invocation —
    §Priority-inversion Implementation-shape).

    `effective_rank[Y] = min(own_rank[Y], min(effective_rank[X] for X in
    tickets whose depends_on contains Y))` — Y inherits the best rank of any
    ticket that (directly or transitively) depends on it. Computed via a
    Kahn's-algorithm topological walk over the reverse-depends_on adjacency:
    a ticket's dependents are always processed before the ticket itself (a
    node reaches in-degree 0 — ready to process — only once every ticket
    that depends on it has already been processed), so each node's
    `effective_rank` is only ever derived from already-finalized inputs.
    O(V+E) total — matches ORG-078's linear-time requirement; ORG-065 §3.6
    DAG-acyclicity guarantees termination. A residual cycle (shouldn't
    happen given §3.6, but a read-only `list` must never hang on a bad
    external invariant) leaves any unprocessed node at its own `own_rank` —
    fails soft.

    Returns `{tid: _PriorityInfo}` for every ticket in `tickets`. A
    `depends_on` target outside `tickets` (dangling ref, or a ticket outside
    this listing's node universe) contributes no inheritance and gets no
    entry of its own.
    """
    own_rank: dict[str, float] = {}
    matched_label: dict[str, str | None] = {}
    tier_rank: dict[str, int | None] = {}
    for t in tickets:
        tid = t.fields.get("id") or t.path.stem
        rank, label = _own_priority_rank(t.fields.get("labels"), rank_map)
        own_rank[tid] = rank
        matched_label[tid] = label
        tier_rank[tid] = _tier_rank(t.fields)

    # dependents_of[Y] = [X, ...] where X's depends_on contains Y (X depends
    # on Y). in_degree[Y] = len(dependents_of[Y]) = count of Y's not-yet-
    # processed dependents; Y becomes ready once every dependent is resolved.
    dependents_of: dict[str, list[str]] = {}
    in_degree: dict[str, int] = {tid: 0 for tid in own_rank}
    for tid, prereqs in depends_on_graph.items():
        if tid not in own_rank:
            continue
        for prereq in prereqs:
            if prereq not in own_rank:
                continue
            dependents_of.setdefault(prereq, []).append(tid)
            in_degree[prereq] += 1

    effective_rank: dict[str, float] = {}
    elevated_by: dict[str, str | None] = {}
    queue: list[str] = [tid for tid, deg in in_degree.items() if deg == 0]
    while queue:
        next_queue: list[str] = []
        for tid in queue:
            best_rank = own_rank[tid]
            best_source: str | None = None
            for dependent in dependents_of.get(tid, []):
                dep_rank = effective_rank[dependent]
                if dep_rank < best_rank:
                    best_rank = dep_rank
                    best_source = dependent
            effective_rank[tid] = best_rank
            elevated_by[tid] = best_source
            for prereq in depends_on_graph.get(tid, []):
                if prereq not in in_degree:
                    continue
                in_degree[prereq] -= 1
                if in_degree[prereq] == 0:
                    next_queue.append(prereq)
        queue = next_queue

    # Residual-cycle guard: never leave a node unresolved (never hang).
    for tid, rank in own_rank.items():
        if tid not in effective_rank:
            effective_rank[tid] = rank
            elevated_by[tid] = None

    return {
        tid: _PriorityInfo(
            own_rank=own_rank[tid],
            matched_label=matched_label[tid],
            effective_rank=effective_rank[tid],
            elevated_by=elevated_by[tid],
            tier_rank=tier_rank[tid],
        )
        for tid in own_rank
    }


def _record_priority_list_health(entry: dict) -> None:
    """Best-effort JSON-line append to `PRIORITY_LIST_HEALTH_PATH` (ORG-078
    §4 failure-mode telemetry + §Cadence-observation counters, T-3020 BT3).
    Never raises — a telemetry-write failure must not break `ticket.py
    list` (same fail-open posture as `closure_gate_health_monitor.py`'s
    `--record`)."""
    try:
        PRIORITY_LIST_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PRIORITY_LIST_HEALTH_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _priority_list_dead_labels(
    rank_map: dict[str, int], tickets: list["Ticket"]
) -> list[str]:
    """Labels that are keys of `rank_map` (i.e. referenced in
    rulebook/priority-list.md) but carried by ZERO of `tickets` — the
    "probably-dead label" detection named in ORG-078
    §Priority-list-drift-hygiene, narrowed for T-3020 BT4 to a pure
    zero-usage check (the ADR's richer high-volume + sample-matching
    heuristics landed later, T-3080; semantic-drift deferred per T-3424).

    `tickets` is expected to be the FULL corpus (inbox+active+done, e.g.
    `_iter_all_tickets()`) — a label used only on a done ticket still
    counts as used, per the brief's explicit tickets/{inbox,active,done}/
    scope. Sorted for deterministic output."""
    used: set[str] = set()
    for t in tickets:
        used.update(t.fields.get("labels") or [])
    return sorted(label for label in rank_map if label not in used)


def _priority_list_high_volume_labels(
    rank_map: dict[str, int], tickets: list["Ticket"]
) -> list[tuple[str, int]]:
    """Labels carried by MORE than `PRIORITY_LIST_HIGH_VOLUME_THRESHOLD`
    tickets across the full corpus (T-3080, ORG-078
    §Priority-list-drift-hygiene: "labels that are HIGH-VOLUME (may be
    over-broad)"). Same full-corpus scope as `_priority_list_dead_labels`.

    Returns `(label, count)` pairs sorted by label."""
    counts: dict[str, int] = {label: 0 for label in rank_map}
    for t in tickets:
        for label in t.fields.get("labels") or []:
            if label in counts:
                counts[label] += 1
    return sorted(
        (label, n) for label, n in counts.items()
        if n > PRIORITY_LIST_HIGH_VOLUME_THRESHOLD
    )


def _priority_list_label_samples(
    rank_map: dict[str, int], tickets: list["Ticket"]
) -> dict[str, list[tuple[str, str]]]:
    """For every priority-list label, up to `PRIORITY_LIST_SAMPLE_SIZE`
    `(ticket_id, title)` pairs of tickets carrying it (T-3080, ORG-078
    §Priority-list-drift-hygiene: "shows sample matching tickets per
    ... label ... so the editor confirms the label attaches to the
    intended tickets"). Sorted by ticket id and truncated (not randomly
    sampled) — the lowest ids are typically a label's founding tickets,
    the most useful confirmation sample; also keeps output deterministic.

    Every label in `rank_map` gets a key, even an empty list (zero
    matches — the dead-label case), so callers never need to guard a
    KeyError."""
    matches: dict[str, list[tuple[str, str]]] = {label: [] for label in rank_map}
    for t in tickets:
        tid = t.fields.get("id") or t.path.stem
        for label in t.fields.get("labels") or []:
            if label in matches:
                matches[label].append((tid, t.title))
    return {
        label: sorted(pairs)[:PRIORITY_LIST_SAMPLE_SIZE]
        for label, pairs in matches.items()
    }


PRIORITY_LIST_SEMANTIC_DRIFT_WINDOW_DAYS = 30
PRIORITY_LIST_SEMANTIC_DRIFT_ARC_THRESHOLD = 3


def _parent_chain_root(ticket_obj: "Ticket", by_id: dict[str, "Ticket"]) -> str:
    """Walk `ticket_obj.parent_ticket` chain until it hits a ticket with no
    parent (or an unresolvable parent id, e.g. a ticket referenced but not
    on disk). Returns the root ticket id.

    A ticket without a parent is its own root. Cycle-safe via a visited-set
    so a malformed self-referencing chain terminates on the first re-visit
    (returning the last non-cyclic id) rather than looping.
    """
    tid = ticket_obj.fields.get("id") or ticket_obj.path.stem
    visited: set[str] = set()
    current = ticket_obj
    while True:
        cur_id = current.fields.get("id") or current.path.stem
        if cur_id in visited:
            return cur_id
        visited.add(cur_id)
        parent_id = current.fields.get("parent_ticket")
        if not parent_id:
            return cur_id
        next_ticket = by_id.get(parent_id)
        if next_ticket is None:
            # Dangling parent ref (referenced but not on disk) — treat the
            # referenced id AS the root, so orphans with a named-but-missing
            # ancestor still cluster under that ancestor for arc-set purposes.
            return parent_id
        current = next_ticket
    # unreachable; keeps type-checkers happy
    return tid


def _priority_list_semantic_drift(
    rank_map: dict[str, int],
    tickets: list["Ticket"],
    *,
    window_days: int = PRIORITY_LIST_SEMANTIC_DRIFT_WINDOW_DAYS,
    arc_threshold: int = PRIORITY_LIST_SEMANTIC_DRIFT_ARC_THRESHOLD,
    now: datetime | None = None,
) -> list[tuple[str, int, list[str]]]:
    """T-3424 / ORG-078 §Priority-list-drift-hygiene fourth heuristic:
    novel-arc-attach event detector for priority-list labels.

    For each label in `rank_map`, split tickets carrying it into HISTORICAL
    (created more than `window_days` ago) vs RECENT (created within the
    window). Compute the historical parent-chain-root set. A RECENT ticket
    whose parent-chain-root is NOT in that set is a novel-arc-attach event —
    the label is being applied to a new arc it wasn't previously covering,
    which is the "suddenly appearing" signal the ADR names.

    Fire the warning ONLY when:
      - novel-arc-attach count >= 1, AND
      - historical arc-set size <= `arc_threshold`

    The threshold structurally excludes cross-cutting design labels (org-065,
    harness-controller, ticket-flow, org-013 — all empirically >=7 arcs) from
    firing, matching T-3424's decision.

    Returns `(label, historical_arc_count, novel_ticket_ids)` tuples. Sorted
    by label. Never raises; a ticket with an unparseable `created` is placed
    in HISTORICAL (conservative — treats it as pre-existing rather than
    novel; false-negative-safer than false-positive-noisier).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    by_id: dict[str, "Ticket"] = {}
    for t in tickets:
        tid = t.fields.get("id") or t.path.stem
        by_id[tid] = t

    # Group tickets by label into HISTORICAL vs RECENT.
    per_label_hist: dict[str, list["Ticket"]] = {label: [] for label in rank_map}
    per_label_recent: dict[str, list["Ticket"]] = {label: [] for label in rank_map}
    for t in tickets:
        created_raw = t.fields.get("created")
        try:
            created_dt = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            created_dt = None
        is_recent = created_dt is not None and created_dt >= cutoff
        for label in t.fields.get("labels") or []:
            if label not in rank_map:
                continue
            (per_label_recent if is_recent else per_label_hist)[label].append(t)

    findings: list[tuple[str, int, list[str]]] = []
    for label in sorted(rank_map):
        hist_tickets = per_label_hist[label]
        recent_tickets = per_label_recent[label]
        historical_arcs = {_parent_chain_root(t, by_id) for t in hist_tickets}
        if not historical_arcs:
            # Young label (T-6239): no history to drift from, so every recent
            # ticket would trivially count as "novel". Not a drift signal.
            continue
        if len(historical_arcs) > arc_threshold:
            continue
        novel_ids: list[str] = []
        for t in recent_tickets:
            if _parent_chain_root(t, by_id) not in historical_arcs:
                tid = t.fields.get("id") or t.path.stem
                novel_ids.append(tid)
        if novel_ids:
            findings.append((label, len(historical_arcs), sorted(novel_ids)))
    return findings


def _priority_list_validate_report() -> tuple[int, list[str]]:
    """ORG-078 §Priority-list-drift-hygiene validation (T-3020 BT4 dead-
    label check + T-3080 high-volume heuristic + T-3429 semantic-drift):
    parse `rulebook/priority-list.md`, then check for probably-dead,
    high-volume, and novel-arc-attach (semantic-drift) labels.

    Returns `(exit_code, lines)`:
      0  — clean parse, no warnings fired. `lines` is a single
           human-readable summary.
      1  — `PriorityListParseError` — `lines` is `[str(exc)]` (carries the
           parser's failure-class prefix, e.g. "sentinel-missing: ...").
      2  — clean parse, but >=1 warning (dead-label / high-volume /
           semantic-drift) fired — `lines` is one message per warning;
           a single label can appear more than once if it trips more than
           one heuristic.

    No printing, no sys.exit — callers (the CLI wrapper below and the
    pre-commit hook) own presentation and blocking-vs-warning semantics,
    which differ between the two (CLI surfaces exit 2 as a caller-visible
    warn-exit; the hook treats exit 2 as non-blocking). Sample-matching
    output (T-3080 ask #3) is NOT part of this report — it's an
    always-on informational surface, not a warn/ok signal, and is
    generated separately by the CLI wrapper below.

    Semantic-drift design (T-3424/T-3429): the T-3424 first-cut static
    parent_ticket-root-fanout implementation false-fired on cross-cutting
    labels. T-3429 landed the temporal-window design: split HISTORICAL vs
    RECENT by created-ts, fire only when historical arc-count is small
    (<= arc_threshold) AND a novel arc appears in the recent window. The
    threshold structurally excludes cross-cutting labels."""
    try:
        rank_map = priority_list_parse.parse_file()
    except priority_list_parse.PriorityListParseError as exc:
        return 1, [str(exc)]

    try:
        expected_broad = priority_list_parse.parse_file_expected_broad()
    except priority_list_parse.PriorityListParseError as exc:
        return 1, [str(exc)]
    # Labels broad by design (T-6239) skip the high-volume + semantic-drift
    # heuristics only; dead-label still sees the full rank_map.
    checked_map = {k: v for k, v in rank_map.items() if k not in expected_broad}

    tickets = _iter_all_tickets()
    warnings: list[str] = []

    warnings.extend(
        f"label {label!r} is referenced in rulebook/priority-list.md but "
        "carried by zero tickets across tickets/{inbox,active,done}/ "
        "(probably-dead label)"
        for label in _priority_list_dead_labels(rank_map, tickets)
    )
    warnings.extend(
        f"label {label!r} is listed under expected-broad in "
        "rulebook/priority-list.md but is not in any priority tier "
        "(stale exemption)"
        for label in sorted(expected_broad)
        if label not in rank_map
    )
    warnings.extend(
        f"label {label!r} is carried by {count} tickets across "
        "tickets/{inbox,active,done}/, above the "
        f"{PRIORITY_LIST_HIGH_VOLUME_THRESHOLD}-ticket high-volume "
        "threshold (may be over-broad)"
        for label, count in _priority_list_high_volume_labels(checked_map, tickets)
    )
    warnings.extend(
        f"label {label!r} has a narrow historical arc-set "
        f"({historical_arcs} arc(s), <= {PRIORITY_LIST_SEMANTIC_DRIFT_ARC_THRESHOLD}) "
        f"but attached in the last {PRIORITY_LIST_SEMANTIC_DRIFT_WINDOW_DAYS}d "
        f"to novel arc(s): {', '.join(novel_ids)} (semantic-drift — label may "
        "have shifted meaning; confirm it still attaches to the intended arc)"
        for label, historical_arcs, novel_ids in _priority_list_semantic_drift(
            checked_map, tickets
        )
    )

    if warnings:
        return 2, warnings
    return 0, [
        f"priority-list parses cleanly; {len(rank_map)} label(s), 0 warnings"
    ]


def _fmt_priority_list_samples(rank_map: dict[str, int], tickets: list["Ticket"]) -> list[str]:
    """Render the T-3080 sample-matching section: for every priority-list
    label (sorted), up to `PRIORITY_LIST_SAMPLE_SIZE` `id: title` lines, or
    an explicit "(no matching tickets)" marker for a dead label — the
    editor's "did I attach the right label" confirmation surface (ORG-078
    §Priority-list-drift-hygiene)."""
    samples = _priority_list_label_samples(rank_map, tickets)
    lines = ["sample matching tickets per label:"]
    for label in sorted(samples):
        lines.append(f"  {label}:")
        pairs = samples[label]
        if not pairs:
            lines.append("    (no matching tickets)")
        else:
            lines.extend(f"    {tid}: {title}" for tid, title in pairs)
    return lines


def cmd_priority_list_validate(args: argparse.Namespace) -> int:
    """`ticket.py priority-list validate` (ORG-078 §Priority-list-drift-hygiene,
    T-3020 BT4 + T-3080). Read-only. Exit 0 clean / 1 parse-fail / 2 parse-
    ok-with-warnings — see `_priority_list_validate_report`. When the parse
    succeeds (any exit code but 1), also prints the T-3080 sample-matching
    section to stdout — an always-on surface, independent of warn/ok."""
    code, lines = _priority_list_validate_report()
    prefix = {0: "ok", 1: "error", 2: "warning"}[code]
    stream = sys.stdout if code == 0 else sys.stderr
    for line in lines:
        print(f"{prefix}: {line}", file=stream)

    if code != 1:
        rank_map = priority_list_parse.parse_file()
        print("", file=sys.stdout)
        for line in _fmt_priority_list_samples(rank_map, _iter_all_tickets()):
            print(line, file=sys.stdout)
    return code


def _fmt_rank(rank: float) -> str:
    """Render a priority rank for human-facing output: `math.inf` -> `∞`
    (the FIFO tail), else the plain integer."""
    return "∞" if rank == math.inf else str(int(rank))


def _explain_priority_report(
    tid: str, info: _PriorityInfo, info_by_id: dict[str, _PriorityInfo]
) -> str:
    """Render the `--explain-priority T-NNNN` diagnostic (ORG-078
    §Priority-inversion + §Missing-label-detection: "doubles as an
    operator-facing sanity check"). No ADR-literal output-shape spec exists
    (the dispatch brief's "§5" citation didn't resolve to an actual
    section) — this format is an autonomous design call: prose over a
    table, since it's always exactly one ticket.

    Reports own_rank + matched_label, effective_rank, and — when
    `info.elevated_by` is set — the full inheritance chain (walks
    `elevated_by` through `info_by_id` until it bottoms out at the ticket
    whose own_rank actually supplied the value)."""
    lines = [f"--explain-priority {tid}"]
    if info.matched_label is not None:
        lines.append(
            f"  own_rank: {_fmt_rank(info.own_rank)} (matched label {info.matched_label!r})"
        )
    else:
        lines.append(f"  own_rank: {_fmt_rank(info.own_rank)} (no label in priority-list matched)")

    if info.elevated_by is None:
        lines.append(
            f"  effective_rank: {_fmt_rank(info.effective_rank)} "
            "(own rank stands — no dependent elevates it)"
        )
    else:
        chain = [tid]
        cur: str | None = info.elevated_by
        while cur is not None:
            chain.append(cur)
            cur_info = info_by_id.get(cur)
            cur = cur_info.elevated_by if cur_info else None
        source = chain[-1]
        source_rank = info_by_id[source].own_rank if source in info_by_id else info.effective_rank
        lines.append(
            f"  effective_rank: {_fmt_rank(info.effective_rank)} "
            f"(inherited via depends_on chain: {' <- '.join(chain)}; "
            f"source {source} own_rank {_fmt_rank(source_rank)})"
        )
    lines.append(
        f"  tier_rank: {info.tier_rank if info.tier_rank is not None else 'absent'} "
        "(intra-tier only; absent falls through to FIFO)"
    )
    lines.append(
        "  sort keys: (effective_rank, tier_rank, effective_queue_ts, ticket_id) ASC — "
        "ties broken by ticket_id (ORG-078 §3)"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ORG-088 Plane-A ticket VISIBILITY (T-3761) — the READ surface.
#
# A `role:<R>` pool ticket is visible to persona P iff R ∈ functional_roles(P)
# (the Plane-A predicate). This is PASSIVE visibility only: no delivery, no
# dispatch stamp, no mutex, no assignment (that is Plane-B / the executor, out of
# scope). role:orchestrator is thereby visible to EVERY orchestrator-tier member —
# the accepted primary case (HO-distributable work), not an edge.
#
# READ fail direction is DEGRADE-GRACEFUL, deliberately ASYMMETRIC to the WRITE
# surfaces (file/retarget/lint refuse fail-closed on the same condition). An
# unverifiable/malformed L3 membership source must not crash a read-only listing:
# eligibility degrades to the EMPTY set (role-pool tickets HIDDEN, persona-specific
# tickets still shown), so the surface under-shows but stays alive.
# ---------------------------------------------------------------------------


def _plane_a_eligible_roles(persona: str) -> frozenset[str]:
    """The functional role_ids *persona* is eligible to SEE role-pool tickets for.

    DEGRADE-GRACEFUL (the READ fail direction): an unverifiable/malformed L3
    membership source returns the empty set rather than raising, so a read-only
    listing hides role tickets but stays alive. Contrast _resolve_or_reject_target,
    the WRITE path, which REFUSES fail-closed on the identical condition."""
    if not persona:
        return frozenset()
    from tools.agent_role_map import functional_roles, PersonaRoleMembershipError

    try:
        return functional_roles(persona)
    except PersonaRoleMembershipError:
        return frozenset()


def _visible_to_target(ticket_target, filter_persona: str, eligible_roles: frozenset[str]) -> bool:
    """Plane-A visibility of a ticket to a `--target <filter_persona>` view.

    True iff the ticket's target EQUALS the filter persona (the pre-T-3761 exact-match
    semantics, preserved) OR the ticket is pooled to a functional role the persona is
    eligible for (`target: role:<R>` with R ∈ *eligible_roles*). role:orchestrator is
    thereby visible to every orchestrator-tier member. An explicit `--target role:<R>`
    operator view matches only via the exact-equality arm (eligible_roles is empty for
    a non-persona filter), so it shows exactly that role's pool."""
    if ticket_target == filter_persona:
        return True
    if isinstance(ticket_target, str) and _ROLE_TARGET_RE.fullmatch(ticket_target):
        return ticket_target.split(":", 1)[1] in eligible_roles
    return False


def cmd_list(args: argparse.Namespace) -> int:
    all_tickets = _iter_all_tickets()
    # T-2720: unblocks is computed over the FULL corpus, before any of the
    # filters below apply — a --target/--state/--label filter narrows which
    # ROWS print, not what a shown ticket unblocks (a dependent outside the
    # filter still counts).
    unblocks = compute_unblocks(all_tickets) if args.show_unblocks else {}

    sort_mode: str = getattr(args, "sort", "priority") or "priority"
    explain_tid: str | None = getattr(args, "explain_priority", None)

    # T-3020 BT3 / ORG-078 §3/§4: priority_info is only computed when needed
    # — `--sort created|retargeted` bypass the priority-list parse entirely
    # (ADR §4), so neither the parse nor the telemetry write fires for them.
    priority_info: dict[str, _PriorityInfo] = {}
    if sort_mode == "priority" or explain_tid:
        rank_map: dict[str, int] = {}
        parse_ok = True
        fail_class: str | None = None
        try:
            rank_map = priority_list_parse.parse_file()
        except priority_list_parse.PriorityListParseError as exc:
            parse_ok = False
            fail_class = str(exc).split(":", 1)[0]
            print(
                f"warning: priority-list parse failed ({fail_class}); "
                f"falling back to created-ASC FIFO ({exc})",
                file=sys.stderr,
            )
            sort_mode = "created"

        if parse_ok:
            depends_on_graph = _build_depends_on_graph(TICKETS_ROOT)
            priority_info = _compute_priority_info(all_tickets, rank_map, depends_on_graph)

        n_ranked = sum(1 for i in priority_info.values() if i.effective_rank != math.inf)
        n_labels_seen = sum(1 for i in priority_info.values() if i.own_rank != math.inf)
        n_promotions = sum(1 for i in priority_info.values() if i.elevated_by is not None)
        _record_priority_list_health(
            {
                "ts": _now_iso(),
                "parse_ok": parse_ok,
                "parse_fail_class": fail_class,
                "n_tickets_ranked": n_ranked,
                "n_tickets_tail_fifo": len(all_tickets) - n_ranked,
                "n_dep_inherit_promotions": n_promotions,
                "n_ticket_labels_seen": n_labels_seen,
            }
        )

    if explain_tid:
        if not any((t.fields.get("id") or t.path.stem) == explain_tid for t in all_tickets):
            print(f"(no such ticket: {explain_tid})", file=sys.stderr)
            return 1
        info = priority_info.get(explain_tid)
        if info is None:
            # Parse failed — priority_info is empty; nothing to explain.
            print(
                f"(priority-list unavailable; {explain_tid}'s rank cannot be "
                "explained — see stderr warning above)"
            )
            return 0
        print(_explain_priority_report(explain_tid, info, priority_info))
        return 0

    tickets = all_tickets
    if args.state:
        tickets = [t for t in tickets if t.fields.get("state") == args.state]
    if args.target:
        # T-3761 / ORG-088 Plane-A: a --target <persona> view also surfaces the
        # role:<R> pool tickets that persona is eligible for (R ∈ functional_roles),
        # alongside its persona-specific tickets. Degrade-graceful on an unverifiable
        # membership source (role tickets hidden, listing stays alive).
        eligible = _plane_a_eligible_roles(args.target)
        tickets = [
            t for t in tickets
            if _visible_to_target(t.fields.get("target"), args.target, eligible)
        ]
    if args.label:
        tickets = [
            t for t in tickets if args.label in (t.fields.get("labels") or [])
        ]

    if not tickets:
        print("(no tickets)")
        return 0

    def _tid(t: "Ticket") -> str:
        return t.fields.get("id") or t.path.stem

    if sort_mode == "priority":
        def _priority_key(t: "Ticket"):
            info = priority_info.get(_tid(t))
            rank = info.effective_rank if info else math.inf
            tier_rank = info.tier_rank if info and info.tier_rank is not None else math.inf
            return (rank, tier_rank, _effective_queue_ts(t.fields), _tid(t))

        tickets = sorted(tickets, key=_priority_key)
    elif sort_mode == "retargeted":
        tickets = sorted(tickets, key=lambda t: (_effective_queue_ts(t.fields), _tid(t)))
    else:  # "created" — either explicit --sort created, or the parse-failure fallback
        tickets = sorted(tickets, key=lambda t: (t.fields.get("created", ""), _tid(t)))

    rows = []
    for t in tickets:
        rows.append(
            (
                t.fields.get("id", "?"),
                t.fields.get("state", "?"),
                t.fields.get("requester", "?"),
                t.fields.get("target", "?"),
                _format_age(t.fields.get("created", "")),
                t.title + (" [security-pending]" if _security_gate.is_pending(t.fields) else ""),
            )
        )

    # Column widths
    w_id = max(len(r[0]) for r in rows)
    w_state = max(len(r[1]) for r in rows)
    w_req = max(len(r[2]) for r in rows)
    w_tgt = max(len(r[3]) for r in rows)
    w_age = max(len(r[4]) for r in rows)

    for r in rows:
        line = (
            f"{r[0]:<{w_id}}  {r[1]:<{w_state}}  "
            f"{r[2]:<{w_req}} → {r[3]:<{w_tgt}}  "
            f"{r[4]:>{w_age}}  {r[5]}"
        )
        deps = unblocks.get(r[0]) or []
        if deps:
            line += f"  unblocks: {', '.join(deps)}"
        print(line)
    return 0


def _not_before_passed(raw: str, now: datetime) -> bool:
    """True if a `not_before` calendar gate has passed (or is unparseable).

    An absent gate is handled by the caller. An unparseable value is treated as
    passed (do not silently hide a ticket on a malformed gate — surface it so the
    bad value gets noticed). Per ORG-019 not_before semantics.
    """
    try:
        nb = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    if nb.tzinfo is None:
        nb = nb.replace(tzinfo=timezone.utc)
    return nb <= now


def cmd_ready(args: argparse.Namespace) -> int:
    """List currently-unblocked tickets (the human-facing `bd ready` analog).

    A ticket is "ready" when it is in `inbox/` or `active/`, has no unresolved
    `depends_on` (dep gate clear), and its `not_before` calendar gate has passed
    or is absent (ORG-019 block semantics). Optional `--target` filter. Read-only.
    """
    now = datetime.now(timezone.utc)
    # T-3540: caller may hide tickets carrying certain labels — e.g. an
    # autonomous scan asking "what can I do solo right now" excludes
    # `interactive-required` (structurally needs a supervised/human session,
    # so it can never be picked up autonomously). Read-only, additive; absent
    # → default set() → behaviour unchanged.
    exclude_labels = set(getattr(args, "exclude_label", None) or [])
    tickets = _iter_all_tickets()
    # T-2720: unblocks over the full unfiltered corpus — see cmd_list.
    unblocks = compute_unblocks(tickets) if args.show_unblocks else {}
    # T-3761 / ORG-088 Plane-A: same visibility predicate as cmd_list — a
    # --target <persona> ready view also surfaces the persona's eligible role:<R>
    # pool tickets. Computed once; degrade-graceful on an unverifiable source.
    eligible = _plane_a_eligible_roles(args.target) if args.target else frozenset()
    ready = []
    for t in tickets:
        if t.fields.get("state") not in ("inbox", "active"):
            continue
        if args.target and not _visible_to_target(t.fields.get("target"), args.target, eligible):
            continue
        if t.fields.get("depends_on"):  # non-empty dep gate → blocked
            continue
        if _security_gate.is_pending(t.fields):  # T-5678: awaiting HO security accept
            continue
        nb = t.fields.get("not_before")
        if nb and not _not_before_passed(nb, now):  # future calendar gate → blocked
            continue
        if exclude_labels & set(t.fields.get("labels") or []):  # T-3540 label filter
            continue
        ready.append(t)

    if not ready:
        # T-2220: "(no ready tickets)" is AMBIGUOUS between "nothing to do, rest"
        # and "your queue is deadlocked" — and that ambiguity cost specialist an arc.
        # A three-way depends_on deadlock (T-2107/T-2160, itself a recurrence of
        # T-2106/T-2107) made `ready --target specialist` return nothing, which reads as
        # correctly-gated. Specialist said it would have sat on it.
        #
        # So an empty result now states WHY it is empty. Same defect class as an
        # empty grep whose zero could not have been otherwise: the output was
        # identical in the healthy and the wedged case, so it could not
        # discriminate the only two states a reader cares about.
        scope = f" for target '{args.target}'" if args.target else ""
        dep_blocked, date_blocked, label_excluded = [], [], []
        for t in tickets:
            if t.fields.get("state") not in ("inbox", "active"):
                continue
            # T-3761: same Plane-A visibility as the ready scan above, so the
            # empty-reason diagnostic accounts for exactly the tickets the persona
            # can see (incl. eligible role-pool tickets).
            if args.target and not _visible_to_target(t.fields.get("target"), args.target, eligible):
                continue
            if t.fields.get("depends_on"):
                dep_blocked.append(t.fields.get("id", "?"))
                continue
            nb = t.fields.get("not_before")
            if nb and not _not_before_passed(nb, now):
                date_blocked.append(t.fields.get("id", "?"))
                continue
            # T-3540: a ticket the caller's --exclude-label hid is otherwise-ready,
            # NOT gated — account for it separately so "genuinely EMPTY" stays honest.
            if exclude_labels & set(t.fields.get("labels") or []):
                label_excluded.append(t.fields.get("id", "?"))
        if not dep_blocked and not date_blocked and not label_excluded:
            print(f"(no ready tickets{scope} — and none blocked either: the queue "
                  f"is genuinely EMPTY, not gated)")
            return 0
        parts = []
        if dep_blocked:
            parts.append(f"{len(dep_blocked)} dep-gated")
        if date_blocked:
            parts.append(f"{len(date_blocked)} date-gated")
        if label_excluded:
            parts.append(f"{len(label_excluded)} label-excluded")
        print(f"(no ready tickets{scope} — but {', '.join(parts)}, so the queue is NOT empty)")
        if dep_blocked:
            print(f"  dep-gated:  {', '.join(sorted(dep_blocked))}")
        if date_blocked:
            print(f"  date-gated: {', '.join(sorted(date_blocked))}")
        if label_excluded:
            print(f"  label-excluded: {', '.join(sorted(label_excluded))}")
        # A cycle among those is a DEADLOCK, not a gate — it will never clear on
        # its own, so it is the one case that needs a human now. Lazy import:
        # ticket_lint imports FROM this module, so a top-level import would be
        # circular. Fail-soft — a read-only listing must never fail because its
        # advisory could not be computed.
        try:
            from tools.ticket_lint import detect_dep_graph_issues
            blocked_set = set(dep_blocked)
            cycles = [f for f in detect_dep_graph_issues(TICKETS_ROOT)
                      if f.kind == "cycle"]
            hits = [f for f in cycles
                    if not blocked_set or any(t in f.detail for t in blocked_set)]
            for f in hits:
                print(f"  ⛔ DEADLOCK — {f.detail}")
            if hits:
                print("     A cycle never clears on its own. Break it by correcting "
                      "the wrong edge (`ticket.py block <id> --clear-deps`).")
        except Exception:
            pass
        return 0

    rows = [
        (
            t.fields.get("id", "?"),
            t.fields.get("state", "?"),
            t.fields.get("requester", "?"),
            t.fields.get("target", "?"),
            _format_age(t.fields.get("created", "")),
            t.title,
        )
        for t in ready
    ]
    w_id = max(len(r[0]) for r in rows)
    w_state = max(len(r[1]) for r in rows)
    w_req = max(len(r[2]) for r in rows)
    w_tgt = max(len(r[3]) for r in rows)
    w_age = max(len(r[4]) for r in rows)
    for r in rows:
        line = (
            f"{r[0]:<{w_id}}  {r[1]:<{w_state}}  "
            f"{r[2]:<{w_req}} → {r[3]:<{w_tgt}}  "
            f"{r[4]:>{w_age}}  {r[5]}"
        )
        deps = unblocks.get(r[0]) or []
        if deps:
            line += f"  unblocks: {', '.join(deps)}"
        print(line)
    return 0


def _ticket_brief(t: "Ticket", unblocks: dict[str, list[str]] | None = None) -> dict:
    """Compact JSON-serializable view of a ticket for `prime` output."""
    return {
        "id": t.fields.get("id"),
        "title": t.title,
        "state": t.fields.get("state"),
        "requester": t.fields.get("requester"),
        "target": t.fields.get("target"),
        "labels": t.fields.get("labels") or [],
        "depends_on": t.fields.get("depends_on") or [],
        "not_before": t.fields.get("not_before"),
        "created": t.fields.get("created"),
        # T-2720: derived, unconditional on this JSON surface (no noise cost
        # here — see compute_unblocks). Empty list, never absent.
        "unblocks": (unblocks or {}).get(t.fields.get("id") or t.path.stem, []),
    }


def cmd_prime(args: argparse.Namespace) -> int:
    """Emit project ticket-state as JSON for a fresh-agent bootstrap (bd-prime analog).

    Categories (each a list of compact ticket briefs):
      - ready:         inbox/active, depends_on empty, not_before passed
      - blocked:       inbox/active, blocked by a dep gate or a future not_before
      - active:        state == active
      - recently_done: most-recent N done tickets (by id desc; --done-limit, default 15)

    Optional `--target` filters ready/blocked/active (not recently_done). Read-only.

    T-3761 / ORG-088 Plane-A: prime is the respawn-bootstrap surface and EXTENDS the
    SAME visibility predicate as list/ready — a respawned persona's bootstrap includes
    the role:<R> pool tickets it is a member of (R ∈ functional_roles). Keeping prime
    and list consistent avoids a split where list shows a persona its role-pool work
    but prime hides it on respawn. Degrade-graceful on an unverifiable membership source.
    """
    now = datetime.now(timezone.utc)
    tickets = _iter_all_tickets()
    # T-2720: over the full corpus, incl. done — a dependent's state doesn't
    # change whether the depended-on ticket unblocks it.
    unblocks = compute_unblocks(tickets)

    eligible = _plane_a_eligible_roles(args.target) if args.target else frozenset()

    def _match_target(t):
        if not args.target:
            return True
        return _visible_to_target(t.fields.get("target"), args.target, eligible)

    ready, blocked, active = [], [], []
    sec_pending = []  # T-5678
    done = []
    for t in tickets:
        state = t.fields.get("state")
        if state == "done":
            done.append(t)
            continue
        if state not in ("inbox", "active"):
            continue
        if state == "active" and _match_target(t):
            active.append(t)
        if not _match_target(t):
            continue
        if _security_gate.is_pending(t.fields):
            sec_pending.append(t)
            continue
        nb = t.fields.get("not_before")
        is_dep_gated = bool(t.fields.get("depends_on"))
        is_cal_gated = bool(nb) and not _not_before_passed(nb, now)
        if is_dep_gated or is_cal_gated:
            blocked.append(t)
        else:
            ready.append(t)

    done_sorted = sorted(done, key=lambda t: t.fields.get("id") or "", reverse=True)
    recently_done = done_sorted[: max(0, args.done_limit)]

    payload = {
        "ready": [_ticket_brief(t, unblocks) for t in ready],
        "blocked": [_ticket_brief(t, unblocks) for t in blocked],
        "active": [_ticket_brief(t, unblocks) for t in active],
        "security_pending": [_ticket_brief(t, unblocks) for t in sec_pending],
        "recently_done": [_ticket_brief(t, unblocks) for t in recently_done],
        "counts": {
            "ready": len(ready),
            "blocked": len(blocked),
            "active": len(active),
            "security_pending": len(sec_pending),
            "done_total": len(done),
        },
    }
    print(json.dumps(payload, indent=2))
    return 0


# ---------------------------------------------------------------------------
# argparse wiring.
# ---------------------------------------------------------------------------


class _NoRepeatStore(argparse.Action):
    """A single-valued option given TWICE is an error, not last-wins (T-2315).

    argparse's default `store` keeps the LAST value and drops the rest with no
    warning and exit 0. That is the actual defect behind T-2302 — specialist's
    narrowing, which I then failed to follow one step: allow_abbrev=False closed
    the route (`--label` as a prefix of `--labels`) and left the defect intact on
    the canonical spelling. Measured after that fix:

        --labels mailbox --labels reliability   -> 'reliability'   silent loss
        --depends-on T-1111 --depends-on T-2222 -> 'T-2222'        A GATE DROPPED
        --serves P1 --serves P7                 -> 'P7'            silent loss

    All 15 value-taking options on `file` were plain stores, so the loss was
    reachable by anyone who never abbreviated anything — and the abbreviation fix
    made one case WORSE, by pointing readers at the canonical spelling as if it
    were safe.

    The depends-on case is the severe one: a dropped dependency silently
    UN-GATES a ticket, which is a correctness defect in the queue rather than
    lost metadata.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        seen = getattr(namespace, "_ticket_seen_opts", None)
        if seen is None:
            seen = set()
            setattr(namespace, "_ticket_seen_opts", seen)
        if self.dest in seen:
            # T-2315 follow-up (specialist): this string is printed by EVERY
            # subcommand, so it must not assert anything true of only one. The
            # first version ended "...and how a --depends-on repeat would
            # un-gate a ticket. Pass ONE value" — printed verbatim on `block`,
            # where --depends-on is an append action and repeating it is the
            # CORRECT idiom. So the error argued the reader out of the right
            # usage, on the subcommand where it is right. An error that fires
            # correctly and then misdirects is the T-2310/T-2290 shape, and I
            # shipped it into the fix for the previous instance.
            parser.error(
                f"{option_string} given more than once, and it stores a SINGLE value — "
                f"argparse would keep only the LAST and drop the rest silently (exit 0, "
                f"no warning). That is how T-2243/T-2295/T-2299 lost labels. Pass one "
                f"{option_string} (comma-separated if it takes a list). Options declared "
                f"action='append' — e.g. `block --depends-on` — are unaffected and may "
                f"legitimately repeat."
            )
        seen.add(self.dest)
        setattr(namespace, self.dest, values)


class _NoAbbrevParser(argparse.ArgumentParser):
    """ArgumentParser with prefix-abbreviation OFF (T-2302, specialist).

    argparse's allow_abbrev defaults True, so ANY unambiguous prefix of a long
    flag is silently accepted. That turned `file --label a --label b` into a
    single-valued `--labels` assignment with last-wins semantics: exit 0, no
    warning, nothing echoed, three of four labels GONE.

    Measured: specialist filed T-2295 ("do not leave a well-formed maildir at the
    old hub") with four labels and it stored one. The dropped
    `post-office-rename` is precisely the label someone sweeps for BEFORE
    performing that cut, so the ticket existed and was unfindable at the one
    moment it exists to be found. Labels are the DISCOVERY surface; losing them
    is not cosmetic. Verified in git history: at filing (2e9c233e4) the
    frontmatter read `labels: [failure-presents-as-absence]` — the LAST value
    passed, exactly as last-wins predicts.

    Wider than labels: every long flag on every subcommand was silently
    abbreviable, so any near-miss typo that happened to be a unique prefix was
    accepted. Off now. `file --label` is a hard error, while `list --label`
    keeps working because it is a REAL flag there — that collision is precisely
    what made the silent case so easy to hit.

    Checked before landing: no in-tree caller relies on an abbreviation.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def add_argument(self, *args, **kwargs):
        """Default value-taking long options to the no-repeat store (T-2315).

        Applied here rather than at ~40 call sites so a NEW flag inherits the
        protection instead of having to remember it. Explicit actions
        (store_true, append, help, version) are left alone, as are positionals.
        """
        is_option = bool(args) and str(args[0]).startswith("-")
        if is_option and "action" not in kwargs and kwargs.get("nargs") is None:
            kwargs["action"] = _NoRepeatStore
        return super().add_argument(*args, **kwargs)


def _build_parser() -> argparse.ArgumentParser:
    actor_help = (
        "Actor identity for the transition-log entry comes from "
        "$PANTHEON_ACTOR if set, else the OS username (getpass.getuser())."
    )
    p = _NoAbbrevParser(
        prog="ticket.py",
        description=(
            "Pantheon ticket lifecycle CLI. " + actor_help
        ),
        # T-4496: wrappers key on the exit code, so it is part of --help.
        epilog=(
            "Exit codes: "
            f"0 = ok. "
            f"{EXIT_FAULT} = fault: ticket.py or its environment failed (git/IO, a "
            "guard, a non-main checkout, a corrupt ticket file) — not your request. "
            "2 = usage error (argparse). "
            f"{EXIT_REFUSAL} = refusal: your request was invalid and was declined "
            "(bad flag value, unknown target/requester/ticket, lineage or appetite "
            "gate, state mismatch). A stale-view conflict (another writer moved the "
            "ticket first) is a refusal too: re-read and retry. The `error: ...` "
            f"stderr line is the same for {EXIT_FAULT} and {EXIT_REFUSAL}; only the "
            "code differs."
        ),
    )
    # parser_class propagates the no-abbrev rule to EVERY subcommand;
    # setting it on the top-level parser alone would not (T-2302).
    sub = p.add_subparsers(dest="cmd", required=True,
                           parser_class=_NoAbbrevParser)

    f = sub.add_parser("file", help="file a new ticket in inbox/")
    f.add_argument(
        "--requester",
        required=True,
        help="who is filing the ticket. Canonical: persona name (orchestrator/specialist/recovery/operator). "
             "Role IDs (orchestrator/head_research_orchestrator, legacy specialist) accepted as aliases — "
             "normalized to persona name on write (T-0492). A fleet OS account name "
             "(e.g. claude-research) is accepted too and normalized to its persona. "
             "Any other value is refused (T-4499).",
    )
    f.add_argument(
        "--threat-chain", dest="threat_chain", default=None,
        help="T-5678 / ORG-105 s4: REQUIRED for a security-shaped ticket (security-ish "
             "label or title/ask keyword). Form: "
             "'foothold=<F>;impact=<I>;path=<how F reaches I>' with F in "
             f"{{{','.join(_security_gate.FOOTHOLDS)}}} and I in "
             f"{{{','.join(_security_gate.ASSETS)}}}. Files land security_gate: pending "
             "until HO runs security-accept.",
    )
    f.add_argument(
        "--path-justification", dest="path_justification", default=None,
        help="T-5678: required with --threat-chain when the ORG-105 s7 matrix marks the "
             "foothold x asset cell with an em dash (no realistic path): why the path is real.",
    )
    f.add_argument(
        "--automated-filer", dest="automated_filer", default=None, metavar="NAME",
        help="T-5678: a non-interactive filer names itself. A security-shaped filing with "
             "no chain is then NOT refused: it lands security_gate: pending + "
             "threat_unclassified (HO supplies the chain at security-accept). Interactive "
             "callers must NOT pass this.",
    )
    f.add_argument(
        "--not-security", dest="not_security", default=None,
        help="T-5678: reason this keyword-only hit is not a security finding. Refused "
             "when the ticket carries a security label.",
    )
    f.add_argument(
        "--target",
        required=True,
        help="who should action the ticket: a role pool `role:<R>` (e.g. "
             "role:engineering_orchestrator) or `operator`. Persona targeting is removed "
             "(ORG-088, T-5239): a persona / alias / bare role-id is REFUSED with the role to use.",
    )
    f.add_argument("--title", required=True)
    f.add_argument("--labels", default=None, help="comma-separated label list")
    f.add_argument("--depends-on", default=None,
                   help="comma-separated ticket ids; each must be T-NNNN or the request "
                        "is refused (T-4499)")
    f.add_argument(
        "--parent-ticket",
        default=None,
        help="parent ticket id (T-NNNN format); links this ticket as a child for lineage tracking",
    )
    f.add_argument(
        "--project-id",
        default=None,
        metavar="STABLE-ID|null",
        help="T-3168/ORG-054 stable project membership. Use literal 'null' for "
             "goal/project-formation roots or goal-neutral maintenance; omission "
             "is retained only for historic compatibility.",
    )
    f.add_argument(
        "--nearest-intent-ref",
        dest="nearest_intent_ref",
        default=None,
        metavar="ticket:T-NNNN|project:ID|goal:KEY|maintenance:ASSET",
        help="T-3168/ORG-054 typed nearest-purpose edge. Goal-neutral maintenance "
             "must name the maintained asset with maintenance:<asset-ref>. "
             "File operator-directed one-offs (ad-hoc the operator asks) with "
             "--nearest-intent-ref ticket:T-5677 (the operational-root ticket, "
             "T-3332 (b)) plus a descriptive --serves (which requires --appetite, "
             "per ORG-089): it traces them to a named non-goal root without a "
             "false goal ladder, and they are still bets.",
    )
    # T-2841 / ORG-065 BT1: typed-edge fields (warn-only model layer). Each is
    # a comma-separated list of targets — a ticket id (T-NNNN) or an ADR
    # obligation/clause id (ADR-NNN#E<k> / ORG-NNN#E<k>.C<m>). Set only at
    # file-time in this phase (mirrors --parent-ticket's set-once-at-creation
    # precedent); a post-creation editor is a natural follow-up once real
    # usage patterns emerge (kept out of scope here — P7).
    f.add_argument(
        "--implements", default=None,
        help="comma-separated targets (T-NNNN or ADR-NNN#E<k>[.C<m>]) this "
             "ticket implements — ticket -> ADR obligation, or -> parent work",
    )
    f.add_argument(
        "--verifies", default=None,
        help="comma-separated targets (T-NNNN or ADR-NNN#E<k>[.C<m>]) this "
             "ticket's evidence verifies (satisfies) — ticket -> ADR obligation. "
             "A T-NNNN target is a BT1 simplification (stands in for a real "
             "commit/test/evidence receipt, which doesn't have a node identity "
             "yet) — see _EDGE_TARGET_RE's docstring before treating it as "
             "closure-validation-ready evidence.",
    )
    f.add_argument(
        "--supersedes", default=None,
        help="comma-separated targets (T-NNNN or ADR-NNN#E<k>[.C<m>]) this "
             "ticket replaces",
    )
    f.add_argument(
        "--informs", default=None,
        help="comma-separated targets (T-NNNN or ADR-NNN#E<k>[.C<m>]) this "
             "ticket advises — NEVER gates, acyclicity-exempt (ORG-065 §3.6)",
    )
    f.add_argument(
        "--ratifies", default=None,
        help="comma-separated targets (T-NNNN or ADR-NNN#E<k>[.C<m>]) this "
             "ticket ratifies — DISTINCT from --implements (T-3476 / ORG-065 "
             "Call 1): a ratifies edge alone never satisfies an implements-"
             "obligation. Whole-ADR anchoring uses the reserved element id "
             "#E0 (T-3205 policy).",
    )
    f.add_argument("--stale-after", default=None, help="e.g. 4h, 2d")
    f.add_argument(
        "--not-before",
        default=None,
        help="earliest dispatch date/time: YYYY-MM-DD (interpreted as 00:00:00Z) "
             "or YYYY-MM-DDTHH:MM:SSZ",
    )
    f.add_argument(
        "--not-before-anchor",
        dest="not_before_anchor",
        default=None,
        metavar="operator:REF|event:KIND:param",
        help="T-2833: REQUIRED with --not-before. Anchors the date gate to the operator "
             "OR a real-world blocker: operator:<T-NNNN|tg:MSGID> or "
             "event:<cert-expiry|key-expiry|cadence|window>:<param>. No free-text.",
    )
    f.add_argument(
        "--ratifiable-file",
        dest="ratifiable_file",
        action="append",
        default=None,
        metavar="REPO:PATH",
        help="'<repo>:<path>' entry for a file being proposed for ratification "
             "(e.g. pantheon-config:sop/standard/foo.md). Repeatable for multi-file "
             "ratification. repo must be in the allowlist; path must be a clean "
             "relative path with no leading '/' or './', no '..' segments, no "
             "empty path components.",
    )
    f.add_argument(
        "--intent",
        default=None,
        choices=INTENT_VOCAB,
        help="T-0465: disambiguate target=operator queue. action (operator action "
             "request; default), ratify (with --ratifiable-file; ORG-015 path), or "
             "noodle (design-noodle, no urgent action). target=operator-only field; "
             "ignored / refused for other targets. Auto-default when target=operator: "
             "'ratify' if --ratifiable-file given else 'action'.",
    )
    f.add_argument("--ask", default=None, help="optional inline body for ## Ask")
    f.add_argument("--context", default=None, help="optional inline body for ## Context")
    f.add_argument(
        "--repair-fields-json", dest="repair_fields_json", default=None, metavar="PATH",
        help="T-4707 / ORG-076 RFC-004 section 3(a): JSON object of runtime repair-ticket "
             "attributes (fault_id, episode_id, domain_key, subsystem, detector, verdict, "
             "repair_route required; closed allowlist). Refuses if a COMMITTED ticket already "
             "carries the episode_id. Emitted only by the ORG-076 episode store.",
    )
    f.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                   help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    # T-0852: ratify file-time impl-followup gate. A target=operator + intent=ratify
    # file must NOT dead-end — it must carry a downstream implementation ticket, so
    # the operator's later ratify cascade-unblocks real work. Exactly one of --spawn-impl,
    # --impl-existing (T-5291) or --terminal is REQUIRED on a ratify file; refused
    # on non-ratify files.
    f.add_argument(
        "--spawn-impl", dest="spawn_impl", action="store_true", default=False,
        help="T-0852 (ratify-only): atomically spawn an implementation child ticket "
             "(depends_on this ratify, so it cascade-unblocks when the operator ratifies). "
             "Requires --impl-title. Mutually exclusive with --terminal / --impl-existing.",
    )
    f.add_argument("--impl-title", dest="impl_title", default=None,
                   help="T-0852: title for the --spawn-impl child ticket")
    f.add_argument("--impl-ask", dest="impl_ask", default=None,
                   help="T-0852: optional ## Ask body for the --spawn-impl child "
                        "(defaults to a stub pointing at the ratified artifact)")
    f.add_argument("--impl-target", dest="impl_target", default=None,
                   help="T-0852: target for the --spawn-impl child (default: this "
                        "ratify's requester — the work routes back to who asked)")
    f.add_argument(
        "--terminal", dest="terminal", action="store_true", default=False,
        help="T-0852 (ratify-only): explicitly assert this ratification needs NO "
             "implementation follow-up (rare). Tags the ticket 'ratify-terminal' "
             "for audit. Mutually exclusive with --spawn-impl / --impl-existing.",
    )
    f.add_argument(
        "--impl-existing", dest="impl_existing", default=None, metavar="T-NNNN",
        help="T-5291 (ratify-only): the implementation ticket ALREADY exists — add "
             "this ratify to its depends_on (same atomic commit) instead of spawning "
             "a duplicate. Must be an open (inbox/active) ticket. Mutually exclusive "
             "with --spawn-impl / --terminal; refuses --impl-title/--impl-ask/--impl-target.",
    )
    f.add_argument(
        "--serves",
        default=None,
        help="T-1103 / ORG-034: arc-intent declaration — a short phrase naming "
             "what this ticket serves (e.g. a goals.md key or a free-text arc "
             "descriptor). Also the goal-edge in the T-3328 lineage DAG: this "
             "is how a ticket traces to a rulebook/goals.md north star. "
             "'goal-neutral-maintenance' is a valid value for housekeeping "
             "tickets. 'operator-ad-hoc' is LEGACY: file ad-hoc the operator asks "
             "with --nearest-intent-ref ticket:T-5677 (the operational-root "
             "ticket, T-3332 (b)) plus a descriptive --serves (which requires "
             "--appetite, per ORG-089). Satisfies the T-3328 "
             "file-time lineage guard (see --orphan below for the alternative). "
             "ORG-089 APPETITE GATE (T-4494): any --serves value other than "
             "'goal-neutral-maintenance' — 'operator-ad-hoc' included — REQUIRES "
             "--appetite <1-10>, or the filing is refused (exit 3). Exempt (the "
             "medium default 5 is stamped instead): 'goal-neutral-maintenance', "
             "a hygiene label, or --nearest-intent-ref maintenance:<asset>.",
    )
    f.add_argument(
        "--retest-status",
        dest="retest_status",
        default=None,
        choices=RETEST_STATUS_VOCAB,
        help="T-2613 / ORG-038: S3* independent-re-test-to-close structural gate. "
             "Optional; set by s3star_audit_runner.py's finding filer to 'pending' "
             "at file time. Not for general ticket-filing use.",
    )
    f.add_argument(
        "--orphan",
        default=None,
        metavar="JUSTIFICATION",
        help="T-3328: file this ticket with NO lineage edge (parent-ticket/"
             "depends-on/serves/implements/verifies/supersedes/informs/"
             "ratifies) — "
             "rare, for genuine root-tier work with no natural connection to "
             "a goal, ADR, or parent ticket. Requires a >=20-char "
             "justification, stored as orphan_reason: in frontmatter for "
             "audit. Prefer a real edge when one exists.",
    )
    f.add_argument(
        "--no-file",
        dest="no_file",
        default=None,
        metavar="REASON",
        help="T-5014: a target=operator ticket must attach the file it is about "
             "(--ratifiable-file <repo>:<path>) OR assert it has none with a "
             ">=20-char reason, stored as no_file_reason: in frontmatter. "
             "Mutually exclusive with --ratifiable-file. Accepted and ignored "
             "for non-operator targets.",
    )
    f.add_argument(
        "--appetite",
        dest="appetite",
        default=None,
        metavar="1-10",
        help="T-3440 / ORG-089: bet-sizing score (integer 1-10, average of "
             "value / strategic-alignment / reversibility per §Decision.3). "
             "REQUIRED for an applicable bet: --serves set to any value other "
             "than goal-neutral-maintenance ('operator-ad-hoc' included), "
             "--bet-ref, or a non-vault-ADR parent — filing without it is "
             "refused (AppetiteRequired, exit 3). Exempt, and the medium default "
             "5 is stamped instead: --serves goal-neutral-maintenance, a hygiene "
             "label (chore, cleanup, hygiene, noodle, noop), or "
             "--nearest-intent-ref maintenance:<asset>.",
    )
    f.add_argument(
        "--bet-ref",
        dest="bet_ref",
        default=None,
        metavar="REF",
        help="T-3440 / ORG-089: optional reference to the bet this ticket serves "
             "(e.g. an ADR id). Its presence marks the ticket a bet for the "
             "appetite-applicability check.",
    )
    f.set_defaults(func=cmd_file)

    purpose = sub.add_parser(
        "purpose",
        help="set structured purpose fields without changing ticket state",
    )
    purpose.add_argument("ticket")
    purpose.add_argument(
        "--project-id", required=True, metavar="STABLE-ID|null",
        help="stable project id, or literal null for an explicit root",
    )
    purpose.add_argument(
        "--nearest-intent-ref", dest="nearest_intent_ref", required=True,
        metavar="ticket:T-NNNN|project:ID|goal:KEY|maintenance:ASSET",
    )
    purpose.add_argument("--serves", default=None)
    purpose.add_argument("--no-push", dest="no_push", action="store_true", default=False)
    purpose.set_defaults(func=cmd_purpose)

    c = sub.add_parser("claim", help="move ticket inbox/ → active/")
    c.add_argument("ticket")
    c.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                   help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    c.set_defaults(func=cmd_claim)

    uc = sub.add_parser("unclaim", help="move ticket active/ → inbox/ (inverse of claim)")
    uc.add_argument("ticket")
    uc.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    uc.set_defaults(func=cmd_unclaim)

    an = sub.add_parser(
        "annotate",
        help="append a transition-log note with NO state change (T-1972)",
    )
    an.add_argument("ticket")
    an.add_argument(
        "--note",
        required=True,
        help="note text to append to the transition log (empty/whitespace refused)",
    )
    an.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    # T-2204: annotate-only. Offered for annotate and NOT for the state-moving
    # subcommands on purpose — those must not be given a way to skip the pull.
    an.add_argument("--no-pull", dest="no_pull", action="store_true", default=False,
                    help="skip the pre-write pull (annotate only; it neither allocates an ID nor "
                         "moves state, so a stale view cannot make the note wrong). A pull failure "
                         "is already only a warning here — use this to skip the attempt entirely.")
    an.set_defaults(func=cmd_annotate)

    # T-2247: the ONLY correction path that reaches an auto-delivered worker.
    # inbox_consumer._ask_body() extracts `## Ask` and injects exactly that as
    # the prompt, so a correction anywhere else on the ticket is not delivered
    # at all — not merely delivered late.
    sa = sub.add_parser(
        "supersede-ask",
        help="correct a stale `## Ask` IN PLACE, keeping the original below it "
             "(T-2247) — the only correction the auto-consumer delivers",
    )
    sa.add_argument("ticket")
    sa.add_argument(
        "--with", dest="with_correction", required=True,
        help="the corrected instruction. Prepended inside `## Ask`; the original "
             "is demoted under a marked subheading, never deleted.",
    )
    sa.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    sa.add_argument("--no-pull", dest="no_pull", action="store_true", default=False,
                    help="skip the pre-write pull (same reasoning as annotate: no ID allocation, "
                         "no state move)")
    sa.set_defaults(func=cmd_supersede_ask)

    # T-2205: the post-resolution correction path. Disjoint from annotate by
    # construction — annotate refuses on done/, addendum refuses on everything
    # else — so neither can do the other's job by accident.
    ad = sub.add_parser(
        "addendum",
        help="append a VISIBLY post-resolution addendum to a done/ ticket "
             "(append-only; does not touch the resolve evidence) (T-2205)",
    )
    ad.add_argument("ticket")
    ad.add_argument(
        "--note",
        required=True,
        help="addendum text to append (empty/whitespace refused)",
    )
    ad.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    ad.add_argument("--no-pull", dest="no_pull", action="store_true", default=False,
                    help="skip the pre-write pull (same reasoning as annotate: append-only, no ID "
                         "allocation, no state move)")
    ad.set_defaults(func=cmd_addendum)

    b = sub.add_parser(
        "block",
        help="set not_before / depends_on gate on a ticket (frontmatter edit; no dir move)"
    )
    b.add_argument("ticket")
    b.add_argument(
        "--not-before",
        dest="not_before",
        default=None,
        metavar="DATE_OR_TS",
        help=(
            "Calendar gate: earliest pickup date. "
            "Formats: YYYY-MM-DD (interpreted as 00:00:00Z) or YYYY-MM-DDTHH:MM:SSZ. "
            "inbox_consumer suppresses until the timestamp passes. "
            "T-2833: requires --not-before-anchor."
        ),
    )
    b.add_argument(
        "--not-before-anchor",
        dest="not_before_anchor",
        default=None,
        metavar="operator:REF",
        help=(
            "T-2833: REQUIRED with --not-before. Anchors the date gate to the operator OR "
            "a real-world blocker — operator:<T-NNNN|tg:MSGID> (a ticket ref must "
            "resolve) OR event:<cert-expiry|key-expiry|cadence|window>:<param> (a "
            "genuine deadline/cadence). No free-text: an arbitrary 'not now' reason "
            "is a --depends-on dependency or a body pickup-note, never a bare date."
        ),
    )
    b.add_argument(
        "--depends-on",
        dest="depends_on",
        action="append",
        default=None,
        metavar="T-NNNN",
        help=(
            "Ticket-dep gate. Repeatable for multiple deps. "
            "Must match T-NNNN format. Forward-references (dep not yet filed) are allowed. "
            "Cascade-unblock (ORG-013) removes the dep from frontmatter when parent resolves."
        ),
    )
    b.add_argument(
        "--clear-not-before",
        dest="clear_not_before",
        action="store_true",
        default=False,
        help=(
            "T-0663: remove the not_before calendar gate from the ticket. "
            "Mutually exclusive with --not-before and --depends-on. "
            "No-op (no commit) if not_before is already absent. "
            "Ticket stays in its current lane."
        ),
    )
    b.add_argument(
        "--clear-dep",
        dest="clear_dep",
        action="append",
        default=None,
        metavar="T-NNNN",
        help=(
            "T-1975: remove a specific depends_on gate (repeatable). The manual "
            "inverse of --depends-on, for correcting a wrongly-set dep. "
            "Mutually exclusive with the add flags and the other clear flags. "
            "CLEAN ERROR (not a no-op) if the named dep is not present — it names "
            "a specific target, so a miss is surfaced rather than silently ignored."
        ),
    )
    b.add_argument(
        "--clear-deps",
        dest="clear_deps",
        action="store_true",
        default=False,
        help=(
            "T-1975: remove ALL depends_on gates. No-op (no commit) if already "
            "empty — idempotent, since it names no specific target (cf. --clear-dep). "
            "Mutually exclusive with the add flags and the other clear flags."
        ),
    )
    b.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                   help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    b.set_defaults(func=cmd_block)

    r = sub.add_parser("resolve", help="move ticket active/ → done/")
    r.add_argument("ticket")
    r.add_argument("--with", dest="with_summary", default=None)
    r.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                   help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    r.add_argument(
        "--receipt",
        action="append",
        default=None,
        metavar="CLAUSE=RECEIPT_ID=PROVENANCE",
        help="T-2842 / ORG-065 BT2: attach an obligation-clause receipt at "
             "resolve time, e.g. ADR-2842#E1.C1=ci-run:12345=ci. Repeatable. "
             "Feeds the closure-validation gate (ORG-065 §3.3) — GATING by "
             "default since the T-2934 flip (2026-08-12): resolve is refused "
             "when a required clause is unsatisfied. "
             "PANTHEON_CLOSURE_GATE_MODE=warn demotes to warn-only.",
    )
    _add_register_args(r)
    _add_successor_arg(r)
    sop_grp = r.add_mutually_exclusive_group()
    sop_grp.add_argument(
        "--sop-updated", dest="sop_updated", default=None, metavar="SHA",
        help="T-4680: for a sop:<stem> ticket whose SOP is still a STUB, the "
             "landed commit (on HEAD; after a squash merge, the squash sha) that "
             "filled sop/standard/<stem>.md. Recorded in the resolve note.",
    )
    sop_grp.add_argument(
        "--sop-no-change", dest="sop_no_change", default=None, metavar="REASON",
        help="T-4680: waive the first-use SOP gate for a sop:<stem> ticket "
             "(the run taught nothing new). Non-empty; recorded in the resolve note.",
    )
    r.set_defaults(func=cmd_resolve)

    # T-4119: hold a WIP slot open for a work-complete ticket awaiting an
    # external (HO-exclusive, T-1673) merge, without unclaiming it.
    am = sub.add_parser(
        "await-merge",
        help="T-4119: set/clear substate: awaiting-merge on an active ticket "
             "(frees the WIP slot without unclaiming; resolve strips it)",
    )
    am.add_argument("ticket")
    am.add_argument(
        "--ref",
        default=None,
        metavar="<repo>#<pr>|<repo>:<branch>",
        help="the external merge this ticket is parked on, e.g. "
             "echo-prismatic#365 or pantheon-config:agent/T-4061. Mutually "
             "exclusive with --clear; re-issuing --ref while already "
             "awaiting-merge updates the ref in place.",
    )
    am.add_argument(
        "--clear",
        action="store_true",
        default=False,
        help="remove substate: awaiting-merge + awaiting_merge_ref (e.g. the "
             "PR bounced back for more work). Idempotent no-op if already "
             "absent; refuses if the ticket is in a DIFFERENT substate.",
    )
    am.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally but skip git push")
    am.set_defaults(func=cmd_await_merge)

    # T-3440 / ORG-089: the four appetite re-decision verbs.
    re_earn = sub.add_parser(
        "re-earn",
        help="ORG-089: clear appetite-overrun with a new appetite + rationale "
             "(stays active; restarts the overrun clock)",
    )
    re_earn.add_argument("ticket")
    re_earn.add_argument("--appetite", required=True, metavar="1-10",
                         help="new appetite score (integer 1-10)")
    re_earn.add_argument("--rationale", required=True,
                         help="why the bet is still worth it at the new score")
    re_earn.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                         help="commit locally but skip git push")
    re_earn.set_defaults(func=cmd_re_earn)

    adj = sub.add_parser(
        "adjust-appetite",
        help="ORG-089: correct the appetite score in place (the ORIGINAL score "
             "was wrong; does not restart the clock)",
    )
    adj.add_argument("ticket")
    adj.add_argument("--appetite", required=True, metavar="1-10",
                     help="corrected appetite score (integer 1-10)")
    adj.add_argument("--rationale", required=True,
                     help="why the original score was wrong")
    adj.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                     help="commit locally but skip git push")
    adj.set_defaults(func=cmd_adjust_appetite)

    tr = sub.add_parser(
        "tier-rank",
        help="T-5715: set or clear optional intra-tier queue ordering",
    )
    tr.add_argument("ticket")
    tr_mode = tr.add_mutually_exclusive_group(required=True)
    tr_mode.add_argument(
        "--rank", type=int, metavar="INT",
        help="intra-tier rank (lower sorts first; cannot cross label tiers)",
    )
    tr_mode.add_argument(
        "--clear", action="store_true", default=False,
        help="remove tier_rank and return the ticket to FIFO among unranked peers",
    )
    tr.add_argument(
        "--reason", required=True,
        help="why this ticket is being reordered (recorded in its transition log)",
    )
    tr.add_argument(
        "--no-push", dest="no_push", action="store_true", default=False,
        help="commit locally but skip git push",
    )
    tr.set_defaults(func=cmd_tier_rank)

    dr = sub.add_parser(
        "drop",
        help="ORG-089: terminally drop a bet (open -> done/ with substate: dropped; "
             "does NOT satisfy dependencies/obligations)",
    )
    dr.add_argument("ticket")
    dr.add_argument("--dropped-reason", dest="dropped_reason", required=True,
                    help="one-line disposition: why this bet is being abandoned")
    dr.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally but skip git push")
    _add_successor_arg(dr)
    dr.set_defaults(func=cmd_drop)

    sacc = sub.add_parser(
        "security-accept",
        help="T-5678 (HO-only): accept a security_gate: pending ticket (ORG-105 s4 chain "
             "reviewed) so it enters the work queue",
        description="HO-only, advisory actor check (same as clear-operator-intent).",
    )
    sacc.add_argument("ticket")
    sacc.add_argument("--rank", default=None, choices=list(_security_gate.RANKS),
                      help="ORG-105 concern rank: primary or secondary (accepts the ticket)")
    sacc.add_argument("--note", required=True, help="why the chain is real / why released")
    sacc.add_argument("--threat-chain", dest="threat_chain", default=None,
                      help="REQUIRED when accepting an UNCLASSIFIED ticket (filed by an "
                           "automated filer with no chain): "
                           "'foothold=<F>;impact=<I>;path=<text>'")
    sacc.add_argument("--path-justification", dest="path_justification", default=None,
                      help="needed when the chain's matrix cell is an em dash")
    sacc.add_argument("--not-security", dest="not_security", default=None,
                      help="release the ticket as NOT a security finding (reason); clears "
                           "the gate instead of accepting")
    sacc.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                      help="commit locally but skip the remote update")
    sacc.set_defaults(func=cmd_security_accept)

    srej = sub.add_parser(
        "security-reject",
        help="T-5678 (HO-only): drop a security_gate: pending ticket with the note as reason",
        description="HO-only, advisory actor check (same as clear-operator-intent).",
    )
    srej.add_argument("ticket")
    srej.add_argument("--note", required=True, help="why there is no in-scope chain")
    srej.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                      help="commit locally but skip the remote update")
    srej.set_defaults(func=cmd_security_reject)

    cmi = sub.add_parser(
        "clear-operator-intent",
        help="T-5522 (HO-only): clear a STALE operator_intent on a ticket pulled to "
             "agent tier and decided there, so closing it needs no fabricated "
             "the operator register row",
        description="HO-only: the actor must resolve to the head engineering "
                    "orchestrator role. The check is advisory, same-user trust like "
                    "PANTHEON_ACTOR elsewhere; it is not an auth system. Refuses "
                    "--decided-by operator outright: a real the operator ruling goes "
                    "through `resolve --ruling` into his decision register.",
    )
    cmi.add_argument("ticket")
    cmi.add_argument("--decided-by", dest="decided_by", required=True,
                     help="who actually decided at agent tier (persona or role; never operator)")
    cmi.add_argument("--ref", required=True,
                     help="anchor for that decision: ticket note, mail id, TG id")
    cmi.add_argument("--why", required=True,
                     help="why the ask no longer needs the operator")
    cmi.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                     help="commit locally but skip git push")
    cmi.set_defaults(func=cmd_clear_operator_intent)

    ro = sub.add_parser(
        "reopen",
        help="ORG-089: reopen a DROPPED bet (done/ -> inbox/) with a fresh "
             "re-earned appetite; restarts the overrun clock",
    )
    ro.add_argument("ticket")
    ro.add_argument("--re-earn-appetite", dest="re_earn_appetite", required=True,
                    metavar="1-10", help="new appetite score for the reopened bet")
    ro.add_argument("--rationale", required=True,
                    help="why the dropped bet is worth reopening")
    ro.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally but skip git push")
    ro.set_defaults(func=cmd_reopen)

    cs = sub.add_parser(
        "cascade",
        help="ORG-013 cascade-unblock pass for a resolved tid (T-3289 PR-merge trigger)",
    )
    cs.add_argument(
        "--tid",
        required=True,
        help="resolved (done/) ticket id whose dependants should be re-checked "
             "for auto-unblock; T-NNNN. Fail-closed: refuses if tid is "
             "malformed, not found, or not currently in done/.",
    )
    cs.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    cs.set_defaults(func=cmd_cascade)

    rt = sub.add_parser(
        "retarget",
        help="swap a ticket's target field (no state change; refuses on done)",
    )
    rt.add_argument("ticket")
    rt.add_argument(
        "--to",
        required=True,
        help="new target: `role:<R>` (re-pools the ticket to a known functional role; "
             "refused fail-closed if R is not a declared role_id, T-3761) or `operator`. "
             "A persona / alias / bare role-id is REFUSED (ORG-088, T-5239).",
    )
    rt.add_argument(
        "--note",
        default=None,
        help="optional one-line context appended to transition log",
    )
    rt.add_argument(
        "--keep-state",
        dest="keep_state",
        action="store_true",
        default=False,
        help="preserve existing state on cross-actor retarget "
             "(default: reset to inbox so new target sees fresh work)",
    )
    rt.add_argument(
        "--intent",
        default=None,
        choices=INTENT_VOCAB,
        help="T-0465: set/amend intent field on retarget. Required only when "
             "explicit intent change is desired; otherwise: retarget to operator "
             "auto-defaults intent if absent; retarget away from operator drops "
             "the field (target=operator-specific surface).",
    )
    rt.add_argument(
        "--ratifiable-file",
        dest="ratifiable_file",
        action="append",
        default=None,
        metavar="REPO:PATH",
        help="T-5014: attach a file (repeatable) when retargeting to operator; "
             "must exist in its repo. Merged into ratifiable_files.",
    )
    rt.add_argument(
        "--no-file",
        dest="no_file",
        default=None,
        metavar="REASON",
        help="T-5014: retargeting to operator requires an attached file or this "
             ">=20-char reason (stored as no_file_reason). Ignored for other targets.",
    )
    rt.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                   help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    rt.add_argument(
        "--serves",
        default=None,
        help="T-1103 / ORG-034: set or overwrite the serves: arc-intent field. "
             "When omitted, any existing serves: value is preserved. "
             "Free-text; 'goal-neutral-maintenance' is a valid value.",
    )
    rt.set_defaults(func=cmd_retarget)

    at = sub.add_parser(
        "attach",
        help="T-5027: add ratifiable_files (any target) or a no-file reason "
             "(target=operator) to an existing inbox/active ticket",
    )
    at.add_argument("ticket")
    at.add_argument("--ratifiable-file", dest="ratifiable_file", action="append",
                    default=None, metavar="REPO:PATH",
                    help="attach a committed file (repeatable, deduped); clears "
                         "any no_file_reason")
    at.add_argument("--no-file", dest="no_file", default=None, metavar="REASON",
                    help=">=20-char reason this operator ask has no file; "
                         "mutually exclusive with --ratifiable-file")
    at.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally, skip the remote update")
    at.set_defaults(func=cmd_attach)

    cn = sub.add_parser(
        "close-noodle",
        help="close a target=operator intent=noodle ticket (optionally spawn child action)",
    )
    cn.add_argument("ticket")
    cn.add_argument("--with", dest="with_summary", default=None)
    cn.add_argument(
        "--spawn-to",
        dest="spawn_to",
        default=None,
        help="spawn a child ticket to this target as part of the close. "
             "Persona name (orchestrator/specialist/recovery/operator) or legacy role ID.",
    )
    cn.add_argument(
        "--as-action",
        dest="as_action",
        action="store_true",
        default=False,
        help="required when --spawn-to is set; the spawned child is filed "
             "with intent=action (if target=operator) and parent_ticket linkage.",
    )
    cn.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    _add_register_args(cn)
    _add_successor_arg(cn)
    cn.set_defaults(func=cmd_close_noodle)

    ca = sub.add_parser(
        "close-action",
        help="close a target=operator intent=action ticket; auto-spawns review to original requester",
    )
    ca.add_argument("ticket")
    ca.add_argument("--with", dest="with_summary", default=None)
    ca.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                    help="commit locally but skip git push; the next pushing op (or a manual push) carries it to origin")
    _add_register_args(ca)
    _add_successor_arg(ca)
    ca.set_defaults(func=cmd_close_action)

    lfm = sub.add_parser(
        "lint-frontmatter",
        help="report frontmatter the ORG-021 field registry does not sanction "
             "(read-only, T-4751)",
    )
    lfm.add_argument(
        "--strict", action="store_true", default=False,
        help="exit 1 on an unknown-field, a bad-value or an unavailable registry "
             "(S2 CI gate); legacy-field use is info only (a hand-added legacy field "
             "is caught at write time, not here); default exits 0 — it is a report",
    )
    # T-4760 (f): read-only, and S2's CI runs `--strict` on PR branches, so it
    # is exempt from the run-from-main guard (see main()).
    lfm.set_defaults(func=cmd_lint_frontmatter, off_main_ok=True)

    ls = sub.add_parser("list", help="list tickets (read-only)")
    ls.add_argument("--state", choices=STATES, default=None)
    ls.add_argument(
        "--target", default=None,
        help="filter by target. A persona also surfaces the role:<R> pool tickets "
             "that persona is eligible for (ORG-088 Plane-A, T-3761); a literal "
             "role:<R> value shows exactly that role's pool.",
    )
    ls.add_argument("--label", default=None)
    ls.add_argument(
        "--show-unblocks", action="store_true",
        help="annotate each row with the derived reverse of depends_on — "
             "which tickets THIS one unblocks (T-2720). Off by default.",
    )
    ls.add_argument(
        "--sort", choices=("priority", "created", "retargeted"), default="priority",
        help="ORG-078 §4: 'priority' (default) applies ceiling priority-rank "
             "+ dependency-inheritance (§Priority-inversion) over "
             "(effective_rank, tier_rank, effective_queue_ts, ticket_id). 'created'/"
             "'retargeted' are pure-FIFO bypass views that skip the "
             "priority-list parse entirely.",
    )
    ls.add_argument(
        "--explain-priority", metavar="T-NNNN", default=None,
        help="ORG-078 §Priority-inversion: print a diagnostic explaining "
             "why the named ticket ranks where it does (own_rank, "
             "effective_rank, inheritance chain if elevated) instead of "
             "the normal row listing.",
    )
    ls.set_defaults(func=cmd_list)

    rdy = sub.add_parser(
        "ready",
        help="list currently-unblocked tickets (inbox/active, no depends_on, "
             "not_before passed) — read-only",
    )
    rdy.add_argument(
        "--target", default=None,
        help="filter by target persona; a persona also surfaces its eligible "
             "role:<R> pool tickets (ORG-088 Plane-A, T-3761)",
    )
    rdy.add_argument(
        "--exclude-label", action="append", metavar="LABEL", default=None,
        help="hide tickets carrying this label (repeatable). e.g. "
             "--exclude-label interactive-required drops supervised/human-session "
             "tickets from an autonomous 'what can I do solo' scan (T-3540).",
    )
    rdy.add_argument(
        "--show-unblocks", action="store_true",
        help="annotate each row with the derived reverse of depends_on — "
             "which tickets THIS one unblocks (T-2720). Off by default.",
    )
    rdy.set_defaults(func=cmd_ready)

    pr = sub.add_parser(
        "prime",
        help="emit project ticket-state as JSON for a fresh-agent bootstrap "
             "(ready/blocked/active/recently_done) — read-only",
    )
    pr.add_argument(
        "--target", default=None,
        help="filter ready/blocked/active by target persona; a persona's bootstrap "
             "also includes its eligible role:<R> pool tickets (ORG-088 Plane-A, T-3761)",
    )
    pr.add_argument("--done-limit", type=int, default=15, help="max recently-done tickets (default 15)")
    pr.set_defaults(func=cmd_prime)

    pl = sub.add_parser(
        "priority-list",
        help="ORG-078 priority-list operator tooling",
    )
    pl_sub = pl.add_subparsers(dest="priority_list_cmd", required=True,
                                parser_class=_NoAbbrevParser)
    plv = pl_sub.add_parser(
        "validate",
        help="validate rulebook/priority-list.md — parse errors + "
             "probably-dead/high-volume label warnings + sample-matching "
             "tickets per label (ORG-078 §Priority-list-drift-hygiene; "
             "semantic-drift deferred per T-3424) — read-only",
    )
    plv.set_defaults(func=cmd_priority_list_validate)

    lb = sub.add_parser(
        "label",
        help="T-3020 BT5: label add/remove/list + backfill-audit — labels: "
             "frontmatter operator tooling",
    )
    lb_sub = lb.add_subparsers(dest="label_cmd", required=True,
                                parser_class=_NoAbbrevParser)

    la = lb_sub.add_parser(
        "add", help="add one or more labels to a ticket (idempotent)"
    )
    la.add_argument("ticket")
    la.add_argument("labels", nargs="+", metavar="LABEL")
    la.add_argument("--threat-chain", dest="threat_chain", default=None,
                    help="T-5678: ORG-105 chain, required when adding a security label "
                         "(same form as `file --threat-chain`)")
    la.add_argument("--path-justification", dest="path_justification", default=None,
                    help="T-5678: see `file --path-justification`")
    la.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                     help="commit locally but skip git push")
    la.set_defaults(func=cmd_label_add)

    lr = lb_sub.add_parser(
        "remove", help="remove one or more labels from a ticket"
    )
    lr.add_argument("ticket")
    lr.add_argument("labels", nargs="+", metavar="LABEL")
    lr.add_argument("--no-push", dest="no_push", action="store_true", default=False,
                     help="commit locally but skip git push")
    lr.set_defaults(func=cmd_label_remove)

    ll = lb_sub.add_parser(
        "list", help="print a ticket's current labels, one per line — read-only"
    )
    ll.add_argument("ticket")
    ll.set_defaults(func=cmd_label_list)

    lba = lb_sub.add_parser(
        "backfill-audit",
        help="report tickets missing arc-labels inferable from parent_ticket "
             "/ depends_on chains + title/body pattern-matching — read-only, "
             "report only (does not auto-apply)",
    )
    lba.add_argument(
        "--scope", choices=["inbox", "active", "done", "all"], default=None,
        help="ticket states to audit (default: inbox+active)",
    )
    lba.set_defaults(func=cmd_label_backfill_audit)

    lin = sub.add_parser(
        "lineage",
        help="T-3332: read-only lineage-graph queries -- what a ticket serves "
             "(up), what serves a node (down), where walks end (roots)",
    )
    lin_sub = lin.add_subparsers(dest="lineage_cmd", required=True,
                                 parser_class=_NoAbbrevParser)
    edges_help = ("edge fields to walk: 'default' (nearest_intent_ref ticket refs, "
                  "parent_ticket, implements, verifies, ratifies), 'all' (adds the "
                  "informational depends_on, supersedes, informs), a comma list, or "
                  "'+field' items added to the default")
    lup = lin_sub.add_parser("up", help="walk T-NNNN up to its terminus -- read-only")
    lup.add_argument("node", metavar="T-NNNN")
    ldn = lin_sub.add_parser(
        "down",
        help="walk down from T-NNNN, goal:KEY, maintenance[:ASSET], "
             "operational-root, or ORG-NNN[#Ek] -- read-only",
    )
    ldn.add_argument("node", metavar="NODE")
    for sp in (lup, ldn):
        sp.add_argument("--edges", default=None, help=edges_help)
        sp.add_argument("--depth", type=int, default=None, help="stop after N hops")
        sp.add_argument("--json", action="store_true", help="graph-shaped JSON output")
        sp.set_defaults(func=cmd_lineage, off_main_ok=True)
    lrt = lin_sub.add_parser(
        "roots",
        help="classify tickets by terminus (goal / operational-root / maintenance / "
             "UNROOTED) -- read-only",
    )
    lrt.add_argument("--state", choices=_LINEAGE_STATE_CHOICES, default="open",
                     help="tickets to classify (default open = inbox+active)")
    lrt.add_argument("--counts", action="store_true",
                     help="per-class counts only (UNROOTED is the honesty metric)")
    lrt.add_argument("--class", dest="cls", default=None,
                     choices=("goal", "operational-root", "maintenance", "UNROOTED"),
                     help="list only this class")
    lrt.add_argument("--limit", type=int, default=None, help="max tickets listed per class")
    lrt.add_argument("--edges", default=None, help=edges_help)
    lrt.add_argument("--json", action="store_true", help="JSON output")
    # Read-only, so exempt from the run-from-main guard (T-4760 (f) precedent).
    lrt.set_defaults(func=cmd_lineage, off_main_ok=True)

    return p


def _current_branch(repo_root: Path) -> str:
    res = _git("branch", "--show-current", cwd=repo_root)
    if res.returncode != 0:
        raw_detail = res.stderr if res.stderr.strip() else res.stdout
        detail_lines = [
            " ".join(line.split())
            for line in raw_detail.splitlines()
            if line.strip()
        ]
        detail = detail_lines[-1] if detail_lines else "no git diagnostic"
        detail = re.sub(r"/home/\S+", "<path>", detail)
        if len(detail) > 90:
            detail = detail[:87] + "..."
        raise TicketError(
            f"cannot determine current git branch (rc={res.returncode}): "
            f"{detail}; refusing before dispatch; "
            "run ticket.py from the canonical pantheon-config checkout on branch main"
        )
    return res.stdout.strip()


def _assert_cli_checkout_allowed(repo_root: Path) -> None:
    """Refuse to run ticket.py from any checkout that is not on branch `main`.

    ticket.py auto-commits-and-pushes every lifecycle verb to `main`. Run from a
    NON-main checkout, that side-effecting commit lands on whatever branch the
    checkout happens to be on instead — the ticket state strands on the feature
    branch (main's copy never sees it), and on a mixed code+ticket arc the
    follow-on push to main carries the code commit and trips the pre-push
    CODE-path guard (see lessons/2026-08-27-repo-global-tool-ops-implicitly-bind-
    to-worktree.md, instance 4; bugs/ticket-py-checkout-guard-agent-only-misses-
    persona-seat-branch.md).

    T-4008 broadened this from an `agent/`-only prefix match to refuse ANY named
    non-main branch. The original guard (T-3020) only knew the `agent/<task>`
    worker-branch shape; orchestrator SEATS (ORG-043 mason/artisan/recovery) name their
    arc branches `<persona>/T-NNNN`, which sailed straight through and stranded
    the ticket commit on the seat branch.

    A DETACHED HEAD (`_current_branch` returns "") is DELIBERATELY still allowed:
    it is not the stranding failure mode but a sanctioned shape. `ticket_lint`'s
    unified-push runs `ticket.py retarget` from a detached-HEAD worktree of
    `origin/main` and pushes `HEAD:main` there (tools/test_ticket_lint.py
    ::TestRetargetDetachedHeadPush), and the in-process real-ticket.py argv
    smokes (test_watch_runner / test_knowledge_sweep) run against a detached temp
    repo. Only a NAMED non-main branch commits to the wrong branch, so only that
    is refused — matching the original guard's detached-HEAD behavior.
    """
    branch = _current_branch(repo_root)
    if branch in ("", "main"):  # "" == detached HEAD (allowed; see docstring)
        return
    # Truncate to keep the whole one-line message within the ~220-char bound the
    # tests assert (T-4008 NIT: real seat branches are far shorter; 50 chars is
    # ample to recognise the offending branch).
    observed = branch[:50]
    raise TicketError(
        f"refusing to run ticket.py from {observed!r} (not 'main'): it "
        "auto-commits+pushes to main, so run from the canonical pantheon-config "
        "checkout on branch main."
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        # T-4760 (f): a subcommand opts out of the run-from-main guard ONLY by
        # setting `off_main_ok=True` in its parser defaults — today
        # `lint-frontmatter` and the T-3332 `lineage up|down|roots` queries.
        # Both are read-only (they never write, commit or push, so they cannot
        # strand ticket state on a branch — the failure the guard exists for);
        # lint-frontmatter must also run on PR branches for S2's CI gate.
        if not getattr(args, "off_main_ok", False):
            _assert_cli_checkout_allowed(REPO_ROOT)
        return args.func(args)
    except TicketError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code


if __name__ == "__main__":
    sys.exit(main())
