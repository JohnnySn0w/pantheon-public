"""Tests for tools.bringup_acceptance.telegram_outbound (T-2556 phase 10 / T-2568).

SEND-ISOLATION: every test injects a fake SendFn — no test in this suite ever
makes a real network call or reads the real bot token/chat. See the module
docstring's SEND-ISOLATION section for why this is deliberate (no dedicated
non-operator test chat_id exists yet) and why make_live_send_fn is NOT
exercised here (mirrors make_live_fire_harness's own "not exercised by the
test suite" precedent).
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tools.bringup_acceptance.telegram_outbound import (
    BOT_LOCK_CONTENTION,
    MCP_TOOL_EVICTION,
    MODULE_ID,
    SINGLE_POLLER_VIOLATION,
    UNKNOWN_FAILURE,
    WRONG_CHAT_ID,
    SendRequest,
    SendResult,
    TelegramOutboundModule,
    assert_chat_id_matches,
    classify_bot_lock,
    classify_send_error,
    prove_mcp_tool_eviction_classifier_guard,
    prove_single_poller_guard,
)
from tools.bringup_acceptance.evidence import RunPaths
from tools.bringup_acceptance.types import HardStop, InventoryPathEntry, RunContext, TargetInventory

PERSONA = "testpersona"
DISPOSABLE_CHAT_ID = "-1009999999999"  # a disposable/non-operator target, never the operator's real chat
REAL_OPERATOR_CHAT_ID = "424242424"


def _run_context(root: Path, *, run_id: str = "20260805T000000Z") -> RunContext:
    return RunContext(
        run_id=run_id, persona=PERSONA, account="test-account", harness="claude",
        profile="interactive", hub_path=root / "hub", state_path=root / "state",
        evidence_root=root / "evidence", baseline_ref="deadbeef" * 5,
    )


def _ok_send(*, message_id: str = "1001", chat_id: str | None = None, echo_reply_to: bool = True):
    """A fake SendFn that always accepts, echoing the intended chat_id back
    (unless overridden, to reproduce a wrong-chat_id response) and echoing
    reply_to_message_id back (unless echo_reply_to=False, to reproduce a
    silently-dropped threading correlation)."""

    def _send(request: SendRequest) -> SendResult:
        return SendResult(
            ok=True,
            message_id=message_id,
            chat_id=chat_id if chat_id is not None else request.chat_id,
            reply_to_message_id=request.reply_to_message_id if echo_reply_to else None,
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


def _module(
    root: Path,
    *,
    send=None,
    chat_id: str = DISPOSABLE_CHAT_ID,
    bot_pid_path: Path | None = None,
    process_alive=lambda pid: True,
) -> TelegramOutboundModule:
    return TelegramOutboundModule(
        send=send if send is not None else _ok_send(),
        chat_id=chat_id,
        bot_pid_path=bot_pid_path if bot_pid_path is not None else root / "no-such-bot.pid",
        process_alive=process_alive,
    )


# --------------------------------------------------------------------- send-isolation


class SendIsolationTest(unittest.TestCase):
    def test_hard_stops_on_construction_against_the_real_operator_chat_id(self) -> None:
        with self.assertRaises(HardStop):
            TelegramOutboundModule(
                send=_ok_send(), chat_id=REAL_OPERATOR_CHAT_ID,
                bot_pid_path=Path("/nonexistent/bot.pid"),
            )

    def test_accepts_a_disposable_chat_id(self) -> None:
        module = _module(Path("/tmp"))
        self.assertEqual(module.id, MODULE_ID)


# --------------------------------------------------------------------- declare


class DeclareTest(unittest.TestCase):
    def test_applies_when_inventory_marks_telegram_outbound_present(self) -> None:
        module = _module(Path("/tmp"))
        inventory = TargetInventory(
            persona=PERSONA,
            entries=(InventoryPathEntry(path=Path("x"), tier="core", status="applies", module_id=MODULE_ID),),
        )
        decl = module.declare(inventory)
        self.assertTrue(decl.applies)
        self.assertEqual(decl.tier, "core")

    def test_does_not_apply_when_inventory_marks_absent(self) -> None:
        module = _module(Path("/tmp"))
        inventory = TargetInventory(
            persona=PERSONA,
            entries=(InventoryPathEntry(path=Path("x"), tier="core", status="absent", module_id=MODULE_ID),),
        )
        decl = module.declare(inventory)
        self.assertFalse(decl.applies)

    def test_does_not_apply_with_no_matching_entry(self) -> None:
        module = _module(Path("/tmp"))
        decl = module.declare(TargetInventory(persona=PERSONA, entries=()))
        self.assertFalse(decl.applies)
        self.assertIn(f"no {MODULE_ID} inventory entry", decl.reason)


# --------------------------------------------------------------------- prove: send-accept


class ProveSendAcceptTest(unittest.TestCase):
    def test_proves_unthreaded_and_threaded_send_accept(self) -> None:
        with _TmpDirCtx() as root:
            module = _module(root, send=_ok_send())
            evidence = module.prove(_run_context(root))
            self.assertEqual(evidence.status, "proven")
            self.assertEqual(evidence.detail["unthreaded"]["message_id"], "1001")
            self.assertEqual(evidence.detail["unthreaded"]["chat_id"], DISPOSABLE_CHAT_ID)
            self.assertIsNone(evidence.detail["unthreaded"]["reply_to_message_id"])
            self.assertEqual(evidence.detail["threaded"]["chat_id"], DISPOSABLE_CHAT_ID)
            self.assertIsNotNone(evidence.detail["threaded"]["reply_to_message_id"])

    def test_send_not_ok_raises_hard_stop(self) -> None:
        with _TmpDirCtx() as root:
            module = _module(root, send=_failing_send())
            with self.assertRaises(HardStop):
                module.prove(_run_context(root))

    def test_missing_message_id_raises_hard_stop(self) -> None:
        with _TmpDirCtx() as root:
            module = _module(root, send=_no_message_id_send())
            with self.assertRaises(HardStop):
                module.prove(_run_context(root))

    def test_wrong_chat_id_response_raises_hard_stop_naming_the_guard(self) -> None:
        with _TmpDirCtx() as root:
            module = _module(root, send=_ok_send(chat_id="-1000000000000"))
            with self.assertRaises(HardStop) as caught:
                module.prove(_run_context(root))
            self.assertIn(WRONG_CHAT_ID, str(caught.exception))

    def test_threaded_reply_to_not_echoed_raises_hard_stop(self) -> None:
        with _TmpDirCtx() as root:
            module = _module(root, send=_ok_send(echo_reply_to=False))
            with self.assertRaises(HardStop):
                module.prove(_run_context(root))


class _TmpDirCtx:
    def __enter__(self) -> Path:
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        return Path(self._td.name)

    def __exit__(self, *exc) -> None:
        self._td.cleanup()


# --------------------------------------------------------------------- assert_chat_id_matches


class AssertChatIdMatchesTest(unittest.TestCase):
    def test_passes_on_a_matching_chat_id(self) -> None:
        result = SendResult(
            ok=True, message_id="1", chat_id=DISPOSABLE_CHAT_ID, reply_to_message_id=None,
            error_kind=None, raw={},
        )
        assert_chat_id_matches(result=result, intended_chat_id=DISPOSABLE_CHAT_ID)  # no raise

    def test_hard_stops_on_a_mismatched_chat_id(self) -> None:
        result = SendResult(
            ok=True, message_id="1", chat_id="-1000000000000", reply_to_message_id=None,
            error_kind=None, raw={},
        )
        with self.assertRaises(HardStop) as caught:
            assert_chat_id_matches(result=result, intended_chat_id=DISPOSABLE_CHAT_ID)
        self.assertIn(WRONG_CHAT_ID, str(caught.exception))


# --------------------------------------------------------------------- bot-lock-contention guard


class ClassifyBotLockTest(unittest.TestCase):
    def test_absent_when_no_pid_text(self) -> None:
        self.assertEqual(classify_bot_lock(pid_text=None, process_alive=lambda pid: True), "absent")
        self.assertEqual(classify_bot_lock(pid_text="   ", process_alive=lambda pid: True), "absent")

    def test_unreadable_when_pid_text_is_not_numeric(self) -> None:
        self.assertEqual(
            classify_bot_lock(pid_text="not-a-pid", process_alive=lambda pid: True), "unreadable",
        )

    def test_alive_when_process_alive_reports_true(self) -> None:
        self.assertEqual(classify_bot_lock(pid_text="12345", process_alive=lambda pid: True), "alive")

    def test_locked_dead_when_process_alive_reports_false(self) -> None:
        self.assertEqual(
            classify_bot_lock(pid_text="12345", process_alive=lambda pid: False), "locked_dead",
        )


class ProveBotLockGuardTest(unittest.TestCase):
    def test_prove_succeeds_when_bot_pid_is_absent(self) -> None:
        with _TmpDirCtx() as root:
            module = _module(root, bot_pid_path=root / "no-such-bot.pid")
            evidence = module.prove(_run_context(root))
            self.assertEqual(evidence.detail["bot_lock_contention_guard"]["status"], "absent")

    def test_prove_hard_stops_on_a_locked_dead_bot_pid(self) -> None:
        with _TmpDirCtx() as root:
            pid_path = root / "bot.pid"
            pid_path.write_text("99999\n", encoding="utf-8")
            module = _module(root, bot_pid_path=pid_path, process_alive=lambda pid: False)
            with self.assertRaises(HardStop) as caught:
                module.prove(_run_context(root))
            self.assertIn(BOT_LOCK_CONTENTION, str(caught.exception))

    def test_prove_succeeds_when_bot_pid_process_is_alive(self) -> None:
        with _TmpDirCtx() as root:
            pid_path = root / "bot.pid"
            pid_path.write_text("12345\n", encoding="utf-8")
            module = _module(root, bot_pid_path=pid_path, process_alive=lambda pid: True)
            evidence = module.prove(_run_context(root))
            self.assertEqual(evidence.detail["bot_lock_contention_guard"]["status"], "alive")


# --------------------------------------------------------------------- single-poller-violation guard


class ProveSinglePollerGuardTest(unittest.TestCase):
    def test_reproduces_a_409_conflict_for_a_second_concurrent_poller(self) -> None:
        detail = prove_single_poller_guard()
        self.assertIn("409", detail["second_poller_conflict"])
        self.assertIn("in-memory race", detail["scope"])

    def test_is_independent_of_the_send_accept_seam(self) -> None:
        # The guard never touches SendFn — proving it alongside a normal
        # send-accept run demonstrates the two do not share state.
        with _TmpDirCtx() as root:
            module = _module(root, send=_ok_send())
            evidence = module.prove(_run_context(root))
            self.assertEqual(evidence.status, "proven")
            self.assertIn("second_poller_conflict", evidence.detail["single_poller_violation_guard"])


# --------------------------------------------------------------------- mcp-tool-eviction guard


class ClassifySendErrorTest(unittest.TestCase):
    def test_maps_401_unauthorized_to_mcp_tool_eviction(self) -> None:
        result = SendResult(
            ok=False, message_id=None, chat_id=None, reply_to_message_id=None,
            error_kind="http_401_unauthorized", raw={},
        )
        self.assertEqual(classify_send_error(result), MCP_TOOL_EVICTION)

    def test_maps_409_conflict_to_single_poller_violation(self) -> None:
        result = SendResult(
            ok=False, message_id=None, chat_id=None, reply_to_message_id=None,
            error_kind="http_409_conflict", raw={},
        )
        self.assertEqual(classify_send_error(result), SINGLE_POLLER_VIOLATION)

    def test_maps_bot_lock_dead_pid_to_bot_lock_contention(self) -> None:
        result = SendResult(
            ok=False, message_id=None, chat_id=None, reply_to_message_id=None,
            error_kind="bot_lock_dead_pid", raw={},
        )
        self.assertEqual(classify_send_error(result), BOT_LOCK_CONTENTION)

    def test_maps_unrecognized_error_kind_to_unknown(self) -> None:
        result = SendResult(
            ok=False, message_id=None, chat_id=None, reply_to_message_id=None,
            error_kind="something_else", raw={},
        )
        self.assertEqual(classify_send_error(result), UNKNOWN_FAILURE)

    def test_hard_stops_when_called_on_an_ok_result(self) -> None:
        result = SendResult(
            ok=True, message_id="1", chat_id=DISPOSABLE_CHAT_ID, reply_to_message_id=None,
            error_kind=None, raw={},
        )
        with self.assertRaises(HardStop):
            classify_send_error(result)


class ProveMcpToolEvictionClassifierGuardTest(unittest.TestCase):
    def test_classifies_the_synthetic_401_fixture_as_mcp_tool_eviction(self) -> None:
        detail = prove_mcp_tool_eviction_classifier_guard()
        self.assertEqual(detail["classified_as"], MCP_TOOL_EVICTION)
        self.assertIn("phase-15", detail["scope"])
        self.assertIn("CLASSIFIER-ONLY", detail["scope"])

    def test_prove_includes_the_classifier_self_check(self) -> None:
        with _TmpDirCtx() as root:
            module = _module(root, send=_ok_send())
            evidence = module.prove(_run_context(root))
            self.assertEqual(
                evidence.detail["mcp_tool_eviction_guard"]["classified_as"], MCP_TOOL_EVICTION,
            )


# --------------------------------------------------------------------- restore / report


class RestoreTest(unittest.TestCase):
    def test_restore_is_a_noop_reporting_no_mutation(self) -> None:
        with _TmpDirCtx() as root:
            module = _module(root)
            restore_evidence = module.restore(_run_context(root))
            self.assertTrue(restore_evidence.restored)
            self.assertIn("no real-state mutation", restore_evidence.detail["note"])


class ReportTest(unittest.TestCase):
    def test_report_summary_names_the_send_isolation_gap_and_the_guard_scopes(self) -> None:
        with _TmpDirCtx() as root:
            module = _module(root, send=_ok_send())
            evidence = module.prove(_run_context(root))
            row = module.report(evidence)
            self.assertEqual(row.module_id, MODULE_ID)
            self.assertEqual(row.tier, "core")
            self.assertEqual(row.status, "proven")
            self.assertIn("NOT a live Telegram round-trip", row.summary)
            self.assertIn("T-2582", row.summary)
            self.assertIn("phase-15", row.summary)
            self.assertTrue(row.evidence_refs)


# --------------------------------------------------------------------- evidence-tree write-through


class EvidenceTreeTest(unittest.TestCase):
    def test_prove_writes_every_named_guard_as_its_own_evidence_file(self) -> None:
        with _TmpDirCtx() as root:
            module = _module(root, send=_ok_send())
            ctx = _run_context(root)
            evidence = module.prove(ctx)
            paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
            evidence_dir = paths.module_evidence_dir(MODULE_ID)
            names = {path.name for path in evidence_dir.glob("*.json")}
            self.assertEqual(
                names,
                {
                    "bot-lock-contention-guard.json",
                    "unthreaded.json",
                    "threaded.json",
                    "single-poller-violation-guard.json",
                    "mcp-tool-eviction-guard.json",
                },
            )
            self.assertEqual(len(evidence.evidence_paths), 5)


if __name__ == "__main__":
    unittest.main()
