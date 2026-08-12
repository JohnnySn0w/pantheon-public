#!/usr/bin/env python3
"""main_guard_lint — catch top-level definitions that sit AFTER a `__main__` guard.

Closes the detection gap behind T-2170 (generalizing orchestrator's 90e7c9d69, which fixed
the first instance found). A `if __name__ == "__main__":` block placed mid-file means
every top-level `class`/`def` below it is defined only on *import*, never when the
file is executed directly — so:

    python3 tools/test_worker_guard.py        -> Ran 67 tests ... OK
    python3 -m unittest tools.test_worker_guard -> Ran 76 tests ... OK

Both say OK. Only the COUNT differs, and nothing announces the gap. Three files
carried it (50 tests), and the skipped classes were disproportionately drift-guards
and incident-regressions — a tripwire a direct-form run does not arm. It already
cost two agents a wrong diagnosis on T-2157. This is a measurement-integrity defect:
the same tree reports two different greens depending on how you invoke it.

Report-only / read-only: parses committed sources with `ast`, authors nothing, and
never imports or executes the linted files (same exemption class as the sibling
lints — see `tempfile_hygiene_lint.py`).

Scope (a definition is FLAGGED iff all hold — structural, NOT an allowlist, so a new
occurrence can't silently escape):
  1. the module has a top-level `if __name__ == "__main__":` block, and
  2. a top-level `ClassDef` / `FunctionDef` / `AsyncFunctionDef` starts at a line
     AFTER that block begins.

Only top-level definitions count: a def nested inside the guard body, inside a class,
or inside another function is unaffected by the guard's position. The whole `tools/`
tree is in scope, not just `test_*.py` — the failure mode (dead code under direct
execution) is not test-specific, and the canonical `main()` shape puts `sys.exit(main())`
in the guard, so anything below it is unreachable when run as a script.

Exit codes: 0 = clean. 1 = a finding. `--strict` is accepted for parity with the
sibling lints (this lint emits only VIOLATIONs, so --strict is a no-op). A companion
test asserts the live `tools/` tree lints clean — that test, run in the suite, is what
actually gates (there is no CI).

Usage:
    python3 tools/main_guard_lint.py            # human report
    python3 tools/main_guard_lint.py --json     # machine-readable
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS = _REPO_ROOT / "tools"

# Documented exceptions: key "filename::DefName" -> structural rationale. Re-stated
# here so a suppression carries its own justification (anti-rot). Empty today; add an
# entry only with a real reason a definition below the guard is actually correct.
KNOWN_EXCEPTIONS: dict[str, str] = {}


@dataclass
class Finding:
    file: str
    name: str
    kind: str
    lineno: int
    guard_lineno: int

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "name": self.name,
            "kind": self.kind,
            "lineno": self.lineno,
            "guard_lineno": self.guard_lineno,
        }


def _read(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


def _is_main_guard(node: ast.AST) -> bool:
    """True if `node` is a top-level `if __name__ == "__main__":`.

    Matches the comparison structurally rather than by source text, so `'__main__'`,
    `"__main__"` and a reversed `"__main__" == __name__` all count.
    """
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    sides = [test.left, test.comparators[0]]
    has_name = any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides)
    has_main = any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides)
    return has_name and has_main


def lint_source(src: str, filename: str) -> list[Finding]:
    """Lint one source string. Pure; returns Findings for post-guard top-level defs."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []  # unparseable -> not our job

    guard_line = None
    for node in tree.body:
        if _is_main_guard(node):
            guard_line = node.lineno
            break
    if guard_line is None:
        return []

    kinds = {ast.ClassDef: "class", ast.FunctionDef: "def", ast.AsyncFunctionDef: "async def"}
    findings: list[Finding] = []
    for node in tree.body:
        kind = kinds.get(type(node))
        if kind is None or node.lineno <= guard_line:
            continue
        if f"{filename}::{node.name}" in KNOWN_EXCEPTIONS:
            continue
        findings.append(Finding(filename, node.name, kind, node.lineno, guard_line))
    return findings


def lint(tools_dir: Path = _TOOLS) -> list[Finding]:
    """Return Findings over all tools/*.py. Read-only."""
    findings: list[Finding] = []
    for py in sorted(tools_dir.glob("*.py")):
        findings.extend(lint_source(_read(py), py.name))
    return findings


def _format_report(findings: list[Finding]) -> str:
    lines = ["main-guard lint — top-level definitions below a `__main__` guard"]
    lines.append(f"  {len(findings)} finding(s)")
    for f in findings:
        lines.append("")
        lines.append(f"  [DEAD-ON-DIRECT-RUN] {f.file}::{f.name}  line {f.lineno}  ({f.kind})")
        lines.append(f"      - the `__main__` guard opens at line {f.guard_lineno}; this "
                     "definition never exists when the file is run directly")
    if not findings:
        lines.append("  every `__main__` guard is the last top-level statement.")
    return "\n".join(lines)


def exit_code(findings: list[Finding], strict: bool = False) -> int:
    return 1 if findings else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Lint tools/*.py for top-level definitions placed after a __main__ guard (report-only)."
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true", help="parity flag (no-op: only VIOLATIONs emitted)")
    args = ap.parse_args(argv)

    findings = lint()
    if args.json:
        print(json.dumps([f.to_dict() for f in findings], indent=2))
    else:
        print(_format_report(findings))
    return exit_code(findings, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
