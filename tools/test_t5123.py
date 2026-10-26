"""T-5123: transcript acknowledgement requires evidence bytes plus a secret nonce."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tools import runtime_controller as rc
from tools import test_runtime_controller as trc
from tools.test_t5028_mr3b import (
    NONCE,
    PROMPTED,
    _Seat,
    _claude_end,
    _claude_read,
    _claude_result,
    _claude_tool,
    _codex_done,
    _codex_exec,
    _codex_meta,
    _codex_output,
)


class OutputNonceTests(_Seat):
    def _claude(self, command: str, *, content: object = "command completed\n", **result_overrides: object) -> str:
        self._transition(harness="claude")
        call = _claude_tool("Bash", {"command": command}, PROMPTED + timedelta(seconds=10))
        result = _claude_result(
            PROMPTED + timedelta(seconds=11), content=content, **result_overrides
        )
        self._claude_transcript([call, result, _claude_end(PROMPTED + timedelta(seconds=30))])
        return self._observe()

    def _codex(self, command: str, *, content: str = "command completed\n", **output_overrides: object) -> str:
        self._transition(harness="codex")
        self._codex_rollout([
            _codex_meta(PROMPTED),
            _codex_exec(command, PROMPTED + timedelta(seconds=10)),
            _codex_output(PROMPTED + timedelta(seconds=11), content=content, **output_overrides),
            _codex_done(PROMPTED + timedelta(seconds=30)),
        ])
        return self._observe()

    def _codex_raw_output(self, output: object) -> str:
        self._transition(harness="codex")
        result = _codex_output(PROMPTED + timedelta(seconds=11))
        result["payload"]["output"] = output
        self._codex_rollout([
            _codex_meta(PROMPTED),
            _codex_exec(f"cat {self.evidence}", PROMPTED + timedelta(seconds=10)),
            result,
            _codex_done(PROMPTED + timedelta(seconds=30)),
        ])
        return self._observe()

    def test_command_text_control_flow_can_never_supply_the_proof(self) -> None:
        commands = (
            f"true || cat {self.evidence}",
            f"false && cat {self.evidence}",
            f"exit 0; cat {self.evidence}",
            f"cat {self.evidence} > /dev/null",
            f"x=$(cat {self.evidence})",
            f"cat {self.evidence} | wc -l",
            f"cat {self.evidence} &",
        )
        for command in commands:
            with self.subTest(harness="claude", command=command):
                self.assertEqual(self._claude(command), "waiting")
            with self.subTest(harness="codex", command=command):
                self.assertEqual(self._codex(command), "waiting")

    def test_wrong_truncated_or_missing_nonce_never_acknowledges(self) -> None:
        outputs = ("# handoff\n", NONCE[:-1] + "\n", NONCE[:-1] + "b\n")
        for output in outputs:
            with self.subTest(harness="claude", output=output):
                self.assertEqual(self._claude(f"cat {self.evidence}", content=output), "waiting")
            with self.subTest(harness="codex", output=output):
                self.assertEqual(self._codex(f"cat {self.evidence}", content=output), "waiting")

    def test_honest_paired_successful_output_with_original_bytes_and_trailer_acknowledges(self) -> None:
        output = f"# handoff\n{NONCE}\n"
        self.assertEqual(self._claude(f"cat {self.evidence}", content=output), "acknowledged")
        self.assertEqual(
            self._claude(
                f"cat {self.evidence}",
                content=[{"type": "text", "text": output}],
            ),
            "acknowledged",
        )
        self.assertEqual(self._codex(f"cat {self.evidence}", content=output), "acknowledged")
        self.assertEqual(
            self._codex_raw_output({"metadata": {"exit_code": 0}, "output": output}),
            "acknowledged",
        )

    def test_codex_wrapper_fields_can_never_supply_the_proof(self) -> None:
        hostile_outputs = (
            f"Chunk ID: {NONCE}\nProcess exited with code 0\nOutput:\nno evidence bytes\n",
            {
                "metadata": {"exit_code": 0, "wrapper_note": NONCE},
                "output": "no evidence bytes\n",
            },
            f"Command: echo {NONCE}\nProcess exited with code 0\nOutput:\nno evidence bytes\n",
        )
        for output in hostile_outputs:
            with self.subTest(output=output):
                self.assertEqual(self._codex_raw_output(output), "waiting")

    def test_codex_missing_or_malformed_native_output_section_fails_closed(self) -> None:
        malformed_outputs = (
            f"Process exited with code 0\n{NONCE}\n",
            f"Process exited with code 0\nOutput: {NONCE}\n",
            {"metadata": {"exit_code": 0, "wrapper_note": NONCE}},
        )
        for output in malformed_outputs:
            with self.subTest(output=output):
                self.assertEqual(self._codex_raw_output(output), "waiting")

    def test_pair_result_and_turn_boundaries_remain_mandatory(self) -> None:
        output = f"# handoff\n{NONCE}\n"
        self.assertEqual(
            self._claude(f"cat {self.evidence}", content=output, tool_id="other"), "waiting"
        )
        self.assertEqual(
            self._claude(f"cat {self.evidence}", content=output, is_error=True), "waiting"
        )
        self.assertEqual(
            self._codex(f"cat {self.evidence}", content=output, call_id="other"), "waiting"
        )
        self.assertEqual(
            self._codex(f"cat {self.evidence}", content=output, code=None), "waiting"
        )
        self._transition(harness="claude")
        self._claude_transcript([
            _claude_read(self.evidence, PROMPTED + timedelta(seconds=10)),
            _claude_result(PROMPTED + timedelta(seconds=11), content=output),
        ])
        self.assertEqual(self._observe(), "waiting")
        self._transition(harness="codex")
        self._codex_rollout([
            _codex_meta(PROMPTED),
            _codex_exec(f"cat {self.evidence}", PROMPTED + timedelta(seconds=10)),
            _codex_output(PROMPTED + timedelta(seconds=11), content=output),
        ])
        self.assertEqual(self._observe(), "waiting")

    def test_missing_or_malformed_proof_state_and_copy_fail_closed(self) -> None:
        cases = (
            {"successor_read_nonce": None},
            {"successor_read_nonce": "not-a-valid-nonce"},
            {"successor_evidence_path": None},
            {"successor_evidence_sha256": None},
            {"successor_evidence_sha256": "0" * 64},
        )
        for override in cases:
            with self.subTest(override=override):
                self._transition(harness="codex", **override)
                self._codex_rollout([
                    _codex_meta(PROMPTED),
                    _codex_exec(f"cat {self.evidence}", PROMPTED + timedelta(seconds=10)),
                    _codex_output(PROMPTED + timedelta(seconds=11), content=f"# handoff\n{NONCE}\n"),
                    _codex_done(PROMPTED + timedelta(seconds=30)),
                ])
                self.assertEqual(self._observe(), "waiting")
        self._transition(harness="codex")
        Path(self.evidence).write_text("# handoff without trailer\n", encoding="utf-8")
        self.assertEqual(self._observe(), "waiting")
        self._transition(harness="codex")
        Path(self.evidence).write_text(NONCE + "\n", encoding="utf-8")
        Path(self.evidence).chmod(0o600)
        self.assertEqual(self._observe(), "waiting")
        self._transition(harness="codex")
        Path(self.evidence).chmod(0o644)
        self.assertEqual(self._observe(), "waiting")
        self._transition(harness="codex")
        Path(self.evidence).unlink()
        self.assertEqual(self._observe(), "waiting")

    def test_malformed_digest_refuses_before_copy_validation(self) -> None:
        self._transition(harness="codex", successor_evidence_sha256="malformed")
        with mock.patch.object(rc, "_successor_copy_is_valid") as validate:
            self.assertIsNone(rc._successor_read_proof(self._state()))
        validate.assert_not_called()


class SuccessorEvidenceCopyTests(unittest.TestCase):
    def test_prompt_refuses_partial_proof_state_before_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = trc._binding(root, harness="codex", profile="fallback")
            original = root / "chronicle.md"
            original.write_text("# original\n", encoding="utf-8")
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, {
                "transition_id": "t-1", "phase": "awaiting-successor-read",
                "expected_successor_read_evidence": "hook:x",
                "successor_evidence_path": str(rc._successor_evidence_copy_path("recovery", root)),
            })
            before = transition_path.read_bytes()
            handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, "s", rc._utc_now())
            with mock.patch.object(rc, "submit_to_runtime") as submit:
                with self.assertRaisesRegex(RuntimeError, "proof state is malformed"):
                    rc._prompt_successor(handle, original, persona="recovery", state_root=root)
            self.assertEqual(transition_path.read_bytes(), before)
            self.assertFalse(rc._successor_evidence_copy_path("recovery", root).exists())
            submit.assert_not_called()

    def test_prompt_publishes_one_private_bounded_copy_then_state_without_leaking_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = trc._binding(root, harness="codex", profile="fallback")
            original = root / "chronicle.md"
            original_bytes = b"# original continuity\nimportant bytes\n"
            original.write_bytes(original_bytes)
            rc._atomic_json(rc._transition_path("recovery", root), {
                "schema": "pantheon-runtime-transition/v1",
                "transition_id": "t-1",
                "phase": "awaiting-successor-read",
                "expected_successor_read_evidence": "hook:x",
            })
            handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, "s", rc._utc_now())
            with mock.patch.object(rc, "submit_to_runtime") as submit, mock.patch.object(
                rc, "_append_controller_evidence"
            ) as append:
                rc._prompt_successor(handle, original, persona="recovery", state_root=root)
                first = rc.transition_state("recovery", state_root=root)
                rc._prompt_successor(handle, original, persona="recovery", state_root=root)
                second = rc.transition_state("recovery", state_root=root)

            copy_path = Path(first["successor_evidence_path"])
            nonce = first["successor_read_nonce"]
            payload = copy_path.read_bytes()
            self.assertNotEqual(copy_path, original)
            self.assertEqual(first["successor_evidence_path"], second["successor_evidence_path"])
            self.assertEqual(nonce, second["successor_read_nonce"])
            self.assertEqual(first["successor_evidence_sha256"], second["successor_evidence_sha256"])
            self.assertEqual(stat.S_IMODE(copy_path.stat().st_mode), 0o600)
            self.assertTrue(payload.startswith(original_bytes))
            self.assertTrue(payload.endswith((nonce + "\n").encode()))
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertEqual(len(list(copy_path.parent.glob("successor-evidence*"))), 1)
            self.assertNotIn(nonce, submit.call_args.args[1].prompt)
            self.assertIn(str(copy_path), submit.call_args.args[1].prompt)
            self.assertNotIn(nonce, repr(append.call_args_list))

    def test_failed_state_publication_leaves_only_the_single_private_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = trc._binding(root, harness="codex", profile="fallback")
            original = root / "chronicle.md"
            original.write_text("# original\n", encoding="utf-8")
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, {
                "transition_id": "t-1", "phase": "awaiting-successor-read",
                "expected_successor_read_evidence": "hook:x",
            })
            before = transition_path.read_bytes()
            handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, "s", rc._utc_now())
            with mock.patch.object(rc, "_atomic_json", side_effect=OSError("state write failed")), \
                    mock.patch.object(rc, "submit_to_runtime") as submit:
                with self.assertRaisesRegex(OSError, "state write failed"):
                    rc._prompt_successor(handle, original, persona="recovery", state_root=root)
            copies = list((rc._persona_state("recovery", root)).glob("successor-evidence*"))
            self.assertEqual(len(copies), 1)
            self.assertEqual(stat.S_IMODE(copies[0].stat().st_mode), 0o600)
            self.assertEqual(transition_path.read_bytes(), before)
            submit.assert_not_called()

    def test_retry_repairs_a_damaged_copy_with_the_same_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = trc._binding(root, harness="codex", profile="fallback")
            original = root / "chronicle.md"
            original.write_text("# original\ncontinuity\n", encoding="utf-8")
            rc._atomic_json(rc._transition_path("recovery", root), {
                "transition_id": "t-1", "phase": "awaiting-successor-read",
                "expected_successor_read_evidence": "hook:x",
            })
            handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, "s", rc._utc_now())
            with mock.patch.object(rc, "submit_to_runtime") as submit:
                rc._prompt_successor(handle, original, persona="recovery", state_root=root)
                first = rc.transition_state("recovery", state_root=root)
                copy_path = Path(first["successor_evidence_path"])
                expected_payload = copy_path.read_bytes()
                copy_path.write_text("damaged\n", encoding="utf-8")
                copy_path.chmod(0o600)
                rc._prompt_successor(handle, original, persona="recovery", state_root=root)
                second = rc.transition_state("recovery", state_root=root)
            self.assertEqual(submit.call_count, 2)
            self.assertEqual(first["successor_read_nonce"], second["successor_read_nonce"])
            self.assertEqual(first["successor_evidence_sha256"], second["successor_evidence_sha256"])
            self.assertEqual(copy_path.read_bytes(), expected_payload)
            self.assertIsNotNone(rc._successor_read_proof(second))

    def test_retry_refuses_changed_source_and_leaves_state_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = trc._binding(root, harness="codex", profile="fallback")
            original = root / "chronicle.md"
            original.write_text("# original\ncontinuity\n", encoding="utf-8")
            transition_path = rc._transition_path("recovery", root)
            rc._atomic_json(transition_path, {
                "transition_id": "t-1", "phase": "awaiting-successor-read",
                "expected_successor_read_evidence": "hook:x",
            })
            handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, "s", rc._utc_now())
            with mock.patch.object(rc, "submit_to_runtime") as submit:
                rc._prompt_successor(handle, original, persona="recovery", state_root=root)
                first = rc.transition_state("recovery", state_root=root)
                before = transition_path.read_bytes()
                copy_path = Path(first["successor_evidence_path"])
                copy_path.write_text("damaged\n", encoding="utf-8")
                copy_path.chmod(0o600)
                original.write_text("# changed\ncontinuity\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "source changed after publication"):
                    rc._prompt_successor(handle, original, persona="recovery", state_root=root)
            self.assertEqual(submit.call_count, 1)
            self.assertEqual(transition_path.read_bytes(), before)
            self.assertIsNone(rc._successor_read_proof(rc.transition_state("recovery", state_root=root)))

    def test_same_process_prompt_to_native_output_to_observer_acknowledges(self) -> None:
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                binding = trc._binding(root, harness=harness, profile="fallback")
                binding.account_home.mkdir(parents=True, exist_ok=True)
                binding.launch_cwd.mkdir(parents=True, exist_ok=True)
                original = root / "chronicle.md"
                original.write_text("# original\ncontinuity\n", encoding="utf-8")
                rc._atomic_json(rc._transition_path("recovery", root), {
                    "schema": "pantheon-runtime-transition/v1", "transition_id": "t-1",
                    "phase": "awaiting-successor-read", "trigger": "planned-quota",
                    "incoming_harness": harness, "incoming_profile": "fallback",
                    "expected_successor_read_evidence": "hook:x", "chronicle_evidence": str(original),
                })
                handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, "s", rc._utc_now())
                with mock.patch.object(rc, "submit_to_runtime"):
                    rc._prompt_successor(handle, original, persona="recovery", state_root=root)
                state = rc.transition_state("recovery", state_root=root)
                evidence_path = state["successor_evidence_path"]
                output = Path(evidence_path).read_text(encoding="utf-8")
                prompted = rc._parse_iso_or_none(state["successor_prompted_at"])
                assert prompted is not None
                if harness == "claude":
                    transcript = binding.account_home / ".claude" / "projects"
                    from tools.harness_adapter.integration import claude_project_slug
                    transcript = transcript / claude_project_slug(binding.launch_cwd) / "s1.jsonl"
                    rows = [_claude_read(evidence_path, prompted + timedelta(seconds=1)),
                            _claude_result(prompted + timedelta(seconds=2), content=output),
                            _claude_end(prompted + timedelta(seconds=3))]
                else:
                    day = prompted.strftime("%Y/%m/%d")
                    transcript = binding.account_home / ".codex" / "sessions" / day / "rollout-2026-10-18T00-00-00-01a1.jsonl"
                    rows = [_codex_meta(prompted), _codex_exec(f"cat {evidence_path}", prompted + timedelta(seconds=1)),
                            _codex_output(prompted + timedelta(seconds=2), content=output),
                            _codex_done(prompted + timedelta(seconds=3))]
                transcript.parent.mkdir(parents=True, exist_ok=True)
                transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                with mock.patch.object(rc, "_route_failure_alert"):
                    result = rc.observe_successor_read(
                        "recovery", binding=binding, state_root=root, now=prompted + timedelta(minutes=1)
                    )
                self.assertEqual(result, "acknowledged")
                observation = rc.transition_state("recovery", state_root=root)["successor_read_observation"]
                self.assertNotIn(state["successor_read_nonce"], repr(observation))


class NaiveStartedAtTests(_Seat):
    def test_naive_runtime_start_waits_then_escalates_once(self) -> None:
        self._transition(harness="codex")
        rc._atomic_json(
            rc._persona_state(self.persona, self.state) / "runtime.json",
            {"harness": "codex", "started_at": "2026-10-16T03:00:00"},
        )
        self.assertEqual(self._observe(now=PROMPTED + timedelta(minutes=5)), "waiting")
        late = PROMPTED + timedelta(seconds=rc.SUCCESSOR_ACK_TIMEOUT_SECONDS + 1)
        self.assertEqual(self._observe(now=late), "escalated")
        self.assertEqual(self._observe(now=late + timedelta(minutes=1)), "escalated-already")
        self.routed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
