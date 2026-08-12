#!/usr/bin/env python3
"""Pantheon scripted knowledge-sweep driver (ORG-012 Phase C, T-0781).

The cross-folder throughline sweep (ORG-012 §Cadence #6) operationalized as a
daily-but-diff-gated scripted process. Reconciles two facts that look like a
contradiction:

- ORG-012 §6 ratified the sweep at a *quarterly* cadence ("every ~3 months").
- The operator (T-0781 Ask, msg 294/296) wants it *daily + scripted*.

Squared by the **diff-vs-last-landed gate** plus a **MIN_SWEEP_INTERVAL_DAYS
floor** (T-0974): the timer fires daily, but it only files a dispatch ticket when
the corpus has actually *changed* since the last completed sweep AND at least N
days (default 7, env-tunable, 0 disables) have passed since the last sweep
LANDED. The diff alone fires ~daily on an active repo (the corpus changes most
days) for a ~0-1-item yield per full review — the floor collapses that daily
noise into bigger, weekly-ish batches matching ORG-012 §Cadence's intent. Quiet
days and within-floor days are silent no-ops; `--force` bypasses both gates.

## Dispatch shape — ORG-025 in-session, team-pattern (T-0855 precedent)

The sweep dispatches *review subagents* over the corpus. Under ORG-025 (watch
dispatch migrated off headless `claude -p` for the same subscription-billing
reason, T-0855) the review fan-out must run **in Specialist's interactive session**,
not headless. So this script is a *due-detector + ticket-filer*, mirroring
`watch_runner.py --file-watch-ticket`:

  cron `--file-sweep-ticket`
     → (corpus changed?) → file ONE target=specialist "knowledge sweep dispatch"
       ticket whose body carries the **team-SOP dispatch recipe**
       (`sop/standard/team-orchestrator-dispatch-checklist.md`: implicit
       agent-team, lead spawns review workers via the Agent tool with distinct
       names, SendMessage(to:<name>) routing + brief construction. NB:
       TeamCreate/TeamDelete were removed in Claude Code 2.1.178)
     → Specialist's inbox-consumer delivers it in-session
     → Specialist runs the review team, diffs candidates vs the prior run, and files
       the result per `sop/standard/throughline-sweep-result-filing.md`
       (parent index ticket + one discrete sub-ticket per item — NEVER a rolling
       decision menu).

This file is **component (1)** of that arc: the gate + the dispatch-ticket
filer. The in-session review-team dispatch and the SOP-shaped multi-ticket filer
are component (2); the last-landed fingerprint that this gate diffs against is
saved by component (2)'s land step (so the gate stays quiet until a sweep
actually completes, not merely until one is dispatched).

CLI:
    python3 tools/knowledge_sweep.py --file-sweep-ticket   # daily timer entrypoint
    python3 tools/knowledge_sweep.py --dry-run             # show the diff; no side effects
    python3 tools/knowledge_sweep.py --repo-root <path>    # tmp-target for integration smoke

Default-safe contract (T-0261, sop/standard/auto-commit-cron-default-safe-contract.md):
refuses to run without an explicit mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Reuse the proven ticket-file invoker (transient-retry kernel) + runner-log
# helper from watch_runner rather than re-implementing them — same discipline,
# one source of truth.
from tools.watch_runner import (  # noqa: E402
    _append_runner_log,
    _invoke_ticket_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TICKET_BIN = REPO_ROOT / "tools" / "ticket.py"

STATE_DIR = Path.home() / ".local" / "state" / "pantheon" / "knowledge_sweep"
STATE_FILE = STATE_DIR / "state.json"

# Specialist's own memory pins. ORG-012 §6 includes feedback_*.md in the sweep corpus
# (Recovery scope note on T-0781); folding them in subsumes the separate weekly
# memory-pin sweep (§Cadence #5). NOTE (cross-user gap, surfaced to the operator):
# §5 as written targets HO's pins at /home/claude/.../memory, which Specialist cannot
# read (cross-user perms, see reference_specialist_git_credential_scope). This script
# fingerprints the pins it *can* read (Specialist's own); HO's pins need either an
# HO-run portion or a perms grant — a component-(2) design question, not a
# blocker for the gate.
MEMORY_DIR = Path.home() / ".claude" / "projects" / "-home-claude-research" / "memory"

# The corpus the sweep reviews (ORG-012 §6 + T-0781 Ask). Recursive *.md per
# subdir; feedback_*.md pins handled separately via MEMORY_DIR.
CORPUS_SUBDIRS = (
    "lessons",
    "bugs",
    "incidents",
    "notes",
    "sop",
    "rulebook",
    "adr",
    "knowledge",
)

# Corpus paths excluded from the fingerprint. `knowledge/watch/reports/<date>/`
# is high-churn research OUTPUT — a new dir lands on every watch fire — and is not
# a promotion-candidate tier (ORG-012 §6's authoritative corpus is lessons/ +
# feedback + sop/ + bugs/ + incidents/ and omits knowledge/ entirely; the T-0781
# Ask broadened it, but the daily-churn reports subdir would trip the diff gate on
# every watch fire and defeat the "avoid daily-noise" intent). knowledge/episodic/
# (research syntheses) stays in — it's low-churn and a legitimate (rare) promotion
# source.
CORPUS_EXCLUDE_PREFIXES = ("knowledge/watch/reports/",)

# Substring marker placed in the dispatch ticket's context. Idempotency scans the
# inbox for it: at most ONE unconsumed sweep dispatch should be pending at a time
# (a sweep is a single unit of work — piling up daily tickets would defeat the
# diff gate's whole purpose).
DISPATCH_MARKER = "knowledge-sweep-dispatch"

SWEEP_LABELS = "knowledge,sweep,sweep-dispatch,org-012"

# MIN_SWEEP_INTERVAL_DAYS floor (T-0974). The diff gate alone fires ~daily on an
# active repo (corpus changes most days), contradicting ORG-012 §Cadence's
# weekly-ish intent — each fire costs a full multi-agent review for a ~0-1-item
# yield (empirical: T-0908 + T-0972). The floor keeps the diff tracking
# continuous but only FILES a dispatch when BOTH (a) the corpus changed AND
# (b) >= N days since the last LANDED sweep — collapsing daily noise into bigger,
# more-worthwhile weekly batches. Env-tunable (default 7d to match §Cadence);
# 0 disables the floor (pure diff-gate, prior behavior); --force always bypasses.
DEFAULT_MIN_SWEEP_INTERVAL_DAYS = 7
MIN_SWEEP_INTERVAL_ENV = "MIN_SWEEP_INTERVAL_DAYS"


def min_sweep_interval_days(env: dict | None = None) -> int:
    """The configured floor in days. Reads MIN_SWEEP_INTERVAL_DAYS from the env;
    falls back to the 7d default on absence / blank / non-int / negative."""
    raw = (env if env is not None else os.environ).get(MIN_SWEEP_INTERVAL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_MIN_SWEEP_INTERVAL_DAYS
    try:
        val = int(raw.strip())
    except ValueError:
        return DEFAULT_MIN_SWEEP_INTERVAL_DAYS
    return val if val >= 0 else DEFAULT_MIN_SWEEP_INTERVAL_DAYS


def within_min_interval(
    last_sweep_iso: str | None, today: date, env: dict | None = None
) -> tuple[int, int] | None:
    """If a sweep LANDED fewer than the floor's days ago, return
    (days_since, floor) to signal "defer". Otherwise (no prior sweep, floor
    disabled, or interval satisfied) return None. Unparseable last_sweep → None
    (fail open: don't suppress a sweep on bad state)."""
    floor = min_sweep_interval_days(env)
    if floor <= 0 or not last_sweep_iso:
        return None
    try:
        last = date.fromisoformat(last_sweep_iso)
    except (ValueError, TypeError):
        return None
    days_since = (today - last).days
    return (days_since, floor) if days_since < floor else None


# ---------------------------------------------------------------------------
# Corpus fingerprint + diff.
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    """sha256 of a file's bytes. Content-hash (not mtime) so git pull/checkout
    mtime churn doesn't trip false-positive sweeps."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def compute_corpus_fingerprint(
    repo_root: Path | None = None,
    memory_dir: Path | None = None,
) -> dict[str, str]:
    """Map of corpus-relative-path → content sha256 over every *.md in the
    corpus subdirs plus the accessible feedback_*.md memory pins.

    Pin keys are prefixed ``memory:`` so they're unambiguous in the diff and can
    never collide with a repo path. A missing/unreadable memory dir is skipped
    (logged by the caller), not an error — the cross-user gap must not brick the
    in-repo sweep.
    """
    root = repo_root or REPO_ROOT
    mem = memory_dir or MEMORY_DIR
    fp: dict[str, str] = {}
    for sub in CORPUS_SUBDIRS:
        d = root / sub
        if not d.is_dir():
            continue
        for md in sorted(d.rglob("*.md")):
            if not md.is_file():
                continue
            rel = str(md.relative_to(root))
            if any(rel.startswith(p) for p in CORPUS_EXCLUDE_PREFIXES):
                continue
            fp[rel] = _hash_file(md)
    if mem and mem.is_dir():
        for pin in sorted(mem.glob("feedback_*.md")):
            if pin.is_file():
                fp[f"memory:{pin.name}"] = _hash_file(pin)
    return fp


def diff_fingerprint(
    old: dict[str, str], new: dict[str, str]
) -> dict[str, list[str]]:
    """Return {added, changed, removed} sorted path lists between two
    fingerprints."""
    old_keys, new_keys = set(old), set(new)
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    changed = sorted(k for k in old_keys & new_keys if old[k] != new[k])
    return {"added": added, "changed": changed, "removed": removed}


def _diff_is_empty(diff: dict[str, list[str]]) -> bool:
    return not (diff["added"] or diff["changed"] or diff["removed"])


# ---------------------------------------------------------------------------
# State (last-LANDED fingerprint; written by component (2)'s land, read here).
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"schema_version": 1, "corpus_fingerprint": {}, "last_sweep": None}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Candidate identity + diff (so a daily sweep surfaces only NEW candidates,
# not the same standing menu every time the corpus changes).
# ---------------------------------------------------------------------------


def candidate_key(cand: dict) -> str:
    """Stable identity for a sweep candidate, independent of rationale wording.

    A candidate is "the same" across runs if it proposes the same move (type) of
    the same sources to the same target tier. Rationale/priority can drift
    without making it a new candidate — that's what keeps the daily diff quiet.
    """
    sources = "|".join(sorted(cand.get("sources", [])))
    payload = f"{cand.get('type', '')}::{sources}::{cand.get('target_tier', '')}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def new_candidates(candidates: list[dict], seen_keys: set[str]) -> list[dict]:
    """Subset of candidates whose key is not in seen_keys (the prior run's set)."""
    return [c for c in candidates if candidate_key(c) not in seen_keys]


# ---------------------------------------------------------------------------
# Idempotency.
# ---------------------------------------------------------------------------


def pending_dispatch_in_inbox(repo_root: Path | None = None) -> str | None:
    """Return the filename of an unconsumed sweep-dispatch ticket if one is
    already sitting in tickets/inbox/, else None. One pending sweep at a time."""
    root = repo_root or REPO_ROOT
    inbox = root / "tickets" / "inbox"
    if not inbox.is_dir():
        return None
    for p in sorted(inbox.glob("*.md")):
        try:
            if DISPATCH_MARKER in p.read_text(encoding="utf-8"):
                return p.name
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Dispatch-ticket body (carries the team-SOP recipe).
# ---------------------------------------------------------------------------


def _build_ask(today: date, diff: dict[str, list[str]]) -> str:
    n = len(diff["added"]) + len(diff["changed"]) + len(diff["removed"])
    changed_lines = "\n".join(
        f"- [{kind}] {p}"
        for kind in ("added", "changed", "removed")
        for p in diff[kind]
    ) or "- (forced run — no diff computed)"
    return (
        f"ORG-012 §6 throughline sweep is due ({today.isoformat()}): {n} corpus "
        f"file(s) changed since the last completed sweep. Run the review fan-out "
        f"IN-SESSION per "
        f"`sop/standard/team-orchestrator-dispatch-checklist.md`. Your session "
        f"has an implicit agent-team and you are its lead — TeamCreate/TeamDelete "
        f"were REMOVED in Claude Code 2.1.178; spawn each review worker via the "
        f"Agent tool with a distinct name and address them via "
        f"SendMessage(to:<name>):\n"
        f"1. Spawn each review worker via Agent (distinct name). Brief each with "
        f"locked corpus scope + the ORG-012 tier-jump / demotion criteria + "
        f"bubble-up criteria verbatim + the HERMES rule + DECISIONS-propagation. "
        f"(SendMessage addresses teammates by the name you gave them.)\n"
        f"2. Each worker applies ORG-012 promotion/demotion criteria to its slice "
        f"of the corpus and returns candidates.\n"
        f"3. Diff candidates vs the prior run; keep only NEW/changed ones.\n"
        f"4. VERIFY each surviving candidate against the live repo BEFORE filing — "
        f"the first dry-run (2026-06-06) found ~1 in 12 raw worker findings was "
        f"materially wrong (claimed an SOP 'never filed' that exists at another "
        f"path; stale-status claims that didn't hold). Confirm each candidate's "
        f"file paths, status lines, and 'already-done?' against the repo (greps "
        f"mis-fire on wording — read the file). Drop or correct any that don't "
        f"hold; mark confidence on the rest.\n"
        f"5. File the result per "
        f"`sop/standard/throughline-sweep-result-filing.md`: a parent INDEX "
        f"ticket + one discrete sub-ticket per VERIFIED item (decisions→operator, "
        f"builds→owning agent, bookkeeping grouped). NEVER a rolling menu.\n"
        f"6. Record the completed sweep (updates the corpus fingerprint so this "
        f"gate goes quiet until the next real change).\n"
        f"The implementer/merge/e2e sections of the team SOP do NOT apply — this "
        f"is a read-only review sweep, nothing to merge.\n\n"
        f"Changed corpus files:\n{changed_lines}"
    )


def _build_context(today: date) -> str:
    return (
        f"{DISPATCH_MARKER}: {today.isoformat()}\n\n"
        f"Filed by `knowledge_sweep.py --file-sweep-ticket` — the ORG-012 Phase C "
        f"(T-0781) scripted-sweep timer entrypoint. The timer is a due-detector + "
        f"ticket-filer; it fires NO researcher/review subagent itself. The "
        f"in-session review fan-out (component 2) runs as a team-pattern cycle "
        f"per the team-orchestrator-dispatch-checklist SOP and files results per "
        f"the throughline-sweep-result-filing SOP. See "
        f"adr/ORG-012-information-inflow-bubble-up-methodology.md §Cadence #6."
    )


# ---------------------------------------------------------------------------
# Day-index integrity check (T-1127, follow-on to T-1121).
# ---------------------------------------------------------------------------


def _check_day_index_drift() -> None:
    """Surface watch-report day-index drift in the daily sweep (T-1127).

    `tools/lint_report_day_index.py` (T-1121) asserts each
    `knowledge/watch/reports/<date>/README.md` matches the `*.md` files on disk
    (dead-link + unindexed detection). The drift it catches comes from MANUAL
    report deliveries that bypass the cron watch_runner's index regen — so the
    *runner* structurally can't catch it (it only sees runner-driven deliveries).
    The daily sweep is the cross-cutting checkpoint that sees every day-dir
    regardless of how reports landed, so the check lives here (DECISIONS: T-1127).

    Contract: NEVER fails the sweep (equivalent to the lint's `--warn`), achieved
    by swallowing the gate-mode returncode here — we run gate-mode (no `--warn`)
    because the returncode is the stable drift signal, then surface any drift to
    stderr (systemd-journal), but the sweep's own exit is never affected.
    Best-effort: any error invoking the lint is logged and ignored, never
    propagated.

    ⚠️ CORRECTED 2026-07-29 (T-2358, specialist). This paragraph used to end "+ the
    runner log (next digest summarizer)". THAT MITIGATION DOES NOT EXIST and never
    did: `watch_digest.py` contains no reference to RUNNER_LOG (verified with a
    known-positive so the zero is a fact about the file, not a broken read), and
    repo-wide RUNNER_LOG has writers and no readers. So the drift reaches the
    journal and stops there — nothing summarises it to anyone.

    Kept as a correction rather than a clean edit because of what the sentence was
    DOING. The swallow above is correct and stays. But a swallowed failure is only
    acceptable if something downstream catches it, and this docstring named a
    catcher that was never wired — so the justification for passing silently was
    itself the unverified part. That is the same shape as the comfort sentence in
    artifact_gc (T-2309): the line that makes a reviewer stop looking is exactly
    the line that was false.

    MEASURED COST (specialist, from the runner log): 21 days of undetected drift in two
    episodes — 2026-06-28..07-13, escalating 1 -> 4 items, and 2026-07-23..07-27,
    1 item. Not hypothetical.

    RESOLVED 2026-07-29 (T-2363, decided by recovery, wired by specialist). Journal-only was
    considered and rejected on the measurement above: "it is in the journal" is a
    reporting surface only if something reads the journal unprompted, and nothing
    does. The real surface is now a drift row in the daily watch digest —
    `watch_digest.collect_day_index_drift()`, silent on a clean day, LOUD if the
    lint itself fails, so digest silence means "ran and clean" and nothing else.
    Regression tests: `tools/test_watch_digest.py::TestDayIndexDriftRow`.

    The digest recomputes the count from disk rather than receiving it from this
    function. That is deliberate: handing it over would couple the two units in
    fire order and add a staleness mode in which a stale zero reads exactly like a
    clean day. This function's swallow is therefore unchanged, and no signal here
    is load-bearing for the report.
    """
    lint_bin = REPO_ROOT / "tools" / "lint_report_day_index.py"
    reports_dir = REPO_ROOT / "knowledge" / "watch" / "reports"
    if not lint_bin.exists() or not reports_dir.exists():
        return
    try:
        proc = subprocess.run(
            [sys.executable, str(lint_bin), str(reports_dir)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:  # never let a hygiene check break the sweep
        print(f"[day-index-lint] skipped (lint invocation error: {exc})", file=sys.stderr)
        return
    if proc.returncode == 0:
        return  # clean — stay quiet
    detail = (proc.stdout or "").strip() or (proc.stderr or "").strip() or "drift detected"
    print(
        f"[day-index-lint] day-index drift detected (T-1121/T-1127) — "
        f"a manual delivery likely bypassed the runner's index regen:\n{detail}",
        file=sys.stderr,
    )
    summary = detail.splitlines()[-1].strip() if detail else "drift detected"
    _append_runner_log(f"[specialist] day-index-lint: DRIFT — {summary}")


# ---------------------------------------------------------------------------
# Timer entrypoint.
# ---------------------------------------------------------------------------


def file_sweep_ticket(
    today: date,
    *,
    force: bool = False,
    ticket_bin: Path = TICKET_BIN,
    env: dict | None = None,
) -> int:
    """Daily timer entrypoint. Files ONE target=specialist knowledge-sweep-dispatch
    ticket iff the corpus changed since the last completed sweep AND no sweep
    dispatch is already pending. Fires NO review subagent. Returns 0 on success /
    nothing-changed / already-pending; 1 on a filing failure.
    """
    # Day-index integrity check (T-1127): runs FIRST, every daily fire, before the
    # corpus-changed gate — so manual-delivery drift surfaces within a day even on
    # days no sweep dispatch is filed. Never affects this function's return.
    _check_day_index_drift()

    new_fp = compute_corpus_fingerprint()
    state = load_state()
    old_fp = state.get("corpus_fingerprint", {})
    diff = diff_fingerprint(old_fp, new_fp)

    if not force and _diff_is_empty(diff):
        print(f"[{today.isoformat()}] corpus unchanged since last sweep; no dispatch filed")
        _append_runner_log(f"[specialist] knowledge-sweep: unchanged ({today.isoformat()})")
        return 0

    # MIN_SWEEP_INTERVAL_DAYS floor (T-0974): the corpus changed, but defer filing
    # if the last sweep landed fewer than the floor's days ago — collapses daily
    # diff-noise into a weekly-ish batch per ORG-012 §Cadence. --force bypasses.
    if not force:
        deferred = within_min_interval(state.get("last_sweep"), today)
        if deferred is not None:
            days_since, floor = deferred
            print(
                f"[{today.isoformat()}] corpus changed but last sweep was "
                f"{days_since}d ago (< {floor}d floor); deferring dispatch"
            )
            _append_runner_log(
                f"[specialist] knowledge-sweep: deferred ({days_since}d < {floor}d floor)"
            )
            return 0

    pending = pending_dispatch_in_inbox()
    if pending:
        print(
            f"[{today.isoformat()}] sweep dispatch already pending ({pending}); skipping"
        )
        _append_runner_log(
            f"[specialist] knowledge-sweep: dispatch already pending ({pending})"
        )
        return 0

    n = len(diff["added"]) + len(diff["changed"]) + len(diff["removed"])
    title = f"Knowledge sweep {today.isoformat()} — {n} corpus change(s) (ORG-012 §6)"
    if env is None:
        env = os.environ.copy() | {"PANTHEON_ACTOR": "specialist"}
    argv = [
        sys.executable,
        str(ticket_bin),
        "file",
        "--requester", "specialist",
        "--target", "specialist",
        # NB: no --intent — ticket.py rejects --intent for non-operator targets
        # (it's target=operator-specific). The 2026-06-06 13:00 fire failed on this.
        "--title", title,
        "--labels", SWEEP_LABELS,
        "--ask", _build_ask(today, diff),
        "--context", _build_context(today),
    ]
    outcome, tid, detail = _invoke_ticket_file(
        argv, f"file_sweep_ticket {today.isoformat()}", env
    )
    if outcome == "filed":
        print(f"[{today.isoformat()}] filed knowledge sweep dispatch ticket {tid or '(id?)'} ({n} change(s))")
        _append_runner_log(f"[specialist] knowledge-sweep: filed {tid} ({n} change(s))")
        return 0
    print(
        f"[{today.isoformat()}] FAILED to file knowledge sweep dispatch ticket: {detail}",
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------------------
# Land a completed sweep: persist the corpus fingerprint + candidate set so the
# gate goes quiet until the NEXT real corpus change, and the next sweep can diff
# its candidates against this run's. Called by the in-session component (2) after
# the review team + SOP-shaped filing complete.
# ---------------------------------------------------------------------------


def land_sweep(
    today: date,
    candidates: list[dict],
    *,
    repo_root: Path | None = None,
    memory_dir: Path | None = None,
) -> int:
    """Record a completed sweep into state. Returns 0."""
    new_fp = compute_corpus_fingerprint(repo_root, memory_dir)
    state = load_state()
    prior_keys = set(state.get("last_candidates", []))
    keys = sorted({candidate_key(c) for c in candidates})
    fresh = new_candidates(candidates, prior_keys)
    state["corpus_fingerprint"] = new_fp
    # Union: a candidate the operator hasn't actioned yet shouldn't re-surface as
    # "new" next run, but genuinely-new ones still appear. Carry prior + this run.
    state["last_candidates"] = sorted(prior_keys | set(keys))
    state["last_sweep"] = today.isoformat()
    save_state(state)
    print(
        f"[{today.isoformat()}] landed sweep: {len(new_fp)} corpus files "
        f"fingerprinted, {len(candidates)} candidate(s) ({len(fresh)} new this run)"
    )
    _append_runner_log(
        f"[specialist] knowledge-sweep: landed ({len(candidates)} cand, {len(fresh)} new)"
    )
    return 0


def load_candidates(path: Path) -> list[dict]:
    """Load a candidates JSON file (list of {sources, type, target_tier, ...})."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("candidates file must be a JSON list of candidate objects")
    return data


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Pantheon scripted knowledge-sweep driver (ORG-012 Phase C, "
                    "T-0781). Default-safe contract (T-0261): must pass "
                    "--file-sweep-ticket, --dry-run, or --repo-root explicitly.",
    )
    p.add_argument(
        "--file-sweep-ticket", action="store_true",
        help="DAILY TIMER ENTRYPOINT: if the corpus changed since the last "
             "completed sweep, file ONE target=specialist knowledge-sweep-dispatch "
             "ticket (carries the team-SOP review recipe). Fires NO subagent. "
             "Idempotent (one pending sweep at a time). Prod-mutating mode.",
    )
    p.add_argument(
        "--land-sweep", action="store_true",
        help="record a completed sweep: persist the corpus fingerprint + candidate "
             "keys (from --candidates-file) so the gate goes quiet until the next "
             "change. Prod-mutating mode (writes state).",
    )
    p.add_argument(
        "--candidates-file", type=Path, default=None,
        help="path to the candidates JSON (list of {sources,type,target_tier,...}) for --land-sweep.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="compute + print the corpus diff vs last sweep; file nothing. Safe-mode.",
    )
    p.add_argument(
        "--repo-root", type=Path, default=None,
        help="override REPO_ROOT (tmp pantheon-config equivalent) for integration smoke. Safe-mode.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="ignore the diff gate; file a dispatch ticket regardless (still idempotent vs a pending one).",
    )
    return p.parse_args(argv)


def _enforce_default_safe(args: argparse.Namespace) -> int | None:
    """Refuse to run without an explicit mode (T-0261). Prod mode
    (--file-sweep-ticket) and safe modes (--dry-run / --repo-root) are mutually
    exclusive. Returns 64 to reject, None to proceed."""
    safe_explicit = args.dry_run or (args.repo_root is not None)
    prod_modes = [
        name for name, on in (
            ("--file-sweep-ticket", args.file_sweep_ticket),
            ("--land-sweep", args.land_sweep),
        ) if on
    ]
    prod_explicit = bool(prod_modes)
    if len(prod_modes) > 1:
        print(
            f"ERROR: prod modes are mutually exclusive; pass exactly one of "
            f"{', '.join(prod_modes)}.",
            file=sys.stderr,
        )
        return 64
    if prod_explicit and safe_explicit:
        print(
            "ERROR: prod mode (--file-sweep-ticket / --land-sweep) conflicts with "
            "safe-mode flag (--dry-run / --repo-root).",
            file=sys.stderr,
        )
        return 64
    if not prod_explicit and not safe_explicit:
        print(
            "ERROR: refusing to run without explicit mode. Pass one of:\n"
            "  --file-sweep-ticket  file a sweep-dispatch ticket if the corpus changed (daily timer)\n"
            "  --land-sweep         record a completed sweep (fingerprint + candidates) into state\n"
            "  --dry-run            print the corpus diff vs last sweep; no side effects\n"
            "  --repo-root <path>   redirect to a tmp pantheon-config dir (smoke path)\n"
            "Per sop/standard/auto-commit-cron-default-safe-contract.md (T-0261).",
            file=sys.stderr,
        )
        return 64
    return None


def main(argv: list[str] | None = None) -> int:
    global REPO_ROOT, TICKET_BIN
    args = parse_args(argv)

    gate = _enforce_default_safe(args)
    if gate is not None:
        return gate

    if args.repo_root is not None:
        REPO_ROOT = args.repo_root.resolve()
        TICKET_BIN = REPO_ROOT / "tools" / "ticket.py"

    if args.dry_run:
        new_fp = compute_corpus_fingerprint()
        diff = diff_fingerprint(load_state().get("corpus_fingerprint", {}), new_fp)
        n = len(diff["added"]) + len(diff["changed"]) + len(diff["removed"])
        print(f"corpus files fingerprinted: {len(new_fp)}")
        print(f"changes since last sweep: {n}")
        for kind in ("added", "changed", "removed"):
            for path in diff[kind]:
                print(f"  [{kind}] {path}")
        if pending := pending_dispatch_in_inbox():
            print(f"(a sweep dispatch is already pending: {pending})")
        return 0

    if args.land_sweep:
        if args.candidates_file is None:
            print("ERROR: --land-sweep requires --candidates-file <path>.", file=sys.stderr)
            return 64
        return land_sweep(_today(), load_candidates(args.candidates_file))

    if args.file_sweep_ticket:
        return file_sweep_ticket(_today(), force=args.force)

    return 0


def _today() -> date:
    """Today's date. Isolated so tests can monkeypatch without touching the
    stdlib (mirrors watch_runner's date handling)."""
    import datetime as _dt

    return _dt.date.today()


if __name__ == "__main__":
    raise SystemExit(main())
