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


class TestLedgerBareTokenAnchored(unittest.TestCase):
    """T-2655: the bare `ledger/<year>` alternative in _HO_RECORDS is anchored to
    require the token be the START of a path (start-of-string / whitespace /
    quote), not merely present anywhere in the command string. An unrelated
    absolute /tmp scratch path that nests a `ledger/<year>` segment partway
    through must not false-positive (the T-2650 implementer's hook-wet-run
    case); a real cwd-relative pantheon-config write (bare `ledger/<year>`
    preceded by whitespace, e.g. after `git add `) must still block, and an
    explicit `pantheon-config-<ticket>/ledger/...` worktree operand must still
    block via the (now worktree-suffix-aware) explicit alternative.
    """

    def test_tmp_scratch_nested_ledger_path_redirect_allowed(self):
        self.assertFalse(_blocked(
            "echo x > /tmp/scratch/ledger/2026-W30/x.jsonl",
            agent_type="implementer",
        ))

    def test_tmp_scratch_nested_ledger_path_cp_allowed(self):
        self.assertFalse(_blocked(
            "cp file.txt /tmp/scratch/ledger/2026-W30/x.jsonl",
            agent_type="implementer",
        ))

    def test_worktree_ledger_write_still_blocked(self):
        self.assertTrue(_blocked(
            "echo x > pantheon-config-T-9999/ledger/2026-W28/x.jsonl",
            agent_type="implementer",
        ))

    def test_bare_relative_ledger_add_still_blocked(self):
        self.assertTrue(_blocked(
            "git add ledger/2026-W26/2026-W26.jsonl",
            agent_type="implementer",
        ))


class TestPushMessageTextNotMatchSurface(unittest.TestCase):
    """T-2716: a git commit whose MESSAGE merely mentions push/PR/merge text
    must not be treated as an actual push/PR/merge attempt — the -m/--message
    value is not a command being run, mirroring the T-1600 discipline already
    applied to _ho_records_match."""

    def test_commit_message_mentioning_git_push_allowed(self):
        # No `;` in the message text: a literal semicolon inside the quoted
        # -m value trips `_COMPOUND_RE`'s raw-string scan (it runs before
        # shlex, so it can't tell the `;` is inside quotes) and falls to the
        # conservative raw-string fallback — that's the same pre-existing,
        # intentional fail-closed behavior `_ho_records_match` already has for
        # compound-looking shapes, not something this fix changes or is
        # required to handle.
        self.assertFalse(_blocked(
            'git commit -m "feat: add --no-verify-push flag, so echo-merge '
            'no longer runs git push after merge"',
            agent_type="implementer",
        ))

    def test_commit_message_mentioning_gh_pr_merge_allowed(self):
        self.assertFalse(_blocked(
            'git commit -m "docs: describe gh pr merge --admin flow"',
            agent_type="t2714-impl",
        ))

    def test_commit_dash_c_message_mentioning_push_allowed(self):
        self.assertFalse(_blocked(
            'git -C /tmp/pantheon-config-T-2714 commit -m '
            '"note: gh pr create is HO-exclusive per this change"',
            agent_type="implementer",
        ))

    def test_real_push_still_blocked(self):
        self.assertTrue(_blocked("git push origin agent/T-2714", agent_type="implementer"))

    def test_real_pr_create_still_blocked(self):
        self.assertTrue(_blocked("gh pr create --fill", agent_type="implementer"))

    def test_real_pr_merge_still_blocked(self):
        self.assertTrue(_blocked("gh pr merge 200 --admin --squash", agent_type="implementer"))

    def test_compound_commit_message_then_real_push_still_blocked(self):
        # Ambiguous compound structure -> fail-closed (conservative fallback),
        # even though the push half is a separate real invocation.
        self.assertTrue(_blocked(
            'git commit -m "mentions git push" && git push',
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


class TestSelfDeclaredD2KnowledgeFileBlocked(unittest.TestCase):
    """T-3089: knowledge/watch/topics.md self-declares 'Workers do not edit this
    file (D2)' in its own header, but the structural guard silently permitted
    worker Edit/Write anyway (T-3039 reviewer finding). knowledge/ is otherwise
    legitimately worker-writable (research reports, watch-dispatch artifacts) so
    this is an exact-path exception, not a blanket knowledge/ block — the sibling
    class below locks that non-coverage in.
    """

    def test_edit_watch_topics_blocked(self):
        self.assertTrue(
            _edit_blocked("/home/claude/pantheon-config/knowledge/watch/topics.md", agent_type="implementer")
        )

    def test_write_watch_topics_blocked(self):
        self.assertTrue(
            _edit_blocked(
                "/home/claude/pantheon-config/knowledge/watch/topics.md", tool="Write", agent_type="researcher"
            )
        )

    def test_worktree_watch_topics_blocked(self):
        self.assertTrue(
            _edit_blocked("/tmp/pantheon-config-T-3089/knowledge/watch/topics.md", agent_type="implementer")
        )

    def test_ho_may_edit_watch_topics(self):
        # agent_type absent → HO → never restricted, even on a self-declared-D2 file.
        self.assertFalse(_edit_blocked("/home/claude/pantheon-config/knowledge/watch/topics.md"))


class TestOtherKnowledgeFilesRemainWorkerWritable(unittest.TestCase):
    """Guard against over-broadening: knowledge/ is legitimately worker-writable
    (research reports, watch-dispatch artifacts) except for the one exact-path
    exception above. These must stay ALLOWED for a subagent.
    """

    def test_edit_watch_report_allowed(self):
        self.assertFalse(
            _edit_blocked(
                "/home/claude/pantheon-config/knowledge/watch/reports/2026-08-23/topic.md",
                agent_type="researcher",
            )
        )

    def test_edit_episodic_notes_allowed(self):
        self.assertFalse(
            _edit_blocked(
                "/home/claude/pantheon-config/knowledge/episodic/2026-08-23-slug/notes.md",
                agent_type="implementer",
            )
        )

    def test_write_episodic_readme_still_protected_by_readme_rule(self):
        # Not a knowledge/-specific carve-out: README.md stays blocked everywhere,
        # including under knowledge/ — via the pre-existing README rule, not the
        # new exact-path exception.
        self.assertTrue(
            _edit_blocked(
                "/home/claude/pantheon-config/knowledge/episodic/README.md", tool="Write", agent_type="implementer"
            )
        )

    def test_edit_watch_readme_still_protected_by_readme_rule(self):
        self.assertTrue(
            _edit_blocked("/home/claude/pantheon-config/knowledge/watch/README.md", agent_type="implementer")
        )


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


class TestGitIdentityGuard(unittest.TestCase):
    """T-1687: workers never touch git identity (after the 6-day --global
    misattribution incident, bugs/subagent-git-config-global-identity-pollution.md)."""

    @staticmethod
    def _bash_cwd(command, agent_type=None, cwd=""):
        d = {"tool_name": "Bash", "tool_input": {"command": command}}
        if agent_type is not None:
            d["agent_type"] = agent_type
        if cwd:
            d["cwd"] = cwd
        return d

    def _blocked_cwd(self, command, agent_type=None, cwd=""):
        return worker_guard.decide(self._bash_cwd(command, agent_type, cwd)) is not None

    # --- the incident command shape ---
    def test_global_identity_write_blocked_for_subagent(self):
        self.assertTrue(self._blocked_cwd(
            'git config --global user.name reviewer', agent_type="reviewer"))

    def test_global_any_write_blocked_for_subagent(self):
        self.assertTrue(self._blocked_cwd(
            'git config --global core.editor vim', agent_type="implementer"))

    def test_system_config_blocked_for_subagent(self):
        self.assertTrue(self._blocked_cwd(
            'git config --system user.email x@y', agent_type="t1336-impl2"))

    def test_ho_exempt_from_global_config(self):
        self.assertFalse(self._blocked_cwd('git config --global user.name orchestrator'))

    # --- shared-checkout identity (non-global) ---
    def test_identity_in_shared_checkout_cwd_blocked(self):
        self.assertTrue(self._blocked_cwd(
            'git config user.email r@l', agent_type="reviewer",
            cwd="/home/claude/pantheon-config"))

    def test_identity_via_dash_C_shared_checkout_blocked(self):
        self.assertTrue(self._blocked_cwd(
            'git -C /home/claude/echo-prismatic config user.name w',
            agent_type="implementer", cwd="/tmp/echo-T-9999"))

    def test_worktree_local_identity_allowed(self):
        self.assertFalse(self._blocked_cwd(
            'git config user.name impl', agent_type="implementer",
            cwd="/tmp/pantheon-config-T-9999"))

    # --- non-identity, non-global config + read-only mentions stay allowed ---
    def test_plain_config_read_allowed_in_shared_checkout(self):
        self.assertFalse(self._blocked_cwd(
            'git config --get remote.origin.url', agent_type="reviewer",
            cwd="/home/claude/pantheon-config"))

    def test_grep_mentioning_global_config_allowed(self):
        self.assertFalse(self._blocked_cwd(
            'grep -rn "git config --global" tools/', agent_type="reviewer",
            cwd="/home/claude/pantheon-config"))

    # --- T-2323: the denial must carry a route that actually works ---
    #
    # This guard's cwd test reads the SESSION's directory, not the command's
    # effective target, so it fires even on a /tmp-targeted command when the
    # worker's session sits in a shared checkout. That over-block is deliberate
    # (parsing a leading `cd` is defeatable, and a false negative re-opens
    # T-1687), which makes the DENIAL TEXT load-bearing: it is the only thing
    # standing between a blocked worker and a dead end. Two workers hit this on
    # 2026-07-29 doing Amendment H falsification in throwaway repos and both had
    # to rediscover the alternative on their own.
    #
    # Asserted rather than argued, because the value of that fix is entirely in
    # message CONTENT and nothing else would notice if an edit dropped it.
    def test_identity_denial_names_a_route_that_actually_works(self):
        reason = worker_guard._git_identity_match(
            "git config user.email r@l", "/home/claude/pantheon-config")
        self.assertIsNotNone(reason, "precondition: this shape must still block")
        for token in ("git -c user.email", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
            self.assertIn(token, reason,
                          f"denial must name {token} — the worker's way through")

    def test_identity_denial_does_not_promise_an_unreachable_tmp_exemption(self):
        # The pre-T-2323 text said "worktree-local identity under /tmp is allowed",
        # which is FALSE for the worker most likely to read it: one whose session
        # cwd is a shared checkout. Promising an exemption that does not apply is
        # worse than promising nothing.
        reason = worker_guard._git_identity_match(
            "git config user.email r@l", "/home/claude/pantheon-config")
        self.assertNotIn("under /tmp is allowed", reason)

    def test_named_alternatives_are_genuinely_unblocked(self):
        # The routes the message recommends must pass the guard from the SAME cwd
        # that triggered the denial — otherwise the advice sends the worker into a
        # second wall. This is the assertion that makes the message trustworthy.
        shared = "/home/claude/pantheon-config"
        self.assertIsNone(worker_guard._git_identity_match(
            "git -c user.email=w@example.com -c user.name=w commit -m x", shared))
        self.assertIsNone(worker_guard._git_identity_match(
            "GIT_AUTHOR_EMAIL=w@example.com GIT_COMMITTER_EMAIL=w@example.com "
            "git commit -m x", shared))


if __name__ == "__main__":
    unittest.main()
