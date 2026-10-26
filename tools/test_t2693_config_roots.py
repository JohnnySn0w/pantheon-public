"""Tests for tools/t2693_config_roots.py (T-2693). A missing codex writable root
bricks every codex shell, so the edit must be valid TOML in every array shape."""
import os
import sys
import tempfile
import tomllib
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import t2693_config_roots as cr  # noqa: E402

CODEX_SHAPE = '''[sandbox_workspace_write]
writable_roots = [
    # own inbox
    "/home/the-ancestor/mailbox/codex",   # own inbox (consume + ack)
    "/home/the-ancestor/mailbox/orchestrator",
    "/home/the-ancestor/mailbox/recovery"
]
'''
SPECIALIST_SHAPE = '''[sandbox_workspace_write]
network_access = true
writable_roots = [
  "/home/claude-research/.local/state/pantheon/personas/specialist",
  "/home/the-ancestor/mailbox/specialist",
  "/home/the-ancestor/mailbox/codex",
]
'''


def roots(text):
    return tomllib.loads(text)["sandbox_workspace_write"]["writable_roots"]


class AddTwinsTest(unittest.TestCase):
    def test_last_element_without_trailing_comma_gets_a_twin(self):
        text, personas = cr.add_toml_twins(CODEX_SHAPE)
        self.assertEqual(personas, ["codex", "orchestrator", "recovery"])
        r = roots(text)
        for p in personas:
            self.assertIn(f"/home/post-office/mailbox/{p}", r)
            self.assertIn(f"/home/the-ancestor/mailbox/{p}", r, "old roots are kept until phase 7")

    def test_trailing_comma_shape(self):
        text, personas = cr.add_toml_twins(SPECIALIST_SHAPE)
        self.assertEqual(len(roots(text)), 5)
        self.assertEqual(cr.check_toml(text, personas, check_access=False), [])

    def test_idempotent(self):
        once, _ = cr.add_toml_twins(CODEX_SHAPE)
        twice, _ = cr.add_toml_twins(once)
        self.assertEqual(once, twice)

    def test_no_old_roots_is_a_failure_not_a_silent_success(self):
        text, personas = cr.add_toml_twins('[x]\nwritable_roots = ["/tmp"]\n')
        self.assertTrue(cr.check_toml(text, personas, check_access=False))

    def test_missing_twin_is_reported(self):
        self.assertIn("missing twin /home/post-office/mailbox/recovery",
                      cr.check_toml(CODEX_SHAPE, ["recovery"], check_access=False))


class CodexTomlEndToEndTest(unittest.TestCase):
    """Hermetic: the new-hub prefix is pointed at a temp dir, never the live hub
    (whose accessibility depends on the account and on whether the cut has run)."""

    def _config(self, d):
        path = os.path.join(d, "config.toml")
        with open(path, "w") as fh:
            fh.write(CODEX_SHAPE)
        return path

    def test_missing_new_root_leaves_the_file_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._config(d)
            with unittest.mock.patch.object(cr, "NEW", os.path.join(d, "absent") + "/"):
                self.assertEqual(cr.codex_toml(path), 1)
            self.assertEqual(open(path).read(), CODEX_SHAPE)
            self.assertFalse(os.path.exists(path + ".bak.t2693"))

    def test_existing_new_roots_are_added_with_a_backup(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._config(d)
            hub = os.path.join(d, "hub")
            for p in ("codex", "orchestrator", "recovery"):
                os.makedirs(os.path.join(hub, p))
            with unittest.mock.patch.object(cr, "NEW", hub + "/"):
                self.assertEqual(cr.codex_toml(path), 0)
                self.assertIn(os.path.join(hub, "recovery"), roots(open(path).read()))
            self.assertEqual(open(path + ".bak.t2693").read(), CODEX_SHAPE)


if __name__ == "__main__":
    unittest.main()
