"""C4 test 4 -- CI-time no-swallow lint (T-3295, ORG-080 tranche D).

README C4 test 4: "A repo-wide grep proves no `except PrimitiveUnavailable`
clause exists that doesn't either re-raise or produce a typed refusal."

This tranche implements the check as an AST-based walk (more robust to
multi-line/nested handlers than a literal ``grep``; the README's own
"documentary vs ref-impl mechanism" carve applies here too -- an
equally-valid mechanism for the same outcome), scoped to
``tools/germinate/`` (the shared parent for tranches A-D), matching
``except`` clauses whose exception name is ``PrimitiveUnavailable`` or ends
in ``PrimitiveUnavailable`` (bare name, dotted attribute, or tuple member).

**Naming convention (PO-approved, T-3295 Phase 1 open question 2):** each
tranche defines its own LOCAL ``PrimitiveUnavailable(name, reason)`` class
(mirroring ``harness.exceptions.HarnessUnavailable``'s shape) rather than
importing one shared canonical class -- so the lint matches by class-name
suffix, not by a single import path. A post-arc follow-up ticket may
consolidate into a shared ``planner_sandbox/exceptions.py`` if the
convention holds across A-D (PO's note); not blocking this arc.

A "swallow" is an ``except`` clause matching the above whose body contains
no ``raise`` statement (bare re-raise or ``raise NewExc(...) from exc``).
"""

from __future__ import annotations

import ast
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCAN_ROOT = _REPO_ROOT / "tools" / "germinate"


@dataclass(frozen=True)
class Violation:
    path: Path
    lineno: int
    detail: str


def _handler_matches_primitive_unavailable(handler: ast.ExceptHandler) -> bool:
    def _name_matches(node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id == "PrimitiveUnavailable" or node.id.endswith(
                "PrimitiveUnavailable"
            )
        if isinstance(node, ast.Attribute):
            return node.attr == "PrimitiveUnavailable" or node.attr.endswith(
                "PrimitiveUnavailable"
            )
        return False

    if handler.type is None:
        return False  # bare `except:` -- not a PrimitiveUnavailable-specific swallow
    if isinstance(handler.type, ast.Tuple):
        return any(_name_matches(elt) for elt in handler.type.elts)
    return _name_matches(handler.type)


def _is_typed_pytest_skip_call(node: ast.AST) -> bool:
    """A ``pytest.skip(reason=...)`` (or ``skip(reason=...)``) call with a
    concrete reason -- this codebase's own established typed-refusal
    pattern for TEST code (conftest.py: "SKIP reasons must always name the
    concrete observed primitive state, never a bare 'skipped'"). A skip
    with no reason argument does NOT count -- that IS the bare-skip
    anti-pattern the convention forbids, so it's still a swallow here.
    """

    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else None
    )
    if name != "skip":
        return False
    has_reason_kwarg = any(kw.arg == "reason" for kw in node.keywords)
    return bool(node.args) or has_reason_kwarg


def _handler_reraises(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return True
        if _is_typed_pytest_skip_call(node):
            return True
    return False


def find_swallowed_primitive_unavailable(source: str, path: Path) -> list[Violation]:
    """Parses ``source`` (as if it were ``path``) and returns one
    ``Violation`` per ``except PrimitiveUnavailable``-matching handler that
    contains no ``raise`` statement anywhere in its body (including nested
    ``if``/``try`` blocks -- ``ast.walk`` covers the whole handler subtree).
    """

    tree = ast.parse(source, filename=str(path))
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _handler_matches_primitive_unavailable(node):
            continue
        if not _handler_reraises(node):
            violations.append(
                Violation(
                    path=path,
                    lineno=node.lineno,
                    detail="except PrimitiveUnavailable clause has no raise statement",
                )
            )
    return violations


def find_swallowed_primitive_unavailable_in_tree(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for py_file in sorted(root.rglob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        violations.extend(find_swallowed_primitive_unavailable(source, py_file))
    return violations


# --- Tests -----------------------------------------------------------------


def test_checker_flags_a_synthetic_swallow() -> None:
    bad = textwrap.dedent(
        """
        def do_thing():
            try:
                risky()
            except PrimitiveUnavailable:
                pass  # silently degrade -- exactly what C4 forbids
        """
    )
    violations = find_swallowed_primitive_unavailable(bad, Path("synthetic_bad.py"))
    assert len(violations) == 1
    assert violations[0].lineno == 5


def test_checker_flags_a_swallow_inside_a_tuple_handler() -> None:
    bad = textwrap.dedent(
        """
        def do_thing():
            try:
                risky()
            except (ValueError, PrimitiveUnavailable):
                log.warning("meh")
        """
    )
    violations = find_swallowed_primitive_unavailable(bad, Path("synthetic_bad_tuple.py"))
    assert len(violations) == 1


def test_checker_flags_a_locally_named_subclass_by_suffix() -> None:
    bad = textwrap.dedent(
        """
        def do_thing():
            try:
                risky()
            except AdmissionPrimitiveUnavailable:
                return None
        """
    )
    violations = find_swallowed_primitive_unavailable(bad, Path("synthetic_bad_subclass.py"))
    assert len(violations) == 1


def test_checker_passes_a_typed_pytest_skip() -> None:
    good = textwrap.dedent(
        """
        def do_thing():
            try:
                risky()
            except PrimitiveUnavailable as exc:
                pytest.skip(reason=f"{exc.name} unavailable: {exc.reason}")
        """
    )
    violations = find_swallowed_primitive_unavailable(good, Path("synthetic_good_skip.py"))
    assert violations == []


def test_checker_flags_a_bare_pytest_skip_with_no_reason() -> None:
    bad = textwrap.dedent(
        """
        def do_thing():
            try:
                risky()
            except PrimitiveUnavailable:
                pytest.skip()
        """
    )
    violations = find_swallowed_primitive_unavailable(bad, Path("synthetic_bad_bare_skip.py"))
    assert len(violations) == 1


def test_checker_passes_a_bare_reraise() -> None:
    good = textwrap.dedent(
        """
        def do_thing():
            try:
                risky()
            except PrimitiveUnavailable:
                log.error("refusing")
                raise
        """
    )
    violations = find_swallowed_primitive_unavailable(good, Path("synthetic_good_reraise.py"))
    assert violations == []


def test_checker_passes_a_typed_refusal_reraise() -> None:
    good = textwrap.dedent(
        """
        def do_thing():
            try:
                risky()
            except PrimitiveUnavailable as exc:
                raise EngineRefused(f"refusing: {exc}") from exc
        """
    )
    violations = find_swallowed_primitive_unavailable(good, Path("synthetic_good_refusal.py"))
    assert violations == []


def test_checker_ignores_unrelated_except_clauses() -> None:
    unrelated = textwrap.dedent(
        """
        def do_thing():
            try:
                risky()
            except ValueError:
                pass
            except KeyError:
                pass
        """
    )
    violations = find_swallowed_primitive_unavailable(unrelated, Path("synthetic_unrelated.py"))
    assert violations == []


def test_checker_scans_a_real_temp_tree() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "clean.py").write_text(
            "try:\n    pass\nexcept PrimitiveUnavailable:\n    raise\n",
            encoding="utf-8",
        )
        (root / "dirty.py").write_text(
            "try:\n    pass\nexcept PrimitiveUnavailable:\n    pass\n",
            encoding="utf-8",
        )
        violations = find_swallowed_primitive_unavailable_in_tree(root)
    assert len(violations) == 1
    assert violations[0].path.name == "dirty.py"


def test_c4_no_swallow_lint() -> None:
    """The real, repo-wide gate: proves tools/germinate/ is clean today.

    Red-before-green shape: before this checker existed, a swallow
    anywhere in tools/germinate/ could land silently. After it exists, any
    tranche (A, B, C, or a future D change) that introduces
    ``except PrimitiveUnavailable: <no raise>`` fails CI here.
    """

    violations = find_swallowed_primitive_unavailable_in_tree(_SCAN_ROOT)
    assert violations == [], "\n".join(
        f"{v.path}:{v.lineno}: {v.detail}" for v in violations
    )
