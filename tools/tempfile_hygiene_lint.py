#!/usr/bin/env python3
"""tempfile_hygiene_lint — catch test temp-DIR creations with no registered cleanup.

Closes the detection gap behind T-1307 + T-1308: two `tools/test_*.py` setUp/helper
methods created a `tempfile.mkdtemp()` / `tempfile.TemporaryDirectory()` and never
removed it, leaking 594 `/tmp/tmpXXXXXXXX` dirs across three accounts over 12 days
before a manual sweep found them. The fixes closed the two instances; this lint
closes the CLASS.

Report-only / read-only: it parses the committed `tools/test_*.py` sources with
`ast` and authors nothing (no commits, no subprocess of the linted files), so it
sits outside the auto-commit contract (same exemption class as the other lints).

Scope (a creation is FLAGGED iff all hold — structural, NOT an allowlist, so a new
leaky test can't silently escape):
  1. a Call to `mkdtemp(...)` or `TemporaryDirectory(...)` (bare or `tempfile.`-qualified)
  2. NOT used as a `with ...:` context manager  (a `with TemporaryDirectory()` is
     self-cleaning; `mkdtemp` has no __exit__ so it's never excluded by this)
  3. its enclosing scope (the nearest class, else the module-level function) contains
     NO cleanup signal — no `rmtree`, no `.cleanup`, anywhere in that scope. The real
     idioms all leave one of those tokens: `addCleanup(self._tmp.cleanup)`,
     `addCleanup(__import__("shutil").rmtree, tmp, True)`, `shutil.rmtree(self.tmp)`,
     or `self._tmp.cleanup()` in tearDown.

The scope check is class-granular on purpose: cleanup is routinely registered in a
sibling method (a helper creates, setUp/tearDown or addCleanup removes), so a
node-local check would false-positive. The cost is a conservative bias — a scope
that cleans a *different* tempdir masks a leak of another. Accepted: an advisory
lint should under- rather than over-report; do not over-engineer per-name dataflow.

Only `test_*.py` is in scope. Live tools create temp dirs rarely and via `with`
or explicit cleanup; flagging them needs different heuristics and is out of scope.

Exit codes: 0 = clean. 1 = a leak finding. `--strict` is accepted for parity with
the sibling lints (this lint emits only VIOLATIONs, so --strict is currently a
no-op). A companion test asserts the live `tools/` tree lints clean — that test,
run in the suite, is what actually gates (there is no CI).

Usage:
    python3 tools/tempfile_hygiene_lint.py            # human report
    python3 tools/tempfile_hygiene_lint.py --json     # machine-readable
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS = _REPO_ROOT / "tools"

_TEMP_FACTORIES = {"mkdtemp", "TemporaryDirectory"}
_CLEANUP_SIGNAL_RE = re.compile(r"rmtree|\.cleanup\b")

# Documented exceptions: scope key "filename::ScopeName" -> structural rationale.
# Re-stated here so a suppression carries its own justification (anti-rot). Empty
# today; add an entry only with a real reason a flagged creation is actually safe.
KNOWN_EXCEPTIONS: dict[str, str] = {}

# T-3097 (Cat E): unittest's module-level fixture idiom. A ClassDef already
# unifies setUp/tearDown/addCleanup for free — get_source_segment on the class
# covers every method in it. setUpModule/tearDownModule are two SEPARATE
# top-level FunctionDefs with no enclosing node to unify them, so a creation in
# setUpModule with its cleanup in tearDownModule (test_activity_monitor.py's
# shape) was scope-invisible to the sibling. This is the module-level analogue
# of the class case, not a new exemption class.
_MODULE_FIXTURE_PAIR = {"setUpModule": "tearDownModule", "tearDownModule": "setUpModule"}


@dataclass
class Finding:
    file: str
    scope: str
    factory: str
    lineno: int

    def to_dict(self) -> dict:
        return {"file": self.file, "scope": self.scope, "factory": self.factory, "lineno": self.lineno}


def _read(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


def _is_temp_factory(call: ast.Call) -> str | None:
    """Return the factory name if `call` is mkdtemp()/TemporaryDirectory(), else None."""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _TEMP_FACTORIES:
        return func.attr
    if isinstance(func, ast.Name) and func.id in _TEMP_FACTORIES:
        return func.id
    return None


def lint_source(src: str, filename: str) -> list[Finding]:
    """Lint one source string. Pure; returns Findings for uncleaned temp-dir creations."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []  # unparseable -> not our job

    # Parent pointers + the set of Calls that are a `with`-item context expression.
    with_context_calls: set[int] = set()
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node  # type: ignore[attr-defined]
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.context_expr, ast.Call):
                    with_context_calls.add(id(item.context_expr))

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        factory = _is_temp_factory(node)
        if factory is None or id(node) in with_context_calls:
            continue
        scope = _enclosing_scope(node)
        scope_name = getattr(scope, "name", "<module>")
        if f"{filename}::{scope_name}" in KNOWN_EXCEPTIONS:
            continue
        scope_src = ast.get_source_segment(src, scope) if scope is not tree else src
        sibling_src = _module_fixture_sibling_source(tree, src, scope)
        if sibling_src:
            scope_src = f"{scope_src}\n{sibling_src}"
        if not _CLEANUP_SIGNAL_RE.search(scope_src or ""):
            findings.append(Finding(filename, scope_name, factory, node.lineno))
    return findings


def _module_fixture_sibling_source(tree: ast.Module, src: str, scope) -> str | None:
    """setUpModule/tearDownModule's sibling source, else None.

    Only true top-level (`scope in tree.body`) functions qualify — a nested
    function that happens to be named setUpModule is not the unittest hook.
    """
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    if scope not in tree.body:
        return None
    sibling_name = _MODULE_FIXTURE_PAIR.get(scope.name)
    if sibling_name is None:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == sibling_name:
            return ast.get_source_segment(src, node)
    return None


def _enclosing_scope(node: ast.AST):
    """Nearest enclosing ClassDef; else nearest module-level FunctionDef; else module."""
    cur = getattr(node, "_parent", None)
    nearest_func = None
    while cur is not None:
        if isinstance(cur, ast.ClassDef):
            return cur
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)) and nearest_func is None:
            nearest_func = cur
        cur = getattr(cur, "_parent", None)
    return nearest_func


def lint(tools_dir: Path = _TOOLS) -> list[Finding]:
    """Return Findings over all tools/test_*.py. Read-only."""
    findings: list[Finding] = []
    for py in sorted(tools_dir.glob("test_*.py")):
        findings.extend(lint_source(_read(py), py.name))
    return findings


def _format_report(findings: list[Finding]) -> str:
    lines = ["tempfile-hygiene lint — test temp-DIR creations without registered cleanup"]
    lines.append(f"  {len(findings)} finding(s)")
    for f in findings:
        lines.append("")
        lines.append(f"  [LEAK] {f.file}::{f.scope}  line {f.lineno}  ({f.factory})")
        lines.append("      - creates a temp dir but its scope registers no cleanup "
                     "(addCleanup/.cleanup()/rmtree)")
    if not findings:
        lines.append("  all test temp-dir creations register cleanup.")
    return "\n".join(lines)


def exit_code(findings: list[Finding], strict: bool = False) -> int:
    return 1 if findings else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Lint tools/test_*.py for temp-dir creations without registered cleanup (report-only)."
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
