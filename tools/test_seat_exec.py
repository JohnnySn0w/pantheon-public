"""Tests for seat_exec.seat_argv (T-4066) — the own-account-safe per-seat argv builder."""
import unittest
from unittest import mock

try:
    from tools import seat_exec as se
except ImportError:  # bare run with cwd=tools/
    import seat_exec as se  # type: ignore


class TestSeatArgv(unittest.TestCase):
    def test_own_account_runs_direct_no_sudo(self):
        with mock.patch.object(se.getpass, "getuser", return_value="recovery-acct"):
            self.assertEqual(se.seat_argv("recovery-acct", ["cat", "/x"]), ["cat", "/x"])

    def test_other_account_prefixes_sudo_n_u(self):
        with mock.patch.object(se.getpass, "getuser", return_value="recovery-acct"):
            self.assertEqual(
                se.seat_argv("claude", ["cat", "/x"]),
                ["sudo", "-n", "-u", "claude", "cat", "/x"])

    def test_custom_sudo_prefix(self):
        with mock.patch.object(se.getpass, "getuser", return_value="recovery-acct"):
            self.assertEqual(
                se.seat_argv("claude", ["git", "log"], sudo=("sudo", "-S", "-k", "-u")),
                ["sudo", "-S", "-k", "-u", "claude", "git", "log"])

    def test_argv_is_copied_not_mutated(self):
        with mock.patch.object(se.getpass, "getuser", return_value="me"):
            original = ["cat", "/x"]
            out = se.seat_argv("me", original)
            self.assertEqual(out, original)
            self.assertIsNot(out, original)  # returns a new list

    def test_bash_c_wrapping_is_the_callers_job(self):
        with mock.patch.object(se.getpass, "getuser", return_value="recovery-acct"):
            self.assertEqual(
                se.seat_argv("artisan", ["bash", "-c", "ls -t /x/*.jsonl"]),
                ["sudo", "-n", "-u", "artisan", "bash", "-c", "ls -t /x/*.jsonl"])

    def test_is_own_account(self):
        with mock.patch.object(se.getpass, "getuser", return_value="me"):
            self.assertTrue(se.is_own_account("me"))
            self.assertFalse(se.is_own_account("other"))


if __name__ == "__main__":
    unittest.main()
