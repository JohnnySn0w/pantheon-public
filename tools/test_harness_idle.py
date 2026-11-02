"""Tests for tools/harness_idle.py (T-5793): harness-neutral idle from session logs.

Fixtures are content-stripped windows of real Claude transcripts and codex rollouts
(tools/fixtures/harness_idle/, provenance in its README). Every filesystem read goes
through FakeReader, keyed by synthetic paths; `now` is injected. Nothing here reads a
seat home or /tmp.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import harness_idle as hi  # noqa: E402
from tools import live_harness  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "harness_idle"
PANES = Path(__file__).resolve().parent / "fixtures" / "codex_panes"


def fx_text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def fx(name: str) -> list[dict]:
    return [json.loads(line) for line in fx_text(name).splitlines() if line.strip()]


def to_text(records) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def last_activity(records, claude=True) -> float:
    kinds = ("user", "assistant", "system")
    return max(epoch(r["timestamp"]) for r in records
               if "timestamp" in r and (not claude or r.get("type") in kinds))


class FakeReader:
    """In-memory stand-in with health_export.SudoReader's method shapes."""

    def __init__(self, files: dict[str, tuple[float, str]] | None = None):
        self.files = dict(files or {})
        self.calls: list[tuple] = []

    def newest_files(self, account, root, min_depth, max_depth, suffix, n):
        self.calls.append(("newest_files", account, root))
        prefix = root.rstrip("/") + "/"
        rows = []
        for path, (mtime, _text) in self.files.items():
            if not path.startswith(prefix) or not path.endswith(suffix):
                continue
            depth = path[len(prefix):].count("/") + 1
            if min_depth <= depth <= max_depth:
                rows.append((mtime, path))
        if not rows and not any(p.startswith(prefix) for p in self.files):
            return None
        return sorted(rows, reverse=True)[:n]

    def read_text_tail(self, account, path, max_bytes):
        self.calls.append(("tail", account, path, max_bytes))
        if path not in self.files:
            return None
        data = self.files[path][1].encode("utf-8")
        return data[-max_bytes:].decode("utf-8", errors="replace")

    def read_text_head(self, account, path, max_bytes):
        self.calls.append(("head", account, path))
        if path not in self.files:
            return None
        return self.files[path][1].encode("utf-8")[:max_bytes].decode("utf-8", errors="replace")

    def grep_tail(self, account, path, max_bytes, needles):
        self.calls.append(("grep", account, path, max_bytes))
        if path not in self.files:
            return None
        data = self.files[path][1].encode("utf-8")
        cut = data[-max_bytes:]
        lines = cut.split(b"\n")
        if len(data) > max_bytes:
            lines = lines[1:]
        return [ln.decode("utf-8") + "\n" for ln in lines if any(n in ln for n in needles)]


class StrippedGrepReader(FakeReader):
    """grep_tail returning lines with their trailing newlines stripped."""

    def grep_tail(self, account, path, max_bytes, needles):
        lines = super().grep_tail(account, path, max_bytes, needles)
        return None if lines is None else [ln.rstrip("\n") for ln in lines]


class NoGrepReader(FakeReader):
    """A reader without grep_tail (e.g. an older SudoReader): window-only tracking."""
    grep_tail = None


class RaisingReader:
    def __getattr__(self, name):
        def boom(*_a, **_k):
            raise RuntimeError("reader exploded")
        return boom


HOME = "/home/seat"
PERSONAS = f"{HOME}/.local/state/pantheon/personas"
EARLY = "2026-10-08T00:00:00Z"
PDIR = f"{HOME}/.claude/projects/-home-seat-pantheon-config"
SESS = f"{HOME}/.codex/sessions"
MAIN_ROLLOUT = f"{SESS}/2026/10/28/rollout-2026-10-28T01-58-11-01a1803a-ae9e-7a33-8988-89e3ce3d3e07.jsonl"
SUB_ROLLOUT = f"{SESS}/2026/10/28/rollout-2026-10-28T11-58-11-01a1803a-0000-7000-8000-00000000a9e7.jsonl"
EXEC_ROLLOUT = f"{SESS}/2026/10/28/rollout-2026-10-28T11-59-00-019ff7b2-212e-7210-95ca-bc782c2c5520.jsonl"


def runtime_json(harness: str, started_at: str | None) -> str:
    state = {"schema": "pantheon-runtime-state/v1", "harness": harness}
    if started_at is not None:
        state["started_at"] = started_at
    return json.dumps(state)


def read(reader, *, agent="seat", default="claude-code", now, **kw):
    return hi.read_idle(agent, account="seat", home=HOME, cwd=None, default_harness=default,
                        reader=reader, now=now, claude_projects_dir=PDIR, **kw)


def shift(records, seconds):
    """Records with every timestamp moved by `seconds` (test-built edge cases only)."""
    out = []
    for r in records:
        r = dict(r)
        if "timestamp" in r:
            dt = datetime.fromtimestamp(epoch(r["timestamp"]) + seconds, tz=timezone.utc)
            r["timestamp"] = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
        out.append(r)
    return out


# ---------------------------------------------------------------------------- claude
class ClassifyClaudeTest(unittest.TestCase):
    def setUp(self):
        self.idle = fx("claude/idle.jsonl")
        self.t_end = last_activity(self.idle)

    def test_idle_after_quiet_window(self):
        r = hi.classify_claude(self.idle, self.t_end + 700, quiet_s=600)
        self.assertIs(r.verdict, hi.Verdict.IDLE)
        self.assertEqual(r.last_boundary, "turn_duration")
        self.assertEqual(r.open_bg, 0)
        self.assertAlmostEqual(r.quiet_s, 700, places=3)

    def test_idle_but_recent_is_not_idle(self):
        r = hi.classify_claude(self.idle, self.t_end + 100, quiet_s=600)
        self.assertIs(r.verdict, hi.Verdict.WORKING)
        self.assertIn("ago", r.reason)

    def test_new_prompt_after_end_is_working(self):
        recs = fx("claude/working_prompt.jsonl")
        r = hi.classify_claude(recs, last_activity(recs) + 5000)
        self.assertIs(r.verdict, hi.Verdict.WORKING)
        self.assertEqual(r.last_boundary, "turn_start")

    def test_mid_tool_is_working_however_long(self):
        recs = fx("claude/mid_tool.jsonl")
        self.assertEqual(recs[-1]["message"]["stop_reason"], "tool_use")
        r = hi.classify_claude(recs, last_activity(recs) + 86400)
        self.assertIs(r.verdict, hi.Verdict.WORKING)

    def test_assistant_after_end_without_turn_origin_is_working(self):
        recs = self.idle + [{"type": "assistant", "timestamp": "2026-10-13T12:29:00.000Z",
                             "message": {"stop_reason": "tool_use"}}]
        r = hi.classify_claude(recs, self.t_end + 5000)
        self.assertIs(r.verdict, hi.Verdict.WORKING)
        self.assertIn("mid-tool", r.reason)

    def test_pending_background_agent_is_bg_wait(self):
        recs = fx("claude/bg_agent.jsonl")
        r = hi.classify_claude(recs, last_activity(recs) + 700, bg_max_s=3600)
        self.assertIs(r.verdict, hi.Verdict.BG_WAIT)
        self.assertEqual(r.open_bg, 1)

    def test_bg_wait_past_max_reads_idle(self):
        recs = fx("claude/bg_agent.jsonl")
        r = hi.classify_claude(recs, last_activity(recs) + 3601, bg_max_s=3600)
        self.assertIs(r.verdict, hi.Verdict.IDLE)
        self.assertEqual(r.open_bg, 1)

    def test_open_background_shell_is_bg_wait(self):
        recs = fx("claude/bg_shell_open.jsonl")
        r = hi.classify_claude(recs, last_activity(recs) + 700)
        self.assertIs(r.verdict, hi.Verdict.BG_WAIT)
        self.assertEqual(r.open_bg, 1)
        self.assertIn("1 shell", r.reason)

    def test_matching_task_notification_closes_the_shell(self):
        recs = fx("claude/bg_shell_closed.jsonl")
        r = hi.classify_claude(recs, last_activity(recs) + 700)
        self.assertIs(r.verdict, hi.Verdict.IDLE)
        self.assertEqual(r.open_bg, 0)

    def test_open_shell_past_max_reads_idle(self):
        recs = fx("claude/bg_shell_open.jsonl")
        r = hi.classify_claude(recs, last_activity(recs) + 4000, bg_max_s=3600)
        self.assertIs(r.verdict, hi.Verdict.IDLE)

    def test_interrupt_is_a_turn_end(self):
        recs = fx("claude/interrupt.jsonl")
        r = hi.classify_claude(recs, last_activity(recs) + 700)
        self.assertIs(r.verdict, hi.Verdict.IDLE)
        self.assertEqual(r.last_boundary, "interrupt")

    def test_no_turn_duration_is_unknown(self):
        r = hi.classify_claude(fx("claude/no_turn_duration.jsonl"), self.t_end + 700)
        self.assertIs(r.verdict, hi.Verdict.UNKNOWN)
        self.assertIn("turn_duration", r.reason)

    def test_trailing_untimestamped_metadata_does_not_reset_clock(self):
        recs = self.idle + [{"type": "ai-title"}, {"type": "last-prompt"}, {"type": "mode"}]
        r = hi.classify_claude(recs, self.t_end + 700)
        self.assertIs(r.verdict, hi.Verdict.IDLE)
        self.assertEqual(r.last_event_ts, self.t_end)

    def test_non_activity_timestamps_do_not_count(self):
        # api-request / queue-operation / attachment carry timestamps but are not activity.
        recs = self.idle + [{"type": "queue-operation", "timestamp": "2026-10-13T13:00:00.000Z"}]
        r = hi.classify_claude(recs, self.t_end + 700)
        self.assertEqual(r.last_event_ts, self.t_end)

    def test_mid_turn_attachment_notification_closes_the_shell(self):
        # MED-2: the completion of bzeog0dnb lands mid-turn as an attachment
        # {type: queued_command, prompt: "<task-notification>..."}, not a user entry.
        recs = fx("claude/bg_shell_attachment_closed.jsonl")
        r = hi.classify_claude(recs, last_activity(recs) + 700)
        self.assertIs(r.verdict, hi.Verdict.IDLE, r.reason)
        self.assertEqual(r.open_bg, 0)

    def test_attachment_case_is_bg_wait_before_its_notification(self):
        recs = fx("claude/bg_shell_attachment_closed.jsonl")
        first_end = next(i for i, x in enumerate(recs) if x.get("subtype") == "turn_duration")
        head = recs[:first_end + 1]
        r = hi.classify_claude(head, last_activity(head) + 700)
        self.assertIs(r.verdict, hi.Verdict.BG_WAIT)
        self.assertEqual(r.open_bg, 1)

    def test_bg_markers_reads_both_notification_shapes(self):
        started, notified = hi.bg_markers(fx("claude/bg_shell_attachment_closed.jsonl")
                                          + fx("claude/bg_shell_closed.jsonl"))
        self.assertIn("bzeog0dnb", started)
        self.assertIn("bbv2hefe2", started)
        self.assertTrue({"bzeog0dnb", "bbv2hefe2"} <= notified)

    def test_garbage_never_raises(self):
        r = hi.classify_claude([None, 5, "x"], 0)
        self.assertIs(r.verdict, hi.Verdict.UNKNOWN)
        self.assertIs(hi.classify_claude([], 0).verdict, hi.Verdict.UNKNOWN)


# ----------------------------------------------------------------------------- codex
class ClassifyCodexTest(unittest.TestCase):
    def test_idle_after_task_complete(self):
        recs = fx("codex/idle.jsonl")
        r = hi.classify_codex(recs, last_activity(recs, claude=False) + 700)
        self.assertIs(r.verdict, hi.Verdict.IDLE)
        self.assertEqual(r.last_boundary, "task_complete")

    def test_recent_task_complete_is_not_idle(self):
        recs = fx("codex/idle.jsonl")
        r = hi.classify_codex(recs, last_activity(recs, claude=False) + 60, quiet_s=600)
        self.assertIs(r.verdict, hi.Verdict.WORKING)

    def test_task_started_is_working(self):
        recs = fx("codex/working.jsonl")
        r = hi.classify_codex(recs, last_activity(recs, claude=False) + 86400)
        self.assertIs(r.verdict, hi.Verdict.WORKING)
        self.assertEqual(r.last_boundary, "task_started")

    def test_turn_aborted_is_a_turn_end(self):
        recs = fx("codex/turn_aborted.jsonl")
        r = hi.classify_codex(recs, last_activity(recs, claude=False) + 700)
        self.assertIs(r.verdict, hi.Verdict.IDLE)
        self.assertEqual(r.last_boundary, "turn_aborted")

    def test_no_boundary_is_unknown(self):
        recs = fx("codex/idle.jsonl")[:1]
        self.assertIs(hi.classify_codex(recs, 2e9).verdict, hi.Verdict.UNKNOWN)
        self.assertIs(hi.classify_codex([None], 2e9).verdict, hi.Verdict.UNKNOWN)


# ------------------------------------------------------------------ locate + read_idle
class ReadIdleCodexTest(unittest.TestCase):
    def setUp(self):
        self.main = fx_text("codex/idle.jsonl")
        self.t_main = last_activity(fx("codex/idle.jsonl"), claude=False)
        self.now = self.t_main + 700
        self.started = "2026-10-28T01:58:00Z"

    def reader(self, **extra):
        files = {
            f"{PERSONAS}/seat/runtime-controller/runtime.json": (0, runtime_json("codex", self.started)),
            MAIN_ROLLOUT: (self.t_main, self.main),
        }
        files.update(extra)
        return FakeReader(files)

    def test_codex_seat_idle(self):
        r = read(self.reader(), now=self.now)
        self.assertIs(r.verdict, hi.Verdict.IDLE, r.reason)
        self.assertEqual(r.harness, "codex")
        self.assertEqual(r.log_path, MAIN_ROLLOUT)
        self.assertEqual(r.session_id, "01a1803a-ae9e-7a33-8988-89e3ce3d3e07")

    def test_newer_subagent_rollout_is_skipped(self):
        sub = fx_text("codex/subagent_rollout.jsonl")
        reader = self.reader(**{SUB_ROLLOUT: (self.t_main + 36000, sub)})
        r = read(reader, now=self.t_main + 36000 + 700)
        self.assertEqual(r.log_path, MAIN_ROLLOUT)

    def test_subagent_skip_holds_while_main_is_working(self):
        # wait_agent: the main rollout's last boundary is task_started; the newer subagent
        # rollout ends in task_complete. Reading the subagent would say IDLE.
        working = fx_text("codex/working.jsonl")
        t_work = last_activity(fx("codex/working.jsonl"), claude=False)
        sub = fx_text("codex/subagent_rollout.jsonl")
        reader = self.reader(**{MAIN_ROLLOUT: (t_work, working),
                                SUB_ROLLOUT: (t_work + 36000, sub)})
        r = read(reader, now=t_work + 36000 + 700)
        self.assertIs(r.verdict, hi.Verdict.WORKING)
        self.assertEqual(r.log_path, MAIN_ROLLOUT)

    def test_exec_rollout_is_skipped(self):
        reader = self.reader(**{EXEC_ROLLOUT: (self.t_main + 50, fx_text("codex/exec_rollout.jsonl"))})
        r = read(reader, now=self.now)
        self.assertEqual(r.log_path, MAIN_ROLLOUT)

    def test_rollout_predating_runtime_start_is_unknown(self):
        self.started = "2026-10-29T00:00:00Z"
        r = read(self.reader(), now=self.now)
        self.assertIs(r.verdict, hi.Verdict.UNKNOWN)
        self.assertIn("stale", r.reason)

    def test_claude_log_while_harness_is_codex_is_unknown(self):
        idle = fx_text("claude/idle.jsonl")
        reader = FakeReader({
            f"{PERSONAS}/seat/runtime-controller/runtime.json": (0, runtime_json("codex", self.started)),
            f"{PDIR}/0a0a0a0a-0000-4000-8000-000000000000.jsonl": (self.now, idle),
        })
        r = read(reader, now=self.now)
        self.assertIs(r.verdict, hi.Verdict.UNKNOWN)
        self.assertEqual(r.harness, "codex")
        self.assertFalse(any(c[0] == "newest_files" and c[2] == PDIR for c in reader.calls))

    def test_no_rollouts_is_unknown(self):
        reader = FakeReader({f"{PERSONAS}/seat/runtime-controller/runtime.json":
                             (0, runtime_json("codex", self.started))})
        self.assertIs(read(reader, now=self.now).verdict, hi.Verdict.UNKNOWN)

    def test_empty_rollout_is_unknown(self):
        r = read(self.reader(**{MAIN_ROLLOUT: (self.t_main, "")}), now=self.now)
        self.assertIs(r.verdict, hi.Verdict.UNKNOWN)

    def test_active_runtime_cache_used_when_runtime_json_absent(self):
        reader = FakeReader({
            f"{PERSONAS}/seat/active-runtime.json": (0, json.dumps({"runtime": "codex/interactive-chatgpt"})),
            MAIN_ROLLOUT: (self.t_main, self.main),
        })
        r = read(reader, now=self.now)
        self.assertEqual(r.harness, "codex")
        self.assertIs(r.verdict, hi.Verdict.IDLE)
        self.assertIn("mtime-only guard", r.reason)

    def test_default_harness_when_no_runtime_state(self):
        r = read(FakeReader({MAIN_ROLLOUT: (self.t_main, self.main)}), default="codex", now=self.now)
        self.assertIs(r.verdict, hi.Verdict.IDLE)

    def test_hermes_runtime_has_no_classifier(self):
        reader = FakeReader({f"{PERSONAS}/seat/runtime-controller/runtime.json":
                             (0, runtime_json("hermes", self.started))})
        r = read(reader, now=self.now)
        self.assertIs(r.verdict, hi.Verdict.UNKNOWN)
        self.assertEqual(r.harness, "hermes")

    def test_reader_errors_never_raise(self):
        r = read(RaisingReader(), now=self.now)
        self.assertIs(r.verdict, hi.Verdict.UNKNOWN)
        self.assertIsNone(hi.locate_log("codex", "seat", HOME, None, reader=RaisingReader()))

    def test_locate_log_returns_main_rollout(self):
        sub = fx_text("codex/subagent_rollout.jsonl")
        reader = self.reader(**{SUB_ROLLOUT: (self.t_main + 36000, sub)})
        self.assertEqual(hi.locate_log("codex", "seat", HOME, None, reader=reader,
                                       started_at=epoch(self.started)), Path(MAIN_ROLLOUT))

    def test_thread_source_and_dict_source_mark_a_subthread(self):
        def head(**payload):
            return json.dumps({"type": "session_meta", "payload": {"id": "x", **payload}}) + "\n"
        self.assertFalse(hi._codex_meta_is_subthread(head(source="cli", thread_source="user")))
        self.assertFalse(hi._codex_meta_is_subthread(head(source="cli")))  # older CLI: no key
        self.assertTrue(hi._codex_meta_is_subthread(head(source="cli", thread_source="subagent")))
        self.assertTrue(hi._codex_meta_is_subthread(
            head(source={"subagent": {"thread_spawn": {}}}, thread_source="user")))

    def test_subthread_rollout_by_thread_source_is_skipped_by_the_locator(self):
        sub = fx("codex/subagent_rollout.jsonl")
        sub[0] = dict(sub[0], payload={"id": "01a1803a-0000-7000-8000-00000000a9e7", "source": "cli",
                                       "originator": "codex-tui", "thread_source": "subagent"})
        reader = self.reader(**{SUB_ROLLOUT: (self.t_main + 36000, to_text(sub))})
        r = read(reader, now=self.t_main + 36000 + 700)
        self.assertEqual(r.log_path, MAIN_ROLLOUT)

    def test_long_first_line_fallback_checks_thread_source_before_base_instructions(self):
        big = "y" * (hi.HEAD_BYTES * 2)
        sub = json.dumps({"type": "session_meta", "payload": {
            "id": "x", "source": "cli", "thread_source": "subagent", "base_instructions": big}})
        self.assertTrue(hi._codex_meta_is_subthread(sub[:hi.HEAD_BYTES]))
        dict_src = json.dumps({"type": "session_meta", "payload": {
            "id": "x", "source": {"subagent": {}}, "thread_source": "user", "base_instructions": big}})
        self.assertTrue(hi._codex_meta_is_subthread(dict_src[:hi.HEAD_BYTES]))
        root = json.dumps({"type": "session_meta", "payload": {
            "id": "x", "source": "cli", "thread_source": "user",
            "base_instructions": 'spawn a "subagent" when ' + big}})
        self.assertFalse(hi._codex_meta_is_subthread(root[:hi.HEAD_BYTES]))
        # the bare "subagent" needle only looks before base_instructions (raw text: a
        # quoted word after it, as unescaped prose would show, must not trip it)
        raw = ('{"type":"session_meta","payload":{"id":"x","source":"cli","thread_source":"user",'
               '"base_instructions":"spawn a "subagent" when ' + big)
        self.assertFalse(hi._codex_meta_is_subthread(raw[:hi.HEAD_BYTES]))
        self.assertTrue(hi._codex_meta_is_subthread(
            '{"type":"session_meta","payload":{"id":"x","subagent":{},"base_instructions":"' + big))

    def test_long_session_meta_line_falls_back_to_text_match(self):
        meta = {"type": "session_meta", "payload": {"id": "x", "parent_thread_id": "p",
                                                     "base_instructions": "y" * (hi.HEAD_BYTES * 2)}}
        self.assertTrue(hi._codex_meta_is_subthread(json.dumps(meta)[:hi.HEAD_BYTES]))
        top = {"type": "session_meta", "payload": {"id": "x", "source": "cli",
                                                    "base_instructions": "y" * (hi.HEAD_BYTES * 2)}}
        self.assertFalse(hi._codex_meta_is_subthread(json.dumps(top)[:hi.HEAD_BYTES]))


class ReadIdleClaudeTest(unittest.TestCase):
    def setUp(self):
        self.idle_recs = fx("claude/idle.jsonl")
        self.t_end = last_activity(self.idle_recs)
        self.now = self.t_end + 700

    def files(self, started="2026-10-13T12:00:00Z", **extra):
        files = {f"{PERSONAS}/seat/runtime-controller/runtime.json":
                 (0, runtime_json("claude", started))}
        files.update(extra)
        return files

    def test_claude_seat_idle_with_session_id(self):
        path = f"{PDIR}/66c41242-708a-4b2d-8f1e-4654544c95bb.jsonl"
        r = read(FakeReader(self.files(**{path: (self.t_end, fx_text("claude/idle.jsonl"))})), now=self.now)
        self.assertIs(r.verdict, hi.Verdict.IDLE, r.reason)
        self.assertEqual(r.harness, "claude-code")
        self.assertEqual(r.session_id, "66c41242-708a-4b2d-8f1e-4654544c95bb")

    def test_mtime_rewritten_while_idle_does_not_reset_quiet_clock(self):
        # T-5050: metadata rewrite bumps mtime to "now"; timestamps say quiet 700 s.
        text = to_text(self.idle_recs + [{"type": "ai-title"}])
        path = f"{PDIR}/66c41242-708a-4b2d-8f1e-4654544c95bb.jsonl"
        r = read(FakeReader(self.files(**{path: (self.now, text)})), now=self.now)
        self.assertIs(r.verdict, hi.Verdict.IDLE)
        self.assertAlmostEqual(r.quiet_s, 700, places=3)

    def test_candidate_with_newest_activity_wins_over_newest_mtime(self):
        old = f"{PDIR}/11111111-0000-4000-8000-000000000000.jsonl"
        live = f"{PDIR}/22222222-0000-4000-8000-000000000000.jsonl"
        working = fx("claude/working_prompt.jsonl")
        # the OLD session has the newer mtime (rewritten while idle) but older activity
        older = [dict(r, timestamp="2026-10-13T11:00:00.000Z") if "timestamp" in r else r
                 for r in self.idle_recs]
        files = self.files(**{old: (self.now, to_text(older)),
                              live: (self.now - 10, to_text(working))})
        r = read(FakeReader(files), now=self.now)
        self.assertEqual(r.log_path, live)
        self.assertIs(r.verdict, hi.Verdict.WORKING)

    def test_transcript_predating_runtime_start_is_unknown(self):
        path = f"{PDIR}/66c41242-708a-4b2d-8f1e-4654544c95bb.jsonl"
        files = {f"{PERSONAS}/seat/runtime-controller/runtime.json":
                 (0, runtime_json("claude", "2026-10-14T00:00:00Z")),
                 path: (self.t_end, fx_text("claude/idle.jsonl"))}
        r = read(FakeReader(files), now=self.now)
        self.assertIs(r.verdict, hi.Verdict.UNKNOWN)
        self.assertIn("stale", r.reason)

    def test_missing_projects_dir_is_unknown(self):
        self.assertIs(read(FakeReader(self.files()), now=self.now).verdict, hi.Verdict.UNKNOWN)

    def _shell_then_more_work(self):  # runtime started before the 2026-10-08 launch: EARLY
        """MED-1: bg_shell_open (launch of bbv2hefe2 + turn end), ~320 KiB of untimestamped
        blobs, then a complete later turn 6 min after the launch. The 256 KiB classification
        window sees only the blobs and the later turn: no backgroundTaskId in it."""
        launch = fx("claude/bg_shell_open.jsonl")
        t_launch = last_activity(launch)
        later = shift(self.idle_recs, (t_launch + 360) - epoch(self.idle_recs[0]["timestamp"]))
        blob = json.dumps({"type": "api-request-blob", "pad": "x" * 4000})
        text = to_text(launch) + (blob + "\n") * 80 + to_text(later)
        return text, later

    def test_shell_launched_before_the_window_still_reads_bg_wait(self):
        text, later = self._shell_then_more_work()
        tail = text.encode("utf-8")[-hi.TAIL_BYTES:]
        self.assertNotIn(b"backgroundTaskId", tail, "the launch must be outside the small window")
        now = last_activity(later) + 700
        path = f"{PDIR}/33333333-0000-4000-8000-000000000000.jsonl"
        r = read(FakeReader(self.files(EARLY, **{path: (now, text)})), now=now)
        self.assertIs(r.verdict, hi.Verdict.BG_WAIT, r.reason)
        self.assertEqual(r.open_bg, 1)
        # without the wide scan (a reader lacking grep_tail) this was the old IDLE verdict
        r_old = read(NoGrepReader(self.files(EARLY, **{path: (now, text)})), now=now)
        self.assertIs(r_old.verdict, hi.Verdict.IDLE)

    def test_completion_inside_the_scan_closes_the_early_shell(self):
        # launch, its mid-turn completion 2 min later, then the blobs and the later turn:
        # both markers sit OUTSIDE the 256 KiB window and only the wide scan sees them.
        launch = fx("claude/bg_shell_open.jsonl")
        t_launch = last_activity(launch)
        done = {"type": "attachment",
                "timestamp": shift([{"timestamp": launch[-1]["timestamp"]}], 120)[0]["timestamp"],
                "attachment": {"type": "queued_command", "commandMode": "task-notification",
                               "prompt": "<task-notification><task-id>bbv2hefe2</task-id>"
                                         "<status>completed</status></task-notification>"}}
        later = shift(self.idle_recs, (t_launch + 360) - epoch(self.idle_recs[0]["timestamp"]))
        blob = json.dumps({"type": "api-request-blob", "pad": "x" * 4000})
        text = to_text(launch + [done]) + (blob + "\n") * 80 + to_text(later)
        self.assertNotIn(b"task-id", text.encode("utf-8")[-hi.TAIL_BYTES:])
        t = last_activity(later)
        path = f"{PDIR}/33333333-0000-4000-8000-000000000000.jsonl"
        r = read(FakeReader(self.files(EARLY, **{path: (t + 700, text)})), now=t + 700)
        self.assertIs(r.verdict, hi.Verdict.IDLE, r.reason)
        self.assertEqual(r.open_bg, 0)

    def test_old_open_shell_is_released_only_by_the_quiet_clock(self):
        # r2: the shell was launched ~19 min before `now` (older than bg_max_s=600) and is
        # still open. Recent activity (quiet 300 s) keeps it BG_WAIT: a cold pre-push can
        # run 60+ min. Only quiet >= bg_max_s releases it to IDLE.
        text, later = self._shell_then_more_work()
        t = last_activity(later)
        path = f"{PDIR}/33333333-0000-4000-8000-000000000000.jsonl"
        files = self.files(EARLY, **{path: (t, text)})
        launch_ts = last_activity(fx("claude/bg_shell_open.jsonl"))
        self.assertGreater(t + 300 - launch_ts, 600, "the shell is older than bg_max_s")
        r = read(FakeReader(files), now=t + 300, bg_max_s=600, quiet_s=60)
        self.assertIs(r.verdict, hi.Verdict.BG_WAIT, r.reason)
        r = read(FakeReader(files), now=t + 600, bg_max_s=600, quiet_s=60)
        self.assertIs(r.verdict, hi.Verdict.IDLE, r.reason)

    def test_marker_lines_without_trailing_newlines_still_parse(self):
        # a `tail -c | grep -F` reader may return lines with no trailing newline
        text, later = self._shell_then_more_work()
        now = last_activity(later) + 700
        path = f"{PDIR}/33333333-0000-4000-8000-000000000000.jsonl"
        reader = StrippedGrepReader(self.files(EARLY, **{path: (now, text)}))
        self.assertTrue(reader.grep_tail("seat", path, hi.BG_SCAN_BYTES, hi._BG_NEEDLES))
        self.assertFalse(any(ln.endswith("\n") for ln in
                             reader.grep_tail("seat", path, hi.BG_SCAN_BYTES, hi._BG_NEEDLES)))
        r = read(reader, now=now)
        self.assertIs(r.verdict, hi.Verdict.BG_WAIT, r.reason)
        self.assertEqual(r.open_bg, 1)
        two = [{"type": "user", "timestamp": "2026-10-08T13:03:43.898Z",
                "toolUseResult": {"backgroundTaskId": "aaa"}},
               {"type": "user", "timestamp": "2026-10-08T13:03:44.000Z",
                "toolUseResult": {"backgroundTaskId": "bbb"}}]
        glued = StrippedGrepReader({"/x.jsonl": (0, to_text(two))})
        started, _ = hi._bg_markers(glued, "seat", "/x.jsonl")
        self.assertEqual(set(started), {"aaa", "bbb"})

    def test_scan_is_bounded_and_prefiltered(self):
        text, later = self._shell_then_more_work()
        now = last_activity(later) + 700
        path = f"{PDIR}/33333333-0000-4000-8000-000000000000.jsonl"
        reader = FakeReader(self.files(EARLY, **{path: (now, text)}))
        read(reader, now=now)
        self.assertIn(("grep", "seat", path, hi.BG_SCAN_BYTES), reader.calls)

    def test_local_reader_grep_tail(self):
        path = str(FIX / "claude" / "bg_shell_attachment_closed.jsonl")
        lines = hi.LocalReader().grep_tail(None, path, hi.BG_SCAN_BYTES, hi._BG_NEEDLES)
        self.assertEqual(len(lines), 2)  # the launch and the attachment notification
        cut = hi.LocalReader().grep_tail(None, path, 300, hi._BG_NEEDLES)
        self.assertEqual(cut, [])  # the last 300 bytes hold no marker
        self.assertIsNone(hi.LocalReader().grep_tail(None, str(FIX / "nope"), 10, hi._BG_NEEDLES))

    def test_boundary_beyond_first_window_is_found_by_widening(self):
        blob = json.dumps({"type": "api-request-blob", "pad": "x" * 4000})
        text = to_text(self.idle_recs) + (blob + "\n") * 80  # ~320 KiB after the turn end
        self.assertGreater(len(text), hi.TAIL_BYTES)
        path = f"{PDIR}/66c41242-708a-4b2d-8f1e-4654544c95bb.jsonl"
        reader = FakeReader(self.files(**{path: (self.now, text)}))
        r = read(reader, now=self.now)
        self.assertIs(r.verdict, hi.Verdict.IDLE, r.reason)
        self.assertIn(hi.WIDE_TAIL_BYTES, [c[3] for c in reader.calls if c[0] == "tail"])


class LocalReaderTest(unittest.TestCase):
    """LocalReader over the committed fixture tree only (no seat paths)."""

    def test_newest_files_depth_and_suffix(self):
        rows = hi.LocalReader().newest_files(None, str(FIX), 2, 2, ".jsonl", 50)
        names = {Path(p).relative_to(FIX).as_posix() for _m, p in rows}
        self.assertIn("claude/idle.jsonl", names)
        self.assertIn("codex/idle.jsonl", names)
        self.assertIsNone(hi.LocalReader().newest_files(None, str(FIX / "nope"), 1, 1, ".jsonl", 5))

    def test_head_and_tail(self):
        path = str(FIX / "codex" / "idle.jsonl")
        text = fx_text("codex/idle.jsonl")
        self.assertEqual(hi.LocalReader().read_text_head(None, path, 20), text[:20])
        self.assertEqual(hi.LocalReader().read_text_tail(None, path, 20), text[-20:])
        self.assertIsNone(hi.LocalReader().read_text_tail(None, str(FIX / "nope"), 20))


class ResolveHarnessTest(unittest.TestCase):
    def test_runtime_json_wins_over_cache(self):
        reader = FakeReader({
            f"{PERSONAS}/seat/runtime-controller/runtime.json": (0, runtime_json("codex", "2026-11-01T17:00:00Z")),
            f"{PERSONAS}/seat/active-runtime.json": (0, json.dumps({"runtime": "claude/x"})),
        })
        profile, started, source = hi.resolve_harness("seat", home=HOME, default="claude-code", reader=reader)
        self.assertEqual((profile, source), ("codex", "runtime.json"))
        self.assertEqual(started, epoch("2026-11-01T17:00:00Z"))

    def test_bad_schema_falls_through(self):
        reader = FakeReader({f"{PERSONAS}/seat/runtime-controller/runtime.json":
                             (0, json.dumps({"schema": "other", "harness": "codex"}))})
        self.assertEqual(hi.resolve_harness("seat", home=HOME, default="claude-code", reader=reader),
                         ("claude-code", None, "default"))

    def test_live_harness_helpers(self):
        self.assertIsNone(live_harness.runtime_profile({"schema": "x", "harness": "codex"}))
        self.assertEqual(live_harness.runtime_profile(
            {"schema": "pantheon-runtime-state/v1", "harness": "claude"}), "claude-code")
        self.assertIsNone(live_harness.runtime_started_at({"started_at": "not a date"}))
        self.assertIsNone(live_harness.runtime_started_at({"started_at": "2026-11-01T17:00:00"}))
        self.assertIsNone(live_harness.profile_for("vim"))


# ------------------------------------------------------------------- pane safety gate
CLAUDE_IDLE_PANE = "\n".join(["● some earlier output", "─" * 80, "❯ ", "─" * 80, "  ? for shortcuts"])
CLAUDE_BUSY_PANE = "\n".join(["│ working on the thing…", "✻ Thinking…", "  ⎿  running tool",
                              "  esc to interrupt"])
CODEX_UPDATE_MODAL = ("\n  ✨ Update available! 0.136.0 -> 0.146.1\n\n"
                      "  Release notes: https://github.com/openai/codex/releases/latest\n\n"
                      "› 1. Update now (runs `npm install -g @openai/codex`)\n  2. Skip\n"
                      "  3. Skip until next version\n\n  Press enter to continue\n")


class PaneInjectSafeTest(unittest.TestCase):
    def setUp(self):
        self.codex_idle = (PANES / "watchman_idle_2026-10-15.txt").read_text(encoding="utf-8")

    def test_codex_idle_pane_is_safe(self):
        self.assertTrue(hi.pane_inject_safe(self.codex_idle, "codex"))

    def test_codex_idle_pane_with_escapes_is_safe(self):
        text = (PANES / "watchman_idle_escaped_2026-10-15.txt").read_text(encoding="utf-8")
        self.assertTrue(hi.pane_inject_safe(text, "codex"))

    def test_codex_busy_pane_is_not_safe(self):
        text = (PANES / "codex_busy_baseline_2026-10-13.txt").read_text(encoding="utf-8")
        self.assertFalse(hi.pane_inject_safe(text, "codex"))

    def test_codex_busy_footer_in_bottom_region_is_not_safe(self):
        lines = self.codex_idle.rstrip("\n").splitlines()
        busy = "\n".join(lines[:-3] + ["◦ Working (12s • esc to interrupt)"] + lines[-3:])
        self.assertFalse(hi.pane_inject_safe(busy, "codex"))

    def test_codex_update_modal_is_not_safe(self):
        self.assertFalse(hi.pane_inject_safe(self.codex_idle + CODEX_UPDATE_MODAL, "codex"))

    def test_claude_gate_is_pane_idle_state(self):
        self.assertTrue(hi.pane_inject_safe(CLAUDE_IDLE_PANE, "claude-code"))
        self.assertFalse(hi.pane_inject_safe(CLAUDE_BUSY_PANE, "claude-code"))

    def test_claude_pane_under_codex_profile_and_unknown_harness(self):
        self.assertFalse(hi.pane_inject_safe(self.codex_idle, "hermes"))
        self.assertFalse(hi.pane_inject_safe(self.codex_idle, None))
        self.assertFalse(hi.pane_inject_safe("", "codex"))
        self.assertFalse(hi.pane_inject_safe(None, "claude-code"))

    def test_gate_errors_are_not_safe(self):
        with mock.patch.object(hi, "_sibling", side_effect=ImportError("gone")):
            self.assertFalse(hi.pane_inject_safe(self.codex_idle, "codex"))


if __name__ == "__main__":
    unittest.main()
