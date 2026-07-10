#!/usr/bin/env python3
"""auto_commit_contract_lint — structural enforcement of the auto-commit-cron default-safe contract.

Enforces `sop/standard/auto-commit-cron-default-safe-contract.md` (Ratified
2026-06-10, T-0825 / T-1072). The contract: an auto-commit *cron* script must
(a) refuse with exit 64 when invoked with no mode flag (the no-prod-default
invariant), and (b) have its systemd unit ExecStart pass an explicit mode flag
(canonically `--commit-prod`), never a bare invocation.

This linter is itself **report-only / advisory**: it authors no commits and
runs no candidate scripts (no `git add`, no subprocess of the linted tools). It
therefore sits OUTSIDE the contract it enforces — same exemption class as
`pantheon_config_sync.py` (no git add → no unreviewed-commit risk). It checks
the REPO-committed scripts (`tools/*.py`) and unit files (`tools/systemd/*.service`)
statically; live installed-unit drift is out of scope (owned by re-provision +
verify_startup).

Scope (a script is IN SCOPE iff all three hold, computed structurally — NOT an
allowlist, so a new cron can't silently escape enforcement):
  1. authors a commit in its OWN body  — a direct `_git("add", ...)` call
  2. pushes it                          — `push_with_retry(...)` or `_git("push"...)`
  3. is wired to a systemd unit ExecStart
Excluded by construction: push-only tools (no git add — e.g. pantheon_config_sync,
kino_pwa_autodeploy), delegators that commit via `ticket.py file` (no direct add
in their own body — knowledge_sweep, soak_filer), and interactive/one-off tools
(never named in a unit ExecStart — ticket.py, migrate_*).

Severity:
  VIOLATION — in scope AND a unit ExecStart is bare (no mode flag). The cron is
              wired to do the real commit+push with no explicit authorization.
  ADVISORY  — in scope, unit passes a mode flag, BUT the script lacks an explicit
              default-refuse gate. The cron path is fine, but a bare *manual* run
              isn't guarded; the no-prod-default invariant prefers an explicit
              exit-64 refuse.

Exit codes: 0 = no VIOLATION (advisory findings don't fail). 1 = a VIOLATION
present. `--strict` also fails on ADVISORY. (So a future pre-push hook can gate.)

Usage:
    python3 tools/auto_commit_contract_lint.py            # human report
    python3 tools/auto_commit_contract_lint.py --json     # machine-readable
    python3 tools/auto_commit_contract_lint.py --strict   # ADVISORY also non-zero

Convention dependency: detection keys on the in-house `_git(...)` /
`push_with_retry(...)` helpers (uniform across the fleet's auto-commit scripts).
A tool that shells git via a dynamic argv would evade the static scan — accepted
for an advisory lint; do not over-engineer taint analysis.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS = _REPO_ROOT / "tools"
_SYSTEMD = _TOOLS / "systemd"

# Whitelist entries carry their structural INVARIANT. The linter re-validates the
# stated reason still holds (anti-rot): a whitelist that only suppresses can hide
# a real regression; one that checks its own justification cannot. If a listed
# script ever starts authoring commits, it gets a LOUD VIOLATION, not silence.
KNOWN_EXCEPTIONS = {
    "pantheon_config_sync.py": (
        "authors no commits — git push of already-committed local-ahead state "
        "+ git pull --ff-only only; no git add (SOP Boundary; T-0471)"
    ),
}

# This linter itself: read-only, no git add — documented exempt for self-consistency.
_SELF = "auto_commit_contract_lint.py"

_ADD_RE = re.compile(r'_git\(\s*["\']add["\']')
_PUSH_RE = re.compile(r'push_with_retry\(|_git\(\s*["\']push["\']')
_DEFAULT_REFUSE_RE = re.compile(r'_enforce_default_safe|return\s+64\b|exit\(\s*64\b|EX_USAGE')
_EXECSTART_SCRIPT_RE = re.compile(r'tools/(\w+\.py)')
_MODE_FLAG_RE = re.compile(r'--[A-Za-z]')


class Severity(Enum):
    VIOLATION = "VIOLATION"
    ADVISORY = "ADVISORY"


@dataclass
class Finding:
    script: str
    severity: Severity
    reasons: list
    units: list  # unit filenames implicated

    def to_dict(self) -> dict:
        return {
            "script": self.script,
            "severity": self.severity.value,
            "reasons": self.reasons,
            "units": self.units,
        }


def _read(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


def parse_execstarts(systemd_dir: Path) -> dict:
    """map script basename -> list of (unit_filename, has_mode_flag)."""
    out: dict = {}
    for svc in sorted(systemd_dir.glob("*.service")):
        for raw in _read(svc).splitlines():
            line = raw.strip()
            if not line.startswith("ExecStart="):
                continue
            m = _EXECSTART_SCRIPT_RE.search(line)
            if not m:
                continue
            name = m.group(1)
            after = line[m.end():]
            has_flag = bool(_MODE_FLAG_RE.search(after))
            out.setdefault(name, []).append((svc.name, has_flag))
    return out


def lint(tools_dir: Path = _TOOLS, systemd_dir: Path = _SYSTEMD) -> list:
    """Return the list of Findings over the repo. Pure / read-only."""
    execstarts = parse_execstarts(systemd_dir)
    findings: list = []

    for py in sorted(tools_dir.glob("*.py")):
        name = py.name
        if name == _SELF:
            continue
        src = _read(py)
        authors = bool(_ADD_RE.search(src))

        # Whitelist invariant re-validation (anti-rot).
        if name in KNOWN_EXCEPTIONS:
            if authors:
                findings.append(Finding(
                    name, Severity.VIOLATION,
                    [f"whitelisted exception no longer satisfies its exemption rationale "
                     f"('{KNOWN_EXCEPTIONS[name]}') — it now authors commits (git add). "
                     f"Re-review the exemption."],
                    execstarts.get(name, []) and [u for u, _ in execstarts[name]] or [],
                ))
            continue

        if not authors:
            continue  # push-only / non-committing -> out of scope (structural exclusion)
        if not _PUSH_RE.search(src):
            continue  # commits but never pushes a shared ref -> not the contract's risk
        units = execstarts.get(name)
        if not units:
            continue  # not wired to a systemd unit -> interactive / one-off, not a cron

        unit_names = [u for u, _ in units]
        bare_units = [u for u, has_flag in units if not has_flag]
        has_refuse = bool(_DEFAULT_REFUSE_RE.search(src))

        reasons: list = []
        severity = None
        if bare_units:
            severity = Severity.VIOLATION
            reasons.append(
                "systemd unit ExecStart is bare (no mode flag): "
                f"{', '.join(bare_units)} — the cron does the real commit+push with no "
                "explicit authorization; must pass --commit-prod (or an explicit mode flag)"
            )
            if not has_refuse:
                reasons.append(
                    "and the script has no default-refuse gate (_enforce_default_safe / "
                    "exit 64) — a bare invocation is wholly unguarded"
                )
        elif not has_refuse:
            severity = Severity.ADVISORY
            reasons.append(
                "no explicit default-refuse gate (_enforce_default_safe / exit 64): the "
                "cron path is flagged correctly, but a bare manual run isn't guarded by the "
                "no-prod-default invariant — prefer an explicit exit-64 refuse"
            )

        if severity is not None:
            findings.append(Finding(name, severity, reasons, unit_names))

    return findings


def _format_report(findings: list) -> str:
    viols = [f for f in findings if f.severity is Severity.VIOLATION]
    advs = [f for f in findings if f.severity is Severity.ADVISORY]
    lines = ["auto-commit-cron contract lint — enforces sop/standard/auto-commit-cron-default-safe-contract.md"]
    lines.append(f"  {len(viols)} VIOLATION(s), {len(advs)} ADVISORY")
    for f in viols + advs:
        lines.append("")
        lines.append(f"  [{f.severity.value}] {f.script}  (units: {', '.join(f.units) or 'n/a'})")
        for r in f.reasons:
            lines.append(f"      - {r}")
    if not findings:
        lines.append("  all in-scope auto-commit crons conform.")
    return "\n".join(lines)


def exit_code(findings: list, strict: bool = False) -> int:
    """0 unless a VIOLATION is present (or, with strict, any ADVISORY)."""
    has_violation = any(f.severity is Severity.VIOLATION for f in findings)
    has_advisory = any(f.severity is Severity.ADVISORY for f in findings)
    return 1 if (has_violation or (strict and has_advisory)) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Lint the auto-commit-cron default-safe contract (report-only).")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true", help="exit non-zero on ADVISORY findings too")
    args = ap.parse_args(argv)

    findings = lint()
    if args.json:
        print(json.dumps([f.to_dict() for f in findings], indent=2))
    else:
        print(_format_report(findings))

    return exit_code(findings, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
