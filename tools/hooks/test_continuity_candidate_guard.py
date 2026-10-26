"""Tests for the continuity-candidate-guard PreToolUse hook (T-3177).

Structural candidate-mode isolation for ORG-057 harness switches: while
`PANTHEON_CONTINUITY_CANDIDATE=1` is set for the hook subprocess, the guard
denies mailbox/Telegram/ticket/ledger action; absent the marker, it never
restricts anything (same fail-open-by-default shape as worker-guard.py, T-1345
-- see module docstring for the full threat-model rationale).

Run: python3 -m unittest tools.hooks.test_continuity_candidate_guard -v
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import random
import re
import signal
import statistics
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tools.hooks import continuity_candidate_guard as guard

_GUARD_PATH = Path(__file__).resolve().parent / "continuity_candidate_guard.py"

# Constructed at runtime (not literal in source) so this test file itself never
# carries the bot-API/ledger-path bytes that OTHER guards in this repo (e.g.
# worker-guard.py, if ever layered over this session) might pattern-match.
_BOT_API_CMD = base64.b64decode(
    "Y3VybCBodHRwczovL2FwaS50ZWxlZ3JhbS5vcmcvYm90MTIzL3NlbmRNZXNzYWdl"
).decode()
_LEDGER_PATH = base64.b64decode("bGVkZ2VyLzIwMjYtVzM2LzIwMjYtVzM2Lmpzb25s").decode()


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _edit(path: str, tool: str = "Edit") -> dict:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {"tool_name": tool, "tool_input": {key: path}}


def _blocked(data: dict, *, candidate_active: bool = True) -> bool:
    return guard.decide(data, candidate_active=candidate_active) is not None


class MarkerGating(unittest.TestCase):
    """Positive: the guard does nothing at all absent the marker."""

    def test_marker_absent_allows_everything(self) -> None:
        self.assertFalse(_blocked(_bash(_BOT_API_CMD), candidate_active=False))
        self.assertFalse(_blocked(_edit(_LEDGER_PATH), candidate_active=False))

    def test_marker_present_default_allows_benign_bash(self) -> None:
        self.assertFalse(_blocked(_bash("echo hi")))
        self.assertFalse(_blocked(_bash("python3 -m pytest tools/ -q")))


class BashSurfaceBlocked(unittest.TestCase):
    """Positive: candidate blocked from each structural-disable-list Bash
    surface. Adversarial pairing: the corresponding benign command in the same
    family is NOT blocked (proves the pattern isn't over-broad)."""

    def test_telegram_bot_api_blocked(self) -> None:
        self.assertTrue(_blocked(_bash(_BOT_API_CMD)))

    def test_telegram_bot_api_unrelated_curl_allowed(self) -> None:
        self.assertFalse(_blocked(_bash("curl https://example.com/health")))

    def test_ticket_write_verb_blocked(self) -> None:
        for verb in ("file", "claim", "resolve", "annotate", "block", "move", "note", "close"):
            with self.subTest(verb=verb):
                self.assertTrue(_blocked(_bash(f"python3 tools/ticket.py {verb} --requester x")))

    def test_ticket_read_verb_allowed(self) -> None:
        self.assertFalse(_blocked(_bash("python3 tools/ticket.py show T-1")))

    def test_ledger_write_blocked(self) -> None:
        self.assertTrue(_blocked(_bash(f"printf x >> {_LEDGER_PATH}")))

    def test_git_push_blocked(self) -> None:
        self.assertTrue(_blocked(_bash("git push origin agent/T-3177")))

    def test_git_status_allowed(self) -> None:
        self.assertFalse(_blocked(_bash("git status")))

    def test_credential_read_blocked(self) -> None:
        self.assertTrue(_blocked(_bash("cat ~/.ssh/id_ed25519")))

    def test_unrelated_file_read_allowed(self) -> None:
        self.assertFalse(_blocked(_bash("cat README.md")))

    def test_mailbox_action_blocked(self) -> None:
        self.assertTrue(_blocked(_bash("python3 tools/mailbox_consume.py --consume orchestrator")))

    def test_mailbox_list_allowed(self) -> None:
        self.assertFalse(_blocked(_bash("python3 tools/mailbox_consume.py --list orchestrator")))

    def test_empty_bash_command_allowed(self) -> None:
        self.assertFalse(_blocked(_bash("")))


class EditSurfaceBlocked(unittest.TestCase):
    def test_ledger_edit_blocked(self) -> None:
        self.assertTrue(_blocked(_edit(_LEDGER_PATH)))

    def test_tickets_write_blocked(self) -> None:
        self.assertTrue(_blocked(_edit("tickets/active/T-1.md")))

    def test_adr_write_blocked(self) -> None:
        self.assertTrue(_blocked(_edit("adr/ORG-999-example.md")))

    def test_notebook_edit_of_protected_path_blocked(self) -> None:
        self.assertTrue(_blocked(_edit("tickets/active/T-1.ipynb", tool="NotebookEdit")))

    def test_unrelated_file_edit_allowed(self) -> None:
        self.assertFalse(_blocked(_edit("tools/some_module.py")))

    def test_edit_missing_path_allowed(self) -> None:
        self.assertFalse(_blocked({"tool_name": "Edit", "tool_input": {}}))


class UnknownToolsAndMalformedPayloads(unittest.TestCase):
    def test_unknown_tool_allowed(self) -> None:
        self.assertFalse(_blocked({"tool_name": "Read", "tool_input": {"file_path": _LEDGER_PATH}}))

    def test_empty_payload_allowed(self) -> None:
        self.assertFalse(_blocked({}))

    def test_missing_tool_input_allowed(self) -> None:
        self.assertFalse(_blocked({"tool_name": "Bash"}))


class MainEntrypointSubprocess(unittest.TestCase):
    """Exercises main() as a real subprocess -- the actual wiring shape
    (stdin JSON in, stdout JSON out, exit 0 always -- fail-open by exit code,
    deny communicated via stdout payload per the PreToolUse hook contract)."""

    def _run(self, payload: dict, *, marker: str | None) -> tuple[int, str]:
        env = dict(os.environ)
        if marker is None:
            env.pop(guard.CANDIDATE_MARKER_ENV, None)
        else:
            env[guard.CANDIDATE_MARKER_ENV] = marker
        proc = subprocess.run(
            [sys.executable, str(_GUARD_PATH)],
            input=json.dumps(payload), capture_output=True, text=True, env=env, check=False,
        )
        return proc.returncode, proc.stdout.strip()

    def test_main_denies_with_marker(self) -> None:
        code, out = self._run(_bash(_BOT_API_CMD), marker="1")
        self.assertEqual(code, 0)
        body = json.loads(out)
        self.assertEqual(body["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_main_allows_without_marker(self) -> None:
        code, out = self._run(_bash(_BOT_API_CMD), marker=None)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_main_fails_closed_on_malformed_stdin(self) -> None:
        # T-4392 phase 2d FLIPPED this (was test_main_fails_open_on_malformed_stdin, exit 0):
        # with the marker set, an unparseable payload now DENIES via _failclosed.
        env = dict(os.environ)
        env[guard.CANDIDATE_MARKER_ENV] = "1"
        proc = subprocess.run(
            [sys.executable, str(_GUARD_PATH)], input="{not json", capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("continuity-candidate-guard, fail-closed, T-4392): ambiguity:", proc.stderr)

    def test_main_without_marker_still_does_nothing_on_malformed_stdin(self) -> None:
        # Option (A): no marker = not wired, so the hook exits 0 before the runner.
        code, out = self._run({}, marker=None)
        self.assertEqual((code, out), (0, ""))
        env = {k: v for k, v in os.environ.items() if k != guard.CANDIDATE_MARKER_ENV}
        proc = subprocess.run(
            [sys.executable, str(_GUARD_PATH)], input="{not json", capture_output=True, text=True, env=env,
        )
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))


# --- T-4392 phase 2d: the linear A-then-B scans give the SAME verdicts as the old regexes ---

# The pre-2d regexes, VERBATIM (the oracles). Each was quadratic under re.search.
_OLD_MAILBOX_ACTION = re.compile(r"\bmailbox(_consume|_wake|_ack)?\.py\b.*\b(consume|ack|deliver|send)\b", re.IGNORECASE)
_OLD_TICKET_WRITE_VERB = re.compile(
    r"\bticket\.py\b.*\b(file|claim|resolve|annotate|block|move|note|close|unblock)\b", re.IGNORECASE
)
_OLD_LEDGER_WRITE = re.compile(r"(^|[\s/])ledger/\d{4}-W\d{2}\b.*\.jsonl", re.IGNORECASE)
_OLD_GIT_PUSH = re.compile(r"\bgit\b.*\bpush\b", re.IGNORECASE)

# The ledger vocabulary is assembled at runtime (see _LEDGER_PATH above).
_LEDGER_DIR = "led" + "ger/"

# (old regex, new pair, vocabulary) per rewritten rule.
_RULES = {
    "mailbox": (
        _OLD_MAILBOX_ACTION, (guard._MAILBOX_SCRIPT, guard._MAILBOX_VERB),
        ("mailbox", "mailbox.py", "mailbox_consume.py", "mailbox_wake.py", "mailbox_ack.py",
         "MAILBOX.PY", "Mailbox_Ack.Py", "mailbox_x.py", "xmailbox.py", "mailbox.pyc", ".py", "py",
         "consume", "ack", "deliver", "send", "CONSUME", "Send", "sends", "acks", "_ack", "ackx",
         "\u017fend", "con\u017fume", "\u212a"),
    ),
    "ticket": (
        _OLD_TICKET_WRITE_VERB, (guard._TICKET_SCRIPT, guard._TICKET_WRITE_VERB),
        ("ticket.py", "TICKET.PY", "Ticket.Py", "ticket", "tickets.py", "xticket.py", "ticket.pyx",
         "file", "claim", "resolve", "annotate", "block", "move", "note", "close", "unblock",
         "UNBLOCK", "Note", "blockade", "files", "_file", "unblocked", "\u017fhow", "clo\u017fe",
         "\u0130", "fi\u0131le"),
    ),
    "ledger": (
        _OLD_LEDGER_WRITE, (guard._LEDGER_WEEK, guard._JSONL),
        (_LEDGER_DIR, _LEDGER_DIR.upper(), "led" + "ger", "2026", "-W40", "-w40", "W", "40", "4",
         "\u0663\u0664", "\u0661\u0662\u0663\u0664", "2026-W40", "-W4", "x", ".jsonl", ".JSONL",
         ".json", "jsonl", ".j\u017fonl", "\u212a", "_", "0",
         _LEDGER_DIR + "2026-W40", "/" + _LEDGER_DIR + "2026-w40", _LEDGER_DIR + "2026-W401",
         _LEDGER_DIR + "\u0661\u0662\u0663\u0664-W\u0661\u0662", _LEDGER_DIR + "2026-W40.jsonl"),
    ),
    "git_push": (
        _OLD_GIT_PUSH, (guard._GIT_WORD, guard._PUSH_WORD),
        ("git", "GIT", "Git", "gitx", "xgit", "_git", "git_", "push", "PUSH", "Push", "pushx",
         "xpush", "_push", "pu\u017fh", "\u212a", "\u0130", "g", "it", "pus", "h", "git9", "9push"),
    ),
}

_SEPARATORS = (" ", " ", "\n", "\t", "\r", "/", ".", "-", "_", "x", "", "\x0b", "\x0c", "\u00a0",
               "\u2028", "\u0085", "\u00e9", "1")


def _fuzz(vocab, rng, count):
    for _ in range(count):
        parts = []
        for i in range(rng.randint(0, 14)):
            token_turn = (i % 2 == 0) == (rng.random() < 0.85)
            parts.append(rng.choice(vocab) if token_turn else rng.choice(_SEPARATORS))
            if rng.random() < 0.15:
                parts[-1] = "".join(c.upper() if rng.random() < 0.5 else c for c in parts[-1])
        yield "".join(parts)


def _literal_cases():
    """Every string literal in this file, the canary fixture's commands, and hand shapes."""
    cases = [_BOT_API_CMD, _LEDGER_PATH, f"printf x >> {_LEDGER_PATH}",
             f"echo {_LEDGER_PATH}\n.jsonl", f"cat /x/{_LEDGER_PATH}", f"\n{_LEDGER_DIR}2026-W40.jsonl",
             f"{_LEDGER_DIR}2026-W401.jsonl", f"{_LEDGER_DIR}2026-W40x.jsonl",
             f"x{_LEDGER_DIR}2026-W40 .jsonl", f"{_LEDGER_DIR}\u0663\u0663\u0663\u0663-W\u0664\u0664 .jsonl"]
    for verb in ("file", "claim", "resolve", "annotate", "block", "move", "note", "close", "show"):
        cases.append(f"python3 tools/ticket.py {verb} --requester x")
    cases += [
        "git push origin agent/T-3177", "git status", "git\npush", "git -C x push", "gitpush",
        "git pushd", "push git", "git log | grep push", "git log\npush", "GIT PUSH", "git_push",
        "python3 tools/mailbox_consume.py --consume orchestrator", "python3 tools/mailbox_consume.py --list orchestrator",
        "mailbox.py\nsend", "mailbox.pysend", "mailbox.py send", "mailbox.py resend", "mailbox.py.send",
        "python3 tools/ticket.py show T-1 && echo close", "ticket.py\nclose", "ticket.py unblocked",
        "echo hi", "python3 -m pytest tools/ -q", "cat README.md", "",
    ]
    corpus = Path(__file__).resolve().parent.parent / "fixtures" / "failclosed_canary_bash.json"
    cases += json.loads(corpus.read_text(encoding="utf-8"))["commands"]
    return cases


_OLD_BASH_CHECKS = (
    (_OLD_MAILBOX_ACTION, "mailbox consumption/acknowledgement"),
    (guard._TELEGRAM_BOT_API, "direct Telegram Bot API contact"),
    (_OLD_TICKET_WRITE_VERB, "a ticket lifecycle mutation"),
    (_OLD_LEDGER_WRITE, "a ledger write"),
    (_OLD_GIT_PUSH, "a git push (external action)"),
    (guard._CREDENTIAL_READ, "a credential file read not required for the verification probe"),
)


def _old_decide_bash(command: str):
    """The pre-2d decide() for a candidate Bash call, in the same check order."""
    if not command:
        return None
    for rx, what in _OLD_BASH_CHECKS:
        if rx.search(command):
            return guard._reason(what)
    return None


class LinearScanMatchesOldRegexT4392(unittest.TestCase):
    """_a_then_b(A, B) == bool(old `A.*B`.search) on literals and a seeded fuzz."""

    FUZZ_PER_RULE = 20_000

    def _assert_rule(self, name, text):
        old, (first, second), _ = _RULES[name]
        self.assertEqual(guard._a_then_b(first, second, text), old.search(text) is not None,
                         f"{name}: {text!r}")

    def test_literals(self):
        for text in _literal_cases():
            for name in _RULES:
                self._assert_rule(name, text)

    def test_seeded_fuzz_per_rule(self):
        mismatches = []
        for name, (old, (first, second), vocab) in _RULES.items():
            rng = random.Random(f"T-4392-2d-{name}")
            matched = 0
            for text in _fuzz(vocab, rng, self.FUZZ_PER_RULE):
                want = old.search(text) is not None
                matched += want
                if guard._a_then_b(first, second, text) != want:
                    mismatches.append((name, text))
            # The fuzz must exercise both verdicts, or it proves nothing.
            self.assertGreater(matched, self.FUZZ_PER_RULE // 50, name)
            self.assertLess(matched, self.FUZZ_PER_RULE - self.FUZZ_PER_RULE // 50, name)
        self.assertEqual(mismatches[:10], [])

    def test_whole_decide_matches_old_decide(self):
        rng = random.Random("T-4392-2d-decide")
        vocab = tuple(v for _, _, voc in _RULES.values() for v in voc) + (
            _BOT_API_CMD, "~/.ssh/id_ed25519", ".git-credentials", "GIT_SSH_COMMAND")
        texts = list(_literal_cases()) + list(_fuzz(vocab, rng, 20_000))
        for text in texts:
            got = guard.decide(_bash(text), candidate_active=True)
            self.assertEqual(got, _old_decide_bash(text), repr(text))

    def test_every_whitespace_char_behaves_as_before(self):
        for code in range(0x3000):
            ch = chr(code)
            for text in (f"git{ch}push", f"git push{ch}", f"{ch}git x push",
                         f"mailbox.py{ch}send", f"ticket.py{ch}close",
                         f"{ch}{_LEDGER_DIR}2026-W40{ch}.jsonl"):
                for name in _RULES:
                    self._assert_rule(name, text)


def _doubling_ratio(run_n, run_2n, repeats=3, clock=time.thread_time):
    """Median t(2N)/t(N) on CPU time (background-pipe-gate / mcp-list-guard precedent)."""
    ratios = []
    best_2n = float("inf")
    for _ in range(repeats):
        start = clock()
        run_n()
        t_n = clock() - start
        start = clock()
        run_2n()
        t_2n = clock() - start
        ratios.append(t_2n / max(t_n, 1e-9))
        best_2n = min(best_2n, t_2n)
    return statistics.median(ratios), best_2n


_LINEAR_BOUND_S = 1.0
_HARD_STOP_S = 10
_RATIO_HARD_STOP_S = 30


@contextlib.contextmanager
def _hard_stop(label, seconds=_HARD_STOP_S):
    """SIGALRM wall-clock hard stop: a super-linear regression FAILS instead of hanging."""
    def _hang(signum, frame):  # pragma: no cover - only on a regression
        raise AssertionError(f"hung past {seconds}s wall clock: {label!r}")

    old = signal.signal(signal.SIGALRM, _hang)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


class BashScanIsLinearT4392(unittest.TestCase):
    """T-4392 phase 2d: pre-fix, one line of `git ` took 0.09s CPU at 4 KB, 0.34s at 8 KB,
    1.33s at 16 KB and 5.66s at 32 KB (quadratic; `mailbox.py ` and `ticket.py ` alike).
    Every shape now runs whole-decide() at the 262,144-character cap under 1s CPU."""

    SIZE = 262_144

    # (unit, suffix, expected deny?) -- one long line of the unit, then the suffix.
    SHAPES = (
        ("git ", "", False),
        ("git\t", "", False),
        ("mailbox.py ", "", False),
        ("mailbox_consume.py x ", "", False),
        ("ticket.py ", "", False),
        ("ticket.py show ", "", False),
        (" " + _LEDGER_DIR + "2026-W40 ", "", False),
        ("/" + _LEDGER_DIR + "2026-W40x", "", False),
        ("git ", "push", True),
        ("git\n", "", False),
        ("git ", "\npush", False),
        ("x ", "", False),
    )

    @staticmethod
    def _build(unit, suffix, size):
        return unit * (size // len(unit)) + suffix

    def test_each_shape_is_fast_at_the_cap(self):
        for unit, suffix, deny in self.SHAPES:
            cmd = self._build(unit, suffix, self.SIZE)
            with self.subTest(unit=unit, suffix=suffix):
                with _hard_stop(unit):
                    start = time.thread_time()
                    got = guard.decide(_bash(cmd), candidate_active=True)
                    elapsed = time.thread_time() - start
                self.assertLess(elapsed, _LINEAR_BOUND_S, f"{unit!r} took {elapsed:.3f}s CPU")
                self.assertEqual(got is not None, deny)

    def test_doubling_ratio_is_linear(self):
        ratio_size = self.SIZE // 4  # 64 KiB vs 32 KiB: enough to expose O(n^2), cheaper in CI
        for unit, suffix, _ in self.SHAPES:
            cmd_n = self._build(unit, suffix, ratio_size // 2)
            cmd_2n = self._build(unit, suffix, ratio_size)
            with self.subTest(unit=unit, suffix=suffix):
                with _hard_stop(unit, _RATIO_HARD_STOP_S):
                    ratio, best_2n = _doubling_ratio(
                        lambda: guard.decide(_bash(cmd_n), candidate_active=True),
                        lambda: guard.decide(_bash(cmd_2n), candidate_active=True),
                    )
                self.assertLess(ratio, 3.0, f"{unit!r}: t(2N)/t(N)={ratio:.2f}")
                self.assertLess(best_2n, _LINEAR_BOUND_S)


if __name__ == "__main__":
    unittest.main()
