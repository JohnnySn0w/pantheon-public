"""Axis-agnostic import-time snapshot-site scanner (T-3573).

Extracted from test_account_persona_map.TestEverySnapshotSiteIsGuarded (T-2334)
so BOTH manifest-derived axes run ONE copy of the detector rather than two that
can drift apart:

  - the ACCOUNT axis   ({account_persona_map, fleet_accounts, account_persona})
    in tools/test_account_persona_map.py
  - the SIBLING axis   ({instance_binding, resolve_instance_role})
    in tools/test_instance_role_grants.py

Each axis parameterises the SAME scan by its own accessor set, its own
PER_SITE_GUARDS-derived GUARDED_SNAPSHOTS (for the re-export rule), and its own
plant header. The detection logic — the whole reason this is worth sharing — is
identical and lives here once.

WHAT THE SCAN DETECTS, precisely — a MODULE-SCOPE assignment in a non-test
tools/*.py, including one inside a module-level if / try / for / while / with and
including tuple-unpack targets, whose right-hand side CONTAINS ANYWHERE a call to
one of the axis's accessors (under any import alias, called by name or as an
attribute), directly or through a module-scope function that calls one, to a
recursion depth of 2; plus a module-scope `from <guarded module> import <guarded
constant>` re-export.

"CONTAINS ANYWHERE" IS THE T-2334-REVIEW FIX AND IT IS THE WHOLE DIFFERENCE. The
first cut inspected only the OUTERMOST callee of an assignment whose RHS *was* a
Call, so the word `dict` was the line between covered and silent: `X = accessor()`
was caught and `X = dict(accessor())` was not, along with every comprehension,
wrapper, BinOp and ternary. This walks the whole right-hand side.

The documented blind spots (built-across-statements, def/class bodies, >2-level
helpers, indirection via getattr / a name bound to the accessor, a bare walrus,
and anything outside tools/*.py) are asserted per-axis in each caller's
_MUST_FIND / _MUST_MISS control planting, so widening this scan reddens those and
the blind-spot list cannot silently go stale.
"""

from __future__ import annotations

import ast
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Iterable


class SnapshotSiteScanner:
    """Parameterised import-time snapshot detector. One instance per axis."""

    # Bodies that do NOT run at import. The scan descends into every OTHER
    # module-level compound statement (if / try / for / while / with), because
    # those do — mailbox_mcp binds its accessor inside a module-level try/except,
    # so that is the live shape.
    NOT_IMPORT_TIME = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

    def __init__(
        self,
        accessors: Iterable[str],
        guarded_snapshots: Iterable[tuple[str, str]],
        plant_header: str,
    ) -> None:
        self._accessors = frozenset(accessors)
        self._guarded = frozenset(guarded_snapshots)
        self._plant_header = plant_header

    @staticmethod
    def _callee(call: ast.Call) -> str:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ""

    def _module_scope(self, body):
        """Yield every statement that RUNS AT IMPORT, descending into module-level
        compound statements but never into a def or class body."""
        for stmt in body or []:
            yield stmt
            if isinstance(stmt, self.NOT_IMPORT_TIME):
                continue
            for field in ("body", "orelse", "finalbody"):
                yield from self._module_scope(getattr(stmt, field, None))
            for handler in getattr(stmt, "handlers", None) or []:
                yield from self._module_scope(handler.body)

    @staticmethod
    def _assigned_names(stmt) -> list[str]:
        """Every name *stmt* binds, tuple-unpack targets included."""
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        else:
            return []
        return [
            node.id
            for target in targets
            for node in ast.walk(target)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        ]

    def scan_module(self, path: Path) -> set[tuple[str, str]]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = path.stem
        statements = list(self._module_scope(tree.body))

        aliases = set(self._accessors)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for name in node.names:
                    if name.name in self._accessors and name.asname:
                        aliases.add(name.asname)
        defs = {s.name: s for s in statements if isinstance(s, ast.FunctionDef)}

        def reaches(name: str, depth: int = 0) -> bool:
            if name in aliases:
                return True
            if depth >= 2 or name not in defs:
                return False
            return any(
                isinstance(node, ast.Call) and reaches(self._callee(node), depth + 1)
                for node in ast.walk(defs[name])
            )

        found: set[tuple[str, str]] = set()
        for stmt in statements:
            names = self._assigned_names(stmt)
            value = getattr(stmt, "value", None)
            # WALK the right-hand side. Inspecting only its head is what made
            # `dict(...)` the line between covered and silent — see module docstring.
            if names and value is not None and any(
                isinstance(node, ast.Call) and reaches(self._callee(node))
                for node in ast.walk(value)
            ):
                found.update((module, name) for name in names)
            if isinstance(stmt, ast.ImportFrom):
                source = (stmt.module or "").split(".")[-1]
                for name in stmt.names:
                    if not name.asname and (source, name.name) in self._guarded:
                        found.add((module, name.name))
        return found

    def scan_source(
        self, source: str, name: str, register_cleanup: Callable[..., object]
    ) -> set[tuple[str, str]]:
        """Plant *source* (behind the axis's plant header) as a throwaway module
        and scan it. *register_cleanup* is the caller's cleanup registrar
        (unittest's self.addCleanup), so the temp dir is torn down with the test."""
        planted = Path(tempfile.mkdtemp(prefix="snapshot-scan-")) / f"{name}.py"
        register_cleanup(shutil.rmtree, planted.parent, True)
        planted.write_text(self._plant_header + source, encoding="utf-8")
        return self.scan_module(planted)
