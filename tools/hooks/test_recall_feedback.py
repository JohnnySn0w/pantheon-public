#!/usr/bin/env python3
"""Hermetic tests for the recall-feedback PreToolUse hook (T-1090)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HOOK = Path(__file__).resolve().parent / "recall-feedback.py"
_spec = importlib.util.spec_from_file_location("recall_feedback", _HOOK)
recall = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recall)


def _write_mem(d: Path, name: str, mem_type: str, description: str, body: str) -> None:
    (d / name).write_text(
        f"---\nname: {name}\ndescription: {description}\nmetadata:\n"
        f"  node_type: memory\n  type: {mem_type}\n---\n\n{body}\n",
        encoding="utf-8",
    )


class TestExtractTokens(unittest.TestCase):
    def test_bash_command_paths_and_ids(self) -> None:
        toks = recall.extract_tokens("Bash", {"command": "python3 tools/ticket.py resolve T-1088"})
        self.assertIn("T-1088", toks)
        self.assertIn("tools/ticket.py", toks)
        self.assertIn("ticket.py", toks)

    def test_edit_file_path(self) -> None:
        toks = recall.extract_tokens("Edit", {"file_path": "/repo/tools/watch_runner.py"})
        self.assertIn("watch_runner.py", toks)
        self.assertTrue(any("watch_runner.py" in t for t in toks))

    def test_org_id_extracted(self) -> None:
        # A mutating command (not a read like grep — those are skipped).
        toks = recall.extract_tokens("Bash", {"command": "git commit -m 'implements ORG-026'"})
        self.assertIn("ORG-026", toks)

    def test_readonly_command_skipped(self) -> None:
        # A pure read must not extract tokens (no about-to-mutate moment).
        self.assertEqual(recall.extract_tokens("Bash", {"command": "grep ORG-026 tickets/"}), set())
        self.assertEqual(recall.extract_tokens("Bash", {"command": "cat tools/ticket.py"}), set())
        # A mutating segment in the chain disables the skip.
        self.assertTrue(recall.extract_tokens("Bash", {"command": "cat x && rm tools/ticket.py"}))

    def test_generic_short_token_rejected(self) -> None:
        # `x.py` is too generic (len < 5) — must not become a match token.
        toks = recall.extract_tokens("Edit", {"file_path": "x.py"})
        self.assertNotIn("x.py", toks)

    def test_empty_surface_no_tokens(self) -> None:
        self.assertEqual(recall.extract_tokens("Bash", {"command": ""}), set())
        self.assertEqual(recall.extract_tokens("Edit", {}), set())


class TestCommandSignatures(unittest.TestCase):
    def test_two_and_three_word_signatures(self) -> None:
        sigs = recall._command_signatures("git stash push tools/foo.py")
        self.assertIn("git stash", sigs)
        self.assertIn("git stash push", sigs)

    def test_skips_wrappers_and_flags(self) -> None:
        sigs = recall._command_signatures("sudo systemctl --user restart x.service")
        # `sudo` wrapper skipped; `--user` flag ends the leading run.
        self.assertIn("systemctl", " ".join(sigs))
        self.assertNotIn("sudo systemctl", sigs)

    def test_chained_segments(self) -> None:
        sigs = recall._command_signatures("cd /x && tailscale serve status")
        self.assertIn("tailscale serve", sigs)
        self.assertIn("tailscale serve status", sigs)

    def test_bare_single_word_not_a_signature(self) -> None:
        # A lone command word yields no >=2-word signature.
        self.assertEqual(recall._command_signatures("ls"), set())

    def test_signatures_only_for_bash(self) -> None:
        # Edit/Write never carry a shell command -> no command signatures.
        toks = recall.extract_tokens("Edit", {"file_path": "git stash push.py"})
        self.assertNotIn("git stash", toks)

    # --- T-1133: quoted prose (commit messages) must not produce signatures ---
    def test_t1133_quoted_commit_prose_yields_no_generic_signature(self) -> None:
        # The exact repro: a commit message whose prose contains ';' + the
        # phrase "this is the supervised ...". Pre-fix this split into a prose
        # segment and emitted the generic signature "this is the", which matched
        # a memory description ending "This is the process." -> false-positive.
        sigs = recall._command_signatures(
            'git commit -m "land it; this is the supervised first fire"'
        )
        self.assertEqual(sigs, {"git commit"})
        self.assertNotIn("this is the", sigs)

    def test_t1133_real_signatures_survive_the_guards(self) -> None:
        # Quote-strip + stopword guard must not suppress legitimate signatures.
        self.assertEqual(
            recall._command_signatures("git stash push -m wip"),
            {"git stash", "git stash push"},
        )
        self.assertIn("go test", recall._command_signatures("go test ./..."))

    def test_t1133_all_stopwords_guard(self) -> None:
        self.assertTrue(recall._all_stopwords("this is the"))
        self.assertFalse(recall._all_stopwords("git stash"))

    # --- T-1138: shell control-flow openers are loop plumbing, not commands ---
    def test_t1138_control_flow_openers_yield_no_signature(self) -> None:
        # The exact repros: a loop var (m/t, non-stopword, often 1-char) cleared
        # the T-1133 stopword guard, so "for m"/"for t" substring-matched prose.
        self.assertEqual(
            recall._command_signatures(
                "for m in test_repo_registry test_ticket_lint; do x; done"
            ),
            set(),
        )
        self.assertEqual(
            recall._command_signatures("for t in T-1024 T-1007; do y; done"), set()
        )
        self.assertEqual(
            recall._command_signatures("while read line; do z; done"), set()
        )
        # opener after a wrapper (`time for ...`) is still dropped.
        self.assertEqual(
            recall._command_signatures("time for x in a b; do c; done"), set()
        )

    def test_t1138_real_command_after_do_then_survives(self) -> None:
        # The actual command lives in the do/then segment and must still mine.
        self.assertEqual(
            recall._command_signatures("if true; then echo hi; fi"), {"echo hi"}
        )

    def test_t1138_opener_as_arg_or_substring_not_dropped(self) -> None:
        # `for` as an ARG to a real command (not the segment opener) is fine.
        self.assertIn("grep for", recall._command_signatures("grep for pattern.txt"))
        # a token that merely CONTAINS an opener word is not an opener.
        self.assertIn(
            "for_each_file tool", recall._command_signatures("for_each_file tool")
        )
        # a legit 1-char command (e.g. R) survives — we drop opener-led segments,
        # NOT 1-char tokens (which would lose `R script.R`).
        self.assertIn("r script.r", recall._command_signatures("R script.R"))


class TestIsDistinctive(unittest.TestCase):
    def test_ids_and_paths_distinctive(self) -> None:
        self.assertTrue(recall._is_distinctive("T-1088"))
        self.assertTrue(recall._is_distinctive("ORG-026"))
        self.assertTrue(recall._is_distinctive("ticket.py"))
        self.assertTrue(recall._is_distinctive("tools/x.py"))
        self.assertTrue(recall._is_distinctive("settings.json"))

    def test_generic_rejected(self) -> None:
        self.assertFalse(recall._is_distinctive("x.py"))   # too short
        self.assertFalse(recall._is_distinctive("hello"))  # no sep/ext
        self.assertFalse(recall._is_distinctive("the"))


class TestMatchMemories(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        _write_mem(self.d, "feedback_ticket.md", "feedback",
                   "ticket.py footgun", "Use tools/ticket.py not hand-written Write.")
        _write_mem(self.d, "project_watch.md", "project",
                   "watch_runner.py is Specialist's actuation cron", "Some body text.")
        _write_mem(self.d, "reference_other.md", "reference",
                   "a reference about ticket.py", "ticket.py is referenced here too.")
        _write_mem(self.d, "feedback_unrelated.md", "feedback",
                   "unrelated", "Nothing about the touched file.")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_feedback_match(self) -> None:
        res = recall.match_memories({"ticket.py"}, self.d)
        names = [m[0] for m in res]
        self.assertIn("feedback_ticket", names)
        # reference type is excluded even though it contains the token.
        self.assertNotIn("reference_other", names)
        # unrelated feedback (no token) excluded.
        self.assertNotIn("feedback_unrelated", names)

    def test_project_type_included(self) -> None:
        res = recall.match_memories({"watch_runner.py"}, self.d)
        self.assertIn("project_watch", [m[0] for m in res])

    def test_id_ranks_above_path(self) -> None:
        _write_mem(self.d, "feedback_id.md", "feedback", "about T-1088", "T-1088 is special.")
        res = recall.match_memories({"T-1088", "ticket.py"}, self.d)
        # The id-matched memory should rank first (specificity).
        self.assertEqual(res[0][0], "feedback_id")

    def test_no_tokens_no_match(self) -> None:
        self.assertEqual(recall.match_memories(set(), self.d), [])


class TestEndToEndSubprocess(unittest.TestCase):
    """Drive the hook as a real subprocess (the actual stdin/stdout contract)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name) / "memory"
        self.d.mkdir()
        self._seen = tempfile.TemporaryDirectory()
        _write_mem(self.d, "feedback_ticket.md", "feedback",
                   "always file tickets via ticket.py", "Use tools/ticket.py, enforces frontmatter.")

    def tearDown(self) -> None:
        self._tmp.cleanup()
        self._seen.cleanup()

    def _run(self, payload: dict) -> tuple[int, str]:
        env = {"RECALL_MEMORY_DIR": str(self.d), "HOME": self._seen.name,
               "PATH": "/usr/bin:/bin"}
        # setup-python tool-cache interpreters need the loader path or
        # sys.executable exits 127 before the hook runs (self-hosted CI).
        if "LD_LIBRARY_PATH" in os.environ:
            env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
        p = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=json.dumps(payload), capture_output=True, text=True, env=env,
        )
        return p.returncode, p.stdout

    def test_injects_additional_context(self) -> None:
        rc, out = self._run({
            "tool_name": "Bash",
            "tool_input": {"command": "python3 tools/ticket.py file ..."},
            "session_id": "sess-1",
        })
        self.assertEqual(rc, 0)
        obj = json.loads(out)
        hso = obj["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "allow")
        self.assertIn("feedback_ticket", hso["additionalContext"])
        self.assertIn("verify it still applies", hso["additionalContext"])

    def test_non_mutating_tool_no_output(self) -> None:
        rc, out = self._run({"tool_name": "Read", "tool_input": {"file_path": "tools/ticket.py"}})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_no_match_no_output(self) -> None:
        rc, out = self._run({
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/totally/unrelated_xyz.md"},
            "session_id": "s",
        })
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_per_session_dedup(self) -> None:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "tools/ticket.py resolve"},
            "session_id": "dedup-sess",
        }
        rc1, out1 = self._run(payload)
        self.assertIn("feedback_ticket", out1)
        # Second identical call in the same session must NOT re-surface it.
        rc2, out2 = self._run(payload)
        self.assertEqual(out2.strip(), "")

    def test_malformed_input_fails_open(self) -> None:
        env = {"RECALL_MEMORY_DIR": str(self.d), "HOME": self._seen.name,
               "PATH": "/usr/bin:/bin"}
        # setup-python tool-cache interpreters need the loader path or
        # sys.executable exits 127 before the hook runs (self-hosted CI).
        if "LD_LIBRARY_PATH" in os.environ:
            env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
        p = subprocess.run(
            [sys.executable, str(_HOOK)],
            input="{not json", capture_output=True, text=True, env=env,
        )
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")


class TestPrecisionCap(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        for i in range(5):
            _write_mem(self.d, f"feedback_{i}.md", "feedback",
                       f"footgun {i} about settings.json", "Careful with settings.json here.")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cap_applied_in_main_flow(self) -> None:
        # match_memories returns all 5; the MAX_SURFACED cap is applied in main.
        res = recall.match_memories({"settings.json"}, self.d)
        self.assertEqual(len(res), 5)
        self.assertEqual(recall.MAX_SURFACED, 2)


if __name__ == "__main__":
    unittest.main()
