#!/usr/bin/env python3
"""Tests for tempfile_hygiene_lint, plus the live-tree regression guard (T-1309)."""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.tempfile_hygiene_lint as thl

# --- fixtures: each is a self-contained test_*.py source string ---

LEAKY_SETUP = '''
import tempfile, unittest
class TestX(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
    def tearDown(self):
        pass
    def test_a(self):
        self.assertTrue(self.tmp)
'''

CLEAN_ADDCLEANUP_CLEANUP = '''
import tempfile, unittest
class TestX(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
'''

CLEAN_ADDCLEANUP_RMTREE = '''
import tempfile, unittest
class TestX(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
'''

CLEAN_TEARDOWN_RMTREE = '''
import tempfile, shutil, unittest
class TestX(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
    def tearDown(self):
        shutil.rmtree(self.tmp)
'''

CLEAN_WITH_CONTEXT = '''
import tempfile, unittest
class TestX(unittest.TestCase):
    def test_a(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(t)
'''

LEAKY_HELPER = '''
import tempfile, unittest
class TestX(unittest.TestCase):
    def _make_tmp(self):
        tmp = tempfile.mkdtemp()
        return tmp
    def tearDown(self):
        pass
    def test_a(self):
        self.assertTrue(self._make_tmp())
'''

CLEAN_HELPER_ADDCLEANUP = '''
import tempfile, unittest
class TestX(unittest.TestCase):
    def _make_tmp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        return tmp
    def test_a(self):
        self.assertTrue(self._make_tmp())
'''

LEAKY_MODULE_FUNC = '''
import tempfile
def build_fixture():
    d = tempfile.mkdtemp()
    return d
'''

LEAKY_BARE_IMPORT = '''
from tempfile import mkdtemp
import unittest
class TestX(unittest.TestCase):
    def setUp(self):
        self.tmp = mkdtemp()
'''


class TestLintSource(unittest.TestCase):
    def _scopes(self, src):
        return {(f.scope, f.factory) for f in thl.lint_source(src, "test_fix.py")}

    def test_leaky_setup_flagged(self):
        f = thl.lint_source(LEAKY_SETUP, "test_fix.py")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].scope, "TestX")
        self.assertEqual(f[0].factory, "mkdtemp")

    def test_addcleanup_cleanup_is_clean(self):
        self.assertEqual(thl.lint_source(CLEAN_ADDCLEANUP_CLEANUP, "test_fix.py"), [])

    def test_addcleanup_rmtree_is_clean(self):
        self.assertEqual(thl.lint_source(CLEAN_ADDCLEANUP_RMTREE, "test_fix.py"), [])

    def test_teardown_rmtree_is_clean(self):
        self.assertEqual(thl.lint_source(CLEAN_TEARDOWN_RMTREE, "test_fix.py"), [])

    def test_with_context_manager_is_clean(self):
        self.assertEqual(thl.lint_source(CLEAN_WITH_CONTEXT, "test_fix.py"), [])

    def test_leaky_helper_flagged(self):
        f = thl.lint_source(LEAKY_HELPER, "test_fix.py")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].scope, "TestX")

    def test_helper_with_addcleanup_is_clean(self):
        self.assertEqual(thl.lint_source(CLEAN_HELPER_ADDCLEANUP, "test_fix.py"), [])

    def test_module_level_function_leak_flagged(self):
        f = thl.lint_source(LEAKY_MODULE_FUNC, "test_fix.py")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].scope, "build_fixture")

    def test_bare_import_factory_flagged(self):
        f = thl.lint_source(LEAKY_BARE_IMPORT, "test_fix.py")
        self.assertEqual(len(f), 1)

    def test_syntax_error_source_is_skipped(self):
        self.assertEqual(thl.lint_source("def (:\n", "test_fix.py"), [])


class TestExitCodeAndDict(unittest.TestCase):
    def test_exit_code_clean(self):
        self.assertEqual(thl.exit_code([]), 0)

    def test_exit_code_finding(self):
        f = thl.lint_source(LEAKY_SETUP, "test_fix.py")
        self.assertEqual(thl.exit_code(f), 1)

    def test_finding_to_dict(self):
        d = thl.lint_source(LEAKY_SETUP, "test_fix.py")[0].to_dict()
        self.assertEqual(set(d), {"file", "scope", "factory", "lineno"})


class TestLiveTreeRegressionGuard(unittest.TestCase):
    """The whole point: the committed tools/ tree must lint clean. Fails loudly if
    anyone adds a test temp-dir creation without registered cleanup (T-1307/1308 class)."""

    def test_live_tools_tree_is_clean(self):
        findings = thl.lint()
        self.assertEqual(
            findings, [],
            msg="uncleaned test temp-dir creation(s): "
                + "; ".join(f"{f.file}::{f.scope} L{f.lineno}" for f in findings),
        )


if __name__ == "__main__":
    unittest.main()
