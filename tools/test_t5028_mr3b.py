"""T-5028 MR-3b (h): the structural ack-read.

T-5026: a codex successor ignored the typed ack-read prompt, so the transition
stayed awaiting-successor-read and mail never reached the seat. The controller
now observes the successor's native transcript on each consume/quota fire: a
tool call naming the continuity evidence, then a completed turn, after the
prompt, acknowledges the transfer through the same validator a typed ack uses;
otherwise, past SUCCESSOR_ACK_TIMEOUT_SECONDS, one escalation per transition.

Fixtures are trimmed copies of REAL record shapes captured on this account on
2026-10-17: Claude Code ~/.claude/projects/<slug>/<session>.jsonl (assistant
tool_use Read{file_path} / stop_reason end_turn, isSidechain) and codex
~/.codex/sessions/.../rollout-*.jsonl (session_meta with/without
parent_thread_id; response_item function_call name=exec_command with a
JSON-string `arguments` {"cmd", "workdir", ...}; event_msg task_complete).
tmp_path stand-ins only; alerts are mocked.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import persona_bringup
from tools import runtime_controller as rc
from tools import test_runtime_controller as trc
from tools.harness_adapter import integration as hi

PROMPTED = datetime(2026, 10, 17, 3, 0, 0, tzinfo=timezone.utc)
NONCE = "pantheon-successor-read-nonce:v1:" + "a" * 64


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _claude_read(evidence: str, at: datetime) -> dict:
    return {"type": "assistant", "isSidechain": False, "timestamp": _iso(at), "sessionId": "s1",
            "message": {"role": "assistant", "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read", "caller": None,
                                     "input": {"file_path": evidence}}]}}


def _claude_tool(name: str, tool_input: dict, at: datetime, tool_id: str = "toolu_1") -> dict:
    return {"type": "assistant", "isSidechain": False, "timestamp": _iso(at), "sessionId": "s1",
            "message": {"role": "assistant", "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": tool_id, "name": name, "caller": None, "input": tool_input}]}}


def _claude_result(
    at: datetime, *, tool_id: str = "toolu_1", is_error: bool | None = False,
    content: object = f"# handoff\n{NONCE}\n",
) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    return {"type": "user", "isSidechain": False, "timestamp": _iso(at), "sessionId": "s1",
            "message": {"role": "user", "content": [block]}}


def _claude_end(at: datetime) -> dict:
    return {"type": "assistant", "isSidechain": False, "timestamp": _iso(at), "sessionId": "s1",
            "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "Read it."}]}}


def _codex_meta(at: datetime, *, parent: str | None = None, source: object = "cli") -> dict:
    payload = {"session_id": "01a1", "id": "01a1", "timestamp": _iso(at), "cwd": "/home/x",
               "originator": "codex_exec" if source == "exec" else "codex-tui", "source": source}
    if parent:
        payload["parent_thread_id"] = parent
    return {"timestamp": _iso(at), "ordinal": 0, "type": "session_meta", "payload": payload}


def _codex_call(evidence: str, at: datetime) -> dict:
    args = {"cmd": f"cat {evidence}", "workdir": "/home/x", "yield_time_ms": 10000, "max_output_tokens": 12000}
    return {"timestamp": _iso(at), "ordinal": 7, "type": "response_item",
            "payload": {"type": "function_call", "id": "fc_1", "name": "exec_command",
                        "arguments": json.dumps(args), "call_id": "call_1"}}


def _codex_exec(cmd: str, at: datetime, call_id: str = "call_1", workdir: str = "/home/x") -> dict:
    args = {"cmd": cmd, "workdir": workdir, "yield_time_ms": 10000, "max_output_tokens": 12000}
    return {"timestamp": _iso(at), "ordinal": 7, "type": "response_item",
            "payload": {"type": "function_call", "id": "fc_1", "name": "exec_command",
                        "arguments": json.dumps(args), "call_id": call_id}}


def _codex_output(
    at: datetime, *, code: int | None = 0, call_id: str = "call_1",
    content: str = f"# handoff\n{NONCE}\n",
) -> dict:
    status = f"Process exited with code {code}\n" if code is not None else "Process running with session ID 7\n"
    text = f"Chunk ID: 29fc0e\nWall time: 0.0000 seconds\n{status}Original token count: 3\nOutput:\n{content}"
    return {"timestamp": _iso(at), "ordinal": 8, "type": "response_item",
            "payload": {"type": "function_call_output", "id": "fco_1", "call_id": call_id, "output": text}}


def _codex_done(at: datetime) -> dict:
    return {"timestamp": _iso(at), "ordinal": 9, "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": "read",
                        "started_at": 1, "completed_at": 2, "duration_ms": 1}}


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


class _Seat(unittest.TestCase):
    persona = "recovery"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.state = base / "personas"
        self.home = base / "home"
        self.cwd = base / "cwd"
        self.cwd.mkdir(parents=True)
        self.evidence = str(base / "chronicle" / "handoff-evidence.md")
        self.binding = SimpleNamespace(account_home=self.home, launch_cwd=self.cwd)
        self.alert = mock.patch.object(rc, "_route_failure_alert")
        self.routed = self.alert.start()
        self.addCleanup(self.alert.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _transition(self, *, harness: str = "codex", prompted: datetime | None = PROMPTED, **extra) -> Path:
        evidence_path = Path(self.evidence)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(f"# handoff\n{NONCE}\n", encoding="utf-8")
        evidence_path.chmod(0o600)
        evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        state = {"schema": "pantheon-runtime-transition/v1", "transition_id": "t-1", "phase": "awaiting-successor-read",
                 "trigger": "planned-quota", "incoming_harness": harness, "incoming_profile": "p",
                 "expected_successor_read_evidence": "hook:runtime-transfer:t-1:handoff-evidence.md",
                 "chronicle_evidence": self.evidence, "successor_evidence_path": self.evidence,
                 "successor_read_nonce": NONCE, "successor_evidence_sha256": evidence_sha256, **extra}
        if prompted is not None:
            state["successor_prompted_at"] = _iso(prompted)
        path = rc._transition_path(self.persona, self.state)
        rc._atomic_json(path, state)
        return path

    def _claude_transcript(self, rows: list[dict], name: str = "s1") -> Path:
        path = self.home / ".claude" / "projects" / hi.claude_project_slug(self.cwd) / f"{name}.jsonl"
        return _write_jsonl(path, rows)

    def _codex_rollout(self, rows: list[dict], name: str = "rollout-2026-10-17T03-00-01-01a1") -> Path:
        return _write_jsonl(self.home / ".codex" / "sessions" / "2026" / "10" / "17" / f"{name}.jsonl", rows)

    def _observe(self, now: datetime | None = None) -> str:
        return rc.observe_successor_read(self.persona, binding=self.binding, state_root=self.state,
                                         now=now or PROMPTED + timedelta(minutes=5))

    def _state(self) -> dict:
        return rc.transition_state(self.persona, state_root=self.state)


class StructuralAckTests(_Seat):
    def test_claude_read_then_end_turn_acknowledges_with_the_observation(self) -> None:
        self._transition(harness="claude")
        path = self._claude_transcript([_claude_read(self.evidence, PROMPTED + timedelta(seconds=20)),
                                        _claude_result(PROMPTED + timedelta(seconds=21)),
                                        _claude_end(PROMPTED + timedelta(seconds=40))])
        self.assertEqual(self._observe(), "acknowledged")
        state = self._state()
        self.assertEqual(state["phase"], "completed")
        self.assertTrue(state["successor_read_ack"])
        self.assertEqual(state["successor_read_evidence"], "hook:runtime-transfer:t-1:handoff-evidence.md")
        observation = state["successor_read_observation"]
        self.assertEqual((observation["transcript"], observation["read_line_in_slice"],
                          observation["completed_turn_line_in_slice"]), (str(path), 2, 3))
        self.assertEqual(observation["slice_start_byte"], 0)
        self.assertEqual(observation["slice_end_byte"], path.stat().st_size)
        self.assertEqual(observation["observed_by"], "controller-transcript")
        events = trc._evidence_events(self.state)
        self.assertIn("successor_read_acknowledged", events)

    def test_codex_read_then_task_complete_acknowledges(self) -> None:
        self._transition(harness="codex")
        self._codex_rollout([_codex_meta(PROMPTED), _codex_call(self.evidence, PROMPTED + timedelta(seconds=10)),
                             _codex_output(PROMPTED + timedelta(seconds=11)),
                             _codex_done(PROMPTED + timedelta(seconds=30))])
        self.assertEqual(self._observe(), "acknowledged")
        self.assertEqual(self._state()["successor_read_observation"]["kind"], "codex-read+task-complete")

    def test_codex_worker_rollouts_do_not_make_it_ambiguous(self) -> None:
        self._transition(harness="codex")
        self._codex_rollout([_codex_meta(PROMPTED), _codex_call(self.evidence, PROMPTED + timedelta(seconds=10)),
                             _codex_output(PROMPTED + timedelta(seconds=11)),
                             _codex_done(PROMPTED + timedelta(seconds=30))])
        self._codex_rollout([_codex_meta(PROMPTED, parent="01a1"), _codex_done(PROMPTED + timedelta(seconds=5))],
                            name="rollout-2026-10-17T03-00-02-01a2")
        self.assertEqual(self._observe(), "acknowledged")

    def test_a_completed_turn_without_the_read_does_not_ack(self) -> None:
        self._transition(harness="codex")
        self._codex_rollout([_codex_meta(PROMPTED), _codex_call("/some/other/file", PROMPTED + timedelta(seconds=10)),
                             _codex_done(PROMPTED + timedelta(seconds=30))])
        self.assertEqual(self._observe(), "waiting")
        self.assertEqual(self._state()["phase"], "awaiting-successor-read")

    def test_a_read_without_a_completed_turn_does_not_ack(self) -> None:
        self._transition(harness="claude")
        self._claude_transcript([_claude_read(self.evidence, PROMPTED + timedelta(seconds=20))])
        self.assertEqual(self._observe(), "waiting")

    def test_a_read_from_before_the_prompt_does_not_ack(self) -> None:
        self._transition(harness="claude")
        self._claude_transcript([_claude_read(self.evidence, PROMPTED - timedelta(minutes=1)),
                                 _claude_result(PROMPTED - timedelta(seconds=50)),
                                 _claude_end(PROMPTED + timedelta(seconds=40))])
        self.assertEqual(self._observe(), "waiting")

    def test_a_sidechain_read_does_not_ack(self) -> None:
        self._transition(harness="claude")
        read = _claude_read(self.evidence, PROMPTED + timedelta(seconds=20))
        read["isSidechain"] = True
        self._claude_transcript([read, _claude_result(PROMPTED + timedelta(seconds=21)), _claude_end(PROMPTED + timedelta(seconds=40))])
        self.assertEqual(self._observe(), "waiting")

    def test_an_ambiguous_transcript_never_acks(self) -> None:
        self._transition(harness="claude")
        rows = [_claude_read(self.evidence, PROMPTED + timedelta(seconds=20)), _claude_result(PROMPTED + timedelta(seconds=21)),
                _claude_end(PROMPTED + timedelta(seconds=40))]
        self._claude_transcript(rows, name="s1")
        self._claude_transcript(rows, name="s2")
        self.assertEqual(self._observe(), "waiting")

    def test_a_typed_ack_first_leaves_nothing_to_observe(self) -> None:
        self._transition(harness="claude")
        rc.acknowledge_successor_read(self.persona, evidence="hook:runtime-transfer:t-1:handoff-evidence.md",
                                      state_root=self.state)
        self.assertEqual(self._observe(), "not-awaiting")
        self.assertNotIn("successor_read_observation", self._state())

    def test_operator_trigger_hash_mismatch_is_refused_by_the_validator(self) -> None:
        handoff = Path(self.evidence)
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text("# handoff\n", encoding="utf-8")
        self._transition(harness="claude", trigger="operator", operator_handoff_sha256="0" * 64)
        self._claude_transcript([_claude_read(self.evidence, PROMPTED + timedelta(seconds=20)),
                                 _claude_result(PROMPTED + timedelta(seconds=21)),
                                 _claude_end(PROMPTED + timedelta(seconds=40))])
        self.assertEqual(self._observe(), "ack-refused")
        self.assertEqual(self._state()["phase"], "awaiting-successor-read")
        # The refusal does not suppress the one escalation.
        late = PROMPTED + timedelta(seconds=rc.SUCCESSOR_ACK_TIMEOUT_SECONDS + 1)
        self.assertEqual(self._observe(now=late), "escalated")
        self.assertIn("refused", self.routed.call_args.args[1])


class FalseAckTests(_Seat):
    """REBOUND 1 MAJOR: only a SUCCESSFUL, read-shaped call on the exact path acks."""

    def _claude(self, rows):
        self._transition(harness="claude")
        self._claude_transcript(rows + [_claude_end(PROMPTED + timedelta(seconds=40))])
        return self._observe()

    def _codex(self, rows):
        self._transition(harness="codex")
        self._codex_rollout([_codex_meta(PROMPTED), *rows, _codex_done(PROMPTED + timedelta(seconds=30))])
        return self._observe()

    def test_a_claude_read_whose_result_is_an_error_does_not_ack(self) -> None:  # (a)
        self.assertEqual(self._claude([_claude_read(self.evidence, PROMPTED + timedelta(seconds=20)),
                                       _claude_result(PROMPTED + timedelta(seconds=21), is_error=True)]), "waiting")

    def test_a_codex_cat_that_exits_nonzero_does_not_ack(self) -> None:  # (b)
        self.assertEqual(self._codex([_codex_exec(f"cat {self.evidence}", PROMPTED + timedelta(seconds=10)),
                                      _codex_output(PROMPTED + timedelta(seconds=11), code=1)]), "waiting")

    def test_a_tool_that_merely_mentions_the_path_does_not_ack(self) -> None:  # (c)
        todo = _claude_tool("TodoWrite", {"todos": [{"content": f"read {self.evidence}", "status": "pending"}]},
                            PROMPTED + timedelta(seconds=20))
        self.assertEqual(self._claude([todo, _claude_result(PROMPTED + timedelta(seconds=21))]), "waiting")
        echo = _codex_exec(f"echo {self.evidence}", PROMPTED + timedelta(seconds=10))
        self.assertEqual(self._codex([echo, _codex_output(PROMPTED + timedelta(seconds=11))]), "waiting")

    def test_a_substring_path_does_not_ack(self) -> None:  # (d)
        bak = _claude_tool("Read", {"file_path": self.evidence + ".bak"}, PROMPTED + timedelta(seconds=20))
        self.assertEqual(self._claude([bak, _claude_result(PROMPTED + timedelta(seconds=21))]), "waiting")
        cat_bak = _codex_exec(f"cat {self.evidence}.bak", PROMPTED + timedelta(seconds=10))
        self.assertEqual(self._codex([cat_bak, _codex_output(PROMPTED + timedelta(seconds=11))]), "waiting")

    def test_a_missing_or_unparseable_result_does_not_ack(self) -> None:
        self.assertEqual(self._claude([_claude_read(self.evidence, PROMPTED + timedelta(seconds=20))]), "waiting")
        self.assertEqual(self._codex([_codex_exec(f"cat {self.evidence}", PROMPTED + timedelta(seconds=10)),
                                      _codex_output(PROMPTED + timedelta(seconds=11), code=None)]), "waiting")

    def test_a_result_for_a_different_call_does_not_ack(self) -> None:
        self.assertEqual(self._codex([_codex_exec(f"cat {self.evidence}", PROMPTED + timedelta(seconds=10)),
                                      _codex_output(PROMPTED + timedelta(seconds=11), call_id="call_other")]), "waiting")

    def test_positive_controls(self) -> None:
        for cmd in (f"cat {self.evidence}", f"sed -n '1,200p' {self.evidence}", f"head -n 50 {self.evidence} | cat",
                    f"cd /tmp && cat < {self.evidence}"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._codex([_codex_exec(cmd, PROMPTED + timedelta(seconds=10)),
                                              _codex_output(PROMPTED + timedelta(seconds=11))]), "acknowledged")
        relative = Path(self.evidence)
        self.assertEqual(self._codex([_codex_exec(f"cat {relative.name}", PROMPTED + timedelta(seconds=10),
                                                  workdir=str(relative.parent)),
                                      _codex_output(PROMPTED + timedelta(seconds=11))]), "acknowledged")
        bash = _claude_tool("Bash", {"command": f"cat {self.evidence}"}, PROMPTED + timedelta(seconds=20))
        self.assertEqual(self._claude([bash, _claude_result(PROMPTED + timedelta(seconds=21), is_error=None)]),
                         "acknowledged")


class BoundedReadTests(_Seat):
    """REBOUND 1 MINOR 1 + MINOR 3."""

    def test_the_resolved_rollout_is_cached_and_reused_without_a_search(self) -> None:
        self._transition(harness="codex")
        rollout = self._codex_rollout([_codex_meta(PROMPTED)])
        self.assertEqual(self._observe(), "waiting")
        self.assertEqual(self._state()["successor_transcript"], str(rollout))
        _write_jsonl(rollout, [_codex_meta(PROMPTED), _codex_exec(f"cat {self.evidence}", PROMPTED + timedelta(seconds=10)),
                               _codex_output(PROMPTED + timedelta(seconds=11)), _codex_done(PROMPTED + timedelta(seconds=30))])
        real = hi._codex_top_level_rollouts
        with mock.patch.object(hi, "_codex_top_level_rollouts", wraps=real) as search:
            self.assertEqual(self._observe(), "acknowledged")
        # Only the bounded "anything newer than the cache?" check runs, never a fresh resolve.
        self.assertTrue(search.call_args_list)
        self.assertTrue(all(call.kwargs.get("newer_than") is not None for call in search.call_args_list))

    def test_a_vanished_cached_rollout_is_re_resolved(self) -> None:
        self._transition(harness="codex", successor_transcript=str(self.home / "gone.jsonl"))
        self._codex_rollout([_codex_meta(PROMPTED), _codex_exec(f"cat {self.evidence}", PROMPTED + timedelta(seconds=10)),
                             _codex_output(PROMPTED + timedelta(seconds=11)), _codex_done(PROMPTED + timedelta(seconds=30))])
        self.assertEqual(self._observe(), "acknowledged")

    def test_only_a_bounded_tail_is_read_and_hashed(self) -> None:
        self._transition(harness="claude")
        filler = {"type": "file-history-snapshot", "snapshot": "x" * 4000}
        rows = [filler] * 40 + [_claude_read(self.evidence, PROMPTED + timedelta(seconds=20)),
                                _claude_result(PROMPTED + timedelta(seconds=21)), _claude_end(PROMPTED + timedelta(seconds=40))]
        path = self._claude_transcript(rows)
        with mock.patch.object(hi, "TRANSCRIPT_READ_CAP_BYTES", 20_000):
            self.assertEqual(self._observe(), "acknowledged")
        observation = self._state()["successor_read_observation"]
        self.assertGreater(observation["slice_start_byte"], 0)
        self.assertLessEqual(observation["slice_end_byte"] - observation["slice_start_byte"], 20_000)
        self.assertEqual(observation["slice_end_byte"], path.stat().st_size)

    def test_codex_exec_runs_are_not_candidates(self) -> None:
        self._transition(harness="codex")
        self._codex_rollout([_codex_meta(PROMPTED), _codex_exec(f"cat {self.evidence}", PROMPTED + timedelta(seconds=10)),
                             _codex_output(PROMPTED + timedelta(seconds=11)), _codex_done(PROMPTED + timedelta(seconds=30))])
        self._codex_rollout([_codex_meta(PROMPTED, source="exec"), _codex_done(PROMPTED + timedelta(seconds=5))],
                            name="rollout-2026-10-17T03-00-03-01a3")
        self.assertEqual(self._observe(), "acknowledged")


class DeltaReviewShellTests(unittest.TestCase):
    """Delta review item 1/2: non-read and partial-read commands never count."""

    EV = "/srv/chronicle/handoff-evidence.md"

    def test_non_reads(self) -> None:
        for cmd in (f"sed -i 's/a/b/' {self.EV}", f"sed --in-place=.bak 's/a/b/' {self.EV}", f"sed -ni 'p' {self.EV}",
                    f"cat {self.EV} > /tmp/copy", f"cat {self.EV} >> /tmp/copy", f"cat {self.EV} > /dev/null",
                    f"cat {self.EV} 1> /dev/null", f"cat {self.EV} &> /dev/null",
                    f"head -c 0 {self.EV}", f"head -n 0 {self.EV}", f"head -n0 {self.EV}", f"head --lines=0 {self.EV}",
                    f"tail {self.EV}", f"tail -n 50 {self.EV}", f"sed -n '5,10p' {self.EV}", f"sed -n '20p' {self.EV}"):
            with self.subTest(cmd=cmd):
                self.assertFalse(hi.shell_reads_path(cmd, self.EV))

    def test_reads(self) -> None:
        for cmd in (f"cat {self.EV}", f"cat {self.EV} 2> /dev/null", f"sed -n '1,200p' {self.EV}", f"sed -n p {self.EV}",
                    f"sed 's/x/y/' {self.EV}", f"head -n 50 {self.EV}", f"head {self.EV}", f"cat < {self.EV}", f"nl {self.EV} | less"):
            with self.subTest(cmd=cmd):
                self.assertTrue(hi.shell_reads_path(cmd, self.EV))


class DeltaReviewTranscriptTests(_Seat):
    def _claude(self, tool_input):
        self._transition(harness="claude")
        self._claude_transcript([_claude_tool("Read", tool_input, PROMPTED + timedelta(seconds=20)),
                                 _claude_result(PROMPTED + timedelta(seconds=21)),
                                 _claude_end(PROMPTED + timedelta(seconds=40))])
        return self._observe()

    def test_claude_read_with_a_limit_still_counts(self) -> None:
        self.assertEqual(self._claude({"file_path": self.evidence, "limit": 200}), "acknowledged")

    def test_claude_read_from_offset_1_counts(self) -> None:
        self.assertEqual(self._claude({"file_path": self.evidence, "offset": 1, "limit": 200}), "acknowledged")

    def test_claude_read_past_the_start_is_refused(self) -> None:
        self.assertEqual(self._claude({"file_path": self.evidence, "offset": 50, "limit": 200}), "waiting")

    def _codex_ack_rows(self, at):
        return [_codex_meta(at), _codex_exec(f"cat {self.evidence}", PROMPTED + timedelta(seconds=10)),
                _codex_output(PROMPTED + timedelta(seconds=11)), _codex_done(PROMPTED + timedelta(seconds=30))]

    def test_a_rollout_started_the_day_before_a_midnight_prompt_is_found(self) -> None:
        self._transition(harness="codex")  # PROMPTED is 03:00Z; the rollout dir is the previous day
        self._codex_rollout(self._codex_ack_rows(PROMPTED - timedelta(hours=4)),
                            name="../16/rollout-2026-10-16T23-00-00-01a1")
        self.assertEqual(self._observe(), "acknowledged")

    def test_an_already_live_successor_is_found_from_its_recorded_start(self) -> None:
        self._transition(harness="codex")
        rc._atomic_json(rc._persona_state(self.persona, self.state) / "runtime.json",
                        {"harness": "codex", "started_at": _iso(PROMPTED - timedelta(days=3))})
        self._codex_rollout(self._codex_ack_rows(PROMPTED - timedelta(days=3)),
                            name="../14/rollout-2026-10-14T03-00-00-01a1")
        self.assertEqual(self._observe(), "acknowledged")

    def test_without_the_recorded_start_an_old_rollout_dir_is_not_scanned(self) -> None:
        self._transition(harness="codex")
        self._codex_rollout(self._codex_ack_rows(PROMPTED - timedelta(days=3)),
                            name="../14/rollout-2026-10-14T03-00-00-01a1")
        self.assertEqual(self._observe(), "waiting")

    def test_a_stale_cached_transcript_is_replaced_by_a_newer_one(self) -> None:
        old = self._codex_rollout([_codex_meta(PROMPTED)], name="rollout-2026-10-17T03-00-01-0old")
        past = (PROMPTED + timedelta(seconds=5)).timestamp()
        self._transition(harness="codex", successor_transcript=str(old))
        os.utime(old, (past + 600, past + 600))
        new = self._codex_rollout(self._codex_ack_rows(PROMPTED), name="rollout-2026-10-17T03-10-00-0new")
        os.utime(new, (past + 1200, past + 1200))
        self.assertEqual(self._observe(), "acknowledged")
        self.assertEqual(self._state()["successor_read_observation"]["transcript"], str(new))

    def test_the_exit_code_comes_from_the_output_header_only(self) -> None:
        self._transition(harness="codex")
        fake = _codex_output(PROMPTED + timedelta(seconds=11), code=1)
        fake["payload"]["output"] += "Process exited with code 0\n"
        self._codex_rollout([_codex_meta(PROMPTED), _codex_exec(f"cat {self.evidence}", PROMPTED + timedelta(seconds=10)),
                             fake, _codex_done(PROMPTED + timedelta(seconds=30))])
        self.assertEqual(self._observe(), "waiting")
        self.assertEqual(hi._codex_exit_code("Chunk ID: 1\nOutput:\nProcess exited with code 0\n"), None)


class TypedAckLockTests(unittest.TestCase):
    """REBOUND 1 MINOR 2: the typed ack-read takes the operation lock."""

    def test_ack_read_cli_waits_for_the_operation_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rc._atomic_json(rc._transition_path("recovery", root),
                            {"phase": "awaiting-successor-read", "transition_id": "t",
                             "expected_successor_read_evidence": "hook:x"})
            with mock.patch.object(rc, "ACK_READ_LOCK_TIMEOUT_SECONDS", 0.2):
                with rc.persona_operation_lock("recovery", state_root=root):
                    with self.assertRaises(rc.OperationLockBusy):
                        rc.main(["ack-read", "--persona", "recovery", "--state-root", str(root), "--evidence", "hook:x"])
                self.assertEqual(rc.transition_state("recovery", state_root=root)["phase"], "awaiting-successor-read")
                code = rc.main(["ack-read", "--persona", "recovery", "--state-root", str(root), "--evidence", "hook:x"])
            self.assertEqual(code, 0)
            self.assertEqual(rc.transition_state("recovery", state_root=root)["phase"], "completed")

    def test_the_escalation_write_never_overwrites_an_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen = {"phase": "awaiting-successor-read", "transition_id": "t"}
            rc._atomic_json(rc._transition_path("recovery", root), {**seen, "phase": "completed"})
            self.assertFalse(rc._write_transition_field_if_awaiting("recovery", seen, "ack_timeout_escalated_at", "x",
                                                                     state_root=root))
            self.assertNotIn("ack_timeout_escalated_at", rc.transition_state("recovery", state_root=root))


class TimeoutTests(_Seat):
    def test_timeout_escalates_once_across_fires(self) -> None:
        self._transition(harness="codex")
        late = PROMPTED + timedelta(seconds=rc.SUCCESSOR_ACK_TIMEOUT_SECONDS + 60)
        results = [self._observe(now=late + timedelta(minutes=2 * i)) for i in range(3)]
        self.assertEqual(results, ["escalated", "escalated-already", "escalated-already"])
        self.routed.assert_called_once()
        self.assertIn("has not acknowledged", self.routed.call_args.args[1])
        self.assertIn("ack_timeout_escalated_at", self._state())
        self.assertEqual(trc._evidence_events(self.state).count("successor_ack_timeout_escalated"), 1)

    def test_before_the_timeout_nothing_escalates(self) -> None:
        self._transition(harness="codex")
        self.assertEqual(self._observe(now=PROMPTED + timedelta(minutes=10)), "waiting")
        self.routed.assert_not_called()

    def test_a_legacy_transition_times_out_from_the_file_mtime(self) -> None:
        path = self._transition(harness="codex", prompted=None)
        old = (PROMPTED - timedelta(hours=5)).timestamp()
        os.utime(path, (old, old))
        self.assertEqual(self._observe(now=PROMPTED), "escalated")
        self.routed.assert_called_once()

    def test_an_unknown_harness_only_gets_the_timeout(self) -> None:
        self._transition(harness="mystery")
        self.assertEqual(self._observe(), "waiting")
        late = PROMPTED + timedelta(seconds=rc.SUCCESSOR_ACK_TIMEOUT_SECONDS + 1)
        self.assertEqual(self._observe(now=late), "escalated")


class PromptRecordsTests(unittest.TestCase):
    def test_prompt_successor_records_when_and_what_and_still_types_the_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = trc._binding(root, harness="codex", profile="fallback")
            rc._atomic_json(rc._transition_path("recovery", root),
                            {"schema": "pantheon-runtime-transition/v1", "transition_id": "t-9",
                             "phase": "awaiting-successor-read", "expected_successor_read_evidence": "hook:x"})
            handle = rc.RuntimeHandle(binding.launch, binding.controller.tmux, "s", rc._utc_now())
            evidence = root / "evidence.md"
            evidence.write_text("# handoff\n", encoding="utf-8")
            with mock.patch.object(rc, "submit_to_runtime") as submit:
                rc._prompt_successor(handle, evidence, persona="recovery", state_root=root)
            state = rc.transition_state("recovery", state_root=root)
            copy_exists = Path(state["successor_evidence_path"]).is_file()
        submit.assert_called_once()
        self.assertIn("ack-read", submit.call_args.args[1].prompt)
        self.assertNotEqual(state["successor_evidence_path"], str(evidence))
        self.assertTrue(copy_exists)
        self.assertTrue(rc._valid_successor_read_nonce(state["successor_read_nonce"]))
        self.assertIsNotNone(rc._parse_iso_or_none(state["successor_prompted_at"]))


class CallSiteTests(unittest.TestCase):
    def test_the_wake_path_observes_before_the_viability_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = trc._binding(root, harness="claude", profile="primary")
            rc._atomic_json(rc._transition_path("recovery", root),
                            {"phase": "awaiting-successor-read", "incoming_harness": "claude",
                             "incoming_profile": "primary", "transition_id": "t"})
            blocked = rc.RuntimeViabilityBlocked("runtime-state-missing-or-mismatched", {})
            with mock.patch.object(rc, "observe_successor_read", return_value="waiting") as observe, \
                    mock.patch.object(rc, "ensure_viable_runtime", side_effect=blocked):
                with self.assertRaises(rc.RuntimeViabilityBlocked):
                    persona_bringup.wake_persistent_runtime(
                        binding, adapter=trc._Adapter(binding), state_dir=root, cwd=root
                    )
        observe.assert_called_once()
        self.assertIs(observe.call_args.kwargs["binding"], binding)

    def test_the_wake_path_does_not_observe_without_a_pending_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = trc._binding(root, harness="claude", profile="primary")
            with mock.patch.object(rc, "observe_successor_read") as observe, \
                    mock.patch.object(rc, "ensure_viable_runtime", side_effect=rc.RuntimeViabilityExhausted("x")):
                with self.assertRaises(rc.RuntimeViabilityExhausted):
                    persona_bringup.wake_persistent_runtime(
                        binding, adapter=trc._Adapter(binding), state_dir=root, cwd=root
                    )
        observe.assert_not_called()

    def test_the_quota_check_transition_branch_observes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binding = trc._binding(root, harness="claude", profile="primary")
            rc._atomic_json(rc._transition_path("recovery", root),
                            {"phase": "awaiting-successor-read", "incoming_harness": "claude",
                             "incoming_profile": "primary", "transition_id": "t"})
            with mock.patch.object(persona_bringup, "quota_check_supported", return_value=(True, "")), \
                    mock.patch.object(rc, "observe_successor_read", return_value="waiting") as observe:
                result = persona_bringup.quota_check(
                    binding, adapter=trc._Adapter(binding), primary_binding=binding, state_dir=root, cwd=root
                )
        observe.assert_called_once()
        self.assertEqual(result.decision, "transition-active-noop")

    def test_the_supervise_loop_never_observes(self) -> None:
        source = inspect.getsource(rc.block_until_tmux_death)
        self.assertNotIn("observe_successor_read", source)
        self.assertNotIn("observe_successor_read", inspect.getsource(rc._fallback_return_eligible))


if __name__ == "__main__":
    unittest.main()
