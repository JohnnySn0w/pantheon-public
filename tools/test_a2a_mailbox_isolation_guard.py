"""T-1941 structural test-isolation guard tests.

Two independent layers, both "catch the NEXT one too" per Orchestrator's ruling
(a future a2a test file must not be able to silently reintroduce the
containment defect root-caused in tools/a2a/_test_mailbox_guard.py's
docstring):

1. Runtime layer (`TestGuardRaisesOnLivePath`): proves
   `guard_live_mailbox()` actually raises when `tools.mailbox.send`/
   `consume` is called with a path under the live mailbox substrate root,
   and does NOT interfere with a legitimate temp-path call — the guard
   itself needs a falsifying test, not just an assertion it exists.
2. Collection-time layer (`TestBuildAppCallSitesAreExplicit`): source-scans
   every a2a test file for `build_app(` call sites and fails if any of
   them omit an explicit `request_inbox=` override — this is the EXACT
   shape of the historical defect (a bare `build_app()` silently
   defaulting to the live paths), caught before the test even runs.

Run from repo root:
    ~/.venv-a2a/bin/python -m unittest tools.test_a2a_mailbox_isolation_guard -v

The runtime-layer tests need the a2a-sdk venv (tools.mailbox itself does
not, but tools.a2a.specialist_seat_server — imported for its build_app source
text — pulls in a2a-sdk at import time); skipped cleanly under system
python3 like the other a2a test modules. The collection-time scan is pure
AST (no a2a-sdk import), so it runs under BOTH interpreters
(belt-and-suspenders: catches the defect shape even in a
system-python3-only CI lane that never exercises the live-server tests).

Non-vacuousness (Orchestrator's REBOUND-round-1 ruling, T-1890/T-1941 merge):
`TestScanIsNonVacuous` proves `_find_bare_build_app_calls` actually goes
RED against a known-bad synthetic fixture (a bare `build_app()` call) and
GREEN against a known-good one (explicit `request_inbox=`) — without
this, `TestBuildAppCallSitesAreExplicit`'s single "the real files are
clean" assertion would still pass if the scan function were silently
broken to always return `[]`; "a guard that can't fail is decoration."
Additionally verified, ad hoc (not committed as a test — coupling a
permanent unit test to a specific git commit hash is the wrong shape),
against the ACTUAL historical pre-fix content of
tools/test_a2a_orchestrator_client.py (`git show ebd12d1d:...`, the T-1889-era
commit immediately before this ticket's changes): the scan flags its
line-64 bare `server_mod.build_app()` call, and passes clean against the
current committed file. See this ticket's final report for that
transcript.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

_A2A_TEST_DIR = Path(__file__).resolve().parent


def _a2a_test_files() -> list[Path]:
    files = sorted(_A2A_TEST_DIR.glob("test_a2a_*.py"))
    files += sorted((_A2A_TEST_DIR / "a2a").glob("test_*.py"))
    return files


def _find_bare_build_app_calls(path: Path) -> list[str]:
    """AST-based (not regex/text) scan for `build_app(...)` CALL sites
    missing an explicit `request_inbox=` keyword argument. AST, not regex,
    deliberately — a text/regex scan false-positives on prose mentions of
    "build_app()" in docstrings/comments (this exact scan's first draft
    did, on three files, including this one's own docstring), where AST
    only ever sees real `ast.Call` nodes."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "build_app":
            continue
        has_request_inbox = any(kw.arg == "request_inbox" for kw in node.keywords)
        if not has_request_inbox:
            offenders.append(f"{path.name}:{node.lineno}: build_app(...) call")
    return offenders


class TestBuildAppCallSitesAreExplicit(unittest.TestCase):
    """Collection-time layer — pure AST scan, no a2a-sdk import needed."""

    def test_every_build_app_call_passes_explicit_request_inbox(self) -> None:
        offenders: list[str] = []
        files = _a2a_test_files()
        self.assertGreater(
            len(files), 0, "no a2a test files found — scan target is wrong"
        )
        for path in files:
            offenders.extend(_find_bare_build_app_calls(path))
        self.assertEqual(
            offenders,
            [],
            "build_app(...) call site(s) missing an explicit request_inbox= "
            "override — this is the exact T-1941 containment-defect shape "
            "(a bare call silently defaults to the live mailbox paths):\n"
            + "\n".join(offenders),
        )


class TestScanIsNonVacuous(unittest.TestCase):
    """Proves `_find_bare_build_app_calls` can actually GO RED, not just
    stay green on already-clean files. Synthetic fixtures, not the real
    a2a test files or git history — a permanent unit test should not
    depend on a specific commit hash to stay meaningful."""

    def _write(self, tmp_dir: str, source: str) -> Path:
        path = Path(tmp_dir) / "test_a2a_fixture.py"
        path.write_text(source, encoding="utf-8")
        return path

    def test_bare_call_is_flagged_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write(
                tmp_dir, "app = server_mod.build_app()\n"
            )
            offenders = _find_bare_build_app_calls(path)
        self.assertEqual(len(offenders), 1)
        self.assertIn("build_app(...) call", offenders[0])

    def test_result_inbox_only_is_still_flagged_red(self) -> None:
        """The historical defect shape is specifically a MISSING
        request_inbox — a call that overrides result_inbox but forgets
        request_inbox is exactly as dangerous (still defaults the staging
        target to the live path) and must still be caught."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write(
                tmp_dir,
                'app = server_mod.build_app(result_inbox="/tmp/x")\n',
            )
            offenders = _find_bare_build_app_calls(path)
        self.assertEqual(len(offenders), 1)

    def test_explicit_request_inbox_is_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write(
                tmp_dir,
                'app = server_mod.build_app(\n'
                '    request_inbox="/tmp/x", result_inbox="/tmp/y"\n'
                ')\n',
            )
            offenders = _find_bare_build_app_calls(path)
        self.assertEqual(offenders, [])

    def test_prose_mention_in_docstring_is_not_flagged(self) -> None:
        """Regression for this scan's own first-draft bug: a regex
        version false-positived on docstring/comment prose mentioning
        "build_app()". AST must not repeat that."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = self._write(
                tmp_dir,
                '"""This module never calls build_app() carelessly."""\n'
                "# see build_app() for details\n",
            )
            offenders = _find_bare_build_app_calls(path)
        self.assertEqual(offenders, [])


_SKIP_REASON: str | None = None
try:
    from tools import mailbox
    from tools.a2a._test_mailbox_guard import (
        FORBIDDEN_PREFIX,
        LiveMailboxGuardError,
        guard_live_mailbox,
    )
    # Importing specialist_seat_server pulls in a2a-sdk; this module's OWN
    # dependency is only tools.mailbox + _test_mailbox_guard, but keeping
    # the same _SKIP_REASON pattern as the sibling a2a test modules for a
    # consistent skip message across the suite.
    from tools.a2a import specialist_seat_server as _server_mod  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised under system python3
    _SKIP_REASON = (
        f"a2a-sdk not importable under this interpreter ({exc}); run with "
        f".venv-a2a/bin/python instead (see tools/a2a/PROVISION.md)"
    )


@unittest.skipIf(_SKIP_REASON is not None, _SKIP_REASON)
class TestGuardRaisesOnLivePath(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.temp_inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.temp_inbox)  # T-2216: send() no longer auto-provisions

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_send_to_live_path_raises(self) -> None:
        with guard_live_mailbox():
            with self.assertRaises(LiveMailboxGuardError):
                mailbox.send(
                    f"{FORBIDDEN_PREFIX}/mailbox/specialist",
                    {"x": 1},
                    "specialist-a2a",
                )

    def test_consume_from_live_path_raises(self) -> None:
        with guard_live_mailbox():
            with self.assertRaises(LiveMailboxGuardError):
                mailbox.consume(f"{FORBIDDEN_PREFIX}/mailbox/specialist", lambda m: None)

    def test_send_to_temp_path_still_works(self) -> None:
        """Regression: the guard must not break legitimate temp-path
        usage — it's a targeted block, not a blanket disable."""
        with guard_live_mailbox():
            mid = mailbox.send(self.temp_inbox, {"x": 1}, "specialist-a2a")
        self.assertTrue((self.temp_inbox / "new" / mid).exists())

    def test_guard_only_active_inside_context(self) -> None:
        """Outside the `with` block, mailbox.send/consume are unpatched —
        proves the guard doesn't leak state across tests."""
        self.assertNotEqual(
            mailbox.send.__qualname__, "guard_live_mailbox.<locals>._guarded_send"
        )


if __name__ == "__main__":
    unittest.main()
