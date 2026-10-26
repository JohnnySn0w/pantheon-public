"""Tests for tools.t2582_live_telegram_accept (T-2582).

SEND-ISOLATION: every test injects a fake SendFn or a plain dict for env --
no test in this suite ever calls make_live_send_fn or main()'s live branch,
mirroring test_bringup_acceptance_telegram_outbound.py's own SEND-ISOLATION
posture. The gate env var (T2582_LIVE_TELEGRAM_ACCEPT) is never set here.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.bringup_acceptance.telegram_outbound import SendRequest, SendResult
from tools.bringup_acceptance.types import HardStop
from tools.t2582_live_telegram_accept import (
    DEDICATED_TEST_CHAT_ID,
    GATE_ENV,
    AcceptanceSkipped,
    read_token,
    refuse_operator_chat,
    require_live_gate,
    run_live_accept,
)

REAL_OPERATOR_CHAT_ID = "424242424"


def _ok_send(*, message_id: str = "6", chat_id: str | None = None):
    def _send(request: SendRequest) -> SendResult:
        return SendResult(
            ok=True,
            message_id=message_id,
            chat_id=chat_id if chat_id is not None else request.chat_id,
            reply_to_message_id=None,
            error_kind=None,
            raw={"ok": True, "result": {"message_id": message_id}},
        )

    return _send


def _failing_send(*, error_kind: str = "network_error"):
    def _send(request: SendRequest) -> SendResult:
        return SendResult(
            ok=False, message_id=None, chat_id=None, reply_to_message_id=None,
            error_kind=error_kind, raw={"ok": False, "error_kind": error_kind},
        )

    return _send


def _no_message_id_send():
    def _send(request: SendRequest) -> SendResult:
        return SendResult(
            ok=True, message_id=None, chat_id=request.chat_id, reply_to_message_id=None,
            error_kind=None, raw={"ok": True, "result": {}},
        )

    return _send


class RequireLiveGateTest(unittest.TestCase):
    def test_skips_when_gate_absent(self) -> None:
        with self.assertRaises(AcceptanceSkipped):
            require_live_gate({})

    def test_skips_when_gate_is_a_falsy_value(self) -> None:
        with self.assertRaises(AcceptanceSkipped):
            require_live_gate({GATE_ENV: "0"})

    def test_passes_for_each_truthy_value(self) -> None:
        for value in ("1", "true", "yes"):
            with self.subTest(value=value):
                require_live_gate({GATE_ENV: value})  # must not raise


class RefuseOperatorChatTest(unittest.TestCase):
    def test_hard_stops_on_the_real_operator_chat_id(self) -> None:
        with self.assertRaises(HardStop):
            refuse_operator_chat(REAL_OPERATOR_CHAT_ID)

    def test_no_op_on_the_dedicated_test_chat(self) -> None:
        refuse_operator_chat(DEDICATED_TEST_CHAT_ID)  # must not raise


class RunLiveAcceptTest(unittest.TestCase):
    def test_returns_message_id_and_chat_id_on_a_clean_accept(self) -> None:
        result = run_live_accept(send=_ok_send(), chat_id=DEDICATED_TEST_CHAT_ID)
        self.assertEqual(result.message_id, "6")
        self.assertEqual(result.chat_id, DEDICATED_TEST_CHAT_ID)

    def test_refuses_the_real_operator_chat_before_ever_sending(self) -> None:
        with self.assertRaises(HardStop):
            run_live_accept(send=_ok_send(), chat_id=REAL_OPERATOR_CHAT_ID)

    def test_hard_stops_when_send_reports_not_ok(self) -> None:
        with self.assertRaises(HardStop):
            run_live_accept(send=_failing_send(), chat_id=DEDICATED_TEST_CHAT_ID)

    def test_hard_stops_when_message_id_is_missing(self) -> None:
        with self.assertRaises(HardStop):
            run_live_accept(send=_no_message_id_send(), chat_id=DEDICATED_TEST_CHAT_ID)

    def test_hard_stops_when_response_chat_id_does_not_match(self) -> None:
        with self.assertRaises(HardStop):
            run_live_accept(
                send=_ok_send(chat_id="-999999"), chat_id=DEDICATED_TEST_CHAT_ID
            )


class ReadTokenTest(unittest.TestCase):
    def test_reads_and_strips_the_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "token"
            token_path.write_text("abc123:XYZ\n", encoding="utf-8")
            self.assertEqual(read_token(token_path), "abc123:XYZ")


if __name__ == "__main__":
    unittest.main()
