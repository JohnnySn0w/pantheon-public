"""Typed harness lifecycle and persona bring-up tests (T-2372)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import types

from tools import mailbox
from tools import persona_bringup
from tools.harness_adapter import (
    ClaudeHarnessAdapter,
    CodexHarnessAdapter,
    ExecutionRequest,
    ExecutionResult,
    HarnessRunner,
    InboundRequest,
    SessionHandle,
)
from tools.harness_adapter.types import TmuxTarget
from tools.persona_bringup import (
    backoff_remaining,
    build_adapter,
    clear_backoff,
    consume_persona_mailbox,
    diagnose_persona,
    HarnessTurnTransientError,
    bump_soft_streak,
    clear_soft_streak,
    EXIT_SOFT_PERSISTENT,
    load_binding,
    load_backoff,
    load_soft_streak,
    SOFT_FAILURE_ALERT_STREAK,
    load_session_id,
    normalize_harness,
    orientation_prompt,
    process_mail_record,
    record_backoff_failure,
    save_session_id,
)


FIXTURES = Path(__file__).parent / "fixtures" / "harness_adapter"


def _events(adapter, path: Path):
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = adapter.parse_event(line)
        if event is not None:
            events.append(event)
    return events


class CommandConstruction(unittest.TestCase):
    def test_codex_prompt_is_stdin_not_argv(self) -> None:
        adapter = CodexHarnessAdapter("recovery")
        spec = adapter.build_exec(
            ExecutionRequest(
                prompt="secret prompt",
                cwd=Path("/tmp"),
                model="gpt-test",
                ephemeral=True,
            )
        )
        self.assertEqual(spec.stdin, "secret prompt")
        self.assertNotIn("secret prompt", spec.argv)
        self.assertIn("--json", spec.argv)
        self.assertIn("--ephemeral", spec.argv)
        self.assertEqual(spec.argv[-1], "-")

    def test_claude_prompt_is_stdin_not_argv(self) -> None:
        adapter = ClaudeHarnessAdapter("recovery")
        spec = adapter.build_exec(
            ExecutionRequest(prompt="secret prompt", cwd=Path("/tmp"))
        )
        self.assertEqual(spec.stdin, "secret prompt")
        self.assertNotIn("secret prompt", spec.argv)
        self.assertIn("stream-json", spec.argv)

    def test_codex_resume_by_id_is_explicit(self) -> None:
        adapter = CodexHarnessAdapter("recovery")
        spec = adapter.build_resume(
            SessionHandle("thread-1", None, Path("/tmp"), True),
            "continue",
        )
        self.assertIn("thread-1", spec.argv)
        self.assertNotIn("--last", spec.argv)


class EventParsing(unittest.TestCase):
    def test_codex_success_yields_structural_result(self) -> None:
        adapter = CodexHarnessAdapter("recovery")
        events = _events(adapter, FIXTURES / "codex" / "success.jsonl")
        result = adapter.finalize(events, returncode=0, stderr="")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.final_message, "RECOVERY_READY")
        self.assertEqual(
            result.session.session_id if result.session else None,
            "018f0000-0000-7000-8000-000000000001",
        )
        self.assertEqual(result.usage.cached_input_tokens if result.usage else None, 2)

    def test_codex_limit_failure_is_typed_and_retryable(self) -> None:
        adapter = CodexHarnessAdapter("recovery")
        events = _events(adapter, FIXTURES / "codex" / "failure.jsonl")
        result = adapter.finalize(events, returncode=1, stderr="")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error.category if result.error else None, "quota")
        self.assertTrue(result.error.retryable if result.error else False)

    def test_claude_result_preserves_session_cost_model_and_usage(self) -> None:
        adapter = ClaudeHarnessAdapter("recovery")
        events = _events(adapter, FIXTURES / "claude" / "success.jsonl")
        result = adapter.finalize(events, returncode=0, stderr="")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.final_message, "RECOVERY_READY")
        self.assertEqual(result.model, "claude-example")
        self.assertEqual(result.usage.cost_usd if result.usage else None, 0.001)
        self.assertEqual(
            result.provider_billing.api_key_source
            if result.provider_billing
            else None,
            "ANTHROPIC_API_KEY",
        )
        self.assertEqual(
            result.provider_billing.total_cost_usd
            if result.provider_billing
            else None,
            0.001,
        )
        self.assertEqual(
            result.provider_billing.source_events if result.provider_billing else (),
            ("system/init", "result"),
        )

    def test_claude_billing_source_only_comes_from_system_init(self) -> None:
        adapter = ClaudeHarnessAdapter("recovery")
        events = [
            adapter.parse_event(
                '{"type":"system","subtype":"init","session_id":"s"}'
            ),
            adapter.parse_event(
                '{"type":"result","subtype":"success","is_error":false,'
                '"session_id":"s","apiKeySource":"ANTHROPIC_API_KEY",'
                '"total_cost_usd":0.001,"usage":{"input_tokens":1}}'
            ),
        ]
        result = adapter.finalize(
            [event for event in events if event is not None],
            returncode=0,
            stderr="",
        )
        self.assertIsNone(
            result.provider_billing.api_key_source
            if result.provider_billing
            else None
        )

    def test_partial_assistant_message_is_not_completion(self) -> None:
        adapter = ClaudeHarnessAdapter("recovery")
        event = adapter.parse_event(
            '{"type":"assistant","session_id":"s","message":{"content":'
            '[{"type":"text","text":"partial"}]}}'
        )
        result = adapter.finalize([event], returncode=0, stderr="")
        self.assertEqual(result.status, "failed")
        self.assertIn("structural", result.error.message if result.error else "")

    def test_malformed_event_is_failure_not_silent_completion(self) -> None:
        adapter = CodexHarnessAdapter("recovery")
        event = adapter.parse_event("{not json")
        result = adapter.finalize([event], returncode=0, stderr="")
        self.assertEqual(result.status, "failed")
        self.assertIn("invalid JSON", result.error.message if result.error else "")


class DeliveryLifecycle(unittest.TestCase):
    def test_duplicate_delivery_is_idempotent_while_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "recovery"
            mailbox.provision(inbox)
            adapter = CodexHarnessAdapter(
                "recovery",
                mailbox_path=inbox,
            )
            request = InboundRequest(
                message="wake",
                correlation_id="corr-1",
                source="test",
                reply_route={},
            )
            first = adapter.deliver_inbound(request)
            second = adapter.deliver_inbound(request)
            self.assertEqual(first.state, "durably_queued")
            self.assertEqual(first.evidence["message_id"], second.evidence["message_id"])
            self.assertEqual(len(list((inbox / "new").iterdir())), 1)


class RunnerFailures(unittest.TestCase):
    def test_spawn_failure_becomes_typed_config_error(self) -> None:
        adapter = CodexHarnessAdapter("recovery")
        request = ExecutionRequest(prompt="x", cwd=Path("/tmp"), ephemeral=True)
        with mock.patch(
            "tools.harness_adapter.runner.subprocess.Popen",
            side_effect=FileNotFoundError("missing"),
        ):
            result = HarnessRunner().run(adapter, request)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error.category if result.error else None, "config")
        self.assertEqual(result.returncode, 127)

    def test_broken_stdin_pipe_becomes_structured_failure(self) -> None:
        class BrokenPipe:
            def write(self, value):
                raise BrokenPipeError("closed")

            def close(self):
                pass

        process = mock.Mock()
        process.stdin = BrokenPipe()
        process.stdout = []
        process.stderr = mock.Mock()
        process.stderr.readlines.return_value = []
        process.wait.return_value = 1
        adapter = CodexHarnessAdapter("recovery")
        request = ExecutionRequest(prompt="x", cwd=Path("/tmp"), ephemeral=True)
        with mock.patch(
            "tools.harness_adapter.runner.subprocess.Popen",
            return_value=process,
        ):
            result = HarnessRunner().run(adapter, request)
        self.assertEqual(result.status, "failed")
        self.assertIn("BrokenPipeError", result.error.message if result.error else "")


class PersonaBindingTests(unittest.TestCase):
    def test_harness_aliases_normalize(self) -> None:
        self.assertEqual(normalize_harness("claude-code"), "claude")
        self.assertEqual(normalize_harness("codex"), "codex")

    def test_manifest_role_and_harness_override_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "instance.yaml"
            manifest.write_text(
                "roles:\n"
                "  recovery:\n"
                "    persona: recovery\n"
                "    account: claude-fallback\n"
                "    harness: claude-code\n",
                encoding="utf-8",
            )
            binding = load_binding(
                manifest,
                "recovery",
                harness_override="codex",
                mailbox_hub=root / "mail",
            )
        self.assertEqual(binding.role, "recovery")
        self.assertEqual(binding.configured_harness, "claude")
        self.assertEqual(binding.selected_harness, "codex")

    def test_manifest_native_channels_threads_through_load_binding_and_adapter(
        self,
    ) -> None:
        # T-2602: channels lives in harness_runtime.roles.<role>.claude.native
        # and must reach build_interactive_launch with zero per-field
        # allowlisting in load_binding/build_adapter (native threads generically).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "instance.yaml"
            manifest.write_text(
                "harness_runtime:\n"
                "  schema: pantheon-harness-runtime/v1\n"
                "  harnesses:\n"
                "    claude:\n"
                "      default_profile: interactive-subscription\n"
                "      profiles:\n"
                "        interactive-subscription:\n"
                "          executable: /tmp/fake-claude\n"
                "          mode: interactive\n"
                "          meter: subscription\n"
                "          quota_source: statusline\n"
                "  roles:\n"
                "    recovery:\n"
                "      fallbacks: []\n"
                "      claude:\n"
                "        profile: interactive-subscription\n"
                "        native:\n"
                "          channels: plugin:telegram@claude-plugins-official\n"
                "roles:\n"
                "  recovery:\n"
                "    persona: recovery\n"
                "    account: claude-fallback\n"
                "    harness: claude-code\n",
                encoding="utf-8",
            )
            binding = load_binding(
                manifest,
                "recovery",
                mailbox_hub=root / "mail",
            )
            harness_adapter = build_adapter(binding, harness_home=root)
            command = harness_adapter.build_interactive_launch(
                TmuxTarget(session="pantheon-recovery", window="harness", pane="0")
            )
        self.assertEqual(command.argv.count("--channels"), 1)
        idx = command.argv.index("--channels")
        self.assertEqual(
            command.argv[idx + 1], "plugin:telegram@claude-plugins-official"
        )

    def test_orientation_preserves_persona_identity(self) -> None:
        binding = load_binding(
            Path("instance.yaml"),
            "recovery",
            harness_override="codex",
        )
        prompt = orientation_prompt(binding, "readiness check")
        self.assertIn("You are recovery", prompt)
        self.assertIn("implementation detail, not your identity", prompt)
        self.assertIn(str(binding.pinned_rules), prompt)

    def test_session_state_round_trip(self) -> None:
        binding = load_binding(
            Path("instance.yaml"),
            "recovery",
            harness_override="codex",
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            save_session_id(state_dir, binding, "session-1")
            self.assertEqual(load_session_id(state_dir, "recovery"), "session-1")

    def test_diagnostics_probe_existing_consumer_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mailbox.provision(root / "mail" / "recovery")
            manifest = root / "instance.yaml"
            manifest.write_text(
                "roles:\n"
                "  recovery:\n"
                "    persona: recovery\n"
                "    account: claude-fallback\n"
                "    harness: claude-code\n",
                encoding="utf-8",
            )
            binding = load_binding(
                manifest,
                "recovery",
                harness_override="codex",
                mailbox_hub=root / "mail",
            )
            report = diagnose_persona(
                binding,
                harness_home=Path.home(),
                cwd=Path("/tmp"),
                execution_context="test",
            )
            lease = next(
                item
                for item in report.observations
                if item.check_id == "persona.consume_lease"
            )
            self.assertEqual(lease.status, "pass")


class PersonaMailboxTests(unittest.TestCase):
    def test_mailbox_tasks_resume_one_logical_persona_session(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.requests = []

            def run(self, adapter, request):
                self.requests.append(request)
                return ExecutionResult(
                    harness="codex",
                    status="completed",
                    final_message=f"done-{len(self.requests)}",
                    session=SessionHandle("recovery-session", None, request.cwd, True),
                    usage=None,
                    model=None,
                    returncode=0,
                    error=None,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub = root / "mail"
            recovery_inbox = hub / "recovery"
            codex_inbox = hub / "codex"
            mailbox.provision(recovery_inbox)
            mailbox.provision(codex_inbox)
            manifest = root / "instance.yaml"
            manifest.write_text(
                "roles:\n"
                "  recovery:\n"
                "    persona: recovery\n"
                "    account: claude-fallback\n"
                "    harness: claude-code\n",
                encoding="utf-8",
            )
            binding = load_binding(
                manifest,
                "recovery",
                harness_override="codex",
                mailbox_hub=hub,
            )
            for index in (1, 2):
                mailbox.send(
                    recovery_inbox,
                    {"message": f"task {index}", "correlation_id": f"c-{index}"},
                    "codex",
                    subject=f"task-{index}",
                )
            runner = FakeRunner()
            stats, outcomes = consume_persona_mailbox(
                binding,
                adapter=CodexHarnessAdapter(
                    "recovery",
                    mailbox_path=recovery_inbox,
                ),
                runner=runner,
                state_dir=root / "state",
                cwd=Path("/tmp"),
                reply_hub=hub,
            )
            self.assertEqual(stats["errors"], [])
            self.assertEqual(len(outcomes), 2)
            self.assertIsNone(runner.requests[0].session_id)
            self.assertEqual(runner.requests[1].session_id, "recovery-session")
            self.assertEqual(len(list((recovery_inbox / "new").iterdir())), 0)
            self.assertEqual(len(list((codex_inbox / "new").iterdir())), 2)

    def test_redelivery_reuses_cached_result_instead_of_model_turn(self) -> None:
        class FakeRunner:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, adapter, request):
                self.calls += 1
                return ExecutionResult(
                    harness="codex",
                    status="completed",
                    final_message="ready",
                    session=SessionHandle("recovery-session", None, request.cwd, True),
                    usage=None,
                    model=None,
                    returncode=0,
                    error=None,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub = root / "mail"
            recovery_inbox = hub / "recovery"
            codex_inbox = hub / "codex"
            mailbox.provision(recovery_inbox)
            mailbox.provision(codex_inbox)
            manifest = root / "instance.yaml"
            manifest.write_text(
                "roles:\n"
                "  recovery:\n"
                "    persona: recovery\n"
                "    account: claude-fallback\n"
                "    harness: claude-code\n",
                encoding="utf-8",
            )
            binding = load_binding(
                manifest,
                "recovery",
                harness_override="codex",
                mailbox_hub=hub,
            )
            record = {
                "id": "message-1",
                "sender": "codex",
                "payload": {
                    "message": "wake",
                    "correlation_id": "correlation-1",
                },
            }
            runner = FakeRunner()
            for _ in range(2):
                process_mail_record(
                    record,
                    binding,
                    adapter=CodexHarnessAdapter(
                        "recovery",
                        mailbox_path=recovery_inbox,
                    ),
                    runner=runner,
                    state_dir=root / "state",
                    cwd=Path("/tmp"),
                    reply_hub=hub,
                )
            self.assertEqual(runner.calls, 1)
            self.assertEqual(len(list((codex_inbox / "new").iterdir())), 1)

    def test_retryable_harness_failure_is_mailbox_transient(self) -> None:
        class QuotaRunner:
            def run(self, adapter, request):
                from tools.harness_adapter import ExecutionError

                return ExecutionResult(
                    harness="codex", status="failed", final_message=None,
                    session=None, usage=None, model=None, returncode=1,
                    error=ExecutionError(
                        category="quota", message="weekly limit", retryable=True,
                        raw={},
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub = root / "mail"
            mailbox.provision(hub / "recovery")
            manifest = root / "instance.yaml"
            manifest.write_text(
                "roles:\n  recovery:\n    persona: recovery\n"
                "    account: claude-fallback\n    harness: codex\n",
                encoding="utf-8",
            )
            binding = load_binding(manifest, "recovery", mailbox_hub=hub)
            with self.assertRaises(HarnessTurnTransientError) as caught:
                process_mail_record(
                    {"id": "message-1", "sender": "codex", "payload": {"message": "wake"}},
                    binding,
                    adapter=CodexHarnessAdapter("recovery", mailbox_path=hub / "recovery"),
                    runner=QuotaRunner(), state_dir=root / "state", cwd=Path("/tmp"),
                    reply_hub=hub,
                )
            self.assertTrue(caught.exception.mailbox_transient)

    def test_unchanged_failure_uses_bounded_exponential_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            first = record_backoff_failure(
                state_dir,
                "recovery",
                "quota",
                now=1000,
            )
            second = record_backoff_failure(
                state_dir,
                "recovery",
                "quota",
                now=1100,
            )
            self.assertEqual(first["delay_seconds"], 120)
            self.assertEqual(second["delay_seconds"], 300)
            self.assertEqual(backoff_remaining(state_dir, "recovery", now=1200), 200)
            self.assertEqual(load_backoff(state_dir, "recovery")["attempts"], 2)
            clear_backoff(state_dir, "recovery")
            self.assertEqual(backoff_remaining(state_dir, "recovery", now=1200), 0)

    def test_new_failure_fingerprint_resets_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            record_backoff_failure(state_dir, "recovery", "quota", now=1000)
            state = record_backoff_failure(
                state_dir,
                "recovery",
                "different failure",
                now=1100,
            )
            self.assertEqual(state["attempts"], 1)
            self.assertEqual(state["delay_seconds"], 120)


class ConsumeSoftFailureAlertStreak(unittest.TestCase):
    """T-2586 cry-wolf discriminator: soft consume failures (exit 3/4/5) only
    escalate to the paging exit code once they persist for SOFT_FAILURE_ALERT_
    STREAK consecutive fires; a genuine success clears the streak."""

    def test_soft_streak_counter_mechanics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            self.assertEqual(load_soft_streak(sd, "recovery"), 0)
            self.assertEqual(bump_soft_streak(sd, "recovery"), 1)
            self.assertEqual(bump_soft_streak(sd, "recovery"), 2)
            self.assertEqual(load_soft_streak(sd, "recovery"), 2)
            clear_soft_streak(sd, "recovery")
            self.assertEqual(load_soft_streak(sd, "recovery"), 0)
            # clearing an absent streak is a no-op, not an error
            clear_soft_streak(sd, "recovery")
            # a corrupt state file reads as 0, never raises
            (sd / "recovery.softstreak.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(load_soft_streak(sd, "recovery"), 0)

    def _run_consume_diag_fail(self, state_dir: Path) -> int:
        """Drive main() consume through the exit-3 (diagnostic-fail) path."""
        report = types.SimpleNamespace(
            observations=[types.SimpleNamespace(status="fail")]
        )
        binding = types.SimpleNamespace(persona="recovery")
        with mock.patch.object(persona_bringup, "load_binding", return_value=binding), \
            mock.patch.object(
                persona_bringup, "select_recorded_runtime_binding", return_value=binding
            ), \
            mock.patch.object(persona_bringup, "diagnose_persona", return_value=report), \
            mock.patch.object(persona_bringup, "_print_json"):
            return persona_bringup.main(
                ["consume", "--persona", "recovery", "--state-dir", str(state_dir)]
            )

    def _run_consume_success(self, state_dir: Path) -> int:
        """Drive main() consume through a clean success (clears the streak)."""
        report = types.SimpleNamespace(
            observations=[types.SimpleNamespace(status="ok")]
        )
        binding = types.SimpleNamespace(
            persona="recovery", launch=types.SimpleNamespace(mode="batch")
        )
        with mock.patch.object(persona_bringup, "load_binding", return_value=binding), \
            mock.patch.object(
                persona_bringup, "select_recorded_runtime_binding", return_value=binding
            ), \
            mock.patch.object(persona_bringup, "diagnose_persona", return_value=report), \
            mock.patch.object(persona_bringup, "build_adapter", return_value=mock.Mock()), \
            mock.patch.object(persona_bringup, "first_fallback_binding", return_value=None), \
            mock.patch.object(persona_bringup, "runtime_reference", return_value="same"), \
            mock.patch.object(
                persona_bringup, "consume_persona_mailbox", return_value=({}, [])
            ), \
            mock.patch.object(persona_bringup, "_print_json"):
            return persona_bringup.main(
                ["consume", "--persona", "recovery", "--state-dir", str(state_dir)]
            )

    def test_isolated_soft_failure_returns_soft_code_not_paging(self) -> None:
        # Below threshold: consume returns the soft code (3), which the unit lists
        # in SuccessExitStatus, so OnFailure does NOT page.
        # This fixture assumes the threshold is above 2 so streaks 1 and 2 stay soft.
        self.assertGreater(SOFT_FAILURE_ALERT_STREAK, 2)
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            self.assertEqual(self._run_consume_diag_fail(sd), 3)  # streak 1
            self.assertEqual(self._run_consume_diag_fail(sd), 3)  # streak 2
            self.assertEqual(load_soft_streak(sd, "recovery"), 2)

    def test_sustained_soft_failure_escalates_to_paging_code(self) -> None:
        # At the threshold the SAME soft failure escalates to the hard code, which
        # the unit does NOT treat as success, so OnFailure pages for a real outage.
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            for _ in range(SOFT_FAILURE_ALERT_STREAK - 1):
                self.assertEqual(self._run_consume_diag_fail(sd), 3)
            self.assertEqual(
                self._run_consume_diag_fail(sd), EXIT_SOFT_PERSISTENT
            )

    def test_success_clears_streak_so_alternating_never_escalates(self) -> None:
        # The measured cry-wolf: fail then recover on the next fire. Because a clean
        # consume clears the streak, an alternating fail/recover pattern never reaches
        # the threshold and never pages — proven end-to-end through main().
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            for _ in range(SOFT_FAILURE_ALERT_STREAK - 1):
                self.assertEqual(self._run_consume_diag_fail(sd), 3)
            self.assertEqual(load_soft_streak(sd, "recovery"), SOFT_FAILURE_ALERT_STREAK - 1)
            self.assertEqual(self._run_consume_success(sd), 0)  # recovery clears
            self.assertEqual(load_soft_streak(sd, "recovery"), 0)
            # a subsequent lone failure starts a fresh streak, still below threshold
            self.assertEqual(self._run_consume_diag_fail(sd), 3)

    def _run_consume_transport_error(self, state_dir: Path) -> int:
        """Drive main() consume through the exit-5 (OSError transport) path."""
        report = types.SimpleNamespace(
            observations=[types.SimpleNamespace(status="ok")]
        )
        binding = types.SimpleNamespace(
            persona="recovery", launch=types.SimpleNamespace(mode="batch")
        )
        with mock.patch.object(persona_bringup, "load_binding", return_value=binding), \
            mock.patch.object(
                persona_bringup, "select_recorded_runtime_binding", return_value=binding
            ), \
            mock.patch.object(persona_bringup, "diagnose_persona", return_value=report), \
            mock.patch.object(persona_bringup, "build_adapter", return_value=mock.Mock()), \
            mock.patch.object(persona_bringup, "first_fallback_binding", return_value=None), \
            mock.patch.object(persona_bringup, "runtime_reference", return_value="same"), \
            mock.patch.object(
                persona_bringup,
                "consume_persona_mailbox",
                side_effect=OSError("connection refused"),
            ), \
            mock.patch.object(persona_bringup, "_print_json"):
            return persona_bringup.main(
                ["consume", "--persona", "recovery", "--state-dir", str(state_dir)]
            )

    def _run_consume_stats_errors(self, state_dir: Path) -> int:
        """Drive main() consume through the exit-4 (consume finished with errors) path."""
        report = types.SimpleNamespace(
            observations=[types.SimpleNamespace(status="ok")]
        )
        binding = types.SimpleNamespace(
            persona="recovery", launch=types.SimpleNamespace(mode="batch")
        )
        with mock.patch.object(persona_bringup, "load_binding", return_value=binding), \
            mock.patch.object(
                persona_bringup, "select_recorded_runtime_binding", return_value=binding
            ), \
            mock.patch.object(persona_bringup, "diagnose_persona", return_value=report), \
            mock.patch.object(persona_bringup, "build_adapter", return_value=mock.Mock()), \
            mock.patch.object(persona_bringup, "first_fallback_binding", return_value=None), \
            mock.patch.object(persona_bringup, "runtime_reference", return_value="same"), \
            mock.patch.object(
                persona_bringup,
                "consume_persona_mailbox",
                return_value=({"errors": ["boom"]}, []),
            ), \
            mock.patch.object(persona_bringup, "_print_json"):
            return persona_bringup.main(
                ["consume", "--persona", "recovery", "--state-dir", str(state_dir)]
            )

    def test_backoff_skip_preserves_streak(self) -> None:
        # THE key outage-masking safety property: a transport failure sets a
        # backoff window; the immediate next fire is skipped (returns 0, a systemd
        # success) — but that skip must NOT clear the streak, or a sustained outage
        # could be masked forever.
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            self.assertEqual(self._run_consume_transport_error(sd), 5)  # streak 1, backoff set
            self.assertEqual(load_soft_streak(sd, "recovery"), 1)
            # next fire lands inside the backoff window -> skipped, returns 0...
            self.assertEqual(self._run_consume_transport_error(sd), 0)
            # ...and the streak is preserved, not reset.
            self.assertEqual(load_soft_streak(sd, "recovery"), 1)

    def test_sustained_transport_outage_escalates_across_backoff(self) -> None:
        # A sustained transport outage still reaches the paging code once backoff
        # windows expire between real attempts (simulated by clearing backoff, which
        # is independent of the soft-streak state file).
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            for expected_streak in range(1, SOFT_FAILURE_ALERT_STREAK):
                self.assertEqual(self._run_consume_transport_error(sd), 5)
                self.assertEqual(load_soft_streak(sd, "recovery"), expected_streak)
                clear_backoff(sd, "recovery")  # simulate the backoff window expiring
            self.assertEqual(
                self._run_consume_transport_error(sd), EXIT_SOFT_PERSISTENT
            )

    def test_stats_errors_path_escalates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            for _ in range(SOFT_FAILURE_ALERT_STREAK - 1):
                self.assertEqual(self._run_consume_stats_errors(sd), 4)
                clear_backoff(sd, "recovery")
            self.assertEqual(
                self._run_consume_stats_errors(sd), EXIT_SOFT_PERSISTENT
            )

    def test_unit_success_exit_status_matches_soft_codes(self) -> None:
        # Guard the code<->unit contract: the template must treat exactly the soft
        # codes (3/4/5) as success and must NOT list the paging code, or the
        # discriminator silently breaks on a future edit to either side.
        unit = (
            Path(persona_bringup.__file__).parent
            / "systemd"
            / "pantheon-persona-consume@.service"
        )
        line = next(
            l for l in unit.read_text(encoding="utf-8").splitlines()
            if l.startswith("SuccessExitStatus=")
        )
        codes = {int(tok) for tok in line.split("=", 1)[1].split()}
        self.assertEqual(codes, {3, 4, 5})
        self.assertNotIn(EXIT_SOFT_PERSISTENT, codes)


if __name__ == "__main__":
    unittest.main()
