#!/usr/bin/env python3
"""tools/scheduler_reconcile.py — ORG-064 General Scheduler, phase 1: render the
`schedules/` spec to concrete systemd unit text, diff against installed state,
report a PLAN (T-2896).

ZERO PRIVILEGE, NO APPLY VERB. This module reads the spec (`scheduler_spec.py`),
renders what each entry's unit pair *would* look like, and compares that against
whatever installed state it can read. It never writes a unit file, never calls
`systemctl`, and touches no live systemd state anywhere — "plan", never "apply".
Apply/migration is T-2662, gated on the Action Broker (ADR ORG-064 §2c).

RENDERING. Two payload shapes (`scheduler_spec.UnitPayload`):
  - `template` — the `.service` file under `tools/systemd/<template>.service` is
    read VERBATIM. The spec does not re-author service/exec content; it owns
    scheduling. The `.timer` half is always synthesized fresh from the entry's own
    `calendar`/`persistent` fields (never read from the existing `.timer` file) —
    that is the one thing this phase's spec is authoritative for, and reading the
    old `.timer` as well would just reintroduce a second, possibly-conflicting
    source of calendar truth.
  - inline — both `.service` and `.timer` are synthesized from the entry's fields,
    for a net-new schedule with no hand-authored template yet.
Every render is scope-invariant across personas (no entry in this phase's seed data
uses `%i` template-instantiation — see scheduler_spec.py's KNOWN GAP note on the
monotonic per-persona-runtime cluster this phase does not model).

INSTALLED-STATE READS, DEGRADE NEVER CRASH. Host scope reads `/etc/systemd/system`
directly (world-readable, no privilege needed — verified on this box). Per-persona
scope needs the OWNING ACCOUNT's `~/.config/systemd/user`: this account's own is a
plain local read; every OTHER account is read via `systemd_drift_sweep.Ops` (T-2444's
established `sudo -n -u <account>` best-effort primitive — reused here rather than
re-implemented, same DRY posture that module already applies to `systemd_drift_lint`).
NOPASSWD cross-account sudo is Recovery's grant, not every seat's — so a persona
reconciled from a seat without it reads UNVERIFIED for that persona, not an error and
not a false CLEAN. See `_read_account_dir` / `_read_host_dir`.

DIFF CLASSIFICATION reuses `systemd_drift_lint`'s semantic INI comparison AND its
severity split (`_semantic_lines`/`_describe`/`_classify_drift`, and by extension
`_INERT_DIRECTIVES`) rather than a second comparator — the same reuse precedent
`systemd_drift_sweep.py` already established for this sibling module's private
underscore-prefixed helpers. The split is NOT optional here, specifically because
of how the `.timer` half is rendered: the `[Service]` half of a template-payload
entry is byte-identical to the tracked template by construction, but the `[Timer]`
half's `Description=` is ALWAYS freshly synthesized (see `render_timer` below) and
will never match a hand-authored `.timer`'s own Description/Documentation text — a
binary MATCH/DRIFT would report every correctly-in-spec unit as DRIFT forever.
(T-2896 REBOUND round 1: a first cut collapsed to binary MATCH/DRIFT, reusing
`_semantic_lines`/`_describe` but stopping one function short of
`_classify_drift` — the very first real wet-run showed exactly this, 4/4
false-positive substantive DRIFT, every one of them Description/Documentation-only
on the `.timer`. Fixed by calling `_classify_drift` on every non-equal pair.)

STATES: MATCH (installed == rendered), DRIFT (installed differs on an operative
directive — an apply would change behavior), DRIFT-COSMETIC (installed differs
ONLY on inert metadata, i.e. `_INERT_DIRECTIVES` — Description=/Documentation= —
an apply would still rewrite bytes, just not change behavior; reported as its own
state rather than folded into MATCH, because "nothing would notice at runtime" is
a different claim than "matches", and phase 2's apply legitimately needs to know a
rewrite is coming), CREATE (spec-owned, not installed — nothing renders it yet),
ORPHAN (installed, not spec-owned for that account — FLAGGED per ADR §2a, never
touched), UNVERIFIED (the account/host could not be read at all).

Usage:
    python3 -m tools.scheduler_reconcile plan [--json] [--account PERSONA ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_role_map as arm  # noqa: E402
import scheduler_spec as sspec  # noqa: E402
import systemd_drift_lint as sdl  # noqa: E402
import systemd_drift_sweep as dsweep  # noqa: E402

HOST_UNIT_DIR = Path("/etc/systemd/system")
HOST_LABEL = "host"

STATUS_MATCH = "MATCH"
STATUS_DRIFT = "DRIFT"
STATUS_DRIFT_COSMETIC = "DRIFT-COSMETIC"
STATUS_CREATE = "CREATE"
STATUS_ORPHAN = "ORPHAN"
STATUS_UNVERIFIED = "UNVERIFIED"

_UNIT_SUFFIXES = (".service", ".timer")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_service(entry: sspec.Entry, template_dir: Path = sspec.UNIT_TEMPLATE_DIR) -> str:
    """The `.service` text this entry would render — verbatim template passthrough,
    or a synthesized inline unit."""
    unit = entry.unit
    if unit.is_template:
        return (template_dir / f"{unit.template}.service").read_text(encoding="utf-8")

    lines = ["[Unit]", f"Description={unit.description}"]
    for a in unit.after:
        lines.append(f"After={a}")
    for w in unit.wants:
        lines.append(f"Wants={w}")
    if entry.on_failure == sspec.ON_FAILURE_STANDARD:
        lines.append("OnFailure=pantheon-unit-failure-alert@%n.service")
    lines += ["", "[Service]", f"Type={unit.unit_type}"]
    if unit.working_directory:
        lines.append(f"WorkingDirectory={unit.working_directory}")
    for e in unit.environment:
        lines.append(f"Environment={e}")
    lines.append(f"ExecStart={unit.exec_start}")
    lines += ["StandardOutput=journal", "StandardError=journal"]
    if unit.timeout_start_sec:
        lines.append(f"TimeoutStartSec={unit.timeout_start_sec}")
    return "\n".join(lines) + "\n"


def render_timer(entry: sspec.Entry, calendar: str, persistent: bool) -> str:
    """The `.timer` text — ALWAYS synthesized from the spec's own calendar/persistent,
    even for a template-payload entry (see module docstring: spec owns scheduling)."""
    lines = ["[Unit]", f"Description=Pantheon schedule: {entry.name} — {entry.serves}", ""]
    lines += ["[Timer]", f"OnCalendar={calendar}"]
    if persistent:
        lines.append("Persistent=true")
    lines += [f"Unit={entry.name}.service", "", "[Install]", "WantedBy=timers.target"]
    return "\n".join(lines) + "\n"


def render_pair(entry: sspec.Entry, calendar: str, persistent: bool,
                 template_dir: Path = sspec.UNIT_TEMPLATE_DIR) -> tuple[str, str]:
    return render_service(entry, template_dir), render_timer(entry, calendar, persistent)


# ---------------------------------------------------------------------------
# Installed-state reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstalledDir:
    """A read (or attempted read) of one directory's unit files. `files is None`
    means UNREADABLE (the three-outcome distinction from systemd_drift_lint's own
    InstalledSource: unreachable / reachable-but-absent / reachable-with-content
    must never collapse into each other — an unreadable account must not report as
    a clean empty one)."""

    label: str
    reachable: bool
    files: dict[str, str] | None
    reason: str = ""

    def has(self, name: str) -> bool:
        return bool(self.files) and name in self.files

    def read(self, name: str) -> str | None:
        return (self.files or {}).get(name)


def _read_host_dir(host_dir: Path = HOST_UNIT_DIR) -> InstalledDir:
    if not host_dir.is_dir():
        return InstalledDir(label=str(host_dir), reachable=True, files={})
    files: dict[str, str] = {}
    try:
        for p in sorted(host_dir.iterdir()):
            if p.is_file() and p.name.endswith(_UNIT_SUFFIXES):
                try:
                    files[p.name] = p.read_text(encoding="utf-8")
                except OSError:
                    continue  # single unreadable file: absent from files, never a false MATCH
    except OSError as exc:
        return InstalledDir(label=str(host_dir), reachable=False, files=None, reason=str(exc))
    return InstalledDir(label=str(host_dir), reachable=True, files=files)


def _read_account_dir(account: str, ops: dsweep.Ops | None = None) -> InstalledDir:
    ops = ops or dsweep.Ops()
    reason, files = ops.read_installed_dir(account)
    label = f"{account}:~/.config/systemd/user"
    if files is None:
        return InstalledDir(label=label, reachable=False, files=None, reason=reason)
    return InstalledDir(label=label, reachable=True, files=files, reason=reason)


def _classify(rendered_text: str, unit_name: str, installed: InstalledDir) -> tuple[str, str, str]:
    """Return (status, detail, severity). `severity` is only meaningful for the two
    DRIFT statuses ("substantive" | "cosmetic", `systemd_drift_lint._classify_drift`'s
    own vocabulary) — empty for every other status."""
    if not installed.reachable:
        return STATUS_UNVERIFIED, installed.reason or f"{installed.label} unreadable", ""
    if not installed.has(unit_name):
        return STATUS_CREATE, "not installed", ""
    installed_text = installed.read(unit_name)
    if installed_text is None:  # listed but this one file failed to read
        return STATUS_UNVERIFIED, "listed but unreadable", ""
    a, b = sdl._semantic_lines(rendered_text), sdl._semantic_lines(installed_text)
    if a == b:
        return STATUS_MATCH, "", ""
    severity = sdl._classify_drift(a, b, unit_name)
    status = STATUS_DRIFT_COSMETIC if severity == "cosmetic" else STATUS_DRIFT
    return status, sdl._describe(a, b), severity


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanItem:
    unit: str          # entry name, e.g. "pantheon-watch"
    suffix: str         # "service" | "timer"
    target: str          # "host" or "<persona>@<account>"
    status: str
    detail: str = ""
    severity: str = ""  # "substantive" | "cosmetic" | "" (non-DRIFT statuses)

    def to_dict(self) -> dict:
        return {"unit": self.unit, "suffix": self.suffix, "target": self.target,
                "status": self.status, "detail": self.detail, "severity": self.severity}


@dataclass(frozen=True)
class PlanResult:
    items: list[PlanItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.items:
            out[item.status] = out.get(item.status, 0) + 1
        return out


def _account_for_persona(persona: str) -> str:
    binding = arm.instance_binding(persona)
    return binding.get("account") or persona


def build_plan(
    spec: sspec.Spec,
    accounts: list[str] | None = None,
    *,
    ops: dsweep.Ops | None = None,
    template_dir: Path = sspec.UNIT_TEMPLATE_DIR,
    host_dir: Path = HOST_UNIT_DIR,
) -> PlanResult:
    """Render every spec-targeted (entry, persona) pair, diff it, and flag orphans.

    `accounts`, when given, limits which persona TARGETS are rendered+compared (host
    is included unless explicitly excluded via accounts=[...] without "host") — it
    does not limit which installed directories get read for orphan detection beyond
    what's needed for the targets actually rendered.
    """
    ops = ops or dsweep.Ops()
    accounts_filter = set(accounts) if accounts else None

    host_src = _read_host_dir(host_dir)
    account_src_cache: dict[str, InstalledDir] = {}

    # INVARIANT (T-2896 REBOUND r1, reviewer nice-to-have — legibility, not a
    # defect): an account is only ever READ if some entry actually TARGETS it —
    # a per-persona entry naming that persona, or an all-persona entry the
    # persona hasn't opted out of. A spec with zero all-persona entries, and no
    # per-persona entry for some persona, never queries that persona's account
    # at all — so the orphan sweep below is only as complete as the spec's own
    # coverage of the fleet, which today is 5 of the T-2662 census's 51 timers.
    # This narrows silently (no warning fires) because it is the correct
    # behavior, not a gap to flag — but it is worth stating plainly so a future
    # reader doesn't mistake a quiet account for a verified-clean one.
    def account_src_for(persona: str) -> tuple[str, InstalledDir]:
        account = _account_for_persona(persona)
        if account not in account_src_cache:
            account_src_cache[account] = _read_account_dir(account, ops=ops)
        return account, account_src_cache[account]

    items: list[PlanItem] = []
    claimed: dict[str, set[str]] = {HOST_LABEL: set()}

    for entry in spec.entries.values():
        if entry.scope == sspec.SCOPE_HOST:
            if accounts_filter is not None and HOST_LABEL not in accounts_filter:
                continue
            svc, tmr = render_pair(entry, entry.calendar, entry.persistent, template_dir)
            claimed[HOST_LABEL].update({f"{entry.name}.service", f"{entry.name}.timer"})
            items.append(_plan_item(entry.name, "service", HOST_LABEL, svc, host_src))
            items.append(_plan_item(entry.name, "timer", HOST_LABEL, tmr, host_src))
            continue

        for eff in sspec.targets(spec, entry):
            if accounts_filter is not None and eff.persona not in accounts_filter:
                continue
            account, src = account_src_for(eff.persona)
            svc, tmr = render_pair(entry, eff.calendar, eff.persistent, template_dir)
            claimed.setdefault(account, set()).update({f"{entry.name}.service", f"{entry.name}.timer"})
            label = f"{eff.persona}@{account}"
            items.append(_plan_item(entry.name, "service", label, svc, src))
            items.append(_plan_item(entry.name, "timer", label, tmr, src))

    items.extend(_orphans(HOST_LABEL, host_src, claimed[HOST_LABEL]))
    for account, src in account_src_cache.items():
        items.extend(_orphans(account, src, claimed.get(account, set())))

    unreachable = [s.label for s in [host_src, *account_src_cache.values()] if not s.reachable]
    return PlanResult(items=items, warnings=list(spec.warnings), unreachable=unreachable)


def _plan_item(name: str, suffix: str, target: str, rendered_text: str, src: InstalledDir) -> PlanItem:
    unit_name = f"{name}.{suffix}"
    status, detail, severity = _classify(rendered_text, unit_name, src)
    return PlanItem(unit=name, suffix=suffix, target=target, status=status, detail=detail, severity=severity)


def _orphans(target: str, src: InstalledDir, claimed: set[str]) -> list[PlanItem]:
    if not src.reachable or not src.files:
        return []
    out = []
    for name in sorted(set(src.files) - claimed):
        suffix = name.rsplit(".", 1)[1]
        unit = name.rsplit(".", 1)[0]
        out.append(PlanItem(unit=unit, suffix=suffix, target=target, status=STATUS_ORPHAN,
                             detail="installed, not spec-owned for this account"))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_result(result: PlanResult) -> None:
    counts = result.counts()
    print("=== schedules/ plan (report-only — no apply) ===")
    print("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) if counts else "counts: (nothing targeted)")
    if result.warnings:
        print(f"\n{len(result.warnings)} spec warning(s):")
        for w in result.warnings:
            print(f"  ! {w}")
    if result.unreachable:
        print(f"\n{len(result.unreachable)} source(s) unreadable (UNVERIFIED, not CLEAN):")
        for label in result.unreachable:
            print(f"  ? {label}")
    # DRIFT gets full detail — it is the thing to look at. DRIFT-COSMETIC gets one
    # compact line each (no detail dump): a Description=/Documentation= mismatch is
    # not a functional divergence and printing its full section diff would just be
    # noise crowding out the substantive findings above it (the T-2179 lesson this
    # module's docstring already cites, applied to the printer, not just the
    # comparator).
    rows = [i for i in result.items if i.status == STATUS_DRIFT]
    if rows:
        print(f"\n{STATUS_DRIFT} ({len(rows)}):")
        for item in rows:
            print(f"  {item.target}: {item.unit}.{item.suffix} — {item.detail}")

    rows = [i for i in result.items if i.status == STATUS_DRIFT_COSMETIC]
    if rows:
        print(f"\n{STATUS_DRIFT_COSMETIC} ({len(rows)}) — metadata-only "
              f"(Description=/Documentation=), not a functional divergence:")
        for item in rows:
            print(f"  {item.target}: {item.unit}.{item.suffix}")

    for status in (STATUS_CREATE, STATUS_ORPHAN, STATUS_MATCH, STATUS_UNVERIFIED):
        rows = [i for i in result.items if i.status == status]
        if not rows:
            continue
        print(f"\n{status} ({len(rows)}):")
        for item in rows:
            detail = f" — {item.detail}" if item.detail else ""
            print(f"  {item.target}: {item.unit}.{item.suffix}{detail}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[2])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="render + diff the schedules/ spec against installed state (read-only)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--account", action="append",
                    help="limit to these targets (persona names, or 'host'); repeatable; default: everything")
    p.add_argument("--schedules-dir", type=Path, default=sspec.SCHEDULES_DIR)
    p.add_argument("--template-dir", type=Path, default=sspec.UNIT_TEMPLATE_DIR,
                    help="override for testing; defaults to the real tools/systemd/")
    args = ap.parse_args(argv)

    try:
        spec = sspec.load_spec(args.schedules_dir, template_dir=args.template_dir)
    except sspec.SpecError as exc:
        print(f"SPEC ERROR: {exc}", file=sys.stderr)
        return 2

    result = build_plan(spec, accounts=args.account, template_dir=args.template_dir)
    if args.json:
        print(json.dumps({
            "items": [i.to_dict() for i in result.items],
            "warnings": result.warnings,
            "unreachable": result.unreachable,
            "counts": result.counts(),
        }, indent=2))
    else:
        _print_result(result)
    return 0  # report-only tool; the plan IS the output, never a pass/fail signal


if __name__ == "__main__":
    sys.exit(main())
