#!/usr/bin/env python3
"""Contract tests for tools/hooks/telegram-reply-gate.py (T-2188).

Runs the hook as a SUBPROCESS with real stdin/stdout, because the thing that can
silently break this gate is the harness contract (block shape, exit code), not the
matching logic. A test that imported the module and called helpers would pass while
the deployed hook enforced nothing.

Run: python3 -m unittest tools.test_telegram_reply_gate -v
"""
import json, os, subprocess, sys, tempfile, unittest, shutil, glob

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "telegram-reply-gate.py")
MARKER_DIR = os.path.expanduser("~/.claude/hooks/.telegram_reply_markers")

TG_TEXT = '<channel source="plugin:telegram:telegram" chat_id="456474197" message_id="99">hello</channel>'


def rec_user(text, tool_result=False):
    if tool_result:
        content = [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "ok"}]
    else:
        content = [{"type": "text", "text": text}]
    return {"type": "user", "message": {"role": "user", "content": content}}


def rec_assistant_reply(name="mcp__plugin_telegram_telegram__reply"):
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": {}}]}}


def rec_assistant_text(text="just thinking"):
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": text}]}}


def write_transcript(records, path):
    with open(path, "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def run_hook(payload, clear_markers=True):
    """Invoke the hook exactly as the harness does. Returns (rc, parsed_stdout_or_None)."""
    if clear_markers:
        shutil.rmtree(MARKER_DIR, ignore_errors=True)
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=30)
    out = p.stdout.strip()
    parsed = None
    if out:
        try:
            parsed = json.loads(out)
        except Exception:
            parsed = {"__unparseable__": out}
    return p.returncode, parsed


class GateBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tgate-")
        self.t = os.path.join(self.dir, "session.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(MARKER_DIR, ignore_errors=True)

    def payload(self, **kw):
        d = {"session_id": "sess-1", "transcript_path": self.t,
             "cwd": self.dir, "hook_event_name": "Stop"}
        d.update(kw)
        return d


class TestBlocking(GateBase):
    def test_telegram_inbound_without_reply_blocks(self):
        write_transcript([rec_user(TG_TEXT), rec_assistant_text()], self.t)
        rc, out = run_hook(self.payload())
        self.assertEqual(rc, 0, "hook must exit 0 even when blocking")
        self.assertIsNotNone(out, "expected a JSON block decision on stdout")
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("reason", out)
        self.assertTrue(out["reason"].strip(), "block reason must be non-empty")

    def test_block_shape_is_top_level_not_presubmit_style(self):
        """Pins the Stop contract. hookSpecificOutput/permissionDecision fails OPEN."""
        write_transcript([rec_user(TG_TEXT), rec_assistant_text()], self.t)
        _, out = run_hook(self.payload())
        self.assertNotIn("hookSpecificOutput", out)
        self.assertNotIn("permissionDecision", out)
        self.assertEqual(set(out.keys()), {"decision", "reason"})


class TestAllowing(GateBase):
    def test_reply_present_allows(self):
        write_transcript([rec_user(TG_TEXT), rec_assistant_reply()], self.t)
        rc, out = run_hook(self.payload())
        self.assertEqual(rc, 0)
        self.assertIsNone(out, "a sent reply must produce NO stdout (allow)")

    def test_non_telegram_turn_allows(self):
        write_transcript([rec_user("ordinary CLI prompt"), rec_assistant_text()], self.t)
        _, out = run_hook(self.payload())
        self.assertIsNone(out)

    def test_mailbox_style_turn_allows(self):
        write_transcript([rec_user("You have 2 unread inter-agent messages"),
                          rec_assistant_text()], self.t)
        _, out = run_hook(self.payload())
        self.assertIsNone(out)

    def test_reply_tool_matched_by_suffix_across_plugin_ids(self):
        for name in ("mcp__plugin_telegram_telegram__reply",
                     "mcp__telegram__reply",
                     "MCP__PLUGIN_TELEGRAM_TELEGRAM__REPLY"):
            with self.subTest(name=name):
                write_transcript([rec_user(TG_TEXT), rec_assistant_reply(name)], self.t)
                _, out = run_hook(self.payload())
                self.assertIsNone(out, f"{name} should count as a reply")

    def test_non_reply_telegram_tool_does_not_satisfy_gate(self):
        """react/edit_message are not answers — they must NOT count as replying."""
        write_transcript([rec_user(TG_TEXT),
                          rec_assistant_reply("mcp__plugin_telegram_telegram__react")], self.t)
        _, out = run_hook(self.payload())
        self.assertEqual(out.get("decision"), "block")


class TestToolResultTrap(GateBase):
    def test_tool_result_record_is_not_the_latest_user_turn(self):
        """tool_result records carry role=user; treating one as the user turn would
        compare from the wrong point and let a dropped reply through."""
        write_transcript([
            rec_user(TG_TEXT),
            rec_assistant_text(),
            rec_user("", tool_result=True),      # role=user but NOT a human turn
        ], self.t)
        _, out = run_hook(self.payload())
        self.assertEqual(out.get("decision"), "block",
                         "tool_result must not mask the telegram turn")

    def test_reply_before_a_later_real_user_turn_does_not_carry_over(self):
        write_transcript([
            rec_user(TG_TEXT), rec_assistant_reply(),
            rec_user(TG_TEXT), rec_assistant_text(),   # second tg turn, no reply
        ], self.t)
        _, out = run_hook(self.payload())
        self.assertEqual(out.get("decision"), "block")


class TestBounded(GateBase):
    def test_blocks_once_then_allows(self):
        write_transcript([rec_user(TG_TEXT), rec_assistant_text()], self.t)
        _, first = run_hook(self.payload())
        self.assertEqual(first.get("decision"), "block")
        _, second = run_hook(self.payload(), clear_markers=False)
        self.assertIsNone(second, "second identical stop must pass (no wedge)")


class TestFailOpen(GateBase):
    def test_empty_stdin_allows(self):
        rc, out = run_hook({})
        self.assertEqual(rc, 0)
        self.assertIsNone(out)

    def test_missing_transcript_allows(self):
        _, out = run_hook(self.payload(transcript_path="/nonexistent/x.jsonl"))
        self.assertIsNone(out)

    def test_malformed_jsonl_allows_rather_than_crashing(self):
        with open(self.t, "w") as fh:
            fh.write("{not json\n" + json.dumps(rec_user(TG_TEXT)) + "\n")
        rc, out = run_hook(self.payload())
        self.assertEqual(rc, 0)
        self.assertEqual(out.get("decision"), "block")  # good record still parsed

    def test_totally_unparseable_transcript_allows(self):
        with open(self.t, "w") as fh:
            fh.write("garbage\nmore garbage\n")
        rc, out = run_hook(self.payload())
        self.assertEqual(rc, 0)
        self.assertIsNone(out)

    def test_garbage_stdin_allows(self):
        p = subprocess.run([sys.executable, HOOK], input="<<<not json>>>",
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")


class TestTailWindow(GateBase):
    def test_large_transcript_reads_only_tail_and_still_blocks(self):
        pad = [rec_assistant_text("x" * 4000) for _ in range(200)]   # ~800KB > TAIL_BYTES
        write_transcript(pad + [rec_user(TG_TEXT), rec_assistant_text()], self.t)
        self.assertGreater(os.path.getsize(self.t), 512 * 1024)
        _, out = run_hook(self.payload())
        self.assertEqual(out.get("decision"), "block")

    def test_user_turn_outside_tail_window_fails_open(self):
        pad = [rec_assistant_text("y" * 4000) for _ in range(200)]
        write_transcript([rec_user(TG_TEXT)] + pad, self.t)
        _, out = run_hook(self.payload())
        self.assertIsNone(out, "unreachable history must fail OPEN, not block")


class TestSessionIdFallback(GateBase):
    def test_reconstructs_path_from_session_id_when_transcript_path_absent(self):
        home = os.path.expanduser("~")
        cwd = os.path.join(self.dir, "proj")
        os.makedirs(cwd, exist_ok=True)
        mangled = cwd.replace("/", "-")
        pdir = os.path.join(home, ".claude", "projects", mangled)
        os.makedirs(pdir, exist_ok=True)
        tp = os.path.join(pdir, "sess-fallback.jsonl")
        try:
            write_transcript([rec_user(TG_TEXT), rec_assistant_text()], tp)
            d = {"session_id": "sess-fallback", "cwd": cwd, "hook_event_name": "Stop"}
            _, out = run_hook(d)
            self.assertEqual(out.get("decision"), "block")
        finally:
            shutil.rmtree(pdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
