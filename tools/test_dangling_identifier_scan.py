"""Tests for tools/dangling_identifier_scan.py (T-2151).

The load-bearing test is test_finds_the_real_t1988_defects: a scanner that merely
passes on a clean tree proves nothing. It has to FIND the historical defects it was
built for — otherwise "no findings" is indistinguishable from "not looking".
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.dangling_identifier_scan import scan  # noqa: E402




class TestDanglingIdentifierScan(unittest.TestCase):

    def _tree(self, files: dict[str, str]) -> Path:
        """Build a throwaway source tree that CLEANS ITSELF UP.

        Deliberately a method rather than a module-level helper: it registers its
        own addCleanup, so no call site can forget. The module-level version leaked
        9 temp dirs per run and tripped the tools/ tempfile-hygiene gate — caller-
        registered cleanup would have worked too, but it is correct-if-every-caller-
        remembers across ~10 sites, and this is correct by construction.
        """
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        for rel, body in files.items():
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body)
        return d

    def test_finds_the_real_t1988_defects(self):
        """The shapes actually found in pantheon-pwa: a phantom env var and a cite
        of a symbol that was removed. If this ever stops finding them, the scanner
        is broken regardless of what it reports elsewhere."""
        root = self._tree({
            "globalSetup.ts": (
                "/*\n"
                " * Tests read KINO_TEST_PORT to get the port.\n"
                " */\n"
                'process.env["KINO_E2E_DAEMON_PID"] = String(pid);\n'
            ),
            "fixtures.ts": (
                "// loadRegistry() in daemon/main.ts reads agents.toml.\n"
                "export const FIXTURE = 1;\n"
            ),
        })
        found = {i for _, i, _, _ in scan(root, ("KINO_",), include_calls=True)}
        self.assertIn("KINO_TEST_PORT", found, "phantom env var not detected")
        self.assertIn("loadRegistry", found, "cite of removed symbol not detected")

    def test_identifier_present_in_code_is_not_reported(self):
        root = self._tree({
            "main.ts": (
                "// The daemon reads KINO_TICKET_ACTOR at startup.\n"
                'const actor = process.env["KINO_TICKET_ACTOR"] ?? "operator";\n'
            ),
        })
        self.assertEqual(scan(root, ("KINO_",), include_calls=True), [])

    def test_suppression_marker_silences_deliberate_history(self):
        """Naming something that no longer exists is often correct prose. The opt-out
        must work, or the tool nags on good docs and gets muted."""
        root = self._tree({
            "fixtures.ts": (
                "// T-0508 removed that subsystem (loadRegistry). [dangling-ok]\n"
                "export const FIXTURE = 1;\n"
            ),
        })
        self.assertEqual(scan(root, ("KINO_",), include_calls=True), [])

    def test_prefix_filter_limits_env_reports(self):
        root = self._tree({
            "a.ts": "// mentions KINO_GHOST and OTHER_GHOST\nconst x = 1;\n",
        })
        kino_only = {i for _, i, _, _ in scan(root, ("KINO_",), include_calls=False)}
        self.assertEqual(kino_only, {"KINO_GHOST"})
        both = {i for _, i, _, _ in scan(root, (), include_calls=False)}
        self.assertIn("OTHER_GHOST", both)

    def test_markdown_is_all_prose(self):
        root = self._tree({"README.md": "Set `KINO_PHANTOM` to enable.\n"})
        found = {i for _, i, _, _ in scan(root, ("KINO_",), include_calls=False)}
        self.assertIn("KINO_PHANTOM", found)

    def test_skips_vendor_and_build_dirs(self):
        """A finding inside node_modules is never actionable and would drown the
        signal — the reason this scanner is narrow by construction."""
        root = self._tree({
            "node_modules/pkg/x.ts": "// KINO_VENDOR_GHOST\nconst y = 1;\n",
            "build/out.js": "// KINO_BUILD_GHOST\n",
        })
        self.assertEqual(scan(root, ("KINO_",), include_calls=False), [])

    def test_python_docstring_mentions_do_not_hide_real_code_usage(self):
        """REGRESSION: the first version of this scanner reported a FALSE POSITIVE on
        tools/usage_monitor.py — naive triple-quote toggling desynchronised across 27
        delimiters, classified the back half of the module as prose, and so reported
        env vars that ARE read in code. Python now goes through tokenize."""
        body = '"""Module doc mentions PANTHEON_ANOMALY_SIGMA.\n\nMore prose.\n"""\n'
        body += 'def f():\n    """Inner doc."""\n    return 1\n\n'
        body += 'def g():\n    """Another."""\n    return 2\n\n'
        body += 'SIGMA = _env_float("PANTHEON_ANOMALY_SIGMA", 3.0)\n'
        root = self._tree({"usage_like.py": body})
        found = {i for _, i, _, _ in scan(root, ("PANTHEON_",), include_calls=False)}
        self.assertNotIn(
            "PANTHEON_ANOMALY_SIGMA", found,
            "env var read via a string literal in code must count as REAL usage — "
            "treating string literals as prose would hide exactly the usage that "
            "proves an identifier exists",
        )

    def test_python_docstring_only_mention_is_still_caught(self):
        """The tokenizer fix must not cost sensitivity: a var named ONLY in a
        docstring, with no code usage anywhere, is still dangling."""
        root = self._tree({"m.py": '"""Set PANTHEON_PHANTOM to enable."""\nX = 1\n'})
        found = {i for _, i, _, _ in scan(root, ("PANTHEON_",), include_calls=False)}
        self.assertIn("PANTHEON_PHANTOM", found)

    def test_call_cite_defined_elsewhere_in_code_is_clean(self):
        root = self._tree({
            "a.ts": "// see helper() for detail\nfunction helper() { return 1; }\n",
        })
        self.assertEqual(scan(root, ("KINO_",), include_calls=True), [])


if __name__ == "__main__":
    unittest.main()
