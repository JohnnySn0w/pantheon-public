#!/usr/bin/env python3
"""Delivery-machine REBASE-crash controller (ORG-096 / RFC-002, ticket T-3651).

The crash-safe contract for the delivery machine's *rebase edge*: the window
between a non-fast-forward push rejection and a clean re-push after rebasing onto
the advanced remote. Every kill-point recovers to exactly one well-defined state
-- never a silent phantom rewrite, never a partial-HEAD push.

Standalone-with-seam: the ORG-076 emission-phase machine, ORG-088
classify_rebind_conflict, and the `episode_id` frontmatter field are all
spec-only today, so this controller is built on `ticket.py file --no-push` +
`tools/_git_utils.py` primitives and owns its OWN durable rebase-edge state. The
future ORG-076 emission machine wires in through `on_nonff_rejection` /
`recover` (public wrappers) or `_deliver_locked` / `_recover_locked` (lock-free
cores, for a caller holding one git_mutation_lock across commit+push+edge).

See architecture/rfc/RFC-002-delivery-rebase-crash-controller.md (R1-R9) and
architecture/adr/ORG-096-delivery-rebase-crash-contract.md (D-1..D-7).

Tests: python3 -m unittest tools.test_delivery_rebase_controller
"""
from __future__ import annotations

import argparse
import dataclasses
import enum
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make `from tools._git_utils import ...` work both as a script and as a package
# import (mirrors the tools/ticket.py bootstrap).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import _git_utils  # noqa: E402 — after sys.path bootstrap.
from tools._git_utils import _NON_FF_MARKERS  # noqa: E402
from tools.agent_role_map import mailbox_hub  # noqa: E402  (T-4096: hub from instance.yaml)

SCHEMA_VERSION = 1
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon" / "delivery-rebase"
MAX_REBASE_ATTEMPTS = 3      # bounds a single _deliver_locked forward loop (R4)
MAX_REENTERS = 5             # bounds the recover->RE_ENTER->re-drive composition (R6/NEW-4)
MAILBOX_HUB = mailbox_hub()

PHASE_INFLIGHT = "REBASE_INFLIGHT"
PHASE_COMPLETED = "REBASE_COMPLETED"

# Network-ish fetch/push stderr signatures -> TypedFailure(NETWORK); everything
# else non-ff-adjacent that is not a clean rejection is treated as AUTH/CORRUPTION.
_NETWORK_MARKERS = ("could not resolve host", "connection", "timed out", "network",
                    "unable to access", "failed to connect")
_AUTH_MARKERS = ("authentication", "permission denied", "403", "denied", "not authorized")


# --------------------------------------------------------------------------- #
# Exceptions (R8)
# --------------------------------------------------------------------------- #
class DeliveryRebaseError(Exception):
    """Base for all controller errors."""


class AmbiguousEpisodeError(DeliveryRebaseError):
    """>1 introducing commit for an episode_id on the local branch or on origin (R5/R4)."""


class HeadMovedError(DeliveryRebaseError):
    """After a mid-conflict abort the episode is unfindable -- a foreign process rewrote HEAD (R6 case ii)."""


class AbortFailedError(DeliveryRebaseError):
    """`git rebase --abort` itself returned non-zero -- corrupt rebase state (R6/M1)."""


class StateCorruptionError(DeliveryRebaseError):
    """Persisted state JSON is unreadable or schema-mismatched (R8)."""


class TypedFailure(DeliveryRebaseError):
    """auth / network / corruption failure at the git transport layer (R4)."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        super().__init__(f"{kind}: {detail}")


# --------------------------------------------------------------------------- #
# Outcomes + classification (R3, R4)
# --------------------------------------------------------------------------- #
class RebaseOutcome(enum.Enum):
    PUSHED = "PUSHED"
    ADOPTED_ORIGIN = "ADOPTED_ORIGIN"
    RE_ENTER = "RE_ENTER"
    UNVERIFIED = "UNVERIFIED"
    TYPED_FAILURE = "TYPED_FAILURE"
    PAGED = "PAGED"


@dataclasses.dataclass(frozen=True)
class RebaseResult:
    outcome: RebaseOutcome
    episode_id: str
    prev_sha: str | None
    effective_sha: str | None
    detail: str


class ClassKind(enum.Enum):
    ADD_ADD = "ADD_ADD"
    ORIGIN_ADVANCE = "ORIGIN_ADVANCE"
    TYPED_FAILURE = "TYPED_FAILURE"


@dataclasses.dataclass(frozen=True)
class Classification:
    kind: ClassKind
    origin_commit_sha: str | None = None
    typed_kind: str | None = None
    detail: str = ""


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# Git observables (R5, R6)
# --------------------------------------------------------------------------- #
def _rev_parse(repo: Path, ref: str, *, git=_git_utils) -> str | None:
    res = git._git("rev-parse", ref, cwd=repo)
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None


def is_rebase_in_progress(repo: Path, *, git=_git_utils) -> bool:
    """Worktree-correct rebase-state-dir predicate (R6). Extracted from the pattern
    inlined in _git_utils._abort_stale_rebase (:496-504); the only definition."""
    for kind in ("rebase-merge", "rebase-apply"):
        res = git._git("rev-parse", "--git-path", kind, cwd=repo)
        if res.returncode != 0:
            continue
        rel = res.stdout.strip()
        if not rel:
            continue
        path = Path(rel) if os.path.isabs(rel) else (repo / rel)
        if path.exists():
            return True
    return False


def _find_introducing_commits(episode_id: str, revrange: str, *, repo: Path, git=_git_utils) -> list[str]:
    """Commits in `revrange` (restricted to tickets/) that changed the count of the
    episode_id frontmatter line -- i.e. INTRODUCED it (M5). Pickaxe (-S) over the
    canonical `episode_id: <id>` line. Returns [] if the range cannot be computed."""
    needle = f"episode_id: {episode_id}"
    res = git._git("log", "--format=%H", "-S", needle, revrange, "--", "tickets/", cwd=repo)
    if res.returncode != 0:
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def find_episode_commit(episode_id: str, *, repo: Path, git=_git_utils) -> str | None:
    """The single local-ahead commit that introduced `episode_id` (R5/D-6). The
    rewrite AUTHORITY -- not `git rev-parse HEAD` (the episode may sit below HEAD).
    >1 -> AmbiguousEpisodeError -> PAGED; 0 -> None."""
    commits = _find_introducing_commits(episode_id, "origin/main..HEAD", repo=repo, git=git)
    if len(commits) > 1:
        raise AmbiguousEpisodeError(
            f"{episode_id}: {len(commits)} introducing commits in origin/main..HEAD: {commits}"
        )
    return commits[0] if commits else None


# --------------------------------------------------------------------------- #
# Durable state (R2)
# --------------------------------------------------------------------------- #
def _state_path(episode_id: str, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    return state_dir / f"{episode_id}.json"


def save_state(episode_id: str, phase: str, prev_sha: str, *,
               new_sha: str | None = None, origin_sha_at_entry: str | None = None,
               reenter_count: int = 0, state_dir: Path = DEFAULT_STATE_DIR,
               now=_utcnow) -> dict[str, Any]:
    """Atomic write (tmp + os.replace) of one per-episode state file (R2). Preserves
    created_at across updates. Threat model = process-kill (no fsync; power-loss OOS)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _state_path(episode_id, state_dir)
    created = None
    try:
        existing = json.loads(path.read_text())
        created = existing.get("created_at")
    except Exception:
        created = None
    ts = now()
    doc = {
        "schema": SCHEMA_VERSION,
        "episode_id": episode_id,
        "phase": phase,
        "prev_sha": prev_sha,
        "new_sha": new_sha,
        "origin_sha_at_entry": origin_sha_at_entry,
        "reenter_count": reenter_count,
        "created_at": created or ts,
        "updated_at": ts,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, path)
    return doc


def load_state(episode_id: str, state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, Any] | None:
    """Return the persisted state, None if absent, or raise StateCorruptionError on
    unreadable/schema-mismatched JSON (R8 -- the caller's Step 1 still runs)."""
    path = _state_path(episode_id, state_dir)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    try:
        doc = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise StateCorruptionError(f"{episode_id}: unreadable state JSON: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA_VERSION:
        raise StateCorruptionError(f"{episode_id}: schema mismatch: {doc!r:.120}")
    return doc


def delete_state(episode_id: str, state_dir: Path = DEFAULT_STATE_DIR) -> None:
    _state_path(episode_id, state_dir).unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Classifier (R4) + ancestry proof (R6)
# --------------------------------------------------------------------------- #
def _typed_kind(stderr: str) -> str:
    low = stderr.lower()
    if any(m in low for m in _NETWORK_MARKERS):
        return "NETWORK"
    if any(m in low for m in _AUTH_MARKERS):
        return "AUTH"
    return "CORRUPTION"


def classify(episode_id: str, prev_sha: str, *, repo: Path, git=_git_utils) -> Classification:
    """Fetch origin/main and classify the non-ff rejection (R4). Runs before every
    persist and before the recovery discriminator (the H1/H2 ordering)."""
    fetch = git._git("fetch", "origin", "main", cwd=repo)
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout).strip()
        return Classification(ClassKind.TYPED_FAILURE, typed_kind=_typed_kind(detail), detail=detail)
    origin_commits = _find_introducing_commits(episode_id, "origin/main", repo=repo, git=git)
    if len(origin_commits) > 1:
        raise AmbiguousEpisodeError(
            f"{episode_id}: {len(origin_commits)} introducing commits on origin/main: {origin_commits}"
        )
    if len(origin_commits) == 1:
        return Classification(ClassKind.ADD_ADD, origin_commit_sha=origin_commits[0],
                              detail=f"origin already carries {episode_id} at {origin_commits[0]}")
    return Classification(ClassKind.ORIGIN_ADVANCE, detail="origin advanced; no colliding episode_id")


def ancestry_proof(episode_id: str, effective_sha: str, *, repo: Path, git=_git_utils) -> bool:
    """PUSHED/ADOPTED -> VERIFIED (R6): effective_sha is an ancestor of origin/main
    AND episode_id resolves on origin to exactly one introducing commit == effective_sha."""
    if git._git("fetch", "origin", "main", cwd=repo).returncode != 0:
        return False
    if git._git("merge-base", "--is-ancestor", effective_sha, "origin/main", cwd=repo).returncode != 0:
        return False
    origin_commits = _find_introducing_commits(episode_id, "origin/main", repo=repo, git=git)
    return len(origin_commits) == 1 and origin_commits[0] == effective_sha


# --------------------------------------------------------------------------- #
# Paging (R8)
# --------------------------------------------------------------------------- #
def _page(subject: str, body: str, *, send=None) -> None:
    """Mailbox Recovery (delivery-surface owner). Injectable for tests."""
    if send is not None:
        send(subject, body)
        return
    try:
        from tools import mailbox  # lazy; tools/ on sys.path
        mailbox.send(f"{MAILBOX_HUB}/recovery", body, sender="delivery-rebase-controller", subject=subject)
    except Exception as exc:  # never let paging failure mask the outcome
        print(f"[delivery-rebase] PAGE (mailbox failed: {exc}): {subject}\n{body}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# The push helper (D-5 guard)
# --------------------------------------------------------------------------- #
def _push_head_main(repo: Path, *, git=_git_utils) -> tuple[bool, str, bool]:
    """(ok, detail, is_non_ff). Refuses to push while a rebase is in progress (D-5)."""
    if is_rebase_in_progress(repo, git=git):
        return False, "refused: rebase-state dir present (D-5)", False
    res = git._git("push", "origin", "HEAD:main", cwd=repo)
    if res.returncode == 0:
        # T-4691: ORG-101 dual-write leg only. No failover: the ancestry proof is
        # against origin/main. Called on the real module, not the injectable `git`
        # (test fakes stub only _git), and inert unless a mirror is writable.
        note = _git_utils.mirror_push_after_origin(repo, "HEAD:main")
        return True, f"pushed HEAD:main; {note}" if note else "pushed HEAD:main", False
    stderr = (res.stderr or res.stdout)
    # T-4388: the pre-push freshness hook (`git pull --rebase`) can stop on a conflict
    # and exit 1 without aborting. No rebase was in progress at entry (checked above),
    # so this one is the hook's: abort it rather than strand the checkout. Origin had
    # moved ahead, so report it as non-ff and let the caller re-classify (RE_ENTER).
    if is_rebase_in_progress(repo, git=git):
        aborted = git._git("rebase", "--abort", cwd=repo).returncode == 0 and not is_rebase_in_progress(repo, git=git)
        if aborted:
            return False, f"pre-push freshness rebase conflicted; aborted (origin moved ahead): {stderr.strip()}", True
        return False, f"pre-push freshness rebase conflicted and `rebase --abort` FAILED; {repo} is STILL MID-REBASE: {stderr.strip()}", False
    return False, stderr.strip(), any(m in stderr.lower() for m in _NON_FF_MARKERS)


# --------------------------------------------------------------------------- #
# Cores (R3, R6) -- assume git_mutation_lock is ALREADY held; never re-acquire.
# --------------------------------------------------------------------------- #
def _result(outcome, episode_id, prev_sha, effective_sha, detail):
    return RebaseResult(outcome, episode_id, prev_sha, effective_sha, detail)


def _finish_push(episode_id: str, prev_sha: str, effective_sha: str, *, repo: Path,
                 state_dir: Path, git, send_page) -> RebaseResult:
    """D-5-guarded push of HEAD:main + ancestry proof. Shared by delivery and 3b."""
    ok, detail, is_non_ff = _push_head_main(repo, git=git)
    if ok:
        if ancestry_proof(episode_id, effective_sha, repo=repo, git=git):
            delete_state(episode_id, state_dir)
            return _result(RebaseOutcome.PUSHED, episode_id, prev_sha, effective_sha, "pushed + verified")
        return _result(RebaseOutcome.UNVERIFIED, episode_id, prev_sha, effective_sha,
                       "pushed; ancestry proof pending (state retained)")
    if is_non_ff:
        return _result(RebaseOutcome.RE_ENTER, episode_id, prev_sha, effective_sha,
                       f"push rejected non-ff; re-enter to re-classify: {detail}")
    return _result(RebaseOutcome.TYPED_FAILURE, episode_id, prev_sha, None,
                   f"push failed (non-retryable): {detail}")


def _adopt_origin(episode_id: str, prev_sha: str, origin_sha: str, *, repo: Path,
                  state_dir: Path, git, now, send_page) -> RebaseResult:
    """Adopt origin's commit for this episode_id, dropping the local episode commit
    (R4 ADD_ADD / M3). Cleanup: abort any rebase, rebase-forward onto origin (a
    patch-identical local commit auto-drops; a content-divergent one conflicts ->
    PAGED, never silently discarding differing local work), verify the local branch
    no longer introduces the episode, then ancestry-proof against origin's sha."""
    if is_rebase_in_progress(repo, git=git):
        if git._git("rebase", "--abort", cwd=repo).returncode != 0:
            _page("delivery-rebase ABORT-FAILED (adopt)", f"{episode_id}: rebase --abort failed", send=send_page)
            return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, "abort failed during adopt")
    git._git("fetch", "origin", "main", cwd=repo)
    rebase = git._git("rebase", "origin/main", cwd=repo)
    if rebase.returncode != 0:
        git._git("rebase", "--abort", cwd=repo)
        _page("delivery-rebase ADD_ADD content-divergent",
              f"{episode_id}: local commit conflicts with origin's same-episode commit {origin_sha}; "
              f"a human must reconcile (not silently dropped).", send=send_page)
        return _result(RebaseOutcome.PAGED, episode_id, prev_sha, origin_sha, "add/add content-divergent")
    try:
        still_local = find_episode_commit(episode_id, repo=repo, git=git)
    except AmbiguousEpisodeError as exc:
        _page("delivery-rebase AMBIGUOUS (adopt)", str(exc), send=send_page)
        return _result(RebaseOutcome.PAGED, episode_id, prev_sha, origin_sha, str(exc))
    if still_local is not None:
        _page("delivery-rebase adopt: local episode not dropped",
              f"{episode_id}: still introduced locally at {still_local} after rebase onto origin.",
              send=send_page)
        return _result(RebaseOutcome.PAGED, episode_id, prev_sha, origin_sha, "local episode not dropped")
    if ancestry_proof(episode_id, origin_sha, repo=repo, git=git):
        delete_state(episode_id, state_dir)
        return _result(RebaseOutcome.ADOPTED_ORIGIN, episode_id, prev_sha, origin_sha,
                       f"adopted origin commit {origin_sha}; local prev_sha dropped")
    return _result(RebaseOutcome.UNVERIFIED, episode_id, prev_sha, origin_sha,
                   "adopted origin; ancestry proof pending (state retained)")


def _deliver_locked(episode_id: str, prev_sha: str, *, repo: Path,
                    state_dir: Path = DEFAULT_STATE_DIR, git=_git_utils, now=_utcnow,
                    send_page=None, reenter_count: int = 0) -> RebaseResult:
    """Forward delivery of the rebase edge (R4). Lock-free core (never re-acquires)."""
    try:
        cls = classify(episode_id, prev_sha, repo=repo, git=git)
    except AmbiguousEpisodeError as exc:
        _page("delivery-rebase AMBIGUOUS (classify)", str(exc), send=send_page)
        return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, str(exc))
    if cls.kind is ClassKind.TYPED_FAILURE:
        return _result(RebaseOutcome.TYPED_FAILURE, episode_id, prev_sha, None, cls.detail)
    if cls.kind is ClassKind.ADD_ADD:
        return _adopt_origin(episode_id, prev_sha, cls.origin_commit_sha, repo=repo,
                             state_dir=state_dir, git=git, now=now, send_page=send_page)
    # ORIGIN_ADVANCE: persist the single durable base, then the bounded rebase-forward loop.
    origin_at_entry = _rev_parse(repo, "origin/main", git=git)
    save_state(episode_id, PHASE_INFLIGHT, prev_sha, origin_sha_at_entry=origin_at_entry,
               reenter_count=reenter_count, state_dir=state_dir, now=now)
    for _attempt in range(1, MAX_REBASE_ATTEMPTS + 1):
        rebase = git._git("rebase", "origin/main", cwd=repo)  # forward, never reset --hard (NEW-1)
        if rebase.returncode != 0:
            if git._git("rebase", "--abort", cwd=repo).returncode != 0:
                _page("delivery-rebase ABORT-FAILED", f"{episode_id}: rebase --abort failed", send=send_page)
                return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, "abort failed")
            return _result(RebaseOutcome.RE_ENTER, episode_id, prev_sha, None,
                           "rebase conflict; aborted; re-enter")
        try:
            new_sha = find_episode_commit(episode_id, repo=repo, git=git)
        except AmbiguousEpisodeError as exc:
            _page("delivery-rebase AMBIGUOUS (post-rebase)", str(exc), send=send_page)
            return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, str(exc))
        if new_sha is None:
            # Patch-identical drop: our episode commit vanished; origin must carry it (H2).
            cls2 = classify(episode_id, prev_sha, repo=repo, git=git)
            if cls2.kind is ClassKind.ADD_ADD:
                return _adopt_origin(episode_id, prev_sha, cls2.origin_commit_sha, repo=repo,
                                     state_dir=state_dir, git=git, now=now, send_page=send_page)
            _page("delivery-rebase episode vanished", f"{episode_id}: dropped by rebase, not on origin",
                  send=send_page)
            return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, "episode vanished post-rebase")
        save_state(episode_id, PHASE_COMPLETED, prev_sha, new_sha=new_sha,
                   origin_sha_at_entry=origin_at_entry, reenter_count=reenter_count,
                   state_dir=state_dir, now=now)
        res = _finish_push(episode_id, prev_sha, new_sha, repo=repo, state_dir=state_dir,
                           git=git, send_page=send_page)
        if res.outcome is not RebaseOutcome.RE_ENTER:
            return res
        # Push rejected non-ff again: re-classify before another rebase (H2).
        try:
            cls3 = classify(episode_id, prev_sha, repo=repo, git=git)
        except AmbiguousEpisodeError as exc:
            _page("delivery-rebase AMBIGUOUS (re-classify)", str(exc), send=send_page)
            return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, str(exc))
        if cls3.kind is ClassKind.ADD_ADD:
            return _adopt_origin(episode_id, prev_sha, cls3.origin_commit_sha, repo=repo,
                                 state_dir=state_dir, git=git, now=now, send_page=send_page)
        if cls3.kind is ClassKind.TYPED_FAILURE:
            return _result(RebaseOutcome.TYPED_FAILURE, episode_id, prev_sha, None, cls3.detail)
        # ORIGIN_ADVANCE again -> loop (rebase forward onto the newly-fetched tip).
    _page("delivery-rebase EXHAUSTED", f"{episode_id}: {MAX_REBASE_ATTEMPTS} rebase attempts exhausted",
          send=send_page)
    return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, "max rebase attempts exhausted")


def _recover_locked(episode_id: str, *, repo: Path, state_dir: Path = DEFAULT_STATE_DIR,
                    git=_git_utils, now=_utcnow, send_page=None) -> RebaseResult:
    """Startup recovery (R6). Lock-free core. Step 1 (fetch+classify) runs FIRST and
    regardless of state-load outcome; then the per-phase discriminator (3a/3b)."""
    try:
        state = load_state(episode_id, state_dir)
    except StateCorruptionError:
        state = None  # Step 1 still runs; episode_id is a param, not read from the file (R8)
    prev_sha = state.get("prev_sha") if state else None
    new_sha = state.get("new_sha") if state else None
    phase = state.get("phase") if state else None
    reenter_count = int(state.get("reenter_count", 0)) if state else 0

    # --- Step 1: re-fetch + re-classify before any discriminator (H1) ---
    try:
        cls = classify(episode_id, prev_sha or "", repo=repo, git=git)
    except AmbiguousEpisodeError as exc:
        _page("delivery-rebase AMBIGUOUS (recover classify)", str(exc), send=send_page)
        return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, str(exc))
    if cls.kind is ClassKind.ADD_ADD:
        # NEW-2: is origin's episode OUR OWN already-pushed commit, or a peer's?
        if phase == PHASE_COMPLETED and new_sha and cls.origin_commit_sha == new_sha:
            # our own push landed before the proof -> complete as PUSHED, not a peer adoption.
            if ancestry_proof(episode_id, new_sha, repo=repo, git=git):
                delete_state(episode_id, state_dir)
                return _result(RebaseOutcome.PUSHED, episode_id, prev_sha, new_sha,
                               "self-push landed pre-proof; verified")
            return _result(RebaseOutcome.UNVERIFIED, episode_id, prev_sha, new_sha, "self-push; proof pending")
        return _adopt_origin(episode_id, prev_sha, cls.origin_commit_sha, repo=repo,
                             state_dir=state_dir, git=git, now=now, send_page=send_page)
    if cls.kind is ClassKind.TYPED_FAILURE:
        return _result(RebaseOutcome.TYPED_FAILURE, episode_id, prev_sha, None, cls.detail)

    if state is None:
        # Not add/add and no recoverable prev_sha (absent or torn state) -> cannot run the
        # discriminator safely (R8). Never guess.
        _page("delivery-rebase UNRECOVERABLE STATE",
              f"{episode_id}: no recoverable prev_sha and not add/add on origin", send=send_page)
        return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, "no recoverable state")

    if reenter_count > MAX_REENTERS:
        _page("delivery-rebase RE-ENTER CAP", f"{episode_id}: reenter_count {reenter_count} > {MAX_REENTERS}",
              send=send_page)
        return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, "re-enter cap exceeded")

    head = _rev_parse(repo, "HEAD", git=git)
    rebase_active = is_rebase_in_progress(repo, git=git)

    # --- Step 3b: phase == REBASE_COMPLETED -> resume-forward (H3) ---
    if phase == PHASE_COMPLETED:
        if rebase_active:
            if git._git("rebase", "--abort", cwd=repo).returncode != 0:
                _page("delivery-rebase ABORT-FAILED (3b)", f"{episode_id}: abort failed", send=send_page)
                return _result(RebaseOutcome.PAGED, episode_id, prev_sha, new_sha, "abort failed (3b)")
        try:
            discovered = find_episode_commit(episode_id, repo=repo, git=git)
        except AmbiguousEpisodeError as exc:
            _page("delivery-rebase AMBIGUOUS (3b)", str(exc), send=send_page)
            return _result(RebaseOutcome.PAGED, episode_id, prev_sha, new_sha, str(exc))
        if discovered != new_sha:
            _page("delivery-rebase 3b MISMATCH",
                  f"{episode_id}: discovered {discovered} != recorded new_sha {new_sha}", send=send_page)
            return _result(RebaseOutcome.PAGED, episode_id, prev_sha, new_sha, "3b scan mismatch")
        return _finish_push(episode_id, prev_sha, new_sha, repo=repo, state_dir=state_dir,
                            git=git, send_page=send_page)

    # --- Step 3a: phase == REBASE_INFLIGHT -> three-way discriminator ---
    if not rebase_active and head == prev_sha:
        return _result(RebaseOutcome.RE_ENTER, episode_id, prev_sha, None, "case (i) pre-rebase; re-enter")
    if rebase_active:
        if git._git("rebase", "--abort", cwd=repo).returncode != 0:
            _page("delivery-rebase ABORT-FAILED (3a-ii)", f"{episode_id}: abort failed", send=send_page)
            return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, "abort failed (case ii)")
        # NEW-1: decide by the scan, not a strict HEAD==prev_sha assertion.
        head_after = _rev_parse(repo, "HEAD", git=git)
        try:
            found = find_episode_commit(episode_id, repo=repo, git=git)
        except AmbiguousEpisodeError as exc:
            _page("delivery-rebase AMBIGUOUS (case ii)", str(exc), send=send_page)
            return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, str(exc))
        if head_after == prev_sha or found is not None:
            return _result(RebaseOutcome.RE_ENTER, episode_id, prev_sha, None, "case (ii) mid-conflict; re-enter")
        _page("delivery-rebase HEAD MOVED", f"{episode_id}: episode unfindable after abort (foreign move)",
              send=send_page)
        return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, "case (ii) foreign HEAD move")
    # HEAD != prev_sha, no rebase dir:
    try:
        discovered = find_episode_commit(episode_id, repo=repo, git=git)
    except AmbiguousEpisodeError as exc:
        _page("delivery-rebase AMBIGUOUS (case iii)", str(exc), send=send_page)
        return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, str(exc))
    if discovered is not None and discovered != prev_sha:
        # case (iii) post-rebase-adopt: the rewrite landed but REBASE_COMPLETED persist was killed.
        save_state(episode_id, PHASE_COMPLETED, prev_sha, new_sha=discovered,
                   origin_sha_at_entry=state.get("origin_sha_at_entry"), reenter_count=reenter_count,
                   state_dir=state_dir, now=now)
        return _finish_push(episode_id, prev_sha, discovered, repo=repo, state_dir=state_dir,
                            git=git, send_page=send_page)
    _page("delivery-rebase UNDIAGNOSED",
          f"{episode_id}: HEAD={head} prev={prev_sha} discovered={discovered} rebase={rebase_active}",
          send=send_page)
    return _result(RebaseOutcome.PAGED, episode_id, prev_sha, None, "undiagnosed recovery state")


# --------------------------------------------------------------------------- #
# Public entry points (R3) -- acquire git_mutation_lock EXACTLY ONCE.
# --------------------------------------------------------------------------- #
def on_nonff_rejection(episode_id: str, prev_sha: str, *, repo: Path,
                       state_dir: Path = DEFAULT_STATE_DIR, git=_git_utils, now=_utcnow) -> RebaseResult:
    with git.git_mutation_lock(label=f"rebase-edge {episode_id}"):
        return _deliver_locked(episode_id, prev_sha, repo=repo, state_dir=state_dir, git=git, now=now)


def recover(episode_id: str, *, repo: Path, state_dir: Path = DEFAULT_STATE_DIR,
            git=_git_utils, now=_utcnow) -> RebaseResult:
    with git.git_mutation_lock(label=f"rebase-recover {episode_id}"):
        return _recover_locked(episode_id, repo=repo, state_dir=state_dir, git=git, now=now)


# --------------------------------------------------------------------------- #
# CLI (standalone driver, not the production caller)
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Delivery-machine REBASE-crash controller (RFC-002).")
    p.add_argument("--repo", default=str(_git_utils.REPO_ROOT))
    p.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("deliver")
    d.add_argument("episode_id")
    d.add_argument("prev_sha")
    r = sub.add_parser("recover")
    r.add_argument("episode_id")
    args = p.parse_args(argv)
    repo, state_dir = Path(args.repo), Path(args.state_dir)
    if args.cmd == "deliver":
        res = on_nonff_rejection(args.episode_id, args.prev_sha, repo=repo, state_dir=state_dir)
    else:
        res = recover(args.episode_id, repo=repo, state_dir=state_dir)
    print(f"[delivery-rebase] {res.outcome.value} episode={res.episode_id} "
          f"effective={res.effective_sha} :: {res.detail}")
    return 0 if res.outcome in (RebaseOutcome.PUSHED, RebaseOutcome.ADOPTED_ORIGIN) else 1


if __name__ == "__main__":
    sys.exit(main())
