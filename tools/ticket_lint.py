#!/usr/bin/env python3
"""Pantheon ticket-lint — hourly frontmatter format scanner and auto-fixer.

Scans `tickets/inbox/` + `tickets/active/` and for each ticket:

AUTO-FIX (mechanical, no judgment — fix in place + commit):
  - target=operator with no `intent` field → set intent per cmd_file auto-default
    (ratify if ratifiable_files present and non-empty, else action).
  - `intent` present but target != operator → drop intent (operator-only per T-0465;
    cmd_retarget already does this on retarget-away-from-operator).

RETARGET (genuinely-improper — bounce back to the originator, T-0556):
  - target=operator + intent=ratify + NO ratifiable_files → UNRATIFIABLE (the cron
    cannot know which file should be ratifiable; bounce to the requester to fix).
  - target=operator + intent=ratify + a ratifiable_files entry POSITIVELY CONFIRMED
    ABSENT at its canonical ref → STALE-PATH (wrong path; bounce to requester).
    Cross-repo entries (`REPO:rel/path`) resolve FAIL-OPEN via
    `ratifiable_path_status`: pantheon-config against the local tree; foreign
    repos (revenue-project / echo-prismatic / pantheon-pwa) against origin/main via
    `sudo -u claude git fetch + cat-file -e`. ONLY a positively-confirmed-absent
    path is a defect — True (present) and None (unverifiable) are BOTH non-defects
    (the T-0549 false-bounce fix: never bounce what we cannot prove absent).
  - EXCEPTION: if `requester` is missing or == operator there is no originator to
    bounce to → fall back to FLAG (below).

FLAG (judgment-needed — surface via digest, do NOT auto-guess):
  - the RETARGET cases above when `requester` is missing or == operator.
  - missing required frontmatter fields (id / target / state / created / requester)
    → PARSE-ERROR.

OUTPUT: commit any auto-fixes (scoped to touched ticket files) + send a Recovery
informational/escalation digest naming (a) what was auto-fixed and (b) what was
flagged-for-review (with the specific malformation per ticket).

CRON ISOLATION: uses `tools/_git_utils.with_worktree` (T-0471 pattern). Module-global
REPO_ROOT is never rebound inside the worktree callback (T-0478 pattern). Mirrors
`tools/spine_index_groom.py`'s `run_all`/`_do_groom` shape.

DEFAULT-SAFE CONTRACT (T-0260): must pass --commit-prod, --dry-run, or --repo-root
explicitly. Bare invocation without an explicit mode refuses (exit code 64).

CLI:
    python3 tools/ticket_lint.py --commit-prod           # cron path: scan+fix+commit+push
    python3 tools/ticket_lint.py --dry-run               # scan+report only; no side effects
    python3 tools/ticket_lint.py --repo-root <path>      # safe mode: tmp repo target
    python3 tools/ticket_lint.py --repo-root <path> --no-digest  # safe mode, skip Telegram

Designed for hourly cron at *:20 UTC on `claude-fallback` (Recovery).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import (  # noqa: E402
    commit_scoped,
    push_with_retry,
    with_worktree,
)
from tools.agent_role_map import (  # noqa: E402
    PersonaRoleMembershipError,
    known_role_ids,
    resolve_instance_role,
    resolve_persona,
    valid_target_values,
)
from tools import grand_project_program  # noqa: E402 — T-2426 / ORG-061 indexes
from tools.adr_reeval_walk import (  # noqa: E402 — T-2841/ORG-065 BT1 reuse
    _adr_id_from,
    _load_yaml,
    _split_frontmatter,
)
from tools.ticket import (  # noqa: E402
    STATES,
    TICKET_FILENAME_RE,
    TicketError,
    _build_ticket_text,
    _fm_snapshot,
    parse_front_matter,
    serialize_front_matter,
)
from tools.watch_digest import read_telegram_token  # noqa: E402

# Role IDs that are not persona-canonical — detected for AUTO-FIX class 3.
# operator is excluded (no persona alias; passes through unchanged).
# head_research_orchestrator is specialist's T-3807 forward audit-id; specialist is
# retained as the legacy audit-id (D1, P4 audit-continuity) — both normalize to specialist.
# ORG-100 (T-3814) added the four UNAMBIGUOUS role-class audit-ids
# (head_engineering_orchestrator→orchestrator, head_system_orchestrator→recovery,
# head_memory_orchestrator→archivist, head_auditor→codex) — each normalizes 1:1 via
# resolve_persona. orchestrator (orchestrator's OLD id) is KEPT: it still normalizes to
# orchestrator via the PERSONA_ALIASES legacy alias (audit-continuity for historical targets).
# engineering_orchestrator is DELIBERATELY EXCLUDED: its reverse is ambiguous
# (mason OR artisan), so auto-normalizing it would silently mis-retarget — those two
# seats' legacy [mason]/[artisan] targets ride persona-name identity instead (D-c).
_ROLE_ID_TARGETS: frozenset[str] = frozenset(
    {
        "orchestrator",
        "head_research_orchestrator",
        "specialist",
        "head_engineering_orchestrator",
        "head_system_orchestrator",
        "head_memory_orchestrator",
        "head_auditor",
    }
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Foreign repos that can appear in a ratifiable_files entry as `REPO:rel/path`.
# These live under the `claude` unix account and are readable from this process
# (claude-fallback/Recovery) ONLY via `sudo -u claude bash -c '...'` (repair-sudo,
# non-interactive). `pantheon-config` itself is resolved against the local
# filesystem tree (repo_root) and is NOT in this map.
RATIFIABLE_FOREIGN_REPOS: dict[str, str] = {
    "revenue-project": "/home/claude/revenue-project",
    "echo-prismatic": "/home/claude/echo-prismatic",
    "pantheon-pwa": "/home/claude/pantheon-pwa",
}

# Bounded timeout (seconds) for the foreign-repo git fetch+verify subprocess.
_RATIFIABLE_FETCH_TIMEOUT = 20

# SAFETY GATE (T-0556): live auto-retarget MUTATION is disabled by default.
# Classification (RETARGET vs FLAG) and digest surfacing are live, but the
# actual `ticket.py retarget` shell-out (commit + push of a real ticket inside
# the lint's commit worktree) initially had no pre-production integration
# coverage AND the push path was broken under detached-HEAD worktrees (the
# ticket.py retarget uses `git push origin main` which is a silent no-op
# from detached HEAD; auto-fix runs piggybacked on _git_commit_fixes' own
# HEAD:main push, but retarget-only runs never reached origin).
# T-0558 closes both gaps: (a) _apply_retarget now always passes --no-push;
# (b) _do_lint runs a UNIFIED push_with_retry(HEAD:main) covering BOTH
# retarget commits and auto-fix commits; (c) a faithful integration test
# reproduces the detached-HEAD worktree (`git worktree add --detach`) and
# asserts retarget-only commits actually land on origin/main.
# With the push path fixed + the test green, the gate flips ON.
RETARGET_AUTO_APPLY = True

# Module-level alias for with_worktree — allows unit tests to patch
# ticket_lint._with_worktree without touching _git_utils internals.
_with_worktree = with_worktree

STATES_TO_LINT = ("inbox", "active")

# Telegram destination — same env-var convention as ticket_grooming.py.
def _default_telegram_env() -> Path:
    chan = Path.home() / ".claude" / "channels" / "telegram"
    pantheon = chan / "pantheon.env"
    return pantheon if pantheon.exists() else chan / ".env"


TELEGRAM_ENV = Path(
    os.environ.get("PANTHEON_TELEGRAM_ENV", str(_default_telegram_env()))
)
TELEGRAM_CHAT_ID = os.environ.get("PANTHEON_TELEGRAM_CHAT_ID", "424242424")  # the operator

# Telegram sendMessage text limit (below 4096 for safety headroom).
_TELEGRAM_MAX_TEXT = 4000


# ---------------------------------------------------------------------------
# Domain types.
# ---------------------------------------------------------------------------


class LintAction(Enum):
    """Classification outcome for one ticket."""
    CLEAN = "clean"          # no malformation; no action needed
    AUTO_FIX = "auto_fix"    # mechanical fix applied (or to be applied)
    FLAG = "flag"            # judgment-needed; surface in digest
    RETARGET = "retarget"    # genuinely-improper; bounce back to the originator
    WARN = "warn"            # soft nudge only (never blocks); filer advisory


class LintError(Exception):
    """Raised when an operation is attempted on a ticket that requires judgment."""


@dataclass
class LintResult:
    """Outcome of linting one ticket file."""
    tid: str
    path: Path
    action: LintAction
    description: str
    # For RETARGET-class results: the originator the ticket should bounce to.
    requester: str | None = None


@dataclass(frozen=True)
class ProgramIndexFinding:
    """Report-only ORG-061 stage/owned-obligation index integrity finding."""

    code: str
    record_id: str
    description: str


@dataclass
class OrphanRatification:
    """A pending-ratification artifact with no open ratify ticket pulling on it.

    REPORT-ONLY (T-0784): surfaced in the digest as a floating-ratification
    finding. No auto-fix, no auto-file — the operator either files a ratify
    ticket naming this path or updates the artifact's stale status line.
    """
    rel_path: str        # repo-relative path to the artifact (e.g. architecture/adr/ORG-013-...md)
    status_excerpt: str  # the matched status line, for operator context


@dataclass
class DepGraphFinding:
    """A structural defect in the ticket depends_on graph (report-only; T-0975).

    Three kinds, all surfaced in the digest and never auto-fixed:
      - "cycle": a set of OPEN tickets whose depends_on edges form a cycle — an
        unbreakable deadlock (none can ever unblock). A human must decide which
        edge is wrong (a common cause: a deploy/carrier CHILD wrongly gated on
        its feature parent, which already depends on the carrier). Found live
        2026-06-08 (T-0367 <-> T-0470). Neither the per-file frontmatter lint
        nor a done/missing-dep scan catches these.
      - "stale_done_dep": an OPEN ticket whose depends_on names already-DONE
        tickets AND WHOSE EVERY DEP IS DONE — the state ORG-013 cascade-unblock
        should have cleared outright (T-0971 fixes that forward-only; nothing
        retroactively scans). Harmless to readiness (done deps already count as
        satisfied) but it is bookkeeping cruft and a real signal the cascade
        missed a case. 2 instances found+fixed 2026-06-08.

        NARROWED at T-2396, and the exclusion is the point: a done dep beside a
        still-OPEN dep is NOT flagged, because the cascade deliberately declines
        to fire on a partial resolve and T-2177 prunes all done deps together
        when the last one lands. That state is self-healing, so flagging it
        emitted permanent unactionable entries into the same section as the
        deadlock class — and its detail string accused cascade-unblock of a miss
        it had not made.
      - "dropped_dep" (T-3440 / ORG-089): an OPEN ticket gated on a dep in done/
        with `substate: dropped`. Distinct from stale_done_dep: a dropped bet
        does NOT satisfy the dependency (D-N), so the gate is LIVE, not
        self-healing — reopen+re-earn+ship the dep or prune it with rationale.
    """
    kind: str          # "cycle" | "stale_done_dep" | "dropped_dep"
    detail: str        # human-readable description (ticket ids + the issue)


# ---------------------------------------------------------------------------
# Ratifiable-files path resolution (the ONLY I/O in the detection path).
# ---------------------------------------------------------------------------


def ratifiable_path_status(repo: str, rel_path: str, root: Path) -> bool | None:
    """Resolve whether a ratifiable_files `REPO:rel/path` entry positively exists.

    Returns:
      True  — confirmed present.
      False — confirmed absent (positive negative: git reports the path missing
              at origin/main, or the local pantheon-config file does not exist).
      None  — UNRESOLVABLE / could-not-verify (sudo denied, timeout, fetch error,
              repo missing, unexpected stderr, unknown repo). FAIL-OPEN sentinel.

    This is the ONLY function in the detection path that performs I/O (subprocess
    sudo/git for foreign repos). It is injectable at module level so tests can
    monkeypatch it; `detect_malformations` stays side-effect-free by delegating
    here.

    SAFETY: callers must treat None as "do NOT flag" — only False (positively
    confirmed absent) is a defect. This is the property that prevents
    false-bouncing a correctly-specced ticket (the T-0549 incident).
    """
    # pantheon-config (or empty repo prefix) → resolve against the local tree.
    if repo in ("", "pantheon-config"):
        return (root / rel_path).exists()

    # Foreign repo → verify against the canonical pushed ref (origin/main).
    abs_path = RATIFIABLE_FOREIGN_REPOS.get(repo)
    if abs_path is None:
        # Unknown repo — cannot verify. Fail-open.
        return None

    inner = (
        f"cd {abs_path} && git fetch -q origin main "
        f"&& git cat-file -e origin/main:{rel_path}"
    )
    try:
        proc = subprocess.run(
            ["sudo", "-u", "claude", "bash", "-c", inner],
            capture_output=True,
            text=True,
            timeout=_RATIFIABLE_FETCH_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 — timeout, OSError, anything → fail-open.
        return None

    if proc.returncode == 0:
        return True

    # Non-zero. Only treat as a positive ABSENT when git positively reports the
    # path is missing at origin/main. The witnessing message is
    #   fatal: path '<rel>' does not exist in 'origin/main'
    # which only appears when the ref RESOLVED but the path is absent. A failed
    # fetch / bad ref says "invalid object name 'origin/main'" instead, and any
    # other failure (sudo denied, repo missing, unexpected stderr) is treated as
    # UNRESOLVABLE → fail-open None. Be conservative: never return False on
    # ambiguity.
    stderr_lc = (proc.stderr or "").lower()
    if "does not exist in" in stderr_lc and "invalid object name" not in stderr_lc:
        return False
    return None


# ---------------------------------------------------------------------------
# Clone-on-demand repo registry (T-0761): the `repos:` field structural gate.
# ---------------------------------------------------------------------------


def _load_repo_registry(root: Path) -> dict | None:
    """Load the repos.yaml clone-on-demand registry from the repo root.

    Returns the parsed mapping, or None if the file is absent / unparseable /
    PyYAML is unavailable. FAIL-OPEN by design (mirrors ratifiable_path_status's
    None sentinel): a missing or broken registry must never flag a ticket — only
    a positively-invalid `repos:` entry against a *present* registry is a defect.

    Thin wrapper around :func:`tools.repo_registry.load_repo_registry` (T-1136).
    """
    from tools.repo_registry import load_repo_registry

    return load_repo_registry(root)


def validate_repos_field(
    repos_list: list, target: str, registry: dict
) -> str | None:
    """Validate a ticket's `repos:` field against the registry.

    Each entry is `name` or `name:access` (access ∈ {ro, rw}, default ro). The
    repo must exist in the registry and the ticket's target agent must hold a
    grant at least as strong as the requested access. Returns an error
    description for the FIRST violation, or None if all entries are valid.
    Pure (no I/O); the registry is loaded by the caller.
    """
    entries = registry.get("repos", []) or []
    grants_by_repo = {
        e.get("name"): (e.get("grants") or {})
        for e in entries
        if isinstance(e, dict) and e.get("name")
    }
    # T-5239 / ORG-088: tickets route by ROLE, so the target is a pool of seats, not one
    # persona. The declaration is satisfiable iff AT LEAST ONE member of the pool holds
    # a sufficient grant (whichever member pulls it can do the work only if it has
    # access; requiring EVERY member would flag any pool with a narrower-granted seat).
    # An unverifiable membership source or an empty pool fails OPEN (no registry
    # judgement is possible); an entry-shape error (bad access / unknown repo) is the
    # same for every member, so the first member's error is reported.
    if target and str(target).startswith("role:"):
        from tools.agent_role_map import role_members
        try:
            members = sorted(role_members(str(target).split(":", 1)[1]))
        except PersonaRoleMembershipError:
            return None
        if not members:
            return None
        first_err: str | None = None
        for member in members:
            err = validate_repos_field(repos_list, member, registry)
            if err is None:
                return None
            first_err = first_err or err
        return first_err
    # Two-step resolution (ORG-030 Phase 1):
    #   1. Normalize audit role-id or persona → canonical persona name.
    #   2. Persona → instance role-key (the grants table axis in repos.yaml).
    persona = resolve_persona(target) if target else target
    role_key = resolve_instance_role(persona) if persona else persona
    for item in repos_list:
        name, _sep, access = str(item).partition(":")
        access = access or "ro"
        if access not in ("ro", "rw"):
            return f"entry {item!r}: access must be 'ro' or 'rw' (got {access!r})"
        if name not in grants_by_repo:
            return f"entry {item!r}: unknown repo {name!r} (not in repos.yaml registry)"
        grant = grants_by_repo[name].get(role_key)
        if grant is None:
            return f"entry {item!r}: {role_key!r} has no grant for {name!r} in repos.yaml"
        if access == "rw" and grant != "rw":
            return (
                f"entry {item!r}: {role_key!r} has {grant!r} (not rw) for {name!r}; "
                f"rw requires an rw grant"
            )
    return None


# ---------------------------------------------------------------------------
# Detection logic.
# ---------------------------------------------------------------------------


def detect_malformations(
    path: Path,
    repo_root: Path | None = None,
) -> LintResult:
    """Classify one ticket file as CLEAN / AUTO_FIX / FLAG.

    `repo_root` is used to resolve ratifiable_files paths for the STALE-PATH
    check. When None, falls back to module-global REPO_ROOT (not-rebindable
    per T-0478; worktree callers should always pass the worktree root).

    Never writes to disk. Side-effect-free classification only.
    """
    _root = repo_root if repo_root is not None else REPO_ROOT

    tid = path.stem

    # Parse frontmatter — parse errors → FLAG.
    try:
        text = path.read_text(encoding="utf-8")
        fields, _body = parse_front_matter(text)
    except TicketError as exc:
        return LintResult(
            tid=tid,
            path=path,
            action=LintAction.FLAG,
            description=f"parse error: {exc}",
        )

    target = fields.get("target", "")
    intent = fields.get("intent")
    ratifiable_files: list[str] = fields.get("ratifiable_files") or []
    requester = fields.get("requester")

    # ---- AUTO-FIX class 1: target=operator, intent absent ----
    if target == "operator" and intent is None:
        inferred = "ratify" if ratifiable_files else "action"
        return LintResult(
            tid=tid,
            path=path,
            action=LintAction.AUTO_FIX,
            description=(
                f"target=operator with no intent field; "
                f"will set intent={inferred} (ratifiable_files={'present' if ratifiable_files else 'absent'})"
            ),
        )

    # ---- AUTO-FIX class 2: intent present but target != operator ----
    if intent is not None and target != "operator":
        return LintResult(
            tid=tid,
            path=path,
            action=LintAction.AUTO_FIX,
            description=(
                f"intent={intent!r} present but target={target!r} (not operator); "
                f"intent is operator-only per T-0465; will drop intent field"
            ),
        )

    # ---- FLAG class: target is a PERSONA (or alias / bare audit role-id) — T-5239 ----
    # ORG-088 amendment (the operator TG 12076/12077, 2026-10-19): tickets route by ROLE
    # only. An OPEN (inbox/active) ticket carrying a seat target is malformed. FLAG,
    # not AUTO_FIX: which role pool fits is the filer's call (the old T-0492 class-3
    # role-id -> persona rewrite is RETIRED — it would now write the very thing this
    # flags). Done tickets are never scanned (STATES_TO_LINT), so historical persona
    # targets stay immutable. `operator` is the operator, not a seat — exempt.
    if target and target != "operator" and not str(target).startswith("role:"):
        from tools.agent_role_map import persona_pool_role
        try:
            pool = persona_pool_role(str(target))
        except Exception:  # noqa: BLE001 - hint only
            pool = None
        if pool or target in _ROLE_ID_TARGETS or target in valid_target_values():
            hint = f"role:{pool}" if pool else "a role:<R> pool"
            return LintResult(
                tid=tid,
                path=path,
                action=LintAction.FLAG,
                description=(
                    f"target={target!r} is a persona/seat; persona targeting is removed "
                    f"(ORG-088, T-5239) - retarget to {hint}"
                ),
            )

    # ---- FLAG class: target is `role:<R>` naming an UNKNOWN functional role ----
    # ORG-088 Plane-A (T-3761). A fail-closed WRITE-class vocabulary check over the
    # SAME known_role_ids() source the CLI file/retarget refusal uses. A role-pool
    # ticket whose R is not a declared functional role_id — or whose membership
    # vocabulary is unverifiable — can never resolve to eligible members, so FLAG it
    # (no silent pass). FLAG, not AUTO_FIX: the correct role is a judgment call, not a
    # mechanical rewrite. `role:<R>` is never target=operator, so this sits ahead of
    # the operator-specific IMPROPER classes below. Shape via ticket_purpose's
    # canonical ROLE_TARGET_RE (lazy import — the same idiom this module uses for its
    # other ticket_purpose references); the role VOCABULARY authority is
    # known_role_ids(), never duplicated.
    from tools.ticket_purpose import ROLE_TARGET_RE

    if isinstance(target, str) and ROLE_TARGET_RE.fullmatch(target):
        role = target.split(":", 1)[1]
        try:
            vocab = known_role_ids()
        except PersonaRoleMembershipError as exc:
            return LintResult(
                tid=tid,
                path=path,
                action=LintAction.FLAG,
                description=(
                    f"target={target!r} is a role-pool target but the functional-role "
                    f"membership vocabulary is unverifiable ({exc}); cannot confirm the "
                    f"role is known — fail-closed FLAG (ORG-088 Plane-A)"
                ),
            )
        if role not in vocab:
            return LintResult(
                tid=tid,
                path=path,
                action=LintAction.FLAG,
                description=(
                    f"target={target!r} names an UNKNOWN functional role {role!r}; "
                    f"not in known_role_ids() {sorted(vocab)} — a role-pool ticket must "
                    f"target a declared functional role (ORG-088 Plane-A)"
                ),
            )

    # A genuinely-improper operator+ratify ticket should bounce to its
    # originator (RETARGET) rather than just FLAG. But we can only bounce when
    # there is a distinct originator to bounce to: if `requester` is missing or
    # is operator itself, there is nowhere to send it → keep FLAG (the legacy
    # behavior) so HO still sees it in the digest.
    def _improper(description: str) -> LintResult:
        if requester and requester != "operator":
            return LintResult(
                tid=tid,
                path=path,
                action=LintAction.RETARGET,
                description=description,
                requester=requester,
            )
        return LintResult(
            tid=tid,
            path=path,
            action=LintAction.FLAG,
            description=description,
        )

    # ---- IMPROPER class 1: target=operator + intent=ratify + no ratifiable_files ----
    if target == "operator" and intent == "ratify" and not ratifiable_files:
        return _improper(
            "intent=ratify but ratifiable_files absent or empty; "
            "UNRATIFIABLE — cannot infer which file to ratify; "
            "originator must add ratifiable_files"
        )

    # ---- IMPROPER class 1b: target=operator + intent=action + body phrasing
    # suggests participation-waiting (T-0655). Examples: T-0618 ("the operator
    # will drive this once T-0615's speaker_sensitivity=0.3 is live in
    # production and a willing co-speaker is available"), T-0619 ("the operator
    # will drive this when he has time + mic variety available"). These are
    # calibration-tasks-waiting-on-operator's-body, NOT decision queue items.
    # Soft-FLAG with retarget suggestion — heuristic, may have false
    # positives; not auto-fixed.
    if target == "operator" and intent == "action":
        body_text = (_body or "").lower()
        # Patterns each chosen because they describe an event the ticket is
        # waiting on, not a decision the ticket asks for.
        participation_waiting_patterns = (
            "operator will drive",
            "when he has time",
            "once a willing co-speaker",
            "once a willing speaker",
            "when a co-speaker",
            "once x is available",
            "once y lands",
            "when x is available",
        )
        for pat in participation_waiting_patterns:
            if pat in body_text:
                return LintResult(
                    tid=tid,
                    path=path,
                    action=LintAction.FLAG,
                    description=(
                        f"intent=action body suggests participation-waiting "
                        f"({pat!r}) — this looks like a calibration-task gated "
                        f"on the operator's body/availability, not a decision queue "
                        f"item. Consider retargeting to the executing agent "
                        f"with blocked_by:event + unblock_when describing the "
                        f"physical/temporal gate."
                    ),
                )

    # ---- IMPROPER class 1c: target=operator + intent=noodle + body phrasing
    # suggests decision-gate (T-0655). Example: T-0554 ("DECISION-GATE
    # gating T-0552 ... OPTION A ... OPTION B ..."). noodle means "no
    # urgent action"; decision-gate means "the operator picks a path." Mismatch.
    # Heuristic; soft-FLAG (not auto-corrected).
    if target == "operator" and intent == "noodle":
        body_text = (_body or "").lower()
        decision_gate_patterns = (
            "decision-gate",
            "decision gate",
            "option a",
            "option b",
            "pick between",
            "choose between",
            "decide between",
        )
        for pat in decision_gate_patterns:
            if pat in body_text:
                return LintResult(
                    tid=tid,
                    path=path,
                    action=LintAction.FLAG,
                    description=(
                        f"intent=noodle body suggests a decision-gate "
                        f"({pat!r}) — noodle means 'no urgent action', but the "
                        f"body asks the operator to pick a path. Consider amending "
                        f"to intent=action (with action-request label) or "
                        f"intent=ratify (with ratifiable_files naming the "
                        f"option documents)."
                    ),
                )

    # ---- IMPROPER class 2: target=operator + intent=ratify + stale path ----
    # FAIL-OPEN (T-0556): each entry resolves to True (present) / False
    # (positively-confirmed-absent) / None (unresolvable). ONLY False is a
    # defect. True and None are both non-defects — never flag/bounce what we
    # cannot positively verify as absent (the T-0549 false-bounce property).
    # T-5014: widened from intent=ratify to EVERY target=operator ticket carrying
    # ratifiable_files — an intent=action ask with a dead attachment is just as
    # unopenable for the operator.
    if target == "operator" and ratifiable_files:
        for entry in ratifiable_files:
            if ":" in entry:
                _repo, _sep, rel_path = entry.partition(":")
            else:
                _repo, rel_path = "", entry
            status = ratifiable_path_status(_repo, rel_path, _root)
            if status is False:
                return _improper(
                    f"ratifiable_files entry {entry!r} positively confirmed absent "
                    f"at the canonical ref; STALE-PATH — originator must correct "
                    f"the path before operator can open it"
                )

    # ---- FLAG class: invalid `repos:` field vs the registry (T-0761) ----
    # Only checked when a repos: field is present; FAIL-OPEN if no registry.
    # FLAG (never auto-fix) — a repo-access declaration must not be mangled
    # mechanically; the originator corrects it (or the grant is added to
    # repos.yaml). repos: must be the inline-list form [a:ro, b:rw] (the
    # frontmatter parser does not support multiline lists).
    repos_field = fields.get("repos")
    if repos_field:
        registry = _load_repo_registry(_root)
        if registry is not None:
            err = validate_repos_field(repos_field, target, registry)
            if err:
                return LintResult(
                    tid=tid,
                    path=path,
                    action=LintAction.FLAG,
                    description=f"repos: field invalid — {err}",
                )

    # ---- FLAG class: substate: awaiting-merge / awaiting_merge_ref
    # consistency (T-4119) ----
    # The two fields always travel together: `awaiting_merge_ref` is only
    # meaningful alongside `substate: awaiting-merge`, and that substate is
    # meaningless without a ref naming WHAT it's awaiting. FLAG (never
    # AUTO_FIX) — the correct fix is either to run `ticket.py await-merge`
    # properly or to hand-correct a stale/drifted pair; a lint auto-fix
    # cannot know which the operator intended.
    substate = fields.get("substate")
    awaiting_merge_ref = fields.get("awaiting_merge_ref")
    if substate == "awaiting-merge" and not (
        isinstance(awaiting_merge_ref, str) and awaiting_merge_ref.strip()
    ):
        return LintResult(
            tid=tid,
            path=path,
            action=LintAction.FLAG,
            description=(
                "substate: awaiting-merge with no (or empty) awaiting_merge_ref; "
                "T-4119 requires a ref (e.g. <repo>#<pr> or <repo>:<branch>) "
                "naming the external merge this ticket is parked on"
            ),
        )
    if awaiting_merge_ref and substate != "awaiting-merge":
        return LintResult(
            tid=tid,
            path=path,
            action=LintAction.FLAG,
            description=(
                f"awaiting_merge_ref={awaiting_merge_ref!r} present but "
                f"substate is {substate!r} (not awaiting-merge); T-4119's ref "
                f"field is only meaningful alongside substate: awaiting-merge"
            ),
        )

    # ---- FLAG class: substate: awaiting-merge outside active/ (T-4119
    # REBOUND-1 finding #6) ----
    # The pair-consistency checks above only verify the two fields travel
    # together; they say nothing about WHERE the ticket lives. The substate
    # is only meaningful while the ticket is actively held (cmd_await_merge
    # refuses to set it anywhere else) — a well-formed pair sitting in
    # inbox/ or done/ means unclaim/retarget/drop failed to strip it (T-4119
    # closes those transition-time paths), or the pair was hand-edited into
    # an invalid state directly. Either way it is a leaked marker: FLAG
    # (never AUTO_FIX — same rationale as the pair checks above, a lint
    # auto-fix cannot know whether the correct repair is to strip the
    # marker or to re-claim the ticket first).
    if substate == "awaiting-merge" and path.parent.name != "active":
        return LintResult(
            tid=tid,
            path=path,
            action=LintAction.FLAG,
            description=(
                f"substate: awaiting-merge found in `{path.parent.name}/`, not "
                f"`active/`; T-4119's substate is only valid while the ticket "
                f"is actively held — this is a leaked marker (unclaim / "
                f"retarget / drop should have stripped it) and must be "
                f"hand-corrected"
            ),
        )

    # ---- FLAG class: duplicate `## Transition log` section header ----
    # A body-fill Edit that includes a second `## Transition log` header
    # after the template's built-in one produces a file that ticket.py
    # append_transition cannot detect or dedupe: the function only verifies
    # the header EXISTS, then appends the new entry at the tail. Subsequent
    # annotates keep growing the file past the duplicate header without
    # noticing. Caught only at read-time (or by this lint). FLAG, not
    # AUTO_FIX: the two "filed in inbox" lines are byte-identical (from a
    # freshly-filed ticket) but a hand-repair still wants the operator to
    # confirm nothing else got duplicated. Discovered 2026-09-21 (T-3851 +
    # T-3852 body-fills both did this).
    if text.count("## Transition log") > 1:
        return LintResult(
            tid=tid,
            path=path,
            action=LintAction.FLAG,
            description=(
                "body contains multiple `## Transition log` section headers "
                "(operator body-fill Edit likely duplicated the template's "
                "built-in section); repair via direct Edit to keep exactly one"
            ),
        )

    # ---- CLEAN ----
    return LintResult(
        tid=tid,
        path=path,
        action=LintAction.CLEAN,
        description="well-formed",
    )


# ---------------------------------------------------------------------------
# Auto-fix application.
# ---------------------------------------------------------------------------


def apply_auto_fix(path: Path, repo_root: Path | None = None) -> None:
    """Apply the mechanical auto-fix to a ticket file in place.

    Only valid for AUTO_FIX class tickets. Raises LintError if called on a
    ticket that is CLEAN or FLAG (judgment-needed — do not guess).

    Reserializes only the frontmatter (body preserved byte-for-byte).
    HERMES guard is the caller's responsibility (git_commit_fixes runs it).

    `repo_root` is forwarded to detect_malformations for ratifiable_files path
    resolution. Defaults to module-global REPO_ROOT when None.
    """
    result = detect_malformations(path, repo_root=repo_root)
    if result.action == LintAction.CLEAN:
        return  # idempotent — nothing to do
    if result.action in (LintAction.FLAG, LintAction.RETARGET):
        raise LintError(
            f"apply_auto_fix called on {result.action.value}-class ticket "
            f"{result.tid}: {result.description} — not a mechanical "
            f"frontmatter fix (FLAG: judgment; RETARGET: bounce to originator)"
        )

    # Re-parse to get fields + body.
    text = path.read_text(encoding="utf-8")
    fields, body = parse_front_matter(text)
    old_fields = _fm_snapshot(fields)  # T-4760: explicit old for the registry check

    target = fields.get("target", "")
    intent = fields.get("intent")
    ratifiable_files = fields.get("ratifiable_files") or []

    if target == "operator" and intent is None:
        # Class 1: set intent based on ratifiable_files presence.
        fields["intent"] = "ratify" if ratifiable_files else "action"

    elif intent is not None and target != "operator":
        # Class 2: drop intent — it is operator-only.
        del fields["intent"]


    new_text = _build_ticket_text(fields, body, old_fields=old_fields)
    path.write_text(new_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Scan loop.
# ---------------------------------------------------------------------------


def scan_tickets(
    tickets_root: Path,
    repo_root: Path | None = None,
) -> list[LintResult]:
    """Scan tickets/inbox/ + tickets/active/ under `tickets_root`.

    `repo_root` is threaded to detect_malformations for ratifiable_files
    path resolution. When None, detect_malformations falls back to module-global
    REPO_ROOT (T-0478: never rebind; worktree callers pass wt_path explicitly).

    Returns a list of LintResult for ALL tickets (CLEAN + AUTO_FIX + FLAG).
    """
    results: list[LintResult] = []
    for state in STATES_TO_LINT:
        d = tickets_root / state
        if not d.exists():
            continue
        for entry in sorted(d.iterdir()):
            if not entry.is_file():
                continue
            if not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            result = detect_malformations(entry, repo_root=repo_root)
            results.append(result)
    return results


# ---------------------------------------------------------------------------
# Serves-field advisory warnings (T-1103 / ORG-034).
#
# Soft nudge (WARN, never FLAG/AUTO_FIX) for inbox tickets missing a serves:
# arc-intent field. The lint warns the filer; it never blocks filing or causes
# downstream retarget/fix actions. active/ tickets are excluded — warns target
# filers, not in-flight workers; pre-ORG-034 tickets in active/ would generate
# noise without improving filer behaviour.
#
# 'goal-neutral-maintenance' is an explicitly valid value (housekeeping work);
# any non-empty serves: string suppresses the warn.
# ---------------------------------------------------------------------------


def warn_tickets(paths: list[Path]) -> list[LintResult]:
    """Return WARN results for inbox tickets missing a serves: field.

    Accepts an explicit list of paths (typically produced by scan_tickets or
    directly from the caller). Only emits WARN for tickets whose parent
    directory is named 'inbox'. active/ tickets are skipped.

    Never raises; parse errors on individual tickets silently skip that file
    (the malformation is already reported by detect_malformations / scan_tickets).
    """
    results: list[LintResult] = []
    for path in paths:
        if path.parent.name != "inbox":
            continue
        try:
            text = path.read_text(encoding="utf-8")
            fields, _body = parse_front_matter(text)
        except Exception:  # noqa: BLE001 — parse/IO failures: skip silently
            continue
        serves = fields.get("serves")
        if serves is not None and str(serves).strip():
            # serves: is present and non-empty — no nudge needed.
            continue
        results.append(LintResult(
            tid=path.stem,
            path=path,
            action=LintAction.WARN,
            description=(
                "inbox ticket missing serves: field (T-1103/ORG-034); "
                "add --serves '<arc-intent or goals.md key>' when filing; "
                "goal-neutral maintenance must name its asset with "
                "nearest_intent_ref: maintenance:<asset-ref>"
            ),
        ))
    return results


# ---------------------------------------------------------------------------
# Unattached-ask advisory (T-5014).
#
# BACKSTOP to the file-time forced choice in ticket.py (`--ratifiable-file` or
# `--no-file`): an OPEN target=operator ticket with no ratifiable_files whose
# Ask/Context names something path-like is probably citing a file the operator cannot
# open (T-4944: a draft under /home/<persona>/ with nothing attached). WARN only
# — legacy tickets must not red CI — and it fires whether or not the ticket
# carries a `no_file_reason` (the reason is audited, not trusted).
# ---------------------------------------------------------------------------

# Path-like tokens: a *.md/*.yaml/*.json filename, an absolute /home/ or /tmp/
# path, or a `repo:path/with.ext` reference.
_UNATTACHED_PATH_RE = re.compile(
    r"(?:"
    r"[\w./-]+\.(?:md|ya?ml|json)\b"
    r"|/(?:home|tmp)/[\w./-]+"
    r"|\b[a-z][\w-]*:[\w./-]*[\w-]/[\w./-]+"
    r")"
)
# An ADR/ORG id within <=6 words of "draft" or "Amendment" (either order).
_UNATTACHED_ADR_ID = r"(?:ADR|ORG)-\d+"
_UNATTACHED_DRAFT_WORD = r"(?:draft|amendment)s?"
_UNATTACHED_ADR_DRAFT_RE = re.compile(
    rf"\b{_UNATTACHED_ADR_ID}\b(?:\W+\w+){{0,6}}?\W+{_UNATTACHED_DRAFT_WORD}\b"
    rf"|\b{_UNATTACHED_DRAFT_WORD}\b(?:\W+\w+){{0,6}}?\W+{_UNATTACHED_ADR_ID}\b",
    re.IGNORECASE,
)


def _unattached_ask_evidence(text: str) -> str | None:
    """First path-like / draft-ADR token in `text`, or None."""
    m = _UNATTACHED_PATH_RE.search(text) or _UNATTACHED_ADR_DRAFT_RE.search(text)
    return " ".join(m.group(0).split()) if m else None


def warn_operator_unattached(paths: list[Path]) -> list[LintResult]:
    """WARN for open target=operator tickets with no ratifiable_files whose
    Ask/Context cites something path-like (T-5014). Never raises; unparseable
    tickets are skipped (already reported by detect_malformations)."""
    results: list[LintResult] = []
    for path in paths:
        if path.parent.name not in ("inbox", "active"):
            continue
        try:
            fields, body = parse_front_matter(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — parse/IO failures: skip silently
            continue
        if fields.get("target") != "operator" or fields.get("ratifiable_files"):
            continue
        # Scan only Ask + Context (the log and title are not the ask).
        m = re.search(r"^## Ask\s*$(.*?)(?=^## Transition log\s*$|\Z)", body, re.M | re.S)
        evidence = _unattached_ask_evidence(m.group(1) if m else "")
        if evidence is None:
            continue
        has_reason = bool(str(fields.get("no_file_reason") or "").strip())
        results.append(LintResult(
            tid=path.stem,
            path=path,
            action=LintAction.WARN,
            description=(
                f"target=operator ask names {evidence!r} but attaches no "
                f"ratifiable_files"
                + (" (no_file_reason is set, but the ask still reads as citing a file)"
                   if has_reason else "")
            ),
        ))
    return results


def warn_ticket_purposes(paths: list[Path]) -> list[LintResult]:
    """Return report-only structured-purpose findings for open tickets."""
    # Lazy imports preserve ticket_lint's longstanding isolated-fixture shape:
    # several lifecycle integration tests copy only ticket.py + ticket_lint.py
    # into a synthetic repository to exercise the real subprocess boundary.
    try:
        from tools.project_catalogue import load_project_records
        from tools.ticket_purpose import (
            PurposeError,
            load_ticket_purpose_index,
            validate_intent_link,
        )
    except ImportError:
        return []
    results: list[LintResult] = []
    if not paths:
        return results
    try:
        tickets_root = paths[0].parents[1]
        repo_root = tickets_root.parent
        purpose_index = load_ticket_purpose_index(tickets_root)
        project_records = load_project_records(repo_root / "projects" / "records")
    except (PurposeError, ValueError, OSError):
        return results
    for path in paths:
        purpose = purpose_index.get(path.stem)
        if purpose is None:
            continue
        for issue in validate_intent_link(
            purpose, purpose_index, project_records, frozenset()
        ):
            results.append(LintResult(
                tid=path.stem,
                path=path,
                action=LintAction.WARN,
                description=f"structured purpose {issue.code}: {issue.detail}",
            ))
    return results


# ---------------------------------------------------------------------------
# Floating-ratification detection (T-0784).
#
# Corpus-level check (NOT per-ticket): scan the ratifiable-artifact substrates
# (sop/ adr/ rulebook/) for documents whose CURRENT status is "awaiting
# ratification", then cross-reference each against the set of OPEN
# target=operator intent=ratify tickets. A pending artifact with no open ratify
# ticket pulling on it is an ORPHAN — report-only finding in the digest.
#
# Encodes architecturally (P1) the rule the operator directed (msg 2454/2455):
# every awaiting-ratification artifact must have an open ratify ticket — so a
# floating ratification becomes a lint finding instead of silently aging out.
# Extends the spirit of ORG-022 (file-tier ratifiable-pairing enforcement).
# ---------------------------------------------------------------------------

# Substrates that hold ratifiable artifacts (per ORG-015). README.md index
# files are excluded — they describe the draft→surface→ratify workflow in prose
# and are not themselves ratifiable artifacts (and would false-positive the
# fuzzy fallback path).
_RATIFICATION_SCAN_DIRS = (
    "sop/standard",
    "sop/remediation",
    "architecture/adr",
    "rulebook",
)

# Tolerant status-convention matchers. Status strings vary widely across the
# corpus ('Drafted DATE; awaiting ratification', 'Authored DATE awaiting
# the operator ratification', frontmatter `status:` values, `## Status` sections).
_AWAITING_RE = re.compile(r"\bawaiting\b", re.IGNORECASE)
_RATIF_RE = re.compile(r"ratif", re.IGNORECASE)            # ratify / ratification
_RATIFIED_RE = re.compile(r"\bratified\b", re.IGNORECASE)
# Guard against "NOT yet awaiting ratification" (a doc explicitly NOT yet in the
# queue — e.g. diagnosing-third-party-surface-issues): that is not an orphan.
_NEGATED_AWAITING_RE = re.compile(r"not\s+(?:yet\s+)?awaiting", re.IGNORECASE)
# An inline status declaration: `**Status:** ...`, `Status: ...`, `> Status: ...`.
_INLINE_STATUS_RE = re.compile(
    r"^\s*(?:>\s*)?(?:[*_]{1,2}\s*)?status(?:\s*[*_]{1,2})?\s*[:：]",
    re.IGNORECASE,
)
# A `## Status` (any heading level) section header — content on following lines.
_HEADING_STATUS_RE = re.compile(r"^\s*#{1,6}\s+status\s*$", re.IGNORECASE)


def _explicit_status_blocks(text: str) -> list[str]:
    """Return explicit status-context blocks in canonical-first priority order.

    (1) frontmatter `status:` value; (2) the first inline `**Status:**`/`Status:`
    line PLUS its wrapped continuation (until a blank line — catches multi-line
    status paragraphs); (3) the first `## Status` section's following paragraph.

    Empty list => the document declares no explicit status anywhere (callers
    then fall back to a fuzzy first-N-lines scan).
    """
    lines = text.splitlines()
    blocks: list[str] = []

    # Frontmatter boundary (if any).
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
            m = re.match(r"\s*status\s*:\s*(.+?)\s*$", lines[i], re.IGNORECASE)
            if m:
                blocks.append(m.group(1).strip().strip("\"'"))

    body = lines[body_start:]

    # First inline status line + wrapped continuation.
    for idx, ln in enumerate(body):
        if _INLINE_STATUS_RE.match(ln):
            chunk = [ln]
            for nxt in body[idx + 1:]:
                if not nxt.strip():
                    break
                chunk.append(nxt)
            blocks.append("\n".join(chunk))
            break

    # First `## Status` section heading → following non-blank paragraph.
    for idx, ln in enumerate(body):
        if _HEADING_STATUS_RE.match(ln):
            j = idx + 1
            while j < len(body) and not body[j].strip():
                j += 1
            chunk: list[str] = []
            while j < len(body) and body[j].strip():
                chunk.append(body[j])
                j += 1
            if chunk:
                blocks.append("\n".join(chunk))
            break

    return blocks


def _classify_status_block(block: str) -> str | None:
    """Classify one status-context block: 'pending' / 'ratified' / None.

    'pending'  — current status is awaiting ratification.
    'ratified' — current status is ratified.
    None       — the block declares a status unrelated to ratification (or the
                 awaiting phrase is negated).
    """
    if _NEGATED_AWAITING_RE.search(block):
        return None
    if _AWAITING_RE.search(block) and _RATIF_RE.search(block):
        return "pending"
    if _RATIFIED_RE.search(block):
        return "ratified"
    return None


def ratification_status(text: str) -> tuple[str, str] | None:
    """Resolve a document's CURRENT ratification status from its status context.

    Returns (state, excerpt) where state ∈ {'pending', 'ratified'}, or None when
    no ratification-relevant status is found. The canonical status is the FIRST
    explicit status declaration (frontmatter > inline > section) — body prose
    that merely mentions 'awaiting'/'ratified' elsewhere is NOT consulted, which
    avoids false positives on docs that discuss ratification but whose own status
    is settled (e.g. a Ratified ADR that references an awaiting sibling).

    Only when a document declares NO explicit status anywhere do we fall back to
    a fuzzy scan of the first 15 body lines (pending-only).
    """
    explicit = _explicit_status_blocks(text)
    if explicit:
        for block in explicit:
            verdict = _classify_status_block(block)
            if verdict is not None:
                return verdict, block.splitlines()[0].strip()
        return None

    # No explicit status declaration → fuzzy fallback (pending only).
    lines = text.splitlines()
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
    head = lines[body_start: body_start + 15]
    block = "\n".join(head)
    if _classify_status_block(block) == "pending":
        for ln in head:
            if _AWAITING_RE.search(ln) and _RATIF_RE.search(ln):
                return "pending", ln.strip()
        return "pending", head[0].strip() if head else ""
    return None


def collect_ratify_coverage(tickets_root: Path) -> set[str]:
    """Repo-relative artifact paths covered by an OPEN ratify ticket.

    Scans tickets/inbox/ + tickets/active/ (NOT done) for target=operator
    intent=ratify tickets and collects their ratifiable_files, stripping an
    optional `pantheon-config:` (or bare) repo prefix to a repo-relative path.
    Foreign-repo entries (revenue-project:..., etc.) are ignored — those artifacts
    do not live in this scan's directory set.
    """
    covered: set[str] = set()
    for state in STATES_TO_LINT:
        d = tickets_root / state
        if not d.exists():
            continue
        for entry in sorted(d.iterdir()):
            if not entry.is_file() or not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            try:
                fields, _body = parse_front_matter(entry.read_text(encoding="utf-8"))
            except TicketError:
                continue
            if fields.get("target") != "operator" or fields.get("intent") != "ratify":
                continue
            for rf in (fields.get("ratifiable_files") or []):
                if ":" in rf:
                    repo, _sep, rel = rf.partition(":")
                    if repo not in ("", "pantheon-config"):
                        continue
                    covered.add(rel)
                else:
                    covered.add(rf)
    return covered


def detect_orphan_ratifications(
    repo_root: Path,
    tickets_root: Path,
) -> list[OrphanRatification]:
    """Find pending-ratification artifacts with no open ratify ticket (T-0784).

    Scans the ratifiable substrates for documents whose current status is
    'awaiting ratification' and subtracts the set covered by an open
    target=operator intent=ratify ticket. The remainder are orphans — report-only
    findings. Side-effect-free except read I/O.
    """
    covered = collect_ratify_coverage(tickets_root)
    orphans: list[OrphanRatification] = []
    for rel_dir in _RATIFICATION_SCAN_DIRS:
        base = repo_root / rel_dir
        if not base.is_dir():
            continue
        for md in sorted(base.rglob("*.md")):
            if md.name == "README.md":
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            verdict = ratification_status(text)
            if verdict is None or verdict[0] != "pending":
                continue
            rel_path = str(md.relative_to(repo_root))
            if rel_path in covered:
                continue
            orphans.append(
                OrphanRatification(rel_path=rel_path, status_excerpt=verdict[1])
            )
    return orphans


# ---------------------------------------------------------------------------
# Dependency-graph structural checks (T-0975).
#
# Corpus-level (NOT per-ticket): build the depends_on graph across all OPEN
# tickets (inbox/ + active/ + blocked/) and detect two defect classes the
# per-file frontmatter lint cannot see:
#   1. CYCLES — A -> B -> ... -> A among open tickets: an unbreakable deadlock.
#      Found live 2026-06-08 (T-0367 <-> T-0470, a deploy-carrier wrongly gated
#      on its feature parent). Neither the frontmatter lint nor a done/missing
#      dep scan catches these.
#   2. STALE done-deps — an open ticket depends_on a resolved (done/) ticket
#      that ORG-013 cascade-unblock failed to strip (T-0971 fixes forward-only).
# Both are report-only in the digest; a human resolves them. Side-effect-free.
# ---------------------------------------------------------------------------

# "Open" states that can carry a live depends_on gate. STATES_TO_LINT is only
# (inbox, active); the dep graph additionally needs blocked/ (open, can have
# deps) and done/ (to resolve whether a dep is already satisfied/stale).
_DEP_GRAPH_OPEN_STATES = ("inbox", "active", "blocked")
_DEP_GRAPH_ALL_STATES = ("inbox", "active", "blocked", "done")


def _ticket_depends_on(path: Path) -> list[str]:
    """Return the depends_on ticket ids for one ticket file.

    Reads ONLY the depends_on line from the frontmatter block — deliberately
    does NOT run full parse_front_matter validation. A ticket missing some
    OTHER required field (e.g. `created`) is still analyzed for its dep edges;
    that malformation is flagged separately by the per-file lint. Robust to
    depends_on rendered as a list or a raw string (T-NNNN regex over the value).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    fm = m.group(1) if m else text
    dm = re.search(r"^depends_on:\s*(.+)$", fm, re.MULTILINE)
    return re.findall(r"T-\d+", dm.group(1)) if dm else []


def _ticket_substate(path: Path) -> str | None:
    """Return the `substate:` frontmatter value for one ticket file, or None.

    T-3440 / ORG-089: same light-touch frontmatter read as `_ticket_depends_on`
    (does NOT run full parse_front_matter). Used to distinguish a DROPPED done
    ticket from a shipped one so a dropped dep gets its own finding rather than
    being mislabeled a stale-resolved dep.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    fm = m.group(1) if m else text
    sm = re.search(r"^substate:\s*(.+)$", fm, re.MULTILINE)
    return sm.group(1).strip() if sm else None


def detect_dep_graph_issues(tickets_root: Path) -> list[DepGraphFinding]:
    """Corpus-level depends_on-graph scan: cycles + stale done-deps.

    Report-only (no mutation). Returns stale-done-dep findings first (sorted),
    then one finding per distinct cycle.
    """
    state: dict[str, str] = {}          # tid -> directory/state
    deps: dict[str, list[str]] = {}     # tid -> depends_on (open tickets only)
    dropped: set[str] = set()           # tid -> done/ but substate: dropped (T-3440)
    for st in _DEP_GRAPH_ALL_STATES:
        d = tickets_root / st
        if not d.exists():
            continue
        for entry in sorted(d.iterdir()):
            if not entry.is_file() or not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            tid = entry.stem
            state[tid] = st
            if st == "done" and _ticket_substate(entry) == "dropped":
                dropped.add(tid)
            if st in _DEP_GRAPH_OPEN_STATES:
                deps[tid] = _ticket_depends_on(entry)

    # T-3440 / ORG-089: a dep is SHIPPED (satisfies the gate) iff it is in done/
    # AND not dropped. A dropped done/ dep is deliberately NOT satisfied (D-N).
    def _shipped(dep: str) -> bool:
        return state.get(dep) == "done" and dep not in dropped

    findings: list[DepGraphFinding] = []

    # (0) Dropped deps (T-3440 / ORG-089): an OPEN ticket gated on a dep that is
    # in done/ with `substate: dropped`. Distinct from stale-done-dep: a dropped
    # dep is NOT satisfied, so the gate is LIVE (not self-healing) — the dep must
    # be reopened+re-earned+shipped, or pruned with rationale. Its own finding
    # kind so the digest does not mislabel it "stale-resolved".
    for tid in sorted(deps):
        for dep in deps[tid]:
            if dep in dropped:
                findings.append(DepGraphFinding(
                    kind="dropped_dep",
                    detail=(
                        f"{tid} depends_on {dep}, which is DROPPED (in done/ but "
                        "substate: dropped) — a dropped bet does NOT satisfy the "
                        "dependency; reopen+re-earn+ship it or prune the dep with "
                        "rationale"
                    ),
                ))

    # (1) Stale done-deps: an open ticket whose depends_on is ENTIRELY done.
    #
    # T-2396 — the all-done gate is load-bearing, not a tightening for neatness.
    # ORG-013 cascade-unblock guards on _all_other_deps_shipped() and DELIBERATELY
    # skips a partial resolve ("multi-dep partial-resolve; wait for the rest");
    # T-2177 then prunes EVERY done dep at once when the last one lands. So a done
    # dep sitting beside a still-open dep is expected, self-healing state with
    # nothing to fix — flagging it produced permanent unactionable entries (live:
    # T-2089, T-2219) in the same digest section that carries dependency CYCLES,
    # i.e. real deadlocks, and taught the reader to skip it. Worse, the old detail
    # string blamed cascade-unblock for a case it correctly declined, which cost a
    # session tracing a ticket.py bug that does not exist.
    #
    # `any(... != "done")` treats an UNKNOWN dep id as not-done, so it suppresses
    # too. That is deliberate: the cascade is fail-closed on ids it cannot reason
    # about, so it would not have fired there either. A dangling dep id is a
    # different defect and belongs to a different check.
    for tid in sorted(deps):
        tid_deps = deps[tid]
        # T-3440: use SHIPPED (done AND not dropped), not bare done/. A dropped
        # dep is handled by the dropped_dep finding above and must NOT trip the
        # "every dep is done, depends_on should be empty" stale-resolved case.
        if not tid_deps or any(not _shipped(d) for d in tid_deps):
            continue
        for dep in tid_deps:
            findings.append(DepGraphFinding(
                kind="stale_done_dep",
                # Attribution stays deliberately open. Every dep being done means
                # depends_on SHOULD be empty, but the two ways to get here differ:
                # cascade-unblock missed it, or the dep was added onto an
                # already-resolved ticket (which the cascade never gets a chance to
                # clear, since it only fires on resolve). Naming one would repeat
                # this check's old mistake of asserting a cause it cannot see.
                detail=(f"{tid} depends_on {dep}, which is DONE — every dep on "
                        f"{tid} is done, so depends_on should be empty (cascade "
                        "missed it, or it was added post-resolve)"),
            ))

    # (2) Cycles among OPEN tickets. An edge A->B is a LIVE blocker only if B is
    #     also open (a done dep is satisfied, not a deadlock edge). Standard DFS
    #     three-color cycle detection; each distinct cycle is canonicalised by
    #     its rotation-minimum so it is reported once regardless of entry point.
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in deps}
    seen_cycles: set[tuple[str, ...]] = set()

    def _live_edges(node: str) -> list[str]:
        return [m for m in deps.get(node, []) if m in deps]

    def _dfs(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in _live_edges(node):
            if color.get(nxt) == GRAY:
                cyc = stack[stack.index(nxt):]
                pivot = min(range(len(cyc)), key=lambda i: cyc[i])
                canon = tuple(cyc[pivot:] + cyc[:pivot])
                if canon not in seen_cycles:
                    seen_cycles.add(canon)
                    findings.append(DepGraphFinding(
                        kind="cycle",
                        detail=("dependency cycle (deadlock): "
                                + " -> ".join(canon) + f" -> {canon[0]}"),
                    ))
            elif color.get(nxt) == WHITE:
                _dfs(nxt, stack)
        stack.pop()
        color[node] = BLACK

    for tid in sorted(deps):
        if color[tid] == WHITE:
            _dfs(tid, [])

    return findings


# ---------------------------------------------------------------------------
# T-2841 / ORG-065 BT1: generalized typed-edge graph (warn-only model layer).
#
# Extends the depends_on-only graph-builder above to the FULL typed edge set
# named in ORG-065 §3.2/3.6, per the ADR's "What this builds on" — this is
# explicitly the graph-builder to generalize, not a parallel implementation.
# `detect_dep_graph_issues` above is UNCHANGED (still the depends_on-only,
# open-ticket-liveness READINESS check its existing callers/tests depend on);
# this section adds the separate, broader ACYCLICITY-as-graph-integrity layer
# (ORG-065 §3.6: "graph integrity, not closure gating").
#
# Node model: (kind, id) pairs. `ticket` and `obligation` are the only kinds
# this BT1 builder actually produces edges for; `adr`/`commit`/`test`/
# `evidence`/`goal` are REGISTERED identities (kind is a first-class axis, not
# hardcoded to "ticket") with no wiring yet — `goal` in particular is the
# reserved, semantics-free landing zone for the goal-distance work named on
# T-2841's own transition log (ORG-061-sequence).
#
# Scope note (state-independence): unlike `detect_dep_graph_issues`'s
# depends_on check — which deliberately treats a DONE dep as a dead end
# (satisfied, no longer a live deadlock edge; see T-2396 above) — this
# generalized builder considers tickets in ANY state (inbox/active/done).
# parent_of/implements/verifies/supersedes are lineage-permanent relationships
# that do not become "satisfied and therefore inert" the way a blocking gate
# does; a cycle through one of them is a graph defect regardless of whether
# the tickets on it have since resolved. depends_on is included in this
# broader scan on the same any-state basis for graph-integrity purposes —
# a narrower reading (open-only) is left to the existing, unchanged function
# above, which readiness/cascade code actually depends on.
#
# Domain note (why parent_of is exempted from the union but everything else
# is NOT): a corpus wet-run against the live tickets/ tree surfaced 15
# instances of a widespread, entirely legitimate idiom — a parent ticket
# `depends_on` its own child (e.g. T-0173 depends_on T-0191; T-0191's
# `parent_ticket` is T-0173, correctly recording lineage). A first pass here
# unioned ALL five edges (depends_on/implements/verifies/supersedes/
# parent_of) into one cycle check, which flagged every one of those 15 as a
# "cycle". A second pass over-corrected the other way — checking all FOUR
# edge types as fully independent domains — which is wrong for a different
# reason: it misses real defects. implements/verifies/supersedes/depends_on
# each carry the exact failure mode §3.6 names them for ("a depends_on loop
# makes nothing ready", "an obligation/verifies loop makes closure-validation
# non-terminating", "a supersedes loop corrupts replacement history") and a
# MIXED loop across them is just as real a problem — e.g. A implements B,
# B depends_on A is still a ticket whose implementation-obligation and its
# gate point at each other, and isolating implements from depends_on would
# leave that uncaught. `parent_of` is the ONE genuine outlier, and the ADR
# text itself draws the line: "parent_ticket — Lineage back-reference only.
# **no cascade, no validation.**" (§3.2) — the identical "carries no
# behavior" framing the ADR uses to exempt `informs` ("NEVER gates"). A
# parent_of-only cycle (A's parent is B, B's parent is A) is still nonsense
# lineage and stays checked — it is just never unioned with the edges that
# actually gate something.
#
# ACYCLICITY_DOMAINS below is that two-way split: {parent_of} as its own,
# separately-checked, never-unioned domain (the ONE exemption with corpus
# evidence behind it — the 15 falsifying instances, plus the ADR's own text),
# and {depends_on, implements, verifies, supersedes} as one unioned domain.
# Be precise about what backs the union choice: a general cycle detector run
# over a graph finds every cycle IN that graph by construction, mixed-type or
# not — that part isn't a claim needing evidence, it's the algorithm. What IS
# a judgment call, not corpus-validated, is which four fields belong in that
# one graph in the first place: only `depends_on` has a live, exercised
# insertion path today; `implements`/`verifies`/`supersedes` have ZERO live
# ticket usage, so there is no wet-run evidence either confirming or
# falsifying their union with depends_on the way there was for parent_of.
# The union rests on the ADR's own text (§3.6's rationale is single-type in
# every failure mode it names, but names no cross-type EXEMPTION either — the
# absence of a stated exemption is not the same evidence class as parent_of's
# stated one) plus the test suite locking in the two concrete blind spots a
# narrower split would have left uncaught (the 2-cycle and 3-hop cases in
# tools/test_typed_edges.py). Reporting/lineage uses of build_typed_edge_graph
# (e.g. a future BT3 query surface) are unaffected — they want the full union
# and still get it via the default `edge_fields=None`.
# ---------------------------------------------------------------------------

NodeId = tuple[str, str]  # (kind, id) — kind in NODE_KINDS

# T-2841 / ORG-065 §3.1: the node-kind axis. Only "ticket" and "obligation"
# are populated by this BT1 builder; the rest are registered, semantics-free
# extensibility hooks (see module note above). `goal` carries NO behavior in
# this phase — do not add goal logic against this constant without a new ADR.
NODE_KINDS = ("adr", "obligation", "ticket", "commit", "test", "evidence", "goal")

# ORG-065 §3.6: the gating/structural ticket-frontmatter fields whose outbound
# edges participate in the acyclicity invariant, taken together (the FULL
# union — used by build_typed_edge_graph's reporting-mode default and by the
# dangling-target scan). `informs` is deliberately excluded — advisory-only,
# acyclicity-EXEMPT per the ADR. Mirrors ticket.py's `_GATING_LIST_FIELDS`
# (kept independently — ticket_lint must not import a private name back out
# of ticket.py for this). "parent_of" is a synthetic field name understood by
# build_typed_edge_graph/edge_fields below — it is derived from the scalar
# `parent_ticket` frontmatter key, not a list field.
_GATING_EDGE_FIELDS = ("depends_on", "implements", "verifies", "supersedes", "ratifies", "parent_of")

# T-3476 / ORG-065 Call 1: `ratifies` joins the union domain below (NOT its
# own exempt domain like parent_of) — a mixed ratifies+implements/verifies/
# supersedes/depends_on loop is a real graph-integrity defect by the same
# §3.6 reasoning already applied to the original four; ratifies has no
# falsifying wet-run evidence the way parent_of's 15-instance idiom did, so
# it defaults to the union rather than getting a bespoke exemption. This is
# additive to the historical wet-run-evidence note above (which predates
# `ratifies` and is left as-written — that note documents specific corpus
# evidence about the ORIGINAL four fields, not a claim this addition should
# be retrofitted into).
#
# The TWO acyclicity domains — see the module note above for the rationale.
# `parent_of` is NOT exempt the way `informs` is (informs participates in
# ZERO cycle checks; a parent_of-only cycle — A's parent is B, B's parent is
# A — is still nonsense lineage and IS checked) — it is only exempt from
# being UNIONED with the other three, which is the ADR's own "no cascade, no
# validation" framing for that one field. `depends_on`/`implements`/
# `verifies`/`supersedes` share one unioned domain since a MIXED loop across
# them is a real defect, not a false positive. Cycle detection runs once per
# domain and never unions across domains.
ACYCLICITY_DOMAINS: tuple[tuple[str, ...], ...] = (
    ("parent_of",),
    ("depends_on", "implements", "verifies", "supersedes", "ratifies"),
)

_TICKET_ID_RE = re.compile(r"^T-\d{4,}$")  # T-3215


def edge_node_id(raw: str) -> NodeId:
    """Classify a raw typed-edge target string into a (kind, id) node.

    `T-NNNN` -> ("ticket", raw); anything else is treated as an obligation id
    (already shape-validated upstream, at every insertion point, as
    `ADR-NNN#E<k>[.C<m>]` / `ORG-NNN#E<k>[.C<m>]` by
    ticket._parse_edge_targets — this function does not re-validate shape).
    """
    if _TICKET_ID_RE.fullmatch(raw):
        return ("ticket", raw)
    return ("obligation", raw)


def build_typed_edge_graph(
    tickets_root: Path,
    edge_fields: tuple[str, ...] | None = None,
) -> dict[NodeId, list[NodeId]]:
    """Corpus-wide adjacency map over a set of typed edges.

    `edge_fields` restricts which edges contribute: each entry is either a
    list-typed frontmatter field name (depends_on/implements/verifies/
    supersedes) or the synthetic name "parent_of" for the edge derived from
    the scalar `parent_ticket` field. Default (None) is the FULL union —
    `_GATING_EDGE_FIELDS` — for reporting/lineage purposes (the node universe
    the dangling-target scan and any future lineage query need). Cycle
    detection MUST pass exactly one domain from ACYCLICITY_DOMAINS at a time
    — see the module note above on why the union is NOT used for cycle
    checking. `informs` is never accepted here (acyclicity/reporting-exempt
    by construction — it has no entry in _GATING_EDGE_FIELDS to begin with).

    Scans every ticket in ANY state (inbox/active/done — see module note on
    why this differs from `detect_dep_graph_issues`'s open-only scope). A
    ticket file that fails to parse is skipped (best-effort — matches
    `_ticket_depends_on`'s tolerance of a ticket malformed in some OTHER
    field).
    """
    if edge_fields is None:
        edge_fields = _GATING_EDGE_FIELDS
    graph: dict[NodeId, list[NodeId]] = {}
    for st in STATES:
        d = tickets_root / st
        if not d.exists():
            continue
        for entry in sorted(d.iterdir()):
            if not entry.is_file() or not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            tid = entry.stem
            try:
                fields, _body = parse_front_matter(entry.read_text(encoding="utf-8"))
            except (TicketError, OSError):
                continue
            src: NodeId = ("ticket", tid)
            out: list[NodeId] = []
            for field_name in edge_fields:
                if field_name == "parent_of":
                    parent = fields.get("parent_ticket")
                    if isinstance(parent, str) and parent.strip():
                        out.append(("ticket", parent.strip()))
                    continue
                for raw in (fields.get(field_name) or []):
                    out.append(edge_node_id(str(raw)))
            graph.setdefault(src, []).extend(out)
    return graph


def find_path(graph: dict[NodeId, list[NodeId]], start: NodeId, goal: NodeId) -> list[NodeId] | None:
    """BFS shortest path start -> goal over `graph`'s adjacency; None if
    unreachable. The returned path includes both endpoints. A node absent
    from `graph` (a leaf, e.g. an obligation with no outbound edges) is a
    valid start with zero neighbours."""
    if start == goal:
        return [start]
    seen: set[NodeId] = {start}
    queue: list[list[NodeId]] = [[start]]
    head = 0
    while head < len(queue):
        path = queue[head]
        head += 1
        node = path[-1]
        for nxt in graph.get(node, []):
            if nxt == goal:
                return path + [nxt]
            if nxt not in seen:
                seen.add(nxt)
                queue.append(path + [nxt])
    return None


def _find_all_cycles(graph: dict[NodeId, list[NodeId]]) -> list[list[NodeId]]:
    """Three-color DFS cycle enumeration, generalized over any NodeId graph.

    Same algorithm as `detect_dep_graph_issues`'s inline depends_on version
    (canonicalised by rotation-minimum so each distinct cycle is reported
    once), lifted to work over the typed (kind, id) node space.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[NodeId, int] = {n: WHITE for n in graph}
    cycles: list[list[NodeId]] = []
    seen_canon: set[tuple[NodeId, ...]] = set()

    def _dfs(node: NodeId, stack: list[NodeId]) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in graph.get(node, []):
            state = color.get(nxt, WHITE)
            if state == GRAY:
                idx = stack.index(nxt)
                cyc = stack[idx:]
                pivot = min(range(len(cyc)), key=lambda i: cyc[i])
                canon = tuple(cyc[pivot:] + cyc[:pivot])
                if canon not in seen_canon:
                    seen_canon.add(canon)
                    cycles.append(list(canon))
            elif state == WHITE:
                color.setdefault(nxt, WHITE)
                _dfs(nxt, stack)
        stack.pop()
        color[node] = BLACK

    for n in sorted(graph, key=str):
        if color.get(n) == WHITE:
            _dfs(n, [])
    return cycles


_ADR_ELEMENT_ID_RE = re.compile(r"^E\d+(?:\.C\d+)?$")


@dataclass
class ElementsLintFinding:
    """A malformation in one ADR's `elements:` frontmatter block (T-2841 /
    ORG-065 BT1; report-only, WARN-tier).

    kinds:
      - "malformed_id" — an element or clause id is empty or does not match
        `E\\d+(\\.C\\d+)?`.
      - "duplicate_id" — the same id (element or clause) appears more than
        once within one ADR's `elements:` block.
    """
    kind: str
    detail: str


def _register_element_id(
    local_id: str,
    adr_id: str,
    seen: dict[str, int],
    known_ids: set[str],
    findings: list[ElementsLintFinding],
) -> None:
    if not local_id:
        findings.append(ElementsLintFinding(
            kind="malformed_id",
            detail=f"{adr_id}: elements: block has an entry with no id",
        ))
        return
    if not _ADR_ELEMENT_ID_RE.fullmatch(local_id):
        findings.append(ElementsLintFinding(
            kind="malformed_id",
            detail=f"{adr_id}: elements: id {local_id!r} does not match E\\d+(.C\\d+)?",
        ))
        return
    seen[local_id] = seen.get(local_id, 0) + 1
    if seen[local_id] > 1:
        findings.append(ElementsLintFinding(
            kind="duplicate_id",
            detail=f"{adr_id}: duplicate elements: id {local_id!r}",
        ))
    known_ids.add(f"{adr_id}#{local_id}")


# T-3477 / ORG-065 Call 2: cross-repo ADR corpus for Echo (echo-prismatic),
# read-only per the operator's T-3206 (TG 6734) ruling — elements: blocks stay in
# echo-prismatic, walked from here, never transcribed into Pantheon-side
# ADRs. Mirrors ticket_grooming.py's `_default_adr_dirs()` convention (same
# absolute path, not derived from REPO_ROOT since echo-prismatic is a
# sibling repo, not a subdirectory) — duplicated here rather than imported,
# to avoid a ticket_lint<->ticket_grooming import cycle (ticket_grooming
# already imports FROM ticket_lint). Scoped to the 200+ agent-authored range
# in echo-prismatic/adr/ only; the 001-199 vault range lives in a DIFFERENT
# directory (`architecture/Supplementary Material/ADRs/`) with a stronger
# "never modify directly" protection tier and is deliberately NOT included
# here (T-3477 plan, Open Q1 — permanently excluded pending a vault-ADR
# cross-repo policy decision).
#
# T-3495: this repo's OWN production `_adr_dirs` construction (the `_do_lint`
# and `main --dry-run` call sites below) no longer UNIONS this directory in —
# recovery (claude-fallback), the sole hourly cron runner, cannot read it
# (EACCES), which made T-3477's echo obligation validation vacuous on the
# only account that ever ran it. The union now lives exclusively in
# `tools/echo_corpus_lint.py`, a companion unit run on the `claude` account
# (native read access). This constant stays here (not moved) so that wrapper
# can import it directly rather than re-declaring the path a third place.
_ECHO_PRISMATIC_ADR_DIR = Path("/home/claude/echo-prismatic/adr")

# T-3495: numeric-ADR-id discriminator for "does this typed-edge target (or
# finding detail string) belong to the echo-prismatic corpus". `\d+` (not a
# fixed 3-digit width) captures the FULL digit run, so the comparison is a
# real numeric >=200 test, not a string-prefix match — deliberately
# open-ended above 200 (a hypothetical future ADR-2345 is still >=200 and
# still recognized) rather than hardcoding an upper bound at 299.
#
# Current-fact: echo-prismatic/adr/ is the 200+ agent-authored range only
# (see the module note above). The pantheon-config vault's 001-199 range is
# a DIFFERENT, today-permanently-excluded corpus. FUTURE EXTENSION SURFACE
# (the operator TG 9250 / T-3485 close-noodle): the T-1809/T-1483 vault-ADR
# elements-mint arc is unblocked and will eventually add cross-repo
# obligation edges targeting vault-range 001-199 ADRs — when that lands,
# this numeric->=200 predicate stops being sufficient (a path-based "target
# maps to a file under echo-prismatic/..." cut would be the more robust
# replacement, but that's a larger change than this ticket's scope). No code
# change here yet — this comment is the anchor for that future reader.
_ECHO_ADR_ID_TOKEN_RE = re.compile(r"ADR-(\d+)(?!\d)")
_ECHO_ADR_MIN_NUM = 200


def is_echo_scoped_id(text: str) -> bool:
    """True if `text` names an ADR-<N> id (N >= 200) anywhere — the
    echo-prismatic agent-authored range (T-3495; see the module note above
    `_ECHO_PRISMATIC_ADR_DIR` for the range's provenance and its future-
    extension caveat). Used both to exempt echo-targeting typed-edges from
    recovery's dangling-target check (this module no longer scans the echo dir —
    see `_ECHO_PRISMATIC_ADR_DIR`'s note) and, imported into
    `tools/echo_corpus_lint.py`, to filter that companion's corpus-wide scan
    down to the echo-scoped subset. Operates on ANY input string, not just a
    bare edge target — `TypedGraphFinding`/`ElementsLintFinding` carry no
    structured target field (kind + rendered `detail` string only), and
    every finding shape scan_adr_elements / detect_typed_graph_issues /
    detect_unverified_elements emit interpolates the raw id verbatim into
    `detail` — so a substring search here is sufficient for both uses."""
    return any(
        int(n) >= _ECHO_ADR_MIN_NUM for n in _ECHO_ADR_ID_TOKEN_RE.findall(text)
    )


def _normalize_adr_dirs(adr_dir: Path | Iterable[Path]) -> list[Path]:
    """Normalize the `adr_dir` parameter shared by scan_adr_elements /
    detect_typed_graph_issues / detect_unverified_elements to a list of
    Paths. Accepts a single bare Path (this repo's own production call
    sites, pre-T-3477 and again post-T-3495 — see `_ECHO_PRISMATIC_ADR_DIR`'s
    note) OR an iterable of Paths (T-3477: introduced for the cross-repo
    union of this repo's architecture/adr/ + echo-prismatic's adr/; T-3495:
    that union is now exercised by `tools/echo_corpus_lint.py`'s companion
    scan on the claude account, not by this module's own production
    pipeline). A bare Path is the common
    case checked first via isinstance rather than a duck-typed
    hasattr/try-except — Path itself is iterable-by-character-adjacent-API-
    surface-free (no __iter__), so this is unambiguous."""
    if isinstance(adr_dir, Path):
        return [adr_dir]
    return list(adr_dir)


def _adr_dir_scannable(one_dir: Path, *, warn: bool = False) -> bool:
    """True if *one_dir* exists AND is accessible enough to scan; False if it is
    missing OR inaccessible — never raises.

    `Path.exists()` RAISES `PermissionError`/`OSError` on EACCES (rather than
    returning False) when a parent directory is 700-mode. That is exactly the
    T-3477 cross-repo case: `_ECHO_PRISMATIC_ADR_DIR` is hardcoded under
    `/home/claude/`, but this hourly lint runs as the recovery (`claude-fallback`)
    account, which cannot stat into claude's 700-mode home — so a bare
    `one_dir.exists()` crashed the whole run (reference: the pathlib-EACCES
    gotcha; `ticket_grooming.py` already guards the identical pattern with
    try/except OSError).

    An inaccessible ADR dir is treated exactly like a missing one (skip). But
    when `warn=True` the EACCES case is WARNED to stderr — NOT silent — because
    recovery is the *sole* hourly lint runner: a silently-skipped echo corpus would
    make every echo cross-repo obligation check vacuously pass, hiding the
    coverage gap (T-3477's echo scan cannot run under an account that can't read
    the clone)."""
    try:
        return one_dir.exists()
    except OSError as exc:
        if warn:
            print(
                f"[adr-scan] WARNING: ADR dir {one_dir} is INACCESSIBLE to this "
                f"account ({type(exc).__name__}: {exc}); skipping it. Any "
                f"cross-repo ADR obligations there are UNVALIDATED this run — "
                f"T-3477 hardcoded echo-prismatic's adr/ under /home/claude, "
                f"unreadable to the recovery lint runner.",
                file=sys.stderr,
            )
        return False


def scan_adr_elements(
    adr_dir: Path | Iterable[Path],
) -> tuple[set[str], list[ElementsLintFinding]]:
    """Parse every ADR's optional `elements:` frontmatter block (ORG-065 §3.1
    obligation identity) across one or more ADR directories. Reuses
    `adr_reeval_walk`'s YAML-frontmatter helpers (the one existing precedent
    for a structured, machine-readable ADR frontmatter key — see ORG-065
    "What this builds on" #5) rather than hand-rolling a second parser.

    `adr_dir` accepts a single Path (this repo's own architecture/adr/,
    every pre-T-3477 call site) OR an iterable of Paths (T-3477: this repo's
    architecture/adr/ UNIONED with echo-prismatic's adr/, so a cross-repo
    `implements:` target resolves against both corpora as one obligation-id
    universe). `_adr_id_from`'s filename-prefix fallback (adr_reeval_walk.py)
    already handles Echo's `ADR-<NNN> — <title>.md` convention (spaces,
    em-dash) identically to this repo's `ORG-<NNN>-<title>.md` — no new
    filename-parsing logic needed for the second directory.

    Returns (known_ids, findings):
      - known_ids: the set of fully-qualified obligation ids across the
        whole corpus (all directories), e.g. "ORG-065#E1", "ADR-232#E3.C1"
        — the target-existence universe for the implements/verifies/
        supersedes/informs/ratifies dangling-target check below.
      - findings: malformed/duplicate id defects (see ElementsLintFinding).

    A `README.md` in any `adr_dir` entry is skipped (index prose, not an
    ADR). A missing directory or an ADR with no `elements:` block yields
    nothing for that file — the block is entirely optional
    (resolved-decision 2) and a not-yet-elementized Echo ADR (the real
    shape of most Echo ADRs today — no YAML frontmatter at all) is silently
    skipped the same way, not an error.
    """
    known_ids: set[str] = set()
    findings: list[ElementsLintFinding] = []
    for one_dir in _normalize_adr_dirs(adr_dir):
        if not _adr_dir_scannable(one_dir, warn=True):
            continue
        for path in sorted(one_dir.glob("*.md")):
            if path.name.upper().startswith("README"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            fm_block, _body = _split_frontmatter(text)
            fm = _load_yaml(fm_block, source=path.name)
            elements = fm.get("elements")
            if not isinstance(elements, list):
                continue
            adr_id = _adr_id_from(fm, path)
            seen: dict[str, int] = {}
            for el in elements:
                if not isinstance(el, dict):
                    continue
                _register_element_id(
                    str(el.get("id", "")).strip(), adr_id, seen, known_ids, findings,
                )
                clauses = el.get("clauses")
                if isinstance(clauses, list):
                    for cl in clauses:
                        if isinstance(cl, dict):
                            _register_element_id(
                                str(cl.get("id", "")).strip(), adr_id, seen, known_ids, findings,
                            )
    return known_ids, findings


@dataclass
class TypedGraphFinding:
    """A structural defect in the GENERALIZED typed-edge graph (T-2841 /
    ORG-065 BT1). Report-only — this phase is warn-only for the corpus-wide
    scan (flipping to gating is BT5); cycle REJECTION already happens at
    insertion time in ticket.py, so a "cycle" finding here means a
    pre-existing / hand-edited defect the insertion guard never saw.

    kinds:
      - "cycle" — the gating/structural edge set (depends_on/implements/
        verifies/supersedes/parent_of) contains a cycle. The graph-integrity
        defect named in ORG-065 §3.6.
      - "dangling_target" — an implements/verifies/supersedes/informs/ratifies entry
        names a ticket or ADR-obligation id that does not resolve in the
        corpus (resolved-decision 1: WARN, never a refusal).
      - "malformed_id" / "duplicate_id" — see ElementsLintFinding; folded in
        here so callers have one report-only finding stream for the whole
        typed-graph model.
      - "duplicate_receipt_id" — see detect_duplicate_receipt_ids (T-2926):
        the same receipts: id is claimed by more than one ticket, corpus-
        wide. NOT itself a typed-edge graph defect (receipts are excluded
        from that model) — reuses this dataclass for finding-shape/report-
        plumbing convenience only.
      - "unverified_element" / "implemented_unverified_element" — see
        detect_unverified_elements (T-2843, split T-3179): an ADR-element
        obligation with no `verifies:` edge. "unverified_element" = no
        `implements:` edge either (true leak, nothing done). "implemented_
        unverified_element" = >=1 `implements:` edge, no `verifies:` yet
        (expected transitional shape — Tranche A lands implements: ahead of
        verifies:). Same not-a-graph-defect caveat as duplicate_receipt_id.
    """
    kind: str
    detail: str


# Ticket frontmatter fields subject to the dangling-target WARN scan. All four
# new edge fields, INCLUDING `informs` — acyclicity-exemption is orthogonal to
# existence-checking (resolved-decision 1 covers all four uniformly). T-3476:
# `ratifies` joins on the same basis — a well-formed-but-unresolvable ratifies
# target (e.g. an ADR whose elements: block hasn't landed yet) is a WARN,
# never a refusal, same as its siblings.
_DANGLING_CHECK_FIELDS = ("implements", "verifies", "supersedes", "informs", "ratifies")


def detect_typed_graph_issues(
    tickets_root: Path,
    adr_dir: Path | Iterable[Path],
    *,
    exempt_dangling_target: Callable[[str], bool] | None = None,
) -> list[TypedGraphFinding]:
    """Corpus-level scan over the generalized typed-edge graph (report-only).

    `adr_dir` accepts a single Path or an iterable of Paths (T-3477 — see
    scan_adr_elements's docstring; this function forwards unchanged).

    `exempt_dangling_target` (T-3495): an OPT-IN predicate over the raw edge
    target string. When it returns True for a target that would otherwise be
    flagged `dangling_target`, that finding is suppressed instead. Deliberately
    NOT a global behavior of this function — it exists for a caller that
    KNOWINGLY scans a NARROWER `adr_dir` than the full obligation universe and
    wants to defer judgment on a specific class of target to a different
    scanner. This module's own production call sites (`_do_lint`, `main
    --dry-run`) pass `is_echo_scoped_id` here (recovery's `adr_dir` no longer
    includes echo-prismatic's corpus — see `_ECHO_PRISMATIC_ADR_DIR`'s module
    note — so an echo-shaped target is neither confirmed nor refuted by recovery's
    own scan). `tools/echo_corpus_lint.py`'s companion call passes NO
    predicate (the default `None`): it unions BOTH adr dirs itself, so it can
    and should still flag a genuinely-dangling echo target as such — an
    unconditional exemption inside this function would blind that caller to
    the exact defect it exists to catch.

    Three checks, per ORG-065 §3.6 "at lint (corpus-wide)":
      1. ADR `elements:` block malformations (scan_adr_elements).
      2. Dangling implements/verifies/supersedes/informs/ratifies targets — a target
         shaped like a ticket id that doesn't resolve to any ticket file, or
         shaped like an obligation id that isn't in any ADR's known element
         ids. Every ticket in ANY state is scanned (mirrors
         build_typed_edge_graph's state-independence).
      3. Cycles, checked PER ACYCLICITY DOMAIN (build_typed_edge_graph +
         _find_all_cycles, once per ACYCLICITY_DOMAINS entry — never the full
         union; see the module note above) — the corpus-wide backstop for the
         SAME invariant ticket.py enforces at insertion.
    """
    findings: list[TypedGraphFinding] = []

    known_obligation_ids, element_findings = scan_adr_elements(adr_dir)
    for ef in element_findings:
        findings.append(TypedGraphFinding(kind=ef.kind, detail=ef.detail))

    for st in STATES:
        d = tickets_root / st
        if not d.exists():
            continue
        for entry in sorted(d.iterdir()):
            if not entry.is_file() or not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            tid = entry.stem
            try:
                fields, _body = parse_front_matter(entry.read_text(encoding="utf-8"))
            except (TicketError, OSError):
                continue
            for field_name in _DANGLING_CHECK_FIELDS:
                for raw in (fields.get(field_name) or []):
                    raw = str(raw)
                    if _TICKET_ID_RE.fullmatch(raw):
                        exists = any(
                            (tickets_root / s / f"{raw}.md").exists() for s in STATES
                        )
                        if not exists:
                            findings.append(TypedGraphFinding(
                                kind="dangling_target",
                                detail=f"{tid} {field_name} -> {raw}, which does not "
                                       f"resolve to any ticket",
                            ))
                    elif raw not in known_obligation_ids:
                        # T-3495: an opt-in caller-supplied exemption (see
                        # exempt_dangling_target's docstring above) — never a
                        # global property of the target shape. Recovery's own
                        # production call sites pass is_echo_scoped_id here
                        # because their adr_dir no longer includes
                        # echo-prismatic's corpus; tools/echo_corpus_lint.py's
                        # companion call passes none, so it still flags a
                        # genuinely-dangling echo target against the union it
                        # explicitly scanned.
                        if exempt_dangling_target is not None and exempt_dangling_target(raw):
                            continue
                        findings.append(TypedGraphFinding(
                            kind="dangling_target",
                            detail=f"{tid} {field_name} -> {raw}, which does not "
                                   f"resolve to any ADR elements: id",
                        ))

    # ACYCLICITY_DOMAINS, never the full union — see the module note above
    # (the false-positive discovery: a parent depends_on its own child).
    for domain in ACYCLICITY_DOMAINS:
        graph = build_typed_edge_graph(tickets_root, domain)
        for cyc in _find_all_cycles(graph):
            chain = " -> ".join(f"{kind}:{ident}" for kind, ident in cyc)
            head_kind, head_id = cyc[0]
            findings.append(TypedGraphFinding(
                kind="cycle",
                detail=f"typed-edge cycle in domain {domain} (deadlock/"
                       f"non-terminating closure): {chain} -> {head_kind}:{head_id}",
            ))

    return findings


# T-2843 / ORG-065 §3.4: element-granularity lineage leak detector — "which
# obligations have no satisfying verifies edge", reproducing the T-2167
# by-hand ADR-element coverage audit as a query. Standalone (not folded into
# detect_typed_graph_issues, same shape as detect_duplicate_receipt_ids
# above): an unverified element is not a typed-edge GRAPH defect (no cycle,
# no dangling target) — it reuses TypedGraphFinding purely for the finding
# shape / report plumbing, concatenated by callers alongside the other two.
#
# T-3179: §3.4's own text defines the query strictly as "no satisfying
# `verifies` edge" — `implements:` is never mentioned as a covering factor,
# in §3.4 or in the §3.5 binding refinements. So the SET this function
# returns (every element lacking a verifies: edge) is UNCHANGED — that is
# the ADR-mandated "unverified" membership and stays untouched. What changes
# is severity/CLASS WITHIN that set: Tranche A (T-2946) lands `implements:`
# edges corpus-wide ahead of `verifies:`, so an element with an `implements:`
# edge and no `verifies:` edge is expected-transitional-shape (work exists,
# evidence pending), not a true leak (nothing done at all). Two kinds now
# share this function's output: "unverified_element" (no implements AND no
# verifies — true leak) and "implemented_unverified_element" (>=1 implements,
# no verifies — names the implementing ticket ids so an operator sees "work
# is in flight" at a glance). Callers/call-sites are unaffected — both still
# concatenate this function's return value into one typed_graph_findings
# list; only the `kind` values differ.
#
# T-3476 / ORG-065 Call 1: the load-bearing invariant the operator ratified (T-3206
# TG 6734) is that a `ratifies` edge must NEVER be counted as satisfying an
# implements-obligation — `ratifies` is gathered into its OWN independent set
# (never unioned into `verified` or `implementers` below), so a ratifies-only
# element falls through to the SAME "unverified_element" true-leak branch it
# would hit with no edges at all (see test_ratifies_only_element_stays_
# unverified in test_typed_edges.py — this is the literal invariant test).
#
# The ONE narrow exception: the reserved whole-ADR sentinel `E0` (T-3205
# policy, "ADR as a whole" — minted for whole-document plan/elaboration
# tickets AND, as of T-3476, for whole-ADR ratify-class tickets). E0 is not
# an implementation obligation in the code sense; for a ratify-class ADR, a
# `ratifies:` edge on its E0 IS that sentinel's terminal completion signal.
# Without this exception, minting E0 for the 4-5 passport/memory ADRs this
# ticket ratifies would create a PERMANENT, unfixable false-positive leak —
# nothing will ever "implement" or "verify" the abstract notion of "was this
# ADR ratified" (the T-2896 lesson: examine what a decision pair composes
# into before landing either alone). The exception is scoped to the LOCAL id
# "E0" specifically, not "any ratifies target" — an ordinary content element
# (E1, E2, ...) that is ratified-only stays a true leak; ratifying a specific
# obligation is explicitly NOT a substitute for implementing/verifying it.
_WHOLE_ADR_ELEMENT_ID = "E0"


def detect_unverified_elements(
    tickets_root: Path,
    adr_dir: Path | Iterable[Path],
) -> list[TypedGraphFinding]:
    """Corpus-wide scan: every ADR `elements:` id (scan_adr_elements) that no
    ticket, in any state, `verifies:`. Split (T-3179) into two finding kinds
    within that same unverified set:

    `adr_dir` accepts a single Path or an iterable of Paths (T-3477 — see
    scan_adr_elements's docstring; forwarded unchanged into that call).

      - "unverified_element" — no `implements:` edge either: a true leak,
        nothing has been done against the obligation.
      - "implemented_unverified_element" — >=1 `implements:` edge but no
        `verifies:` edge: expected transitional shape (ORG-065 Tranche A
        lands implements: ahead of verifies:); detail names the implementing
        ticket id(s).

    T-3476: a THIRD, narrower effect — an element whose LOCAL id is the
    reserved whole-ADR sentinel `E0` (T-3205 policy) and which has >=1
    `ratifies:` edge, and NO `implements:` edge, is EXCLUDED from this
    function's output entirely (no finding of either kind). See the module
    note directly above this function for the full rationale. This exclusion
    never fires for a non-E0 element, and never overrides the
    implements-priority branch (an E0 that is both implemented and ratified
    stays "implemented_unverified_element" — implements-priority is
    unchanged).

    Reuses `build_typed_edge_graph` for the verifies-edge set, the
    implements-edge set, AND the ratifies-edge set, via THREE SEPARATE CALLS
    rather than one combined call — the helper flat-concatenates targets from
    every requested field into one per-ticket adjacency list with no per-edge
    field tag (see its body), so a single combined call can only yield a
    unioned "covered-by-any" set and cannot distinguish which field produced
    which target. Separate calls preserve `build_typed_edge_graph`'s existing
    untagged-union contract (relied on elsewhere: ACYCLICITY_DOMAINS, the
    dangling-target scan) rather than extending it for this one caller, and
    are what makes the ratifies-never-satisfies-implements invariant possible
    to enforce in the first place — a unioned call could not keep the three
    edge types apart.

    Element/clause ids are tracked independently, with no roll-up: verifying
    (or implementing) `E1` does not satisfy `E1.C1` — mirrors
    scan_adr_elements's own flat, hierarchy-free `known_ids` set (T-2841
    precedent). A malformed element id never enters `known_ids` in the first
    place (scan_adr_elements's own concern, surfaced separately by
    detect_typed_graph_issues) so it is never checked here. A
    malformed/garbage `verifies:`/`implements:`/`ratifies:` entry is
    classified by `edge_node_id` as an ("obligation", raw) tuple that matches
    no real element — harmless, contributes no false coverage.
    """
    known_ids, _element_findings = scan_adr_elements(adr_dir)
    if not known_ids:
        return []
    verifies_graph = build_typed_edge_graph(tickets_root, edge_fields=("verifies",))
    verified: set[str] = set()
    for targets in verifies_graph.values():
        for kind, ident in targets:
            if kind == "obligation":
                verified.add(ident)

    # T-3179: implementing-ticket ids per obligation, for the
    # "implemented_unverified_element" detail line — names WHICH ticket(s)
    # implement it, so an operator sees "work exists" without a second lookup.
    implements_graph = build_typed_edge_graph(tickets_root, edge_fields=("implements",))
    implementers: dict[str, set[str]] = {}
    for src, targets in implements_graph.items():
        src_kind, src_id = src
        if src_kind != "ticket":
            continue
        for kind, ident in targets:
            if kind == "obligation":
                implementers.setdefault(ident, set()).add(src_id)

    # T-3476: ratifies-edge targets, gathered INDEPENDENTLY — never unioned
    # into `verified` or `implementers` above (that union is the whole point
    # of the operator's invariant). Consulted ONLY for the narrow E0 exclusion.
    ratifies_graph = build_typed_edge_graph(tickets_root, edge_fields=("ratifies",))
    ratified: set[str] = set()
    for targets in ratifies_graph.values():
        for kind, ident in targets:
            if kind == "obligation":
                ratified.add(ident)

    findings: list[TypedGraphFinding] = []
    for oid in sorted(known_ids - verified):
        implementing_tids = sorted(implementers.get(oid, ()))
        if implementing_tids:
            findings.append(TypedGraphFinding(
                kind="implemented_unverified_element",
                detail=f"{oid} implemented by {', '.join(implementing_tids)} but "
                       f"has no ticket verifies: edge — expected transitional "
                       f"shape, ORG-065 §3.4",
            ))
            continue
        # T-3476: the whole-ADR-sentinel exclusion — see module note above.
        local_id = oid.rsplit("#", 1)[-1]
        if local_id == _WHOLE_ADR_ELEMENT_ID and oid in ratified:
            continue
        findings.append(TypedGraphFinding(
            kind="unverified_element",
            detail=f"{oid} has no ticket verifies: edge (open or done) — "
                   f"unverified obligation, ORG-065 §3.4",
        ))
    return findings


# ---------------------------------------------------------------------------
# T-2842 / ORG-065 BT2: closure-validation gate (§3.3/§3.5) — pure detection
# layer. ticket.py owns ENFORCEMENT (the warn/gate mode decision, the
# resolve-time hook); everything here is a pure function returning findings,
# mirroring the detect-vs-enforce split BT1 already draws between this
# module's report-only corpus scans and ticket.py's insertion-time hard
# refusals (_parse_edge_targets / _reject_if_cycle).
#
# What this closes: BT1's own handoff note (ticket.py ~:438-450) is explicit
# that a bare ticket-id `verifies` target is a simplification standing in for
# evidence, and "MUST NOT [be] treat[ed]... as satisfying the receipt/
# evidence-authority requirements... without a real commit/test/evidence node
# behind it." `verifies` therefore stays exactly as BT1 built it (a coarse
# declarative edge, still feeding the acyclicity/lineage graph unchanged);
# the NEW `receipts:` frontmatter field (ticket.py) carries the actual
# evidence, and the closure gate below consults `receipts` ONLY — a
# `verifies` entry with no corresponding receipt satisfies nothing. This is
# the structural form of "a commit link is not a receipt" (§3.5.2).
#
# PROVENANCE IS DECLARED, NOT AUTHENTICATED (T-2842 plan addition A). A
# receipt's `provenance` (ci/reviewer/self) is whatever string the resolving
# actor typed — nothing here verifies it actually came from CI or a
# reviewer. This module enforces VOCABULARY + the DENIAL RULE (a high-risk
# clause refuses a self-asserted receipt); it does not AUTHENTICATE who
# produced the evidence. Same trust shape as `ratifiable_files` (a filer
# asserts a path was ratified; nothing cryptographically proves it).
# Authenticating provenance is downstream evidence-node work, not this
# ticket's scope — do not read the evidence-authority check below as more
# than a scoped denial rule over a self-reported field.
#
# COMPOSITION: `receipts` is deliberately EXCLUDED from `_GATING_EDGE_FIELDS`
# / `_DANGLING_CHECK_FIELDS` / `ACYCLICITY_DOMAINS` above — it is evidence-
# attachment data, not a graph edge, and a receipt id never enters
# `edge_node_id` classification. Non-participation is intentional, not an
# oversight — do not add "receipts" to those constants without a new ADR
# reading.
#
# `_CLAUSE_ID_RE` below duplicates ticket.py's `_EDGE_TARGET_RE` obligation
# half (`(ADR|ORG)-\d+#E\d+(\.C\d+)?`) rather than importing it — same
# "ticket_lint must not import a private name back out of ticket.py"
# precedent BT1 already documents for `_GATING_EDGE_FIELDS`/
# `_GATING_LIST_FIELDS` above. Keep the two shapes in sync by hand.
#
# `load_adr_obligation_index` below is a DELIBERATELY INDEPENDENT walk over
# ADR `elements:` blocks, not a refactor of `scan_adr_elements`'s shared
# file-walk prefix, even though the two duplicate ~15 lines of glob/
# skip-README/frontmatter-split/YAML-load boilerplate. Refactoring BT1's
# already-tested, landed function for the DRY gain would add regression
# surface to code outside this ticket's brief for a benefit that changes no
# behavior (P7 cost-asymmetry) — ratified in plan review (T-2842, HO
# disposition 2, 2026-08-12).
# ---------------------------------------------------------------------------

# Namespaced receipt-identity prefixes (T-2842 / ORG-065 §3.5.3: "stable
# namespaced identities... so an edge resolves to exactly one artifact").
# Corpus-wide UNIQUENESS of a receipt id is explicitly OUT OF SCOPE for BT2
# (HO disposition 1, 2026-08-12) — filed as a follow-up, dep-gated on
# T-2842. This module only validates SHAPE, not corpus-wide identity.
RECEIPT_NAMESPACES = ("ci-run", "test-result", "observable")
_RECEIPT_ID_RE = re.compile(
    r"^(?:" + "|".join(RECEIPT_NAMESPACES) + r"):[A-Za-z0-9._\-/]+$"
)

# Provenance vocabulary (§3.5.4). AUTHORIZED_PROVENANCE is the subset that
# may satisfy a HIGH-RISK clause — "self" (self-asserted) is refused there,
# per the module note above ("declared, not authenticated": this is a
# vocabulary/denial rule, not authentication).
PROVENANCE_VOCAB = ("ci", "reviewer", "self")
AUTHORIZED_PROVENANCE = ("ci", "reviewer")

# Mirrors ticket.py's _EDGE_TARGET_RE obligation half — see module note above
# on why this is duplicated rather than imported.
_CLAUSE_ID_RE = re.compile(r"^(?:ADR|ORG)-\d+#E\d+(?:\.C\d+)?$")


@dataclass
class ReceiptEntry:
    """One decoded `receipts:` frontmatter entry (T-2842 / ORG-065 BT2)."""
    clause: str
    receipt_id: str
    provenance: str


def decode_receipt(raw: str) -> "ReceiptEntry | None":
    """Decode one `<clause>=<receipt-id>=<provenance>` receipts: entry.

    Returns None (never raises) on any shape defect — malformed entries are
    a caller concern: ticket.py's `_parse_receipt_entries` raises TicketError
    at insertion time using this as the validity check; the closure
    evaluator below treats a None decode as a "malformed_receipt" finding
    (defense in depth for hand-edited/fixture data that bypassed CLI
    validation — the same posture BT1's dangling-target scan takes toward
    hand-edited defects the insertion guard never saw).
    """
    parts = raw.split("=")
    if len(parts) != 3:
        return None
    clause, receipt_id, provenance = (p.strip() for p in parts)
    if not _CLAUSE_ID_RE.fullmatch(clause):
        return None
    if not _RECEIPT_ID_RE.fullmatch(receipt_id):
        return None
    if provenance not in PROVENANCE_VOCAB:
        return None
    return ReceiptEntry(clause=clause, receipt_id=receipt_id, provenance=provenance)


@dataclass
class ObligationInfo:
    """Detail for one ADR obligation id (element or clause), T-2842 BT2."""
    text: str
    high_risk: bool


@dataclass
class ObligationIndex:
    """Per-corpus obligation detail, keyed by fully-qualified id
    (`ADR-NNN#E<k>` or `ADR-NNN#E<k>.C<m>`).

    `details`: every element AND clause id -> ObligationInfo (text/high_risk).
    `element_clauses`: element id -> its clause ids (possibly [] if the
    element has no `clauses:` sub-block, in which case the element itself is
    the atomic obligation unit — see `evaluate_closure_fields`'s expansion).
    """
    details: dict
    element_clauses: dict


def load_adr_obligation_index(adr_dir: Path | Iterable[Path]) -> ObligationIndex:
    """Walk every ADR's optional `elements:` frontmatter block and build the
    detail index the closure evaluator needs (text + high_risk per id, and
    element->clause-id expansion).

    `adr_dir` accepts a single bare Path (this repo's own architecture/adr/,
    every pre-T-3578 call site) OR an iterable of Paths (T-3578 / ORG-065
    closure-gate cross-repo fix: this repo's architecture/adr/ UNIONED with
    echo-prismatic's adr/, so a ticket carrying a cross-repo echo obligation
    — e.g. `implements: [ADR-216#E4]` — resolves against both corpora as one
    obligation-id universe rather than hitting a spurious dangling_obligation).
    Each directory is walked independently and its results UNIONED into the
    single `details`/`element_clauses` dicts. ADR-NNN (echo) and ORG-NNN
    (pantheon) fully-qualified ids are disjoint namespaces, so the merge is
    collision-free; on the theoretical id collision the first directory in the
    iterable wins (a later dir does not overwrite an id an earlier dir already
    indexed), for deterministic behavior.

    Deliberately independent of `scan_adr_elements` (see module note above)
    — same YAML-frontmatter reuse (`_split_frontmatter`/`_load_yaml`/
    `_adr_id_from`), different extraction: `scan_adr_elements` validates
    id shape/duplication and returns a flat id set; this returns per-id
    TEXT and HIGH_RISK detail plus the element->clauses mapping. Tolerant of
    the same malformed shapes scan_adr_elements flags (an id-shape defect
    there does not crash this walk — it just yields an entry the closure
    evaluator's `dangling_obligation` check won't find a match for, since a
    malformed id here still gets indexed under whatever string was given).
    """
    details: dict = {}
    element_clauses: dict = {}
    for one_dir in _normalize_adr_dirs(adr_dir):
        # Silent guard (not warn): this is called per-ticket-with-obligations,
        # so a per-dir warning would spam; scan_adr_elements carries the visible
        # warn for the same inaccessible dir. The closure-gate's actor-relative
        # warn-fallback (ticket.py _gate_echo_adr_dir / T-3578) carries the loud
        # echo-specific warn. See _adr_dir_scannable (T-3477 EACCES).
        if not _adr_dir_scannable(one_dir):
            continue
        for path in sorted(one_dir.glob("*.md")):
            if path.name.upper().startswith("README"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            fm_block, _body = _split_frontmatter(text)
            fm = _load_yaml(fm_block, source=path.name)
            elements = fm.get("elements")
            if not isinstance(elements, list):
                continue
            adr_id = _adr_id_from(fm, path)
            for el in elements:
                if not isinstance(el, dict):
                    continue
                el_id = str(el.get("id", "")).strip()
                if not el_id:
                    continue
                fq_el = f"{adr_id}#{el_id}"
                el_high_risk = bool(el.get("high_risk", False))
                # first-dir-wins on the theoretical cross-corpus id collision
                # (disjoint namespaces make this vacuous in practice).
                if fq_el not in details:
                    details[fq_el] = ObligationInfo(
                        text=str(el.get("title", "") or el.get("text", "")),
                        high_risk=el_high_risk,
                    )
                clause_ids: list = []
                clauses = el.get("clauses")
                if isinstance(clauses, list):
                    for cl in clauses:
                        if not isinstance(cl, dict):
                            continue
                        cl_id = str(cl.get("id", "")).strip()
                        if not cl_id:
                            continue
                        fq_cl = f"{adr_id}#{cl_id}"
                        clause_ids.append(fq_cl)
                        if fq_cl not in details:
                            details[fq_cl] = ObligationInfo(
                                text=str(cl.get("text", "") or cl.get("title", "")),
                                high_risk=bool(cl.get("high_risk", el_high_risk)),
                            )
                element_clauses.setdefault(fq_el, clause_ids)
    return ObligationIndex(details, element_clauses)


@dataclass
class ClosureFinding:
    """A closure-validation defect (T-2842 / ORG-065 BT2, §3.3/§3.5).
    Report-only from this module's side — ticket.py decides warn vs refuse.

    kinds:
      - "unsatisfied_clause" — a required clause (from `implements`) has no
        `receipts` entry naming it.
      - "evidence_authority_denied" — a required clause IS high-risk and
        every receipts entry naming it has provenance="self" (§3.5.4: only
        authorized ci/reviewer evidence may satisfy a high-risk obligation).
      - "dangling_obligation" — a required id does not resolve to any known
        ADR `elements:` entry (can't verify satisfaction against an unknown
        obligation).
      - "malformed_receipt" — a `receipts:` entry does not decode (see
        decode_receipt) — defensive, for hand-edited/fixture data.
    """
    kind: str
    detail: str


def evaluate_closure_fields(fields: dict, adr_dir: Path | Iterable[Path]) -> list:
    """The core closure-validation algorithm (T-2842 / ORG-065 §3.3), pure:
    takes already-parsed ticket frontmatter fields + an ADR directory (or, per
    T-3578, an iterable of ADR directories to union — this repo's
    architecture/adr/ plus echo-prismatic's adr/ so a cross-repo echo
    obligation resolves), no other I/O. `evaluate_closure_for_ticket` below is
    the thin I/O wrapper that resolves a ticket id to fields and calls this.

    Closure-gate scope (ADR resolved-decision 3): ONLY `implements` entries
    shaped like an ADR obligation (`_CLAUSE_ID_RE`) participate — a bare
    ticket-id `implements` entry ("implements parent work", per BT1's own
    field semantics) is not an obligation edge and is skipped. `depends_on`
    is untouched (cascade semantics unchanged, per the ADR).

    Returns [] (no obligation-shaped implements entries) for the overwhelming
    majority of existing tickets — the gate is a true no-op for any ticket
    that never declared an ADR obligation.
    """
    implements = fields.get("implements") or []
    obligation_targets = [t for t in implements if _CLAUSE_ID_RE.fullmatch(str(t))]
    if not obligation_targets:
        return []

    index = load_adr_obligation_index(adr_dir)

    # Expand each implements target to its required clause set (dedup,
    # order-preserving). An element with no clauses: sub-block requires
    # itself as the atomic obligation unit.
    required: list = []
    for target in obligation_targets:
        clause_ids = index.element_clauses.get(target)
        if clause_ids:
            for c in clause_ids:
                if c not in required:
                    required.append(c)
        elif target not in required:
            required.append(target)

    # Decode receipts: entries (malformed ones are their own finding, never
    # silently dropped — same "surface, don't invent/hide" posture as §3.5.5,
    # applied here to malformed data rather than uncertain inference).
    receipts_raw = fields.get("receipts") or []
    findings: list = []
    by_clause: dict = {}
    for raw in receipts_raw:
        entry = decode_receipt(str(raw))
        if entry is None:
            findings.append(ClosureFinding(
                kind="malformed_receipt",
                detail=f"receipts entry {raw!r} does not decode as "
                       f"<clause>=<receipt-id>=<provenance>",
            ))
            continue
        by_clause.setdefault(entry.clause, []).append(entry)

    for clause in required:
        info = index.details.get(clause)
        if info is None:
            findings.append(ClosureFinding(
                kind="dangling_obligation",
                detail=f"{clause} does not resolve to any ADR elements: entry",
            ))
            continue
        candidates = by_clause.get(clause, [])
        if not candidates:
            findings.append(ClosureFinding(
                kind="unsatisfied_clause",
                detail=f"{clause} has no satisfying receipt — {info.text!r}",
            ))
            continue
        if info.high_risk and not any(
            c.provenance in AUTHORIZED_PROVENANCE for c in candidates
        ):
            findings.append(ClosureFinding(
                kind="evidence_authority_denied",
                detail=f"{clause} is HIGH-RISK and only has self-asserted "
                       f"receipt(s) — {info.text!r}",
            ))

    return findings


def evaluate_closure_for_ticket(tid: str, tickets_root: Path, adr_dir: Path) -> list:
    """The single-named-ticket validator entry point (T-2842 brief): finds
    `tid` in ANY state (mirrors build_typed_edge_graph's state-independence —
    a done ticket's closure is exactly what this re-checks), parses it, and
    delegates to evaluate_closure_fields. This is the hook a disposable
    live test ticket exercises post-flip, independent of the resolve CLI
    path (ticket.py's `_do_transition` calls the same underlying function
    against in-memory fields, not this wrapper, to avoid a redundant re-read
    inside the same transition).

    Raises TicketError if `tid` does not resolve to any ticket file.
    """
    for st in STATES:
        p = tickets_root / st / f"{tid}.md"
        if p.exists():
            fields, _body = parse_front_matter(p.read_text(encoding="utf-8"))
            return evaluate_closure_fields(fields, adr_dir)
    raise TicketError(f"evaluate_closure_for_ticket: {tid} not found in any state")


def detect_duplicate_receipt_ids(tickets_root: Path) -> list:
    """Corpus-wide receipt-id uniqueness scan (T-2926 / ORG-065 §3.5.3 —
    the follow-up T-2842 disposition 1 explicitly deferred: "Corpus-wide
    UNIQUENESS of a receipt id is explicitly OUT OF SCOPE for BT2", see the
    RECEIPT_NAMESPACES module note above). Analogous to scan_adr_elements's
    duplicate_id check, but cross-ticket rather than within-one-file — a
    receipt id names one artifact, and §3.5.3 requires it resolve to exactly
    one; two different tickets both claiming it violates that.

    Standalone (not folded into detect_typed_graph_issues): receipts are
    deliberately NOT part of the typed-edge graph model (see the BT2 module
    note above on why receipts stay out of _GATING_EDGE_FIELDS/
    _DANGLING_CHECK_FIELDS/ACYCLICITY_DOMAINS) — this scan reuses
    TypedGraphFinding only for the finding shape / report plumbing, not
    because a duplicate receipt id is a graph defect. Callers concatenate
    this function's output with detect_typed_graph_issues's to feed one
    report (see the two call sites in _do_lint / --dry-run).

    Reuse of the same receipt id ACROSS CLAUSES WITHIN one ticket is
    legitimate (one CI run can satisfy several of that ticket's own
    obligations) — only cross-ticket reuse is flagged, so ownership is
    tracked per distinct ticket id, not per raw occurrence. Entries that
    fail decode_receipt are skipped (not re-flagged here as
    "malformed_receipt" — that is evaluate_closure_fields's per-ticket
    concern; duplicating it corpus-wide is out of this scan's scope).
    """
    owners: dict = {}
    for st in STATES:
        d = tickets_root / st
        if not d.exists():
            continue
        for entry in sorted(d.iterdir()):
            if not entry.is_file() or not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            tid = entry.stem
            try:
                fields, _body = parse_front_matter(entry.read_text(encoding="utf-8"))
            except (TicketError, OSError):
                continue
            for raw in (fields.get("receipts") or []):
                decoded = decode_receipt(str(raw))
                if decoded is None:
                    continue
                owners.setdefault(decoded.receipt_id, set()).add(tid)

    findings: list = []
    for receipt_id in sorted(owners):
        tids = owners[receipt_id]
        if len(tids) > 1:
            findings.append(TypedGraphFinding(
                kind="duplicate_receipt_id",
                detail=f"receipt id {receipt_id!r} is claimed by multiple "
                       f"tickets: {', '.join(sorted(tids))}",
            ))
    return findings


def detect_duplicate_episode_ids(tickets_root: Path) -> list:
    """Corpus-wide ORG-076 ``episode_id`` uniqueness scan (T-4707 / RFC-004 §3(a)).

    One repair ticket per fault episode: two tickets (any state) carrying the same
    ``episode_id`` frontmatter value is a duplicate-emission defect. Complements the
    CLI-side refusal in ``ticket.py file --repair-fields-json`` (which checks the
    COMMITTED corpus) for hand-edited / racing writers. Reuses TypedGraphFinding for
    report plumbing only, like detect_duplicate_receipt_ids.
    """
    owners: dict = {}
    for st in STATES:
        d = tickets_root / st
        if not d.exists():
            continue
        for entry in sorted(d.iterdir()):
            if not entry.is_file() or not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            try:
                fields, _body = parse_front_matter(entry.read_text(encoding="utf-8"))
            except (TicketError, OSError):
                continue
            ep = fields.get("episode_id")
            if isinstance(ep, str) and ep:
                owners.setdefault(ep, set()).add(entry.stem)
    return [
        TypedGraphFinding(
            kind="duplicate_episode_id",
            detail=f"episode_id {ep!r} is carried by multiple tickets: {', '.join(sorted(tids))}",
        )
        for ep, tids in sorted(owners.items()) if len(tids) > 1
    ]


# ---------------------------------------------------------------------------
# T-2845 / ORG-065 BT5: closure-gate health monitor — the ORG-068 "health-
# monitor" gap the T-2842 reviewer named (nothing observed gate-mode drift or
# finding-volume trend ahead of the warn->gate flip). Pure/report-only, same
# shape as every other corpus scan in this module — `detect_closure_gate_
# health` takes the effective/intended mode strings as plain parameters
# rather than importing ticket.py's `_closure_gate_mode()` itself, keeping
# this module's detect-vs-enforce split intact (see the T-2842 banner comment
# above); the caller that resolves the REAL env value and owns persistence/
# CLI/printing is the standalone tools/closure_gate_health_monitor.py.
#
# Scans EVERY state (mirrors build_typed_edge_graph's ALL-states scope) so
# the two named consumers are both served from one walk: `corpus_*` /
# `by_state` track full-corpus migration-walk progress (including
# already-closed tickets whose obligations predate a receipt), while
# `open_*` (inbox+active only) is the slice the gate will actually re-fire
# on once flipped — that is what a flip-readiness read should use.
# ---------------------------------------------------------------------------

_OPEN_STATES = ("inbox", "active")


@dataclass
class ClosureGateHealthReport:
    """Corpus-wide closure-gate health snapshot (T-2845 flip prerequisite).

    `open_*` fields (inbox+active only) are the flip-readiness signal — the
    slice the gate will actually fire on post-flip. `corpus_*` / `by_state`
    cover every state including `done`, tracking full-corpus migration-walk
    progress; do not read `corpus_*` as a flip go/no-go number.
    """
    effective_mode: str
    intended_mode: str
    mode_drift: bool
    corpus_tickets_scanned: int
    corpus_tickets_with_obligations: int
    corpus_tickets_with_findings: int
    corpus_total_findings: int
    corpus_findings_by_kind: dict
    open_tickets_scanned: int
    open_tickets_with_obligations: int
    open_tickets_with_findings: int
    open_total_findings: int
    open_findings_by_kind: dict
    # state -> {"scanned", "with_obligations", "with_findings", "total_findings"}
    by_state: dict


def detect_closure_gate_health(
    tickets_root: Path, adr_dir: Path, *, effective_mode: str, intended_mode: str,
) -> ClosureGateHealthReport:
    """Walk every ticket in every state, re-evaluate each via
    evaluate_closure_fields (a live re-derivation, never a cache — same
    posture as evaluate_closure_for_ticket), and aggregate both the
    full-corpus and open-only (inbox+active) rollups plus a raw per-state
    breakdown. `effective_mode`/`intended_mode` are caller-supplied — see
    module note above.
    """
    by_state: dict = {}
    kinds_by_state: dict = {}
    for st in STATES:
        d = tickets_root / st
        scanned = with_obligations = with_findings = total = 0
        kind_counts: dict = {}
        if d.exists():
            for entry in sorted(d.iterdir()):
                if not entry.is_file() or not TICKET_FILENAME_RE.fullmatch(entry.name):
                    continue
                scanned += 1
                try:
                    fields, _body = parse_front_matter(entry.read_text(encoding="utf-8"))
                except (TicketError, OSError):
                    continue
                implements = fields.get("implements") or []
                if any(_CLAUSE_ID_RE.fullmatch(str(t)) for t in implements):
                    with_obligations += 1
                findings = evaluate_closure_fields(fields, adr_dir)
                if findings:
                    with_findings += 1
                    total += len(findings)
                    for f in findings:
                        kind_counts[f.kind] = kind_counts.get(f.kind, 0) + 1
        by_state[st] = {
            "scanned": scanned,
            "with_obligations": with_obligations,
            "with_findings": with_findings,
            "total_findings": total,
        }
        kinds_by_state[st] = kind_counts

    def _sum(states, key):
        return sum(by_state[s][key] for s in states if s in by_state)

    def _sum_kinds(states):
        out: dict = {}
        for s in states:
            for k, v in kinds_by_state.get(s, {}).items():
                out[k] = out.get(k, 0) + v
        return out

    all_states = list(STATES)
    open_states = [s for s in _OPEN_STATES if s in STATES]

    return ClosureGateHealthReport(
        effective_mode=effective_mode,
        intended_mode=intended_mode,
        mode_drift=(effective_mode != intended_mode),
        corpus_tickets_scanned=_sum(all_states, "scanned"),
        corpus_tickets_with_obligations=_sum(all_states, "with_obligations"),
        corpus_tickets_with_findings=_sum(all_states, "with_findings"),
        corpus_total_findings=_sum(all_states, "total_findings"),
        corpus_findings_by_kind=_sum_kinds(all_states),
        open_tickets_scanned=_sum(open_states, "scanned"),
        open_tickets_with_obligations=_sum(open_states, "with_obligations"),
        open_tickets_with_findings=_sum(open_states, "with_findings"),
        open_total_findings=_sum(open_states, "total_findings"),
        open_findings_by_kind=_sum_kinds(open_states),
        by_state=by_state,
    )


# ---------------------------------------------------------------------------
# T-2833: unanchored / bad not_before date-gate scan (report-only).
#
# Every not_before date gate must carry a `operator:<T-NNNN|tg:MSGID>` anchor
# (ticket.py enforces this at SET time; this scan surfaces PRE-EXISTING gates
# grandfathered before the rule, and any hand-edited violation the CLI didn't
# gate). A date gate may mean exactly one thing — "the operator picked this date" —
# so a gate that is unanchored, malformed, or cites a non-existent ticket is a
# candidate for migration to a --depends-on dependency or an event-driven watch.
# ---------------------------------------------------------------------------

# Mirror of ticket.py's anchor rule (kept local to avoid a lint->ticket import
# cycle; the shapes must stay in sync). Two legal forms: operator:<T-NNNN|tg:digits>
# and event:<KIND>:<param> with KIND a closed enum (cert-expiry/key-expiry/
# cadence/window) — the closed set keeps "fresh_session" out.
# T-3215: widened in lockstep with ticket.py's _OPERATOR_ANCHOR_RE (4+ digits).
_LINT_OPERATOR_ANCHOR_RE = re.compile(r"^operator:(T-\d{4,}|tg:\d+)$")
_LINT_EVENT_ANCHOR_KINDS = ("cert-expiry", "key-expiry", "cadence", "window")
_LINT_EVENT_ANCHOR_RE = re.compile(
    r"^event:(?:" + "|".join(_LINT_EVENT_ANCHOR_KINDS) + r"):.+$"
)


@dataclass
class UnanchoredGateFinding:
    """A not_before date gate that fails the T-2833 anchor rule (report-only).

    kinds:
      - "unanchored"      — not_before set, no not_before_anchor at all.
      - "malformed"       — anchor present but not operator:<T-NNNN|tg:MSGID>
                            (e.g. the `event:fresh_session` / `operator:fresh_session`
                            free-text bypass the CLI now refuses).
      - "dangling"        — anchor is operator:T-NNNN but that ticket does not exist.
    """
    tid: str
    kind: str      # "unanchored" | "malformed" | "dangling"
    detail: str


def _ticket_scalar_field(path: Path, key: str) -> str | None:
    """Read one scalar frontmatter field's value (or None). Same lightweight,
    validation-free idiom as _ticket_depends_on — a ticket missing some OTHER
    required field is still analyzed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    fm = m.group(1) if m else text
    fm_match = re.search(rf"^{re.escape(key)}:\s*(.+)$", fm, re.MULTILINE)
    if not fm_match:
        return None
    val = fm_match.group(1).strip()
    return val or None


def detect_unanchored_gates(tickets_root: Path) -> list[UnanchoredGateFinding]:
    """Corpus scan (inbox+active) for not_before gates that fail the anchor rule.

    Report-only, no mutation. Dangling-ref resolution checks against ALL states
    (an operator-anchor may cite a resolved decision ticket in done/)."""
    # All ticket ids across every state, for dangling-ref resolution.
    all_ids: set[str] = set()
    for st in _DEP_GRAPH_ALL_STATES:
        d = tickets_root / st
        if not d.exists():
            continue
        for entry in d.iterdir():
            if entry.is_file() and TICKET_FILENAME_RE.fullmatch(entry.name):
                all_ids.add(entry.stem)

    findings: list[UnanchoredGateFinding] = []
    for st in STATES_TO_LINT:  # inbox + active only (done gates are inert)
        d = tickets_root / st
        if not d.exists():
            continue
        for entry in sorted(d.iterdir()):
            if not entry.is_file() or not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            not_before = _ticket_scalar_field(entry, "not_before")
            if not not_before:
                continue  # no date gate → nothing to anchor
            tid = entry.stem
            anchor = _ticket_scalar_field(entry, "not_before_anchor")
            if not anchor:
                findings.append(UnanchoredGateFinding(
                    tid, "unanchored",
                    f"{tid}: not_before {not_before} has no anchor — migrate to "
                    "--depends-on or an event-driven watch, or anchor to the operator"))
                continue
            m = _LINT_OPERATOR_ANCHOR_RE.match(anchor)
            if m:
                ref = m.group(1)
                if ref.startswith("T-") and ref not in all_ids:
                    findings.append(UnanchoredGateFinding(
                        tid, "dangling",
                        f"{tid}: not_before anchor cites {ref}, which does not exist"))
                continue
            if _LINT_EVENT_ANCHOR_RE.match(anchor):
                continue  # valid real-world-event anchor
            findings.append(UnanchoredGateFinding(
                tid, "malformed",
                f"{tid}: not_before anchor {anchor!r} is not "
                "operator:<T-NNNN|tg:MSGID> or event:<kind>:<param> "
                "(free-text reasons aren't gates)"))
    return findings


# ---------------------------------------------------------------------------
# Commit helper.
# ---------------------------------------------------------------------------


def _git_commit_fixes(
    fixed_paths: list[Path],
    now_iso: str,
    *,
    cwd: Path | None = None,
) -> tuple[bool, str]:
    """HERMES-guarded commit of auto-fixed ticket files.

    `cwd` is the working tree root for git operations. None = REPO_ROOT (legacy
    path for --repo-root safe mode); pass worktree path for with_worktree isolation.

    Order: per-file HERMES guard → commit-message HERMES belt → narrow
    git add tickets/ → scope assert → empty short-circuit → commit. Does NOT
    push — the caller (`_do_lint`) fires a single unified push_with_retry(HEAD:main)
    covering both auto-fix and retarget commits (T-0558). No force, no --no-verify.
    """
    _cwd = cwd if cwd is not None else REPO_ROOT

    n = len(fixed_paths)
    ticket_word = "ticket" if n == 1 else "tickets"
    tids = ", ".join(p.stem for p in fixed_paths)
    commit_msg = (
        f"tickets(lint): auto-fix {n} {ticket_word} — {now_iso}\n\n"
        f"Tickets fixed: {tids}\n"
        "Auto-committed by ticket_lint.py (Recovery, hourly *:20 UTC).\n"
        "Scope: tickets/inbox/ + tickets/active/ only.\n\n"
        "DECISIONS:\n"
        "- Commit scoped to touched ticket files only — justifies via P1 "
        "(structural enforcement) — scope guard below enforces; dirty shared tree "
        "cannot contaminate.\n"
        "- HERMES guard on file content + message — justifies via P1 (structural "
        "enforcement) — prevents the trigger sequence from entering git history "
        "via ticket prose.\n"
    )
    # Delegates to commit_scoped (T-1088 convergence). push=False / sync=False:
    # this function does NOT push (the caller _do_lint fires a single unified
    # push_with_retry(HEAD:main) covering both auto-fix + retarget commits, T-0558)
    # and does NOT sync here. allow_prefix stages the tickets/inbox/ + tickets/active/
    # dirs; the HERMES file-guard now scans the actually-staged ticket files.
    return commit_scoped(
        ["tickets/inbox/", "tickets/active/"], commit_msg,
        cwd=_cwd, push=False, sync=False, allow_prefix=True,
    )


# ---------------------------------------------------------------------------
# Retarget application (genuinely-improper tickets → bounce to originator).
# ---------------------------------------------------------------------------


def _apply_retarget(
    result: LintResult,
    *,
    cwd: Path | None = None,
    push: bool,
) -> tuple[bool, str]:
    """Bounce a RETARGET-class ticket back to its originator.

    Shells out to the EXISTING retarget machinery (`tools/ticket.py retarget`)
    so all its discipline (transition log, intent drop, HERMES-clean commit,
    pull-first) is reused rather than reimplemented. Only invoked from the
    real-run/commit path; dry-run/safe modes report "would retarget" instead.

    Idempotent: cmd_retarget itself no-ops when the ticket already targets the
    requester (old_target == new_target), so a repeat fire cannot loop.

    Returns (ok, message).
    """
    requester = result.requester
    if not requester or requester == "operator":
        # Defensive — detect_malformations should never emit RETARGET here.
        return False, f"{result.tid}: no bounceable originator (requester={requester!r})"

    # T-5239 / ORG-088: the bounce routes to the originator's ROLE pool, never to the
    # requester persona (a persona target is refused on every write surface).
    from tools.agent_role_map import persona_pool_role
    try:
        pool = persona_pool_role(requester)
    except PersonaRoleMembershipError as exc:
        return False, f"{result.tid}: cannot derive a role for requester {requester!r}: {exc}"
    if not pool:
        return False, f"{result.tid}: no role pool for requester {requester!r}"
    bounce_to = f"role:{pool}"

    _cwd = cwd if cwd is not None else REPO_ROOT
    note = f"ticket_lint auto-bounce (T-0556): {result.description}"
    cmd = [
        sys.executable,
        str(_cwd / "tools" / "ticket.py"),
        "retarget",
        result.tid,
        "--to",
        bounce_to,
        "--note",
        note,
    ]
    # T-0558: ALWAYS pass --no-push. ticket.py retarget's own push uses
    # `git push origin main`, which is a silent NO-OP under the detached
    # HEAD that with_worktree(..., detach=True) creates — the commit lands
    # on detached HEAD, local refs/heads/main is unmoved, push reports
    # success against the unchanged main. The unified push_with_retry in
    # _do_lint (HEAD:main refspec) is the load-bearing push for both
    # retarget commits AND auto-fix commits.
    cmd.append("--no-push")

    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(_cwd))
    if proc.returncode != 0:
        return False, (
            f"{result.tid}: retarget failed: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return True, f"{result.tid}: retargeted to {bounce_to}"


# ---------------------------------------------------------------------------
# Digest builder + Telegram sender.
# ---------------------------------------------------------------------------


def detect_program_index_findings(repo_root: Path) -> list[ProgramIndexFinding]:
    """Run the pure ORG-061 index lint when the paired indexes are configured.

    Repositories without either index retain the legacy ticket-lint behavior.
    A one-file partial deployment or strict-load error is visible and fail-closed;
    this adapter never edits either index or any stage/ticket state.
    """
    stage_path = repo_root / "tickets" / "grand-project-stage-evidence.yaml"
    obligation_path = repo_root / "tickets" / "grand-project-reevaluation-obligations.yaml"
    present = (stage_path.is_file(), obligation_path.is_file())
    if present == (False, False):
        return []
    if present != (True, True):
        return [
            ProgramIndexFinding(
                code="program-index-pair-incomplete",
                record_id="ORG-061",
                description="stage and owned-obligation indexes must be deployed as one pair",
            )
        ]
    try:
        findings = grand_project_program.lint_repository_indexes(repo_root)
    except (OSError, ValueError) as exc:
        return [
            ProgramIndexFinding(
                code="program-index-load-invalid",
                record_id="ORG-061",
                description=f"{type(exc).__name__}: {exc}",
            )
        ]
    return [
        ProgramIndexFinding(
            code=finding.code,
            record_id=finding.record_id,
            description=finding.message,
        )
        for finding in findings
    ]


def build_lint_digest(
    now_iso: str,
    auto_fixed: list[LintResult],
    flagged: list[LintResult],
    total_scanned: int,
    retargeted: list[LintResult] | None = None,
    retarget_verb: str = "Retargeted to originator",
    orphan_ratifications: list[OrphanRatification] | None = None,
    dep_graph_findings: list[DepGraphFinding] | None = None,
    unanchored_gates: list[UnanchoredGateFinding] | None = None,
    warned: list[LintResult] | None = None,
    purpose_warned: list[LintResult] | None = None,
    unattached_warned: list[LintResult] | None = None,
    typed_graph_findings: list[TypedGraphFinding] | None = None,
    program_index_findings: list[ProgramIndexFinding] | None = None,
) -> str:
    """Render the lint digest as plain text (no Markdown parse_mode).

    Header convention: `[Recovery · ticket-lint · <date>]`.
    Same skeleton as grooming digest so a Telegram reader can pattern-match sender.

    `retarget_verb` lets the caller switch between "Retargeted to originator"
    (commit-prod) and "Would retarget to originator" (dry-run/safe modes).

    `orphan_ratifications` (T-0784): pending-ratification artifacts with no open
    ratify ticket — report-only findings appended in their own digest section.

    `warned` (T-1105 / ORG-034): inbox tickets missing a serves: arc-intent field.
    Soft nudges only — never blocks, never triggers auto-fix/retarget. When
    non-empty, a separate section is appended. When empty/None, no section is
    emitted (no empty-section noise). Warns also prevent the all-clean early-return
    (an "all clean" claim is false when there are nudges to surface).

    `typed_graph_findings` (T-2841 / ORG-065 BT1, extended T-2926, T-2843,
    T-3179): the generalized typed-edge model's report-only findings (cycles
    / dangling targets / malformed or duplicate ADR `elements:` ids), PLUS
    the T-2926 corpus-wide duplicate-receipt-id findings, PLUS the T-2843
    element-lineage leak-detector findings (callers concatenate all three
    lists — see detect_typed_graph_issues / detect_duplicate_receipt_ids /
    detect_unverified_elements). Separate section from the legacy
    depends_on-only `dep_graph_findings` above. T-3179 split the leak-
    detector's own output into two kinds rendered as two sub-sections below
    — true leaks ("unverified_element") vs implemented-but-unverified
    ("implemented_unverified_element", expected transitional shape) — so the
    real leak signal isn't buried under Tranche-A's expected noise.
    """
    retargeted = retargeted or []
    orphan_ratifications = orphan_ratifications or []
    dep_graph_findings = dep_graph_findings or []
    unanchored_gates = unanchored_gates or []
    warned = warned or []
    purpose_warned = purpose_warned or []
    unattached_warned = unattached_warned or []
    typed_graph_findings = typed_graph_findings or []
    program_index_findings = program_index_findings or []
    header = f"[Recovery · ticket-lint · {now_iso}]"

    if (not auto_fixed and not flagged and not retargeted
            and not orphan_ratifications and not dep_graph_findings
            and not unanchored_gates and not warned and not purpose_warned
            and not unattached_warned
            and not typed_graph_findings
            and not program_index_findings):
        return (
            f"{header}\n\n"
            f"All {total_scanned} ticket(s) scanned: all clean.\n"
            "No auto-fixes applied. No flags raised. No retargets.\n"
            "No floating ratifications. No dependency-graph defects.\n"
        )

    lines = [header, ""]

    if program_index_findings:
        lines.append(f"Grand Project program-index findings ({len(program_index_findings)}):")
        for finding in program_index_findings:
            lines.append(
                f"  - {finding.record_id} [{finding.code}]: {finding.description}"
            )
        lines.append("")

    if auto_fixed:
        lines.append(f"Auto-fixed ({len(auto_fixed)}):")
        for r in auto_fixed:
            lines.append(f"  - {r.tid}: {r.description}")
        lines.append("")

    if retargeted:
        lines.append(f"{retarget_verb} ({len(retargeted)}):")
        for r in retargeted:
            lines.append(f"  - {r.tid} → {r.requester}: {r.description}")
        lines.append("")

    if flagged:
        lines.append(f"Flagged for HO review ({len(flagged)}):")
        for r in flagged:
            lines.append(f"  - {r.tid}: {r.description}")
        lines.append("")

    if orphan_ratifications:
        lines.append(
            f"Floating ratifications — no open ratify ticket ({len(orphan_ratifications)}):"
        )
        for o in orphan_ratifications:
            lines.append(f"  - {o.rel_path} — status: {o.status_excerpt}")
        lines.append(
            "    Fix: ticket.py file --target operator --intent ratify "
            "--ratifiable-file pantheon-config:<path>  (or update the stale status line)"
        )
        lines.append("")

    if dep_graph_findings:
        cycles = [f for f in dep_graph_findings if f.kind == "cycle"]
        stale = [f for f in dep_graph_findings if f.kind == "stale_done_dep"]
        if cycles:
            lines.append(f"Dependency CYCLES — deadlock, pick the wrong edge ({len(cycles)}):")
            for f in cycles:
                lines.append(f"  - {f.detail}")
            lines.append("")
        if stale:
            lines.append(f"Stale done-deps — resolved parent not stripped ({len(stale)}):")
            for f in stale:
                lines.append(f"  - {f.detail}")
            lines.append(
                "    Fix: remove the done dep from the open ticket's depends_on "
                "(cascade-unblock should have)."
            )
            lines.append("")

    # T-2833: not_before date gates that fail the anchor rule.
    if unanchored_gates:
        lines.append(
            f"Unanchored/bad date gates — a not_before must anchor to the operator "
            f"or be a depends_on ({len(unanchored_gates)}):"
        )
        for g in unanchored_gates:
            lines.append(f"  - [{g.kind}] {g.detail}")
        lines.append(
            "    Fix: clear the date (ticket.py block <id> --clear-not-before) and "
            "either --depends-on a real blocker, use an event-driven watch, or "
            "re-set with --not-before-anchor operator:<T-NNNN|tg:MSGID>."
        )
        lines.append("")

    # T-1105 / ORG-034: serves-field nudges — soft advisory, last section so
    # malformation sections read first. Never blocks or affects exit code.
    if warned:
        lines.append(
            f"Serves-field nudges — add --serves to these inbox tickets ({len(warned)}):"
        )
        for r in warned:
            lines.append(f"  - {r.tid}: {r.description}")
        lines.append(
            "    Fix: ticket.py retarget <tid> --to <target> --serves '<arc-intent>' "
            "(or 'goal-neutral-maintenance' for housekeeping tickets)"
        )
        lines.append("")

    # T-5014: unattached-ask advisory — report-only, never affects exit code.
    if unattached_warned:
        lines.append(
            f"the operator asks citing a file with none attached ({len(unattached_warned)}):"
        )
        for r in unattached_warned:
            lines.append(f"  - {r.tid}: {r.description}")
        lines.append(
            "    Fix: commit the file into a tracked repo, then "
            "ticket.py retarget <tid> --to operator --ratifiable-file <repo>:<path> "
            "(or --no-file '<reason>' if the ask truly has no file)"
        )
        lines.append("")

    # T-3168 / ORG-054: warn-first migration for structured purpose fields.
    # This is separate from the legacy serves nudge so callers can distinguish
    # compatibility debt from malformed/dangling typed graph state.
    if purpose_warned:
        lines.append(
            f"Structured ticket-purpose warnings ({len(purpose_warned)}):"
        )
        for r in purpose_warned:
            lines.append(f"  - {r.tid}: {r.description}")
        lines.append(
            "    Fix: ticket.py purpose <tid> --project-id <id|null> "
            "--nearest-intent-ref <typed-ref> [--serves '<arc-intent>']"
        )
        lines.append("")

    # T-2841 / ORG-065 BT1: generalized typed-edge graph findings. Report-only
    # (warn-only phase) — cycle REJECTION already happened at insertion time;
    # a "cycle" finding here means pre-existing/hand-edited data the
    # insertion guard never saw.
    if typed_graph_findings:
        typed_cycles = [f for f in typed_graph_findings if f.kind == "cycle"]
        typed_dangling = [f for f in typed_graph_findings if f.kind == "dangling_target"]
        typed_elements = [f for f in typed_graph_findings
                           if f.kind in ("malformed_id", "duplicate_id")]
        typed_receipt_dupes = [f for f in typed_graph_findings
                                if f.kind == "duplicate_receipt_id"]
        typed_episode_dupes = [f for f in typed_graph_findings
                                if f.kind == "duplicate_episode_id"]
        typed_unverified = [f for f in typed_graph_findings
                             if f.kind == "unverified_element"]
        typed_implemented_unverified = [f for f in typed_graph_findings
                                          if f.kind == "implemented_unverified_element"]
        if typed_cycles:
            lines.append(
                f"Typed-edge graph CYCLES — graph-integrity defect, ORG-065 §3.6 ({len(typed_cycles)}):"
            )
            for f in typed_cycles:
                lines.append(f"  - {f.detail}")
            lines.append("")
        if typed_dangling:
            lines.append(
                f"Typed-edge dangling targets — implements/verifies/supersedes/"
                f"informs/ratifies names an unresolvable id ({len(typed_dangling)}):"
            )
            for f in typed_dangling:
                lines.append(f"  - {f.detail}")
            lines.append("")
        if typed_elements:
            lines.append(
                f"ADR elements: id defects — malformed or duplicate ({len(typed_elements)}):"
            )
            for f in typed_elements:
                lines.append(f"  - [{f.kind}] {f.detail}")
            lines.append("")
        if typed_receipt_dupes:
            lines.append(
                f"Duplicate receipt ids — same receipt id claimed by multiple "
                f"tickets ({len(typed_receipt_dupes)}):"
            )
            for f in typed_receipt_dupes:
                lines.append(f"  - {f.detail}")
            lines.append("")
        if typed_episode_dupes:
            lines.append(
                f"Duplicate episode_ids — ORG-076 emitted more than one ticket for "
                f"one fault episode ({len(typed_episode_dupes)}):"
            )
            for f in typed_episode_dupes:
                lines.append(f"  - {f.detail}")
            lines.append("")
        if typed_unverified:
            lines.append(
                f"Unverified ADR elements — TRUE LEAK, no implements: and no "
                f"verifies: edge, ORG-065 §3.4 ({len(typed_unverified)}):"
            )
            for f in typed_unverified:
                lines.append(f"  - {f.detail}")
            lines.append("")
        if typed_implemented_unverified:
            lines.append(
                f"Implemented, evidence pending — implements: edge exists, no "
                f"verifies: edge yet (expected transitional shape, ORG-065 "
                f"§3.4) ({len(typed_implemented_unverified)}):"
            )
            for f in typed_implemented_unverified:
                lines.append(f"  - {f.detail}")
            lines.append("")

    lines.append(f"Total scanned: {total_scanned} ticket(s).")
    return "\n".join(lines) + "\n"


def _chunk_for_telegram(text: str, limit: int = _TELEGRAM_MAX_TEXT) -> list[str]:
    """Split text into chunks <= limit chars at paragraph boundaries."""
    if len(text) <= limit:
        return [text]
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = (current + "\n\n" + para) if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(para) <= limit:
            current = para
        else:
            for i in range(0, len(para), limit):
                chunks.append(para[i: i + limit])
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _send_telegram_plain(token: str, chat_id: str, text: str) -> bool:
    """Send a plain-text Telegram message (no parse_mode)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:  # noqa: BLE001
        print(f"telegram send failed: {exc}", file=sys.stderr)
        return False


def _send_lint_digest(text: str) -> bool:
    """Resolve token + send. No-token = warning + treat as success."""
    token = read_telegram_token(TELEGRAM_ENV)
    if not token:
        print(
            f"warning: no Telegram token at {TELEGRAM_ENV}; skipping send",
            file=sys.stderr,
        )
        return True
    chunks = _chunk_for_telegram(text)
    all_ok = True
    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            chunk = f"[part {i + 1}/{len(chunks)}]\n{chunk}"
        if not _send_telegram_plain(token, TELEGRAM_CHAT_ID, chunk):
            all_ok = False
    return all_ok


# ---------------------------------------------------------------------------
# Core lint loop (worktree-safe — no module-global rebind).
# ---------------------------------------------------------------------------


def _do_lint(
    wt_path: Path | None,
    *,
    push: bool,
    send_digest: bool,
    tickets_root: Path | None = None,
    repo_root: Path | None = None,
) -> int:
    """Inner function: scan + fix + commit. Runs inside the worktree callback.

    T-0478: do NOT rebind module-global REPO_ROOT here. Thread wt_path
    explicitly via parameters — mirrors spine_index_groom's correct pattern.

    In worktree mode (wt_path is not None): use wt_path/tickets as the scan
    root and wt_path as git cwd.
    In test/safe mode (wt_path=None): use the caller-supplied tickets_root
    (which may be a tmp repo for --repo-root, or module-global TICKETS_ROOT
    if tests patch it).
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(microsecond=0)
    now_iso = now.strftime("%Y-%m-%d %H:%M UTC")

    # Resolve scan root + repo root without touching module globals.
    if wt_path is not None:
        _tickets_root = wt_path / "tickets"
        _repo_root = wt_path
        _cwd = wt_path
    else:
        # Safe/test mode: use caller-provided overrides.
        _tickets_root = tickets_root if tickets_root is not None else (REPO_ROOT / "tickets")
        _repo_root = repo_root if repo_root is not None else REPO_ROOT
        _cwd = None  # git ops use REPO_ROOT default (safe mode doesn't push to origin)

    results = scan_tickets(_tickets_root, repo_root=_repo_root)
    total_scanned = len(results)

    # T-0784: corpus-level floating-ratification scan (report-only; no mutation).
    orphan_ratifications = detect_orphan_ratifications(_repo_root, _tickets_root)

    # T-0975: corpus-level depends_on-graph scan — cycles + stale done-deps
    # (report-only; no mutation).
    dep_graph_findings = detect_dep_graph_issues(_tickets_root)

    # T-2833: corpus-level not_before-anchor scan — unanchored / malformed /
    # dangling date gates (report-only; surfaces gates for migration).
    unanchored_gates = detect_unanchored_gates(_tickets_root)

    # T-1105 / ORG-034: serves-field advisory warnings. Pass all scanned paths
    # to warn_tickets(); it self-filters to inbox-only (single source of truth
    # for that predicate). Warns never affect failures, push, or exit code.
    warn_results = warn_tickets([r.path for r in results])
    purpose_warn_results = warn_ticket_purposes([r.path for r in results])
    unattached_warn_results = warn_operator_unattached([r.path for r in results])

    # T-2841 / ORG-065 BT1: generalized typed-edge graph scan — cycles +
    # dangling targets + ADR elements: id defects (report-only; warn-only
    # phase, no mutation). Concatenated with the T-2926 corpus-wide receipt-
    # id uniqueness scan and the T-2843 element-lineage leak detector — all
    # three feed the same typed_graph_findings report section (see
    # TypedGraphFinding's kind docstring for why they are separate functions
    # sharing one finding shape).
    # T-3477 / ORG-065 Call 2, REVERTED T-3495: the ADR corpus here is this
    # repo's own architecture/adr/ ONLY — recovery (the sole hourly cron runner)
    # cannot read echo-prismatic's adr/ (EACCES), which made the cross-repo
    # union vacuous on the only account that ran it. Echo obligation
    # validation now runs as a companion scan on the claude account (see
    # tools/echo_corpus_lint.py); the dangling-target check below exempts
    # echo-shaped targets via is_echo_scoped_id rather than false-flagging
    # them (see _ECHO_PRISMATIC_ADR_DIR's module note).
    _adr_dirs = _repo_root / "architecture" / "adr"
    typed_graph_findings = (
        detect_typed_graph_issues(
            _tickets_root, _adr_dirs, exempt_dangling_target=is_echo_scoped_id,
        )
        + detect_duplicate_receipt_ids(_tickets_root)
        + detect_duplicate_episode_ids(_tickets_root)
        + detect_unverified_elements(_tickets_root, _adr_dirs)
    )

    # T-2426 / ORG-061: paired program indexes are a pure, report-only lint
    # surface. Their findings never trigger ticket/index mutation here.
    program_index_findings = detect_program_index_findings(_repo_root)

    auto_fix_results = [r for r in results if r.action == LintAction.AUTO_FIX]
    flag_results = [r for r in results if r.action == LintAction.FLAG]
    retarget_results = [r for r in results if r.action == LintAction.RETARGET]

    failures = 0
    fixed_paths: list[Path] = []

    # Apply auto-fixes.
    for r in auto_fix_results:
        try:
            apply_auto_fix(r.path)
            fixed_paths.append(r.path)
        except LintError as exc:
            print(f"error: auto-fix failed for {r.path.name}: {exc}", file=sys.stderr)
            failures += 1

    # Apply retargets — ONLY in the real-run/commit-prod path (wt_path not None).
    # Mirrors the AUTO_FIX commit gating: in --repo-root safe mode (_cwd is None)
    # we do NOT mutate; the digest reports "would retarget" instead.
    retargeted_done: list[LintResult] = []
    is_commit_path = wt_path is not None
    # RETARGET_AUTO_APPLY gate (T-0556): the live mutation is OFF by default
    # until the commit-path integration is validated. When off, retarget_results
    # are surfaced in the digest as "would retarget" but not mutated.
    if is_commit_path and RETARGET_AUTO_APPLY:
        for r in retarget_results:
            # T-0558: _apply_retarget itself runs ticket.py retarget with
            # --no-push (commit-only). The unified push below covers it.
            ok, rmsg = _apply_retarget(r, cwd=_cwd, push=False)
            print(f"[retarget] {rmsg}", file=sys.stdout if ok else sys.stderr)
            if ok:
                retargeted_done.append(r)
            else:
                failures += 1

    # Commit auto-fixes if any — only in worktree/prod mode (wt_path not None).
    # In safe --repo-root mode (_cwd is None): fixes are written to disk but
    # not committed (the tmp repo is ephemeral; no git commit needed for smoke/test).
    # T-0558: _git_commit_fixes itself no longer pushes — the unified push
    # below covers both auto-fix commits AND retarget commits in one
    # HEAD:main refspec.
    commit_ok = True
    if fixed_paths and wt_path is not None:
        today_iso = now.date().isoformat()
        ok, gmsg = _git_commit_fixes(
            fixed_paths,
            today_iso,
            cwd=_cwd,
        )
        print(f"[git] {gmsg}", file=sys.stdout if ok else sys.stderr)
        if not ok:
            failures += 1
            commit_ok = False

    # T-0558: UNIFIED push covering both retarget commits AND auto-fix
    # commits. Without unification, a retarget-only run (no fixed_paths)
    # would never push — _git_commit_fixes' empty short-circuit (or skip)
    # means its previous push call never ran, and ticket.py retarget's own
    # push is a silent no-op under detached HEAD. push_with_retry's
    # HEAD:main refspec is the only push shape that lands the detached
    # commit on origin/main.
    if push and is_commit_path and (retargeted_done or (fixed_paths and commit_ok)):
        ok_push, push_msg = push_with_retry(_cwd, refspec="HEAD:main")
        if not ok_push:
            print(f"[git] unified push failed: {push_msg}", file=sys.stderr)
            failures += 1
            commit_ok = False

    # Build and send digest.
    if send_digest:
        if is_commit_path and RETARGET_AUTO_APPLY:
            digest_retargeted = retargeted_done
            retarget_verb = "Retargeted to originator"
        else:
            # Mutation not applied (safe/--repo-root mode, or RETARGET_AUTO_APPLY
            # off): report what WOULD happen, originator + defect named, no mutation.
            digest_retargeted = retarget_results
            retarget_verb = "Would retarget to originator"
        digest = build_lint_digest(
            now_iso=now_iso,
            auto_fixed=[r for r in auto_fix_results if r.path in fixed_paths],
            flagged=flag_results,
            total_scanned=total_scanned,
            retargeted=digest_retargeted,
            retarget_verb=retarget_verb,
            orphan_ratifications=orphan_ratifications,
            dep_graph_findings=dep_graph_findings,
            unanchored_gates=unanchored_gates,
            warned=warn_results,
            purpose_warned=purpose_warn_results,
            unattached_warned=unattached_warn_results,
            typed_graph_findings=typed_graph_findings,
            program_index_findings=program_index_findings,
        )
        if failures > 0 or not commit_ok:
            digest += f"\nWARNING: ticket_lint auto-commit reported {failures} failure(s); see stderr.\n"
        ok = _send_lint_digest(digest)
        if not ok:
            print("warning: telegram digest send failed", file=sys.stderr)

    return 1 if failures else 0


# ---------------------------------------------------------------------------
# run_all entry (with_worktree isolation).
# ---------------------------------------------------------------------------


def run_all(*, push: bool, send_digest: bool) -> int:
    """Single-fire entry for production cron (--commit-prod).

    Wraps the lint loop in with_worktree so each cron fire runs in an isolated
    /tmp/ worktree off origin/main (T-0471 pattern; justifies-via P1).
    """
    def _cb(wt_path: Path) -> int:
        return _do_lint(wt_path, push=push, send_digest=send_digest)

    return _with_worktree(_cb)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ticket_lint.py",
        description=(
            "Pantheon ticket-lint hourly frontmatter scanner. Default-safe "
            "contract (T-0260): must pass --commit-prod, --dry-run, or "
            "--repo-root <path> explicitly."
        ),
    )
    p.add_argument(
        "--commit-prod", action="store_true",
        help=(
            "real run: scan+fix+commit+push to production. "
            "systemd cron sets this; manual supervised live fire also uses this."
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="scan + report; no writes, no commits, no digest send",
    )
    p.add_argument(
        "--repo-root", type=Path, default=None,
        help="redirect to a tmp pantheon-config dir (safe mode for testing/smoke)",
    )
    p.add_argument(
        "--no-digest", action="store_true",
        help="commit+push but skip Telegram send (modifier; pair with --commit-prod or --repo-root)",
    )
    return p.parse_args(argv)


def _enforce_default_safe(args: argparse.Namespace) -> int | None:
    """Refuse bare invocation without explicit mode (T-0260 default-safe contract).

    Returns exit code 64 if the invocation should be rejected; None otherwise.
    """
    safe_explicit = args.dry_run or (args.repo_root is not None)
    if args.commit_prod and safe_explicit:
        print(
            "ERROR: --commit-prod conflicts with safe-mode flag (--dry-run / --repo-root).",
            file=sys.stderr,
        )
        return 64
    if not args.commit_prod and not safe_explicit:
        print(
            "ERROR: refusing to run without explicit mode. Pass one of:\n"
            "  --commit-prod        commit + push to production (cron + supervised live fire)\n"
            "  --dry-run            scan + report only; no side effects\n"
            "  --repo-root <path>   redirect to a tmp dir (smoke/test path)\n"
            "Per sop/standard/auto-commit-cron-default-safe-contract.md (T-0260).",
            file=sys.stderr,
        )
        return 64
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Default-safe gate.
    refusal = _enforce_default_safe(args)
    if refusal is not None:
        return refusal

    send_digest = not args.no_digest

    # --dry-run: scan + print, no writes.
    if args.dry_run:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).replace(microsecond=0)
        repo_root_arg = args.repo_root.resolve() if args.repo_root else REPO_ROOT
        tickets_root_dry = repo_root_arg / "tickets"
        results = scan_tickets(tickets_root_dry, repo_root=repo_root_arg)
        auto_fix = [r for r in results if r.action == LintAction.AUTO_FIX]
        flags = [r for r in results if r.action == LintAction.FLAG]
        retargets = [r for r in results if r.action == LintAction.RETARGET]
        dep_graph_findings = detect_dep_graph_issues(tickets_root_dry)
        # T-1105 / ORG-034: dry-run warn parity — same tickets as the cron digest shows.
        dry_warns = warn_tickets([r.path for r in results])
        dry_unattached = warn_operator_unattached([r.path for r in results])
        # T-2841 / ORG-065 BT1: dry-run parity for the generalized typed-edge graph.
        # T-2926: dry-run parity for the corpus-wide receipt-id uniqueness scan.
        # T-2843: dry-run parity for the element-lineage leak detector.
        # T-3477, REVERTED T-3495: dry-run parity with the non-dry-run
        # _adr_dirs construction above — pantheon-only, echo union moved to
        # tools/echo_corpus_lint.py (see that call site's comment).
        _adr_dirs_dry = repo_root_arg / "architecture" / "adr"
        typed_graph_findings = (
            detect_typed_graph_issues(
                tickets_root_dry, _adr_dirs_dry, exempt_dangling_target=is_echo_scoped_id,
            )
            + detect_duplicate_receipt_ids(tickets_root_dry)
            + detect_duplicate_episode_ids(tickets_root_dry)
            + detect_unverified_elements(tickets_root_dry, _adr_dirs_dry)
        )
        print(f"[ticket_lint] dry-run — {now.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"  scanned: {len(results)} ticket(s)")
        if auto_fix:
            print(f"  would auto-fix ({len(auto_fix)}):")
            for r in auto_fix:
                print(f"    {r.tid}: {r.description}")
        if retargets:
            print(f"  would retarget ({len(retargets)}):")
            for r in retargets:
                print(f"    {r.tid} → {r.requester}: {r.description}")
        if flags:
            print(f"  would flag ({len(flags)}):")
            for r in flags:
                print(f"    {r.tid}: {r.description}")
        if dep_graph_findings:
            print(f"  dependency-graph defects ({len(dep_graph_findings)}):")
            for f in dep_graph_findings:
                print(f"    [{f.kind}] {f.detail}")
        if dry_warns:
            print(f"  serves-field nudges ({len(dry_warns)}):")
            for r in dry_warns:
                print(f"    {r.tid}: {r.description}")
        if dry_unattached:
            print(f"  operator asks citing a file with none attached ({len(dry_unattached)}):")
            for r in dry_unattached:
                print(f"    {r.tid}: {r.description}")
        if typed_graph_findings:
            print(f"  typed-edge graph findings ({len(typed_graph_findings)}):")
            for f in typed_graph_findings:
                print(f"    [{f.kind}] {f.detail}")
        if (not auto_fix and not flags and not retargets and not dep_graph_findings
                and not dry_warns and not dry_unattached and not typed_graph_findings):
            print("  all clean — no actions needed")
        return 0

    # --commit-prod: wrap in with_worktree.
    if args.commit_prod:
        return run_all(push=True, send_digest=send_digest)

    # --repo-root safe mode: run directly against the tmp repo (no worktree).
    repo_root_safe = args.repo_root.resolve()
    return _do_lint(
        wt_path=None,
        push=False,  # safe mode never pushes to real origin
        send_digest=send_digest,
        tickets_root=repo_root_safe / "tickets",
        repo_root=repo_root_safe,
    )


if __name__ == "__main__":
    sys.exit(main())
