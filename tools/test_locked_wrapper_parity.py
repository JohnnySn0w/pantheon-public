"""T-4888: structural lint -- a public wrapper must stay in keyword parity with its ``*_locked`` worker.

Outage 2026-10-12 21:55Z (#894): ``_wake_persistent_runtime_locked`` gained
``gate_on_usability``/``probe_deps`` but the public ``wake_persistent_runtime`` wrapper did not,
so ``main()`` raised TypeError on every persona-consume fire while the suite stayed green.

For every public (no leading underscore) function in ``tools/*.py`` that calls a ``*_locked``
worker, AST-check:
  (a) the wrapper accepts every keyword-only parameter of the worker (or has ``**kwargs``);
  (b) every keyword-only parameter of the wrapper is forwarded in the call (or the call uses ``**``);
  (c) every keyword the call passes exists on the worker (or the worker has ``**kwargs``).
Workers that cannot be resolved by AST (methods, dynamic lookups, modules outside tools/) are
collected in ``Scan.unresolved`` -- visible, asserted on, never silently dropped.
``ALLOWLIST`` maps (module, wrapper, worker, param, check) -> reason; stale entries fail.
"""

from __future__ import annotations

import ast
import unittest
from dataclasses import dataclass, field
from pathlib import Path

TOOLS = Path(__file__).resolve().parent

# (module stem, wrapper, worker, param, check) -> documented reason. Only deliberate, reasoned
# exceptions; a drifted wrapper is fixed, not excused.
_NARROW = ("deliberately narrower public surface; internal callers needing this kwarg call the "
           "*_locked core directly (episode_store.py:1065/1086/1095); T-4888 ruling")
ALLOWLIST: dict[tuple[str, str, str, str, str], str] = {
    ("delivery_rebase_controller", "on_nonff_rejection", "_deliver_locked", "send_page",
     "wrapper-lacks-worker-kwonly"): _NARROW,
    ("delivery_rebase_controller", "on_nonff_rejection", "_deliver_locked", "reenter_count",
     "wrapper-lacks-worker-kwonly"): _NARROW,
    ("delivery_rebase_controller", "recover", "_recover_locked", "send_page",
     "wrapper-lacks-worker-kwonly"): _NARROW,
}


# (module, public function/method) that calls a *_locked core but is a direct CALLER of it by
# design, not a forwarding wrapper: checks (a)/(b) are skipped, (c) call-keyword-validity still runs.
NOT_WRAPPERS: dict[tuple[str, str], str] = {
    ("episode_store", "RebaseEdge.on_nonff"):
        "direct caller of the core by design; not a forwarding wrapper; T-4888 ruling",
    ("episode_store", "RebaseEdge.on_recover"):
        "direct caller of the core by design; not a forwarding wrapper; T-4888 ruling",
}

# (module, public function/method, callee text) the AST cannot follow, each with a reason.
# A NEW unresolvable site fails test_unresolved_workers_are_reported_not_ignored until decided.
UNRESOLVED_OK: dict[tuple[str, str, str], str] = {
    ("inbox_consumer", "list_eligible_tickets", "interactive_locked"):
        "local list variable named interactive_locked, not a worker reference; T-4888 ruling",
}


@dataclass
class Scan:
    checked: list[tuple[str, str, str]] = field(default_factory=list)  # (module, wrapper, worker)
    violations: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    suppressed: list[tuple[str, str, str, str, str]] = field(default_factory=list)  # allowlisted hits
    unresolved: list[tuple[str, str, str, str]] = field(default_factory=list)  # (module, wrapper, callee, why)


def _is_locked(name: str) -> bool:
    return name.endswith("_locked")


def _module_funcs(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _import_maps(tree: ast.Module) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """(name -> (tools module stem, original name)) for ``from tools.x import y``;
    (alias -> tools module stem) for ``from tools import x`` / ``import tools.x as a``."""
    names: dict[str, tuple[str, str]] = {}
    mods: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.module == "tools" or (n.module is None and n.level):
                for a in n.names:
                    mods[a.asname or a.name] = a.name
            elif n.module and n.module.startswith("tools."):
                for a in n.names:
                    names[a.asname or a.name] = (n.module.split(".", 1)[1], a.name)
            elif n.level and n.module:
                for a in n.names:
                    names[a.asname or a.name] = (n.module, a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith("tools.") and a.asname:
                    mods[a.asname] = a.name.split(".", 1)[1]
    return names, mods


def _kwonly(fn) -> list[str]:
    return [a.arg for a in fn.args.kwonlyargs]


def _accepts(fn) -> set[str]:
    return {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}


def _direct_locked_calls(fn) -> list[ast.Call]:
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and _is_locked(f.id)) or (
                isinstance(f, ast.Attribute) and _is_locked(f.attr)
            ):
                out.append(node)
    return out


def _indirect_locked_mentions(fn) -> list[str]:
    """``*_locked`` names read anywhere other than as a direct ``Call.func`` (alias assignment,
    argument, ``functools.partial(...)``): the AST cannot follow those to a call."""
    direct = {id(c.func) for c in _direct_locked_calls(fn)}
    out = []
    for node in ast.walk(fn):
        if id(node) in direct:
            continue
        if isinstance(node, ast.Name) and _is_locked(node.id) and isinstance(node.ctx, ast.Load):
            out.append(node.id)
        elif isinstance(node, ast.Attribute) and _is_locked(node.attr) and isinstance(node.ctx, ast.Load):
            out.append(ast.unparse(node))
    return out


def check_pair(module: str, label: str, wrapper, worker, call: ast.Call, scan: Scan) -> None:
    wname, kname = label, worker.name
    scan.checked.append((module, wname, kname))
    direct_caller = (module, label) in NOT_WRAPPERS
    w_accepts, k_accepts = _accepts(wrapper), _accepts(worker)
    forwarded = {k.arg for k in call.keywords if k.arg}
    star = any(k.arg is None for k in call.keywords)

    def bad(param: str, check: str) -> None:
        key = (module, wname, kname, param, check)
        (scan.suppressed if key in ALLOWLIST else scan.violations).append(key)

    if wrapper.args.kwarg is None and not direct_caller:
        for p in _kwonly(worker):
            if p not in w_accepts:
                bad(p, "wrapper-lacks-worker-kwonly")
    if not star and not direct_caller:
        for p in _kwonly(wrapper):
            if p not in forwarded:
                bad(p, "wrapper-kwonly-not-forwarded")
    if worker.args.kwarg is None:
        for p in sorted(forwarded):
            if p not in k_accepts:
                bad(p, "call-keyword-unknown-to-worker")


def _candidates(tree: ast.Module):
    """Public wrapper candidates: (label, fn, same-class methods | None). Module-level public
    functions and public methods of any (top-level) class."""
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_"):
            yield n.name, n, None
        elif isinstance(n, ast.ClassDef):
            methods = {m.name: m for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for m in methods.values():
                if not m.name.startswith("_"):
                    yield f"{n.name}.{m.name}", m, methods


def scan_sources(sources: dict[str, str]) -> Scan:
    scan = Scan()
    trees = {m: ast.parse(s) for m, s in sources.items()}
    for module in sorted(sources):
        tree = trees[module]
        funcs = _module_funcs(tree)
        names, mods = _import_maps(tree)
        for label, wrapper, methods in _candidates(tree):
            for ref in _indirect_locked_mentions(wrapper):
                scan.unresolved.append(
                    (module, label, ref, "referenced outside a direct call (alias/partial/argument)"))
            for call in _direct_locked_calls(wrapper):
                f = call.func
                target = None
                if isinstance(f, ast.Name):
                    if f.id in funcs:
                        target = funcs[f.id]
                    elif f.id in names and names[f.id][0] in trees:
                        target = _module_funcs(trees[names[f.id][0]]).get(names[f.id][1])
                    callee = f.id
                else:
                    callee = ast.unparse(f)
                    if isinstance(f.value, ast.Name) and f.value.id in mods and mods[f.value.id] in trees:
                        target = _module_funcs(trees[mods[f.value.id]]).get(f.attr)
                    elif (methods is not None and isinstance(f.value, ast.Name)
                          and f.value.id in ("self", "cls")):
                        target = methods.get(f.attr)
                if target is None:
                    scan.unresolved.append((module, label, callee, "no module-level or same-class def found by AST"))
                else:
                    check_pair(module, label, wrapper, target, call, scan)
    return scan


def scan_tools() -> Scan:
    sources = {
        p.stem: p.read_text(encoding="utf-8")
        for p in sorted(TOOLS.glob("*.py"))
        if not p.name.startswith("test_") and p.name != "conftest.py"
    }
    return scan_sources(sources)


def stale_allowlist(scan: Scan) -> list:
    """Allowlist entries that no longer suppress a real finding (renamed, removed or fixed)."""
    hit = set(scan.suppressed)
    return [key for key in ALLOWLIST if key not in hit]


class LockedWrapperParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scan = scan_tools()

    def test_persona_bringup_wake_wrapper_is_covered(self):
        self.assertIn(
            ("persona_bringup", "wake_persistent_runtime", "_wake_persistent_runtime_locked"),
            self.scan.checked,
        )

    def test_no_wrapper_worker_keyword_drift(self):
        self.assertEqual(self.scan.violations, [], "wrapper/worker keyword drift (#894 shape)")

    def test_allowlisted_gaps_are_the_reasoned_ones(self):
        self.assertEqual(sorted(self.scan.suppressed), sorted(ALLOWLIST))
        for reason in ALLOWLIST.values():
            self.assertIn("T-4888 ruling", reason)

    def test_allowlist_has_no_stale_entries(self):
        self.assertEqual(stale_allowlist(self.scan), [])

    def test_unresolved_workers_are_reported_not_ignored(self):
        # Unresolvable *_locked references (alias, partial, argument, unknown callee) must equal a
        # reasoned set; a new one forces a decision instead of passing silently.
        got = {(m, w, c) for m, w, c, _why in self.scan.unresolved}
        self.assertEqual(sorted(got), sorted(UNRESOLVED_OK))
        for entry in self.scan.unresolved:
            self.assertTrue(entry[3])
        for reason in (*UNRESOLVED_OK.values(), *NOT_WRAPPERS.values()):
            self.assertIn("T-4888 ruling", reason)
        print(f"\n[T-4888] locked-wrapper lint: checked={self.scan.checked} "
              f"unresolved={self.scan.unresolved}")

    def test_not_wrappers_are_real_direct_callers(self):
        checked = {(m, w) for m, w, _k in self.scan.checked}
        self.assertEqual([k for k in NOT_WRAPPERS if k not in checked], [])


class CheckerSelfTests(unittest.TestCase):
    """The lint itself must be able to fail (synthetic sources, no repo files)."""

    CLEAN = (
        "def pub(a, *, x=1, y=2):\n    return _w_locked(a, x=x, y=y)\n"
        "def _w_locked(a, *, x=1, y=2):\n    return a\n"
    )

    def test_clean_passes(self):
        s = scan_sources({"m": self.CLEAN})
        self.assertEqual(s.violations, [])
        self.assertEqual(s.checked, [("m", "pub", "_w_locked")])

    def test_worker_kwarg_missing_on_wrapper(self):  # the #894 shape
        src = (
            "def pub(a, *, x=1):\n    return _w_locked(a, x=x)\n"
            "def _w_locked(a, *, x=1, gate=False):\n    return a\n"
        )
        s = scan_sources({"m": src})
        self.assertEqual(s.violations, [("m", "pub", "_w_locked", "gate", "wrapper-lacks-worker-kwonly")])

    def test_wrapper_kwarg_accepted_but_dropped(self):
        src = (
            "def pub(a, *, x=1, gate=False):\n    return _w_locked(a, x=x)\n"
            "def _w_locked(a, *, x=1, gate=False):\n    return a\n"
        )
        s = scan_sources({"m": src})
        self.assertEqual(s.violations, [("m", "pub", "_w_locked", "gate", "wrapper-kwonly-not-forwarded")])

    def test_call_keyword_unknown_to_worker(self):
        src = (
            "def pub(a):\n    return _w_locked(a, nope=1)\n"
            "def _w_locked(a):\n    return a\n"
        )
        s = scan_sources({"m": src})
        self.assertEqual(s.violations, [("m", "pub", "_w_locked", "nope", "call-keyword-unknown-to-worker")])

    def test_cross_module_worker_resolved(self):
        a = "from tools.b import _w_locked\ndef pub(*, x=1):\n    return _w_locked(x=x)\n"
        b = "def _w_locked(*, x=1, gate=False):\n    return x\n"
        s = scan_sources({"a": a, "b": b})
        self.assertEqual([v[3] for v in s.violations], ["gate"])

    def test_unresolvable_worker_is_listed(self):
        src = "def pub(self):\n    return self._w_locked()\n"
        s = scan_sources({"m": src})
        self.assertEqual(s.violations, [])
        self.assertEqual([u[:3] for u in s.unresolved], [("m", "pub", "self._w_locked")])

    def test_alias_is_unresolved(self):
        src = "def pub(*, x=1):\n    f = _w_locked\n    return f(x=x)\ndef _w_locked(*, x=1, g=0):\n    pass\n"
        s = scan_sources({"m": src})
        self.assertEqual([u[:3] for u in s.unresolved], [("m", "pub", "_w_locked")])

    def test_partial_and_argument_are_unresolved(self):
        src = ("import functools\ndef pub(*, x=1):\n    return functools.partial(_w_locked, x=x)\n"
               "def pub2(run):\n    return run(_w_locked)\ndef _w_locked(*, x=1):\n    pass\n")
        s = scan_sources({"m": src})
        self.assertEqual(sorted(u[1] for u in s.unresolved), ["pub", "pub2"])

    def test_public_method_resolved_and_checked(self):
        src = ("class C:\n    def pub(self, *, x=1):\n        return self._w_locked(x=x)\n"
               "    def _w_locked(self, *, x=1, gate=False):\n        return x\n")
        s = scan_sources({"m": src})
        self.assertEqual(s.checked, [("m", "C.pub", "_w_locked")])
        self.assertEqual([v[3] for v in s.violations], ["gate"])

    def test_clean_method_passes_and_unknown_self_attr_is_unresolved(self):
        ok = ("class C:\n    def pub(self, *, x=1):\n        return self._w_locked(x=x)\n"
              "    def _w_locked(self, *, x=1):\n        return x\n")
        self.assertEqual(scan_sources({"m": ok}).violations, [])
        bad = "class C:\n    def pub(self):\n        return self._gone_locked()\n"
        self.assertEqual([u[:3] for u in scan_sources({"m": bad}).unresolved], [("m", "C.pub", "self._gone_locked")])

    def test_private_wrapper_ignored(self):
        src = "def _pub(*, x=1):\n    return _w_locked()\ndef _w_locked(*, x=1, g=0):\n    pass\n"
        s = scan_sources({"m": src})
        self.assertEqual((s.checked, s.violations, s.unresolved), ([], [], []))

    def test_stale_allowlist_detected(self):
        import unittest.mock as mock

        s = scan_sources({"m": self.CLEAN})
        with mock.patch.dict(ALLOWLIST, {("m", "pub", "_w_locked", "x", "wrapper-lacks-worker-kwonly"): "r"}):
            # pair exists but nothing to suppress -> stale (the 3 real entries are stale vs this
            # synthetic scan too, so assert membership)
            self.assertIn(("m", "pub", "_w_locked", "x", "wrapper-lacks-worker-kwonly"), stale_allowlist(s))


if __name__ == "__main__":
    unittest.main()
