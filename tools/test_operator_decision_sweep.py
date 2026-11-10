"""Tests for tools/operator_decision_sweep.py (T-5015 phase 2). Fixtures only; no live data."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import operator_decision_sweep as sweep  # noqa: E402

CHAT = "424242424"
NOW = datetime(2026, 10, 16, 11, 0, tzinfo=timezone.utc)


def tag(mid, body, chat=CHAT, ts=None):
    ts = ts or (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return (f'<channel source="plugin:telegram:telegram" chat_id="{chat}" message_id="{mid}" '
            f'user="u" user_id="{chat}" ts="{ts}">\n{body}\n</channel>')


def user_entry(text):
    return {"type": "user", "message": {"content": text}}


def queue_entry(text):
    return {"type": "queue-operation", "operation": "enqueue", "content": text}


def attach_entry(text):
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": text}}


def reply_entry(text):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "mcp__plugin_telegram_telegram__reply",
         "input": {"chat_id": CHAT, "text": text}}]}}


class Fixture:
    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / "projects"
        (self.projects / "proj").mkdir(parents=True)
        self.state = self.root / "state"
        self.register = self.root / "register.md"
        self.register.write_text("| 2026-10-14 | T-1 | x | y (TG 11782/11787) | z |\n| a | TG11800 | b | TG 11801 |\n")

    def session(self, entries, name="s.jsonl"):
        p = self.projects / "proj" / name
        p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        return p

    def run(self, *extra):
        argv = ["--projects-root", str(self.projects), "--state-dir", str(self.state),
                "--register", str(self.register), *extra]
        return sweep.main(argv, now=NOW.timestamp())


class HeuristicTests(unittest.TestCase):
    def test_positives(self):
        for text in ["ratified", "go ahead", "yes", "Yeah do that", "nope", "don't do that",
                     "sounds good", "ship it", "hold off on that", "from now on use X",
                     "let's swap the model", "never again", "I prefer B"]:
            ok, reason = sweep.is_decision_shaped(text, "")
            self.assertTrue(ok, text)
            self.assertTrue(reason)

    def test_short_answer_to_question(self):
        ok, reason = sweep.is_decision_shaped("the blue one", "Which colour do you want?")
        self.assertTrue(ok)
        self.assertIn("answer", reason)

    def test_long_reply_to_question_without_vocab_not_flagged(self):
        ok, _ = sweep.is_decision_shaped("the weather is nice today " * 10, "Which colour?")
        self.assertFalse(ok)

    def test_ack_negatives(self):
        for text in ["gotcha", "ok", "kk", "Thanks!", "ty", "nice", "lol", "👍", "🙏🙏", "ok."]:
            self.assertFalse(sweep.is_decision_shaped(text, "Shall I proceed?")[0], text)

    def test_statement_without_question_not_flagged(self):
        self.assertFalse(sweep.is_decision_shaped("it is raining outside", "status update.")[0])


class CoveredTests(unittest.TestCase):
    def test_forms(self):
        text = "x (TG 11782/11787) y TG11800 z TG 11801 and TG 8313 and TG 5, 6"
        ids = sweep.covered_ids(text)
        self.assertEqual(ids, {"11782", "11787", "11800", "11801", "8313", "5", "6"})

    def test_no_false_cover(self):
        self.assertEqual(sweep.covered_ids("ticket T-11782 and 11783"), set())


class ExtractTests(unittest.TestCase):
    def test_shapes_dedupe_chat_filter_and_prior_ask(self):
        fx = Fixture(self)
        fx.session([
            reply_entry("Should I swap the model? " + "x" * 400),
            queue_entry(tag(100, "yes swap it")),
            user_entry(tag(100, "yes swap it")),          # dup of queue entry
            attach_entry(tag(101, "interesting")),
            user_entry(tag(102, "ratified", chat="999")),  # wrong chat
            user_entry("quoted: " + tag(103, "no")),       # not starts-with-tag
        ])
        msgs = sweep.extract_messages(fx.projects, NOW.timestamp() - 26 * 3600, NOW.timestamp())
        self.assertEqual(sorted(m["id"] for m in msgs), ["100", "101"])
        m100 = next(m for m in msgs if m["id"] == "100")
        self.assertEqual(m100["text"], "yes swap it")
        self.assertTrue(m100["prior_ask"].startswith("Should I swap the model?"))
        self.assertLessEqual(len(m100["prior_ask"]), 300)

    def test_window_excludes_old_message(self):
        fx = Fixture(self)
        old = (NOW - timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        fx.session([user_entry(tag(7, "ratified", ts=old))])
        self.assertEqual(sweep.extract_messages(fx.projects, NOW.timestamp() - 26 * 3600, NOW.timestamp()), [])

    def test_prior_ask_is_most_recent(self):
        fx = Fixture(self)
        fx.session([reply_entry("first?"), reply_entry("second?"), user_entry(tag(5, "yes"))])
        (m,) = sweep.extract_messages(fx.projects, 0, NOW.timestamp())
        self.assertEqual(m["prior_ask"], "second?")

    def test_unreadable_root_raises(self):
        with self.assertRaises(OSError):
            sweep.extract_messages(Path("/nonexistent-root-xyz"), 0, NOW.timestamp())


class MainTests(unittest.TestCase):
    def _entries(self):
        return [reply_entry("Proceed?"), user_entry(tag(11782, "yes")), user_entry(tag(200, "go ahead")),
                user_entry(tag(201, "ok"))]

    def _ok_run(self):
        return subprocess.CompletedProcess([], 0, stdout="filed T-9999", stderr="")

    def test_once_only_across_two_runs_and_digest_mode(self):
        fx = Fixture(self)
        fx.session(self._entries())
        with mock.patch.object(sweep.subprocess, "run", return_value=self._ok_run()) as run:
            self.assertEqual(fx.run(), 0)
            self.assertEqual(run.call_count, 1)
            args = run.call_args[0][0]
            self.assertIn("file", args)
            self.assertIn("orchestrator", args)
            joined = " ".join(args)
            self.assertIn("1 uncovered", joined)
            self.assertIn("200", joined)
            self.assertNotIn("go ahead", joined)       # privacy: no text in ticket
            self.assertEqual(fx.run(), 0)
            self.assertEqual(run.call_count, 1)        # nothing new, no second ticket
        digest = fx.state / "2026-10-16.md"
        self.assertTrue(digest.exists())
        self.assertEqual(stat.S_IMODE(digest.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(fx.state.stat().st_mode), 0o700)
        self.assertIn("go ahead", digest.read_text())
        st = json.loads((fx.state / "state.json").read_text())
        self.assertIn("200", st["flagged"])
        self.assertIn("11782", st["covered"])

    def test_covered_not_flagged(self):
        fx = Fixture(self)
        fx.session([user_entry(tag(11782, "yes"))])
        with mock.patch.object(sweep.subprocess, "run") as run:
            self.assertEqual(fx.run(), 0)
            run.assert_not_called()

    def test_no_file_and_dry_run_never_call_ticket(self):
        for flag in ("--no-file", "--dry-run"):
            fx = Fixture(self)
            fx.session(self._entries())
            with mock.patch.object(sweep.subprocess, "run") as run:
                self.assertEqual(fx.run(flag), 0)
                run.assert_not_called()
            if flag == "--dry-run":
                self.assertFalse(fx.state.exists())
            else:
                self.assertTrue((fx.state / "state.json").exists())

    def test_ticket_failure_nonzero_and_not_marked(self):
        fx = Fixture(self)
        fx.session(self._entries())
        bad = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with mock.patch.object(sweep.subprocess, "run", return_value=bad):
            self.assertNotEqual(fx.run(), 0)
        with mock.patch.object(sweep.subprocess, "run", return_value=self._ok_run()) as run:
            self.assertEqual(fx.run(), 0)
            self.assertEqual(run.call_count, 1)

    def test_unreadable_projects_root_nonzero(self):
        fx = Fixture(self)
        argv = ["--projects-root", "/nonexistent-root-xyz", "--state-dir", str(fx.state),
                "--register", str(fx.register), "--dry-run"]
        self.assertNotEqual(sweep.main(argv, now=NOW.timestamp()), 0)


POLLER_SUFFIX = ('  (Reply via the telegram MCP `reply` tool with chat_id="%s". For attachments, call the '
                 'telegram MCP `download_attachment` tool with attachment_file_id, then read the returned '
                 'local path.)')


def codex_dm(mid, body, chat=CHAT, ts=None):
    """Fixture re-derived from a real DM-bearing record in ~/.codex/sessions (rollout of
    2026-11-05, message_id 13063): record keys, header and the poller's reply-instructions
    suffix are exact (suffix also matches poller.py wrap()); message text is redacted to
    neutral. T-5054."""
    ts = ts or (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"timestamp": ts, "ordinal": 614, "type": "response_item", "payload": {
        "type": "message", "id": "msg_x", "role": "user", "content": [{"type": "input_text", "text":
            f"[inbound Telegram DM from the operator | chat_id={chat} | message_id={mid}] {body}"
            + POLLER_SUFFIX % chat}],
        "internal_chat_message_metadata_passthrough": {"turn_id": "t"}}}


def codex_assistant(text):
    return {"timestamp": "2026-10-16T08:00:00.000Z", "type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}}


class SeatTests(unittest.TestCase):
    def _codex_fx(self):
        fx = Fixture(self)
        d = fx.root / "sessions" / "2026" / "10" / "16"
        d.mkdir(parents=True)
        return fx, d

    def test_codex_parse(self):
        fx, d = self._codex_fx()
        entries = [codex_assistant("Proceed with the swap? " + "x" * 400), codex_dm(300, "yes swap it"),
                   codex_dm(301, "ratified", chat="999"),
                   {"timestamp": "2026-10-16T08:00:00.000Z", "type": "response_item", "payload": {
                       "type": "message", "role": "user", "content": [{"type": "input_text",
                       "text": "quote: [inbound Telegram DM from the operator | chat_id=%s | message_id=302] no" % CHAT}]}}]
        (d / "rollout-x.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        msgs = sweep.extract_messages(fx.root / "sessions", 0, NOW.timestamp(), harness="codex", seat="watchman")
        self.assertEqual([m["id"] for m in msgs], ["watchman:300"])
        self.assertEqual(msgs[0]["text"], "yes swap it")
        self.assertTrue(msgs[0]["prior_ask"].startswith("Proceed with the swap?"))
        self.assertLessEqual(len(msgs[0]["prior_ask"]), 300)

    def test_codex_suffix_stripped_and_short_answer_flagged(self):
        fx, d = self._codex_fx()
        entries = [codex_assistant("Option A or option B?"), codex_dm(400, "B")]
        (d / "rollout-y.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        (m,) = sweep.extract_messages(fx.root / "sessions", 0, NOW.timestamp(), harness="codex", seat="watchman")
        self.assertEqual(m["text"], "B")
        self.assertTrue(sweep.is_decision_shaped(m["text"], m["prior_ask"])[0])

    def test_codex_missing_root_raises(self):
        with self.assertRaises(OSError):
            sweep.extract_messages(Path("/nonexistent-root-xyz"), 0, NOW.timestamp(), harness="codex")

    def test_seat_key_and_orchestrator_bare(self):
        fx = Fixture(self)
        fx.session([user_entry(tag(5, "yes"))])
        a = sweep.extract_messages(fx.projects, 0, NOW.timestamp())
        b = sweep.extract_messages(fx.projects, 0, NOW.timestamp(), seat="recovery")
        self.assertEqual(a[0]["id"], "5")
        self.assertEqual(b[0]["id"], "recovery:5")

    def test_covered_ids_seat_forms(self):
        self.assertEqual(sweep.covered_ids("(TG recovery:123) and TG 5 and TGs specialist:7/specialist:8"),
                         {"recovery:123", "5", "specialist:7", "specialist:8"})
        self.assertNotIn("123", sweep.covered_ids("TG recovery:123"))

    def test_seat_run_files_to_head_role_as_seat_and_dedups_by_key(self):
        fx = Fixture(self)
        fx.session([reply_entry("Proceed?"), user_entry(tag(11782, "yes")), user_entry(tag(200, "go ahead"))])
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(sweep.subprocess, "run", return_value=ok) as run:
            self.assertEqual(fx.run("--seat", "recovery"), 0)
            args = run.call_args[0][0]
            self.assertEqual(args[args.index("--requester") + 1], "recovery")
            self.assertEqual(args[args.index("--target") + 1], "role:head_engineering_orchestrator")
            joined = " ".join(args)
            self.assertIn("TG recovery:200", joined)
            self.assertIn("TG recovery:11782", joined)   # bare register row 11782 is orchestrator's, not recovery's
            self.assertNotIn("go ahead", joined)
            self.assertEqual(fx.run("--seat", "recovery"), 0)
            self.assertEqual(run.call_count, 1)
        st = json.loads((fx.state / "state.json").read_text())
        self.assertIn("recovery:200", st["flagged"])

    def test_default_run_still_orchestrator(self):
        fx = Fixture(self)
        fx.session([user_entry(tag(200, "go ahead"))])
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(sweep.subprocess, "run", return_value=ok) as run:
            fx.run()
            args = run.call_args[0][0]
            self.assertEqual(args[args.index("--requester") + 1], "orchestrator")
            self.assertEqual(args[args.index("--target") + 1], "role:engineering_orchestrator")

    def test_sessions_root_alias_and_codex_main(self):
        fx, d = self._codex_fx()
        (d / "rollout-x.jsonl").write_text(json.dumps(codex_dm(9, "ratified")) + "\n")
        argv = ["--sessions-root", str(fx.root / "sessions"), "--harness", "codex", "--seat", "watchman",
                "--state-dir", str(fx.state), "--register", str(fx.register), "--dry-run"]
        self.assertEqual(sweep.main(argv, now=NOW.timestamp()), 0)


class UnitTemplateTests(unittest.TestCase):
    D = Path(__file__).resolve().parent / "systemd"

    def test_template_is_instanced_and_dry_run(self):
        svc = (self.D / "pantheon-decision-sweep@.service").read_text()
        self.assertIn("--seat %i", svc)
        self.assertIn("--dry-run", svc)
        self.assertIn("OnFailure=pantheon-unit-failure-alert@%n.service", svc)
        tim = (self.D / "pantheon-decision-sweep@.timer").read_text()
        self.assertIn("pantheon-decision-sweep@%i.service", tim)


if __name__ == "__main__":
    unittest.main()
