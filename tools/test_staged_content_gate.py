"""T-4402: writer-side gate for the two content-coupled nightly invariants.

The nightly CI run read live repo content in two tests (the ticket project_id
justification backfill, and the watch-topic TOPIC_POLICY registration) and sat
red for 20 days because nothing failed at the WRITE. `tools/staged_content_gate.py`
runs the same predicates against the STAGED blobs at pre-commit time; the
`.githooks/pre-commit` adapter is exercised at the bottom, including against a
real git repo so the HEAD / rename lookups are proven on real git output.

Blocking rule under test: only a violation INTRODUCED by the commit blocks. A
pre-existing red (already in HEAD, or in the rename source of a moved ticket)
must never lock unrelated ticket operations (bubble item 4: no legitimate write
path may be blocked).
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import staged_content_gate as gate
from tools import watch_runner

REPO_ROOT = Path(__file__).resolve().parents[1]
PRE_COMMIT_HOOK = REPO_ROOT / ".githooks" / "pre-commit"

INBOX = "tickets/inbox/T-9101.md"
ACTIVE = "tickets/active/T-9101.md"
DONE = "tickets/done/T-9101.md"


def _ticket(tid="T-9101", *, project_id="biometrics", labels=("hardware",),
            nearest=None, state="inbox", note="") -> str:
    lines = ["---", f"id: {tid}", f"state: {state}"]
    lines.append(f"project_id: {project_id}" if project_id else "project_id: null")
    if nearest:
        lines.append(f"nearest_intent_ref: {nearest}")
    lines.append(f"labels: [{', '.join(labels)}]")
    lines += ["serves: keep the gate honest", "---", "", f"# {tid}", note]
    return "\n".join(lines) + "\n"


class _Repo:
    """Fake staged/HEAD content for the gate's injected readers."""

    def __init__(self, staged=None, head=None, renames=None):
        self.staged = {k: v.encode() for k, v in (staged or {}).items()}
        self.head = {k: v.encode() for k, v in (head or {}).items()}
        self.renames = dict(renames or {})
        self.calls: list[tuple[str, str]] = []
        self.warnings: list[str] = []

    def read_staged(self, path):
        self.calls.append(("staged", path))
        return self.staged.get(path)

    def read_head(self, path):
        self.calls.append(("head", path))
        return self.head.get(path)

    def rename_source(self, path):
        self.calls.append(("rename", path))
        return self.renames.get(path)


def _tickets_root(td: str, tickets: dict[str, str] | None = None) -> Path:
    root = Path(td) / "tickets"
    for state in ("inbox", "active", "done"):
        (root / state).mkdir(parents=True, exist_ok=True)
    for rel, text in (tickets or {}).items():
        (root / rel).write_text(text, encoding="utf-8")
    return root


class TicketPurposeGateTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)

    def _run(self, repo: _Repo, staged, tickets=None):
        root = _tickets_root(self._td.name, tickets)
        return gate.ticket_purpose_findings(
            staged, read_staged=repo.read_staged, read_head=repo.read_head,
            rename_source=repo.rename_source, tickets_root=root,
            warn=repo.warnings.append)

    def test_new_ticket_with_unjustified_project_id_is_blocked(self):
        repo = _Repo(staged={INBOX: _ticket(labels=("hardware",))})
        findings = self._run(repo, [INBOX])
        self.assertEqual(len(findings), 1)
        self.assertIn(INBOX, findings[0])
        self.assertIn("biometrics", findings[0])
        self.assertEqual(repo.warnings, [])

    def test_label_removal_that_unjustifies_is_blocked(self):
        head = _ticket(labels=("hardware", "biometrics"))
        repo = _Repo(staged={INBOX: _ticket(labels=("hardware",))}, head={INBOX: head})
        self.assertEqual(len(self._run(repo, [INBOX])), 1)

    def test_exact_label_match_justifies(self):
        repo = _Repo(staged={INBOX: _ticket(labels=("hardware", "biometrics"))})
        self.assertEqual(self._run(repo, [INBOX]), [])

    def test_null_project_id_is_clean(self):
        repo = _Repo(staged={INBOX: _ticket(project_id=None)})
        self.assertEqual(self._run(repo, [INBOX]), [])

    def test_ticket_chain_hop_to_same_project_ancestor_justifies(self):
        parent = _ticket("T-9000", labels=("biometrics",))
        child = _ticket(labels=("hardware",), nearest="ticket:T-9000")
        repo = _Repo(staged={INBOX: child})
        self.assertEqual(self._run(repo, [INBOX], {"inbox/T-9000.md": parent}), [])

    def test_broken_chain_is_blocked(self):
        child = _ticket(labels=("hardware",), nearest="ticket:T-9999")
        repo = _Repo(staged={INBOX: child})
        self.assertEqual(len(self._run(repo, [INBOX])), 1)

    def test_done_ticket_state_is_skipped(self):
        repo = _Repo(staged={INBOX: _ticket(state="done", labels=("hardware",))})
        self.assertEqual(self._run(repo, [INBOX]), [])

    def test_pre_existing_unjustified_ticket_is_not_reblocked(self):
        # HEAD is ALREADY unjustified; the commit only appends a log line
        # (annotate). Blocking it would lock ticket ops on a red ticket.
        head = _ticket(labels=("hardware",))
        repo = _Repo(staged={INBOX: _ticket(labels=("hardware",), note="- annotated")},
                     head={INBOX: head})
        self.assertEqual(self._run(repo, [INBOX]), [])

    def test_changing_project_id_to_another_unjustified_one_is_blocked(self):
        head = _ticket(project_id="biometrics", labels=("hardware",))
        staged = _ticket(project_id="echo-prismatic", labels=("hardware",))
        repo = _Repo(staged={INBOX: staged}, head={INBOX: head})
        findings = self._run(repo, [INBOX])
        self.assertEqual(len(findings), 1)
        self.assertIn("echo-prismatic", findings[0])

    def test_pure_rename_of_pre_existing_unjustified_ticket_does_not_block(self):
        # ticket.py claim moves inbox -> active. HEAD:<new path> does not exist;
        # the comparison must follow the rename source, or a pre-existing red
        # ticket reads as newly introduced and the claim is refused.
        moved = _ticket(labels=("hardware",), state="active")
        head_old = _ticket(labels=("hardware",))
        repo = _Repo(staged={ACTIVE: moved}, head={INBOX: head_old},
                     renames={ACTIVE: INBOX})
        self.assertEqual(self._run(repo, [ACTIVE]), [])
        self.assertIn(("rename", ACTIVE), repo.calls)

    def test_rename_of_justified_ticket_that_drops_its_label_is_blocked(self):
        moved = _ticket(labels=("hardware",), state="active")
        head_old = _ticket(labels=("hardware", "biometrics"))
        repo = _Repo(staged={ACTIVE: moved}, head={INBOX: head_old},
                     renames={ACTIVE: INBOX})
        self.assertEqual(len(self._run(repo, [ACTIVE])), 1)

    def test_absent_at_head_with_no_rename_source_is_treated_as_new(self):
        repo = _Repo(staged={ACTIVE: _ticket(labels=("hardware",), state="active")})
        self.assertEqual(len(self._run(repo, [ACTIVE])), 1)

    def test_unparseable_staged_frontmatter_fails_open_and_keeps_sibling_finding(self):
        other = "tickets/inbox/T-9102.md"
        repo = _Repo(staged={
            INBOX: "no frontmatter at all\n",
            other: _ticket("T-9102", labels=("hardware",)),
        })
        findings = self._run(repo, [INBOX, other])
        self.assertEqual(len(findings), 1)
        self.assertIn(other, findings[0])
        self.assertEqual(len(repo.warnings), 1)
        self.assertIn(INBOX, repo.warnings[0])

    def test_unparseable_head_blob_is_ignored_and_the_violation_still_blocks(self):
        repo = _Repo(staged={INBOX: _ticket(labels=("hardware",))},
                     head={INBOX: "garbage\n"})
        self.assertEqual(len(self._run(repo, [INBOX])), 1)

    def test_non_ticket_and_done_paths_are_a_cheap_skip(self):
        repo = _Repo()
        staged = ["tools/x.py", "tickets/done/T-9101.md", "tickets/README.md",
                  "tickets/inbox/notes.md"]
        self.assertEqual(self._run(repo, staged), [])
        self.assertEqual(repo.calls, [])

    def test_a_newline_suffixed_ticket_path_is_not_a_ticket(self):
        """T-4541: `$` matches before a trailing newline, so `T-9101.md\\n` read as a
        ticket here while the corpus (`glob("T-*.md")`) and the hook's cheap-skip
        (`endswith(".md")`) both say it is not one. The definitions must agree."""
        repo = _Repo(staged={INBOX + "\n": _ticket(labels=("hardware",))})
        self.assertEqual(self._run(repo, [INBOX + "\n"]), [])
        self.assertEqual(repo.calls, [])

    def test_index_is_not_loaded_unless_a_chain_walk_needs_it(self):
        repo = _Repo(staged={INBOX: _ticket(labels=("hardware", "biometrics"))})
        with patch("tools.ticket_purpose.load_ticket_purpose_index",
                   side_effect=AssertionError("index must stay unloaded")):
            self.assertEqual(self._run(repo, [INBOX]), [])
        self.assertEqual(repo.warnings, [])

    def test_index_load_failure_fails_open_for_the_chain_candidate(self):
        child = _ticket(labels=("hardware",), nearest="ticket:T-9000")
        repo = _Repo(staged={INBOX: child})
        with patch("tools.ticket_purpose.load_ticket_purpose_index",
                   side_effect=RuntimeError("corpus unreadable")):
            self.assertEqual(self._run(repo, [INBOX]), [])
        self.assertEqual(len(repo.warnings), 1)
        self.assertIn("corpus unreadable", repo.warnings[0])


class TopicPolicyGateTests(unittest.TestCase):
    REGISTERED = next(iter(watch_runner.TOPIC_POLICY))

    @staticmethod
    def _topics(*stanzas: tuple[str, str]) -> str:
        body = "# topics\n"
        for slug, cadence in stanzas:
            body += f"\n## {slug}\n- **cadence:** {cadence}\n- **scope:** watch things\n"
        return body

    def _run(self, repo: _Repo, staged=None):
        return gate.topic_policy_findings(
            [gate.TOPICS_PATH] if staged is None else staged,
            read_staged=repo.read_staged, read_head=repo.read_head,
            rename_source=repo.rename_source, warn=repo.warnings.append)

    def test_new_active_unregistered_topic_is_blocked_and_names_the_fix(self):
        text = self._topics((self.REGISTERED, "weekly"), ("brand-new-topic", "weekly"))
        repo = _Repo(staged={gate.TOPICS_PATH: text},
                     head={gate.TOPICS_PATH: self._topics((self.REGISTERED, "weekly"))})
        findings = self._run(repo)
        self.assertEqual(len(findings), 1)
        self.assertIn("brand-new-topic", findings[0])
        self.assertNotIn(self.REGISTERED, findings[0])
        self.assertIn("TOPIC_POLICY", findings[0])
        self.assertIn("tools/watch_runner.py", findings[0])

    def test_paused_unregistered_topic_is_clean(self):
        repo = _Repo(staged={gate.TOPICS_PATH: self._topics(("brand-new-topic", "paused"))})
        self.assertEqual(self._run(repo), [])

    def test_unpausing_an_unregistered_topic_is_blocked(self):
        repo = _Repo(
            staged={gate.TOPICS_PATH: self._topics(("brand-new-topic", "weekly"))},
            head={gate.TOPICS_PATH: self._topics(("brand-new-topic", "paused"))})
        self.assertEqual(len(self._run(repo)), 1)

    def test_registered_active_topic_is_clean(self):
        repo = _Repo(staged={gate.TOPICS_PATH: self._topics((self.REGISTERED, "weekly"))})
        self.assertEqual(self._run(repo), [])

    def test_pre_existing_unregistered_topic_is_not_reblocked_but_a_new_one_is(self):
        head = self._topics(("old-unregistered", "weekly"))
        same = self._topics(("old-unregistered", "weekly"))
        repo = _Repo(staged={gate.TOPICS_PATH: same}, head={gate.TOPICS_PATH: head})
        self.assertEqual(self._run(repo), [])
        both = self._topics(("old-unregistered", "weekly"), ("fresh-one", "daily"))
        repo = _Repo(staged={gate.TOPICS_PATH: both}, head={gate.TOPICS_PATH: head})
        findings = self._run(repo)
        self.assertEqual(len(findings), 1)
        self.assertIn("fresh-one", findings[0])
        self.assertNotIn("old-unregistered", findings[0])

    def test_topics_file_not_staged_is_a_cheap_skip(self):
        repo = _Repo()
        self.assertEqual(self._run(repo, ["tools/watch_runner.py", "README.md"]), [])
        self.assertEqual(repo.calls, [])

    def test_file_new_at_head_is_all_introduced(self):
        repo = _Repo(staged={gate.TOPICS_PATH: self._topics(("brand-new-topic", "weekly"))})
        self.assertEqual(len(self._run(repo)), 1)

    def test_watch_runner_import_failure_fails_open(self):
        repo = _Repo(staged={gate.TOPICS_PATH: self._topics(("brand-new-topic", "weekly"))})
        with patch.dict(sys.modules, {"tools.watch_runner": None}):
            self.assertEqual(self._run(repo), [])
        self.assertEqual(len(repo.warnings), 1)

    def test_unreadable_staged_blob_fails_open(self):
        repo = _Repo()  # read_staged returns None
        self.assertEqual(self._run(repo), [])


class ContentGateCombinedTests(unittest.TestCase):
    def test_combined_reports_both_kinds_and_skips_when_nothing_matches(self):
        with tempfile.TemporaryDirectory() as td:
            root = _tickets_root(td)
            repo = _Repo(staged={
                INBOX: _ticket(labels=("hardware",)),
                gate.TOPICS_PATH: TopicPolicyGateTests._topics(("brand-new-topic", "weekly")),
            })
            findings = gate.content_gate_findings(
                [INBOX, gate.TOPICS_PATH], read_staged=repo.read_staged,
                read_head=repo.read_head, rename_source=repo.rename_source,
                tickets_root=root, warn=repo.warnings.append)
            self.assertEqual(len(findings), 2)
            quiet = _Repo()
            self.assertEqual(gate.content_gate_findings(
                ["tools/x.py"], read_staged=quiet.read_staged, read_head=quiet.read_head,
                rename_source=quiet.rename_source, tickets_root=root,
                warn=quiet.warnings.append), [])
            self.assertEqual(quiet.calls, [])


# ---------------------------------------------------------------------------
# .githooks/pre-commit adapter
# ---------------------------------------------------------------------------


def _load_hook():
    loader = importlib.machinery.SourceFileLoader(
        "_pre_commit_hook_under_test_t4402", str(PRE_COMMIT_HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout


class HookAdapterTests(unittest.TestCase):
    def setUp(self):
        self.hook = _load_hook()

    def test_unrelated_staged_paths_are_a_cheap_skip_without_importing_the_gate(self):
        with patch.dict(sys.modules, {"tools.staged_content_gate": None}):
            self.assertEqual(self.hook._content_gate_findings(["tools/x.py", "README.md"]), [])

    def test_gate_module_unavailable_fails_open_with_a_warning(self):
        err = io.StringIO()
        with patch.dict(sys.modules, {"tools.staged_content_gate": None}), \
                contextlib.redirect_stderr(err):
            self.assertIsNone(self.hook._content_gate_findings([INBOX]))
        self.assertIn("WARNING", err.getvalue())
        self.assertIn("fail-open", err.getvalue())

    def test_gate_crash_fails_open_with_a_warning(self):
        err = io.StringIO()
        with patch.object(gate, "content_gate_findings", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err):
            self.assertIsNone(self.hook._content_gate_findings([INBOX]))
        self.assertIn("boom", err.getvalue())

    def _main(self, staged, findings, sources=None, id_sources=None):
        """Run main() with every check stubbed; `sources` is {dest: rename source},
        `id_sources` what the ticket-id lookup (T-4470) would add. The stubs of the two
        baselined checks are kept on `self.mocks`."""
        changes = [(p, (sources or {}).get(p)) for p in staged]
        self.mocks = {}
        with patch.object(self.hook, "_staged_changes", return_value=changes), \
                patch.object(self.hook, "_ticket_id_sources", return_value=id_sources or {}), \
                patch.object(self.hook, "_read_staged_blob", return_value=b""), \
                patch.object(self.hook, "_warn_directive_drift"), \
                patch.object(self.hook, "_hook_allowlist_drift", return_value=None), \
                patch.object(self.hook, "_main_guard_lint_findings", return_value=[]), \
                patch.object(self.hook, "_self_sudo_guard_findings", return_value=[]) as sudo, \
                patch.object(self.hook, "_ledger_validity_findings", return_value=[]), \
                patch.object(self.hook, "_priority_list_validate_findings", return_value=[]), \
                patch.object(self.hook, "_instance_yaml_binding_findings", return_value=[]), \
                patch.object(self.hook, "_content_gate_findings", return_value=findings) as cgate:
            self.mocks = {"sudo": sudo, "content_gate": cgate}
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = self.hook.main()
        return rc, err.getvalue()

    def test_main_threads_one_rename_map_to_the_baselined_checks(self):
        self._main([ACTIVE, "tools/x.py"], [], sources={ACTIVE: INBOX})
        expected = ([ACTIVE, "tools/x.py"], {ACTIVE: INBOX})
        self.mocks["content_gate"].assert_called_once_with(*expected)
        self.mocks["sudo"].assert_called_once_with(*expected)

    def test_main_adds_the_ticket_id_sources_to_the_threaded_rename_map(self):
        """T-4470: a staged ticket `-M` left unpaired reaches the baselined checks
        with its ticket-id source, alongside any `-M` source."""
        self._main([ACTIVE, "tickets/active/T-9102.md"], [],
                   sources={"tickets/active/T-9102.md": "tickets/inbox/T-9102.md"},
                   id_sources={ACTIVE: INBOX})
        expected = ([ACTIVE, "tickets/active/T-9102.md"],
                    {ACTIVE: INBOX, "tickets/active/T-9102.md": "tickets/inbox/T-9102.md"})
        self.mocks["content_gate"].assert_called_once_with(*expected)
        self.mocks["sudo"].assert_called_once_with(*expected)

    def test_a_git_rename_source_wins_over_a_ticket_id_source(self):
        self._main([ACTIVE], [], sources={ACTIVE: "tickets/inbox/T-9101-moved.md"},
                   id_sources={ACTIVE: INBOX})
        self.mocks["content_gate"].assert_called_once_with(
            [ACTIVE], {ACTIVE: "tickets/inbox/T-9101-moved.md"})

    def test_content_gate_uses_the_threaded_rename_map_when_given(self):
        with patch.object(gate, "content_gate_findings", return_value=[]) as cg:
            self.hook._content_gate_findings([ACTIVE], {ACTIVE: INBOX})
        self.assertEqual(cg.call_args.kwargs["rename_source"](ACTIVE), INBOX)
        self.assertIsNone(cg.call_args.kwargs["rename_source"]("tickets/inbox/T-1.md"))

    def test_main_refuses_the_commit_on_a_content_finding(self):
        rc, err = self._main([INBOX], [f"{INBOX}: project_id 'biometrics' is not justified"])
        self.assertEqual(rc, 1)
        self.assertIn("content-drift gate", err)
        self.assertIn(INBOX, err)

    def test_main_allows_the_commit_when_the_gate_is_clean_or_failed_open(self):
        self.assertEqual(self._main([INBOX], [])[0], 0)
        self.assertEqual(self._main([INBOX], None)[0], 0)

    def test_real_git_new_unjustified_ticket_blocks_and_pure_rename_does_not(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-q", ".")
            (repo / "tickets" / "inbox").mkdir(parents=True)
            (repo / "tickets" / "active").mkdir(parents=True)
            body = "\n".join(f"- log line {i}" for i in range(30))
            (repo / INBOX).write_text(_ticket(labels=("hardware",), note=body))
            _git(repo, "add", "-A")

            # Newly introduced unjustified ticket: blocked.
            with contextlib.chdir(repo):
                staged = self.hook._staged_files()
                findings = self.hook._content_gate_findings(staged)
            self.assertEqual(staged, [INBOX])
            self.assertEqual(len(findings), 1)

            # Land it (as if it were legacy red on main), then move it like
            # `ticket.py claim` does.
            _git(repo, "commit", "-q", "-m", "legacy red ticket")
            _git(repo, "mv", INBOX, ACTIVE)
            with contextlib.chdir(repo):
                status = _git(repo, "diff", "--cached", "-M", "--name-status")
                self.assertTrue(status.startswith("R"), status)
                self.assertEqual(self.hook._rename_source(ACTIVE), INBOX)
                # The hook's own staged list includes the rename destination
                # (T-4451), so the gate sees the moved path as the hook would.
                staged = self.hook._staged_files()
                self.assertEqual(staged, [ACTIVE])
                self.assertEqual(self.hook._content_gate_findings(staged), [])
                # ... and an EDIT after the move (claim) is also fine.
                (repo / ACTIVE).write_text(
                    _ticket(labels=("hardware",), state="active", note=body + "\n- claimed"))
                _git(repo, "add", "-A")
                staged = self.hook._staged_files()
                self.assertEqual(staged, [ACTIVE])
                self.assertEqual(self.hook._content_gate_findings(staged), [])

    def test_real_git_topics_change_blocks_only_the_newly_unregistered_slug(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git(repo, "init", "-q", ".")
            topics = repo / gate.TOPICS_PATH
            topics.parent.mkdir(parents=True)
            registered = TopicPolicyGateTests.REGISTERED
            topics.write_text(TopicPolicyGateTests._topics((registered, "weekly")))
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "seed topics")
            topics.write_text(TopicPolicyGateTests._topics(
                (registered, "weekly"), ("brand-new-topic", "weekly")))
            _git(repo, "add", "-A")
            with contextlib.chdir(repo):
                findings = self.hook._content_gate_findings([gate.TOPICS_PATH])
            self.assertEqual(len(findings), 1)
            self.assertIn("brand-new-topic", findings[0])


class TicketIdSourceTests(unittest.TestCase):
    """T-4470: `_ticket_id_sources` resolves the baseline of a staged ticket that git's
    `-M` left unpaired (an edit dropped similarity below 50%, so the move is an A + a D,
    not an R) by ticket id from the staged deletions. Real git throughout."""

    SHORT = _ticket(note="")
    LONG = _ticket(note="\n".join(f"- a wholly different log line number {i}" for i in range(40)))

    def setUp(self):
        self.hook = _load_hook()
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.repo = Path(self._td.name)
        _git(self.repo, "init", "-q", ".")
        for state in ("inbox", "active", "done"):
            (self.repo / "tickets" / state).mkdir(parents=True)

    def _commit(self, files: dict[str, str]):
        for rel, text in files.items():
            (self.repo / rel).write_text(text, encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "seed")

    def _stage(self, *, remove=(), write=None):
        for rel in remove:
            _git(self.repo, "rm", "-q", rel)
        for rel, text in (write or {}).items():
            (self.repo / rel).write_text(text, encoding="utf-8")
        _git(self.repo, "add", "-A")

    def _sources(self):
        with contextlib.chdir(self.repo):
            return self.hook._ticket_id_sources(self.hook._staged_changes())

    def _precondition_unpaired(self):
        status = _git(self.repo, "diff", "--cached", "-M", "--name-status")
        self.assertNotIn("R", [line[:1] for line in status.splitlines()], status)

    def test_a_low_similarity_claim_resolves_its_source_by_ticket_id(self):
        self._commit({INBOX: self.SHORT})
        self._stage(remove=[INBOX], write={ACTIVE: self.LONG})
        self._precondition_unpaired()
        self.assertEqual(self._sources(), {ACTIVE: INBOX})

    def test_a_low_similarity_resolve_to_done_resolves_its_source_by_ticket_id(self):
        self._commit({ACTIVE: self.SHORT})
        self._stage(remove=[ACTIVE], write={DONE: self.LONG})
        self._precondition_unpaired()
        self.assertEqual(self._sources(), {DONE: ACTIVE})

    def test_a_high_similarity_rename_is_paired_by_git_and_needs_no_id_lookup(self):
        self._commit({INBOX: self.LONG})
        self._stage(remove=[INBOX], write={ACTIVE: self.LONG + "- claimed\n"})
        with contextlib.chdir(self.repo):
            changes = self.hook._staged_changes()
            self.assertEqual(changes, [(ACTIVE, INBOX)])
            self.assertEqual(self.hook._ticket_id_sources(changes), {})
            self.assertEqual(self.hook._rename_map(changes), {ACTIVE: INBOX})

    def test_two_same_id_deletions_are_ambiguous_and_get_no_credit(self):
        self._commit({INBOX: self.SHORT, DONE: self.SHORT})
        self._stage(remove=[INBOX, DONE], write={ACTIVE: self.LONG})
        self._precondition_unpaired()
        self.assertEqual(self._sources(), {})

    def test_two_same_id_additions_get_no_credit(self):
        """One deleted source cannot be the baseline of two destinations (a copy never
        earns the credit -- see `_staged_changes`)."""
        self._commit({INBOX: self.SHORT})
        self._stage(remove=[INBOX], write={ACTIVE: self.LONG, DONE: self.LONG})
        self._precondition_unpaired()
        self.assertEqual(self._sources(), {})

    def test_a_deletion_with_a_different_id_gets_no_credit(self):
        self._commit({"tickets/inbox/T-9102.md": _ticket("T-9102", note="")})
        self._stage(remove=["tickets/inbox/T-9102.md"], write={ACTIVE: self.LONG})
        self._precondition_unpaired()
        self.assertEqual(self._sources(), {})

    def test_a_new_ticket_with_no_deletion_gets_no_credit(self):
        self._stage(write={INBOX: self.LONG})
        self.assertEqual(self._sources(), {})

    def test_a_modified_ticket_gets_no_id_source(self):
        """M is not an unpaired addition: the path is in HEAD, so HEAD is its baseline
        even when another same-id file is being deleted."""
        self._commit({ACTIVE: self.SHORT, DONE: self.SHORT})
        self._stage(remove=[DONE], write={ACTIVE: self.LONG})
        self.assertEqual(self._sources(), {})

    def test_a_non_ticket_path_is_never_resolved_and_runs_no_git(self):
        with contextlib.chdir(self.repo), \
                patch.object(self.hook.subprocess, "run") as run:
            self.assertEqual(self.hook._ticket_id_sources([("tools/x.py", None)]), {})
        run.assert_not_called()

    def test_a_newline_suffixed_destination_gets_no_credit(self):
        """T-4541: a file named `T-9101.md\\n` is not a ticket, so it earns no baseline."""
        self._commit({INBOX: self.SHORT})
        self._stage(remove=[INBOX], write={ACTIVE + "\n": self.LONG})
        self._precondition_unpaired()
        self.assertEqual(self._sources(), {})

    def test_a_newline_suffixed_deletion_is_not_a_ticket_id_source(self):
        """T-4541: nor can one be the baseline another ticket is credited with."""
        self._commit({INBOX + "\n": self.SHORT})
        self._stage(remove=[INBOX + "\n"], write={ACTIVE: self.LONG})
        self._precondition_unpaired()
        self.assertEqual(self._sources(), {})

    def test_a_git_error_gives_no_credit(self):
        self._commit({INBOX: self.SHORT})
        self._stage(remove=[INBOX], write={ACTIVE: self.LONG})
        # Valid-looking stdout, so only the exit code stands between it and a credit.
        failed = subprocess.CompletedProcess(
            [], 128, f"A\0{ACTIVE}\0D\0{INBOX}\0".encode(), b"fatal: boom")
        with contextlib.chdir(self.repo), \
                patch.object(self.hook.subprocess, "run", return_value=failed):
            self.assertEqual(self.hook._ticket_id_sources([(ACTIVE, None)]), {})

    def test_malformed_git_output_gives_no_credit(self):
        """A truncated tail could be a lost second same-id deletion, so it is not
        guessed past (unlike `_staged_changes`, which drops it): fail-closed."""
        garbled = subprocess.CompletedProcess(
            [], 0, f"A\0{ACTIVE}\0D\0{INBOX}\0D\0".encode(), b"")
        with contextlib.chdir(self.repo), \
                patch.object(self.hook.subprocess, "run", return_value=garbled):
            self.assertEqual(self.hook._ticket_id_sources([(ACTIVE, None)]), {})


class GitCommitSmokeTests(unittest.TestCase):
    """End-to-end through a real `git commit` with the live hook installed via
    core.hooksPath (the test_hermes_hook.TestGitSmoke pattern): the throwaway
    repo never touches pantheon-config, identity and hooksPath are passed per
    invocation with -c so nothing is written to any git config."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.repo = Path(self._td.name)
        subprocess.run(["git", "init", "-q", "."], cwd=self.repo, check=True)
        (self.repo / "tickets" / "inbox").mkdir(parents=True)

    def _git(self, *args: str, verify: bool = True) -> subprocess.CompletedProcess:
        cmd = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
               "-c", f"core.hooksPath={REPO_ROOT / '.githooks'}", *args]
        if not verify and args[0] == "commit":
            cmd.append("--no-verify")
        return subprocess.run(cmd, cwd=self.repo, capture_output=True, text=True, check=False)

    def _commit_ticket(self, text: str, msg: str, verify: bool = True):
        (self.repo / INBOX).write_text(text, encoding="utf-8")
        self._git("add", "-A")
        return self._git("commit", "-q", "-m", msg, verify=verify)

    def test_refuses_an_unjustified_ticket_then_accepts_it_once_labelled(self):
        refused = self._commit_ticket(_ticket(labels=("hardware",)), "bad ticket")
        self.assertNotEqual(refused.returncode, 0, refused.stderr)
        self.assertIn("content-drift gate", refused.stderr)
        self.assertIn(INBOX, refused.stderr)
        accepted = self._commit_ticket(
            _ticket(labels=("hardware", "biometrics")), "good ticket")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_a_pre_existing_red_ticket_can_still_be_annotated_and_moved(self):
        legacy = self._commit_ticket(
            _ticket(labels=("hardware",)), "legacy red ticket", verify=False)
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        annotated = self._commit_ticket(
            _ticket(labels=("hardware",), note="- annotated"), "annotate")
        self.assertEqual(annotated.returncode, 0, annotated.stderr)
        (self.repo / "tickets" / "active").mkdir()
        self._git("mv", INBOX, ACTIVE)
        moved = self._git("commit", "-q", "-m", "claim")
        self.assertEqual(moved.returncode, 0, moved.stderr)

    # -- ticket.py-shaped moves: `git mv` + a frontmatter edit in ONE commit (T-4451) --
    # `ticket.py claim`/`unclaim`/`resolve` do exactly this. Status is R, which the
    # hook's staged list used to drop, so none of these reached the gate at all.

    BODY = "\n".join(f"- log line {i}" for i in range(30))

    def _seed(self, text: str, verify: bool = True):
        done = self._commit_ticket(text, "seed ticket", verify=verify)
        self.assertEqual(done.returncode, 0, done.stderr)

    def _claim(self, text: str, msg: str = "claim", paired: bool = True):
        """`git mv` inbox -> active AND rewrite the moved file, then commit. `paired`
        is the precondition on git's `-M`: an R entry, or (False) an unpaired A + D
        because the rewrite dropped similarity below 50% (T-4470)."""
        (self.repo / "tickets" / "active").mkdir(exist_ok=True)
        moved = self._git("mv", INBOX, ACTIVE)
        self.assertEqual(moved.returncode, 0, moved.stderr)
        (self.repo / ACTIVE).write_text(text, encoding="utf-8")
        self._git("add", "-A")
        status = self._git("diff", "--cached", "-M", "--name-status").stdout
        kinds = sorted(line[:1] for line in status.splitlines())
        want = ["R"] if paired else ["A", "D"]
        self.assertEqual(kinds, want, f"precondition: want {want} from -M, got {status!r}")
        return self._git("commit", "-q", "-m", msg)

    def test_claiming_a_pre_existing_red_ticket_with_a_frontmatter_edit_is_allowed(self):
        self._seed(_ticket(labels=("hardware",), note=self.BODY), verify=False)  # legacy red
        claimed = self._claim(_ticket(labels=("hardware",), state="active", note=self.BODY))
        self.assertEqual(claimed.returncode, 0, claimed.stderr)

    def test_claiming_a_clean_ticket_with_a_frontmatter_edit_is_allowed(self):
        labels = ("hardware", "biometrics")
        self._seed(_ticket(labels=labels, note=self.BODY))
        claimed = self._claim(_ticket(labels=labels, state="active", note=self.BODY))
        self.assertEqual(claimed.returncode, 0, claimed.stderr)

    def test_a_claim_whose_edit_drops_the_justifying_label_is_refused(self):
        """The rename INTRODUCES the finding (HEAD's copy was justified), so the
        gate must fire on the moved path -- the case the old ACM filter hid."""
        self._seed(_ticket(labels=("hardware", "biometrics"), note=self.BODY))
        refused = self._claim(_ticket(labels=("hardware",), state="active", note=self.BODY))
        self.assertNotEqual(refused.returncode, 0, "renamed+edited ticket skipped the gate")
        self.assertIn("content-drift gate", refused.stderr)
        self.assertIn(ACTIVE, refused.stderr)

    def test_a_claim_whose_edit_adds_the_byte_sequence_is_refused(self):
        labels = ("hardware", "biometrics")
        self._seed(_ticket(labels=labels, note=self.BODY))
        trigger = "- pasted " + ("HERME" + "S.md")
        refused = self._claim(_ticket(labels=labels, state="active", note=self.BODY + "\n" + trigger))
        self.assertNotEqual(refused.returncode, 0, "renamed+edited ticket skipped the scan")
        self.assertIn("discipline violation", refused.stderr)
        self.assertIn(ACTIVE, refused.stderr)

    # -- T-4470: the same moves when the edit drops similarity below git's 50% -M floor --
    # The move is then an unpaired A + D, so `-M` supplies no baseline; the hook resolves
    # it by ticket id from the staged deletions.

    LOW_SIM = "\n".join(f"- a wholly different log line number {i}" for i in range(40))

    def test_claiming_a_pre_existing_red_ticket_below_the_rename_floor_is_allowed(self):
        self._seed(_ticket(labels=("hardware",), note=""), verify=False)  # legacy red
        claimed = self._claim(
            _ticket(labels=("hardware",), state="active", note=self.LOW_SIM), paired=False)
        self.assertEqual(claimed.returncode, 0, claimed.stderr)

    def test_a_below_the_floor_claim_whose_edit_drops_the_justifying_label_is_refused(self):
        """Baseline credit is for pre-existing findings only: HEAD's copy was justified,
        so the label drop is still INTRODUCED by the commit."""
        self._seed(_ticket(labels=("hardware", "biometrics"), note=""))
        refused = self._claim(
            _ticket(labels=("hardware",), state="active", note=self.LOW_SIM), paired=False)
        self.assertNotEqual(refused.returncode, 0, "the id-resolved baseline hid a new finding")
        self.assertIn("content-drift gate", refused.stderr)
        self.assertIn(ACTIVE, refused.stderr)

    def test_a_below_the_floor_claim_whose_edit_adds_the_byte_sequence_is_refused(self):
        labels = ("hardware", "biometrics")
        self._seed(_ticket(labels=labels, note=""))
        trigger = "- pasted " + ("HERME" + "S.md")
        refused = self._claim(
            _ticket(labels=labels, state="active", note=self.LOW_SIM + "\n" + trigger),
            paired=False)
        self.assertNotEqual(refused.returncode, 0, "renamed+edited ticket skipped the scan")
        self.assertIn("discipline violation", refused.stderr)

    def test_a_below_the_floor_claim_is_refused_when_two_same_id_tickets_are_deleted(self):
        """Ambiguous source -> no credit -> the pre-existing red reads as introduced."""
        self._seed(_ticket(labels=("hardware",), note=""), verify=False)
        (self.repo / "tickets" / "done").mkdir()
        (self.repo / DONE).write_text(_ticket(labels=("hardware",), note=""), encoding="utf-8")
        self._git("add", "-A")
        dup = self._git("commit", "-q", "-m", "duplicate id", "--no-verify")
        self.assertEqual(dup.returncode, 0, dup.stderr)
        (self.repo / "tickets" / "active").mkdir()
        self._git("rm", "-q", INBOX, DONE)
        (self.repo / ACTIVE).write_text(
            _ticket(labels=("hardware",), state="active", note=self.LOW_SIM), encoding="utf-8")
        self._git("add", "-A")
        refused = self._git("commit", "-q", "-m", "claim")
        self.assertNotEqual(refused.returncode, 0, "ambiguous id source was credited")
        self.assertIn("content-drift gate", refused.stderr)


if __name__ == "__main__":
    unittest.main()
