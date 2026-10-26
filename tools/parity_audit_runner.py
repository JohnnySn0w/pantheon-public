"""tools/parity_audit_runner.py — scheduled fleet seat-parity audit under recovery's
substrate-audit lane (T-3750, ORG-099 §9 BT3).

WHAT THIS IS. tools/parity_render.py is the audit ENGINE — it renders and runs
each ORG-099 axis check against each seat and returns per-axis verdicts + a
per-seat aggregate state. This module is the durable SUBSTRATE SIGNAL around it:
a 6-hourly runner (mirroring tools/delivery_reachability.py's armed cadence
under recovery) that persists the report, tracks drift ACROSS runs, and escalates
STRUCTURAL gaps to orchestrator — so a seat that silently drifts off parity is caught
by a standing net rather than only at the next hand-audit.

ESCALATION CONTRACT (ORG-099 §9, the ticket's four rules):

  HARD FAIL  — a seat is BROKEN (a HARD/BILLING/DOMAIN axis is non-passing).
               -> IMMEDIATE mailbox to orchestrator. Dedup'd by a break-set signature
                  so a persistent break does not re-alert every 6h; it re-alerts
                  only when the set of broken (seat, axis) pairs CHANGES, and a
                  clear-then-reappear re-alerts because the signature returns to
                  empty in between.
  CARVE_OUT_BROKEN — a ratified carve-out's verifier itself failed.
               -> mailbox orchestrator for a SPEC-TIER decision (carve-out expired? seat
                  drifted? spec needs amend?). Same signature dedupe as HARD.
  SOFT FAIL persisting > 30d — a seat has been PARTIAL (only SOFT axes failing)
               continuously for longer than the rebaseline horizon.
               -> file a REBASELINE ticket target=role:engineering_orchestrator (one per seat; cleared
                  when the seat recovers, so a later regression re-files).
  UNMEASURABLE persisting >= N runs — a (seat, axis) cell has read UNMEASURABLE
               for N consecutive runs (a check_ref bug or a substrate-access
               problem, not a transient sudo hiccup).
               -> mailbox orchestrator, batched. Cleared when the cell becomes
                  measurable again.

READ-ONLY, like delivery_reachability. It invokes parity_render (which itself
never injects, enables or sends) and writes only to recovery's OWN report dir and to
the mailbox/ticket lanes. It pokes no live agent pane.

EXIT 0 REGARDLESS. The escalation IS the output. A non-zero exit would fire the
unit's OnFailure= and report THIS monitor instead of the drift it found — the
same reason delivery_reachability.main() always returns 0. Genuine crashes
(an unreadable manifest, an import failure) still propagate to OnFailure.

RECOVERY-SIDE SCOPE CALLS (the ticket delegates the wire-up mechanic to Recovery):
  * A fresh dedicated runner + unit pair, NOT a wrapper riding delivery
    reachability's timer — the two audits have independent failure surfaces and
    cadence knobs and should not share a oneshot.
  * All four categories route to ORCHESTRATOR (who authors axes.yaml / carve-outs.yaml
    and owns remediation dispatch); the affected seat is named in every message
    so orchestrator routes to the seat-owner. We do not fan escalations out into other
    agents' mailboxes unsolicited — a poke costs that agent a turn, and orchestrator is
    the single spec+dispatch owner, so cross-routing there is both sufficient and
    the conservative choice (delivery_reachability routes everything through one
    triage lane for the same reason).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

# parity_render lives beside us; import it in-process so we get one audit pass
# with typed CheckOutcome access (.verdict/.tier) rather than subprocess-parsing
# a second, cost-doubling cross-account run. Mirrors delivery_reachability's
# sys.path.insert + bare-import of its sibling helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import parity_render as pr  # noqa: E402

AGENT = "recovery"
REPO_ROOT = Path(__file__).resolve().parent.parent
TICKET_BIN = REPO_ROOT / "tools" / "ticket.py"

DEFAULT_REPORT_DIR = Path.home() / "parity-audit"
STATE_BASENAME = "state.json"
LATEST_STATUS_BASENAME = "latest-status.txt"

# 4 consecutive 6-hourly runs ≈ 24h of an axis reading UNMEASURABLE — long enough
# to clear a transient sudo/bus hiccup, short enough that a real check_ref bug or
# access regression surfaces inside a day.
UNMEASURABLE_ESCALATE_RUNS = 4
# A seat PARTIAL (SOFT axes only) for longer than this is drift the spec should
# either fix or ratify; either way it is orchestrator's call, filed as a rebaseline.
SOFT_FAIL_REBASELINE_DAYS = 30
# Keep the report dir bounded: 14 days * 4/day.
REPORT_RETENTION = 56

_REBASELINE_SERVES = (
    "maintenance:seat-parity — a seat drifting off parity on SOFT axes for a full "
    "rebaseline horizon is either a spec that needs updating or a seat that needs "
    "remediation; orchestrator (axes.yaml owner) decides which."
)


def _log(msg: str) -> None:
    print(f"[parity_audit_runner] {msg}", file=sys.stderr)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Audit pass — the one place that actually runs the checks (via parity_render).
# ---------------------------------------------------------------------------

def run_fleet_audit(*, execute: bool = True) -> tuple[list[pr.SeatReport], list[pr.Axis]]:
    """Evaluate every seat against every applicable axis. Returns (reports, axes).

    Raises pr.ManifestError only for a genuinely-fatal manifest (unreadable /
    non-mapping) — that SHOULD propagate to OnFailure, because a runner that
    cannot read its own contract must not report a clean fleet."""
    axes = pr.load_axes()
    carve_outs = pr.load_carve_outs()
    seats = pr.load_seats()
    seats_by_persona = {s.persona: s for s in seats}
    instance_raw = pr._load_yaml(pr.DEFAULT_INSTANCE_PATH)
    runner = pr.Runner()
    reports = [
        pr.evaluate_seat(s, axes, carve_outs, instance_raw, seats_by_persona, runner, execute)
        for s in seats
    ]
    return reports, axes


# ---------------------------------------------------------------------------
# Classification — PURE. Reduces reports to the shapes the escalation rules key
# on. No I/O, no clock, no side effects: this is what the tests drive directly.
# ---------------------------------------------------------------------------

# parity_render's own non-passing set (FAIL / UNMEASURABLE / CARVE_OUT_BROKEN).
_NON_PASSING = pr._NON_PASSING
_HARD_TIERS = ("HARD", "BILLING", "DOMAIN")


@dataclass(frozen=True)
class Findings:
    # (seat, axis, verdict) for hard-tier DEFINITE FAILs — the immediate-alarm set.
    # NOTE: this is FAIL only, NOT UNMEASURABLE. parity_render's aggregate folds an
    # UNMEASURABLE HARD axis into BROKEN (alarm-biased, ORG-099 §8), but the ticket's
    # escalation contract keeps the two APART: a definite HARD FAIL pages orchestrator
    # immediately, whereas an UNMEASURABLE cell (a check_ref bug or a substrate-access
    # gap — e.g. sudo-to-self on recovery's own seat, or a prose/non-boolean check_ref)
    # only escalates after it PERSISTS N runs. Conflating them makes every audit that
    # cannot cleanly measure an axis read as a fleet-wide urgent break. So HARD FAIL =
    # verdict == "FAIL" on a HARD/BILLING/DOMAIN axis, and nothing else.
    hard_fails: tuple[tuple[str, str, str], ...]
    # (seat, axis) whose verdict is exactly CARVE_OUT_BROKEN (any tier).
    carve_outs_broken: tuple[tuple[str, str], ...]
    # seats whose aggregate is PARTIAL (SOFT-only failing).
    partial_seats: tuple[str, ...]
    # every seat name seen this run (to expire cleared state).
    all_seats: tuple[str, ...]
    # (seat, axis) whose verdict is UNMEASURABLE this run.
    unmeasurable_cells: tuple[tuple[str, str], ...]
    # seat -> aggregate state, for the human status line.
    seat_state: dict[str, str] = field(default_factory=dict)
    # seats BROKEN specifically because a HARD axis FAILed (vs only UNMEASURABLE) —
    # lets the status line say which of the BROKEN seats are real FAILs.
    fail_broken_seats: tuple[str, ...] = ()


def classify(reports: list[pr.SeatReport]) -> Findings:
    hard_fails: list[tuple[str, str, str]] = []
    carve_broken: list[tuple[str, str]] = []
    partial: list[str] = []
    unmeasurable: list[tuple[str, str]] = []
    seat_state: dict[str, str] = {}
    all_seats: list[str] = []
    fail_broken: set[str] = set()
    for r in reports:
        all_seats.append(r.seat)
        seat_state[r.seat] = r.state
        if r.state == "PARTIAL":
            partial.append(r.seat)
        for o in r.outcomes:
            if o.verdict == "CARVE_OUT_BROKEN":
                carve_broken.append((r.seat, o.axis_id))
            if o.verdict == "UNMEASURABLE":
                unmeasurable.append((r.seat, o.axis_id))
            if o.tier in _HARD_TIERS and o.verdict == "FAIL":
                hard_fails.append((r.seat, o.axis_id, o.verdict))
                fail_broken.add(r.seat)
    return Findings(
        hard_fails=tuple(sorted(hard_fails)),
        carve_outs_broken=tuple(sorted(carve_broken)),
        partial_seats=tuple(sorted(partial)),
        all_seats=tuple(sorted(all_seats)),
        unmeasurable_cells=tuple(sorted(unmeasurable)),
        seat_state=seat_state,
        fail_broken_seats=tuple(sorted(fail_broken)),
    )


def _seat_in_maintenance(seat: str) -> bool:
    """T-5254: True iff `seat` (a persona name — SeatReport.seat is SeatBinding.persona, the
    same key space as notify_policy) is in an ACTIVE seat-maintenance window. Fail-open."""
    try:
        try:
            from tools.notify_policy import in_maintenance
        except ImportError:  # bare run with cwd=tools/
            from notify_policy import in_maintenance  # type: ignore
        return bool(in_maintenance(seat))
    except Exception:  # noqa: BLE001 — a registry fault must never silence a real alert
        return False


def held_seats(findings: Findings, in_maintenance=None) -> tuple[str, ...]:
    check = in_maintenance or _seat_in_maintenance  # resolved per call: patchable
    return tuple(s for s in findings.all_seats if check(s))


def exclude_held(findings: Findings, in_maintenance=None) -> Findings:
    """T-5254: drop held seats from every ALERT-facing set BEFORE decide(), so a held seat
    neither pages nor stamps a dedupe key (else the first real alert after the hold would be
    throttled as already-alerted). seat_state/all_seats stay intact: the audit table and
    status line keep recording the seat's real state."""
    held = set(held_seats(findings, in_maintenance))
    if not held:
        return findings
    return replace(
        findings,
        hard_fails=tuple(x for x in findings.hard_fails if x[0] not in held),
        carve_outs_broken=tuple(x for x in findings.carve_outs_broken if x[0] not in held),
        partial_seats=tuple(x for x in findings.partial_seats if x not in held),
        unmeasurable_cells=tuple(x for x in findings.unmeasurable_cells if x[0] not in held),
        fail_broken_seats=tuple(x for x in findings.fail_broken_seats if x not in held),
    )


def _signature(pairs) -> str:
    """Stable short digest of a set of tuples — the dedupe key for an alert set.
    Empty set -> "" so a cleared condition resets and a reappearance re-alerts."""
    items = sorted(pairs)
    if not items:
        return ""
    blob = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Decision — PURE. (findings, prior_state, now) -> (actions, new_state). Every
# dedupe/persistence rule lives here and nowhere else, so a test asserts on the
# returned actions without touching a mailbox or a ticket.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Action:
    kind: str              # "mailbox" | "ticket"
    subject: str
    body: str
    severity: str = "normal"


def _blank_state() -> dict:
    return {
        "soft_fail_first_seen": {},   # seat -> iso
        "unmeasurable_streak": {},    # "seat\x1faxis" -> int
        "unmeasurable_alerted": [],   # ["seat\x1faxis", ...]
        "rebaseline_filed": {},       # seat -> iso
        "alerted_hard": "",           # signature
        "alerted_carveout": "",       # signature
        "last_run": None,             # iso
    }


def _cell_key(seat: str, axis: str) -> str:
    return f"{seat}\x1f{axis}"


def decide(findings: Findings, prior: dict, now: datetime) -> tuple[list[Action], dict]:
    state = _blank_state()
    # carry forward known keys, tolerating a partial/old state file
    for k, default in _blank_state().items():
        if k in prior and isinstance(prior[k], type(default)):
            state[k] = json.loads(json.dumps(prior[k]))  # deep copy
    actions: list[Action] = []

    # ---- HARD FAIL (seat BROKEN) ------------------------------------------
    hard_sig = _signature(findings.hard_fails)
    if hard_sig != state["alerted_hard"]:
        if findings.hard_fails:
            actions.append(Action(
                kind="mailbox",
                severity="urgent",
                subject="[parity-audit] HARD FAIL — seat off parity",
                body=_render_hard_body(findings.hard_fails),
            ))
        state["alerted_hard"] = hard_sig

    # ---- CARVE_OUT_BROKEN -------------------------------------------------
    carve_sig = _signature(findings.carve_outs_broken)
    if carve_sig != state["alerted_carveout"]:
        if findings.carve_outs_broken:
            actions.append(Action(
                kind="mailbox",
                subject="[parity-audit] CARVE_OUT_BROKEN — spec-tier decision needed",
                body=_render_carveout_body(findings.carve_outs_broken),
            ))
        state["alerted_carveout"] = carve_sig

    # ---- SOFT FAIL persisting > 30d --------------------------------------
    partial = set(findings.partial_seats)
    first_seen = state["soft_fail_first_seen"]
    filed = state["rebaseline_filed"]
    # expire seats no longer PARTIAL so a future regression re-tracks + re-files.
    for seat in list(first_seen):
        if seat not in partial:
            first_seen.pop(seat, None)
            filed.pop(seat, None)
    for seat in sorted(partial):
        first_seen.setdefault(seat, _iso(now))
        seen_dt = _parse_iso(first_seen[seat]) or now
        age_days = (now - seen_dt).days
        if age_days >= SOFT_FAIL_REBASELINE_DAYS and seat not in filed:
            actions.append(Action(
                kind="ticket",
                subject=seat,
                body=_render_rebaseline_body(seat, first_seen[seat], age_days),
            ))
            filed[seat] = _iso(now)

    # ---- UNMEASURABLE persisting >= N runs -------------------------------
    streak = state["unmeasurable_streak"]
    alerted = set(state["unmeasurable_alerted"])
    current = {_cell_key(s, a) for (s, a) in findings.unmeasurable_cells}
    for key in list(streak):
        if key not in current:
            streak.pop(key, None)
            alerted.discard(key)
    newly: list[str] = []
    for key in sorted(current):
        streak[key] = streak.get(key, 0) + 1
        if streak[key] >= UNMEASURABLE_ESCALATE_RUNS and key not in alerted:
            newly.append(key)
            alerted.add(key)
    if newly:
        cells = [tuple(k.split("\x1f", 1)) for k in sorted(newly)]
        actions.append(Action(
            kind="mailbox",
            subject="[parity-audit] persistent UNMEASURABLE cells",
            body=_render_unmeasurable_body(cells, UNMEASURABLE_ESCALATE_RUNS),
        ))
    state["unmeasurable_alerted"] = sorted(alerted)

    state["last_run"] = _iso(now)
    return actions, state


# ---------------------------------------------------------------------------
# Message bodies
# ---------------------------------------------------------------------------

def _render_hard_body(hard_fails) -> str:
    lines = ["Fleet seat-parity audit found HARD-tier failures (seat state = BROKEN).",
             "These block the seat from a capability the ORG-099 spec treats as required.",
             ""]
    for seat, axis, verdict in hard_fails:
        lines.append(f"  BROKEN  {seat}  axis={axis}  verdict={verdict}")
    lines += ["",
              "Owner: orchestrator (axes.yaml/carve-outs.yaml + remediation dispatch). If a listed",
              "seat is owned by another persona, route the fix there. Full audit table at the",
              "report path in recovery's ~/parity-audit/ (latest-status.txt has the one-line summary)."]
    return "\n".join(lines)


def _render_carveout_body(pairs) -> str:
    lines = ["A ratified seat-parity CARVE-OUT's own verifier FAILED (CARVE_OUT_BROKEN).",
             "This is a spec-tier decision, not a seat bug: the carve-out may have expired,",
             "the seat may have drifted out from under it, or the spec may need amending.",
             ""]
    for seat, axis in pairs:
        lines.append(f"  CARVE_OUT_BROKEN  {seat}  axis={axis}")
    lines += ["", "Owner: orchestrator (carve-outs.yaml). See recovery's ~/parity-audit/ for the full table."]
    return "\n".join(lines)


def _render_rebaseline_body(seat, first_seen_iso, age_days) -> str:
    return (
        f"Seat '{seat}' has been PARTIAL (only SOFT-tier axes failing) continuously for "
        f"{age_days}d (first observed {first_seen_iso}), past the {SOFT_FAIL_REBASELINE_DAYS}d "
        f"rebaseline horizon.\n\n"
        f"## Ask\n"
        f"Decide the rebaseline for seat '{seat}': is the persistently-failing SOFT axis a spec "
        f"expectation that no longer applies to this seat (declare a carve-out or amend axes.yaml), "
        f"or a real seat-config drift to remediate? Recovery filed this automatically from the standing "
        f"parity audit; the current per-axis verdicts are in recovery's ~/parity-audit/ report.\n\n"
        f"## Context\n"
        f"Auto-filed by tools/parity_audit_runner.py (T-3750). SOFT-fail persistence is tracked "
        f"across the 6-hourly runs; this fires once per seat and is cleared when the seat returns "
        f"to FULL, so a later regression re-files."
    )


def _render_unmeasurable_body(cells, n) -> str:
    lines = [f"Seat-parity cells have read UNMEASURABLE for >= {n} consecutive runs — a check_ref "
             f"bug or a substrate-access problem, not a transient probe hiccup:",
             ""]
    for seat, axis in cells:
        lines.append(f"  UNMEASURABLE  {seat}  axis={axis}")
    lines += ["", "Owner: orchestrator (axes.yaml check_ref) or a recovery-side sudo/substrate access fix.",
              "See recovery's ~/parity-audit/ for the evidence strings."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Execution — the ONLY side-effecting layer. Sinks are injected so the whole
# decision path above stays testable without a real mailbox or a real ticket.
# ---------------------------------------------------------------------------

def _default_mailbox_sink(subject: str, body: str, severity: str) -> None:
    import mailbox as mb
    import agent_role_map as arm
    inbox = os.environ.get("PANTHEON_ORCHESTRATOR_INBOX", f"{arm.mailbox_hub()}/orchestrator")
    mb.send(inbox, body, sender="parity-audit", subject=subject, severity=severity)


def _default_ticket_sink(seat: str, body: str) -> None:
    # ask/context are split on the sentinel headings _render_rebaseline_body emits.
    ask, context = _split_ask_context(body)
    argv = [
        sys.executable, str(TICKET_BIN), "file",
        "--requester", "recovery", "--target", "role:engineering_orchestrator",
        "--title", f"Seat-parity REBASELINE — {seat} SOFT-drift persisting >{SOFT_FAIL_REBASELINE_DAYS}d",
        "--labels", "seat-parity,parity-audit,rebaseline",
        "--project-id", "null",
        "--nearest-intent-ref", "ticket:T-3747",
        "--serves", _REBASELINE_SERVES,
        "--appetite", "2",
        "--ask", ask,
        "--context", context,
    ]
    env = os.environ.copy() | {"PANTHEON_ACTOR": "recovery"}
    r = subprocess.run(argv, cwd=str(REPO_ROOT), capture_output=True, text=True, env=env)
    if r.returncode != 0:
        _log(f"rebaseline ticket file FAILED for {seat}: rc={r.returncode} "
             f"stderr={r.stderr.strip()[-400:]!r}")
        raise RuntimeError(f"ticket file failed for {seat}")


def _split_ask_context(body: str) -> tuple[str, str]:
    ask, context = "", ""
    if "## Ask" in body and "## Context" in body:
        _, rest = body.split("## Ask", 1)
        ask_part, context = rest.split("## Context", 1)
        ask = ask_part.strip()
        context = context.strip()
    else:
        ask = body.strip()
    return ask, context


@dataclass
class Sinks:
    mailbox: object = _default_mailbox_sink   # (subject, body, severity) -> None
    file_ticket: object = _default_ticket_sink  # (seat, body) -> None


def execute_actions(actions: list[Action], sinks: Sinks) -> list[str]:
    """Fire each action through its sink. Fail-open per action — one failed
    escalation must not swallow the others or crash the runner. Returns the
    list of failure descriptions (empty on full success)."""
    failures: list[str] = []
    for a in actions:
        try:
            if a.kind == "mailbox":
                sinks.mailbox(a.subject, a.body, a.severity)
            elif a.kind == "ticket":
                sinks.file_ticket(a.subject, a.body)
            else:
                failures.append(f"unknown action kind {a.kind!r}")
        except Exception as exc:  # noqa: BLE001 — fail-open is the contract
            failures.append(f"{a.kind}/{a.subject}: {exc!r}")
            _log(f"action FAILED ({a.kind} {a.subject!r}): {exc!r}")
    return failures


# ---------------------------------------------------------------------------
# Report + state persistence
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if not path.exists():
        return _blank_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else _blank_state()
    except (json.JSONDecodeError, OSError):
        return _blank_state()


def save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        _log(f"state save FAILED: {exc!r}")


def _status_line(findings: Findings, now: datetime, held: tuple[str, ...] = ()) -> str:
    counts = {"FULL": 0, "PARTIAL": 0, "BROKEN": 0}
    for st in findings.seat_state.values():
        counts[st] = counts.get(st, 0) + 1
    broken = [s for s, st in sorted(findings.seat_state.items()) if st == "BROKEN"]
    partial = [s for s, st in sorted(findings.seat_state.items()) if st == "PARTIAL"]
    fail_broken = [s for s in broken if s in findings.fail_broken_seats]
    unmeas_broken = [s for s in broken if s not in findings.fail_broken_seats]
    tail = ""
    if broken:
        # Distinguish real FAIL-driven breaks from UNMEASURABLE-only breaks: a seat
        # that is BROKEN only because an axis could not be measured is a very different
        # signal from one that FAILed a definite check, and folding them into one scary
        # "N BROKEN" count is what makes an unmeasurable baseline read as a fleet crisis.
        if fail_broken:
            tail += f" BROKEN-FAIL={','.join(fail_broken)}"
        if unmeas_broken:
            tail += f" BROKEN-UNMEASURABLE={','.join(unmeas_broken)}"
    if partial:
        tail += f" PARTIAL={','.join(partial)}"
    if held:
        tail += f" held={','.join(sorted(held))}"
    stamp = now.strftime("%Y-%m-%dT%H:%MZ")
    return (f"{stamp} fleet-parity: {counts['FULL']} FULL, "
            f"{counts['PARTIAL']} PARTIAL, {counts['BROKEN']} BROKEN{tail}")


def write_reports(report_dir: Path, reports: list[pr.SeatReport], axes: list[pr.Axis],
                  findings: Findings, now: datetime, held: tuple[str, ...] = ()) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d-%H")
    md_path = report_dir / f"{stamp}.md"
    json_path = report_dir / f"{stamp}.json"
    md_path.write_text(pr.render_fleet_table_md(reports, axes), encoding="utf-8")
    json_path.write_text(pr.render_fleet_table_json(reports), encoding="utf-8")
    (report_dir / LATEST_STATUS_BASENAME).write_text(_status_line(findings, now, held) + "\n",
                                                      encoding="utf-8")
    _prune_reports(report_dir)
    return md_path


def _prune_reports(report_dir: Path) -> None:
    """Keep the report dir bounded — most-recent REPORT_RETENTION of each suffix."""
    for suffix in (".md", ".json"):
        files = sorted(report_dir.glob(f"*{suffix}"), key=lambda p: p.name, reverse=True)
        for stale in files[REPORT_RETENTION:]:
            try:
                stale.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def seed_baseline_state(findings: Findings, now: datetime) -> dict:
    """Build a state file that treats the CURRENT audit result as already-acknowledged
    baseline, so the very first scheduled run escalates nothing and the timer only pages
    on drift BEYOND today. Used once at arming time when the landed axes.yaml has a dirty
    baseline (unmeasurable-by-construction axes, a retiring seat) that orchestrator is handling
    out of band — we hand orchestrator the baseline with context rather than auto-firing it."""
    state = _blank_state()
    state["alerted_hard"] = _signature(findings.hard_fails)
    state["alerted_carveout"] = _signature(findings.carve_outs_broken)
    # Pre-age every current UNMEASURABLE cell to the escalation threshold AND mark it
    # alerted, so only NEW unmeasurable cells trip the N-run path.
    streak, alerted = {}, []
    for seat, axis in findings.unmeasurable_cells:
        key = _cell_key(seat, axis)
        streak[key] = UNMEASURABLE_ESCALATE_RUNS
        alerted.append(key)
    state["unmeasurable_streak"] = streak
    state["unmeasurable_alerted"] = sorted(alerted)
    # Start each PARTIAL seat's 30d rebaseline clock now (not retroactively).
    state["soft_fail_first_seen"] = {s: _iso(now) for s in findings.partial_seats}
    state["last_run"] = _iso(now)
    return state


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    ap.add_argument("--dry-run", action="store_true",
                    help="run the audit + write reports, but never mailbox, file a ticket, "
                         "or persist state — escalations are printed instead")
    ap.add_argument("--seed-baseline", action="store_true",
                    help="run the audit, write reports, and persist a state file that marks "
                         "TODAY's result as already-acknowledged (no escalation) so the timer "
                         "pages only on future drift. Run once at arming time.")
    args = ap.parse_args(argv)

    now = _now()
    reports, axes = run_fleet_audit(execute=True)
    raw_findings = classify(reports)
    # T-5254: report the table unfiltered (honest), page/seed/dedupe on the filtered view.
    held = held_seats(raw_findings)
    findings = exclude_held(raw_findings)

    md_path = write_reports(args.report_dir, reports, axes, raw_findings, now, held)
    _log(_status_line(raw_findings, now, held) + f"  report={md_path}")

    state_path = args.report_dir / STATE_BASENAME

    if args.seed_baseline:
        seeded = seed_baseline_state(findings, now)
        save_state(state_path, seeded)
        _log(f"seeded baseline state -> {state_path} "
             f"(hard_fails={len(findings.hard_fails)}, carve_broken={len(findings.carve_outs_broken)}, "
             f"unmeasurable={len(findings.unmeasurable_cells)}, partial={len(findings.partial_seats)}); "
             f"no escalation sent")
        return 0

    prior = load_state(state_path)
    actions, new_state = decide(findings, prior, now)

    if args.dry_run:
        for a in actions:
            _log(f"WOULD {a.kind} [{a.severity}] {a.subject}")
        _log(f"(dry-run: {len(actions)} escalation(s); state NOT persisted)")
        return 0

    failures = execute_actions(actions, Sinks())
    # Persist state even if some escalations failed: the dedupe keys reflect what
    # we DECIDED to alert on this run. A failed mailbox is logged + surfaced via
    # the unit's OnFailure only for a genuine crash, not per-action — re-alerting
    # on the next run would just re-hit the same failing sink. (Escalations that
    # never fired because the sink threw are still visible in the report + status
    # line; a persistent sink outage is itself a fleet signal recovery would notice.)
    save_state(state_path, new_state)
    if actions:
        _log(f"{len(actions)} escalation(s) dispatched, {len(failures)} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
