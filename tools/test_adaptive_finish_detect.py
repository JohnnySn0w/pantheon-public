"""Unit tests for tools/adaptive_finish_detect.py — T-0220 Adaptive-Finish Stop hook.

Run from repo root:
    python3 -m unittest tools.test_adaptive_finish_detect -v

Covers:
  - narrate-then-exit fixture (regex match, no tool_use) → hit logged
  - narrate-with-tool-use fixture (regex match, tool_use present) → no hit
  - negative-control (no match phrase) → no hit
  - HO exclusion (agent_type = orchestrator) → no hit
  - missing agent_type → no hit (fail-safe)
  - empty transcript → no crash, no hit
  - malformed JSONL → no crash, no hit
  - missing transcript path → no crash, no hit
  - multiple assistant turns → only last turn scanned
  - hit record fields validated (ts, transcript_path, agent_type, matched_phrase, message_excerpt, false_positive_marked)
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.adaptive_finish_detect as afd
from tools.adaptive_finish_detect import (
    NARRATE_RE,
    _DEFAULT_LOG_PATH,
    detect_narrate_no_tool,
    load_event,
    process_event,
    read_last_assistant_turn,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jsonl(turns: list[dict]) -> bytes:
    """Build a JSONL transcript from a list of turn dicts.

    Each dict may have:
      type: "assistant" | "user"
      text: str  (content text block)
      tool_use: bool  (if True, appends a tool_use block)
    """
    lines = []
    for t in turns:
        if t.get("type") == "user":
            obj = {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": t.get("text", "user message")}],
                },
            }
        else:  # assistant
            content: list[dict] = []
            if "text" in t:
                content.append({"type": "text", "text": t["text"]})
            if t.get("tool_use"):
                content.append(
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "Write",
                        "input": {"file_path": "/tmp/x", "content": "hello"},
                    }
                )
            obj = {
                "type": "assistant",
                "message": {"role": "assistant", "content": content},
            }
        lines.append(json.dumps(obj))
    return "\n".join(lines).encode()


def _event(transcript_path: str, agent_type: str = "implementer") -> dict:
    return {
        "session_id": "test-session-123",
        "agent_type": agent_type,
        "agent_transcript_path": transcript_path,
        "description": "test subagent",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNarrateRegex(unittest.TestCase):
    def test_matches_i_will_now(self) -> None:
        self.assertTrue(NARRATE_RE.search("I will now write the output."))

    def test_matches_i_will(self) -> None:
        self.assertTrue(NARRATE_RE.search("I will send the message."))

    def test_matches_let_me(self) -> None:
        self.assertTrue(NARRATE_RE.search("Let me compile the report."))

    def test_matches_i_will_go_ahead(self) -> None:
        self.assertTrue(NARRATE_RE.search("I will go ahead and write the file."))

    def test_matches_next_i_will(self) -> None:
        self.assertTrue(NARRATE_RE.search("Next, I will paste the smoke output."))

    def test_matches_finally_i_will(self) -> None:
        self.assertTrue(NARRATE_RE.search("Finally, I will commit the changes."))

    def test_no_match_approve_message(self) -> None:
        self.assertIsNone(NARRATE_RE.search("APPROVE. All gates pass. Smoke green."))

    def test_no_match_ready_message(self) -> None:
        self.assertIsNone(
            NARRATE_RE.search("READY. Branch feat/T-0220 pushed. SHA: abc1234.")
        )

    def test_case_insensitive(self) -> None:
        self.assertTrue(NARRATE_RE.search("let me verify this"))


class TestReadLastAssistantTurn(unittest.TestCase):
    def test_returns_last_assistant_text_and_tool_use_flag(self) -> None:
        turns = [
            {"type": "user", "text": "Do the task."},
            {"type": "assistant", "text": "I will read the file first.", "tool_use": True},
            {"type": "user", "text": "ok"},
            {"type": "assistant", "text": "Done. READY.", "tool_use": False},
        ]
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            f.write(_make_jsonl(turns))
            path = f.name
        try:
            text, has_tool = read_last_assistant_turn(path)
            self.assertIsNotNone(text)
            self.assertIn("READY", text)
            self.assertFalse(has_tool)
        finally:
            os.unlink(path)

    def test_detects_tool_use_in_last_turn(self) -> None:
        turns = [
            {"type": "assistant", "text": "I will write the file.", "tool_use": True},
        ]
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            f.write(_make_jsonl(turns))
            path = f.name
        try:
            text, has_tool = read_last_assistant_turn(path)
            self.assertTrue(has_tool)
        finally:
            os.unlink(path)

    def test_empty_file_returns_none(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            f.write(b"")
            path = f.name
        try:
            result = read_last_assistant_turn(path)
            self.assertIsNone(result)
        finally:
            os.unlink(path)

    def test_malformed_jsonl_returns_none(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            f.write(b"not json at all\n{broken\n")
            path = f.name
        try:
            result = read_last_assistant_turn(path)
            self.assertIsNone(result)
        finally:
            os.unlink(path)

    def test_missing_file_returns_none(self) -> None:
        result = read_last_assistant_turn("/nonexistent/path/agent.jsonl")
        self.assertIsNone(result)

    def test_uses_last_turn_not_first(self) -> None:
        """Only the last assistant turn should be checked."""
        turns = [
            {"type": "assistant", "text": "I will now read the file.", "tool_use": False},
            {"type": "user", "text": "continue"},
            {"type": "assistant", "text": "APPROVE. All done.", "tool_use": False},
        ]
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            f.write(_make_jsonl(turns))
            path = f.name
        try:
            text, _ = read_last_assistant_turn(path)
            self.assertIn("APPROVE", text)
            self.assertNotIn("I will now", text)
        finally:
            os.unlink(path)


class TestDetectNarrateNoTool(unittest.TestCase):
    def test_match_no_tool_use_returns_match(self) -> None:
        m = detect_narrate_no_tool("I will now write the smoke output.", has_tool_use=False)
        self.assertIsNotNone(m)

    def test_match_with_tool_use_returns_none(self) -> None:
        m = detect_narrate_no_tool("I will now write the smoke output.", has_tool_use=True)
        self.assertIsNone(m)

    def test_no_match_returns_none(self) -> None:
        m = detect_narrate_no_tool("READY. Smoke green.", has_tool_use=False)
        self.assertIsNone(m)

    # --- T-1128: trailing-output gate (narrate-then-EXIT vs narrate-then-WORK) ---

    def test_narrate_at_tail_fires(self) -> None:
        """Phrase is the tail with a short clause after it → genuine exit, fires."""
        m = detect_narrate_no_tool("Smoke is green. Let me wrap up.", has_tool_use=False)
        self.assertIsNotNone(m)

    def test_narrate_then_inline_work_suppressed(self) -> None:
        """Phrase followed by a paragraph of inline output → false positive, suppressed.

        This is the dominant T-1128 FP: a subagent says "Let me compile the answers:"
        then actually writes the answers in the same turn (no tool_use).
        """
        text = (
            "Let me compile the answers:\n\n"
            "1. The auth flow uses OAuth with a refresh token stored in the keyring.\n"
            "2. The retry budget is three attempts with exponential backoff.\n"
            "3. The digest fires weekly via the cron timer."
        )
        m = detect_narrate_no_tool(text, has_tool_use=False)
        self.assertIsNone(m)

    def test_last_match_used_not_first(self) -> None:
        """An early phrase followed by work does NOT fire on a later tail phrase's behalf;
        the LAST phrase governs. Here the last phrase is at the tail → fires."""
        text = "Let me check the config. " + "Verified all keys present. " * 2 + "I will now finalize."
        m = detect_narrate_no_tool(text, has_tool_use=False)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(0), "I will now")

    def test_early_phrase_then_long_work_suppressed(self) -> None:
        """Only phrase is early, followed by substantial inline work → suppressed."""
        text = "Let me walk through it. " + "The function validates input and returns a struct. " * 3
        m = detect_narrate_no_tool(text, has_tool_use=False)
        self.assertIsNone(m)

    # --- T-1449: composed-artifact gate (plan/report/assessment FP class) ---
    # Fixtures below are derived from real false-positive records in
    # ~/.local/state/pantheon/adaptive_finish_log.jsonl: implementers composing a
    # PLAN, an Explore compiling a STRUCTURED MAP, an orchestrator relaying a plan,
    # and a reviewer composing a final assessment. Each matched a narrate phrase
    # whose LAST occurrence fell inside the composed artifact with little trailing
    # text, slipping past the T-1128 trailing gate. The artifact is the discriminator.

    def test_plan_composition_suppressed(self) -> None:
        """Implementer narrates then composes a PLAN inline (heading + ---) → suppressed."""
        text = (
            "Good, vendor is intact. Now I have everything for the plan. Let me compose it:\n\n"
            "---\n\n"
            "## PLAN — Echo 4d.1 AEC Core\n\n"
            "### Arc-intent\n"
            "Deliver the shared AEC crate.\n\n"
            "### Steps\n"
            "1. Stub the trait surface.\n"
            "2. Wire the backend.\n"
            "Then I will run the smoke loop."
        )
        m = detect_narrate_no_tool(text, has_tool_use=False)
        self.assertIsNone(m)

    def test_structured_map_compilation_suppressed(self) -> None:
        """Explore narrates then compiles a STRUCTURED MAP inline → suppressed."""
        text = (
            "Perfect! Now I have all the information I need. Let me compile the structured report:\n\n"
            "---\n\n"
            "## STRUCTURED MAP: ADR-223 Current Shapes\n\n"
            "### 1. ToolDescriptor + ToolExecutor trait\n"
            "The trait lives in core. I will note the integration points below."
        )
        m = detect_narrate_no_tool(text, has_tool_use=False)
        self.assertIsNone(m)

    def test_reviewer_assessment_with_heading_suppressed(self) -> None:
        """Reviewer narrates a final assessment that it then composes inline → suppressed."""
        text = (
            "No D1/P5/D4 mis-cites in the new code.\n\n"
            "Now let me do a final assessment of the critical bug severity.\n\n"
            "## Assessment\n"
            "The double-qualification path is reachable only under the legacy flag."
        )
        m = detect_narrate_no_tool(text, has_tool_use=False)
        self.assertIsNone(m)

    def test_genuine_exit_with_no_artifact_still_fires(self) -> None:
        """Regression: a real narrate-then-exit (no composed artifact) still fires."""
        text = "Smoke is green and the branch is pushed. I will now hand off."
        m = detect_narrate_no_tool(text, has_tool_use=False)
        self.assertIsNotNone(m)


class TestComposedArtifactDetector(unittest.TestCase):
    def test_heading_detected(self) -> None:
        self.assertTrue(afd._contains_composed_artifact("intro\n\n## PLAN\nbody"))

    def test_hrule_detected(self) -> None:
        self.assertTrue(afd._contains_composed_artifact("intro\n\n---\n\nbody"))

    def test_indented_heading_detected(self) -> None:
        self.assertTrue(afd._contains_composed_artifact("  ## Section\nbody"))

    def test_plain_prose_not_detected(self) -> None:
        self.assertFalse(
            afd._contains_composed_artifact("Smoke is green. Let me wrap up now.")
        )

    def test_hashtag_midline_not_detected(self) -> None:
        """A '#' mid-sentence (e.g. a ticket ref 'PR #37') is not a heading."""
        self.assertFalse(
            afd._contains_composed_artifact("Merged PR #37 to main. I will finalize.")
        )

    def test_numbered_list_alone_not_detected(self) -> None:
        """A bare numbered list (no heading/hrule) is not a composed-artifact marker;
        the T-1128 trailing gate handles that shape instead."""
        self.assertFalse(
            afd._contains_composed_artifact("Findings:\n1. a\n2. b\n3. c")
        )


class TestProcessEvent(unittest.TestCase):
    def test_narrate_then_exit_logs_hit(self) -> None:
        """Fixture: narrate phrase + no tool_use → hit logged."""
        turns = [{"type": "assistant", "text": "I will now write the output.", "tool_use": False}]
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = os.path.join(tmpdir, "agent.jsonl")
            Path(transcript).write_bytes(_make_jsonl(turns))
            log_path = os.path.join(tmpdir, "adaptive_finish_log.jsonl")
            event = _event(transcript, agent_type="implementer")
            process_event(event, log_path=log_path)
            self.assertTrue(os.path.exists(log_path))
            record = json.loads(Path(log_path).read_text().strip())
            self.assertIn("ts", record)
            self.assertIn("transcript_path", record)
            self.assertIn("agent_type", record)
            self.assertIn("matched_phrase", record)
            self.assertIn("message_excerpt", record)
            self.assertIsNone(record["false_positive_marked"])
            self.assertEqual(record["agent_type"], "implementer")

    def test_narrate_with_tool_use_no_hit(self) -> None:
        """Fixture: narrate phrase + tool_use present → no hit."""
        turns = [{"type": "assistant", "text": "I will now write the file.", "tool_use": True}]
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = os.path.join(tmpdir, "agent.jsonl")
            Path(transcript).write_bytes(_make_jsonl(turns))
            log_path = os.path.join(tmpdir, "adaptive_finish_log.jsonl")
            event = _event(transcript, agent_type="implementer")
            process_event(event, log_path=log_path)
            self.assertFalse(os.path.exists(log_path))

    def test_negative_control_no_hit(self) -> None:
        """Fixture: no narrate phrase, no tool_use → no hit."""
        turns = [{"type": "assistant", "text": "APPROVE. All gates pass.", "tool_use": False}]
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = os.path.join(tmpdir, "agent.jsonl")
            Path(transcript).write_bytes(_make_jsonl(turns))
            log_path = os.path.join(tmpdir, "adaptive_finish_log.jsonl")
            event = _event(transcript, agent_type="reviewer")
            process_event(event, log_path=log_path)
            self.assertFalse(os.path.exists(log_path))

    def test_ho_excluded(self) -> None:
        """Fixture: narrate phrase + no tool_use + agent_type=orchestrator → skip."""
        turns = [{"type": "assistant", "text": "I will now write the analysis.", "tool_use": False}]
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = os.path.join(tmpdir, "agent.jsonl")
            Path(transcript).write_bytes(_make_jsonl(turns))
            log_path = os.path.join(tmpdir, "adaptive_finish_log.jsonl")
            event = _event(transcript, agent_type="orchestrator")
            process_event(event, log_path=log_path)
            self.assertFalse(os.path.exists(log_path))

    def test_missing_agent_type_no_hit(self) -> None:
        """Event with no agent_type field → fail-safe skip, no hit."""
        turns = [{"type": "assistant", "text": "I will now write the report.", "tool_use": False}]
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = os.path.join(tmpdir, "agent.jsonl")
            Path(transcript).write_bytes(_make_jsonl(turns))
            log_path = os.path.join(tmpdir, "adaptive_finish_log.jsonl")
            event = {"session_id": "x", "agent_transcript_path": transcript}  # no agent_type
            process_event(event, log_path=log_path)
            self.assertFalse(os.path.exists(log_path))

    def test_missing_transcript_no_crash(self) -> None:
        """agent_transcript_path points to nonexistent file → no crash, no hit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "adaptive_finish_log.jsonl")
            event = _event("/nonexistent/path/agent.jsonl", agent_type="implementer")
            # Should not raise
            process_event(event, log_path=log_path)
            self.assertFalse(os.path.exists(log_path))

    def test_empty_transcript_no_crash(self) -> None:
        """Empty JSONL file → no crash, no hit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = os.path.join(tmpdir, "agent.jsonl")
            Path(transcript).write_bytes(b"")
            log_path = os.path.join(tmpdir, "adaptive_finish_log.jsonl")
            event = _event(transcript, agent_type="implementer")
            process_event(event, log_path=log_path)
            self.assertFalse(os.path.exists(log_path))

    def test_malformed_jsonl_no_crash(self) -> None:
        """Corrupt JSONL lines → no crash, no hit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = os.path.join(tmpdir, "agent.jsonl")
            Path(transcript).write_bytes(b"not json\n{broken\n")
            log_path = os.path.join(tmpdir, "adaptive_finish_log.jsonl")
            event = _event(transcript, agent_type="implementer")
            process_event(event, log_path=log_path)
            self.assertFalse(os.path.exists(log_path))

    def test_message_excerpt_capped_at_200(self) -> None:
        """message_excerpt in log record is at most 200 chars."""
        # Phrase at the TAIL so it still registers as narrate-then-exit (T-1128),
        # with long leading content to exercise the 200-char excerpt cap.
        long_text = "x" * 500 + " Let me wrap up."
        turns = [{"type": "assistant", "text": long_text, "tool_use": False}]
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = os.path.join(tmpdir, "agent.jsonl")
            Path(transcript).write_bytes(_make_jsonl(turns))
            log_path = os.path.join(tmpdir, "adaptive_finish_log.jsonl")
            event = _event(transcript, agent_type="implementer")
            process_event(event, log_path=log_path)
            record = json.loads(Path(log_path).read_text().strip())
            self.assertLessEqual(len(record["message_excerpt"]), 200)

    def test_append_multiple_hits(self) -> None:
        """Multiple process_event calls append separate records."""
        turns = [{"type": "assistant", "text": "I will now do it.", "tool_use": False}]
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = os.path.join(tmpdir, "agent.jsonl")
            Path(transcript).write_bytes(_make_jsonl(turns))
            log_path = os.path.join(tmpdir, "adaptive_finish_log.jsonl")
            event = _event(transcript, agent_type="implementer")
            process_event(event, log_path=log_path)
            process_event(event, log_path=log_path)
            lines = [l for l in Path(log_path).read_text().splitlines() if l.strip()]
            self.assertEqual(len(lines), 2)


class TestLoadEvent(unittest.TestCase):
    def test_valid_json_on_stdin(self) -> None:
        payload = json.dumps({"session_id": "x", "agent_type": "implementer"})
        event = load_event(io.StringIO(payload))
        self.assertEqual(event["agent_type"], "implementer")

    def test_empty_stdin_returns_none(self) -> None:
        event = load_event(io.StringIO(""))
        self.assertIsNone(event)

    def test_invalid_json_returns_none(self) -> None:
        event = load_event(io.StringIO("not json"))
        self.assertIsNone(event)

    def test_non_dict_returns_none(self) -> None:
        event = load_event(io.StringIO(json.dumps([1, 2, 3])))
        self.assertIsNone(event)


class TestMainIntegration(unittest.TestCase):
    def test_main_exits_0_on_hit(self) -> None:
        """main() always exits 0 (hook must not block Claude Code)."""
        turns = [{"type": "assistant", "text": "I will now write the output.", "tool_use": False}]
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = os.path.join(tmpdir, "agent.jsonl")
            Path(transcript).write_bytes(_make_jsonl(turns))
            log_path = os.path.join(tmpdir, "adaptive_finish_log.jsonl")
            payload = json.dumps(_event(transcript, agent_type="implementer"))
            with patch("sys.stdin", io.StringIO(payload)):
                rc = afd.main(log_path=log_path)
            self.assertEqual(rc, 0)

    def test_main_exits_0_on_no_hit(self) -> None:
        turns = [{"type": "assistant", "text": "READY. Smoke green.", "tool_use": False}]
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = os.path.join(tmpdir, "agent.jsonl")
            Path(transcript).write_bytes(_make_jsonl(turns))
            log_path = os.path.join(tmpdir, "adaptive_finish_log.jsonl")
            payload = json.dumps(_event(transcript, agent_type="implementer"))
            with patch("sys.stdin", io.StringIO(payload)):
                rc = afd.main(log_path=log_path)
            self.assertEqual(rc, 0)

    def test_main_exits_0_on_bad_stdin(self) -> None:
        """Malformed event JSON → exit 0, no crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "adaptive_finish_log.jsonl")
            with patch("sys.stdin", io.StringIO("not json")):
                rc = afd.main(log_path=log_path)
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
