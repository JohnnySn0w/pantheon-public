#!/usr/bin/env python3
"""Weekly mirror drift-sweep (T-1994).

ONE recovery-account weekly systemd timer. Per canonical source->mirror pair
(mirror-drift-pairs.yaml) it detects when an outward-facing mirror has fallen
behind its source and auto-files a refresh ticket routed to the pair's
refresh_target; it also ALERTS on the built-but-unpushed case (a refresh bundle
built + delivered for publish but not yet published).

WHY THE DESIGN IS MIRROR-READ-FREE (finalized with orchestrator, T-1994):
recovery's account cannot read 3 of the 4 mirrors (gh unauthenticated, HTTPS no
creds, SSH deploy keys source-scoped) AND the genericized mirrors have DISJOINT
history (published as orphan `git bundle` heads, no shared ancestry with the
source), so `git rev-list mirror..source` is meaningless. Instead drift is
measured against a RECORDED ANCHOR — the SOURCE head from which each mirror was
last published (mirror-published-state.yaml, written by tools/mirror_state.py at
build/publish time). This sweep reads the current SOURCE head (recovery CAN read all
four sources) and compares to the anchor. No mirror is ever read.

Per pair, classification (priority order):
  * gated         — pair is ratify-gated (pantheon-public-mirror); DETECT + report
                    in the digest but NEVER auto-file while gated.
  * coverage_gap  — no anchor recorded and nothing staged; can't evaluate.
                    Reported, never filed, never crashes (the honest pre-seed /
                    dropped-handoff state per the orchestrator contract).
  * drift         — source moved past both the anchor AND any staged build =>
                    a NEW build is needed => file a refresh ticket.
  * built_unpushed— a bundle was built+delivered (staged) but not yet published,
                    and the source hasn't moved past it => ALERT (a live-audience
                    mirror is sitting on stale content), no new build ticket.
  * no_drift      — source head == anchor (fully published).
  * error         — the source head could not be read (network/transient); the
                    pair is reported and skipped, the sweep does not abort.

GENERATOR PAIRS (T-4768 PR5, T-4750 C5 / ORG-091 phase 5): a pair with a
`generator: {tool: build_harness_mirror, mirror: <name>}` block is a release
mirror GENERATED from pantheon-config (source `local_repo`). For those, drift is
a PAYLOAD-TREE change, not a commit count: the sweep imports
tools/build_harness_mirror.py, builds the payload at the source head, and
compares its tree with `published_payload_tree` (and any staged
`payload_tree`) in mirror-published-state.yaml. A commit that does not change
the tree is not drift. A build tree that differs from the published one only by
the manifest's acceptance allowlist counts as published. Extra statuses:
  * leak   — the leak gate failed on the build. ALERT ONLY (recovery W8): never
             auto-filed; the digest names the pattern class + file:line only,
             never the matched string.
  * stale  — a shadow's source blob moved off its pin (a refresh trigger even if
             the tree is unchanged). Auto-filed like drift (W8).
A generator pair's evaluation failing (manifest/rule/coverage error, a missing
leak-gate pattern source, an unreadable head) is a CONFIG error (exit 2).
Precedence when several apply to one pair: leak > stale > drift. The payload
paths (orchestrator T3) are derived from the manifest (payload_paths()), never listed
in the pairs file.

EXIT CODES (recovery W6; the unit sets SuccessExitStatus=10 13 so every other code pages):
  0 nothing actionable, 10 drift / built-but-unpushed, 13 STALE shadow,
  2 config/run error (incl. ANY failure to file, commit or push a drift/stale
    ticket, and every pair errored), 4 LEAK.
  Across pairs the most severe wins, ranked 4 > 2 > 13 > 10 > 0: a code the unit
  treats as failure (4, 2) always beats one it treats as success (13, 10), so a
  drift week never masks a config fault. A ratify-gated pair (detect-only)
  contributes no 10/13. The per-fire digest is sent on every exit path.

Filing is OFF by default (ship-dark). Pass --commit-prod to actually file
tickets. The Telegram digest is sent every run as proof-of-life (silence != a
healthy no-drift week) unless --no-digest.

CADENCE-FIRED REMEDIATION CONFORMANCE (sop/standard/cadence-fired-remediation-
design.md — this weekly timer files a remediation ticket while a trigger holds,
so the four required checks are designed in, mapped here for review legibility):
  1. Effectiveness signal — the recorded anchor advancing (orchestrator's publish leg
     calls mirror_state.record-published, which clears the drift) is the "did the
     last remediation work?" observable. Drift gone => no file.
  2. Bounded attempts — existing_open_refresh_ticket() makes filing idempotent:
     at most ONE outstanding refresh ticket per pair; a still-open ticket
     suppresses re-filing, so a persistent drift never spams (bounded to 1).
  3. Pre-action state-gate (runs BEFORE the file action) — gated pairs =>
     detect-but-never-file; coverage_gap (no anchor) => report-never-file;
     read-error => report-never-file-never-crash. Only a genuine, evaluable
     drift on an ungated pair reaches the default (file) action.
  4. Bounded escalation — the refresh ticket is filed once (per #2); the weekly
     digest is proof-of-life, NOT a per-cycle re-fire of the remediation.

CLI:
    python3 tools/mirror_drift_sweep.py                 # dry-run: evaluate + digest, file nothing
    python3 tools/mirror_drift_sweep.py --commit-prod   # evaluate + file refresh tickets + digest
    python3 tools/mirror_drift_sweep.py --no-digest      # skip the Telegram send
    python3 tools/mirror_drift_sweep.py --json           # machine-readable result dump
    python3 tools/mirror_drift_sweep.py --no-fetch       # skip `git fetch` before the behind-origin check
    python3 tools/mirror_drift_sweep.py --rebase-anchor <slug> [--published-tree <tree>] [--write]
        # generator pair: find the pantheon-config sha whose build reproduces the
        # published tree (newest exact, else newest allowlisted); dry-run unless --write.
        # Exit 0 found, 1 none found (pair stays coverage_gap), 2 config error.
    python3 tools/mirror_drift_sweep.py --cadence-report [--since <sha>]
        # informational ORG-091 falsifier (orchestrator T3): real publishes vs payload-path
        # commits per generator pair. Exit 0, or 2 on a config error.

Tests: python3 -m unittest tools.test_mirror_drift_sweep
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Script/package dual-mode import bootstrap (same shape as ticket_grooming.py):
# `python3 tools/mirror_drift_sweep.py` prepends tools/ to sys.path (so the
# `tools` package isn't importable); inject the repo root in that case.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import mirror_state  # noqa: E402
from tools.mirror_state import MirrorStateError, load_state  # noqa: E402
from tools.mirror_leak_gate import (  # noqa: E402,F401  (T-4821: shared redaction)
    path_hit_paths, redact_leak_line, redact_leak_lines, scrub_paths)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PAIRS_PATH = REPO_ROOT / "mirror-drift-pairs.yaml"
DEFAULT_STATE_PATH = REPO_ROOT / "mirror-published-state.yaml"
DEFAULT_CACHE_DIR = Path(
    os.environ.get(
        "PANTHEON_MIRROR_DRIFT_CACHE",
        str(Path.home() / ".cache" / "pantheon-mirror-drift"),
    )
)
TICKETS_ROOT = REPO_ROOT / "tickets"

REFRESH_LABEL = "mirror-refresh"

# T-3638: a pair actively serving stale content (drift / built-but-unpushed)
# whose last publish is older than this many days is an SLA breach — surfaced in
# a distinct, louder digest section so an editorial refresh stops silently losing
# priority to more-urgent work (the diagnosed systemic mirror-lag root cause).
SLA_STALE_DAYS = 14

# Statuses where the mirror is actively serving stale content (a build is owed).
# `stale` (a shadow off its pin) is owed a refresh; `leak` is an alert (W8).
_ACTIONABLE = ("leak", "stale", "drift", "built_unpushed")

# Statuses that auto-file a refresh ticket (when ungated and --commit-prod).
# `leak` is deliberately absent: alert only, never a ticket (recovery W8).
_FILEABLE = ("stale", "drift")

EXIT_OK = 0
# Drift / stale use codes Python never produces on its own (an uncaught
# exception or an import failure exits 1), so a crash can never read as a
# routine drift/stale week once the unit treats 10 and 13 as success (recovery B1).
EXIT_DRIFT = 10
EXIT_CONFIG = 2
EXIT_STALE = 13
EXIT_LEAK = 4
# Severity rank across pairs (higher wins): failure codes (4, 2) above success
# codes (13, 10) so a drift/stale week never masks a config fault (recovery W6).
_EXIT_RANK = {EXIT_OK: 0, EXIT_DRIFT: 1, EXIT_STALE: 2, EXIT_CONFIG: 3, EXIT_LEAK: 4}

GENERATOR_TOOL = "build_harness_mirror"
GENERATOR_KEYS = {"tool", "mirror"}


def _bhm():
    """The generator module, imported on first use (no shell-out) so a broken
    generator only affects generator pairs. It injects tools/ into sys.path."""
    from tools import build_harness_mirror
    return build_harness_mirror

# Telegram: same env convention as ticket_grooming.py. The live send is
# suppressed when PANTHEON_DISABLE_LIVE_TELEGRAM is set (test-suite guard so a
# full-suite run never leaks a real message).
TELEGRAM_CHAT_ID = os.environ.get("PANTHEON_TELEGRAM_CHAT_ID", "424242424")  # the operator


def _default_telegram_env() -> Path:
    chan = Path.home() / ".claude" / "channels" / "telegram"
    pantheon = chan / "pantheon.env"
    return pantheon if pantheon.exists() else chan / ".env"


TELEGRAM_ENV = Path(
    os.environ.get("PANTHEON_TELEGRAM_ENV", str(_default_telegram_env()))
)

GIT_TIMEOUT = 60  # seconds per git subprocess (ls-remote / fetch / rev-list)


# ---------------------------------------------------------------------------
# Time / staleness helpers (module-level so tests patch one symbol: _now).
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Current UTC time. Module-level so tests patch a single symbol."""
    return datetime.now(timezone.utc)


def _parse_published_at(value: str | None) -> datetime | None:
    """Parse a mirror_state ``published_at`` stamp (mirror_state._now_iso format).

    Returns None for a missing/blank/unparseable value (never raises) so a
    malformed stamp degrades to "age unknown" rather than wedging the sweep.
    """
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _stale_days(published_at: str | None, *, now: datetime | None = None) -> int | None:
    """Whole days since ``published_at`` (>= 0), or None if the stamp is unusable.

    A future stamp (clock skew) clamps to 0 rather than reporting a negative age.
    """
    dt = _parse_published_at(published_at)
    if dt is None:
        return None
    return max(((now or _now()) - dt).days, 0)


# ---------------------------------------------------------------------------
# Result record.
# ---------------------------------------------------------------------------


@dataclass
class PairResult:
    slug: str
    status: str  # leak | stale | gated | coverage_gap | drift | built_unpushed | no_drift | error
    anchor: str | None = None
    current_head: str | None = None
    staged_head: str | None = None
    commits_behind: int | None = None
    refresh_target: str | None = None
    publish_via: str | None = None
    detail: str = ""
    filed_ticket: str | None = None  # set when a refresh ticket was filed
    gate_tickets: list[str] = field(default_factory=list)
    published_at: str | None = None  # mirror_state stamp of the last publish (T-3638)
    stale_days: int | None = None    # whole days since published_at (T-3638)
    gated: bool = False              # ratify-gated pair — excluded from SLA breach (T-3638)
    # Generator pairs (T-4768 PR5).
    generator: str | None = None     # mirror name in build_harness_mirror.MIRRORS
    payload_tree: str | None = None  # tree of build(current_head)
    published_payload_tree: str | None = None
    staged_payload_tree: str | None = None
    payload_commits_behind: int | None = None  # first-parent anchor..head over payload paths
    stale: list[str] = field(default_factory=list)          # STALE lines (no secrets)
    leaks: int = 0                                          # leak-gate finding count
    leak_findings: list[str] = field(default_factory=list)  # REDACTED: class + file:line only
    mismatches: list[str] = field(default_factory=list)     # check() MISMATCH/ALLOWLIST lines
    config_error: bool = False       # evaluation failed for a config reason -> exit 2
    filing_failed: bool = False      # a drift/stale ticket could not be filed -> exit 2 (W6)


# ---------------------------------------------------------------------------
# Config load.
# ---------------------------------------------------------------------------


def load_pairs(path: Path = DEFAULT_PAIRS_PATH) -> list[dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "pairs" not in raw:
        raise ValueError(f"{path}: expected a mapping with a 'pairs' key")
    pairs = raw["pairs"]
    if not isinstance(pairs, list):
        raise ValueError(f"{path}: 'pairs' must be a list")
    for i, pair in enumerate(pairs):
        _validate_pair(f"{path}: pairs[{i}]", pair)
    return pairs


def _validate_pair(where: str, pair: Any) -> None:
    """Static (no git) schema checks. Anything needing the manifest is checked per
    pair at evaluation time, so one bad generator pair cannot stop the others or
    the digest (recovery W6)."""
    if not isinstance(pair, dict) or not pair.get("slug") or not isinstance(pair.get("source"), dict):
        raise ValueError(f"{where}: expected a mapping with 'slug' and a 'source' mapping")
    src = pair["source"]
    if "paths" in src:
        raise ValueError(
            f"{where} ({pair['slug']}): source.paths is not supported; a generator pair's "
            "payload paths are derived from its manifest (payload_paths, T3)"
        )
    gen = pair.get("generator")
    if gen is None:
        return
    if not isinstance(gen, dict) or set(gen) != GENERATOR_KEYS:
        raise ValueError(f"{where} ({pair['slug']}): generator must be "
                         f"{{tool: {GENERATOR_TOOL}, mirror: <name>}}")
    if gen["tool"] != GENERATOR_TOOL:
        raise ValueError(f"{where} ({pair['slug']}): unknown generator tool {gen['tool']!r}")
    mirrors = _bhm().MIRRORS
    if gen["mirror"] not in mirrors:
        raise ValueError(f"{where} ({pair['slug']}): unknown generator mirror "
                         f"{gen['mirror']!r}; known: {sorted(mirrors)}")
    if src.get("local_repo") is None:
        raise ValueError(f"{where} ({pair['slug']}): a generator pair needs source.local_repo "
                         "(the build reads git objects locally)")


# ---------------------------------------------------------------------------
# Git helpers (source reads only — recovery can read all four sources).
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )


def read_source_head(pair: dict[str, Any]) -> str:
    """Return the current SOURCE head sha for a pair.

    ssh_alias source  -> `git ls-remote git@<alias>:<repo> <ref>` (no clone).
    local_repo source -> `git -C <repo> rev-parse <ref>` (pantheon-config itself).

    Raises RuntimeError on any read failure — the caller catches it and marks the
    pair `error` (reported, not fatal).
    """
    src = pair["source"]
    ref = src.get("ref", "HEAD")
    if src.get("local_repo") is not None:
        repo_path = (REPO_ROOT / src["local_repo"]).resolve()
        res = _run_git(["rev-parse", ref], cwd=repo_path)
        if res.returncode != 0:
            raise RuntimeError(f"rev-parse {ref} in {repo_path} failed: {res.stderr.strip()}")
        return res.stdout.strip().lower()
    alias = src["ssh_alias"]
    repo = src["repo"]
    url = f"git@{alias}:{repo}"
    res = _run_git(["ls-remote", url, ref])
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError(f"ls-remote {url} {ref} failed: {res.stderr.strip() or 'empty'}")
    return res.stdout.split()[0].strip().lower()


def _source_cache_url(pair: dict[str, Any]) -> str | None:
    """The clonable URL for the pair's source (None for a local_repo)."""
    src = pair["source"]
    if src.get("local_repo") is not None:
        return None
    return f"git@{src['ssh_alias']}:{src['repo']}"


def count_commits_behind(
    pair: dict[str, Any], anchor: str, current_head: str, cache_dir: Path
) -> int | None:
    """Exact commit count of `anchor..current_head` in the SOURCE.

    Maintains a bare cache clone per source under `cache_dir` (clone if absent,
    else fetch). Returns the count, or None if uncountable — clone/fetch failed,
    or `anchor` is not an ancestor of `current_head` (a history rewrite). None is
    reported as "moved (exact count unavailable)" rather than crashing.
    """
    if anchor == current_head:
        return 0
    src = pair["source"]
    # Local source: count directly in the working repo.
    if src.get("local_repo") is not None:
        repo_path = (REPO_ROOT / src["local_repo"]).resolve()
        return _rev_list_count(repo_path, anchor, current_head)

    url = _source_cache_url(pair)
    cache_dir.mkdir(parents=True, exist_ok=True)
    clone = cache_dir / f"{pair['slug']}.git"
    try:
        if not clone.exists():
            res = _run_git(["clone", "--bare", "--quiet", url, str(clone)])
            if res.returncode != 0:
                return None
        else:
            res = _run_git(["fetch", "--quiet", "origin", "+refs/heads/*:refs/heads/*"], cwd=clone)
            if res.returncode != 0:
                # A stale/broken cache clone shouldn't wedge the sweep.
                return None
    except (subprocess.TimeoutExpired, OSError):
        return None
    return _rev_list_count(clone, anchor, current_head)


def _rev_list_count(repo: Path, anchor: str, current_head: str) -> int | None:
    res = _run_git(["rev-list", "--count", f"{anchor}..{current_head}"], cwd=repo)
    if res.returncode != 0:
        return None  # anchor not reachable (rewrite) or object absent
    try:
        return int(res.stdout.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Per-pair evaluation.
# ---------------------------------------------------------------------------


def evaluate_pair(
    pair: dict[str, Any], state: dict[str, Any], cache_dir: Path
) -> PairResult:
    """Classify one pair. Never raises — a read failure becomes status=error."""
    if pair.get("generator"):
        return evaluate_generated_pair(pair, state)
    slug = pair["slug"]
    refresh_target = pair.get("refresh_target")
    publish_via = pair.get("publish_via")
    gated = bool(pair.get("gated", False))
    gate_tickets = list(pair.get("gate_tickets", []) or [])

    entry = state.get("pairs", {}).get(slug) or {}
    anchor = entry.get("source_head_at_last_publish")
    published_at = entry.get("published_at")
    staged = entry.get("staged_bundle")
    staged_head = staged.get("source_head_at_build") if isinstance(staged, dict) else None

    result = PairResult(
        slug=slug,
        status="error",
        anchor=anchor,
        staged_head=staged_head,
        refresh_target=refresh_target,
        publish_via=publish_via,
        gate_tickets=gate_tickets,
        published_at=published_at,
        stale_days=_stale_days(published_at),
        gated=gated,
    )

    # Read the current source head first — needed for every non-trivial branch.
    try:
        current = read_source_head(pair)
    except (RuntimeError, subprocess.TimeoutExpired, OSError, KeyError) as exc:
        result.status = "error"
        result.detail = f"could not read source head: {exc}"
        return result
    result.current_head = current

    # A gated pair that has never been published (no anchor) is not a coverage-
    # gap — it is correctly awaiting ratification. Report it as gated (tracking
    # suppressed) rather than as a dropped-handoff coverage-gap.
    if gated and anchor is None and staged_head is None:
        result.status = "gated"
        result.detail = (
            f"ratify-gated on {', '.join(gate_tickets)}; seed not yet published, "
            "drift tracking suppressed until ratification"
        )
        return result

    # coverage_gap: no way to measure drift (never crash, never false-file).
    if anchor is None and staged_head is None:
        result.status = "coverage_gap"
        result.detail = (
            "no published anchor recorded (pre-seed, or a bundle handoff omitted "
            "source_head_at_build); cannot evaluate drift"
        )
        return result

    built_unpushed = staged_head is not None and staged_head != anchor

    # DRIFT: source moved past BOTH the anchor and any staged build => new build.
    if current != anchor and current != staged_head:
        result.status = "drift"
        result.commits_behind = (
            count_commits_behind(pair, anchor, current, cache_dir)
            if anchor is not None else None
        )
        n = result.commits_behind
        behind = f"{n} commit(s) behind" if n is not None else "moved past anchor (exact count unavailable)"
        result.detail = f"source is {behind}; a refresh build is needed"
        if built_unpushed:
            result.detail += (
                f" (note: an older bundle @ {staged_head[:12]} is also staged-but-unpublished)"
            )
        # Gating suppresses filing but not detection.
        if gated:
            result.detail += f" — GATED on {', '.join(gate_tickets)}; detect-only, not filing"
        return result

    # BUILT-BUT-UNPUSHED: latest source is already built (staged) but not published.
    if built_unpushed and current == staged_head:
        result.status = "built_unpushed"
        result.detail = (
            f"a refresh bundle built from {staged_head[:12]} was delivered but is not "
            f"yet published (publish leg: {publish_via}); mirror is serving stale content"
        )
        if gated:
            result.detail += f" — GATED on {', '.join(gate_tickets)}; alert-only"
        return result

    # NO DRIFT.
    result.status = "no_drift"
    result.detail = "source head == last-published anchor; mirror is current"
    return result


# ---------------------------------------------------------------------------
# Generator pairs (T-4768 PR5): payload-tree drift via build_harness_mirror.
# ---------------------------------------------------------------------------

# Leak-gate report-line redaction (recovery W8/B2) lives in tools/mirror_leak_gate.py
# since T-4821, shared with build_harness_mirror's CLI; re-exported here unchanged.


def _local_repo(pair: dict[str, Any]) -> Path:
    return (REPO_ROOT / pair["source"]["local_repo"]).resolve()


def payload_paths_of(m: dict[str, Any], mirror: str) -> list[str]:
    bhm = _bhm()
    return bhm.payload_paths(m, bhm.MIRRORS.get(mirror))


def count_payload_commits(repo: Path, anchor: str, head: str, paths: list[str]) -> int | None:
    """First-parent commits in anchor..head touching the payload paths (T3)."""
    if anchor == head:
        return 0
    res = _run_git(["rev-list", "--first-parent", "--count", f"{anchor}..{head}", "--", *paths],
                   cwd=repo)
    if res.returncode != 0:
        return None
    try:
        return int(res.stdout.strip())
    except ValueError:
        return None


def _payload_tree(res) -> str:
    bhm = _bhm()
    return bhm.tree_sha(bhm.listing_of(res.entries, res.object_format), res.object_format)


def _matches_published(res, m: dict[str, Any], t_head: str, t_pub: str) -> tuple[bool, list[str]]:
    """Does build(head) count as the published tree? Exact equality, or (only when
    t_pub is the manifest's acceptance.published_tree, whose entries are a
    self-verifying fixture) a check() pass through the acceptance allowlist.
    Returns (match, MISMATCH/ALLOWLIST report lines)."""
    if t_head == t_pub:
        return True, []
    bhm = _bhm()
    acc = m.get("acceptance") or {}
    if acc.get("published_tree") != t_pub:
        return False, [f"MISMATCH tree {t_head} != published {t_pub}"]
    # Tree verdict only: STALE and LEAK are classified separately (precedence).
    clean = bhm.BuildResult(entries=res.entries, object_format=res.object_format,
                            verdict=res.verdict)
    code, report = bhm.check(clean, m, t_pub)
    if code == bhm.EXIT_CONFIG:
        raise ValueError("; ".join(report[-2:]))
    lines = [ln for ln in report if ln.startswith(("MISMATCH", "ALLOWLIST "))]
    return code == bhm.EXIT_OK, lines


def evaluate_generated_pair(pair: dict[str, Any], state: dict[str, Any]) -> PairResult:
    """Classify a generator pair by payload tree. Never raises.

    Order: leak > error (config) > stale > gated > coverage_gap > drift >
    built_unpushed > no_drift. leak and stale are properties of the head build
    alone, so they need no anchor and come before coverage_gap. A config error
    does not hide a leak (recovery N2): build() skips the gate on errors, so the gate
    is run here over the partial payload.
    """
    slug = pair["slug"]
    gated = bool(pair.get("gated", False))
    gate_tickets = list(pair.get("gate_tickets", []) or [])
    mirror = pair["generator"]["mirror"]
    entry = state.get("pairs", {}).get(slug) or {}
    staged = entry.get("staged_bundle")
    staged = staged if isinstance(staged, dict) else {}
    result = PairResult(
        slug=slug,
        status="error",
        anchor=entry.get("source_head_at_last_publish"),
        staged_head=staged.get("source_head_at_build"),
        refresh_target=pair.get("refresh_target"),
        publish_via=pair.get("publish_via"),
        gate_tickets=gate_tickets,
        published_at=entry.get("published_at"),
        stale_days=_stale_days(entry.get("published_at")),
        gated=gated,
        generator=mirror,
        published_payload_tree=entry.get("published_payload_tree"),
        staged_payload_tree=staged.get("payload_tree"),
    )
    gate_note = f" — GATED on {', '.join(gate_tickets)}; detect-only, not filing" if gated else ""

    try:
        bhm = _bhm()
        config_errors = (*bhm.CONFIG_ERRORS, subprocess.SubprocessError, KeyError)
    except Exception as exc:  # noqa: BLE001  (an unimportable generator is a config error)
        result.config_error = True
        result.detail = f"generator import failed: {exc}"
        return result
    try:
        repo = _local_repo(pair)
        current = read_source_head(pair)
        result.current_head = current
        src, cfg, m = bhm.open_mirror(repo, current, mirror)
        res = bhm.build(src, cfg, m)
        if res.errors and not res.verdict:
            bhm.run_gate(res, src, cfg, m, src)  # N2: scan what did build
    except config_errors as exc:
        result.config_error = True
        result.detail = f"generator evaluation failed: {exc}"
        return result

    if res.leaks:
        # recovery W8/B2: nothing that can carry a matched string or a withheld path
        # leaves this branch: findings redacted, mismatches dropped, rest scrubbed.
        result.status = "leak"
        result.leaks = len(res.leaks)
        result.leak_findings, withheld = redact_leak_lines(res.leaks)
        result.stale = [scrub_paths(s, withheld) for s in res.stale]
        result.mismatches = []
        result.detail = (
            f"LEAK GATE FAILED on build({current[:12]}): {result.leaks} finding(s); "
            "alert only, never auto-filed (W8)"
        )
        if res.errors:
            result.config_error = True
            result.detail += scrub_paths(
                f"; also {len(res.errors)} config error(s): " + "; ".join(res.errors[:3]),
                withheld)
        return result

    if res.errors:
        result.config_error = True
        result.detail = (f"generator config error at {current[:12]} ({len(res.errors)}): "
                         + "; ".join(res.errors[:3]))
        return result

    try:
        t_head = _payload_tree(res)
        result.payload_tree = t_head
        paths = payload_paths_of(m, mirror)
        if result.anchor:
            result.payload_commits_behind = count_payload_commits(repo, result.anchor, current, paths)
        t_pub = result.published_payload_tree
        matches_pub, result.mismatches = (
            _matches_published(res, m, t_head, t_pub) if t_pub else (False, [])
        )
    except config_errors as exc:
        result.config_error = True
        result.detail = f"generator evaluation failed: {exc}"
        return result

    behind = result.payload_commits_behind
    behind_txt = (f"{behind} payload-path commit(s) since anchor" if behind is not None
                  else "payload-path commits since anchor uncountable")
    drifted = bool(t_pub) and not matches_pub and t_head != result.staged_payload_tree

    if res.stale:
        result.status = "stale"
        result.stale = list(res.stale)
        result.detail = (f"{len(res.stale)} STALE shadow(s): a shadow's source moved off its "
                         "pin; port the change or re-pin (reviewed, not ported)")
        if drifted:
            result.detail += f"; payload tree also drifted ({behind_txt})"
        result.detail += gate_note
        return result

    if gated and not result.anchor and not result.staged_head:
        result.status = "gated"
        result.detail = f"ratify-gated on {', '.join(gate_tickets)}; drift tracking suppressed"
        return result

    if not t_pub and not result.staged_payload_tree:
        result.status = "coverage_gap"
        result.detail = ("no published_payload_tree recorded; cannot evaluate payload drift "
                         "(run --rebase-anchor, or record-published --payload-tree)")
        return result

    if drifted or (not t_pub and t_head != result.staged_payload_tree):
        result.status = "drift"
        result.detail = (f"payload tree {t_head[:12]} != published "
                         f"{(t_pub or 'none')[:12]} ({behind_txt}); a refresh build is needed")
        if result.mismatches:
            shown = [ln for ln in result.mismatches if ln.startswith("MISMATCH")][:3]
            result.detail += ": " + "; ".join(shown)
        if result.staged_payload_tree:
            result.detail += (f" (note: an older build, tree "
                              f"{result.staged_payload_tree[:12]}, is staged-but-unpublished)")
        result.detail += gate_note
        return result

    if result.staged_payload_tree == t_head and not matches_pub:
        result.status = "built_unpushed"
        result.detail = (f"a refresh build (tree {t_head[:12]}) was staged but is not yet "
                         f"published (publish leg: {result.publish_via}); mirror is serving "
                         "stale content")
        if gated:
            result.detail += f" — GATED on {', '.join(gate_tickets)}; alert-only"
        return result

    result.status = "no_drift"
    result.detail = (f"build({current[:12]}) tree {t_head[:12]} == published payload tree"
                     + (" (via the acceptance allowlist)" if t_head != t_pub else "")
                     + "; mirror is current")
    return result


# ---------------------------------------------------------------------------
# Idempotent ticket filing.
# ---------------------------------------------------------------------------


def existing_open_refresh_ticket(slug: str, tickets_root: Path = TICKETS_ROOT) -> str | None:
    """Return the id of an OPEN (inbox/active) refresh ticket for `slug`, or None.

    Idempotency guard: a ticket carrying both the `mirror-refresh` label and the
    `pair:<slug>` label already covers this pair — don't file a duplicate. Parsed
    via ticket.py so label handling matches the rest of the system.
    """
    from tools.ticket import TICKET_FILENAME_RE, TicketError, parse_ticket

    pair_label = f"pair:{slug}"
    for state_dir in ("inbox", "active"):
        d = tickets_root / state_dir
        if not d.exists():
            continue
        for path in sorted(d.iterdir()):
            if not (path.is_file() and TICKET_FILENAME_RE.fullmatch(path.name)):
                continue
            try:
                ticket = parse_ticket(path)
            except TicketError:
                continue
            labels = ticket.fields.get("labels") or []
            if isinstance(labels, list) and REFRESH_LABEL in labels and pair_label in labels:
                return str(ticket.fields.get("id", path.stem))
    return None


def file_refresh_ticket(
    pair: dict[str, Any], result: PairResult, *, repo_root: Path = REPO_ROOT
) -> str:
    """Shell out to tools/ticket.py to file a refresh ticket. Returns the new id.

    Routed to the pair's refresh_target (recovery — the builder is the first actor).
    The ticket records the publish leg so the picking agent knows the build hands
    off to that persona for publish. Raises subprocess.CalledProcessError on
    failure (the caller records the pair as filed=None and surfaces it).
    """
    slug = result.slug
    n = result.commits_behind
    behind = f"{n} commit(s) behind" if n is not None else "past its published anchor"
    title = f"Mirror refresh: {slug} source is {behind}"
    src = pair["source"]
    src_desc = src.get("local_repo") or f"{src.get('ssh_alias')}:{src.get('repo')}"
    if result.generator:
        title, ask = _generated_ticket_text(result, src_desc, src.get("ref", "HEAD"))
    else:
        ask = _legacy_ticket_ask(result, slug, behind, src_desc, src.get("ref", "HEAD"))
    return _file_ticket(result, slug, title, ask, repo_root)


def _generated_ticket_text(result: PairResult, src_desc: str, ref: str) -> tuple[str, str]:
    """Title + ask for a generator pair (T-4768 PR5): payload drift or STALE shadows."""
    slug, mirror = result.slug, result.generator
    n = result.payload_commits_behind
    if result.status == "stale":
        title = f"Mirror refresh: {slug} has {len(result.stale)} STALE shadow(s)"
    else:
        count = f"{n} payload-path commit(s) since anchor" if n is not None else "anchor moved"
        title = f"Mirror refresh: {slug} payload tree drifted ({count})"
    lines = [
        f"The weekly drift-sweep found that the generated {slug} payload no longer matches "
        f"what was published ({result.status}).",
        "",
        f"Source: pantheon-config {src_desc} (ref {ref}), generator mirror {mirror}",
        f"Anchor (source_head_at_last_publish): {result.anchor}",
        f"Current source head: {result.current_head}",
        f"Payload tree at head: {result.payload_tree}",
        f"Published payload tree: {result.published_payload_tree}",
        f"Payload-path commits since anchor: {n if n is not None else 'unknown'}",
    ]
    if result.stale:
        lines += ["", "STALE shadows (port the source change into the shadow, or re-pin "
                  "source_blob with no content change = reviewed, not ported):"]
        lines += [f"  {s}" for s in result.stale]
    if result.mismatches:
        lines += ["", "Tree differences vs the published tree:"]
        lines += [f"  {s}" for s in result.mismatches[:20]]
    lines += [
        "",
        "Refresh per sop/standard/refreshing-an-org-039-release-mirror.md, generator-pair "
        "[G] steps, at the CURRENT origin/main (not this ticket's head): get the live mirror "
        f"head from a {result.publish_via}-handed bundle (step 1); step 2a first: "
        f"`tools/build_harness_mirror.py check --mirror {mirror} --source-sha <anchor> "
        "--expect-tree <live mirror tree> --against-repo <mirror clone>` (the anchor must "
        "still build the live tree), then `diff --against <live head> --against-repo "
        "<mirror clone>`; build the publish "
        "commit from `build --write-tree --object-repo <mirror clone>`, record-staged with "
        f"--payload-tree, and hand off to {result.publish_via} for publish "
        "(record-published --payload-tree).",
        "",
        "Auto-filed by tools/mirror_drift_sweep.py (T-1994, generator pairs T-4768).",
    ]
    return title, "\n".join(lines)


def _legacy_ticket_ask(result: PairResult, slug: str, behind: str, src_desc: str,
                       ref: str) -> str:
    return (
        f"The weekly drift-sweep detected that the source for {slug} has moved "
        f"{behind} its last-published anchor. Build a refresh bundle from the "
        f"current source head and hand it to {result.publish_via} for publish "
        f"(record the new anchor via tools/mirror_state.py record-published, "
        f"carrying source_head_at_build in the handoff).\n\n"
        f"Source: {src_desc} (ref {ref})\n"
        f"Last-published anchor: {result.anchor}\n"
        f"Current source head: {result.current_head}\n"
        f"Publish leg: {result.publish_via}\n\n"
        f"Auto-filed by tools/mirror_drift_sweep.py (T-1994)."
    )


def _file_ticket(result: PairResult, slug: str, title: str, ask: str, repo_root: Path) -> str:
    labels = f"{REFRESH_LABEL},pair:{slug},infra,mirror"
    cmd = [
        sys.executable, str(repo_root / "tools" / "ticket.py"), "file",
        "--automated-filer", "mirror_drift_sweep",  # T-5678: ORG-105 gate, non-interactive
        "--requester", "recovery",
        "--target", result.refresh_target or "role:system_orchestrator",
        "--title", title,
        "--labels", labels,
        "--project-id", "null",
        "--nearest-intent-ref", f"maintenance:mirrors/{slug}",
        "--ask", ask,
        "--serves", f"the {slug} outward-facing mirror does not silently rot behind its source",
    ]
    res = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise subprocess.CalledProcessError(
            res.returncode, cmd, output=res.stdout, stderr=res.stderr
        )
    # ticket.py prints the new id; extract a T-NNNN token from its output.
    m = re.search(r"\bT-\d{4,}\b", res.stdout + res.stderr)  # T-3215
    return m.group(0) if m else "(filed; id not parsed)"


# ---------------------------------------------------------------------------
# Digest.
# ---------------------------------------------------------------------------

_STATUS_ICON = {
    "leak": "🚨 LEAK",
    "stale": "🟡 STALE-SHADOW",
    "drift": "🔴 DRIFT",
    "built_unpushed": "🟠 BUILT-BUT-UNPUSHED",
    "coverage_gap": "⚪ COVERAGE-GAP",
    "error": "⚠️ READ-ERROR",
    "gated": "🔒 GATED",
    "no_drift": "🟢 current",
}


def sla_breached(result: PairResult, threshold_days: int = SLA_STALE_DAYS) -> bool:
    """True when a pair is actively serving stale content past the SLA threshold.

    Only actionable statuses (drift / built_unpushed) count — a ``no_drift`` pair
    published long ago is current, not overdue; coverage_gap / error are not
    "owed a build" in a way an SLA escalation should shout about. A ratify-gated
    pair is deliberately suppressed (it surfaces as GATED every digest), so it is
    excluded too. An unknown age (no parseable published_at) is never a breach.
    """
    return (
        result.status in _ACTIONABLE
        and not result.gated
        and result.stale_days is not None
        and result.stale_days >= threshold_days
    )


def build_digest(
    results: list[PairResult], *, committed: bool, sla_threshold: int = SLA_STALE_DAYS,
    warnings: list[str] | None = None,
) -> str:
    """Human-readable digest. Always sent (proof-of-life)."""
    mode = "FILING" if committed else "dry-run (no tickets filed)"
    header = f"Mirror drift-sweep — {mode}"
    lines = [header, ""]
    if warnings:
        lines += [f"⚠️ {w}" for w in warnings] + [""]

    # recovery W8: a leak is an ALERT (never a ticket). Findings are already redacted
    # to pattern class + file:line (redact_leak_line); no matched string here.
    leaking = [r for r in results if r.status == "leak"]
    if leaking:
        lines.append("🚨 LEAK ALERT (alert only, not filed):")
        for r in leaking:
            lines.append(f"  {r.slug}: {r.leaks} finding(s)")
            lines += [f"    {f}" for f in r.leak_findings[:20]]
            if len(r.leak_findings) > 20:
                lines.append(f"    ... {len(r.leak_findings) - 20} more")
        lines.append("")

    # T-3638: SLA-breach section — the loud escalation, most-stale first, above
    # the routine Actionable list (breached pairs also appear there, complete).
    breached = sorted(
        (r for r in results if sla_breached(r, sla_threshold)),
        key=lambda r: r.stale_days or 0,
        reverse=True,
    )
    if breached:
        lines.append(f"⏰ SLA BREACH — serving stale content > {sla_threshold}d:")
        for r in breached:
            icon = _STATUS_ICON.get(r.status, r.status)
            filed = f" -> filed {r.filed_ticket}" if r.filed_ticket else ""
            lines.append(
                f"  {icon}  {r.slug}: {r.stale_days}d since last publish — {r.detail}{filed}"
            )
        lines.append("")

    actionable = [r for r in results if r.status in _ACTIONABLE]

    if actionable:
        lines.append("Actionable:")
        for r in actionable:
            icon = _STATUS_ICON.get(r.status, r.status)
            filed = f" -> filed {r.filed_ticket}" if r.filed_ticket else (
                # A gated pair is never filed (run_sweep), so never "would file" either.
                " (would file)" if r.status in _FILEABLE and not committed and not r.gated else ""
            )
            lines.append(f"  {icon}  {r.slug}: {r.detail}{filed}")
            lines += [f"      {s}" for s in r.stale[:10]]
        lines.append("")

    broken = [r for r in results if r.config_error or r.filing_failed]
    if broken:
        lines.append("Config / filing errors (exit 2):")
        for r in broken:
            lines.append(f"  ⚠️ {r.slug}: {r.detail}")
        lines.append("")

    lines.append("All pairs:")
    for r in results:
        icon = _STATUS_ICON.get(r.status, r.status)
        # N3: a read error shows its cause. KNOWN GAP (recovery N3, not escalated): a
        # single legacy READ-ERROR pair still contributes exit 0, so only this digest
        # line reports it; only "every pair errored" pages (exit 2).
        why = f": {r.detail}" if r.status == "error" and not r.config_error and r.detail else ""
        lines.append(f"  {icon}  {r.slug}{why}")
    lines.append("")
    lines.append("Auto-sent by tools/mirror_drift_sweep.py (T-1994, Recovery unattended, weekly).")
    return "\n".join(lines)


def _send_telegram(text: str) -> bool:
    """Send the digest. No-token or the live-disable guard => treated as success
    (parity with ticket_grooming; a missing token must not fail the sweep)."""
    if os.environ.get("PANTHEON_DISABLE_LIVE_TELEGRAM"):
        print("[telegram suppressed: PANTHEON_DISABLE_LIVE_TELEGRAM]", file=sys.stderr)
        return True
    try:
        from tools.watch_digest import read_telegram_token
    except Exception:  # noqa: BLE001
        read_telegram_token = None  # type: ignore
    try:
        token = read_telegram_token(TELEGRAM_ENV) if read_telegram_token else None
    except Exception as exc:  # noqa: BLE001  (recovery B1: surfaced by main as exit 2)
        raise RuntimeError(f"telegram token read failed ({type(exc).__name__}) "
                           f"at {TELEGRAM_ENV}") from exc
    if not token:
        print(f"warning: no Telegram token at {TELEGRAM_ENV}; skipping send", file=sys.stderr)
        return True
    import urllib.parse
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:  # noqa: BLE001
        print(f"telegram send failed: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Behind-origin guard (orchestrator T10 conditions): local-source pairs are evaluated at
# the LOCAL checkout's ref, so a checkout behind origin silently under-reports.
# Warn in the digest; never fail the sweep over it.
# ---------------------------------------------------------------------------


def behind_origin_warnings(pairs: list[dict[str, Any]], *, fetch: bool = True) -> list[str]:
    warnings: list[str] = []
    seen: set[Path] = set()
    for pair in pairs:
        src = pair.get("source") or {}
        if src.get("local_repo") is None:
            continue
        repo = _local_repo(pair)
        if repo in seen:
            continue
        seen.add(repo)
        warnings += _behind_origin(repo, src.get("ref", "HEAD"), fetch=fetch)
    return warnings


def _behind_origin(repo: Path, ref: str, *, fetch: bool) -> list[str]:
    out: list[str] = []
    try:
        if fetch:
            res = _run_git(["fetch", "--quiet", "origin"], cwd=repo)
            if res.returncode != 0:
                out.append(f"{repo}: `git fetch origin` failed "
                           f"({res.stderr.strip()[:200] or 'no detail'}); compared against "
                           "the last-fetched origin refs")
        up = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                       f"{ref}@{{upstream}}"], cwd=repo)
        upstream = up.stdout.strip() if up.returncode == 0 and up.stdout.strip() else "origin/main"
        res = _run_git(["rev-list", "--count", f"{ref}..{upstream}"], cwd=repo)
    except (subprocess.SubprocessError, OSError) as exc:
        return out + [f"{repo}: behind-origin check failed ({exc})"]
    if res.returncode != 0:
        return out + [f"{repo}: could not compare {ref} with {upstream} "
                      f"({res.stderr.strip()[:200] or 'no detail'})"]
    try:
        n = int(res.stdout.strip())
    except ValueError:
        return out + [f"{repo}: could not count {ref}..{upstream}"]
    if n > 0:
        out.append(f"{repo} checkout is {n} commit(s) behind {upstream}: local-source pairs "
                   f"were evaluated at the local {ref} and may UNDER-REPORT drift")
    return out


# ---------------------------------------------------------------------------
# Sweep + exit codes.
# ---------------------------------------------------------------------------


def _file_if_needed(pair: dict[str, Any], result: PairResult, tickets_root: Path,
                    commit_prod: bool) -> None:
    """Idempotent filing for a fileable, ungated pair. Any failure to prove
    idempotency or to file (ticket.py also commits + pushes) under --commit-prod
    marks the pair filing_failed -> exit 2 (recovery W6), never 1 or 3."""
    try:
        existing = existing_open_refresh_ticket(result.slug, tickets_root)
    except Exception as exc:  # noqa: BLE001  (unreadable tickets: cannot prove idempotency)
        result.detail += f" (open-ticket check FAILED: {exc})"
        result.filing_failed = commit_prod
        return
    if existing:
        result.detail += f" (open refresh ticket {existing} already covers this — not re-filing)"
        result.filed_ticket = existing
        return
    if not commit_prod:
        return
    try:
        result.filed_ticket = file_refresh_ticket(pair, result)
    except Exception as exc:  # noqa: BLE001  (N1: one pair's filing bug must not drop
        # the other pairs' results, e.g. a co-occurring LEAK on another pair)
        tail = str(getattr(exc, "stderr", None) or "").strip()[-300:]
        result.filing_failed = True
        result.detail += f" (FILING FAILED: {exc}{'; ' + tail if tail else ''})"


def run_sweep(
    *,
    pairs_path: Path = DEFAULT_PAIRS_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    tickets_root: Path = TICKETS_ROOT,
    commit_prod: bool = False,
    fetch: bool = False,
    warnings: list[str] | None = None,
) -> list[PairResult]:
    """Evaluate every pair; file refresh tickets when commit_prod and drift/stale found.

    Returns the per-pair results. Filing is idempotent (skips a pair that already
    has an open refresh ticket); gated pairs and leaks are never filed. When a
    `warnings` list is passed, the behind-origin check runs and appends to it.
    """
    pairs = load_pairs(pairs_path)
    state = load_state(state_path)
    if warnings is not None:
        try:  # advisory only: it must never cost the pairs' results (e.g. a LEAK)
            warnings.extend(behind_origin_warnings(pairs, fetch=fetch))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"behind-origin check crashed: {exc!r}")
    results: list[PairResult] = []
    for pair in pairs:
        try:
            result = evaluate_pair(pair, state, cache_dir)
        except Exception as exc:  # noqa: BLE001  (one crashing pair must not stop the digest)
            traceback.print_exc(file=sys.stderr)
            result = PairResult(slug=pair["slug"], status="error", config_error=True,
                                gated=bool(pair.get("gated", False)),
                                detail=f"evaluation crashed: {exc!r}")
        if result.status in _FILEABLE and not pair.get("gated", False):
            _file_if_needed(pair, result, tickets_root, commit_prod)
        results.append(result)
    return results


def pair_exit_code(r: PairResult) -> int:
    """One pair's contribution to the exit code (see the module docstring)."""
    if r.status == "leak":
        return EXIT_LEAK  # gated or not, config error or not: a leak always alerts (N2)
    if r.config_error or r.filing_failed:
        return EXIT_CONFIG
    if r.gated:
        return EXIT_OK    # detect-only: owned elsewhere (T-1327), never a 10/13
    if r.status == "stale":
        return EXIT_STALE
    if r.status in ("drift", "built_unpushed"):
        return EXIT_DRIFT
    return EXIT_OK


def sweep_exit_code(results: list[PairResult]) -> int:
    """Most severe pair code, ranked 4 > 2 > 13 > 10 > 0; every pair errored -> 2."""
    if results and all(r.status == "error" for r in results):
        return EXIT_CONFIG  # recovery W6: was 1 before T-4768
    code = EXIT_OK
    for r in results:
        c = pair_exit_code(r)
        if _EXIT_RANK[c] > _EXIT_RANK[code]:
            code = c
    return code


# ---------------------------------------------------------------------------
# Anchor rebase (generator pairs): the pantheon-config sha whose build is the
# published payload tree.
# ---------------------------------------------------------------------------


@dataclass
class AnchorMatch:
    sha: str
    match: str  # exact | allowlisted
    tree: str   # the tree build(sha) produced


def _first_parent_payload_commits(repo: Path, rev_range: str, paths: list[str]) -> list[str]:
    res = _run_git(["rev-list", "--first-parent", rev_range, "--", *paths], cwd=repo)
    if res.returncode != 0:
        raise RuntimeError(f"rev-list {rev_range} failed: {res.stderr.strip()}")
    return res.stdout.split()


def find_anchor(repo: Path, head: str, mirror: str, t_pub: str) -> tuple[AnchorMatch | None, str]:
    """Search the first-parent payload-path commits of `head`, newest first, for the
    newest whose build is exactly `t_pub`, else the newest that matches it through
    the acceptance allowlist. Stops at the first unbuildable commit (the
    pre-generator / pre-gate floor). Never guesses: (None, reason) when nothing
    matches. Raises a CONFIG error if the manifest at `head` cannot be read."""
    bhm = _bhm()
    _src, _cfg, m = bhm.open_mirror(repo, head, mirror)
    candidates = _first_parent_payload_commits(repo, head, payload_paths_of(m, mirror))
    allowlisted: AnchorMatch | None = None
    searched, floor = 0, ""
    for c in candidates:
        try:
            src, cfg, mc = bhm.open_mirror(repo, c, mirror)
            res = bhm.build(src, cfg, mc)
        except bhm.CONFIG_ERRORS as exc:
            floor = f"{c[:12]} is unbuildable ({exc})"
            break
        if res.errors:
            floor = f"{c[:12]} is unbuildable ({res.errors[0]})"
            break
        searched += 1
        tree = _payload_tree(res)
        if tree == t_pub:
            return AnchorMatch(c, "exact", tree), f"exact match after {searched} build(s)"
        if allowlisted is None:
            try:
                ok, _ = _matches_published(res, mc, tree, t_pub)
            except ValueError:
                ok = False
            if ok:
                allowlisted = AnchorMatch(c, "allowlisted", tree)
    stop = f"; stopped: {floor}, older commits not searched" if floor else ""
    if allowlisted:
        return allowlisted, f"no exact match in {searched} build(s){stop}; newest allowlisted"
    return None, f"no build of {searched} candidate(s) matches {t_pub}{stop}"


def _generator_pair(pairs_path: Path, slug: str) -> dict[str, Any]:
    for pair in load_pairs(pairs_path):
        if pair["slug"] == slug:
            if not pair.get("generator"):
                raise ValueError(f"{slug} is not a generator pair")
            return pair
    raise ValueError(f"no pair {slug!r} in {pairs_path}")


def rebase_anchor_cli(args: argparse.Namespace) -> int:
    state_path = Path(args.state_path)
    try:
        pair = _generator_pair(Path(args.pairs_path), args.rebase_anchor)
        state = load_state(state_path)
        entry = state.get("pairs", {}).get(pair["slug"]) or {}
        t_pub = args.published_tree or entry.get("published_payload_tree")
        if not t_pub:
            raise ValueError("no published tree: pass --published-tree <tree> "
                             "(the published mirror head's ^{tree})")
        t_pub = mirror_state._validate_sha(t_pub)
        repo = _local_repo(pair)
        head = read_source_head(pair)
        found, why = find_anchor(repo, head, pair["generator"]["mirror"], t_pub)
    except (MirrorStateError, RuntimeError, subprocess.SubprocessError, *_bhm().CONFIG_ERRORS,
            yaml.YAMLError) as exc:
        print(f"error: rebase-anchor failed: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if found is None:
        print(f"{pair['slug']}: NO ANCHOR — {why}; the pair stays coverage_gap")
        return 1
    print(f"{pair['slug']}: anchor {found.sha} ({found.match}; build tree {found.tree}, "
          f"published tree {t_pub}) — {why}")
    if not args.write:
        print("dry-run: state not written (pass --write)")
        return EXIT_OK
    try:
        mirror_state.rebase_anchor(state, pair["slug"], found.sha, t_pub, match=found.match)
        mirror_state.save_state(state, state_path)
    except (MirrorStateError, OSError) as exc:
        print(f"error: could not write {state_path}: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"wrote {state_path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Cadence report (orchestrator T3): the ORG-091 falsifier. Informational only.
# ---------------------------------------------------------------------------


@dataclass
class CadenceReport:
    slug: str
    since: str
    head: str
    paths: list[str]
    harness_commits: int = 0          # H: first-parent payload-path commits in since..head
    payload_changing: list[str] = field(default_factory=list)  # C: tree differs from parent
    unbuildable: int = 0              # U: commit or its first parent cannot be built
    publishes: int = 0                # R: published_mirror_head changed in the state file
    anchor_only: int = 0              # A: anchor moved, mirror head did not
    lag_days: int | None = None       # oldest unpublished payload change, None if current
    verdicts: list[str] = field(default_factory=list)

    def line(self) -> str:
        lag = "-" if self.lag_days is None else f"{self.lag_days}d"
        return (f"{self.slug}: H={self.harness_commits} C={len(self.payload_changing)} "
                f"U={self.unbuildable} R={self.publishes} A={self.anchor_only} lag={lag} "
                f"(since {self.since[:12]}) -> {', '.join(self.verdicts)}")


class _TreeMemo:
    """tree(build(sha)) memoised for one run (risk: two builds per commit)."""

    def __init__(self, repo: Path, mirror: str):
        self.repo, self.mirror, self._memo = repo, mirror, {}

    def get(self, sha: str):
        """(tree, BuildResult, manifest) or None when unbuildable."""
        if sha not in self._memo:
            bhm = _bhm()
            try:
                src, cfg, m = bhm.open_mirror(self.repo, sha, self.mirror)
                res = bhm.build(src, cfg, m)
                self._memo[sha] = None if res.errors else (_payload_tree(res), res, m)
            except bhm.CONFIG_ERRORS:
                self._memo[sha] = None
        return self._memo[sha]


def _git_out(repo: Path, *args: str) -> str | None:
    res = _run_git(list(args), cwd=repo)
    return res.stdout.strip() if res.returncode == 0 else None


def _state_at(repo: Path, rev: str, state_rel: str) -> dict[str, Any]:
    text = _git_out(repo, "show", f"{rev}:{state_rel}")
    if not text:
        return {}
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return (raw or {}).get("pairs") or {} if isinstance(raw, dict) else {}


def cadence_for_pair(pair: dict[str, Any], state: dict[str, Any], state_path: Path, *,
                     since: str | None = None, sla_days: int = SLA_STALE_DAYS,
                     now: datetime | None = None) -> CadenceReport:
    """H / C / U / R / A / lag for one generator pair (C5-PR5-PLAN 'Cadence report').
    Raises a CONFIG error (manifest at head unreadable, bad --since)."""
    bhm = _bhm()
    slug, mirror = pair["slug"], pair["generator"]["mirror"]
    repo = _local_repo(pair)
    head = read_source_head(pair)
    _src, _cfg, m = bhm.open_mirror(repo, head, mirror)
    paths = payload_paths_of(m, mirror)
    if since is None:  # the import commit: the first first-parent rev touching source_root
        revs = _first_parent_payload_commits(repo, head, [m["source_root"].rstrip("/")])
        if not revs:
            raise ValueError(f"{slug}: no commit touches {m['source_root']}")
        since = revs[-1]
    else:
        resolved = _git_out(repo, "rev-parse", "--verify", f"{since}^{{commit}}")
        if not resolved:
            raise ValueError(f"--since {since!r} is not a commit")
        since = resolved
    rep = CadenceReport(slug=slug, since=since, head=head, paths=paths)
    h = _first_parent_payload_commits(repo, f"{since}..{head}", paths)
    rep.harness_commits = len(h)
    memo = _TreeMemo(repo, mirror)
    for c in h:
        parent = _git_out(repo, "rev-parse", "--verify", f"{c}^1")
        cur, prev = memo.get(c), (memo.get(parent) if parent else None)
        if cur is None or prev is None:
            rep.unbuildable += 1
        elif cur[0] != prev[0]:
            rep.payload_changing.append(c)

    # R / A from the state file's own history in the window.
    try:
        state_rel = state_path.resolve().relative_to(repo).as_posix()
    except ValueError:
        state_rel = None
    if state_rel:
        for c in _first_parent_payload_commits(repo, f"{since}..{head}", [state_rel]):
            new = _state_at(repo, c, state_rel).get(slug) or {}
            old = _state_at(repo, f"{c}^1", state_rel).get(slug) or {}
            if new.get("published_mirror_head") and \
                    new.get("published_mirror_head") != old.get("published_mirror_head"):
                rep.publishes += 1
            elif new.get("source_head_at_last_publish") != old.get("source_head_at_last_publish"):
                rep.anchor_only += 1

    # Lag: only while the head build is NOT the published tree.
    entry = state.get("pairs", {}).get(slug) or {}
    t_pub, anchor = entry.get("published_payload_tree"), entry.get("source_head_at_last_publish")
    head_build = memo.get(head)
    current = False
    if head_build and t_pub:
        try:
            current, _ = _matches_published(head_build[1], head_build[2], head_build[0], t_pub)
        except ValueError:
            current = False
    if not current:
        pending = list(rep.payload_changing)
        if anchor and _git_out(repo, "merge-base", "--is-ancestor", anchor, head) is not None:
            after = set(_first_parent_payload_commits(repo, f"{anchor}..{head}", paths))
            pending = [c for c in pending if c in after]
        if pending:
            ts = _git_out(repo, "show", "-s", "--format=%ct", pending[-1])  # oldest (newest-first list)
            if ts:
                when = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                rep.lag_days = max(((now or _now()) - when).days, 0)
    if rep.publishes > len(rep.payload_changing):
        rep.verdicts.append(f"over-publish (R={rep.publishes} > C={len(rep.payload_changing)})")
    if rep.lag_days is not None and rep.lag_days > sla_days:
        rep.verdicts.append(f"under-publish (lag {rep.lag_days}d > {sla_days}d)")
    if not rep.verdicts:
        rep.verdicts.append("healthy")
    return rep


def cadence_report_cli(args: argparse.Namespace) -> int:
    try:
        pairs = [p for p in load_pairs(Path(args.pairs_path)) if p.get("generator")]
        state = load_state(Path(args.state_path))
    except (ValueError, OSError, yaml.YAMLError, MirrorStateError) as exc:
        print(f"error: cadence report failed: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    code, reports = EXIT_OK, []
    for pair in pairs:
        try:
            reports.append(cadence_for_pair(pair, state, Path(args.state_path), since=args.since,
                                            sla_days=args.sla_stale_days))
        except (RuntimeError, subprocess.SubprocessError, *_bhm().CONFIG_ERRORS) as exc:
            print(f"error: {pair['slug']}: {exc}", file=sys.stderr)
            code = EXIT_CONFIG
    if args.json:
        print(json.dumps([asdict(r) for r in reports], indent=2, sort_keys=True))
    else:
        print("Mirror cadence report (ORG-091 falsifier, orchestrator T3; informational)")
        for r in reports:
            print(f"  {r.line()}")
        if not pairs:
            print("  (no generator pairs)")
    return code


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point. ANY uncaught failure returns 2 (recovery B1): Python's own exit 1
    would otherwise be indistinguishable from a crash only by convention, and the
    unit must page on it. SystemExit (argparse) and KeyboardInterrupt pass through."""
    try:
        return _main(argv)
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return EXIT_CONFIG


def _main(argv: list[str] | None) -> int:
    p = argparse.ArgumentParser(description="Weekly mirror drift-sweep (T-1994).")
    p.add_argument("--pairs-path", default=str(DEFAULT_PAIRS_PATH))
    p.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    p.add_argument("--tickets-root", default=str(TICKETS_ROOT))
    p.add_argument(
        "--commit-prod", action="store_true",
        help="actually file refresh tickets (default: dry-run, evaluate + digest only)",
    )
    p.add_argument("--no-digest", action="store_true", help="skip the Telegram digest send")
    p.add_argument("--json", action="store_true", help="print machine-readable results to stdout")
    p.add_argument("--no-fetch", action="store_true",
                   help="skip `git fetch origin` before the behind-origin warning check")
    p.add_argument(
        "--sla-stale-days", type=int, default=SLA_STALE_DAYS,
        help=(
            "days since last publish that marks an actionable pair an SLA breach, "
            f"escalated into a distinct digest section (default {SLA_STALE_DAYS}); "
            "also the cadence report's lag SLA"
        ),
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--rebase-anchor", metavar="SLUG",
                      help="generator pair: find the anchor sha whose build is the published tree")
    mode.add_argument("--cadence-report", action="store_true",
                      help="print the ORG-091 cadence falsifier per generator pair (orchestrator T3)")
    p.add_argument("--published-tree", help="--rebase-anchor: the published payload tree "
                   "(default: the state's published_payload_tree)")
    p.add_argument("--write", action="store_true", help="--rebase-anchor: write the state file")
    p.add_argument("--since", help="--cadence-report: window start (default: the import commit)")
    args = p.parse_args(argv)
    if (args.published_tree or args.write) and not args.rebase_anchor:
        p.error("--published-tree / --write need --rebase-anchor")
    if args.since and not args.cadence_report:
        p.error("--since needs --cadence-report")

    if args.rebase_anchor:
        return rebase_anchor_cli(args)
    if args.cadence_report:
        return cadence_report_cli(args)

    warnings: list[str] = []
    results: list[PairResult] = []
    failed: int | None = None
    try:
        results = run_sweep(
            pairs_path=Path(args.pairs_path),
            state_path=Path(args.state_path),
            cache_dir=Path(args.cache_dir),
            tickets_root=Path(args.tickets_root),
            commit_prod=args.commit_prod,
            fetch=not args.no_fetch,
            warnings=warnings,
        )
    except (ValueError, OSError, yaml.YAMLError, MirrorStateError) as exc:
        print(f"error: sweep failed to run: {exc}", file=sys.stderr)
        warnings.append(f"SWEEP FAILED TO RUN: {exc}")
        failed = EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001  (W6: the digest still sends)
        traceback.print_exc(file=sys.stderr)
        warnings.append(f"SWEEP CRASHED: {exc!r}")
        failed = EXIT_CONFIG

    # recovery W6: the per-fire digest is unconditional (every exit path). A failure
    # to build, print or send it (incl. the token read) is itself exit 2 (recovery B1).
    try:
        digest = build_digest(results, committed=args.commit_prod,
                              sla_threshold=args.sla_stale_days, warnings=warnings)
        if args.json:
            print(json.dumps([asdict(r) for r in results], indent=2, sort_keys=True))
        else:
            print(digest)
        if not args.no_digest:
            _send_telegram(digest)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        if not args.no_digest:
            try:  # best-effort proof-of-life; the type only (no finding text)
                _send_telegram(f"Mirror drift-sweep — DIGEST FAILED ({type(exc).__name__}); "
                               "exit 2, see the unit journal")
            except Exception:  # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
        return EXIT_CONFIG

    # Exit table in the module docstring. The unit sets SuccessExitStatus=10 13, so
    # 1 (a Python crash), 2 (config / run / filing / digest failure) and 4 (leak)
    # fire OnFailure -> recovery.
    return failed if failed is not None else sweep_exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
