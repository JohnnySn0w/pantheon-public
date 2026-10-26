#!/usr/bin/env python3
"""Contract tests for tools/hooks/telegram-reply-gate.py (T-2188).

Runs the hook as a SUBPROCESS with real stdin/stdout, because the thing that can
silently break this gate is the harness contract (block shape, exit code), not the
matching logic. A test that imported the module and called helpers would pass while
the deployed hook enforced nothing.

HOME ISOLATION (T-4530): the hook resolves its marker dir and its session-id
transcript fallback via os.path.expanduser("~/.claude/..."), so HOME decides where it
reads and writes. Every hook subprocess here runs with HOME pointed at a per-test temp
dir. The real ~/.claude/hooks/.telegram_reply_markers is never touched: tests on
different xdist workers (or two CI runs on one runner user) used to race on that one
shared dir, and a local run wiped the live gate's real markers.

Run: python3 -m unittest tools.test_telegram_reply_gate -v
"""
import json, os, subprocess, sys, tempfile, unittest, shutil, uuid

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "telegram-reply-gate.py")
MARKER_SUBPATH = (".claude", "hooks", ".telegram_reply_markers")   # under the hook's HOME

TG_TEXT = '<channel source="plugin:telegram:telegram" chat_id="424242424" message_id="99">hello</channel>'


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


def marker_dir(home):
    """Where the hook (running with HOME=home) keeps its once-per-message markers."""
    return os.path.join(home, *MARKER_SUBPATH)


def hook_env(home):
    """Subprocess env for the hook: HOME is a per-test dir, never the real one.

    Refuses the real home outright so no future test can quietly reintroduce the
    shared-marker-dir race (T-4530) by passing it through.
    """
    if os.path.realpath(home) == os.path.realpath(os.path.expanduser("~")):
        raise ValueError("refusing to run the hook against the real HOME (T-4530): " + home)
    return {**os.environ, "HOME": home}


def run_hook(payload, home, clear_markers=True):
    """Invoke the hook exactly as the harness does, under HOME=`home`.

    `home` is the test's own temp home (see GateBase.home). Returns
    (rc, parsed_stdout_or_None).
    """
    env = hook_env(home)            # validate BEFORE any rmtree: a real HOME must never get that far
    if clear_markers:
        shutil.rmtree(marker_dir(home), ignore_errors=True)
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=30, env=env)
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
        self.home = os.path.join(self.dir, "home")   # per-test HOME for the hook subprocess
        os.makedirs(self.home)
        self.t = os.path.join(self.dir, "session.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)   # takes self.home + its markers with it

    def payload(self, **kw):
        d = {"session_id": "sess-1", "transcript_path": self.t,
             "cwd": self.dir, "hook_event_name": "Stop"}
        d.update(kw)
        return d


class TestBlocking(GateBase):
    def test_telegram_inbound_without_reply_blocks(self):
        write_transcript([rec_user(TG_TEXT), rec_assistant_text()], self.t)
        rc, out = run_hook(self.payload(), self.home)
        self.assertEqual(rc, 0, "hook must exit 0 even when blocking")
        self.assertIsNotNone(out, "expected a JSON block decision on stdout")
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("reason", out)
        self.assertTrue(out["reason"].strip(), "block reason must be non-empty")

    def test_block_shape_is_top_level_not_presubmit_style(self):
        """Pins the Stop contract. hookSpecificOutput/permissionDecision fails OPEN."""
        write_transcript([rec_user(TG_TEXT), rec_assistant_text()], self.t)
        _, out = run_hook(self.payload(), self.home)
        self.assertNotIn("hookSpecificOutput", out)
        self.assertNotIn("permissionDecision", out)
        self.assertEqual(set(out.keys()), {"decision", "reason"})


class TestAllowing(GateBase):
    def test_reply_present_allows(self):
        write_transcript([rec_user(TG_TEXT), rec_assistant_reply()], self.t)
        rc, out = run_hook(self.payload(), self.home)
        self.assertEqual(rc, 0)
        self.assertIsNone(out, "a sent reply must produce NO stdout (allow)")

    def test_non_telegram_turn_allows(self):
        write_transcript([rec_user("ordinary CLI prompt"), rec_assistant_text()], self.t)
        _, out = run_hook(self.payload(), self.home)
        self.assertIsNone(out)

    def test_mailbox_style_turn_allows(self):
        write_transcript([rec_user("You have 2 unread inter-agent messages"),
                          rec_assistant_text()], self.t)
        _, out = run_hook(self.payload(), self.home)
        self.assertIsNone(out)

    def test_reply_tool_matched_by_suffix_across_plugin_ids(self):
        for name in ("mcp__plugin_telegram_telegram__reply",
                     "mcp__telegram__reply",
                     "MCP__PLUGIN_TELEGRAM_TELEGRAM__REPLY"):
            with self.subTest(name=name):
                write_transcript([rec_user(TG_TEXT), rec_assistant_reply(name)], self.t)
                _, out = run_hook(self.payload(), self.home)
                self.assertIsNone(out, f"{name} should count as a reply")

    def test_non_reply_telegram_tool_does_not_satisfy_gate(self):
        """react/edit_message are not answers — they must NOT count as replying."""
        write_transcript([rec_user(TG_TEXT),
                          rec_assistant_reply("mcp__plugin_telegram_telegram__react")], self.t)
        _, out = run_hook(self.payload(), self.home)
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
        _, out = run_hook(self.payload(), self.home)
        self.assertEqual(out.get("decision"), "block",
                         "tool_result must not mask the telegram turn")

    def test_reply_before_a_later_real_user_turn_does_not_carry_over(self):
        write_transcript([
            rec_user(TG_TEXT), rec_assistant_reply(),
            rec_user(TG_TEXT), rec_assistant_text(),   # second tg turn, no reply
        ], self.t)
        _, out = run_hook(self.payload(), self.home)
        self.assertEqual(out.get("decision"), "block")


class TestBounded(GateBase):
    def test_blocks_once_then_allows(self):
        write_transcript([rec_user(TG_TEXT), rec_assistant_text()], self.t)
        _, first = run_hook(self.payload(), self.home)
        self.assertEqual(first.get("decision"), "block")
        _, second = run_hook(self.payload(), self.home, clear_markers=False)
        self.assertIsNone(second, "second identical stop must pass (no wedge)")


class TestFailOpen(GateBase):
    def test_empty_stdin_allows(self):
        rc, out = run_hook({}, self.home)
        self.assertEqual(rc, 0)
        self.assertIsNone(out)

    def test_missing_transcript_allows(self):
        _, out = run_hook(self.payload(transcript_path="/nonexistent/x.jsonl"), self.home)
        self.assertIsNone(out)

    def test_malformed_jsonl_allows_rather_than_crashing(self):
        with open(self.t, "w") as fh:
            fh.write("{not json\n" + json.dumps(rec_user(TG_TEXT)) + "\n")
        rc, out = run_hook(self.payload(), self.home)
        self.assertEqual(rc, 0)
        self.assertEqual(out.get("decision"), "block")  # good record still parsed

    def test_totally_unparseable_transcript_allows(self):
        with open(self.t, "w") as fh:
            fh.write("garbage\nmore garbage\n")
        rc, out = run_hook(self.payload(), self.home)
        self.assertEqual(rc, 0)
        self.assertIsNone(out)

    def test_garbage_stdin_allows(self):
        p = subprocess.run([sys.executable, HOOK], input="<<<not json>>>",
                           capture_output=True, text=True, timeout=30, env=hook_env(self.home))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")


class TestTailWindow(GateBase):
    def test_large_transcript_reads_only_tail_and_still_blocks(self):
        pad = [rec_assistant_text("x" * 4000) for _ in range(200)]   # ~800KB > TAIL_BYTES
        write_transcript(pad + [rec_user(TG_TEXT), rec_assistant_text()], self.t)
        self.assertGreater(os.path.getsize(self.t), 512 * 1024)
        _, out = run_hook(self.payload(), self.home)
        self.assertEqual(out.get("decision"), "block")

    def test_user_turn_outside_tail_window_fails_open(self):
        pad = [rec_assistant_text("y" * 4000) for _ in range(200)]
        write_transcript([rec_user(TG_TEXT)] + pad, self.t)
        _, out = run_hook(self.payload(), self.home)
        self.assertIsNone(out, "unreachable history must fail OPEN, not block")


class TestSessionIdFallback(GateBase):
    def test_reconstructs_path_from_session_id_when_transcript_path_absent(self):
        # The hook builds ~/.claude/projects/<mangled-cwd>/<sid>.jsonl from HOME, so the
        # seeded transcript lives under the per-test home (removed with self.dir).
        cwd = os.path.join(self.dir, "proj")
        os.makedirs(cwd, exist_ok=True)
        mangled = cwd.replace("/", "-")
        pdir = os.path.join(self.home, ".claude", "projects", mangled)
        os.makedirs(pdir, exist_ok=True)
        tp = os.path.join(pdir, "sess-fallback.jsonl")
        write_transcript([rec_user(TG_TEXT), rec_assistant_text()], tp)
        d = {"session_id": "sess-fallback", "cwd": cwd, "hook_event_name": "Stop"}
        _, out = run_hook(d, self.home)
        self.assertEqual(out.get("decision"), "block")


class TestHomeIsolation(GateBase):
    """T-4530 guards: the tests must never touch the real shared marker dir."""

    def test_marker_lands_in_per_test_home_not_the_real_one(self):
        # Unique session id => unique marker filename (sha1 of sid|text). A live gate on
        # this box cannot produce it, so "absent from the real dir" is deterministic even
        # while real hooks run (a before/after mtime diff of the real dir would not be).
        sid = "t4530-" + uuid.uuid4().hex
        write_transcript([rec_user(TG_TEXT), rec_assistant_text()], self.t)
        _, out = run_hook(self.payload(session_id=sid), self.home)
        self.assertEqual(out.get("decision"), "block")
        names = os.listdir(marker_dir(self.home))
        self.assertEqual(len(names), 1, "hook must write its marker under the per-test HOME")
        real_dir = marker_dir(os.path.expanduser("~"))
        real_names = os.listdir(real_dir) if os.path.isdir(real_dir) else []
        self.assertNotIn(names[0], real_names, "marker leaked into the real shared marker dir")

    def test_run_hook_refuses_the_real_home(self):
        with self.assertRaises(ValueError):
            run_hook(self.payload(), os.path.expanduser("~"))

    def test_concurrent_homes_do_not_share_markers(self):
        # Two tests' hooks, same session + message, different HOMEs: each gets its own
        # first block. Under the old shared dir the second would have found the first's
        # marker and been let through (the CI flake).
        other = tempfile.mkdtemp(prefix="tgate-other-")
        try:
            write_transcript([rec_user(TG_TEXT), rec_assistant_text()], self.t)
            _, a = run_hook(self.payload(), self.home)
            _, b = run_hook(self.payload(), other)
            self.assertEqual(a.get("decision"), "block")
            self.assertEqual(b.get("decision"), "block")
        finally:
            shutil.rmtree(other, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
