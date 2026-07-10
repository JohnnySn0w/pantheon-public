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
from tools.agent_role_map import resolve_instance_role, resolve_persona  # noqa: E402
from tools.ticket import (  # noqa: E402
    TICKET_FILENAME_RE,
    TicketError,
    _build_ticket_text,
    parse_front_matter,
    serialize_front_matter,
)
from tools.watch_digest import read_telegram_token  # noqa: E402

# Role IDs that are not persona-canonical — detected for AUTO-FIX class 3.
# operator is excluded (no persona alias; passes through unchanged).
_ROLE_ID_TARGETS: frozenset[str] = frozenset({"orchestrator", "specialist"})

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
TELEGRAM_CHAT_ID = os.environ.get("PANTHEON_TELEGRAM_CHAT_ID", "456474197")  # the operator

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


@dataclass
class OrphanRatification:
    """A pending-ratification artifact with no open ratify ticket pulling on it.

    REPORT-ONLY (T-0784): surfaced in the digest as a floating-ratification
    finding. No auto-fix, no auto-file — the operator either files a ratify
    ticket naming this path or updates the artifact's stale status line.
    """
    rel_path: str        # repo-relative path to the artifact (e.g. adr/ORG-013-...md)
    status_excerpt: str  # the matched status line, for operator context


@dataclass
class DepGraphFinding:
    """A structural defect in the ticket depends_on graph (report-only; T-0975).

    Two kinds, both surfaced in the digest and never auto-fixed:
      - "cycle": a set of OPEN tickets whose depends_on edges form a cycle — an
        unbreakable deadlock (none can ever unblock). A human must decide which
        edge is wrong (a common cause: a deploy/carrier CHILD wrongly gated on
        its feature parent, which already depends on the carrier). Found live
        2026-06-08 (T-0367 <-> T-0470). Neither the per-file frontmatter lint
        nor a done/missing-dep scan catches these.
      - "stale_done_dep": an OPEN ticket whose depends_on names an already-DONE
        ticket that ORG-013 cascade-unblock failed to strip (T-0971 fixes that
        forward-only; nothing retroactively scans). Harmless to readiness (done
        deps already count as satisfied) but it is bookkeeping cruft and a
        signal cascade-unblock missed a case. 2 instances found+fixed 2026-06-08.
    """
    kind: str          # "cycle" | "stale_done_dep"
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

    # ---- AUTO-FIX class 3: target is a role-ID, not a persona name (T-0492) ----
    # Persona names are now canonical for ticket target; role IDs are accepted
    # as aliased input but normalized to persona names on write (msg_1661).
    if target in _ROLE_ID_TARGETS:
        persona = resolve_persona(target)
        return LintResult(
            tid=tid,
            path=path,
            action=LintAction.AUTO_FIX,
            description=(
                f"target={target!r} is a role-ID; persona name is canonical "
                f"per T-0492/msg_1661; will normalize to {persona!r}"
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
    if target == "operator" and intent == "ratify" and ratifiable_files:
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
                    f"the path before operator can ratify"
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

    target = fields.get("target", "")
    intent = fields.get("intent")
    ratifiable_files = fields.get("ratifiable_files") or []

    if target == "operator" and intent is None:
        # Class 1: set intent based on ratifiable_files presence.
        fields["intent"] = "ratify" if ratifiable_files else "action"

    elif intent is not None and target != "operator":
        # Class 2: drop intent — it is operator-only.
        del fields["intent"]

    elif target in _ROLE_ID_TARGETS:
        # Class 3 (T-0492): normalize role-ID target to persona name.
        fields["target"] = resolve_persona(target)

    new_text = _build_ticket_text(fields, body)
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
        # Only warn on inbox tickets.
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
                "'goal-neutral-maintenance' is valid for housekeeping tickets"
            ),
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
    "adr",
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


def detect_dep_graph_issues(tickets_root: Path) -> list[DepGraphFinding]:
    """Corpus-level depends_on-graph scan: cycles + stale done-deps.

    Report-only (no mutation). Returns stale-done-dep findings first (sorted),
    then one finding per distinct cycle.
    """
    state: dict[str, str] = {}          # tid -> directory/state
    deps: dict[str, list[str]] = {}     # tid -> depends_on (open tickets only)
    for st in _DEP_GRAPH_ALL_STATES:
        d = tickets_root / st
        if not d.exists():
            continue
        for entry in sorted(d.iterdir()):
            if not entry.is_file() or not TICKET_FILENAME_RE.fullmatch(entry.name):
                continue
            tid = entry.stem
            state[tid] = st
            if st in _DEP_GRAPH_OPEN_STATES:
                deps[tid] = _ticket_depends_on(entry)

    findings: list[DepGraphFinding] = []

    # (1) Stale done-deps: an open ticket pointing at an already-resolved dep.
    for tid in sorted(deps):
        for dep in deps[tid]:
            if state.get(dep) == "done":
                findings.append(DepGraphFinding(
                    kind="stale_done_dep",
                    detail=(f"{tid} depends_on {dep}, which is DONE — "
                            "cascade-unblock should have removed it"),
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

    _cwd = cwd if cwd is not None else REPO_ROOT
    note = f"ticket_lint auto-bounce (T-0556): {result.description}"
    cmd = [
        sys.executable,
        str(_cwd / "tools" / "ticket.py"),
        "retarget",
        result.tid,
        "--to",
        requester,
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
    return True, f"{result.tid}: retargeted to {requester}"


# ---------------------------------------------------------------------------
# Digest builder + Telegram sender.
# ---------------------------------------------------------------------------


def build_lint_digest(
    now_iso: str,
    auto_fixed: list[LintResult],
    flagged: list[LintResult],
    total_scanned: int,
    retargeted: list[LintResult] | None = None,
    retarget_verb: str = "Retargeted to originator",
    orphan_ratifications: list[OrphanRatification] | None = None,
    dep_graph_findings: list[DepGraphFinding] | None = None,
    warned: list[LintResult] | None = None,
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
    """
    retargeted = retargeted or []
    orphan_ratifications = orphan_ratifications or []
    dep_graph_findings = dep_graph_findings or []
    warned = warned or []
    header = f"[Recovery · ticket-lint · {now_iso}]"

    if (not auto_fixed and not flagged and not retargeted
            and not orphan_ratifications and not dep_graph_findings
            and not warned):
        return (
            f"{header}\n\n"
            f"All {total_scanned} ticket(s) scanned: all clean.\n"
            "No auto-fixes applied. No flags raised. No retargets.\n"
            "No floating ratifications. No dependency-graph defects.\n"
        )

    lines = [header, ""]

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

    # T-1105 / ORG-034: serves-field advisory warnings. Pass all scanned paths
    # to warn_tickets(); it self-filters to inbox-only (single source of truth
    # for that predicate). Warns never affect failures, push, or exit code.
    warn_results = warn_tickets([r.path for r in results])

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
            warned=warn_results,
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
        if not auto_fix and not flags and not retargets and not dep_graph_findings and not dry_warns:
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
