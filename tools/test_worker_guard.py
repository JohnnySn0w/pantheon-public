"""Tests for the worker-guard PreToolUse hook (T-1345 + T-1336-incident hardening).

The guard (tools/hooks/worker-guard.py) blocks drift-dangerous Bash commands for
SUBAGENTS while never restricting the head orchestrator (agent_type absent).

REGRESSION ANCHOR (2026-06-27 T-1336 incident): a NAMED implementer instance
(agent_type = "t1336-impl2", the instance name — NOT the persona name "implementer")
self-pushed + self-merged an unreviewed change to main. The original guard gated the
push/PR/merge block on `agent_type in {"implementer","reviewer"}`, which an instance-named
agent_type slips past. The fix: push/PR/merge is HO-exclusive — blocked for ANY subagent
(gate on agent_type PRESENCE, not value). `test_named_instance_*` lock this in.

Run: python3 -m unittest tools.test_worker_guard -v
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_GUARD_PATH = Path(__file__).resolve().parent / "hooks" / "worker-guard.py"
_spec = importlib.util.spec_from_file_location("worker_guard", _GUARD_PATH)
worker_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker_guard)


def _bash(command: str, agent_type=None) -> dict:
    d = {"tool_name": "Bash", "tool_input": {"command": command}}
    if agent_type is not None:
        d["agent_type"] = agent_type
    return d


def _blocked(command: str, agent_type=None) -> bool:
    return worker_guard.decide(_bash(command, agent_type)) is not None


def _edit(path: str, tool: str = "Edit", agent_type=None) -> dict:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    d = {"tool_name": tool, "tool_input": {key: path}}
    if agent_type is not None:
        d["agent_type"] = agent_type
    return d


def _edit_blocked(path: str, tool: str = "Edit", agent_type=None) -> bool:
    return worker_guard.decide(_edit(path, tool, agent_type)) is not None


class TestHeadOrchestratorExempt(unittest.TestCase):
    """agent_type absent → head orchestrator → never restricted."""

    def test_ho_may_push(self):
        self.assertFalse(_blocked("git push origin main"))

    def test_ho_may_merge_pr(self):
        self.assertFalse(_blocked("gh pr merge 155 --admin --squash"))

    def test_ho_may_read_token(self):
        self.assertFalse(_blocked("cat ~/.claude/channels/telegram/.env"))

    def test_ho_may_write_ledger(self):
        self.assertFalse(_blocked("echo x >> pantheon-config/ledger/2026-W26/2026-W26.jsonl"))


class TestPushIsHoExclusive(unittest.TestCase):
    """Push/PR/merge blocked for EVERY subagent regardless of agent_type value."""

    def test_implementer_push_blocked(self):
        self.assertTrue(_blocked("git push origin agent/T-1336", agent_type="implementer"))

    def test_reviewer_push_blocked(self):
        self.assertTrue(_blocked("git push", agent_type="reviewer"))

    def test_project_orchestrator_push_blocked(self):
        # HO-exclusive: even the orchestrator now reports up; HO pushes.
        self.assertTrue(_blocked("git push origin agent/T-1336", agent_type="project-orchestrator"))

    def test_named_instance_push_blocked(self):
        # THE regression: an instance-named agent_type must NOT slip the push block.
        self.assertTrue(_blocked("git push origin agent/T-1336-namespace", agent_type="t1336-impl2"))

    def test_named_instance_pr_create_blocked(self):
        self.assertTrue(_blocked("gh pr create --fill", agent_type="t1336-impl2"))

    def test_named_instance_pr_merge_admin_blocked(self):
        # The exact command from the incident.
        self.assertTrue(_blocked("gh pr merge 155 --admin --squash --delete-branch", agent_type="t1336-impl2"))

    def test_unknown_future_agent_type_push_blocked(self):
        self.assertTrue(_blocked("git push", agent_type="some-future-persona"))


class TestAlwaysBlockedForSubagents(unittest.TestCase):
    """Token read / Bot API / HO-records blocked for any subagent."""

    def test_token_read_blocked(self):
        self.assertTrue(_blocked("cat /home/claude/.claude/channels/telegram/.env", agent_type="t1336-impl2"))

    def test_bot_api_blocked(self):
        self.assertTrue(_blocked("curl https://api.telegram.org/bot123/sendMessage", agent_type="implementer"))

    def test_ledger_write_blocked(self):
        self.assertTrue(_blocked("git -C ~/pantheon-config add ledger/2026-W26/2026-W26.jsonl", agent_type="t1336-impl2"))

    def test_adr_write_blocked(self):
        self.assertTrue(_blocked("vim pantheon-config/adr/ORG-040.md", agent_type="reviewer"))


class TestReadOnlyProtectedPathMentionAllowed(unittest.TestCase):
    """T-1600 misfire #1 regression: a read-only command that only MENTIONS a
    protected path (no write) must pass. Shape-vs-substance: the guard now
    matches the command's real operand surface, not any substring anywhere in
    the raw string."""

    def test_grep_ledger_path_readonly_allowed(self):
        self.assertFalse(_blocked(
            "grep -l 'entries' pantheon-config/ledger/2026-W27/2026-W27.jsonl",
            agent_type="reviewer",
        ))

    def test_cat_ledger_path_readonly_allowed(self):
        self.assertFalse(_blocked(
            "cat pantheon-config/ledger/2026-W27/2026-W27.jsonl",
            agent_type="implementer",
        ))

    def test_git_log_on_protected_path_allowed(self):
        self.assertFalse(_blocked(
            "git log --oneline -- pantheon-config/ledger/2026-W27/",
            agent_type="t1336-impl2",
        ))

    def test_git_show_on_protected_path_allowed(self):
        self.assertFalse(_blocked(
            "git show HEAD:pantheon-config/adr/ORG-040.md",
            agent_type="implementer",
        ))

    def test_git_diff_on_protected_path_allowed(self):
        self.assertFalse(_blocked(
            "git diff -- pantheon-config/ledger/2026-W27/2026-W27.jsonl",
            agent_type="reviewer",
        ))

    def test_git_blame_on_protected_path_allowed(self):
        self.assertFalse(_blocked(
            "git blame pantheon-config/adr/ORG-040.md",
            agent_type="implementer",
        ))

    def test_find_without_delete_or_exec_allowed(self):
        self.assertFalse(_blocked(
            "find pantheon-config/ledger/2026-W27 -name '*.jsonl'",
            agent_type="implementer",
        ))


class TestGitCommitMessageNotMatchSurface(unittest.TestCase):
    """T-1600 misfire #2 regression: a clean-scope `git commit` whose MESSAGE
    text mentions a protected path must pass — the -m/--message value is not a
    file operand."""

    def test_commit_message_mentioning_ledger_path_allowed(self):
        self.assertFalse(_blocked(
            'git commit -m "docs: policy_outflow_sync digest update '
            '(ref pantheon-config/ledger/2026-W27 conventions)"',
            agent_type="project-orchestrator",
        ))

    def test_commit_dash_c_message_mentioning_adr_path_allowed(self):
        self.assertFalse(_blocked(
            'git -C /tmp/pantheon-config-T-1589 commit -m '
            '"sync: notes reference pantheon-config/adr/ORG-040.md context"',
            agent_type="implementer",
        ))

    def test_commit_dash_am_message_mentioning_ledger_path_allowed(self):
        # Nice-to-have: `-am` (combined -a + -m shorthand) wasn't recognized by
        # _drop_commit_message, so its message text still over-blocked.
        self.assertFalse(_blocked(
            'git commit -am "docs: ref pantheon-config/ledger/2026-W27 conventions"',
            agent_type="implementer",
        ))

    def test_commit_getting_protected_content_staged_still_blocked(self):
        # Chain-coverage: stripping -m text from the match surface does not
        # reopen a write path, because getting protected content STAGED still
        # requires `git add <protected-path>` — which is blocked below (`add`
        # is not a read-only verb, so the rejoined-token surface matches).
        # This test pins that upstream link explicitly.
        self.assertTrue(_blocked(
            "git add pantheon-config/ledger/2026-W27/2026-W27.jsonl",
            agent_type="implementer",
        ))


class TestQuoteSplitEvasionStillCaught(unittest.TestCase):
    """The NAMED off-limits evasion class (quote-splitting the guarded word to
    dodge the regex) must stay caught. Reconstructing the match surface via
    shlex-tokenize-then-rejoin defeats this — POSIX word-splitting joins
    adjacent quoted/unquoted fragments back into one token."""

    def test_double_quote_split_word_still_blocked(self):
        self.assertTrue(_blocked(
            'git add l"edger"/2026-W27/x.jsonl',
            agent_type="implementer",
        ))

    def test_empty_single_quote_split_word_still_blocked(self):
        self.assertTrue(_blocked(
            "git -C ~/pantheon-config add led''ger/2026-W27/x.jsonl",
            agent_type="t1336-impl2",
        ))


class TestHoRecordsWriteBlockingNotWeakened(unittest.TestCase):
    """No weakening of actual protected-path WRITE blocking (req #5)."""

    def test_sed_inplace_on_protected_path_blocked(self):
        self.assertTrue(_blocked(
            "sed -i 's/x/y/' pantheon-config/ledger/2026-W27/2026-W27.jsonl",
            agent_type="implementer",
        ))

    def test_tee_into_protected_path_blocked(self):
        self.assertTrue(_blocked(
            "echo x | tee pantheon-config/ledger/2026-W27/2026-W27.jsonl",
            agent_type="implementer",
        ))

    def test_redirect_into_protected_path_blocked(self):
        self.assertTrue(_blocked(
            "echo x > pantheon-config/ledger/2026-W27/2026-W27.jsonl",
            agent_type="implementer",
        ))

    def test_rm_protected_path_blocked(self):
        self.assertTrue(_blocked(
            "rm pantheon-config/ledger/2026-W27/2026-W27.jsonl",
            agent_type="implementer",
        ))

    def test_mv_into_protected_notes_path_blocked(self):
        self.assertTrue(_blocked(
            "mv draft.md pantheon-config/notes/x.md",
            agent_type="implementer",
        ))

    def test_cp_into_protected_path_blocked(self):
        self.assertTrue(_blocked(
            "cp file.txt pantheon-config/ledger/2026-W27/target.jsonl",
            agent_type="implementer",
        ))

    def test_find_with_delete_on_protected_path_blocked(self):
        self.assertTrue(_blocked(
            "find pantheon-config/ledger/2026-W27 -delete",
            agent_type="implementer",
        ))

    def test_find_with_exec_on_protected_path_blocked(self):
        self.assertTrue(_blocked(
            r"find pantheon-config/ledger/2026-W27 -exec rm {} \;",
            agent_type="implementer",
        ))

    def test_compound_readonly_looking_command_still_blocked(self):
        # Ambiguous compound structure -> fail-closed (conservative fallback),
        # even though the second half looks read-only in isolation.
        self.assertTrue(_blocked(
            "git status && cat pantheon-config/ledger/2026-W27/2026-W27.jsonl",
            agent_type="implementer",
        ))

    def test_find_fprintf_writing_protected_path_blocked(self):
        # Round-1 reviewer HOLE A: -fprintf writes formatted output to a FILE
        # argument — a read-only-LOOKING `find` invocation that actually writes.
        self.assertTrue(_blocked(
            "find /tmp -name '*' -fprintf pantheon-config/ledger/2026-W27/out.txt '%p\\n'",
            agent_type="implementer",
        ))

    def test_find_fprint_writing_protected_path_blocked(self):
        self.assertTrue(_blocked(
            "find /tmp -name '*' -fprint pantheon-config/ledger/2026-W27/out.txt",
            agent_type="implementer",
        ))

    def test_find_fprint0_writing_protected_path_blocked(self):
        self.assertTrue(_blocked(
            "find /tmp -name '*' -fprint0 pantheon-config/ledger/2026-W27/out.txt",
            agent_type="implementer",
        ))

    def test_find_fls_writing_protected_path_blocked(self):
        self.assertTrue(_blocked(
            "find /tmp -name '*' -fls pantheon-config/ledger/2026-W27/out.txt",
            agent_type="implementer",
        ))

    def test_git_diff_output_writing_protected_path_blocked(self):
        # Round-1 reviewer HOLE B: --output=<file> writes real content with no
        # shell redirect char involved.
        self.assertTrue(_blocked(
            "git diff --output=pantheon-config/ledger/2026-W27/x.jsonl",
            agent_type="implementer",
        ))

    def test_git_log_output_space_form_writing_protected_path_blocked(self):
        self.assertTrue(_blocked(
            "git log --output pantheon-config/ledger/2026-W27/x.jsonl",
            agent_type="implementer",
        ))

    def test_git_show_output_writing_protected_path_blocked(self):
        self.assertTrue(_blocked(
            "git show --output=pantheon-config/adr/ORG-040.md HEAD",
            agent_type="implementer",
        ))

    def test_unparseable_quote_fallback_blocked(self):
        # Unbalanced quoting -> _tokens_or_none returns None -> conservative
        # raw-string fallback (pre-T-1600 behavior): still blocks on mention.
        self.assertTrue(_blocked(
            'git commit -m "reference pantheon-config/ledger/2026-W27 unterminated',
            agent_type="implementer",
        ))


class TestNormalWorkAllowed(unittest.TestCase):
    """Default-allow: ordinary worker commands pass."""

    def test_cargo_test_allowed(self):
        self.assertFalse(_blocked("cargo test --workspace", agent_type="t1336-impl2"))

    def test_git_commit_allowed(self):
        self.assertFalse(_blocked("git commit -m 'feat: x'", agent_type="implementer"))

    def test_git_status_allowed(self):
        self.assertFalse(_blocked("git status", agent_type="reviewer"))

    def test_git_add_non_ho_path_allowed(self):
        self.assertFalse(_blocked("git add crates/echo-core/src/lib.rs", agent_type="t1336-impl2"))

    def test_edit_with_no_path_ignored(self):
        # Edit/Write fire the guard now, but with no path there is nothing to protect.
        self.assertIsNone(worker_guard.decide({"tool_name": "Edit", "agent_type": "implementer"}))

    def test_unknown_tool_ignored(self):
        self.assertIsNone(worker_guard.decide({"tool_name": "Grep", "agent_type": "implementer"}))


class TestCanonicalDocEditBlocked(unittest.TestCase):
    """T-1381 / ORG-A2: a subagent must not mutate canonical docs via Edit/Write.

    The Bash guard alone left the Edit/Write tool surface open — these lock the
    structural D2 protection across rulebook / ADR / ledger / notes / bugs /
    incidents / lessons / sop / README / BACKLOG / goals.
    """

    def test_edit_rulebook_blocked(self):
        self.assertTrue(_edit_blocked("/home/claude/pantheon-config/rulebook/directives.md", agent_type="implementer"))

    def test_write_adr_blocked(self):
        self.assertTrue(_edit_blocked("/home/claude/pantheon-config/adr/ORG-040.md", tool="Write", agent_type="t1336-impl2"))

    def test_edit_ledger_blocked(self):
        self.assertTrue(_edit_blocked("/home/claude/pantheon-config/ledger/2026-W27/2026-W27.jsonl", agent_type="reviewer"))

    def test_edit_backlog_blocked(self):
        self.assertTrue(_edit_blocked("/home/claude/pantheon-config/BACKLOG.md", agent_type="implementer"))

    def test_edit_goals_blocked(self):
        self.assertTrue(_edit_blocked("/home/claude/pantheon-config/goals.md", agent_type="implementer"))

    def test_edit_readme_spine_blocked(self):
        self.assertTrue(_edit_blocked("/home/claude/pantheon-config/sop/standard/README.md", agent_type="implementer"))

    def test_edit_bugs_incidents_lessons_blocked(self):
        for seg in ("bugs", "incidents", "lessons"):
            self.assertTrue(
                _edit_blocked(f"/home/claude/pantheon-config/{seg}/x.md", agent_type="implementer"),
                f"{seg}/ must be protected",
            )

    def test_notebook_edit_protected_path_blocked(self):
        self.assertTrue(_edit_blocked("/home/claude/pantheon-config/notes/x.ipynb", tool="NotebookEdit", agent_type="implementer"))

    def test_multiedit_protected_path_blocked(self):
        self.assertTrue(_edit_blocked("/home/claude/pantheon-config/rulebook/principles.md", tool="MultiEdit", agent_type="implementer"))

    def test_worktree_path_blocked(self):
        # Workers operate in /tmp/pantheon-config-<ticket>/ worktrees — must still catch.
        self.assertTrue(_edit_blocked("/tmp/pantheon-config-T-1381/rulebook/principles.md", agent_type="implementer"))

    def test_ho_may_edit_canonical_doc(self):
        # agent_type absent → HO → never restricted, even on a protected doc.
        self.assertFalse(_edit_blocked("/home/claude/pantheon-config/adr/ORG-040.md"))


class TestWorkerCodeEditsAllowed(unittest.TestCase):
    """No false-positives: writing code + sibling-repo docs stays ALLOWED."""

    def test_edit_tool_code_allowed(self):
        self.assertFalse(_edit_blocked("/home/claude/pantheon-config/tools/artifact_gc.py", agent_type="implementer"))

    def test_edit_crate_code_allowed(self):
        self.assertFalse(_edit_blocked("/tmp/echo-T-1373/crates/echo-core/src/lib.rs", agent_type="implementer"))

    def test_edit_sibling_repo_readme_allowed(self):
        # An echo implementer editing echo's README must NOT be blocked by the pantheon-config guard.
        self.assertFalse(_edit_blocked("/home/claude/echo-prismatic/README.md", agent_type="implementer"))

    def test_edit_sibling_repo_bugs_allowed(self):
        self.assertFalse(_edit_blocked("/home/claude/echo-prismatic/bugs/x.md", agent_type="implementer"))

    def test_edit_screenshots_evidence_allowed(self):
        self.assertFalse(_edit_blocked("/home/claude/pantheon-config/screenshots/T-1381/note.txt", agent_type="implementer"))


if __name__ == "__main__":
    unittest.main()
