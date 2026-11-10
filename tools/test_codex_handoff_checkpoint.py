"""T-6115 D-1: codex seats write a fresh HANDOFF at idle checkpoints.

Intent: the org record (HANDOFF -> Chronicle), not the codex thread rollout, is a codex
seat's durable memory, so a thread can be bounded/rotated without losing context. Nothing
asked consultant to write a HANDOFF (stale since 2026-10-17); this checkpoint does, through the
seat's OWN mailbox (idle-drained by the app-server delivery path, never pane injection),
and treats the HANDOFF mtime moving as the ack (tools.handoff_first.request_handoff).

Hermetic: tmp home, fake mailbox send, fake clock. Nothing touches a live seat.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from tools import codex_handoff_checkpoint as chk
from tools import handoff_first


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        self.slept += s


class CheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.handoff = self.home / "HANDOFF.md"
        self.sent: list[tuple[str, str, str]] = []
        self.clock = Clock()

    def age(self, hours: float) -> None:
        self.handoff.write_text("old\n")
        t = time.time() - hours * 3600
        os.utime(self.handoff, (t, t))

    def send(self, writes_handoff: bool = True):
        def _send(inbox: str, payload: str, subject: str) -> str:
            self.sent.append((inbox, subject, payload))
            if writes_handoff:
                self.handoff.write_text("fresh\n")
            return "mid"
        return _send

    def run_chk(self, **kw):
        return chk.checkpoint(
            persona=kw.pop("persona", "codex"), home=self.home,
            send=kw.pop("send", self.send()), now=kw.pop("now", time.time()),
            sleep=self.clock.sleep, monotonic=self.clock.monotonic, **kw)

    def test_fresh_handoff_is_a_noop_and_sends_nothing(self) -> None:
        self.age(2)
        out = self.run_chk()
        self.assertEqual(out.status, "fresh")
        self.assertEqual(self.sent, [])
        self.assertEqual(out.exit_code, 0)

    def test_stale_handoff_is_requested_via_own_inbox_and_acked(self) -> None:
        self.age(30)
        out = self.run_chk()
        self.assertEqual(out.status, "written")
        self.assertEqual(out.exit_code, 0)
        [(inbox, subject, payload)] = self.sent
        self.assertEqual(inbox, "/home/post-office/mailbox/codex")
        self.assertIn("checkpoint", subject.lower())
        self.assertIn(str(self.handoff), payload)
        self.assertNotIn("about to end this session", payload)

    def test_missing_handoff_counts_as_stale(self) -> None:
        out = self.run_chk()
        self.assertEqual(out.status, "written")
        self.assertEqual(len(self.sent), 1)

    def test_no_ack_within_timeout_is_soft_failure_exit_3(self) -> None:
        self.age(30)
        out = self.run_chk(send=self.send(writes_handoff=False), ack_timeout_seconds=60)
        self.assertEqual(out.status, "no-ack")
        self.assertEqual(out.exit_code, 3)
        self.assertGreaterEqual(self.clock.slept, 60)

    def test_send_failure_is_not_confirmed_and_never_raises(self) -> None:
        self.age(30)

        def boom(*_a):
            raise PermissionError("hub")
        out = self.run_chk(send=boom)
        self.assertEqual(out.status, "no-ack")
        self.assertIn("not delivered", out.reason)

    def test_watchman_uses_its_own_inbox(self) -> None:
        self.age(30)
        self.run_chk(persona="watchman")
        self.assertEqual(self.sent[0][0], "/home/post-office/mailbox/watchman")

    def test_unknown_persona_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.run_chk(persona="orchestrator")

    def test_max_age_boundary(self) -> None:
        self.age(23.9)
        self.assertEqual(self.run_chk(max_age_hours=24).status, "fresh")
        self.age(24.1)
        self.assertEqual(self.run_chk(max_age_hours=24).status, "written")


class RealSenderTests(unittest.TestCase):
    def test_real_sender_uses_the_pantheon_mailbox_not_stdlib(self) -> None:
        from unittest import mock

        with mock.patch("tools.mailbox.send", return_value="mid") as send:
            chk._mailbox_send("/home/post-office/mailbox/codex", "body", "subj")
        send.assert_called_once_with("/home/post-office/mailbox/codex", "body", "checkpoint", subject="subj")


class AccountHomeTests(unittest.TestCase):
    def test_home_comes_from_passwd_not_HOME(self) -> None:
        import pwd
        from unittest import mock

        with mock.patch.dict(os.environ, {"HOME": "/home/someone-else"}):
            self.assertEqual(chk.account_home(), Path(pwd.getpwuid(os.geteuid()).pw_dir))


class ResolvePersonaTests(unittest.TestCase):
    """T-6125: the persona is the running account's persona (instance.yaml), so one unit
    template instanced on the ACCOUNT serves consultant (codex) and watchman (codex-fallback)."""

    def test_derived_from_the_account_when_not_passed(self) -> None:
        self.assertEqual(chk.resolve_persona(None, account="codex"), "codex")
        self.assertEqual(chk.resolve_persona(None, account="codex-fallback"), "watchman")

    def test_explicit_persona_that_agrees_is_accepted(self) -> None:
        self.assertEqual(chk.resolve_persona("codex", account="codex"), "codex")
        self.assertEqual(chk.resolve_persona("watchman", account="codex-fallback"), "watchman")

    def test_explicit_persona_that_disagrees_is_refused(self) -> None:
        with self.assertRaises(chk.PersonaError):
            chk.resolve_persona("watchman", account="codex")
        with self.assertRaises(chk.PersonaError):
            chk.resolve_persona("codex", account="codex-fallback")

    def test_unknown_or_non_codex_account_is_refused(self) -> None:
        for acct in ("runner", "claude-fallback", "claude"):  # recovery's account maps to a non-codex persona
            with self.subTest(acct=acct), self.assertRaises(chk.PersonaError):
                chk.resolve_persona(None, account=acct)

    def test_main_runs_with_no_persona_flag(self) -> None:
        from unittest import mock
        with mock.patch.object(chk, "current_account", return_value="codex-fallback"), \
                mock.patch.object(chk, "checkpoint", return_value=chk.Outcome("fresh", "ok", 0)) as cp:
            self.assertEqual(chk.main([]), 0)
        self.assertEqual(cp.call_args.kwargs["persona"], "watchman")


class RequestHandoffPromptTests(unittest.TestCase):
    def test_default_prompt_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            got = []
            path = Path(d) / "HANDOFF.md"
            handoff_first.request_handoff(path, got.append, timeout_seconds=0,
                                          sleep=lambda s: None, monotonic=lambda: 0.0)
            self.assertEqual(got, [handoff_first.HANDOFF_PROMPT_TEMPLATE.format(path=path)])

    def test_custom_prompt_is_formatted_with_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            got = []
            path = Path(d) / "HANDOFF.md"
            handoff_first.request_handoff(path, got.append, timeout_seconds=0, prompt="write {path} now",
                                          sleep=lambda s: None, monotonic=lambda: 0.0)
            self.assertEqual(got, [f"write {path} now"])


if __name__ == "__main__":
    unittest.main()
