"""Tests for tools/a2a/orchestrator_client.py (T-1887, rung-1 orchestrator-seat A2A client).

Run from repo root:
    ~/.venv-a2a/bin/python -m unittest tools.test_a2a_orchestrator_client -v

Spins up the real specialist_seat_server (T-1886) in-process on an ephemeral
loopback port and drives it with orchestrator_client's own client-building code —
same seam as tools/test_a2a_specialist_seat_server.py's `_LiveServerTestCase`,
duplicated locally (not imported) to keep the two test modules independent.

Dependency note: `a2a-sdk`/`uvicorn`/`httpx` are installed worktree-scoped
per D11 (see tools/a2a/PROVISION.md — orchestrator-side venv at ~/.venv-a2a,
mirroring the server's own venv pattern), not into the repo's system
python3. The whole-suite smoke command (`python3 -m unittest discover -s
tools -p 'test_*.py'`) runs under system python3, which won't have them —
every test class here is skipped cleanly in that run (via `_SKIP_REASON`
below) rather than erroring, mirroring T-1886's pattern.

T-1890 (BT-5) note: `_LiveServerTestCase.setUpClass` below builds its
server with a per-class TEMP `request_inbox`/`result_inbox` (never the
live `/home/the-ancestor/mailbox/` boxes) — mandatory now that the real
handler actually calls `mailbox.send()` on a passing request; the BT-1..4
stub never touched the mailbox at all. A passing (non-above-bar) request
now stages and stays WORKING rather than completing synchronously (no
specialist session drains these tests' temp request inbox), so
`TestRoundTrip`/`TestPauseResumeCancelHelpers.test_resume_task_...` use a
short explicit `poll_timeout_s` override (instead of `send_and_follow`'s
120s default) to stay fast and assert WORKING rather than COMPLETED.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_SKIP_REASON: str | None = None

try:
    import uvicorn
    from google.protobuf.json_format import MessageToDict

    from a2a.types import a2a_pb2 as pb

    from tools.a2a import specialist_seat_server as server_mod
    from tools.a2a import orchestrator_client
    from tools.a2a._test_mailbox_guard import FORBIDDEN_PREFIX, guard_live_mailbox
except ImportError as exc:  # pragma: no cover - exercised under system python3
    _SKIP_REASON = (
        f"a2a-sdk/uvicorn not importable under this interpreter ({exc}); "
        f"run with ~/.venv-a2a/bin/python instead (see tools/a2a/PROVISION.md)"
    )

# Kept short (seconds, not the client's 120s production default) so tests
# that observe the new "stages and stays WORKING" outcome don't wait out
# the full production poll timeout for a state that will never resolve in
# this test environment (no live specialist session drains the temp inbox).
_TEST_POLL_TIMEOUT_S = 2.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipIf(_SKIP_REASON is not None, _SKIP_REASON)
class _LiveServerTestCase(unittest.IsolatedAsyncioTestCase):
    """Base class: spins up the real specialist stub server on an ephemeral
    loopback port. Its served Agent Card still declares the fixed
    production url (127.0.0.1:41100, per agent_card.json) — that mismatch
    is exactly what `orchestrator_client.build_client`'s resolve-then-rewrite is
    for; see orchestrator_client.py's DECISIONS-worthy design note."""

    @classmethod
    def setUpClass(cls) -> None:
        # T-1941 structural guard: this is the exact class shape whose
        # earlier, unguarded version caused the containment defect (a
        # bare `build_app()` call defaulted to the live mailbox paths on
        # this file's above-bar-then-resume tests). Guard first, for the
        # class's whole lifetime -- see _test_mailbox_guard's docstring.
        cls._mailbox_guard = guard_live_mailbox()
        cls._mailbox_guard.__enter__()

        cls._tmp = tempfile.TemporaryDirectory()
        request_inbox = Path(cls._tmp.name) / "specialist-inbox"
        result_inbox = Path(cls._tmp.name) / "specialist-a2a-inbox"
        for inbox in (request_inbox, result_inbox):
            assert not str(inbox).startswith(FORBIDDEN_PREFIX), (
                f"test-configured inbox {inbox} resolves under the live "
                f"mailbox substrate root {FORBIDDEN_PREFIX!r} -- must be a "
                f"temp path (T-1941)"
            )

        cls.port = _free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        app = server_mod.build_app(
            request_inbox=str(request_inbox), result_inbox=str(result_inbox)
        )
        config = uvicorn.Config(
            app, host="127.0.0.1", port=cls.port, log_level="warning"
        )
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not cls.server.started:
            raise RuntimeError("test server did not start in time")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls._tmp.cleanup()
        cls._mailbox_guard.__exit__(None, None, None)


@unittest.skipIf(_SKIP_REASON is not None, _SKIP_REASON)
class TestRoundTrip(_LiveServerTestCase):
    """T-1890 (BT-5): a passing request no longer completes synchronously
    with a canned stub artifact (T-1886..T-1888 behavior) — it stages to
    the (temp, per-class) request inbox and stays WORKING. Terminal-state
    round-trip coverage now lives in
    tools/test_a2a_bt5_research_handoff.py's result-poll-loop tests; this
    test's job is to prove the CLIENT observes that staged/WORKING outcome
    cleanly, not hanging or misreporting it as an error."""

    async def test_client_stages_request_and_reports_working(self) -> None:
        client = await orchestrator_client.build_client(self.base_url)
        try:
            message = orchestrator_client.build_message(
                question="What changed in the MCP spec?",
                depth="quick",
                domains_hint=["modelcontextprotocol.io"],
                recency_days=30,
                requester_context="T-1887 in-process round-trip test",
            )
            request = pb.SendMessageRequest(message=message)
            task = await orchestrator_client.send_and_follow(
                client, request, poll_timeout_s=_TEST_POLL_TIMEOUT_S
            )
        finally:
            await client.close()

        self.assertEqual(task.status.state, pb.TaskState.TASK_STATE_WORKING)
        self.assertEqual(len(task.artifacts), 0)


ABOVE_BAR_QUESTION = "What are the credentials for the specialist seat?"


@unittest.skipIf(_SKIP_REASON is not None, _SKIP_REASON)
class TestPauseResumeCancelHelpers(_LiveServerTestCase):
    """T-1889 (BT-4): orchestrator_client.resume_task/cancel_task unit-level
    coverage against a hand-driven paused task, mirroring TestRoundTrip's
    pattern (separate from the end-to-end main_async coverage below)."""

    async def _pause(self, client):
        message = orchestrator_client.build_message(
            question=ABOVE_BAR_QUESTION,
            depth="quick",
            domains_hint=None,
            recency_days=None,
            requester_context="T-1889 pause/resume test",
        )
        request = pb.SendMessageRequest(message=message)
        task = await orchestrator_client.send_and_follow(client, request)
        self.assertEqual(
            task.status.state, pb.TaskState.TASK_STATE_INPUT_REQUIRED
        )
        return task

    async def test_resume_task_returns_to_working(self) -> None:
        """T-1890 (BT-5): resume now stages (not completes) — inlines
        `resume_task`'s own steps (`build_resume_message` +
        `send_and_follow`) with a short `poll_timeout_s` instead of calling
        `resume_task` directly, since that convenience wrapper doesn't
        expose a timeout override and the production 120s default would
        make this test needlessly slow for an outcome (WORKING) that never
        resolves in this test environment (no live specialist session)."""
        client = await orchestrator_client.build_client(self.base_url)
        try:
            paused = await self._pause(client)
            resume_message = orchestrator_client.build_resume_message(
                paused, text="approved"
            )
            resumed = await orchestrator_client.send_and_follow(
                client,
                pb.SendMessageRequest(message=resume_message),
                poll_timeout_s=_TEST_POLL_TIMEOUT_S,
            )
        finally:
            await client.close()
        self.assertEqual(resumed.status.state, pb.TaskState.TASK_STATE_WORKING)
        self.assertEqual(len(resumed.artifacts), 0)

    async def test_cancel_task_drives_canceled(self) -> None:
        client = await orchestrator_client.build_client(self.base_url)
        try:
            paused = await self._pause(client)
            canceled = await orchestrator_client.cancel_task(client, paused)
        finally:
            await client.close()
        self.assertEqual(
            canceled.status.state, pb.TaskState.TASK_STATE_CANCELED
        )


@unittest.skipIf(_SKIP_REASON is not None, _SKIP_REASON)
class TestMainAsyncOnPause(_LiveServerTestCase):
    """T-1889 (BT-4): end-to-end CLI coverage of --on-pause against an
    above-bar question, driven through a single main_async invocation
    (orchestrator-as-human at the client, per design notes §2.4's rung boundary —
    no Telegram)."""

    async def test_on_pause_resume_drives_working_and_rc0(self) -> None:
        """T-1890 (BT-5) + T-1942: resume stages and stays WORKING rather
        than completing, and under the T-1942 exit-code contract that is a
        SUCCESS of the submission (rc==0) — the request was accepted and
        staged for async research. `--poll-timeout-s` keeps the poll short
        (the task will not settle: WORKING is the expected outcome, so
        waiting the production 120s buys nothing — this was the exact
        reason the pre-T-1942 version of this test was skipped)."""
        rc = await orchestrator_client.main_async(
            [
                "--base-url",
                self.base_url,
                "--question",
                ABOVE_BAR_QUESTION,
                "--on-pause",
                "resume",
                "--poll-timeout-s",
                "2",
            ]
        )
        self.assertEqual(rc, 0)

    async def test_on_pause_cancel_drives_canceled(self) -> None:
        rc = await orchestrator_client.main_async(
            [
                "--base-url",
                self.base_url,
                "--question",
                ABOVE_BAR_QUESTION,
                "--on-pause",
                "cancel",
            ]
        )
        self.assertEqual(rc, 0)

    async def test_on_pause_report_default_preserves_bt2_bt3_behavior(
        self,
    ) -> None:
        """Regression: default --on-pause (report) on a NOW-actually-pausing
        request still exits cleanly without hanging or attempting to
        resume/cancel — same contract BT-2/BT-3 established, now exercised
        against a real pause instead of a request that never paused."""
        rc = await orchestrator_client.main_async(
            [
                "--base-url",
                self.base_url,
                "--question",
                ABOVE_BAR_QUESTION,
            ]
        )
        self.assertEqual(rc, 1)


@unittest.skipIf(_SKIP_REASON is not None, _SKIP_REASON)
class TestUnreachableServer(unittest.IsolatedAsyncioTestCase):
    async def test_main_async_clean_error_no_traceback_leak(self) -> None:
        dead_port = _free_port()  # bound-then-released; nothing listens
        base_url = f"http://127.0.0.1:{dead_port}"
        rc = await orchestrator_client.main_async(["--base-url", base_url])
        self.assertEqual(rc, 1)


@unittest.skipIf(_SKIP_REASON is not None, _SKIP_REASON)
class TestResolveBaseUrl(unittest.TestCase):
    def test_default_is_production_loopback(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(orchestrator_client.BASE_URL_ENV, None)
            self.assertEqual(
                orchestrator_client.resolve_base_url(), orchestrator_client.DEFAULT_BASE_URL
            )

    def test_env_override_wins(self) -> None:
        with patch.dict(
            os.environ,
            {orchestrator_client.BASE_URL_ENV: "http://127.0.0.1:9999"},
            clear=False,
        ):
            self.assertEqual(
                orchestrator_client.resolve_base_url(), "http://127.0.0.1:9999"
            )


if __name__ == "__main__":
    unittest.main()
