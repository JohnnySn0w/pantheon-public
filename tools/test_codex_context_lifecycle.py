from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools import codex_context_lifecycle as ccl


SESSION_ID = "01a0582e-cdf1-7e90-bef0-11fbb6ccf7ff"
OTHER_SESSION_ID = "01a059d9-781c-7fb1-84a3-e0fea01c1627"
FORBIDDEN_SENTINEL = "FORBIDDEN_BODY_SENTINEL_do_not_retain"
NOW = datetime(2026, 8, 31, 22, 3, 50, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _rollout_path(root: Path, session_id: str = SESSION_ID) -> Path:
    return root / f"rollout-2026-08-31T22-03-39-{session_id}.jsonl"


def _meta(
    session_id: str = SESSION_ID,
    *,
    timestamp: datetime = NOW,
    cli_version: str = ccl.PINNED_CODEX_CLI_VERSION,
    source="cli",
    thread_source: str = "user",
    extra_payload: dict | None = None,
) -> dict:
    payload = {
        "id": session_id,
        "session_id": session_id,
        "cli_version": cli_version,
        "context_window": {"window_id": "01a0582e-cdf1-7e90-bef0-window"},
        "cwd": "/home/codex/worktrees/T-2957",
        "history_mode": "paginated",
        "model_provider": "openai",
        "originator": "codex-tui",
        "source": source,
        "thread_source": thread_source,
        "timestamp": _iso(timestamp),
        "base_instructions": FORBIDDEN_SENTINEL,
    }
    if extra_payload:
        payload.update(extra_payload)
    return {
        "timestamp": _iso(timestamp),
        "ordinal": 0,
        "type": "session_meta",
        "payload": payload,
    }


def _token(
    ordinal: int,
    current: int,
    lifetime: int,
    window: int = 100,
    *,
    timestamp: datetime = NOW + timedelta(seconds=5),
) -> dict:
    return {
        "timestamp": _iso(timestamp),
        "ordinal": ordinal,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": current,
                    "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": current,
                },
                "total_token_usage": {
                    "input_tokens": lifetime,
                    "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "total_tokens": lifetime,
                },
                "model_context_window": window,
            },
        },
    }


def _compacted(ordinal: int, *, timestamp: datetime = NOW + timedelta(seconds=3)) -> dict:
    return {"timestamp": _iso(timestamp), "ordinal": ordinal, "type": "compacted"}


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True))
            handle.write("\n")
    return path


def _hint(path: Path, *, session_id: str = SESSION_ID, observed_at: datetime | None = None) -> ccl.DaemonHint:
    return ccl.DaemonHint(
        daemon_instance_id="daemon-before",
        session_id=session_id,
        rollout_path=path,
        started_at=NOW - timedelta(seconds=5),
        observed_at=observed_at or NOW + timedelta(seconds=15),
        process_id=123,
    )


def _measurement(tmp: Path, current: int, lifetime: int | None = None) -> ccl.CodexContextMeasurement:
    path = _write_jsonl(_rollout_path(tmp), [_meta(), _token(1, current, lifetime or current)])
    return ccl.read_codex_rollout_measurement(path, daemon_hint=_hint(path))


def _gate(state: ccl.GateState = ccl.GateState.CLEAR, *, now: datetime = NOW) -> ccl.CodexProductionGate:
    return ccl.CodexProductionGate(
        schema_version=ccl.CODEX_CONTEXT_SCHEMA_VERSION,
        gate_id="gate-1",
        operation_id="op-1",
        state=state,
        authority="test-authority",
        source_identity="test-source",
        updated_at=now,
        expiry=now + timedelta(hours=1),
        integrity="integrity-v1",
        version=1,
    )


def _active_step() -> ccl.ActiveStepSnapshot:
    return ccl.ActiveStepSnapshot(
        step_id="T-2957/step",
        summary="implement lifecycle",
        chat_id="456474197",
        status="active",
        started_at=NOW,
        updated_at=NOW,
        note="",
    )


def _project(tmp: Path) -> ccl.ProjectSnapshot:
    return ccl.ProjectSnapshot(
        ticket_id="T-2957",
        worktree=tmp.resolve(),
        branch="agent/T-2957",
        commit="13ba68976cccbb36d14c48497cdb059db3341867",
    )


def _persona() -> ccl.PersonaSnapshot:
    return ccl.PersonaSnapshot(
        persona="codex",
        account="codex",
        harness="codex",
        source="instance.yaml:peer-daemon-codex",
    )


def _subagents(tmp: Path) -> tuple[ccl.SubagentSnapshot, ...]:
    return (
        ccl.SubagentSnapshot(
            task_name="reviewer",
            agent_path="/root/t2908_orchestrator/t2957_review",
            state="running",
            worktree=tmp.resolve(),
            mailbox=Path("/home/the-ancestor/mailbox/codex"),
            last_seen_at=NOW,
        ),
    )


def _mailboxes() -> tuple[ccl.MailboxOffsetSnapshot, ...]:
    return (
        ccl.MailboxOffsetSnapshot(
            mailbox=Path("/home/the-ancestor/mailbox/codex"),
            cursor_kind="maildir",
            new_count=1,
            cur_count=2,
            last_seen_name="1700000000.msg",
            digest="a" * 64,
        ),
    )


def _telegram() -> ccl.TelegramSnapshot:
    return ccl.TelegramSnapshot(
        offset=42,
        pending=(
            ccl.TelegramSpoolEntry(
                update_id=41,
                path=Path("/home/codex/.config/codex-telegram/pending-inbound/00000000000000000041.json"),
                wrapped_sha256="b" * 64,
                enqueued_at=NOW,
            ),
        ),
    )


def _handoff(tmp: Path, measurement: ccl.CodexContextMeasurement | None = None) -> ccl.CodexContextHandoff:
    measurement = measurement or _measurement(tmp, 91)
    return ccl.CodexContextHandoff(
        schema_version=ccl.CODEX_CONTEXT_SCHEMA_VERSION,
        handoff_id="handoff-1",
        created_at=NOW,
        daemon_instance_id=measurement.daemon_instance_id,
        session_id=measurement.session_id,
        measurement_id=measurement.measurement_id,
        current_tokens=measurement.current_tokens,
        window_tokens=measurement.window_tokens,
        threshold_state=ccl.ThresholdState.DEFERRED,
        active_step=_active_step(),
        project=_project(tmp),
        persona=_persona(),
        subagents=_subagents(tmp),
        mailbox_offsets=_mailboxes(),
        telegram=_telegram(),
        kick_id="kick-1",
        resume_idempotency_key="resume-key-1",
        complete=True,
    )


def _restart_receipt(
    handoff: ccl.CodexContextHandoff,
    *,
    after_id: str = "daemon-after",
    after_pid: int = 456,
) -> ccl.RestartResumeReceipt:
    before = ccl.DaemonInstance("daemon-before", SESSION_ID, 123, NOW)
    after = ccl.DaemonInstance(after_id, SESSION_ID, after_pid, NOW + timedelta(minutes=1))
    kick = ccl.KickReceipt("kick-1", "daemon-before", SESSION_ID, NOW, handoff.resume_idempotency_key)
    return ccl.correlate_restart_resume(before, handoff, kick, after)


def _evidence(path: Path, receipt: ccl.HandoffReceipt) -> ccl.DurableHandoffEvidence:
    return ccl.DurableHandoffEvidence(
        path=path,
        receipt=receipt,
        expected_sha256=receipt.sha256,
        expected_handoff_id=receipt.handoff_id,
        expected_measurement_id=receipt.measurement_id,
        expected_daemon_instance_id=receipt.daemon_instance_id,
    )


def _json_blob(obj) -> str:
    return json.dumps(ccl._to_jsonable(obj), sort_keys=True)


class CodexContextMeasurementTest(unittest.TestCase):
    def test_threshold_crossing_uses_hysteresis_without_flapping(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            gates = ccl.ResetGates(production_gate=None, handoff_evidence=None)
            ok = ccl.CodexContextState(ccl.ThresholdState.OK)
            self.assertEqual(
                ccl.decide_codex_context_state(_measurement(tmp, 69), ok, gates).next_state,
                ccl.ThresholdState.OK,
            )
            warn = ccl.decide_codex_context_state(_measurement(tmp, 70), ok, gates)
            self.assertEqual(warn.next_state, ccl.ThresholdState.WARN)
            held = ccl.decide_codex_context_state(
                _measurement(tmp, 66), ccl.CodexContextState(ccl.ThresholdState.WARN), gates
            )
            self.assertEqual(held.next_state, ccl.ThresholdState.WARN)
            exited = ccl.decide_codex_context_state(
                _measurement(tmp, 64), ccl.CodexContextState(ccl.ThresholdState.WARN), gates
            )
            self.assertEqual(exited.next_state, ccl.ThresholdState.OK)

    def test_stale_or_malformed_measurement_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            stale_path = _write_jsonl(
                _rollout_path(tmp),
                [_meta(timestamp=NOW - timedelta(hours=1)), _token(1, 80, 80, timestamp=NOW - timedelta(hours=1))],
            )
            with self.assertRaises(ccl.RolloutAttributionError):
                ccl.read_codex_rollout_measurement(stale_path, daemon_hint=_hint(stale_path))
            malformed = _rollout_path(tmp, "01a0582e-cdf1-7e90-bef0-malformed")
            malformed.write_text("{not-json\n", encoding="utf-8")
            with self.assertRaises(ccl.RolloutMeasurementError):
                ccl.read_codex_rollout_measurement(malformed, daemon_hint=_hint(malformed, session_id="01a0582e-cdf1-7e90-bef0-malformed"))
            decision = ccl.decide_codex_context_state(
                ccl.CodexContextMeasurement(
                    schema_version=1,
                    measurement_id="bad",
                    source=stale_path,
                    source_sha256="0" * 64,
                    daemon_instance_id="daemon-before",
                    session_id=SESSION_ID,
                    token_ordinal=1,
                    latest_compaction_ordinal=None,
                    measured_at=NOW,
                    current_tokens=0,
                    lifetime_tokens=0,
                    window_tokens=100,
                    fill_ratio=0.0,
                    cli_version=ccl.PINNED_CODEX_CLI_VERSION,
                    stale=True,
                    valid=False,
                    fail_reason="bad",
                ),
                ccl.CodexContextState(ccl.ThresholdState.WARN),
                ccl.ResetGates(None, None),
            )
            self.assertEqual(decision.next_state, ccl.ThresholdState.FAIL_CLOSED)

    def test_current_usage_does_not_use_lifetime_total(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            measurement = _measurement(Path(d), 10, lifetime=95)
            self.assertEqual(measurement.current_tokens, 10)
            self.assertEqual(measurement.lifetime_tokens, 95)
            self.assertEqual(measurement.fill_ratio, 0.10)

    def test_newer_unrelated_rollout_is_not_active_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            root = _write_jsonl(_rollout_path(tmp), [_meta(), _token(1, 75, 75)])
            subagent = _write_jsonl(
                _rollout_path(tmp, OTHER_SESSION_ID),
                [
                    _meta(
                        OTHER_SESSION_ID,
                        source={"subagent": {"thread_spawn": {"agent_path": "/root/sub"}}},
                        thread_source="subagent",
                        extra_payload={"parent_thread_id": SESSION_ID, "agent_path": "/root/sub"},
                    ),
                    _token(1, 95, 95),
                ],
            )
            measurement = ccl.read_codex_rollout_measurement(root, daemon_hint=_hint(root))
            self.assertEqual(measurement.session_id, SESSION_ID)
            with self.assertRaises(ccl.RolloutAttributionError):
                ccl.read_codex_rollout_measurement(subagent, daemon_hint=_hint(subagent, session_id=OTHER_SESSION_ID))

    def test_rollout_cli_version_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _write_jsonl(_rollout_path(tmp), [_meta(cli_version="0.148.0"), _token(1, 10, 10)])
            with self.assertRaises(ccl.RolloutAttributionError):
                ccl.read_codex_rollout_measurement(path, daemon_hint=_hint(path))

    def test_rollout_source_shape_uses_top_level_session_meta_ordinal_zero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _write_jsonl(_rollout_path(tmp), [_meta(), _token(1, 10, 10)])
            measurement = ccl.read_codex_rollout_measurement(path, daemon_hint=_hint(path))
            self.assertNotIn(FORBIDDEN_SENTINEL, _json_blob(measurement))
            event_msg_meta = _rollout_path(tmp, "01a0582e-cdf1-7e90-bef0-eventmeta")
            rec = _meta("01a0582e-cdf1-7e90-bef0-eventmeta")
            rec["type"] = "event_msg"
            rec["payload"]["type"] = "session_meta"
            _write_jsonl(event_msg_meta, [rec, _token(1, 10, 10)])
            with self.assertRaises(ccl.RolloutAttributionError):
                ccl.read_codex_rollout_measurement(
                    event_msg_meta,
                    daemon_hint=_hint(event_msg_meta, session_id="01a0582e-cdf1-7e90-bef0-eventmeta"),
                )
            bad_window = _rollout_path(tmp, "01a0582e-cdf1-7e90-bef0-badwindow")
            _write_jsonl(
                bad_window,
                [
                    _meta(
                        "01a0582e-cdf1-7e90-bef0-badwindow",
                        extra_payload={"context_window": 258400},
                    ),
                    _token(1, 10, 10),
                ],
            )
            with self.assertRaises(ccl.RolloutAttributionError):
                ccl.read_codex_rollout_measurement(
                    bad_window,
                    daemon_hint=_hint(bad_window, session_id="01a0582e-cdf1-7e90-bef0-badwindow"),
                )


class CodexContextLifecycleTest(unittest.TestCase):
    def test_internal_compaction_does_not_emit_daemon_restart_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _write_jsonl(_rollout_path(tmp), [_meta(), _compacted(2), _token(3, 20, 120)])
            measurement = ccl.read_codex_rollout_measurement(path, daemon_hint=_hint(path))
            decision = ccl.decide_codex_context_state(
                measurement,
                ccl.CodexContextState(ccl.ThresholdState.WARN, last_processed_compaction_ordinal=-1),
                ccl.ResetGates(None, None),
            )
            self.assertEqual(decision.receipt, ccl.ReceiptKind.COMPACTION)
            self.assertEqual(decision.action, ccl.DecisionAction.COMPACTION_RECEIPT)
            self.assertNotEqual(decision.receipt, ccl.ReceiptKind.RESTART_RESUME)

    def test_restart_resume_correlates_daemon_instance_handoff_and_kick(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            handoff = _handoff(tmp)
            before = ccl.DaemonInstance("daemon-before", SESSION_ID, 123, NOW)
            after = ccl.DaemonInstance("daemon-after", SESSION_ID, 456, NOW + timedelta(minutes=1))
            kick = ccl.KickReceipt("kick-1", "daemon-before", SESSION_ID, NOW, handoff.resume_idempotency_key)
            receipt = ccl.correlate_restart_resume(before, handoff, kick, after)
            self.assertTrue(receipt.resumed)
            with self.assertRaises(ccl.RestartCorrelationError):
                ccl.correlate_restart_resume(
                    before,
                    handoff,
                    ccl.KickReceipt("kick-1", "wrong", SESSION_ID, NOW, handoff.resume_idempotency_key),
                    after,
                )

    def test_old_compaction_ordinal_does_not_repeat_receipt_or_suppress_threshold_decision(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _write_jsonl(_rollout_path(tmp), [_meta(), _compacted(2), _token(3, 85, 185)])
            measurement = ccl.read_codex_rollout_measurement(path, daemon_hint=_hint(path))
            prior = ccl.CodexContextState(
                ccl.ThresholdState.WARN,
                last_seen_compaction_ordinal=2,
                last_processed_compaction_ordinal=2,
            )
            decision = ccl.decide_codex_context_state(measurement, prior, ccl.ResetGates(None, None))
            self.assertIsNone(decision.receipt)
            self.assertEqual(decision.action, ccl.DecisionAction.WRITE_HANDOFF)
            self.assertEqual(decision.next_state, ccl.ThresholdState.DEFERRED)


class CodexContextHandoffTest(unittest.TestCase):
    def test_missing_or_incomplete_handoff_blocks_reset(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            measurement = _measurement(tmp, 91)
            prior = ccl.CodexContextState(ccl.ThresholdState.DEFERRED)
            decision = ccl.decide_codex_context_state(
                measurement,
                prior,
                ccl.ResetGates(production_gate=_gate(), handoff_evidence=None),
            )
            self.assertFalse(decision.reset_ready)
            self.assertEqual(decision.next_state, ccl.ThresholdState.BLOCKED)
            bad_path = tmp / "handoff.json"
            bad_path.write_text(json.dumps({"schema_version": 1, "complete": False}), encoding="utf-8")
            with self.assertRaises(ccl.HandoffValidationError):
                ccl.validate_complete_handoff(bad_path)

    def test_atomic_complete_handoff_conserves_active_step_project_subagent_and_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            handoff = _handoff(tmp)
            path = tmp / "state" / "handoff.json"
            receipt = ccl.write_atomic_context_handoff(path, handoff)
            loaded = ccl.validate_complete_handoff(path)
            self.assertEqual(loaded.active_step, handoff.active_step)
            self.assertEqual(loaded.project, handoff.project)
            self.assertEqual(loaded.subagents, handoff.subagents)
            self.assertEqual(loaded.mailbox_offsets, handoff.mailbox_offsets)
            self.assertEqual(loaded.telegram, handoff.telegram)
            self.assertTrue(receipt.durable)
            self.assertTrue(receipt.validated)
            combined = _json_blob(loaded) + _json_blob(receipt)
            self.assertNotIn(FORBIDDEN_SENTINEL, combined)

    def test_complete_handoff_requires_literal_json_true(self) -> None:
        invalid_values = ("true", "false", 1, 0, [], {}, None, False)
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            base = ccl._to_jsonable(_handoff(tmp))
            for value in invalid_values:
                with self.subTest(value=repr(value)):
                    payload = json.loads(json.dumps(base))
                    payload["complete"] = value
                    path = tmp / f"handoff-{type(value).__name__}-{len(repr(value))}.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ccl.HandoffValidationError):
                        ccl.validate_complete_handoff(path)
            missing = json.loads(json.dumps(base))
            del missing["complete"]
            missing_path = tmp / "handoff-missing-complete.json"
            missing_path.write_text(json.dumps(missing), encoding="utf-8")
            with self.assertRaises(ccl.HandoffValidationError):
                ccl.validate_complete_handoff(missing_path)

    def test_file_fsync_failure_leaves_no_durable_handoff_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = tmp / "handoff.json"
            real_fsync = os.fsync

            def fail_file_fsync(fd):
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("file fsync failed")
                return real_fsync(fd)

            with patch("tools.codex_context_lifecycle.os.fsync", side_effect=fail_file_fsync):
                with self.assertRaises(OSError):
                    ccl.write_atomic_context_handoff(path, _handoff(tmp))

    def test_replace_failure_leaves_no_durable_handoff_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = tmp / "handoff.json"
            with patch("tools.codex_context_lifecycle.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    ccl.write_atomic_context_handoff(path, _handoff(tmp))
            self.assertFalse(path.exists())

    def test_parent_fsync_failure_leaves_no_durable_handoff_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = tmp / "handoff.json"
            real_fsync = os.fsync

            def fail_dir_fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("dir fsync failed")
                return real_fsync(fd)

            with patch("tools.codex_context_lifecycle.os.fsync", side_effect=fail_dir_fsync):
                with self.assertRaises(OSError):
                    ccl.write_atomic_context_handoff(path, _handoff(tmp))


class CodexContextResetTest(unittest.TestCase):
    def test_non_interruptible_production_transition_blocks_reset(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            measurement = _measurement(tmp, 91)
            handoff = _handoff(tmp, measurement)
            handoff_path = tmp / "handoff.json"
            receipt = ccl.write_atomic_context_handoff(handoff_path, handoff)
            decision = ccl.decide_codex_context_state(
                measurement,
                ccl.CodexContextState(ccl.ThresholdState.DEFERRED),
                ccl.ResetGates(
                    production_gate=_gate(ccl.GateState.NON_INTERRUPTIBLE),
                    handoff_evidence=_evidence(handoff_path, receipt),
                ),
            )
            self.assertFalse(decision.reset_ready)
            self.assertEqual(decision.next_state, ccl.ThresholdState.BLOCKED)


class CodexContextContinuityTest(unittest.TestCase):
    def test_restart_replay_does_not_duplicate_mailbox_or_telegram_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            handoff = _handoff(tmp)
            before = ccl.DaemonInstance("daemon-before", SESSION_ID, 123, NOW)
            after = ccl.DaemonInstance("daemon-after", SESSION_ID, 456, NOW + timedelta(minutes=1))
            kick = ccl.KickReceipt("kick-1", "daemon-before", SESSION_ID, NOW, handoff.resume_idempotency_key)
            receipt = ccl.correlate_restart_resume(before, handoff, kick, after)
            ledger = tmp / "resume-ledger.json"
            first = ccl.commit_restart_resume_once(
                ledger,
                receipt,
                handoff,
                expected_offsets=handoff.mailbox_offsets,
                expected_telegram=handoff.telegram,
            )
            second = ccl.commit_restart_resume_once(
                ledger,
                receipt,
                handoff,
                expected_offsets=handoff.mailbox_offsets,
                expected_telegram=handoff.telegram,
            )
            self.assertEqual(first.disposition, ccl.ReplayDisposition.COMMITTED)
            self.assertEqual(second.disposition, ccl.ReplayDisposition.PRIOR_RECEIPT)
            self.assertEqual(second.receipt, receipt)

    def test_restart_resume_commit_is_idempotent_and_returns_prior_receipt(self) -> None:
        self.test_restart_replay_does_not_duplicate_mailbox_or_telegram_consumption()

    def test_restart_resume_rejects_same_key_with_changed_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            handoff = _handoff(tmp)
            before = ccl.DaemonInstance("daemon-before", SESSION_ID, 123, NOW)
            after = ccl.DaemonInstance("daemon-after", SESSION_ID, 456, NOW + timedelta(minutes=1))
            kick = ccl.KickReceipt("kick-1", "daemon-before", SESSION_ID, NOW, handoff.resume_idempotency_key)
            receipt = ccl.correlate_restart_resume(before, handoff, kick, after)
            ledger = tmp / "resume-ledger.json"
            ccl.commit_restart_resume_once(
                ledger,
                receipt,
                handoff,
                expected_offsets=handoff.mailbox_offsets,
                expected_telegram=handoff.telegram,
            )
            changed_receipt = ccl.RestartResumeReceipt(
                **{
                    **ccl._to_jsonable(receipt),
                    "mailbox_offsets": [
                        {
                            **ccl._to_jsonable(handoff.mailbox_offsets[0]),
                            "new_count": 99,
                        }
                    ],
                }
            )
            with self.assertRaises(ccl.RestartCorrelationError):
                ccl.commit_restart_resume_once(
                    ledger,
                    changed_receipt,
                    handoff,
                    expected_offsets=handoff.mailbox_offsets,
                    expected_telegram=handoff.telegram,
                )

    def test_concurrent_resume_commit_has_single_winner_and_stable_final_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            handoff = _handoff(tmp)
            receipt_a = _restart_receipt(handoff, after_id="daemon-after-a", after_pid=456)
            receipt_b = _restart_receipt(handoff, after_id="daemon-after-b", after_pid=457)
            ledger = tmp / "resume-ledger.json"
            barrier = threading.Barrier(2)
            results: list[ccl.ReplayResult] = []
            errors: list[BaseException] = []

            def commit(receipt: ccl.RestartResumeReceipt) -> None:
                try:
                    results.append(
                        ccl.commit_restart_resume_once(
                            ledger,
                            receipt,
                            handoff,
                            expected_offsets=handoff.mailbox_offsets,
                            expected_telegram=handoff.telegram,
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            ccl._TEST_RACE_BARRIER = barrier
            try:
                threads = [
                    threading.Thread(target=commit, args=(receipt_a,)),
                    threading.Thread(target=commit, args=(receipt_b,)),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                self.assertFalse(any(thread.is_alive() for thread in threads))
            finally:
                ccl._TEST_RACE_BARRIER = None
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            committed = [result for result in results if result.disposition is ccl.ReplayDisposition.COMMITTED]
            self.assertEqual(len(committed), 1)
            losers = [result for result in results if result.disposition is not ccl.ReplayDisposition.COMMITTED]
            self.assertEqual(len(losers), 1)
            self.assertIn(
                losers[0].disposition,
                (ccl.ReplayDisposition.PRIOR_RECEIPT, ccl.ReplayDisposition.REJECTED),
            )
            final = ccl._ledger_entry_from_dict(ccl._read_json(ledger))
            self.assertEqual(final.receipt.receipt_id, committed[0].receipt.receipt_id)
            self.assertEqual(final.receipt.mailbox_offsets, handoff.mailbox_offsets)
            self.assertEqual(final.receipt.telegram_offset, handoff.telegram.offset)

    def test_resume_receipt_resumed_requires_literal_json_true(self) -> None:
        invalid_values = ("true", "false", 1, 0, [], {}, None, False)
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            handoff = _handoff(tmp)
            receipt = _restart_receipt(handoff)
            base_entry = ccl._to_jsonable(
                ccl.ResumeLedgerEntry(
                    schema_version=ccl.CODEX_CONTEXT_SCHEMA_VERSION,
                    resume_idempotency_key=receipt.resume_idempotency_key,
                    receipt=receipt,
                    handoff_sha256=ccl._sha256_bytes(ccl._canonical_json_bytes(handoff)),
                    committed_at=NOW,
                )
            )
            for value in invalid_values:
                with self.subTest(value=repr(value)):
                    payload = json.loads(json.dumps(base_entry))
                    payload["receipt"]["resumed"] = value
                    with self.assertRaises(ccl.RestartCorrelationError):
                        ccl._ledger_entry_from_dict(payload)
            missing = json.loads(json.dumps(base_entry))
            del missing["receipt"]["resumed"]
            with self.assertRaises(ccl.RestartCorrelationError):
                ccl._ledger_entry_from_dict(missing)


class CodexContextProductionFlowTest(unittest.TestCase):
    def test_temp_state_flow_measurement_warning_handoff_reset_ready_new_daemon_kick_resume_health(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            measurement = _measurement(tmp, 91)
            warn = ccl.decide_codex_context_state(
                measurement,
                ccl.CodexContextState(ccl.ThresholdState.DEFERRED),
                ccl.ResetGates(production_gate=None, handoff_evidence=None),
            )
            self.assertEqual(warn.next_state, ccl.ThresholdState.BLOCKED)
            handoff = _handoff(tmp, measurement)
            handoff_path = tmp / "context-handoff.json"
            receipt = ccl.write_atomic_context_handoff(handoff_path, handoff)
            reset_ready = ccl.decide_codex_context_state(
                measurement,
                ccl.CodexContextState(ccl.ThresholdState.DEFERRED),
                ccl.ResetGates(production_gate=_gate(), handoff_evidence=_evidence(handoff_path, receipt)),
            )
            self.assertTrue(reset_ready.reset_ready)
            before = ccl.DaemonInstance("daemon-before", SESSION_ID, 123, NOW)
            after = ccl.DaemonInstance("daemon-after", SESSION_ID, 456, NOW + timedelta(minutes=1))
            kick = ccl.KickReceipt("kick-1", "daemon-before", SESSION_ID, NOW, handoff.resume_idempotency_key)
            resume = ccl.correlate_restart_resume(before, handoff, kick, after)
            committed = ccl.commit_restart_resume_once(
                tmp / "resume-ledger.json",
                resume,
                handoff,
                expected_offsets=handoff.mailbox_offsets,
                expected_telegram=handoff.telegram,
            )
            self.assertEqual(committed.disposition, ccl.ReplayDisposition.COMMITTED)

    def test_sanitized_synthetic_forbidden_content_never_appears_in_outputs_or_cli_json(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = _write_jsonl(_rollout_path(tmp), [_meta(), _token(1, 75, 75)])
            hint = _hint(path)
            measurement = ccl.read_codex_rollout_measurement(path, daemon_hint=hint)
            decision = ccl.decide_codex_context_state(
                measurement, ccl.CodexContextState(ccl.ThresholdState.OK), ccl.ResetGates(None, None)
            )
            handoff = _handoff(tmp, measurement)
            receipt = ccl.write_atomic_context_handoff(tmp / "handoff.json", handoff)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = ccl.main(
                    [
                        "inspect",
                        "--rollout",
                        str(path),
                        "--daemon-instance-id",
                        hint.daemon_instance_id,
                        "--session-id",
                        hint.session_id,
                        "--daemon-started-at",
                        _iso(hint.started_at),
                        "--observed-at",
                        _iso(hint.observed_at),
                        "--json",
                    ]
                )
            self.assertEqual(rc, 0)
            blob = _json_blob(measurement) + _json_blob(decision) + _json_blob(handoff) + _json_blob(receipt) + stdout.getvalue()
            self.assertNotIn(FORBIDDEN_SENTINEL, blob)

    def test_metadata_token_only_sanitized_copy_from_actual_rollout(self) -> None:
        source = os.environ.get("CODEX_CONTEXT_LIVE_ROLLOUT")
        if not source:
            self.skipTest("CODEX_CONTEXT_LIVE_ROLLOUT explicit source not provided")
        source_path = Path(source)
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            dest = tmp / source_path.name
            count = ccl.sanitize_rollout_to_file(source_path, dest)
            text = dest.read_text(encoding="utf-8")
            self.assertGreater(count, 1)
            self.assertNotIn("base_instructions", text)
            self.assertNotIn(FORBIDDEN_SENTINEL, text)
            with source_path.open(encoding="utf-8") as handle:
                first = json.loads(handle.readline())
            payload = first["payload"]
            if payload.get("thread_source") != "user":
                self.skipTest("explicit source is not the active root rollout")
            hint = ccl.DaemonHint(
                daemon_instance_id="live-copy-daemon",
                session_id=payload["session_id"],
                rollout_path=dest,
                started_at=ccl._parse_dt(payload["timestamp"], field="timestamp") - timedelta(seconds=1),
                observed_at=datetime.now(timezone.utc) + timedelta(days=1),
                max_age_seconds=10 * 365 * 24 * 3600,
            )
            measurement = ccl.read_codex_rollout_measurement(dest, daemon_hint=hint)
            self.assertTrue(measurement.valid)
            self.assertNotIn("base_instructions", _json_blob(measurement))


if __name__ == "__main__":
    unittest.main()
