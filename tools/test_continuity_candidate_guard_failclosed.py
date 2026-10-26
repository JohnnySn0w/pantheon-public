#!/usr/bin/env python3
"""continuity-candidate-guard wrapped by tools/hooks/_failclosed.py (T-4392 phase 2d).

  * CI CANARY: ordinary commands and edits must stay ALLOW through the wrap, so a
    throw-on-everything or deny-on-everything bug cannot merge. The checked-in
    tools/fixtures/failclosed_canary_bash.json holds exactly FOUR commands that are real rule
    hits for this guard (mailbox consume, mailbox send, the Telegram bot API, a ticket
    annotate). They are pinned by name; every other command must ALLOW, and every verdict
    must equal the pre-2d oracle's.
  * IMPORT FAILURE of _failclosed denies (exit 2), including a broken helper; a decoy helper
    on PYTHONPATH cannot win; a symlinked deploy resolves to the clone.
  * The real script end to end, with the marker set as the coordinator's wired command sets
    it: the rule deny is unchanged; ambiguity and exceptions deny; the 262,144-character cap
    applies to the Bash command only, so a 2 MB Write (content or file_path) is decided in
    far less than the budget.
"""

from __future__ import annotations

import io
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_TOOLS = Path(__file__).resolve().parent
_HOOKS = _TOOLS / "hooks"
_HOOK = _HOOKS / "continuity_candidate_guard.py"
_CORPUS = _TOOLS / "fixtures" / "failclosed_canary_bash.json"
_NAME = "continuity-candidate-guard"

sys.path.insert(0, str(_HOOKS))
import _failclosed as fc  # noqa: E402

from tools.hooks import continuity_candidate_guard as guard  # noqa: E402
# The pre-2d decide() for a Bash call, built from the VERBATIM old regexes (the oracle).
from tools.hooks.test_continuity_candidate_guard import _old_decide_bash  # noqa: E402

# The fixture commands this guard DENIES by rule, before and after the wrap. Pinned so a new
# deny in the canary shows up as a failure rather than slipping into an "expected" set.
TRUE_RULE_HITS = frozenset({
    "python3 tools/mailbox.py consume --recipient orchestrator",
    "curl -s https://api.telegram.org/bot$TOKEN/getMe | jq .ok",
    "python3 tools/ticket.py annotate T-4392 --note 'claude seat: mcp-list-guard wired from the clone'",
    "python3 tools/mailbox.py send --to orchestrator --subject 'claude -p question' --body-file /tmp/body.txt",
})

# Ordinary non-Bash calls a candidate makes: all must ALLOW.
ORDINARY_EDITS = (
    {"tool_name": "Edit", "tool_input": {"file_path": "/home/x/pantheon-config/tools/foo.py",
                                         "old_string": "a", "new_string": "b"}},
    {"tool_name": "Edit", "tool_input": {"file_path": "README.md", "old_string": "x",
                                         "new_string": "y", "replace_all": True}},
    {"tool_name": "Write", "tool_input": {"file_path": "/tmp/scratch/notes.txt", "content": "hi\n"}},
    {"tool_name": "Write", "tool_input": {"file_path": "tools/test_new.py", "content": "import os\n"}},
    {"tool_name": "Write", "tool_input": {"file_path": "docs/ledgerish/x.md", "content": ""}},
    {"tool_name": "MultiEdit", "tool_input": {"file_path": "tools/a.py",
                                              "edits": [{"old_string": "a", "new_string": "b"}]}},
    {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "analysis/n.ipynb",
                                                 "new_source": "print(1)"}},
    {"tool_name": "Edit", "tool_input": {"file_path": "tools/tickets_helper.py", "old_string": "a",
                                         "new_string": "b"}},
    {"tool_name": "Read", "tool_input": {"file_path": "tickets/active/T-1.md"}},
    {"tool_name": "Grep", "tool_input": {"pattern": "mailbox", "path": "mailbox/"}},
)


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _fixture() -> list[str]:
    return json.loads(_CORPUS.read_text(encoding="utf-8"))["commands"]


def _evaluate(payload: dict):
    with mock.patch.dict(os.environ, {guard.CANDIDATE_MARKER_ENV: "1"}), \
            mock.patch.object(fc, "elapsed_since_process_start", return_value=0.0):
        return fc.evaluate(_NAME, guard._decide_from_env, max_command_chars=guard.MAX_COMMAND_CHARS,
                           stdin=io.StringIO(json.dumps(payload)))


def _env(marker: bool = True) -> dict:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop(guard.CANDIDATE_MARKER_ENV, None)
    if marker:
        env[guard.CANDIDATE_MARKER_ENV] = "1"  # as the coordinator's wired command sets it
    return env


def _run_script(stdin_text: str, hook: Path = _HOOK, env: dict | None = None):
    return subprocess.run([sys.executable, str(hook)], input=stdin_text, capture_output=True,
                          text=True, env=_env() if env is None else env, timeout=60)


def _deny_reason(out: str) -> str:
    body = json.loads(out)["hookSpecificOutput"]
    assert body["permissionDecision"] == "deny", body
    return body["permissionDecisionReason"]


class CanaryAllAllow(unittest.TestCase):
    def test_corpus_is_substantial_and_holds_exactly_the_pinned_hits(self):
        fixture = _fixture()
        self.assertGreaterEqual(len(fixture), 100)
        hits = {c for c in fixture if _old_decide_bash(c) is not None}
        self.assertEqual(hits, TRUE_RULE_HITS)

    def test_fixture_through_the_wrap_matches_the_oracle(self):
        for command in _fixture():
            with self.subTest(command=command[:80]):
                code, out, err, _ = _evaluate(_bash(command))
                self.assertEqual((code, err), (0, ""))
                if command in TRUE_RULE_HITS:
                    self.assertEqual(json.loads(out), _old_decide_bash(command))
                else:
                    self.assertEqual(out, "")

    def test_ordinary_edits_all_allow_in_process(self):
        for payload in ORDINARY_EDITS:
            with self.subTest(payload=payload):
                self.assertEqual(_evaluate(payload)[:3], (0, "", ""))

    def test_real_script_sample_allows(self):
        fixture = [c for c in _fixture() if c not in TRUE_RULE_HITS]
        sample = [_bash(c) for c in fixture[:: max(1, len(fixture) // 20)]] + list(ORDINARY_EDITS)
        for payload in sample:
            with self.subTest(payload=str(payload)[:100]):
                r = _run_script(json.dumps(payload))
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_real_script_rule_denies_are_unchanged(self):
        for payload, what in (
                (_bash(sorted(TRUE_RULE_HITS)[0]), None),
                (_bash("git -C /x push origin main"), "a git push (external action)"),
                ({"tool_name": "Edit", "tool_input": {"file_path": "tickets/active/T-1.md"}},
                 "a canonical-record write")):
            with self.subTest(payload=payload):
                r = _run_script(json.dumps(payload))
                self.assertEqual((r.returncode, r.stderr), (0, ""))
                reason = _deny_reason(r.stdout)
                self.assertIn("BLOCKED (continuity-candidate-guard, T-3177)", reason)
                if what:
                    self.assertIn(what, reason)

    def test_empty_and_unknown_payloads_allow(self):
        for text in ("", json.dumps({}), json.dumps({"tool_name": "Read", "tool_input": {}}),
                     json.dumps({"tool_name": "Bash", "tool_input": {"command": ""}})):
            with self.subTest(text=text):
                r = _run_script(text)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


class AmbiguityAndExceptionsDeny(unittest.TestCase):
    def test_garbage_and_non_object_payloads_deny(self):
        for text in ("{not json", "[1, 2]", json.dumps({"tool_name": "Bash", "tool_input": "x"}),
                     json.dumps({"tool_name": "Bash", "tool_input": {"command": 7}})):
            with self.subTest(text=text):
                r = _run_script(text)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(r.stdout, "")
                self.assertIn(f"BLOCKED ({_NAME}, fail-closed, T-4392): ambiguity: ", r.stderr)

    def test_malformed_edit_payloads_now_deny(self):
        # New in 2d (was fail-open): decide() raises on these, and an exception denies.
        for payload in ({"tool_name": "Edit", "tool_input": {"file_path": 7}},
                        {"tool_name": "Write", "tool_input": {"file_path": ["tickets"]}},
                        {"tool_name": "Edit", "tool_input": ["x"]},
                        {"tool_name": "NotebookEdit", "tool_input": "x"}):
            with self.subTest(payload=payload):
                r = _run_script(json.dumps(payload))
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn(f"BLOCKED ({_NAME}, fail-closed, T-4392): exception: ", r.stderr)

    def test_exception_in_decide_denies(self):
        def boom(data):
            raise RuntimeError("synthetic")
        with mock.patch.object(fc, "elapsed_since_process_start", return_value=0.0):
            code, out, err, _ = fc.evaluate(_NAME, boom, stdin=io.StringIO(json.dumps(_bash("ls"))))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("exception: RuntimeError: synthetic", err)

    def test_no_marker_exits_before_the_runner(self):
        for text in ("{not json", json.dumps(_bash("a" * (guard.MAX_COMMAND_CHARS + 1)))):
            with self.subTest(text=text[:20]):
                r = _run_script(text, env=_env(marker=False))
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


class ImportFailureDenies(unittest.TestCase):
    def _copy_hook(self, td: Path) -> Path:
        dst = td / "continuity_candidate_guard.py"
        shutil.copyfile(_HOOK, dst)
        return dst

    def _run(self, hook: Path):
        return _run_script(json.dumps(_bash("ls")), hook=hook)

    def test_missing_helper_denies_with_recovery_hint(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._run(self._copy_hook(Path(td)))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn(f"BLOCKED ({_NAME}, fail-closed, T-4392): cannot import _failclosed", r.stderr)
        self.assertIn("The hook's directory lacks _failclosed.py: run the hook from the "
                      "pantheon-config clone, or unwire it via the Edit tool.", r.stderr)
        self.assertNotIn("failed to import", r.stderr)

    def test_broken_helper_denies(self):
        with tempfile.TemporaryDirectory() as td:
            hook = self._copy_hook(Path(td))
            (Path(td) / "_failclosed.py").write_text("raise RuntimeError('corrupt helper')\n")
            r = self._run(hook)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("corrupt helper", r.stderr)
        self.assertIn("the hook's _failclosed.py failed to import (RuntimeError): fix it, or "
                      "unwire the hook via the Edit tool.", r.stderr)
        self.assertNotIn("lacks _failclosed.py", r.stderr)

    def test_syntax_error_helper_is_broken_not_missing(self):
        with tempfile.TemporaryDirectory() as td:
            hook = self._copy_hook(Path(td))
            (Path(td) / "_failclosed.py").write_text("def (:\n")
            r = self._run(hook)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("failed to import (SyntaxError)", r.stderr)

    def test_helper_missing_its_own_dependency_is_broken_not_missing(self):
        with tempfile.TemporaryDirectory() as td:
            hook = self._copy_hook(Path(td))
            (Path(td) / "_failclosed.py").write_text("import no_such_module_t4392\n")
            r = self._run(hook)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("failed to import (ModuleNotFoundError)", r.stderr)

    def test_decoy_on_pythonpath_loses(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as decoy:
            hook = self._copy_hook(Path(td))
            shutil.copyfile(_HOOKS / "_failclosed.py", Path(td) / "_failclosed.py")
            (Path(decoy) / "_failclosed.py").write_text(
                "import sys\ndef run(*a, **k):\n    sys.exit(0)\n")
            env = dict(_env(), PYTHONPATH=decoy)
            r = _run_script(json.dumps(_bash("a" * (guard.MAX_COMMAND_CHARS + 1))), hook=hook, env=env)
        self.assertEqual(r.returncode, 2, r.stderr)  # the real helper's cap, not the decoy's allow
        self.assertIn(": cap: ", r.stderr)

    def test_symlinked_deploy_resolves_to_the_clone(self):
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / "continuity_candidate_guard.py"
            link.symlink_to(_HOOK)
            r = self._run(link)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


def _child_cpu_and_wall(fn):
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.monotonic()
    result = fn()
    wall = time.monotonic() - t0
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return result, (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime), wall


class CapAndBudget(unittest.TestCase):
    TWO_MB = 2 * 1024 * 1024

    def test_cap_constant(self):
        self.assertEqual(guard.MAX_COMMAND_CHARS, 262_144)
        self.assertEqual(fc.DEFAULT_MAX_COMMAND_CHARS, 65_536)  # the helper default is unchanged

    def test_over_cap_bash_denies(self):
        r = _run_script(json.dumps(_bash("a" * (guard.MAX_COMMAND_CHARS + 1))))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn(f"BLOCKED ({_NAME}, fail-closed, T-4392): cap: ", r.stderr)

    def test_at_cap_bash_allows(self):
        r = _run_script(json.dumps(_bash("a" * guard.MAX_COMMAND_CHARS)))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def _assert_in_budget(self, payload: dict, expect_deny: bool):
        """The real script on a large non-Bash payload: never the cap; allow (or the rule
        deny) well inside the budget in CPU; on a contended core only the deadline deny."""
        r, cpu, wall = _child_cpu_and_wall(lambda: _run_script(json.dumps(payload)))
        self.assertLess(cpu, 2.5, f"hook CPU {cpu:.2f}s")
        self.assertNotIn(": cap: ", r.stderr)
        if r.returncode == 0:
            self.assertEqual(r.stderr, "")
            if expect_deny:
                self.assertIn("canonical-record write", _deny_reason(r.stdout))
            else:
                self.assertEqual(r.stdout, "")
        else:
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn(f"BLOCKED ({_NAME}, fail-closed, T-4392): deadline: ", r.stderr)
            self.assertGreaterEqual(wall, fc.DEADLINE_S * 0.9)

    def test_two_mb_write_content_is_not_capped(self):
        content = ("git push; mailbox.py send; ticket.py close\n" * (self.TWO_MB // 43 + 1))[:self.TWO_MB]
        self._assert_in_budget({"tool_name": "Write",
                                "tool_input": {"file_path": "tools/big.txt", "content": content}}, False)
        self._assert_in_budget({"tool_name": "Write",
                                "tool_input": {"file_path": "tickets/active/T-1.md", "content": content}}, True)
        self._assert_in_budget({"tool_name": "MultiEdit", "tool_input": {
            "file_path": "tools/big.py", "edits": [{"old_string": content, "new_string": content}]}}, False)

    def test_two_mb_file_path_is_not_capped(self):
        path = "ledgerx/ticketsy/" * (self.TWO_MB // 17) + "file.txt"  # ~2 MB, no protected segment
        self._assert_in_budget({"tool_name": "Write", "tool_input": {"file_path": path, "content": ""}}, False)
        self._assert_in_budget({"tool_name": "Edit", "tool_input": {"file_path": path + "/adr/x.md"}}, True)

    def test_decide_on_two_mb_path_in_process_is_fast(self):
        path = "ledgerx/ticketsy/" * (self.TWO_MB // 17) + "file.txt"  # ~2 MB, no protected segment
        t0 = time.thread_time()
        verdict = guard.decide({"tool_name": "Write", "tool_input": {"file_path": path}},
                               candidate_active=True)
        took = time.thread_time() - t0
        self.assertIsNone(verdict)
        self.assertLess(took, 1.0, f"{took:.3f}s CPU")

    def test_real_script_worst_bash_shape_at_the_cap(self):
        cmd = "git " * (guard.MAX_COMMAND_CHARS // 4)
        r, cpu, wall = _child_cpu_and_wall(lambda: _run_script(json.dumps(_bash(cmd))))
        self.assertLess(cpu, 2.5, f"hook CPU {cpu:.2f}s at the cap")
        if r.returncode == 0:
            self.assertEqual((r.stdout, r.stderr), ("", ""))
        else:
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn(f"BLOCKED ({_NAME}, fail-closed, T-4392): deadline: ", r.stderr)
            self.assertGreaterEqual(wall, fc.DEADLINE_S * 0.9)


if __name__ == "__main__":
    unittest.main()
