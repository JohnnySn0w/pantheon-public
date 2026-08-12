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

Tests: python3 -m unittest tools.test_mirror_drift_sweep
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Script/package dual-mode import bootstrap (same shape as ticket_grooming.py):
# `python3 tools/mirror_drift_sweep.py` prepends tools/ to sys.path (so the
# `tools` package isn't importable); inject the repo root in that case.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.mirror_state import load_state  # noqa: E402

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

# Telegram: same env convention as ticket_grooming.py. The live send is
# suppressed when PANTHEON_DISABLE_LIVE_TELEGRAM is set (test-suite guard so a
# full-suite run never leaks a real message).
TELEGRAM_CHAT_ID = os.environ.get("PANTHEON_TELEGRAM_CHAT_ID", "456474197")  # the operator


def _default_telegram_env() -> Path:
    chan = Path.home() / ".claude" / "channels" / "telegram"
    pantheon = chan / "pantheon.env"
    return pantheon if pantheon.exists() else chan / ".env"


TELEGRAM_ENV = Path(
    os.environ.get("PANTHEON_TELEGRAM_ENV", str(_default_telegram_env()))
)

GIT_TIMEOUT = 60  # seconds per git subprocess (ls-remote / fetch / rev-list)


# ---------------------------------------------------------------------------
# Result record.
# ---------------------------------------------------------------------------


@dataclass
class PairResult:
    slug: str
    status: str  # gated | coverage_gap | drift | built_unpushed | no_drift | error
    anchor: str | None = None
    current_head: str | None = None
    staged_head: str | None = None
    commits_behind: int | None = None
    refresh_target: str | None = None
    publish_via: str | None = None
    detail: str = ""
    filed_ticket: str | None = None  # set when a refresh ticket was filed
    gate_tickets: list[str] = field(default_factory=list)


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
    return pairs


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
    slug = pair["slug"]
    refresh_target = pair.get("refresh_target")
    publish_via = pair.get("publish_via")
    gated = bool(pair.get("gated", False))
    gate_tickets = list(pair.get("gate_tickets", []) or [])

    entry = state.get("pairs", {}).get(slug) or {}
    anchor = entry.get("source_head_at_last_publish")
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
    ask = (
        f"The weekly drift-sweep detected that the source for {slug} has moved "
        f"{behind} its last-published anchor. Build a refresh bundle from the "
        f"current source head and hand it to {result.publish_via} for publish "
        f"(record the new anchor via tools/mirror_state.py record-published, "
        f"carrying source_head_at_build in the handoff).\n\n"
        f"Source: {src_desc} (ref {src.get('ref', 'HEAD')})\n"
        f"Last-published anchor: {result.anchor}\n"
        f"Current source head: {result.current_head}\n"
        f"Publish leg: {result.publish_via}\n\n"
        f"Auto-filed by tools/mirror_drift_sweep.py (T-1994)."
    )
    labels = f"{REFRESH_LABEL},pair:{slug},infra,mirror"
    cmd = [
        sys.executable, str(repo_root / "tools" / "ticket.py"), "file",
        "--requester", "recovery",
        "--target", result.refresh_target or "recovery",
        "--title", title,
        "--labels", labels,
        "--ask", ask,
        "--serves", f"the {slug} outward-facing mirror does not silently rot behind its source",
    ]
    res = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise subprocess.CalledProcessError(
            res.returncode, cmd, output=res.stdout, stderr=res.stderr
        )
    # ticket.py prints the new id; extract a T-NNNN token from its output.
    import re
    m = re.search(r"\bT-\d{4}\b", res.stdout + res.stderr)
    return m.group(0) if m else "(filed; id not parsed)"


# ---------------------------------------------------------------------------
# Digest.
# ---------------------------------------------------------------------------

_STATUS_ICON = {
    "drift": "🔴 DRIFT",
    "built_unpushed": "🟠 BUILT-BUT-UNPUSHED",
    "coverage_gap": "⚪ COVERAGE-GAP",
    "error": "⚠️ READ-ERROR",
    "gated": "🔒 GATED",
    "no_drift": "🟢 current",
}


def build_digest(results: list[PairResult], *, committed: bool) -> str:
    """Human-readable digest. Always sent (proof-of-life)."""
    mode = "FILING" if committed else "dry-run (no tickets filed)"
    header = f"Mirror drift-sweep — {mode}"
    lines = [header, ""]

    actionable = [r for r in results if r.status in ("drift", "built_unpushed")]
    other = [r for r in results if r.status not in ("drift", "built_unpushed")]

    if actionable:
        lines.append("Actionable:")
        for r in actionable:
            icon = _STATUS_ICON.get(r.status, r.status)
            filed = f" -> filed {r.filed_ticket}" if r.filed_ticket else (
                " (would file)" if r.status == "drift" and not committed else ""
            )
            lines.append(f"  {icon}  {r.slug}: {r.detail}{filed}")
        lines.append("")

    lines.append("All pairs:")
    for r in results:
        icon = _STATUS_ICON.get(r.status, r.status)
        lines.append(f"  {icon}  {r.slug}")
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
    token = read_telegram_token(TELEGRAM_ENV) if read_telegram_token else None
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
# Main.
# ---------------------------------------------------------------------------


def run_sweep(
    *,
    pairs_path: Path = DEFAULT_PAIRS_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    tickets_root: Path = TICKETS_ROOT,
    commit_prod: bool = False,
) -> list[PairResult]:
    """Evaluate every pair; file refresh tickets when commit_prod and drift found.

    Returns the per-pair results. Filing is idempotent (skips a pair that already
    has an open refresh ticket) and gated pairs are never filed.
    """
    pairs = load_pairs(pairs_path)
    state = load_state(state_path)
    results: list[PairResult] = []
    for pair in pairs:
        result = evaluate_pair(pair, state, cache_dir)
        if result.status == "drift" and not pair.get("gated", False):
            existing = existing_open_refresh_ticket(result.slug, tickets_root)
            if existing:
                result.detail += f" (open refresh ticket {existing} already covers this — not re-filing)"
                result.filed_ticket = existing
            elif commit_prod:
                try:
                    result.filed_ticket = file_refresh_ticket(pair, result)
                except subprocess.SubprocessError as exc:
                    result.detail += f" (FILING FAILED: {exc})"
        results.append(result)
    return results


def main(argv: list[str] | None = None) -> int:
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
    args = p.parse_args(argv)

    try:
        results = run_sweep(
            pairs_path=Path(args.pairs_path),
            state_path=Path(args.state_path),
            cache_dir=Path(args.cache_dir),
            tickets_root=Path(args.tickets_root),
            commit_prod=args.commit_prod,
        )
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"error: sweep failed to run: {exc}", file=sys.stderr)
        return 2

    digest = build_digest(results, committed=args.commit_prod)
    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2, sort_keys=True))
    else:
        print(digest)

    if not args.no_digest:
        _send_telegram(digest)

    # Exit non-zero only on a hard config/read error across ALL pairs (so the
    # OnFailure mailbox alert fires for a genuinely broken sweep, not for a
    # normal drift-found week). A single pair error is reported in the digest.
    if results and all(r.status == "error" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
