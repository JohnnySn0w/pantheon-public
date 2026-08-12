#!/usr/bin/env python3
"""Tests for main_guard_lint, plus the live-tree regression guard (T-2170)."""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.main_guard_lint as mgl

# --- fixtures: each is a self-contained module source string ---

DEFECTIVE_CLASS_AFTER_GUARD = '''
import unittest
class TestA(unittest.TestCase):
    def test_a(self):
        pass

if __name__ == "__main__":
    unittest.main()

class TestB(unittest.TestCase):
    def test_b(self):
        pass
'''

DEFECTIVE_MULTIPLE_AFTER_GUARD = '''
import unittest
if __name__ == "__main__":
    unittest.main()

class TestB(unittest.TestCase):
    pass

def helper():
    pass

async def ahelper():
    pass
'''

CLEAN_GUARD_LAST = '''
import unittest
class TestA(unittest.TestCase):
    def test_a(self):
        pass

if __name__ == "__main__":
    unittest.main()
'''

CLEAN_NO_GUARD = '''
import unittest
class TestA(unittest.TestCase):
    pass
'''

CLEAN_NESTED_DEF_INSIDE_GUARD = '''
import unittest

if __name__ == "__main__":
    def _only_for_cli():
        pass
    _only_for_cli()
'''

CLEAN_TRAILING_NON_DEF_STATEMENTS = '''
import sys

def main():
    return 0

if __name__ == "__main__":
    sys.exit(main())
'''

SINGLE_QUOTED_GUARD_DEFECTIVE = """
import unittest
if __name__ == '__main__':
    unittest.main()

class TestB(unittest.TestCase):
    pass
"""

REVERSED_GUARD_DEFECTIVE = '''
import unittest
if "__main__" == __name__:
    unittest.main()

class TestB(unittest.TestCase):
    pass
'''

CLEAN_METHOD_AFTER_GUARD_LINE_BUT_NESTED = '''
import unittest

class TestA(unittest.TestCase):
    def test_a(self):
        if __name__ == "__main__":
            pass

    def test_b(self):
        pass
'''


class TestLintSource(unittest.TestCase):
    def test_class_after_guard_flagged(self):
        f = mgl.lint_source(DEFECTIVE_CLASS_AFTER_GUARD, "test_fix.py")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].name, "TestB")
        self.assertEqual(f[0].kind, "class")

    def test_all_kinds_after_guard_flagged(self):
        f = mgl.lint_source(DEFECTIVE_MULTIPLE_AFTER_GUARD, "test_fix.py")
        self.assertEqual([(x.name, x.kind) for x in f],
                         [("TestB", "class"), ("helper", "def"), ("ahelper", "async def")])

    def test_guard_last_is_clean(self):
        self.assertEqual(mgl.lint_source(CLEAN_GUARD_LAST, "test_fix.py"), [])

    def test_no_guard_is_clean(self):
        self.assertEqual(mgl.lint_source(CLEAN_NO_GUARD, "test_fix.py"), [])

    def test_def_nested_inside_guard_body_is_clean(self):
        """Only TOP-LEVEL definitions matter; a def inside the guard body is fine."""
        self.assertEqual(mgl.lint_source(CLEAN_NESTED_DEF_INSIDE_GUARD, "test_fix.py"), [])

    def test_trailing_non_definition_statements_are_clean(self):
        self.assertEqual(mgl.lint_source(CLEAN_TRAILING_NON_DEF_STATEMENTS, "test_fix.py"), [])

    def test_single_quoted_guard_matched(self):
        """The guard is matched structurally, not by source text."""
        f = mgl.lint_source(SINGLE_QUOTED_GUARD_DEFECTIVE, "test_fix.py")
        self.assertEqual(len(f), 1)

    def test_reversed_comparison_guard_matched(self):
        f = mgl.lint_source(REVERSED_GUARD_DEFECTIVE, "test_fix.py")
        self.assertEqual(len(f), 1)

    def test_guard_nested_in_a_method_is_not_a_module_guard(self):
        """A guard inside a method must not make sibling methods look post-guard."""
        self.assertEqual(mgl.lint_source(CLEAN_METHOD_AFTER_GUARD_LINE_BUT_NESTED, "test_fix.py"), [])

    def test_known_exception_suppresses(self):
        mgl.KNOWN_EXCEPTIONS["test_fix.py::TestB"] = "fixture for this test"
        self.addCleanup(mgl.KNOWN_EXCEPTIONS.pop, "test_fix.py::TestB", None)
        self.assertEqual(mgl.lint_source(DEFECTIVE_CLASS_AFTER_GUARD, "test_fix.py"), [])

    def test_syntax_error_source_is_skipped(self):
        self.assertEqual(mgl.lint_source("def (:\n", "test_fix.py"), [])


class TestExitCodeAndDict(unittest.TestCase):
    def test_exit_code_clean(self):
        self.assertEqual(mgl.exit_code([]), 0)

    def test_exit_code_finding(self):
        f = mgl.lint_source(DEFECTIVE_CLASS_AFTER_GUARD, "test_fix.py")
        self.assertEqual(mgl.exit_code(f), 1)

    def test_finding_to_dict(self):
        d = mgl.lint_source(DEFECTIVE_CLASS_AFTER_GUARD, "test_fix.py")[0].to_dict()
        self.assertEqual(set(d), {"file", "name", "kind", "lineno", "guard_lineno"})


class TestLiveTreeRegressionGuard(unittest.TestCase):
    """The whole point: the committed tools/ tree must lint clean. Fails loudly if
    anyone lands a top-level definition below a __main__ guard (T-2170 class)."""

    def test_live_tools_tree_is_clean(self):
        findings = mgl.lint()
        self.assertEqual(
            findings, [],
            msg="top-level definition(s) below a __main__ guard: "
                + "; ".join(f"{f.file}::{f.name} L{f.lineno}" for f in findings),
        )


class TestCountStabilityOfThisFile(unittest.TestCase):
    """T-2170 self-application: this file's own two invocations must agree.

    Cheap structural proof — if this module ever grows a post-guard definition, the
    live-tree guard above catches it, and this asserts the same for itself explicitly.
    """

    def test_this_module_is_clean(self):
        src = Path(__file__).read_text()
        self.assertEqual(mgl.lint_source(src, Path(__file__).name), [])


if __name__ == "__main__":
    unittest.main()
