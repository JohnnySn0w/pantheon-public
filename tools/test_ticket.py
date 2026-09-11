"""Unit tests for tools/ticket.py.

Run from repo root:
    python3 -m unittest tools.test_ticket -v

Covers ID generation, front-matter round-trip, transition-log append,
HERMES guard on file content + commit message, lifecycle invariants
(state-mismatch refusals), list filtering, and the full file→claim→resolve
git-command sequence with subprocess mocked.

Fixture-string discipline: any test that needs the contiguous unescaped
HERMES trigger bytes constructs them at runtime via string concatenation
("HERME" + "S.md") so the literal 9-byte sequence never appears in this
source file. Operator can verify with `grep -F` for the unescaped trigger
phrase against this file expecting zero hits. Per /home/claude/CLAUDE.md
"HERMES\\.md byte-sequence discipline" (escape form in prose, mentally
drop the backslash).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _git_utils, ticket


# Runtime-constructed unescaped trigger; never appears as a contiguous
# 9-byte sequence in this source file. See module docstring.
TRIGGER = "HERME" + "S.md"


def _branch_proc(
    stdout: str,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        ["git", "branch", "--show-current"],
        returncode,
        stdout=stdout,
        stderr=stderr or ("fatal: synthetic branch query failure\n" if returncode else ""),
    )


def _write_ticket(
    path: Path,
    *,
    tid: str,
    state: str,
    requester: str = "orchestrator",
    target: str = "recovery",
    created: str = "2026-05-02T01:30:00Z",
    title: str = "Example ticket",
    labels: list[str] | None = None,
    depends_on: list[str] | None = None,
    not_before: str | None = None,
    extra_body: str = "",
    ask: str = "",
    context: str = "",
    log_entries: list[str] | None = None,
) -> None:
    """Write a well-formed ticket file for tests."""
    fm_lines = [
        "---",
        f"id: {tid}",
        f"created: {created}",
        f"requester: {requester}",
        f"target: {target}",
        f"state: {state}",
        "parent_ticket: null",
        f"depends_on: [{', '.join(depends_on)}]" if depends_on else "depends_on: []",
    ]
    if not_before:
        fm_lines.append(f"not_before: {not_before}")
    if labels:
        fm_lines.append(f"labels: [{', '.join(labels)}]")
    fm_lines.append("---")

    log = log_entries or [f"- {created} [orchestrator] filed in inbox"]
    body_lines = [
        "",
        f"# {title}",
        "",
        "## Ask",
        "",
        ask,
        "## Context",
        "",
        context,
        "## Transition log",
        "",
        *log,
        extra_body,
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm_lines + body_lines), encoding="utf-8")


class TestMainCheckoutGuard(unittest.TestCase):
    def _run_main_with_branch(
        self,
        argv: list[str],
        branch_stdout: str,
        *,
        returncode: int = 0,
        branch_stderr: str = "",
    ) -> tuple[int, str, MagicMock]:
        git_mock = MagicMock(
            return_value=_branch_proc(branch_stdout, returncode, branch_stderr)
        )
        with patch.object(ticket, "_git", git_mock), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            rc = ticket.main(argv)
        git_mock.assert_called_once_with(
            "branch", "--show-current", cwd=ticket.REPO_ROOT
        )
        return rc, stderr.getvalue(), git_mock

    def _assert_bounded_one_line(self, stderr: str) -> None:
        lines = stderr.splitlines()
        self.assertEqual(len(lines), 1, stderr)
        self.assertLessEqual(len(lines[0]), 220)
        self.assertNotIn("/home/", stderr)

    def test_main_refuses_agent_branch_before_mutating_dispatch(self) -> None:
        handler = MagicMock(return_value=23)
        with patch.object(ticket, "cmd_annotate", handler):
            rc, stderr, _ = self._run_main_with_branch(
                ["annotate", "T-2929", "--note", "blocked in agent checkout"],
                "agent/test\n",
            )

        self.assertEqual(rc, 1)
        self._assert_bounded_one_line(stderr)
        self.assertIn("agent/test", stderr)
        self.assertIn("canonical pantheon-config checkout on branch main", stderr)
        handler.assert_not_called()

    def test_main_refuses_agent_branch_before_each_read_only_dispatch(self) -> None:
        cases = (
            ("list", ["list"], "cmd_list"),
            ("ready", ["ready"], "cmd_ready"),
            ("prime", ["prime"], "cmd_prime"),
        )
        for name, argv, handler_name in cases:
            with self.subTest(name=name):
                handler = MagicMock(return_value=24)
                with patch.object(ticket, handler_name, handler):
                    rc, stderr, _ = self._run_main_with_branch(argv, "agent/test\n")

                self.assertEqual(rc, 1)
                self._assert_bounded_one_line(stderr)
                self.assertIn("agent/test", stderr)
                handler.assert_not_called()

    def test_main_allows_non_agent_branches(self) -> None:
        for branch in ("main\n", "feature/ticket-cleanup\n"):
            with self.subTest(branch=branch.strip()):
                handler = MagicMock(return_value=25)
                with patch.object(ticket, "cmd_list", handler):
                    rc, stderr, _ = self._run_main_with_branch(["list"], branch)

                self.assertEqual(rc, 25)
                self.assertEqual(stderr, "")
                handler.assert_called_once()

    def test_main_refuses_when_branch_query_fails(self) -> None:
        handler = MagicMock(return_value=26)
        with patch.object(ticket, "cmd_list", handler):
            rc, stderr, _ = self._run_main_with_branch(
                ["list"],
                "stdout first line\nstdout final line\n",
                returncode=128,
                branch_stderr=(
                    "stderr first line /home/codex/ignored\n"
                    "\n"
                    "fatal: cannot read /home/codex/worktrees/T-2929/.git\n"
                ),
            )

        self.assertEqual(rc, 1)
        self._assert_bounded_one_line(stderr)
        self.assertIn("rc=128", stderr)
        self.assertIn("cannot determine current git branch", stderr)
        self.assertIn("fatal: cannot read <path>", stderr)
        self.assertNotIn("stdout final line", stderr)
        self.assertIn("canonical pantheon-config checkout on branch main", stderr)
        handler.assert_not_called()

    def test_main_allows_detached_head(self) -> None:
        handler = MagicMock(return_value=27)
        with patch.object(ticket, "cmd_list", handler):
            rc, stderr, _ = self._run_main_with_branch(["list"], "")

        self.assertEqual(rc, 27)
        self.assertEqual(stderr, "")
        handler.assert_called_once()

    def test_agent_branch_subprocess_refuses_before_ticket_or_head_mutation(self) -> None:
        from tools._test_fixtures import (
            _run,
            assert_real_repo_untouched,
            git_head,
            setup_integration_env,
            snapshot_real_repo,
            teardown_integration_env,
        )

        real_snapshot = snapshot_real_repo()
        env = setup_integration_env()
        try:
            tools_dir = env.repo_path / "tools"
            tools_dir.mkdir(parents=True, exist_ok=True)
            repo_tools = Path(__file__).resolve().parent
            for fname in (
                "__init__.py",
                "ticket.py",
                "_git_utils.py",
                "agent_role_map.py",
                "_md_table.py",
                "hermes_action.py",
                # T-3020 BT3 / ORG-078: ticket.py now imports this at module
                # scope (priority-sort's rank-map source) — the subprocess
                # fixture needs it on disk too, or import fails before the
                # guard under test even runs.
                "priority_list_parse.py",
            ):
                src = repo_tools / fname
                if src.exists():
                    shutil.copy2(src, tools_dir / fname)

            for state in ("inbox", "active", "done"):
                (env.repo_path / "tickets" / state).mkdir(parents=True, exist_ok=True)
                (env.repo_path / "tickets" / state / ".gitkeep").touch()
            (env.repo_path / "tickets" / "README.md").write_text(
                "# Tickets\n\n## Current state\n\n<!-- AUTOGEN -->\n",
                encoding="utf-8",
            )
            ticket_path = env.repo_path / "tickets" / "active" / "T-2929.md"
            _write_ticket(
                ticket_path,
                tid="T-2929",
                state="active",
                title="Agent branch guard fixture",
            )
            _run(["git", "add", "tickets/", "tools/"], cwd=env.repo_path)
            _run(["git", "commit", "-m", "seed ticket cli fixture"], cwd=env.repo_path)
            _run(["git", "push", "origin", "main"], cwd=env.repo_path)
            _run(["git", "switch", "-c", "agent/test"], cwd=env.repo_path)

            pre_head = git_head(env.repo_path)
            pre_ticket = ticket_path.read_bytes()
            pre_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=env.repo_path,
                capture_output=True,
                text=True,
                check=True,
            ).stdout

            result = subprocess.run(
                [
                    "python3",
                    str(env.repo_path / "tools" / "ticket.py"),
                    "annotate",
                    "T-2929",
                    "--note",
                    "must not mutate from agent branch",
                    "--no-push",
                    "--no-pull",
                ],
                cwd=env.repo_path,
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    **env.env_overrides,
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                timeout=60,
            )

            post_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=env.repo_path,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self._assert_bounded_one_line(result.stderr)
            self.assertIn("agent/test", result.stderr)
            self.assertIn("canonical pantheon-config checkout on branch main", result.stderr)
            self.assertEqual(git_head(env.repo_path), pre_head)
            self.assertEqual(ticket_path.read_bytes(), pre_ticket)
            self.assertEqual(post_status, pre_status)
        finally:
            teardown_integration_env(env)
            assert_real_repo_untouched(real_snapshot)


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


class TestIdGeneration(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._patch.start()
        # _next_ticket_id now calls `git fetch origin main` and
        # `git ls-tree origin/main tickets/` as side effects (T-0068).
        # Mock subprocess.run so these don't hit real git: fetch succeeds,
        # ls-tree returns empty stdout (origin contributes 0 to the max).
        def _git_stub(argv, **kwargs):
            if argv[:3] == ["git", "fetch", "origin"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "ls-tree"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        self._sub_patch = patch("tools._git_utils.subprocess.run", side_effect=_git_stub)
        self._sub_patch.start()

    def tearDown(self) -> None:
        self._sub_patch.stop()
        self._patch.stop()
        self._tmp.cleanup()

    def test_empty_dirs_yield_T0001(self) -> None:
        self.assertEqual(ticket._next_ticket_id(), "T-0001")

    def test_max_plus_one_in_inbox(self) -> None:
        for n in (1, 7, 9):
            (self.root / "inbox" / f"T-{n:04d}.md").write_text("stub")
        self.assertEqual(ticket._next_ticket_id(), "T-0010")

    def test_scans_all_three_state_dirs(self) -> None:
        # ORG-019: blocked/ retired; 3-state machine: inbox/active/done.
        (self.root / "inbox" / "T-0001.md").write_text("stub")
        (self.root / "active" / "T-0005.md").write_text("stub")
        (self.root / "done" / "T-0003.md").write_text("stub")
        self.assertEqual(ticket._next_ticket_id(), "T-0006")

    def test_skips_non_matching_filenames(self) -> None:
        # T-3215: "T-12345.md" was dropped from this fixture — under the
        # widened TICKET_FILENAME_RE (4+ digits) it is now WELL-FORMED and
        # counts toward the max (see test_five_digit_filename_counts_toward_max
        # below), so it no longer belongs in a "does not match" list.
        for name in ("README.md", "T-not-a-ticket.md", "T-12.md", "T-123.md"):
            (self.root / "inbox" / name).write_text("stub")
        # Only well-formed T-NNNN(N+).md counts. None match → T-0001.
        self.assertEqual(ticket._next_ticket_id(), "T-0001")

    def test_five_digit_filename_counts_toward_max(self) -> None:
        """T-3215: a five-or-more-digit filename must count toward allocation —
        the ten-thousandth ticket (crossing four digits) must not silently
        become invisible to _next_ticket_id."""
        five_digit_id = "T-" + "12345"
        (self.root / "inbox" / f"{five_digit_id}.md").write_text("stub")
        self.assertEqual(ticket._next_ticket_id(), "T-" + "12346")

    def test_skips_origin_side_id_higher_than_local(self) -> None:
        """Origin/main has a HIGHER ticket than the local tree → allocate above origin.

        T-1939 regression: `_next_ticket_id` has consulted origin since T-0068
        (2026-05-05), but every existing test in this class mocks `ls-tree
        origin/main` to EMPTY, so origin_max is always 0 and the case that
        actually matters — the origin-side ID exceeding local_max, i.e. the exact
        T-1922 collision scenario — was never exercised. Correct code protected
        only by tests that route around the real case (the T-1931 shape).
        """
        (self.root / "inbox" / "T-0100.md").write_text("stub")  # local max = 100

        def _stub(argv, **kwargs):
            if argv[:3] == ["git", "fetch", "origin"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "ls-tree"]:
                # origin/main carries T-0500 (+ a lower one, to prove max not last)
                return MagicMock(
                    returncode=0,
                    stdout="tickets/done/T-0500.md\ntickets/inbox/T-0100.md\n",
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("tools._git_utils.subprocess.run", side_effect=_stub):
            # A local-only allocator would return T-0101; consulting origin → T-0501.
            self.assertEqual(ticket._next_ticket_id(), "T-0501")

    def test_raises_when_fetch_fails_rather_than_stale_allocation(self) -> None:
        """Fail-loud on fetch failure — NO offline fallback (T-1939 ruling).

        A failed fetch means the local view of origin may be stale, which is
        exactly how the duplicate-ID race arises (T-0068 rationale). Allocation
        must RAISE, not fall back to a local-only guess. This test locks that
        decision: a future well-meaning "offline-fallback" change trips it and
        has to make its own safety argument (T-1939: fail-soft was considered and
        rejected — it trades collision-safety for offline usability nobody asked
        for).
        """
        def _stub(argv, **kwargs):
            if argv[:3] == ["git", "fetch", "origin"]:
                return MagicMock(returncode=1, stdout="", stderr="fatal: network unreachable")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("tools._git_utils.subprocess.run", side_effect=_stub):
            with self.assertRaises(ticket.TicketError):
                ticket._next_ticket_id()


# ---------------------------------------------------------------------------
# Front-matter round-trip
# ---------------------------------------------------------------------------


class TestFrontMatterRoundTrip(unittest.TestCase):
    def test_parse_minimal_required_fields(self) -> None:
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-05-02T01:30:00Z\n"
            "requester: orchestrator\n"
            "target: recovery\n"
            "state: inbox\n"
            "---\n"
            "\n"
            "# title\n"
        )
        fields, body = ticket.parse_front_matter(text)
        self.assertEqual(fields["id"], "T-0001")
        self.assertEqual(fields["state"], "inbox")
        self.assertNotIn("parent_ticket", fields)
        self.assertTrue(body.startswith("# title"))

    def test_parse_with_lists_and_null(self) -> None:
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-05-02T01:30:00Z\n"
            "requester: orchestrator\n"
            "target: recovery\n"
            "state: inbox\n"
            "parent_ticket: null\n"
            "depends_on: [T-0001, T-0002]\n"
            "labels: [a, b]\n"
            "stale_after: 4h\n"
            "---\n"
        )
        fields, _ = ticket.parse_front_matter(text)
        self.assertIsNone(fields["parent_ticket"])
        self.assertEqual(fields["depends_on"], ["T-0001", "T-0002"])
        self.assertEqual(fields["labels"], ["a", "b"])
        self.assertEqual(fields["stale_after"], "4h")

    def test_parse_with_empty_list(self) -> None:
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-05-02T01:30:00Z\n"
            "requester: orchestrator\n"
            "target: recovery\n"
            "state: inbox\n"
            "depends_on: []\n"
            "---\n"
        )
        fields, _ = ticket.parse_front_matter(text)
        self.assertEqual(fields["depends_on"], [])

    def test_serialize_preserves_field_order(self) -> None:
        fields = {
            "id": "T-0001",
            "created": "2026-05-02T01:30:00Z",
            "requester": "orchestrator",
            "target": "recovery",
            "state": "inbox",
            "parent_ticket": None,
            "depends_on": [],
            "labels": ["a", "b"],
            "stale_after": "4h",
        }
        out = ticket.serialize_front_matter(fields)
        # First line is ---, second is `id:`, then in canonical order
        lines = out.splitlines()
        self.assertEqual(lines[0], "---")
        keys = []
        for line in lines[1:-1]:
            keys.append(line.split(":", 1)[0])
        self.assertEqual(
            keys,
            [
                "id",
                "created",
                "requester",
                "target",
                "state",
                "parent_ticket",
                "depends_on",
                "labels",
                "stale_after",
            ],
        )

    def test_state_mutation_round_trip(self) -> None:
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-05-02T01:30:00Z\n"
            "requester: orchestrator\n"
            "target: recovery\n"
            "state: inbox\n"
            "parent_ticket: null\n"
            "depends_on: [T-0002]\n"
            "labels: [foo]\n"
            "---\n"
            "\n"
            "# t\n"
        )
        fields, body = ticket.parse_front_matter(text)
        fields["state"] = "active"
        round_tripped = ticket.serialize_front_matter(fields) + "\n" + body
        fields2, body2 = ticket.parse_front_matter(round_tripped)
        self.assertEqual(fields2["state"], "active")
        self.assertEqual(fields2["id"], fields["id"])
        self.assertEqual(fields2["depends_on"], fields["depends_on"])
        self.assertEqual(fields2["labels"], fields["labels"])
        self.assertIsNone(fields2["parent_ticket"])
        self.assertEqual(body2, body)

    def test_missing_required_field_refused(self) -> None:
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-05-02T01:30:00Z\n"
            "requester: orchestrator\n"
            "target: recovery\n"
            "---\n"
        )
        with self.assertRaises(ticket.TicketError):
            ticket.parse_front_matter(text)

    def test_missing_opening_delimiter_refused(self) -> None:
        with self.assertRaises(ticket.TicketError):
            ticket.parse_front_matter("id: T-0001\n")


# ---------------------------------------------------------------------------
# T-3440 / ORG-089: appetite + DROPPED substate frontmatter round-trip.
# ---------------------------------------------------------------------------


class TestAppetiteFieldRoundTrip(unittest.TestCase):
    """Every new ORG-089 frontmatter field survives a full
    parse_front_matter → serialize_front_matter → parse round-trip, and
    renders in FRONT_MATTER_FIELD_ORDER position (not the unknown-key tail)."""

    def _roundtrip_fields(self, extra_lines: str) -> dict:
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-09-06T01:30:00Z\n"
            "requester: orchestrator\n"
            "target: metis\n"
            "state: active\n"
            f"{extra_lines}"
            "---\n"
            "\n"
            "# t\n"
        )
        fields, body = ticket.parse_front_matter(text)
        serialized = ticket.serialize_front_matter(fields)
        fields2, _ = ticket.parse_front_matter(serialized + "\n# t\n")
        return fields2

    def test_appetite_bet_ref_round_trip(self) -> None:
        fields2 = self._roundtrip_fields("appetite: 7\nbet_ref: ORG-089\n")
        # appetite is stored as the raw scalar string on the generic path;
        # normalization to int is the validator's job, not the parser's.
        self.assertEqual(fields2["appetite"], "7")
        self.assertEqual(fields2["bet_ref"], "ORG-089")

    def test_substate_appetite_overrun_round_trip(self) -> None:
        fields2 = self._roundtrip_fields("substate: appetite-overrun\n")
        self.assertEqual(fields2["substate"], "appetite-overrun")

    def test_dropped_disposition_round_trip(self) -> None:
        fields2 = self._roundtrip_fields(
            "substate: dropped\n"
            'dropped_reason: bet no longer worth it at score 3\n'
            "dropped_from_state: active\n"
        )
        self.assertEqual(fields2["substate"], "dropped")
        self.assertEqual(fields2["dropped_reason"], "bet no longer worth it at score 3")
        self.assertEqual(fields2["dropped_from_state"], "active")

    def test_new_fields_render_in_field_order_not_unknown_tail(self) -> None:
        # A field present in FRONT_MATTER_FIELD_ORDER renders in its canonical
        # slot; the unknown-key fallback appends AFTER the closing sentinel would
        # be, so an int appetite line must appear before any stray key.
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-09-06T01:30:00Z\n"
            "requester: orchestrator\n"
            "target: metis\n"
            "state: active\n"
            "appetite: 5\n"
            "stray_key: x\n"
            "---\n"
            "\n# t\n"
        )
        fields, _ = ticket.parse_front_matter(text)
        serialized = ticket.serialize_front_matter(fields)
        self.assertLess(
            serialized.index("appetite: 5"),
            serialized.index("stray_key: x"),
            "known ORG-089 field must render before unknown-key tail",
        )


# ---------------------------------------------------------------------------
# List round-trip: comma-bearing items (T-0690)
# ---------------------------------------------------------------------------


class TestListRoundTripCommaInItem(unittest.TestCase):
    """Quote-aware serialize/parse: list items containing commas must survive
    a full _render_value → _parse_value round-trip intact.

    TDD: these tests are written before the fix; they must fail against the
    unfixed code and pass after.
    """

    def _roundtrip(self, items: list) -> list:
        rendered = ticket._render_value("ratifiable_files", items)
        return ticket._parse_value("ratifiable_files", rendered)

    # (a) Single item with a comma — the ADR-210 example from T-0690 brief
    # (using innocuous path text; no HERMES trigger bytes here).
    def test_single_item_with_comma_roundtrips(self) -> None:
        path = (
            "echo-prismatic:adr/ADR-210 — TTS audio routing — "
            "producer publishes raw PCM to bus, consumer plays locally.md"
        )
        result = self._roundtrip([path])
        self.assertEqual(result, [path])

    # (b) Format-stability: comma-free list renders byte-identical to today.
    def test_comma_free_list_format_stable(self) -> None:
        items = ["a", "b", "c"]
        rendered = ticket._render_value("ratifiable_files", items)
        self.assertEqual(rendered, "[a, b, c]")

    # (c) Two items, one comma-bearing — both preserved.
    def test_multi_item_one_comma_bearing(self) -> None:
        items = ["clean-path:foo.md", "repo:bar, baz.md"]
        result = self._roundtrip(items)
        self.assertEqual(result, items)

    # (d) Item containing a double-quote — escaping tested.
    def test_item_with_double_quote_roundtrips(self) -> None:
        items = ['say "hi" here']
        result = self._roundtrip(items)
        self.assertEqual(result, items)

    # (e) Integration-style: full frontmatter parse → serialize → re-parse
    # with a comma-bearing ratifiable_files entry — one element preserved.
    def test_frontmatter_roundtrip_comma_in_ratifiable_files(self) -> None:
        path = (
            "pantheon-config:adr/ADR-210 — audio routing — "
            "producer sends to bus, consumer plays.md"
        )
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-05-29T00:00:00Z\n"
            "requester: orchestrator\n"
            "target: operator\n"
            "state: inbox\n"
            "ratifiable_files: [" + ticket._render_value("ratifiable_files", [path])[1:-1] + "]\n"
            "---\n"
            "\n"
            "# title\n"
        )
        fields, _ = ticket.parse_front_matter(text)
        self.assertEqual(fields["ratifiable_files"], [path])
        serialized = ticket.serialize_front_matter(fields)
        fields2, _ = ticket.parse_front_matter(serialized + "\n# title\n")
        self.assertEqual(fields2["ratifiable_files"], [path])

    # (e2) Full-text frontmatter with pre-rendered comma-quoted entry.
    def test_parse_quoted_item_in_raw_frontmatter(self) -> None:
        path = (
            "pantheon-config:adr/ADR-210 — audio, routing.md"
        )
        # Build the raw frontmatter text as an external YAML writer would:
        # the item is already double-quoted because it contains a comma.
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-05-29T00:00:00Z\n"
            "requester: orchestrator\n"
            "target: operator\n"
            "state: inbox\n"
            'ratifiable_files: ["pantheon-config:adr/ADR-210 — audio, routing.md"]\n'
            "---\n"
            "\n"
            "# title\n"
        )
        fields, _ = ticket.parse_front_matter(text)
        self.assertEqual(fields["ratifiable_files"], [path])


# ---------------------------------------------------------------------------
# Transition-log append
# ---------------------------------------------------------------------------


class TestTransitionLogAppend(unittest.TestCase):
    def test_append_to_existing_log(self) -> None:
        body = (
            "# t\n\n## Ask\n\n## Context\n\n## Transition log\n\n"
            "- 2026-05-02T01:30:00Z [orchestrator] filed in inbox\n"
        )
        new_body = ticket.append_transition(body, "recovery", "claimed", "moving to active")
        self.assertIn("filed in inbox", new_body)
        # New entry must be after the existing one.
        idx_old = new_body.index("filed in inbox")
        idx_new = new_body.index("moving to active")
        self.assertLess(idx_old, idx_new)

    def test_entry_format_matches_spec(self) -> None:
        body = "## Transition log\n\n- 2026-05-02T01:30:00Z [orchestrator] filed\n"
        new_body = ticket.append_transition(body, "recovery", "claimed", "moving to active")
        last_line = [
            ln for ln in new_body.splitlines() if ln.startswith("- ")
        ][-1]
        # Format (T-2203): - <iso> [<actor>] <verb>[ @<sha>]; <detail>
        # The sha is optional in the pattern because the stamp is fail-open —
        # outside a git repo it yields None and the line degrades to the
        # pre-T-2203 shape rather than blocking the transition.
        import re
        self.assertRegex(
            last_line,
            r"^- \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[recovery\] claimed"
            r"(?: @[0-9a-f]{7,40})?; moving to active$",
        )

    def test_stamp_follows_the_verb_not_precedes_it(self) -> None:
        """T-2203: placement is load-bearing, not cosmetic.

        activity_digest.py matches `\\] resolved` and weekly_ticket_summary.py
        matches `\\[(?P<actor>[^\\]]+)\\]\\s+(?P<action>resolved|...)`. Both
        anchor the verb directly to the actor bracket, so a sha inserted
        BETWEEN them makes each regex match nothing — and the visible symptom
        is a digest reporting zero resolves, which reads as a quiet week
        rather than as a broken parser. Pin the order explicitly.
        """
        body = "## Transition log\n\n- 2026-05-02T01:30:00Z [orchestrator] filed\n"
        new_body = ticket.append_transition(body, "recovery", "resolved", "done")
        last_line = [ln for ln in new_body.splitlines() if ln.startswith("- ")][-1]
        if "@" not in last_line:
            self.skipTest("no git HEAD available; stamp is fail-open here")
        self.assertLess(
            last_line.index("resolved"), last_line.index("@"),
            "sha must follow the verb; before it breaks both digest regexes",
        )
        # And prove it against the real downstream patterns, not a paraphrase.
        import re
        self.assertRegex(last_line, r"- \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[.*?\] resolved")
        self.assertRegex(
            last_line,
            r"^- (?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+\[(?P<actor>[^\]]+)\]\s+"
            r"(?P<action>resolved|resolve|close-action|close-noodle)\b",
        )

    def test_stamp_uses_the_published_parent_not_local_head(self) -> None:
        """T-2360: stamp `merge-base HEAD origin/main`, never local HEAD.

        This repo's pre-push hook REBASES onto origin/main, and a resolve is
        normally made while the branch is ahead (commit the code, then resolve
        the ticket). So a HEAD stamp named the one commit guaranteed to be
        rewritten by the very next push — 22 of 33 unreachable citations in the
        repo came from here, regenerating one per ticket operation, which is why
        T-2272's lint could never converge.

        Asserted on the COMMAND, not just the return value: a test that only
        checked the string would pass against an implementation that still
        stamped HEAD and happened to be on origin/main at the time.
        """
        import subprocess as _sp
        calls: list[tuple] = []

        def fake_git(*args: str, **_kw):
            calls.append(args)
            if args[0] == "merge-base":
                return _sp.CompletedProcess(args, 0, stdout="b" * 40, stderr="")
            if args[:2] == ("rev-parse", "--short"):
                return _sp.CompletedProcess(args, 0, stdout="bbbbbbb\n", stderr="")
            return _sp.CompletedProcess(args, 1, stdout="", stderr="")

        with unittest.mock.patch.object(ticket, "_git", side_effect=fake_git):
            sha = ticket._published_parent_sha()

        self.assertEqual(sha, "bbbbbbb")
        self.assertIn(("merge-base", "HEAD", "origin/main"), calls)
        self.assertNotIn(
            ("rev-parse", "--short", "HEAD"), calls,
            "must not stamp local HEAD — the pre-push rebase rewrites exactly it",
        )

    def test_published_parent_does_not_fall_back_to_head(self) -> None:
        """When origin/main is absent (fresh clone, no remote, test fixture) the
        stamp is OMITTED, not silently taken from HEAD.

        A HEAD fallback would reintroduce the rewritable stamp this replaces, in
        precisely the situation nobody inspects. Fail-open means *no sha rather
        than a wrong one* — the line degrades to the pre-T-2203 shape.
        """
        import subprocess as _sp
        calls: list[tuple] = []

        def no_merge_base(*args: str, **_kw):
            calls.append(args)
            return _sp.CompletedProcess(args, 1, stdout="", stderr="no upstream")

        with unittest.mock.patch.object(ticket, "_git", side_effect=no_merge_base):
            self.assertIsNone(ticket._published_parent_sha())
        self.assertNotIn(("rev-parse", "--short", "HEAD"), calls)

    def test_stamp_is_fail_open(self) -> None:
        """T-2203: a rev-parse failure must degrade the line, never raise.

        Recording provenance must not be able to block the state change it
        describes.
        """
        with unittest.mock.patch.object(ticket, "_git", side_effect=OSError("no git")):
            self.assertIsNone(ticket._published_parent_sha())
        body = "## Transition log\n\n- 2026-05-02T01:30:00Z [orchestrator] filed\n"
        with unittest.mock.patch.object(ticket, "_published_parent_sha", return_value=None):
            new_body = ticket.append_transition(body, "recovery", "claimed", "d")
        last_line = [ln for ln in new_body.splitlines() if ln.startswith("- ")][-1]
        self.assertNotIn("@", last_line)
        self.assertTrue(last_line.endswith("[recovery] claimed; d"))

    def test_append_with_no_detail(self) -> None:
        body = "## Transition log\n\n- 2026-05-02T01:30:00Z [orchestrator] filed\n"
        new_body = ticket.append_transition(body, "recovery", "unblocked", None)
        last_line = [
            ln for ln in new_body.splitlines() if ln.startswith("- ")
        ][-1]
        # No semicolon when no detail (the sha stamp must not introduce one).
        self.assertNotIn(";", last_line)
        import re
        self.assertRegex(last_line, r"\[recovery\] unblocked(?: @[0-9a-f]{7,40})?$")

    def test_section_header_preserved(self) -> None:
        body = "# t\n\n## Transition log\n\n- 2026-05-02T01:30:00Z [orchestrator] filed\n"
        new_body = ticket.append_transition(body, "recovery", "claimed", None)
        self.assertIn("## Transition log", new_body)

    def test_missing_section_header_refused(self) -> None:
        body = "# t\n\nNo log section.\n"
        with self.assertRaises(ticket.TicketError):
            ticket.append_transition(body, "recovery", "claimed", None)


# ---------------------------------------------------------------------------
# HERMES guard on file content
# ---------------------------------------------------------------------------


class TestHermesGuardOnFileContent(unittest.TestCase):
    """Source-time concat: trigger bytes assembled at runtime; this source
    file never carries the contiguous 9-byte sequence. See module docstring.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clean_ticket_passes(self) -> None:
        p = self.dir / "T-0001.md"
        p.write_text("# clean ticket\n\nNo trigger anywhere.\n")
        ok, detail = ticket._hermes_scan_file(p)
        self.assertTrue(ok)
        self.assertIsNone(detail)

    def test_escaped_form_passes(self) -> None:
        # Discipline form: literal backslash between S and .
        p = self.dir / "T-0002.md"
        p.write_text("Reference: HERMES\\.md is the trigger phrase we avoid.\n")
        ok, detail = ticket._hermes_scan_file(p)
        self.assertTrue(ok, f"escaped form should pass; got detail={detail!r}")

    def test_unescaped_form_in_body_refused(self) -> None:
        p = self.dir / "T-0003.md"
        p.write_text("Line 1\nThis line has " + TRIGGER + " unescaped.\nLine 3\n")
        ok, detail = ticket._hermes_scan_file(p)
        self.assertFalse(ok)
        self.assertIsNotNone(detail)
        self.assertIn("T-0003.md", detail)
        self.assertIn(":2", detail)

    def test_unescaped_in_front_matter_refused(self) -> None:
        # Trigger embedded in a label string — even within front matter, raw
        # bytes are scanned identically.
        p = self.dir / "T-0004.md"
        p.write_text(
            "---\n"
            "id: T-0004\n"
            "labels: [bad-" + TRIGGER + "-label]\n"
            "---\n"
            "# t\n"
        )
        ok, detail = ticket._hermes_scan_file(p)
        self.assertFalse(ok)
        self.assertIn("T-0004.md", detail)


# ---------------------------------------------------------------------------
# HERMES guard on commit message
# ---------------------------------------------------------------------------


class TestHermesGuardOnCommitMessage(unittest.TestCase):
    def test_clean_subject_passes(self) -> None:
        msg = "tickets: block T-0001 — waiting on review\n"
        self.assertTrue(ticket._hermes_scan_message(msg))

    def test_escaped_in_block_reason_passes(self) -> None:
        msg = "tickets: block T-0001 — see HERMES\\.md research\n"
        self.assertTrue(ticket._hermes_scan_message(msg))

    def test_unescaped_in_block_reason_refused(self) -> None:
        # Source-time concat per the module docstring discipline.
        msg = "tickets: block T-0001 — refers to " + TRIGGER + "\n"
        self.assertFalse(ticket._hermes_scan_message(msg))


# ---------------------------------------------------------------------------
# Lifecycle invariants — state-mismatch refusals
# ---------------------------------------------------------------------------


class TestLifecycleStateMismatch(unittest.TestCase):
    """State-mismatch refusals do not invoke git. We patch TICKETS_ROOT to a
    tmpdir and assert each invalid transition raises TicketError before any
    subprocess.run call.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        # `_git`'s default cwd= is `_git_utils.REPO_ROOT`; pin it to tmpdir so
        # tests don't rely on the real repo as fallback.
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        # subprocess.run must NOT be called when refusal is correct. `_git`
        # lives in tools._git_utils post-extraction.
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()
        self.mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    def tearDown(self) -> None:
        self._tickets_patch.stop()
        self._gu_repo_patch.stop()
        self._sub_patch.stop()
        self._tmp.cleanup()

    def _seed(self, tid: str, state: str) -> None:
        _write_ticket(self.root / state / f"{tid}.md", tid=tid, state=state)

    def test_claim_from_active_refuses(self) -> None:
        self._seed("T-0001", "active")
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket._do_transition(
                tid="T-0001",
                expected_state="inbox",
                new_state="active",
                verb="claim",
                detail="moving to active",
            )
        self.assertIn("state-mismatch", str(ctx.exception))
        self.mock_run.assert_not_called()

    def test_claim_from_done_refuses(self) -> None:
        # ORG-019: blocked/ retired; test done-state refusal instead.
        self._seed("T-0001", "done")
        with self.assertRaises(ticket.TicketError):
            ticket._do_transition("T-0001", "inbox", "active", "claim", "x")
        self.mock_run.assert_not_called()

    def test_resolve_from_inbox_refuses(self) -> None:
        # resolve requires active/, not inbox/.
        self._seed("T-0001", "inbox")
        with self.assertRaises(ticket.TicketError):
            ticket._do_transition("T-0001", "active", "done", "resolve", None)
        self.mock_run.assert_not_called()

    def test_unknown_ticket_refused(self) -> None:
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket._do_transition("T-9999", "inbox", "active", "claim", None)
        self.assertIn("ticket not found", str(ctx.exception))


# ---------------------------------------------------------------------------
# `list` filtering (read-only, no git)
# ---------------------------------------------------------------------------


class TestListFiltering(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        # `_git` lives in tools._git_utils post-extraction; if any list-path
        # logic accidentally invoked git, it would route through here.
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()
        # T-3020 BT3: cmd_list's default --sort priority now writes a
        # telemetry line per invocation (ORG-078 §4) — redirect it off the
        # real user homedir so these tests don't leave side effects there.
        self._health_patch = patch.object(
            ticket, "PRIORITY_LIST_HEALTH_PATH", self.root / "priority_list_health.jsonl"
        )
        self._health_patch.start()
        # Seed: 1 inbox/recovery/foo, 1 active/recovery/bar, 1 active/science/baz, 1 done/recovery/foo
        _write_ticket(
            self.root / "inbox" / "T-0001.md",
            tid="T-0001",
            state="inbox",
            target="recovery",
            labels=["foo"],
        )
        _write_ticket(
            self.root / "active" / "T-0002.md",
            tid="T-0002",
            state="active",
            target="recovery",
            labels=["bar"],
        )
        _write_ticket(
            self.root / "active" / "T-0003.md",
            tid="T-0003",
            state="active",
            target="specialist",
            labels=["baz"],
        )
        _write_ticket(
            self.root / "done" / "T-0004.md",
            tid="T-0004",
            state="done",
            target="recovery",
            labels=["foo"],
        )

    def tearDown(self) -> None:
        self._tickets_patch.stop()
        self._sub_patch.stop()
        self._health_patch.stop()
        self._tmp.cleanup()

    def _run_list(self, **kwargs) -> int:
        ns = MagicMock()
        ns.state = kwargs.get("state")
        ns.target = kwargs.get("target")
        ns.label = kwargs.get("label")
        # T-2720: MagicMock auto-vivifies unset attrs as truthy, so this must
        # be set explicitly or every pre-existing list test would silently
        # run with --show-unblocks ON.
        ns.show_unblocks = kwargs.get("show_unblocks", False)
        # T-3020 BT3: same MagicMock auto-vivification footgun — `sort`/
        # `explain_priority` must be set explicitly, or every pre-existing
        # list test would silently run in --explain-priority mode (an
        # auto-vivified MagicMock is truthy) instead of a normal listing.
        ns.sort = kwargs.get("sort", "priority")
        ns.explain_priority = kwargs.get("explain_priority", None)
        return ticket.cmd_list(ns)

    def test_list_no_filter_returns_all(self) -> None:
        with patch("builtins.print") as mock_print:
            rc = self._run_list()
        self.assertEqual(rc, 0)
        printed = "\n".join(call.args[0] for call in mock_print.call_args_list)
        for tid in ("T-0001", "T-0002", "T-0003", "T-0004"):
            self.assertIn(tid, printed)

    def test_list_state_filter(self) -> None:
        with patch("builtins.print") as mock_print:
            self._run_list(state="active")
        printed = "\n".join(call.args[0] for call in mock_print.call_args_list)
        self.assertIn("T-0002", printed)
        self.assertIn("T-0003", printed)
        self.assertNotIn("T-0001", printed)
        self.assertNotIn("T-0004", printed)

    def test_list_target_filter(self) -> None:
        with patch("builtins.print") as mock_print:
            self._run_list(target="recovery")
        printed = "\n".join(call.args[0] for call in mock_print.call_args_list)
        self.assertIn("T-0001", printed)
        self.assertIn("T-0002", printed)
        self.assertIn("T-0004", printed)
        self.assertNotIn("T-0003", printed)

    def test_list_label_filter(self) -> None:
        with patch("builtins.print") as mock_print:
            self._run_list(label="foo")
        printed = "\n".join(call.args[0] for call in mock_print.call_args_list)
        self.assertIn("T-0001", printed)
        self.assertIn("T-0004", printed)
        self.assertNotIn("T-0002", printed)
        self.assertNotIn("T-0003", printed)

    def test_list_no_commit_invoked(self) -> None:
        self._run_list()
        # subprocess.run is mocked; assert no `git` invocation occurred.
        for call in self.mock_run.call_args_list:
            argv = call.args[0]
            self.assertNotEqual(argv[0], "git", f"list invoked git: {argv!r}")

    def test_list_show_unblocks_off_by_default(self) -> None:
        """No --show-unblocks: no 'unblocks' annotation anywhere (T-2720)."""
        with patch("builtins.print") as mock_print:
            self._run_list()
        printed = "\n".join(call.args[0] for call in mock_print.call_args_list)
        self.assertNotIn("unblocks", printed)

    def test_list_show_unblocks_renders_annotation(self) -> None:
        # T-0002 depends_on nothing here, but T-0001 unblocks nothing either
        # in this fixture — add a dep edge so there's something to render.
        _write_ticket(
            self.root / "inbox" / "T-0005.md", tid="T-0005", state="inbox",
            target="recovery", depends_on=["T-0001"],
        )
        with patch("builtins.print") as mock_print:
            self._run_list(show_unblocks=True)
        printed = "\n".join(call.args[0] for call in mock_print.call_args_list)
        # T-0001's row must show it unblocks T-0005.
        t0001_line = next(
            line for line in printed.splitlines() if line.startswith("T-0001")
        )
        self.assertIn("unblocks: T-0005", t0001_line)

    def test_list_show_unblocks_silent_when_empty(self) -> None:
        """A ticket with zero dependents gets no annotation, even with the flag on."""
        with patch("builtins.print") as mock_print:
            self._run_list(show_unblocks=True)
        printed = "\n".join(call.args[0] for call in mock_print.call_args_list)
        # None of the seeded tickets (T-0001..T-0004) have dependents.
        self.assertNotIn("unblocks:", printed)


class TestReadyFiltering(unittest.TestCase):
    """`ticket.py ready` — inbox/active, no depends_on, not_before passed (T-0736)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()
        # T-0001 inbox/orchestrator, clean → ready
        _write_ticket(self.root / "inbox" / "T-0001.md", tid="T-0001", state="inbox", target="orchestrator")
        # T-0002 active/orchestrator, clean → ready
        _write_ticket(self.root / "active" / "T-0002.md", tid="T-0002", state="active", target="orchestrator")
        # T-0003 inbox/orchestrator, dep gate → NOT ready
        _write_ticket(
            self.root / "inbox" / "T-0003.md", tid="T-0003", state="inbox",
            target="orchestrator", depends_on=["T-0002"],
        )
        # T-0004 done/orchestrator → NOT ready (state)
        _write_ticket(self.root / "done" / "T-0004.md", tid="T-0004", state="done", target="orchestrator")
        # T-0005 inbox/orchestrator, future not_before → NOT ready (calendar gate)
        _write_ticket(
            self.root / "inbox" / "T-0005.md", tid="T-0005", state="inbox",
            target="orchestrator", not_before="2099-01-01T00:00:00Z",
        )
        # T-0006 inbox/orchestrator, past not_before → ready
        _write_ticket(
            self.root / "inbox" / "T-0006.md", tid="T-0006", state="inbox",
            target="orchestrator", not_before="2020-01-01T00:00:00Z",
        )
        # T-0007 inbox/specialist, clean → ready but other-target (for --target filter)
        _write_ticket(self.root / "inbox" / "T-0007.md", tid="T-0007", state="inbox", target="specialist")
        # T-0008 inbox/orchestrator, clean, labelled → ready, but hideable via --exclude-label (T-3540)
        _write_ticket(
            self.root / "inbox" / "T-0008.md", tid="T-0008", state="inbox",
            target="orchestrator", labels=["interactive-required", "cutover"],
        )
        # T-0009 inbox/metis, clean, labelled → the ONLY metis ticket, so excluding its
        # label empties the metis scope and must exercise the label-excluded diagnostic (T-3540)
        _write_ticket(
            self.root / "inbox" / "T-0009.md", tid="T-0009", state="inbox",
            target="metis", labels=["interactive-required"],
        )

    def tearDown(self) -> None:
        self._tickets_patch.stop()
        self._sub_patch.stop()
        self._tmp.cleanup()

    def _run_ready(self, **kwargs) -> str:
        ns = MagicMock()
        ns.target = kwargs.get("target")
        # T-2720: see TestListFiltering._run_list — same MagicMock gotcha.
        ns.show_unblocks = kwargs.get("show_unblocks", False)
        # T-3540: cmd_ready reads args.exclude_label; a bare MagicMock attr
        # iterates empty by luck, but set it explicitly so the helper is honest.
        ns.exclude_label = kwargs.get("exclude_label")
        with patch("builtins.print") as mock_print:
            rc = ticket.cmd_ready(ns)
        self.assertEqual(rc, 0)
        return "\n".join(call.args[0] for call in mock_print.call_args_list)

    def test_ready_includes_clean_inbox_and_active(self) -> None:
        printed = self._run_ready()
        self.assertIn("T-0001", printed)
        self.assertIn("T-0002", printed)

    def test_ready_excludes_dep_gated(self) -> None:
        self.assertNotIn("T-0003", self._run_ready())

    def test_ready_excludes_done_state(self) -> None:
        self.assertNotIn("T-0004", self._run_ready())

    def test_ready_excludes_future_not_before(self) -> None:
        self.assertNotIn("T-0005", self._run_ready())

    def test_ready_includes_past_not_before(self) -> None:
        self.assertIn("T-0006", self._run_ready())

    def test_ready_target_filter(self) -> None:
        printed = self._run_ready(target="orchestrator")
        self.assertIn("T-0001", printed)
        self.assertNotIn("T-0007", printed)  # specialist filtered out

    def test_ready_no_commit_invoked(self) -> None:
        self._run_ready()
        for call in self.mock_run.call_args_list:
            argv = call.args[0]
            self.assertNotEqual(argv[0], "git", f"ready invoked git: {argv!r}")

    def test_ready_show_unblocks_off_by_default(self) -> None:
        printed = self._run_ready()
        self.assertNotIn("unblocks", printed)

    def test_ready_show_unblocks_renders_annotation(self) -> None:
        # T-0003 (dep-gated, not itself ready) depends_on T-0002, so T-0002's
        # ready row must show what clearing it unblocks.
        printed = self._run_ready(show_unblocks=True)
        t0002_line = next(
            line for line in printed.splitlines() if line.startswith("T-0002")
        )
        self.assertIn("unblocks: T-0003", t0002_line)

    # --- T-3540: --exclude-label ---
    def test_ready_includes_labelled_by_default(self) -> None:
        # No --exclude-label → a labelled-but-otherwise-ready ticket still shows.
        self.assertIn("T-0008", self._run_ready())

    def test_ready_exclude_label_hides_matching(self) -> None:
        printed = self._run_ready(exclude_label=["interactive-required"])
        self.assertNotIn("T-0008", printed)   # carries the excluded label → hidden
        self.assertIn("T-0001", printed)      # unlabelled → still ready

    def test_ready_exclude_label_multiple_values(self) -> None:
        # Repeatable: any match hides. T-0008 has 'cutover'; hide on that too.
        self.assertNotIn("T-0008", self._run_ready(exclude_label=["nope", "cutover"]))

    def test_ready_exclude_label_nonmatching_keeps_all(self) -> None:
        # Excluding a label nothing carries changes nothing.
        self.assertIn("T-0008", self._run_ready(exclude_label=["no-such-label"]))

    def test_ready_exclude_label_diagnostic_not_genuinely_empty(self) -> None:
        # metis has exactly one ready ticket (T-0009) and it is label-excluded,
        # so the empty result must say WHY (label-excluded), NOT "genuinely EMPTY"
        # (the T-2220 state-why-empty contract).
        printed = self._run_ready(target="metis", exclude_label=["interactive-required"])
        self.assertIn("label-excluded", printed)
        self.assertIn("T-0009", printed)
        self.assertNotIn("genuinely EMPTY", printed)


class TestPrime(unittest.TestCase):
    """`ticket.py prime` — JSON project-state bootstrap (T-0737)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()
        _write_ticket(self.root / "inbox" / "T-0001.md", tid="T-0001", state="inbox", target="orchestrator")
        _write_ticket(self.root / "active" / "T-0002.md", tid="T-0002", state="active", target="orchestrator")
        _write_ticket(
            self.root / "inbox" / "T-0003.md", tid="T-0003", state="inbox",
            target="orchestrator", depends_on=["T-0002"],
        )
        _write_ticket(
            self.root / "inbox" / "T-0005.md", tid="T-0005", state="inbox",
            target="orchestrator", not_before="2099-01-01T00:00:00Z",
        )
        _write_ticket(self.root / "done" / "T-0004.md", tid="T-0004", state="done", target="orchestrator")
        _write_ticket(self.root / "done" / "T-0009.md", tid="T-0009", state="done", target="orchestrator")

    def tearDown(self) -> None:
        self._tickets_patch.stop()
        self._sub_patch.stop()
        self._tmp.cleanup()

    def _run_prime(self, **kwargs) -> dict:
        ns = MagicMock()
        ns.target = kwargs.get("target")
        ns.done_limit = kwargs.get("done_limit", 15)
        with patch("builtins.print") as mock_print:
            rc = ticket.cmd_prime(ns)
        self.assertEqual(rc, 0)
        # cmd_prime emits one print of json.dumps — parse it (asserts valid JSON).
        payload = "\n".join(call.args[0] for call in mock_print.call_args_list)
        return json.loads(payload)

    def _ids(self, entries) -> set:
        return {e["id"] for e in entries}

    def test_prime_categorizes_ready_blocked_active(self) -> None:
        p = self._run_prime()
        self.assertIn("T-0001", self._ids(p["ready"]))
        self.assertIn("T-0002", self._ids(p["ready"]))      # active + unblocked → also ready
        self.assertIn("T-0003", self._ids(p["blocked"]))    # dep gate
        self.assertIn("T-0005", self._ids(p["blocked"]))    # future not_before
        self.assertIn("T-0002", self._ids(p["active"]))     # active state

    def test_prime_recently_done_sorted_desc(self) -> None:
        p = self._run_prime()
        done_ids = [e["id"] for e in p["recently_done"]]
        self.assertEqual(done_ids, ["T-0009", "T-0004"])  # id desc

    def test_prime_done_limit(self) -> None:
        p = self._run_prime(done_limit=1)
        self.assertEqual([e["id"] for e in p["recently_done"]], ["T-0009"])

    def test_prime_counts(self) -> None:
        p = self._run_prime()
        self.assertEqual(p["counts"]["done_total"], 2)
        self.assertEqual(p["counts"]["ready"], len(p["ready"]))

    def test_prime_target_filter_excludes_other(self) -> None:
        _write_ticket(self.root / "inbox" / "T-0007.md", tid="T-0007", state="inbox", target="specialist")
        p = self._run_prime(target="orchestrator")
        all_non_done = self._ids(p["ready"]) | self._ids(p["blocked"]) | self._ids(p["active"])
        self.assertNotIn("T-0007", all_non_done)

    def test_prime_no_commit_invoked(self) -> None:
        self._run_prime()
        for call in self.mock_run.call_args_list:
            argv = call.args[0]
            self.assertNotEqual(argv[0], "git", f"prime invoked git: {argv!r}")

    def test_prime_briefs_carry_unblocks(self) -> None:
        """T-2720: every brief (ready/blocked/active/recently_done) gets an
        'unblocks' key unconditionally — no flag needed, it's the JSON surface."""
        p = self._run_prime()
        # T-0002 (active, unblocked → also ready) is depended on by T-0003.
        ready_by_id = {e["id"]: e for e in p["ready"]}
        self.assertIn("unblocks", ready_by_id["T-0001"])
        self.assertEqual(ready_by_id["T-0002"]["unblocks"], ["T-0003"])
        self.assertEqual(ready_by_id["T-0001"]["unblocks"], [])
        # blocked/active/recently_done briefs also carry the key.
        blocked_by_id = {e["id"]: e for e in p["blocked"]}
        self.assertIn("unblocks", blocked_by_id["T-0003"])
        done_by_id = {e["id"]: e for e in p["recently_done"]}
        self.assertIn("unblocks", done_by_id["T-0004"])


class TestComputeUnblocks(unittest.TestCase):
    """`compute_unblocks` — derived reverse index of depends_on (T-2720).

    DERIVED-ON-READ from depends_on, never persisted to frontmatter — see
    module docstring on tools/ticket.py's compute_unblocks for rationale.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()

    def tearDown(self) -> None:
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def test_zero_dependents(self) -> None:
        _write_ticket(self.root / "inbox" / "T-0001.md", tid="T-0001", state="inbox")
        tickets = ticket._iter_all_tickets()
        self.assertEqual(ticket.compute_unblocks(tickets)["T-0001"], [])

    def test_single_dependent(self) -> None:
        _write_ticket(self.root / "inbox" / "T-0001.md", tid="T-0001", state="inbox")
        _write_ticket(
            self.root / "inbox" / "T-0002.md", tid="T-0002", state="inbox",
            depends_on=["T-0001"],
        )
        tickets = ticket._iter_all_tickets()
        out = ticket.compute_unblocks(tickets)
        self.assertEqual(out["T-0001"], ["T-0002"])
        self.assertEqual(out["T-0002"], [])

    def test_multiple_dependents_sorted(self) -> None:
        _write_ticket(self.root / "inbox" / "T-0001.md", tid="T-0001", state="inbox")
        _write_ticket(
            self.root / "active" / "T-0003.md", tid="T-0003", state="active",
            depends_on=["T-0001"],
        )
        _write_ticket(
            self.root / "inbox" / "T-0002.md", tid="T-0002", state="inbox",
            depends_on=["T-0001"],
        )
        tickets = ticket._iter_all_tickets()
        out = ticket.compute_unblocks(tickets)
        # Sorted, not insertion order — T-0002 filed after T-0003 in this
        # setUp but must sort before it.
        self.assertEqual(out["T-0001"], ["T-0002", "T-0003"])

    def test_dependent_counts_regardless_of_own_state(self) -> None:
        """A dependent in inbox, active, OR done still counts (T-2720 brief)."""
        _write_ticket(self.root / "inbox" / "T-0001.md", tid="T-0001", state="inbox")
        _write_ticket(
            self.root / "inbox" / "T-0002.md", tid="T-0002", state="inbox",
            depends_on=["T-0001"],
        )
        _write_ticket(
            self.root / "active" / "T-0003.md", tid="T-0003", state="active",
            depends_on=["T-0001"],
        )
        _write_ticket(
            self.root / "done" / "T-0004.md", tid="T-0004", state="done",
            depends_on=["T-0001"],
        )
        tickets = ticket._iter_all_tickets()
        out = ticket.compute_unblocks(tickets)
        self.assertEqual(out["T-0001"], ["T-0002", "T-0003", "T-0004"])

    def test_dependent_pointing_outside_loaded_set_does_not_crash(self) -> None:
        """depends_on referencing an id not in the scanned corpus is tolerated."""
        _write_ticket(
            self.root / "inbox" / "T-0002.md", tid="T-0002", state="inbox",
            depends_on=["T-9999"],
        )
        tickets = ticket._iter_all_tickets()
        out = ticket.compute_unblocks(tickets)
        self.assertEqual(out["T-9999"], ["T-0002"])
        self.assertEqual(out["T-0002"], [])


# ---------------------------------------------------------------------------
# Full file → claim → resolve git command sequence (subprocess mocked)
# ---------------------------------------------------------------------------


class TestFullLifecycleGitCommands(unittest.TestCase):
    """Drive cmd_file → cmd_claim → cmd_resolve in sequence with the git
    subprocess mocked. Captures argvs and asserts the canonical order
    pull → add → diff → commit → push for each transition. For transitions
    that involve `git mv`, the mv is the first git invocation, before
    pull. For `file`, no mv.
    """

    def setUp(self) -> None:
        # T-1943: the expected git-command sequences below include the
        # rev-parse that an UNCACHED _git_common_dir issues at mutation-lock
        # entry. Any earlier test that resolves it for real (e.g. the
        # test_git_utils_integ integration tests — the isolated polluter)
        # leaves the module-global cache warm, the rev-parse vanishes, and
        # these assertions fail under discover while passing standalone.
        # Reset the cache so this class is order-independent; T-1931
        # guarantees our mocked-subprocess runs cannot re-poison it for
        # later tests (garbage is never cached).
        _git_utils._git_common_dir_cache = None
        self.addCleanup(setattr, _git_utils, "_git_common_dir_cache", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        # Repo-root patch — _commit_and_push converts to relative paths.
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        # `_git`'s default cwd= reads `_git_utils.REPO_ROOT`; pin to tmpdir so
        # subprocess invocations (mocked) don't carry the real-repo cwd.
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._tickets_patch = patch.object(
            ticket, "TICKETS_ROOT", self.root / "tickets"
        )
        self._tickets_patch.start()
        # Recreate state dirs under the repo-relative location.
        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True, exist_ok=True)

        # Create a minimal README.md so table rebuild doesn't fail
        readme = self.root / "tickets" / "README.md"
        readme.write_text(
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
            "## Related\n\n"
            "- Link\n",
            encoding="utf-8",
        )

        # `_git` lives in tools._git_utils post-extraction.
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()

        def fake_run(argv, **kwargs):
            # `git mv` actually moves the file in the working tree so the
            # subsequent in-process read works. We emulate just enough.
            if argv[:2] == ["git", "mv"] and len(argv) >= 4:
                src = self.root / argv[2]
                dst = self.root / argv[3]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "add"] and "--" in argv:
                # Realistic refusal: real git fails with returncode=128 +
                # `fatal: pathspec ... did not match any files` when asked
                # to add a path that does not exist in the working tree.
                # The fixture must mirror that or it masks the post-mv
                # stale-path bug (T-0004).
                idx = argv.index("--")
                paths = argv[idx + 1 :]
                missing = [p for p in paths if not (self.root / p).exists()]
                if missing:
                    return MagicMock(
                        returncode=128,
                        stdout="",
                        stderr=(
                            "fatal: pathspec '"
                            + missing[0]
                            + "' did not match any files\n"
                        ),
                    )
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "diff"]:
                # Reflect what real git would report from the index. The
                # CLI invokes `git diff --cached --name-only --no-renames`;
                # `--no-renames` disables rename-detection coalescing, so a
                # `git mv old new` shows up as TWO entries (the deletion at
                # `old` and the addition at `new`) rather than a single
                # rename-entry that --name-only would report as just `new`.
                # The fixture must model that or it masks the latent
                # scope-guard bug T-0004's fix surfaces — this mock walks
                # the call history (rather than side-channel state) because
                # the same mock is shared across one test method.
                self.assertIn(
                    "--no-renames",
                    argv,
                    "scope guard must pass --no-renames to git diff so the "
                    "expected-set [old, new] matches what git reports for "
                    "renames; see T-0004 follow-up.",
                )
                staged_set: set[str] = set()
                for prior in self.mock_run.call_args_list:
                    pa = prior.args[0]
                    if pa[:2] == ["git", "mv"] and len(pa) >= 4:
                        staged_set.add(pa[2])
                        staged_set.add(pa[3])
                    elif pa[:2] == ["git", "add"] and "--" in pa:
                        i = pa.index("--")
                        for p in pa[i + 1 :]:
                            if (self.root / p).exists():
                                staged_set.add(p)
                    elif pa[:2] == ["git", "commit"]:
                        staged_set.clear()
                staged = sorted(staged_set)
                return MagicMock(
                    returncode=0,
                    stdout="\n".join(staged) + ("\n" if staged else ""),
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_run

    def tearDown(self) -> None:
        self._sub_patch.stop()
        self._tickets_patch.stop()
        self._gu_repo_patch.stop()
        self._repo_patch.stop()
        self._tmp.cleanup()

    def _git_kinds_after(self, n_before: int) -> list[str]:
        """Return the second-token of each git invocation since marker n_before.

        Excludes the T-3600 push-guard's read-only probe: push_with_retry runs
        `git symbolic-ref` (and, on a named branch, `git log`) before every push to
        warn about a HEAD:main push from a feature branch. Those probes are
        orthogonal to the lifecycle command sequence these tests pin (and are
        covered directly by test_git_utils.TestPushGuardT3600)."""
        argvs = [c.args[0] for c in self.mock_run.call_args_list[n_before:]]
        return [a[1] for a in argvs
                if a and a[0] == "git" and a[1] not in ("symbolic-ref", "log")]

    def test_file_then_claim_then_resolve_full_command_sequence(self) -> None:
        # Use a fixed actor for determinism
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            # ---- file ----
            mark = len(self.mock_run.call_args_list)
            file_args = MagicMock()
            file_args.requester = "orchestrator"
            file_args.target = "recovery"
            file_args.title = "Test ticket"
            file_args.labels = "watch-loop"
            file_args.depends_on = None
            file_args.stale_after = None
            file_args.not_before = None
            file_args.ratifiable_file = None
            file_args.ask = "do the thing"
            file_args.context = "the thing matters"
            file_args.no_push = False
            file_args.spawn_impl = False
            file_args.terminal = False
            file_args.impl_title = None
            file_args.impl_ask = None
            file_args.appetite = None  # T-3440/ORG-089: getattr-read default
            file_args.bet_ref = None
            file_args.impl_target = None
            # T-3328: this test is about the git-command sequence, not DAG
            # connectivity — orphan escape hatch (fires before any git call,
            # so it does not perturb the asserted sequence below).
            file_args.orphan = "test fixture: full file/claim/resolve git-command sequence"
            ticket.cmd_file(file_args)
            kinds = self._git_kinds_after(mark)
            # file: no mv. _next_ticket_id issues fetch + ls-tree first
            # (T-0068 fix). After T-0442, the pre-commit pull is now
            # pull_repo_strict's resilient fetch+ff-merge two-step rather than
            # the legacy `git pull --ff-only origin main`. Full order:
            # fetch (id-alloc) → ls-tree (id-alloc) → rev-parse ×2 (T-1191
            # _abort_stale_rebase stale-rebase probe, run at pull entry) →
            # fetch (pull) → merge (pull) → rev-parse (T-1816: uncached
            # _git_common_dir lockfile resolution at mutation-lock entry) →
            # add → diff → commit → push.
            self.assertEqual(
                kinds,
                ["fetch", "ls-tree", "rev-parse", "rev-parse", "fetch", "merge", "rev-parse", "add", "diff", "commit", "push"],
            )

            # The new ticket should now exist in inbox/.
            inbox_files = list((self.root / "tickets" / "inbox").iterdir())
            self.assertEqual(len(inbox_files), 1)
            tid = inbox_files[0].name.replace(".md", "")

            # ---- claim ----
            mark = len(self.mock_run.call_args_list)
            claim_args = MagicMock()
            claim_args.ticket = tid
            claim_args.no_push = False
            ticket.cmd_claim(claim_args)
            kinds = self._git_kinds_after(mark)
            # Order: fetch → show* → rev-parse×2 (T-1191 stale-rebase probe at
            # pull entry) → fetch → merge → mv → add → diff → commit → push.
            # T-0244 moved the pull BEFORE the mv so a pull-fail can't leave
            # the filesystem inconsistent. T-0442 replaced the legacy
            # `git pull --ff-only` with pull_repo_strict's resilient
            # fetch+ff-merge two-step. T-0463 added the pre-claim
            # concurrent-claim guard: an initial fetch + per-state
            # `git show origin/main:tickets/<state>/T-NNNN.md` walk (stops
            # at first hit, so "inbox" found → one show call only).
            # T-1816: third rev-parse = uncached _git_common_dir lockfile
            # resolution at mutation-lock entry (lock wraps the pull here).
            self.assertEqual(
                kinds, ["fetch", "show", "rev-parse", "rev-parse", "rev-parse", "fetch", "merge", "mv", "merge-base", "add", "diff", "commit", "push"]
            )
            self.assertTrue((self.root / "tickets" / "active" / f"{tid}.md").exists())
            self.assertFalse((self.root / "tickets" / "inbox" / f"{tid}.md").exists())

            # ---- resolve ----
            mark = len(self.mock_run.call_args_list)
            resolve_args = MagicMock()
            resolve_args.ticket = tid
            resolve_args.with_summary = "done and dusted"
            resolve_args.no_push = False
            ticket.cmd_resolve(resolve_args)
            kinds = self._git_kinds_after(mark)
            self.assertEqual(
                kinds,
                # rev-parse×2 = T-1191 _abort_stale_rebase probe at pull entry;
                # third = T-1816 uncached lockfile resolution at lock entry.
                ["rev-parse", "rev-parse", "rev-parse", "fetch", "merge", "mv", "merge-base", "add", "diff", "commit", "push"],
            )
            self.assertTrue((self.root / "tickets" / "done" / f"{tid}.md").exists())

            # The done ticket's transition log must contain three entries:
            # filed, claimed, resolved.
            text = (self.root / "tickets" / "done" / f"{tid}.md").read_text()
            self.assertIn("filed", text)
            self.assertIn("claimed", text)
            self.assertIn("resolved; done and dusted", text)
            # Final state field reflects done.
            fields, _ = ticket.parse_front_matter(text)
            self.assertEqual(fields["state"], "done")
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_transition_git_add_omits_missing_old_path(self) -> None:
        """Regression: post-`git mv`, the old path no longer exists in the
        working tree. The auto-commit pipeline must not pass it to
        `git add` (real git returns `fatal: pathspec ... did not match
        any files` and exits 128, leaving the rename half-staged with no
        commit). The new path is sufficient because `git mv` already
        staged the deletion side of the rename. See T-0004.
        """
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0042"
            _write_ticket(
                self.root / "tickets" / "inbox" / f"{tid}.md",
                tid=tid,
                state="inbox",
            )
            mark = len(self.mock_run.call_args_list)
            claim_args = MagicMock()
            claim_args.ticket = tid
            claim_args.no_push = False
            # Must not raise.
            ticket.cmd_claim(claim_args)

            add_argvs = [
                c.args[0]
                for c in self.mock_run.call_args_list[mark:]
                if c.args[0][:2] == ["git", "add"]
            ]
            self.assertEqual(
                len(add_argvs), 1, f"expected 1 git add, got {add_argvs!r}"
            )
            argv = add_argvs[0]
            idx = argv.index("--")
            added_paths = argv[idx + 1 :]
            new_rel = f"tickets/active/{tid}.md"
            old_rel = f"tickets/inbox/{tid}.md"
            self.assertIn(new_rel, added_paths)
            self.assertNotIn(
                old_rel,
                added_paths,
                "old path passed to git add would fail post-mv with "
                "`pathspec did not match any files`",
            )
            # And the transition completed end-to-end: commit + push fired.
            # T-0442: pre-commit pull is now fetch+ff-merge two-step.
            # T-0463: cmd_claim adds a pre-claim fetch + per-state `git show`
            # walk (stops at first hit, so "inbox" found → one show call).
            # T-1816: third rev-parse = uncached _git_common_dir lockfile
            # resolution at mutation-lock entry (here the lock wraps the pull,
            # so it lands before the fetch+merge pair).
            kinds = self._git_kinds_after(mark)
            self.assertEqual(
                kinds, ["fetch", "show", "rev-parse", "rev-parse", "rev-parse", "fetch", "merge", "mv", "merge-base", "add", "diff", "commit", "push"]
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]


# ---------------------------------------------------------------------------
# Commit-message templates
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# not_before field — parse helper + round-trip
# ---------------------------------------------------------------------------


class TestNotBeforeField(unittest.TestCase):
    def test_bare_date_normalizes_to_midnight_utc(self) -> None:
        result = ticket._parse_not_before("2026-06-01")
        self.assertEqual(result, "2026-06-01T00:00:00Z")

    def test_full_iso_stored_as_is(self) -> None:
        result = ticket._parse_not_before("2026-06-01T08:30:00Z")
        self.assertEqual(result, "2026-06-01T08:30:00Z")

    def test_malformed_value_raises_ticket_error(self) -> None:
        for bad in ("next-tuesday", "2026/06/01", "2026-06-01T", "2026-99-01"):
            with self.subTest(bad=bad):
                with self.assertRaises(ticket.TicketError):
                    ticket._parse_not_before(bad)

    def test_parse_roundtrip_preserves_not_before(self) -> None:
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-05-04T10:00:00Z\n"
            "requester: orchestrator\n"
            "target: recovery\n"
            "state: inbox\n"
            "stale_after: 7d\n"
            "not_before: 2026-06-01T00:00:00Z\n"
            "---\n"
            "\n"
            "# title\n"
        )
        fields, body = ticket.parse_front_matter(text)
        self.assertEqual(fields["not_before"], "2026-06-01T00:00:00Z")
        serialized = ticket.serialize_front_matter(fields)
        # not_before must appear after stale_after in serialized output
        self.assertIn("not_before: 2026-06-01T00:00:00Z", serialized)
        idx_stale = serialized.index("stale_after:")
        idx_nb = serialized.index("not_before:")
        self.assertGreater(idx_nb, idx_stale)
        # Re-parse: field survives
        fields2, _ = ticket.parse_front_matter(serialized + "\n" + body)
        self.assertEqual(fields2["not_before"], "2026-06-01T00:00:00Z")


class TestRatifiableFilesField(unittest.TestCase):
    def test_ratifiable_files_single_entry_with_allowlisted_repo_accepted(self) -> None:
        entries = ["pantheon-config:sop/standard/foo.md"]
        result = ticket._parse_ratifiable_files(entries)
        self.assertEqual(result, entries)

    def test_ratifiable_files_multi_entry_accepted(self) -> None:
        entries = [
            "pantheon-config:rulebook/directives.md",
            "pantheon-config:rulebook/policies/trust-posture.md",
            "revenue-project:adr/MX-001-token-cost-asymmetry.md",
        ]
        result = ticket._parse_ratifiable_files(entries)
        self.assertEqual(result, entries)

    def test_ratifiable_files_all_allowlisted_repos_accepted(self) -> None:
        for repo in ticket.RATIFIABLE_REPO_ALLOWLIST:
            with self.subTest(repo=repo):
                result = ticket._parse_ratifiable_files([f"{repo}:some/path.md"])
                self.assertEqual(result, [f"{repo}:some/path.md"])

    def test_ratifiable_files_accepts_pantheon_public(self) -> None:
        """T-1964: seed ratify surfaces live in pantheon-public (T-1933 arc).

        Explicit named regression alongside the iterating test above — before
        T-1964 a pantheon-public ratify entry was rejected and the workaround
        pointed at the pantheon-config design-of-record instead.
        """
        self.assertIn("pantheon-public", ticket.RATIFIABLE_REPO_ALLOWLIST)
        entry = "pantheon-public:adr/ORG-001-ledger-format.md"
        self.assertEqual(ticket._parse_ratifiable_files([entry]), [entry])

    def test_ratifiable_files_rejects_repo_not_in_allowlist(self) -> None:
        for bad_repo in ("unknown-repo", "github.com", "", "PANTHEON-CONFIG"):
            with self.subTest(bad_repo=bad_repo):
                with self.assertRaises(ticket.TicketError):
                    ticket._parse_ratifiable_files([f"{bad_repo}:some/path.md"])

    def test_ratifiable_files_rejects_entry_without_colon(self) -> None:
        for bad in ("pantheon-config", "sop/standard/foo.md", "justpath"):
            with self.subTest(bad=bad):
                with self.assertRaises(ticket.TicketError):
                    ticket._parse_ratifiable_files([bad])

    def test_ratifiable_files_rejects_traversal_in_path(self) -> None:
        for bad_path in (
            "../etc/passwd",
            "sop/../../etc/passwd",
            "sop/standard/..",
            "..",
        ):
            with self.subTest(bad_path=bad_path):
                with self.assertRaises(ticket.TicketError):
                    ticket._parse_ratifiable_files([f"pantheon-config:{bad_path}"])

    def test_ratifiable_files_cli_flag_repeatable(self) -> None:
        with (
            patch("tools.ticket._next_ticket_id", return_value="T-0099"),
            patch("tools.ticket._pull_if_needed"),
            patch("tools.ticket._commit_and_push"),
        ):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                orig_tickets_root = ticket.TICKETS_ROOT
                ticket.TICKETS_ROOT = root / "tickets"
                (root / "tickets" / "inbox").mkdir(parents=True)
                try:
                    args = MagicMock()
                    args.requester = "orchestrator"
                    args.target = "operator"
                    args.title = "Multi-file ratification"
                    args.labels = "ratification"
                    args.depends_on = None
                    args.stale_after = None
                    args.not_before = None
                    args.parent_ticket = None
                    args.ratifiable_file = [
                        "pantheon-config:rulebook/directives.md",
                        "revenue-project:adr/MX-001-token-cost-asymmetry.md",
                    ]
                    args.ask = ""
                    args.context = ""
                    args.no_push = False
                    # target=operator + ratifiable_file → ratify; satisfy the T-0852
                    # gate with --terminal (this test exercises ratifiable_files
                    # parsing, not the impl-gate; terminal keeps file-count at 1).
                    args.spawn_impl = False
                    args.terminal = True
                    args.impl_title = None
                    args.impl_ask = None
                    args.appetite = None  # T-3440/ORG-089: getattr-read default
                    args.bet_ref = None
                    args.impl_target = None
                    # T-3328: ratifiable_files is not itself a lineage edge —
                    # orphan escape hatch (this test is about the repeatable
                    # --ratifiable-file flag, not DAG connectivity).
                    args.orphan = "test fixture: repeatable --ratifiable-file CLI flag"
                    ticket.cmd_file(args)
                    filed = list((root / "tickets" / "inbox").iterdir())
                    self.assertEqual(len(filed), 1)
                    text = filed[0].read_text()
                    self.assertIn(
                        "ratifiable_files: [pantheon-config:rulebook/directives.md, "
                        "revenue-project:adr/MX-001-token-cost-asymmetry.md]",
                        text,
                    )
                finally:
                    ticket.TICKETS_ROOT = orig_tickets_root

    def test_ratifiable_files_roundtrip_through_frontmatter(self) -> None:
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-05-04T10:00:00Z\n"
            "requester: orchestrator\n"
            "target: operator\n"
            "state: inbox\n"
            "labels: [ratification, sop]\n"
            "ratifiable_files: [pantheon-config:sop/standard/foo.md, "
            "revenue-project:adr/MX-001-token-cost-asymmetry.md]\n"
            "---\n"
            "\n"
            "# title\n"
        )
        fields, body = ticket.parse_front_matter(text)
        self.assertEqual(
            fields["ratifiable_files"],
            [
                "pantheon-config:sop/standard/foo.md",
                "revenue-project:adr/MX-001-token-cost-asymmetry.md",
            ],
        )
        serialized = ticket.serialize_front_matter(fields)
        self.assertIn(
            "ratifiable_files: [pantheon-config:sop/standard/foo.md, "
            "revenue-project:adr/MX-001-token-cost-asymmetry.md]",
            serialized,
        )
        fields2, _ = ticket.parse_front_matter(serialized + "\n" + body)
        self.assertEqual(fields2["ratifiable_files"], fields["ratifiable_files"])


class TestRatifiablePairingEnforcement(unittest.TestCase):
    """ORG-022: ratifiable label ↔ ratifiable_files file-tier pairing.

    Refuse: labels contains 'ratifiable' but no ratifiable_files.
    Auto-add: ratifiable_files present but labels missing 'ratifiable'.
    """

    def _file_args(self, *, target: str, labels=None, ratifiable_file=None,
                   terminal=False, spawn_impl=False, impl_title=None):
        args = MagicMock()
        args.requester = "orchestrator"
        args.target = target
        args.title = "test ticket"
        args.labels = labels
        args.depends_on = None
        args.stale_after = None
        args.not_before = None
        args.parent_ticket = None
        args.ratifiable_file = ratifiable_file
        args.intent = None
        args.ask = ""
        args.context = ""
        args.no_push = False
        # T-0852 ratify impl-gate flags (argparse defaults mirrored).
        args.spawn_impl = spawn_impl
        args.terminal = terminal
        args.impl_title = impl_title
        args.impl_ask = None
        args.appetite = None  # T-3440/ORG-089: getattr-read default
        args.bet_ref = None
        args.impl_target = None
        # T-3328: this class's fixtures fabricate a root-tier test ticket with
        # no semantic lineage (ORG-022 pairing enforcement is orthogonal to
        # the DAG-connectivity guard) — orphan escape hatch, not a real edge.
        args.orphan = "test fixture: ORG-022 ratifiable-label pairing enforcement"
        return args

    def _file_under_tmp(self, args) -> str:
        with (
            patch("tools.ticket._next_ticket_id", return_value="T-0099"),
            patch("tools.ticket._pull_if_needed"),
            patch("tools.ticket._commit_and_push"),
        ):
            import tempfile as _tf
            with _tf.TemporaryDirectory() as tmp:
                root = Path(tmp)
                orig_root = ticket.TICKETS_ROOT
                ticket.TICKETS_ROOT = root / "tickets"
                (root / "tickets" / "inbox").mkdir(parents=True)
                try:
                    ticket.cmd_file(args)
                    filed = list((root / "tickets" / "inbox").iterdir())
                    self.assertEqual(len(filed), 1)
                    return filed[0].read_text()
                finally:
                    ticket.TICKETS_ROOT = orig_root

    def test_ratifiable_label_without_files_refused(self) -> None:
        with self.assertRaises(ticket.TicketError) as cm:
            self._file_under_tmp(self._file_args(
                target="operator", labels="ratification,sop", ratifiable_file=None,
            ))
        self.assertIn("ratification label requires --ratifiable-file", str(cm.exception))

    def test_ratifiable_files_without_label_auto_adds(self) -> None:
        # terminal=True satisfies the T-0852 ratify gate without spawning a child
        # (keeps this test focused on ORG-022 label auto-add).
        text = self._file_under_tmp(self._file_args(
            target="operator", labels="sop,from-something",
            ratifiable_file=["pantheon-config:sop/standard/foo.md"], terminal=True,
        ))
        # Auto-added 'ratification' label (matches PWA frontend filter)
        self.assertIn("ratification", text)
        # Files still there
        self.assertIn("pantheon-config:sop/standard/foo.md", text)

    def test_ratifiable_files_without_any_labels_auto_adds(self) -> None:
        """labels arg None → labels list created with 'ratification' (+ ratify-terminal
        from the T-0852 --terminal gate-satisfier)."""
        text = self._file_under_tmp(self._file_args(
            target="operator", labels=None,
            ratifiable_file=["pantheon-config:sop/standard/foo.md"], terminal=True,
        ))
        self.assertIn("labels: [ratify-terminal, ratification]", text)

    def test_both_present_happy_path(self) -> None:
        """label + files together → no validation fires, no auto-add."""
        text = self._file_under_tmp(self._file_args(
            target="operator", labels="ratification,sop",
            ratifiable_file=["pantheon-config:sop/standard/foo.md"], terminal=True,
        ))
        self.assertIn("ratification", text)
        self.assertIn("pantheon-config:sop/standard/foo.md", text)
        # Should not duplicate the label
        labels_line = [l for l in text.splitlines() if l.startswith("labels:")][0]
        self.assertEqual(labels_line.count("ratification"), 1)

    def test_non_ratify_ticket_unaffected(self) -> None:
        """Neither label nor files → no validation, no auto-add."""
        text = self._file_under_tmp(self._file_args(
            target="orchestrator", labels="design,via-msg-1234", ratifiable_file=None,
        ))
        self.assertNotIn("ratification", text)
        self.assertNotIn("ratifiable_files", text)


class TestIntentField(unittest.TestCase):
    """T-0465: intent disambiguation for target=operator queue.

    intent ∈ {action, ratify, noodle}; target=operator-only field.
    """

    def _file_args(self, *, target: str, intent=None, ratifiable_file=None,
                   terminal=False, spawn_impl=False, impl_title=None):
        args = MagicMock()
        args.requester = "orchestrator"
        args.target = target
        args.title = "test ticket"
        args.labels = None
        args.depends_on = None
        args.stale_after = None
        args.not_before = None
        args.parent_ticket = None
        args.ratifiable_file = ratifiable_file
        args.intent = intent
        args.ask = ""
        args.context = ""
        args.no_push = False
        # T-0852 ratify impl-gate flags (argparse defaults mirrored).
        args.spawn_impl = spawn_impl
        args.terminal = terminal
        args.impl_title = impl_title
        args.impl_ask = None
        args.appetite = None  # T-3440/ORG-089: getattr-read default
        args.bet_ref = None
        args.impl_target = None
        # T-3328: this class's fixtures fabricate a root-tier test ticket with
        # no semantic lineage (intent-field disambiguation is orthogonal to
        # DAG connectivity) — orphan escape hatch, not a real edge.
        args.orphan = "test fixture: T-0465 intent-field disambiguation"
        return args

    def _file_under_tmp(self, args) -> str:
        """Run cmd_file inside a fresh tmpdir; return frontmatter+body text."""
        with (
            patch("tools.ticket._next_ticket_id", return_value="T-0099"),
            patch("tools.ticket._pull_if_needed"),
            patch("tools.ticket._commit_and_push"),
        ):
            import tempfile as _tf
            with _tf.TemporaryDirectory() as tmp:
                root = Path(tmp)
                orig_root = ticket.TICKETS_ROOT
                ticket.TICKETS_ROOT = root / "tickets"
                (root / "tickets" / "inbox").mkdir(parents=True)
                try:
                    ticket.cmd_file(args)
                    filed = list((root / "tickets" / "inbox").iterdir())
                    self.assertEqual(len(filed), 1)
                    return filed[0].read_text()
                finally:
                    ticket.TICKETS_ROOT = orig_root

    def test_intent_explicit_action_on_operator_target(self) -> None:
        text = self._file_under_tmp(self._file_args(target="operator", intent="action"))
        self.assertIn("intent: action", text)

    def test_intent_explicit_noodle_on_operator_target(self) -> None:
        text = self._file_under_tmp(self._file_args(target="operator", intent="noodle"))
        self.assertIn("intent: noodle", text)

    def test_intent_auto_defaults_action_when_operator_no_ratifiable(self) -> None:
        text = self._file_under_tmp(self._file_args(target="operator"))
        self.assertIn("intent: action", text)

    def test_intent_auto_defaults_ratify_when_operator_with_ratifiable(self) -> None:
        text = self._file_under_tmp(
            self._file_args(
                target="operator",
                ratifiable_file=["pantheon-config:sop/standard/foo.md"],
                terminal=True,  # satisfy T-0852 ratify gate (not under test here)
            )
        )
        self.assertIn("intent: ratify", text)

    def test_intent_absent_when_target_not_operator(self) -> None:
        text = self._file_under_tmp(self._file_args(target="orchestrator"))
        self.assertNotIn("intent:", text)

    def test_intent_refused_when_target_not_operator(self) -> None:
        with self.assertRaises(ticket.TicketError) as cm:
            self._file_under_tmp(self._file_args(target="orchestrator", intent="action"))
        self.assertIn("target=operator-specific", str(cm.exception))

    def test_intent_invalid_vocab_refused(self) -> None:
        # Bypass argparse choices by calling cmd_file directly with bad intent
        with self.assertRaises(ticket.TicketError) as cm:
            self._file_under_tmp(self._file_args(target="operator", intent="bogus"))
        self.assertIn("not in vocab", str(cm.exception))

    def test_intent_roundtrip_through_frontmatter(self) -> None:
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-05-19T00:00:00Z\n"
            "requester: orchestrator\n"
            "target: operator\n"
            "state: inbox\n"
            "intent: noodle\n"
            "---\n"
            "\n"
            "# title\n"
        )
        fields, body = ticket.parse_front_matter(text)
        self.assertEqual(fields["intent"], "noodle")
        serialized = ticket.serialize_front_matter(fields)
        self.assertIn("intent: noodle", serialized)


class TestRatifyImplGate(unittest.TestCase):
    """T-0852: a target=operator + intent=ratify file must not dead-end.

    The filer must supply --spawn-impl (+ --impl-title) to atomically file an
    implementation child gated on the ratify, or --terminal to assert no
    follow-up. On the operator's later ratify the impl child cascade-unblocks.
    """

    def _file_args(self, *, target="operator", ratifiable_file=None, intent=None,
                   labels=None, spawn_impl=False, terminal=False,
                   impl_title=None, impl_ask=None, impl_target=None):
        args = MagicMock()
        args.requester = "specialist"
        args.target = target
        args.title = "ratify something"
        args.labels = labels
        args.depends_on = None
        args.stale_after = None
        args.not_before = None
        args.parent_ticket = None
        args.ratifiable_file = (
            ratifiable_file
            if ratifiable_file is not None
            else (["pantheon-config:sop/standard/foo.md"] if target == "operator" else None)
        )
        args.intent = intent
        args.ask = ""
        args.context = ""
        args.no_push = False
        args.spawn_impl = spawn_impl
        args.terminal = terminal
        args.impl_title = impl_title
        args.impl_ask = impl_ask
        args.impl_target = impl_target
        args.appetite = None  # T-3440/ORG-089: getattr-read default
        args.bet_ref = None
        # T-3328: this class tests the T-0852 ratify-impl-gate, orthogonal to
        # DAG connectivity — orphan escape hatch so the gate tests exercise
        # their own error paths uninterrupted by the lineage guard.
        args.orphan = "test fixture: T-0852 ratify impl-followup gate"
        return args

    def _run(self, args, *, ids=("T-0100", "T-0101")):
        """cmd_file under tmpdir with patched id-alloc + spy commit. Returns
        (inbox_files_by_stem, commit_affected_paths)."""
        captured = {}

        def _spy_commit(affected, msg, no_push=False, rebase_on_nonff=True):
            captured["affected"] = list(affected)

        with (
            patch("tools.ticket._next_ticket_id", side_effect=list(ids)),
            patch("tools.ticket._pull_if_needed"),
            patch("tools.ticket._commit_and_push", side_effect=_spy_commit),
        ):
            import tempfile as _tf
            with _tf.TemporaryDirectory() as tmp:
                root = Path(tmp)
                orig_root = ticket.TICKETS_ROOT
                ticket.TICKETS_ROOT = root / "tickets"
                (root / "tickets" / "inbox").mkdir(parents=True)
                try:
                    rc = ticket.cmd_file(args)
                    self.assertEqual(rc, 0)
                    by_stem = {
                        p.stem: p.read_text()
                        for p in (root / "tickets" / "inbox").iterdir()
                    }
                    return by_stem, captured.get("affected", [])
                finally:
                    ticket.TICKETS_ROOT = orig_root

    def test_gate_refuses_ratify_without_followup(self) -> None:
        with self.assertRaises(ticket.TicketError) as cm:
            self._run(self._file_args())  # no spawn, no terminal
        self.assertIn("must not dead-end", str(cm.exception))

    def test_terminal_satisfies_gate_no_child(self) -> None:
        by_stem, affected = self._run(self._file_args(terminal=True))
        self.assertEqual(set(by_stem), {"T-0100"})  # exactly the ratify, no child
        self.assertIn("ratify-terminal", by_stem["T-0100"])
        self.assertIn("intent: ratify", by_stem["T-0100"])
        self.assertEqual(len(affected), 1)

    def test_spawn_impl_creates_gated_child(self) -> None:
        by_stem, affected = self._run(self._file_args(
            spawn_impl=True, impl_title="Build the thing", impl_target="recovery",
        ))
        self.assertEqual(set(by_stem), {"T-0100", "T-0101"})
        ratify, impl = by_stem["T-0100"], by_stem["T-0101"]
        self.assertIn("intent: ratify", ratify)
        # Child is gated on the ratify and carries lineage + routes to impl-target.
        self.assertIn("depends_on: [T-0100]", impl)
        self.assertIn("parent_ticket: T-0100", impl)
        self.assertIn("target: recovery", impl)
        self.assertIn("Build the thing", impl)
        self.assertIn("ratify-impl", impl)
        # Both files land in ONE commit (scope guard sees both).
        self.assertEqual({p.stem for p in affected}, {"T-0100", "T-0101"})

    def test_spawn_impl_defaults_target_to_requester(self) -> None:
        by_stem, _ = self._run(self._file_args(
            spawn_impl=True, impl_title="Build it",  # no impl_target
        ))
        self.assertIn("target: specialist", by_stem["T-0101"])  # = requester

    def test_spawn_impl_requires_impl_title(self) -> None:
        with self.assertRaises(ticket.TicketError) as cm:
            self._run(self._file_args(spawn_impl=True))
        self.assertIn("--impl-title", str(cm.exception))

    def test_spawn_and_terminal_mutually_exclusive(self) -> None:
        with self.assertRaises(ticket.TicketError) as cm:
            self._run(self._file_args(spawn_impl=True, impl_title="x", terminal=True))
        self.assertIn("mutually exclusive", str(cm.exception))

    def test_auto_default_ratify_triggers_gate(self) -> None:
        # target=operator + ratifiable_file but NO explicit --intent → intent
        # auto-defaults to ratify → the gate must still fire (bypass trap).
        with self.assertRaises(ticket.TicketError) as cm:
            self._run(self._file_args(intent=None))
        self.assertIn("must not dead-end", str(cm.exception))

    def test_impl_flags_refused_on_non_ratify(self) -> None:
        # intent=action on operator target → not a ratify → flags refused.
        with self.assertRaises(ticket.TicketError) as cm:
            self._run(self._file_args(
                intent="action", ratifiable_file=None,
                spawn_impl=True, impl_title="x",
            ))
        self.assertIn("ratify-only", str(cm.exception))

    def test_non_operator_target_unaffected(self) -> None:
        # target=recovery, no impl flags → files normally, gate does not apply.
        by_stem, _ = self._run(self._file_args(target="recovery", ratifiable_file=None))
        self.assertEqual(set(by_stem), {"T-0100"})


class TestRatifyRequiresRatifiableFiles(unittest.TestCase):
    """T-1321: intent=ratify (explicit) must fail at the tool boundary when no
    ratifiable_files are present.  Closes the gap where --intent ratify could
    produce a ratify ticket the PWA has nothing to show (ORG-015, P1).
    """

    # ------------------------------------------------------------------
    # Helpers — cmd_file
    # ------------------------------------------------------------------

    def _file_args(self, *, intent=None, ratifiable_file=None,
                   target="operator", terminal=True, spawn_impl=False,
                   impl_title=None):
        """Minimal MagicMock args for cmd_file; terminal=True by default so
        the T-0852 dead-end gate is satisfied, keeping these tests focused on
        the ratifiable_files check only."""
        args = MagicMock()
        args.requester = "specialist"
        args.target = target
        args.title = "ratify gate test"
        args.labels = None
        args.depends_on = None
        args.stale_after = None
        args.not_before = None
        args.parent_ticket = None
        args.ratifiable_file = ratifiable_file
        args.intent = intent
        args.ask = ""
        args.context = ""
        args.no_push = False
        args.spawn_impl = spawn_impl
        args.terminal = terminal
        args.impl_title = impl_title
        args.impl_ask = None
        args.appetite = None  # T-3440/ORG-089: getattr-read default
        args.bet_ref = None
        args.impl_target = None
        args.serves = None
        # T-3328: this class tests T-1321 ratifiable_files requirements,
        # orthogonal to DAG connectivity — orphan escape hatch.
        args.orphan = "test fixture: T-1321 ratify-requires-ratifiable-files gate"
        return args

    def _run_file(self, args):
        """Execute cmd_file in an isolated tmpdir; return inbox file text."""
        with (
            patch("tools.ticket._next_ticket_id", return_value="T-0200"),
            patch("tools.ticket._pull_if_needed"),
            patch("tools.ticket._commit_and_push"),
        ):
            import tempfile as _tf
            with _tf.TemporaryDirectory() as tmp:
                root = Path(tmp)
                orig_root = ticket.TICKETS_ROOT
                ticket.TICKETS_ROOT = root / "tickets"
                (root / "tickets" / "inbox").mkdir(parents=True)
                try:
                    ticket.cmd_file(args)
                    filed = list((root / "tickets" / "inbox").iterdir())
                    self.assertEqual(len(filed), 1)
                    return filed[0].read_text()
                finally:
                    ticket.TICKETS_ROOT = orig_root

    # ------------------------------------------------------------------
    # cmd_file tests
    # ------------------------------------------------------------------

    def test_cmd_file_explicit_ratify_no_ratifiable_raises(self) -> None:
        """--intent ratify with no --ratifiable-file must raise TicketError."""
        args = self._file_args(intent="ratify", ratifiable_file=None)
        with self.assertRaises(ticket.TicketError) as cm:
            with (
                patch("tools.ticket._next_ticket_id", return_value="T-0200"),
                patch("tools.ticket._pull_if_needed"),
                patch("tools.ticket._commit_and_push"),
            ):
                import tempfile as _tf
                with _tf.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    orig_root = ticket.TICKETS_ROOT
                    ticket.TICKETS_ROOT = root / "tickets"
                    (root / "tickets" / "inbox").mkdir(parents=True)
                    try:
                        ticket.cmd_file(args)
                    finally:
                        ticket.TICKETS_ROOT = orig_root
        self.assertIn("--ratifiable-file", str(cm.exception))
        # Verify no ticket file was left behind (side-effect check).
        # The exception fires before any file write, so inbox is empty.
        # (We can't check the dir here since it's torn down, but the raise
        # itself guarantees no commit path was reached — _commit_and_push
        # is patched and must not have been called.)

    def test_cmd_file_explicit_ratify_with_ratifiable_succeeds(self) -> None:
        """--intent ratify WITH --ratifiable-file succeeds (positive path)."""
        args = self._file_args(
            intent="ratify",
            ratifiable_file=["pantheon-config:sop/standard/foo.md"],
        )
        text = self._run_file(args)
        self.assertIn("intent: ratify", text)
        self.assertIn("ratifiable_files", text)

    def test_cmd_file_target_operator_no_intent_no_files_stays_action(self) -> None:
        """Regression: target=operator, no --intent, no files → intent=action.
        The auto-default path must remain unchanged (no raise)."""
        args = self._file_args(intent=None, ratifiable_file=None, terminal=False,
                               target="operator")
        # intent=action does not trigger the dead-end gate (gate is ratify-only),
        # so no --terminal needed.  Override terminal=False for this case.
        args.terminal = False
        text = self._run_file(args)
        self.assertIn("intent: action", text)
        self.assertNotIn("intent: ratify", text)

    # ------------------------------------------------------------------
    # Helpers — cmd_retarget
    # ------------------------------------------------------------------

    def _run_retarget(self, ticket_content: str, intent_arg: str | None,
                      to: str = "operator") -> str:
        """Execute cmd_retarget on a hand-crafted ticket; return updated text."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "inbox").mkdir()
            path = root / "inbox" / "T-0001.md"
            path.write_text(ticket_content, encoding="utf-8")

            with (
                patch.object(ticket, "TICKETS_ROOT", root),
                patch.object(ticket, "REPO_ROOT", root),
                patch("tools._git_utils.subprocess.run",
                      return_value=MagicMock(returncode=0, stdout="", stderr="")),
                patch.object(ticket, "_commit_and_push"),
                patch.object(ticket, "_pull_if_needed"),
            ):
                ns = argparse.Namespace(
                    ticket="T-0001",
                    to=to,
                    note=None,
                    no_push=False,
                    keep_state=False,
                    intent=intent_arg,
                    serves=None,
                )
                ticket.cmd_retarget(ns)

            return path.read_text(encoding="utf-8")

    def _ticket_without_ratifiable(self) -> str:
        """A minimal ticket with no ratifiable_files in frontmatter."""
        return (
            "---\n"
            "id: T-0001\n"
            "created: 2026-06-01T00:00:00Z\n"
            "requester: specialist\n"
            "target: recovery\n"
            "state: inbox\n"
            "parent_ticket: null\n"
            "depends_on: []\n"
            "---\n"
            "\n# Test\n\n## Ask\n\nDo it.\n\n## Transition log\n\n"
            "- 2026-06-01T00:00:00Z [specialist] filed in inbox\n"
        )

    def _ticket_with_ratifiable(self) -> str:
        """A minimal ticket that already carries ratifiable_files."""
        return (
            "---\n"
            "id: T-0001\n"
            "created: 2026-06-01T00:00:00Z\n"
            "requester: specialist\n"
            "target: recovery\n"
            "state: inbox\n"
            "parent_ticket: null\n"
            "depends_on: []\n"
            "ratifiable_files: [pantheon-config:sop/standard/foo.md]\n"
            "---\n"
            "\n# Test\n\n## Ask\n\nDo it.\n\n## Transition log\n\n"
            "- 2026-06-01T00:00:00Z [specialist] filed in inbox\n"
        )

    # ------------------------------------------------------------------
    # cmd_retarget tests
    # ------------------------------------------------------------------

    def test_cmd_retarget_explicit_ratify_no_existing_ratifiable_raises(self) -> None:
        """retarget --intent ratify on ticket with no ratifiable_files raises."""
        content = self._ticket_without_ratifiable()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "inbox").mkdir()
            path = root / "inbox" / "T-0001.md"
            path.write_text(content, encoding="utf-8")

            with (
                patch.object(ticket, "TICKETS_ROOT", root),
                patch.object(ticket, "REPO_ROOT", root),
                patch("tools._git_utils.subprocess.run",
                      return_value=MagicMock(returncode=0, stdout="", stderr="")),
                patch.object(ticket, "_commit_and_push") as mock_commit,
                patch.object(ticket, "_pull_if_needed"),
            ):
                ns = argparse.Namespace(
                    ticket="T-0001",
                    to="operator",
                    note=None,
                    no_push=False,
                    keep_state=False,
                    intent="ratify",
                    serves=None,
                )
                with self.assertRaises(ticket.TicketError) as cm:
                    ticket.cmd_retarget(ns)

            self.assertIn("ratifiable_files", str(cm.exception))
            # No commit must have been issued.
            mock_commit.assert_not_called()

    def test_cmd_retarget_explicit_ratify_with_existing_ratifiable_succeeds(self) -> None:
        """retarget --intent ratify on ticket that already has ratifiable_files succeeds."""
        content = self._ticket_with_ratifiable()
        text = self._run_retarget(content, intent_arg="ratify")
        fields, _ = ticket.parse_front_matter(text)
        self.assertEqual(fields.get("intent"), "ratify")
        self.assertIn("pantheon-config:sop/standard/foo.md",
                      str(fields.get("ratifiable_files", "")))

    def test_cmd_retarget_to_operator_no_intent_autorestores_ratify(self) -> None:
        """A ratify ticket (ratifiable_files present, no intent) retargeted TO
        operator WITHOUT --intent auto-restores intent:ratify. Pins the round-trip
        self-heal: an away-from-operator leg drops intent (operator-specific), and
        the return leg restores it from the durable ratifiable_files. Regression
        for the TG7307 concern (T-2668) that a codex round-trip loses the ratify
        nature — it does not; ratifiable_files never drop and intent comes back.
        """
        content = self._ticket_with_ratifiable()  # target=recovery, ratifiable_files, no intent
        text = self._run_retarget(content, intent_arg=None, to="operator")
        fields, _ = ticket.parse_front_matter(text)
        self.assertEqual(fields.get("intent"), "ratify")
        self.assertIn("pantheon-config:sop/standard/foo.md",
                      str(fields.get("ratifiable_files", "")))

    def test_cmd_retarget_away_from_operator_drops_intent_and_logs_it(self) -> None:
        """Retargeting an operator/intent:ratify ticket AWAY from operator drops the
        intent field (operator-specific per T-0465) but records the drop AND its
        auto-restore in the transition log, so the drop is not silently alarming
        (T-2668)."""
        content = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-06-01T00:00:00Z\n"
            "requester: orchestrator\n"
            "target: operator\n"
            "state: inbox\n"
            "parent_ticket: null\n"
            "depends_on: []\n"
            "ratifiable_files: [pantheon-config:sop/standard/foo.md]\n"
            "intent: ratify\n"
            "---\n"
            "\n# Test\n\n## Ask\n\nRatify.\n\n## Transition log\n\n"
            "- 2026-06-01T00:00:00Z [orchestrator] filed in inbox\n"
        )
        text = self._run_retarget(content, intent_arg=None, to="codex")
        fields, _ = ticket.parse_front_matter(text)
        self.assertIsNone(fields.get("intent"))  # dropped on the away leg
        self.assertIn("auto-restores on return to operator", text)  # self-documented


class TestCommitMessageTemplates(unittest.TestCase):
    def test_file_subject(self) -> None:
        msg = ticket._build_commit_message("file", "T-0001", "My title", None)
        self.assertTrue(
            msg.startswith("tickets: file T-0001 — My title"),
            f"got {msg!r}",
        )

    def test_claim_subject(self) -> None:
        msg = ticket._build_commit_message("claim", "T-0001", None, None)
        self.assertEqual(msg.strip(), "tickets: claim T-0001")

    def test_block_subject_with_short_reason(self) -> None:
        msg = ticket._build_commit_message("block", "T-0001", None, "waiting on review")
        self.assertIn("tickets: block T-0001 — waiting on review", msg)

    def test_block_subject_with_long_reason_truncates_subject(self) -> None:
        long_reason = "x" * 100
        msg = ticket._build_commit_message("block", "T-0001", None, long_reason)
        subject = msg.splitlines()[0]
        # Subject contains a truncation marker; full reason in body.
        self.assertIn("...", subject)
        self.assertIn(long_reason, msg)

    def test_resolve_subject_no_summary(self) -> None:
        msg = ticket._build_commit_message("resolve", "T-0001", None, None)
        self.assertEqual(msg.strip(), "tickets: resolve T-0001")

    def test_resolve_subject_with_summary(self) -> None:
        msg = ticket._build_commit_message("resolve", "T-0001", None, "fixed it")
        self.assertIn("tickets: resolve T-0001 — fixed it", msg)

    def test_no_co_authored_by_trailer(self) -> None:
        # ORG-019: "unblock" verb removed; test surviving verbs only.
        for verb in ("file", "claim", "block", "resolve"):
            detail = "x" if verb in ("block",) else None
            title = "t" if verb == "file" else None
            msg = ticket._build_commit_message(verb, "T-0001", title, detail)
            self.assertNotIn("Co-Authored-By", msg)
            self.assertNotIn("Co-authored-by", msg)


# ---------------------------------------------------------------------------
# README table rebuild
# ---------------------------------------------------------------------------


class TestReadmeTableRebuild(unittest.TestCase):
    """Tests for _rebuild_readme_table function that regenerates the
    Current state table in tickets/README.md from filesystem state.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_readme(self, content: str) -> Path:
        """Write a README.md file and return its path."""
        readme = self.root / "README.md"
        readme.write_text(content, encoding="utf-8")
        return readme

    def test_table_sorts_by_id_descending(self) -> None:
        """Table rows appear in descending ID order (T-0003, T-0002, T-0001)."""
        _write_ticket(
            self.root / "done" / "T-0001.md",
            tid="T-0001",
            state="done",
            title="First ticket",
            created="2026-05-01T10:00:00Z",
        )
        _write_ticket(
            self.root / "active" / "T-0002.md",
            tid="T-0002",
            state="active",
            title="Second ticket",
            created="2026-05-01T11:00:00Z",
        )
        _write_ticket(
            self.root / "inbox" / "T-0003.md",
            tid="T-0003",
            state="inbox",
            title="Third ticket",
            created="2026-05-01T12:00:00Z",
        )
        readme_content = (
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
        )
        readme = self._write_readme(readme_content)
        ticket._rebuild_readme_table(self.root, readme)

        result = readme.read_text(encoding="utf-8")
        # Extract table rows (skip header + separator)
        lines = result.splitlines()
        table_start = lines.index("| ID | State | Title | Requester → Target | Filed |")
        row_lines = [
            ln for ln in lines[table_start + 2 :] if ln.strip().startswith("|")
        ]
        # First data row should be T-0003, last should be T-0001
        self.assertIn("T-0003", row_lines[0])
        self.assertIn("T-0001", row_lines[-1])

    def test_table_is_idempotent(self) -> None:
        """Running rebuild twice on same state produces identical output."""
        _write_ticket(
            self.root / "inbox" / "T-0001.md",
            tid="T-0001",
            state="inbox",
            title="Test",
            created="2026-05-01T10:00:00Z",
        )
        readme_content = (
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
        )
        readme = self._write_readme(readme_content)
        ticket._rebuild_readme_table(self.root, readme)
        first = readme.read_text(encoding="utf-8")
        ticket._rebuild_readme_table(self.root, readme)
        second = readme.read_text(encoding="utf-8")
        self.assertEqual(first, second)

    def test_related_section_preserved(self) -> None:
        """Content after the table (e.g. ## Related) is preserved intact."""
        _write_ticket(
            self.root / "inbox" / "T-0001.md",
            tid="T-0001",
            state="inbox",
            title="Test",
            created="2026-05-01T10:00:00Z",
        )
        readme_content = (
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "| T-0099 | old | old title | x → y | 2026-01-01 |\n"
            "\n"
            "## Related\n\n"
            "- Important link preserved\n"
        )
        readme = self._write_readme(readme_content)
        ticket._rebuild_readme_table(self.root, readme)
        result = readme.read_text(encoding="utf-8")
        self.assertIn("## Related", result)
        self.assertIn("Important link preserved", result)

    def test_table_reflects_all_three_states(self) -> None:
        """Tickets in inbox, active, done all appear in table. ORG-019: no blocked/."""
        _write_ticket(
            self.root / "inbox" / "T-0001.md",
            tid="T-0001",
            state="inbox",
            title="Inbox ticket",
            created="2026-05-01T10:00:00Z",
        )
        _write_ticket(
            self.root / "active" / "T-0002.md",
            tid="T-0002",
            state="active",
            title="Active ticket",
            created="2026-05-01T11:00:00Z",
        )
        _write_ticket(
            self.root / "done" / "T-0004.md",
            tid="T-0004",
            state="done",
            title="Done ticket",
            created="2026-05-01T13:00:00Z",
        )
        readme_content = (
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
        )
        readme = self._write_readme(readme_content)
        ticket._rebuild_readme_table(self.root, readme)
        result = readme.read_text(encoding="utf-8")
        for tid in ("T-0001", "T-0002", "T-0004"):
            self.assertIn(tid, result)
        for state in ("inbox", "active", "done"):
            self.assertIn(state, result)

    def test_rebuild_returns_true_when_content_changed(self) -> None:
        """First rebuild after seeding returns True (content differs from empty table)."""
        _write_ticket(self.root / "inbox" / "T-0001.md", tid="T-0001", state="inbox", title="t", created="2026-05-19T00:00:00Z")
        readme_content = (
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
        )
        readme = self._write_readme(readme_content)
        changed = ticket._rebuild_readme_table(self.root, readme)
        self.assertTrue(changed, "first rebuild should report content changed")

    def test_rebuild_returns_false_when_content_unchanged(self) -> None:
        """Idempotent second rebuild returns False (post-ORG-019 block-subcommand
        edits frontmatter in place; if README content is byte-identical, scope
        guard would trip when README is staged with no diff — caller relies on
        this return-False signal to skip staging README in that case.
        """
        _write_ticket(self.root / "inbox" / "T-0001.md", tid="T-0001", state="inbox", title="t", created="2026-05-19T00:00:00Z")
        readme_content = (
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
        )
        readme = self._write_readme(readme_content)
        # First rebuild: True (writes the table)
        first = ticket._rebuild_readme_table(self.root, readme)
        self.assertTrue(first)
        # Second rebuild on the same on-disk state: False (no-op)
        second = ticket._rebuild_readme_table(self.root, readme)
        self.assertFalse(second, "second rebuild on unchanged state should report no-change")

    def test_write_permission_error_degrades_gracefully(self) -> None:
        """When write_text raises PermissionError, _rebuild_readme_table returns
        False and emits a warning to stderr without propagating the exception.

        Regression guard for T-1003: cross-owner README on the multi-writer VPS
        (kino-uid-created file at 0644 blocks claude-user ticket ops).
        """
        import io
        _write_ticket(
            self.root / "inbox" / "T-0001.md",
            tid="T-0001",
            state="inbox",
            title="Test ticket",
            created="2026-06-09T00:00:00Z",
        )
        readme_content = (
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
        )
        readme = self._write_readme(readme_content)

        original_write_text = Path.write_text

        def raise_permission_error(self_path, *args, **kwargs):
            if self_path == readme:
                raise PermissionError(13, "Permission denied")
            return original_write_text(self_path, *args, **kwargs)

        stderr_capture = io.StringIO()
        with patch.object(Path, "write_text", raise_permission_error), \
             patch("sys.stderr", stderr_capture):
            result = ticket._rebuild_readme_table(self.root, readme)

        self.assertFalse(result, "should return False when write was skipped due to PermissionError")
        warning_text = stderr_capture.getvalue()
        self.assertIn(str(readme), warning_text, "warning should name the readme path")
        self.assertTrue(
            len(warning_text) > 0,
            "warning should be emitted to stderr",
        )

    def test_write_oserror_degrades_gracefully(self) -> None:
        """When write_text raises a generic OSError (e.g. ENOSPC), same graceful-
        degrade: returns False and warns to stderr without propagating.
        """
        import io
        _write_ticket(
            self.root / "inbox" / "T-0001.md",
            tid="T-0001",
            state="inbox",
            title="Test ticket",
            created="2026-06-09T00:00:00Z",
        )
        readme_content = (
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
        )
        readme = self._write_readme(readme_content)

        original_write_text = Path.write_text

        def raise_os_error(self_path, *args, **kwargs):
            if self_path == readme:
                raise OSError(28, "No space left on device")
            return original_write_text(self_path, *args, **kwargs)

        stderr_capture = io.StringIO()
        with patch.object(Path, "write_text", raise_os_error), \
             patch("sys.stderr", stderr_capture):
            result = ticket._rebuild_readme_table(self.root, readme)

        self.assertFalse(result, "should return False when write was skipped due to OSError")
        warning_text = stderr_capture.getvalue()
        self.assertTrue(
            len(warning_text) > 0,
            "warning should be emitted to stderr",
        )


class TestReadmeDecay(unittest.TestCase):
    """Tests for _partition_done_rows — the pure helper that splits done tickets
    into full table rows and monthly digest lines based on README_DONE_ROW_FLOOR.
    """

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _make_done_ticket(self, tid: str, created: str) -> "ticket.Ticket":
        """Build a minimal Ticket object with the given id and created timestamp."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / f"{tid}.md"
        _write_ticket(path, tid=tid, state="done", title=f"Title {tid}", created=created)
        return ticket.parse_ticket(path)

    def _make_done_batch(
        self, count: int, *, id_start: int = 1, created: str = "2026-05-01T00:00:00Z"
    ) -> list["ticket.Ticket"]:
        """Make ``count`` done tickets with IDs T-<id_start> … T-<id_start+count-1>."""
        tickets = [
            self._make_done_ticket(
                f"T-{id_start + i:04d}", created
            )
            for i in range(count)
        ]
        # Caller-owns the sort (descending by ID, highest first)
        tickets.sort(key=lambda t: t.fields.get("id", ""), reverse=True)
        return tickets

    # -----------------------------------------------------------------------
    # Edge cases: empty and at-or-below floor
    # -----------------------------------------------------------------------

    def test_empty_done(self) -> None:
        """Zero done tickets → both output lists are empty."""
        full_rows, digest_lines = ticket._partition_done_rows([])
        self.assertEqual(full_rows, [])
        self.assertEqual(digest_lines, [])

    def test_floor_no_decay_exactly_40(self) -> None:
        """Exactly README_DONE_ROW_FLOOR done tickets → all become full rows, no digest."""
        k = ticket.README_DONE_ROW_FLOOR
        done = self._make_done_batch(k)
        full_rows, digest_lines = ticket._partition_done_rows(done)
        self.assertEqual(len(full_rows), k)
        self.assertEqual(digest_lines, [])

    def test_below_floor_no_decay(self) -> None:
        """Fewer than README_DONE_ROW_FLOOR done tickets → all full rows, no digest."""
        done = self._make_done_batch(ticket.README_DONE_ROW_FLOOR - 1)
        full_rows, digest_lines = ticket._partition_done_rows(done)
        self.assertEqual(len(full_rows), ticket.README_DONE_ROW_FLOOR - 1)
        self.assertEqual(digest_lines, [])

    # -----------------------------------------------------------------------
    # Decay fires for >floor done tickets
    # -----------------------------------------------------------------------

    def test_floor_plus_one_produces_one_digest(self) -> None:
        """README_DONE_ROW_FLOOR + 1 done tickets in one month → 40 full rows + 1 digest."""
        k = ticket.README_DONE_ROW_FLOOR
        done = self._make_done_batch(k + 1, created="2026-05-01T00:00:00Z")
        full_rows, digest_lines = ticket._partition_done_rows(done)
        self.assertEqual(len(full_rows), k)
        self.assertEqual(len(digest_lines), 1)

    def test_full_rows_are_highest_ids(self) -> None:
        """Full rows contain the top-K tickets by ID descending."""
        k = ticket.README_DONE_ROW_FLOOR
        done = self._make_done_batch(k + 5, id_start=1, created="2026-05-10T00:00:00Z")
        # done is sorted descending; top K are IDs k+5 down to 6
        full_rows, _ = ticket._partition_done_rows(done)
        # The first full row should be the highest ID
        top_id = f"T-{k + 5:04d}"
        self.assertIn(top_id, full_rows[0])
        # The last full row should be the k-th highest ID (ID = 6)
        bottom_full_id = f"T-{6:04d}"
        self.assertIn(bottom_full_id, full_rows[-1])

    # -----------------------------------------------------------------------
    # Digest correctness: count and id-range
    # -----------------------------------------------------------------------

    def test_digest_count_and_id_range_single_month(self) -> None:
        """Digest line for a single-month collapse has correct count and T-lo/T-hi."""
        k = ticket.README_DONE_ROW_FLOOR
        # k+3 tickets in one month → 3 collapse into digest
        done = self._make_done_batch(k + 3, id_start=1, created="2026-05-01T00:00:00Z")
        _, digest_lines = ticket._partition_done_rows(done)
        self.assertEqual(len(digest_lines), 1)
        line = digest_lines[0]
        # Should mention the month
        self.assertIn("2026-05", line)
        # Should report count = 3
        self.assertIn("3 done", line)
        # Should report ID range T-0001–T-0003 (the 3 lowest IDs)
        self.assertIn("T-0001", line)
        self.assertIn("T-0003", line)

    def test_digest_two_months_sorted_descending(self) -> None:
        """Two months of collapsed done tickets → two digest lines, newer month first."""
        k = ticket.README_DONE_ROW_FLOOR
        # Tickets 1-5: May 2026 (older IDs, will be in digest)
        may_tickets = [
            self._make_done_ticket(f"T-{i:04d}", "2026-05-01T00:00:00Z")
            for i in range(1, 6)
        ]
        # Tickets 6-10: June 2026 (older IDs, will also be in digest)
        jun_tickets = [
            self._make_done_ticket(f"T-{i:04d}", "2026-06-01T00:00:00Z")
            for i in range(6, 11)
        ]
        # Tickets 11 through k+10: July 2026 (these become the full rows)
        jul_tickets = [
            self._make_done_ticket(f"T-{i:04d}", "2026-07-01T00:00:00Z")
            for i in range(11, k + 11)
        ]
        all_done = may_tickets + jun_tickets + jul_tickets
        all_done.sort(key=lambda t: t.fields.get("id", ""), reverse=True)

        full_rows, digest_lines = ticket._partition_done_rows(all_done)
        self.assertEqual(len(full_rows), k)
        # Should have 2 digest lines (May + June)
        self.assertEqual(len(digest_lines), 2)
        # First digest line (index 0) should be the newer month (June)
        self.assertIn("2026-06", digest_lines[0])
        # Second digest line (index 1) should be the older month (May)
        self.assertIn("2026-05", digest_lines[1])

    # -----------------------------------------------------------------------
    # Straddle: a month where some tickets are in full rows and some in digest
    # -----------------------------------------------------------------------

    def test_boundary_month_straddle(self) -> None:
        """When a created-month straddles the floor, the digest line reflects
        ONLY the collapsed subset (count and T-lo/T-hi), not the whole month.
        """
        k = ticket.README_DONE_ROW_FLOOR
        # 5 tickets in May (IDs 1-5) — all will be in digest (low IDs)
        may_old = [
            self._make_done_ticket(f"T-{i:04d}", "2026-05-01T00:00:00Z")
            for i in range(1, 6)
        ]
        # 5 tickets in May (IDs k+1 to k+5) — all will be in full rows (high IDs)
        may_new = [
            self._make_done_ticket(f"T-{k + i:04d}", "2026-05-15T00:00:00Z")
            for i in range(1, 6)
        ]
        # k-5 tickets in April (IDs 6 to k) — these fill up the remaining full-row slots
        apr_tickets = [
            self._make_done_ticket(f"T-{i:04d}", "2026-04-01T00:00:00Z")
            for i in range(6, k + 1)
        ]
        all_done = may_old + may_new + apr_tickets
        all_done.sort(key=lambda t: t.fields.get("id", ""), reverse=True)

        # Total = 5 + 5 + (k-5) = k+5, so 5 tickets go to digest.
        # The top-k by ID are: may_new (IDs k+1..k+5) + apr_tickets (IDs 6..k) = k tickets.
        # The 5 in digest are: may_old (IDs 1-5), all May.
        full_rows, digest_lines = ticket._partition_done_rows(all_done)
        self.assertEqual(len(full_rows), k)

        # Find the May digest line
        may_digest = [ln for ln in digest_lines if "2026-05" in ln]
        self.assertEqual(len(may_digest), 1, "should be exactly one May digest line")
        line = may_digest[0]

        # The collapsed May subset is ONLY T-0001..T-0005 (the 5 low-ID May tickets).
        # The May tickets in full rows (T-k+1..T-k+5) must NOT affect the digest count/range.
        self.assertIn("5 done", line)
        self.assertIn("T-0001", line)  # lo end of the collapsed May subset
        self.assertIn("T-0005", line)  # hi end of the collapsed May subset

        # Confirm that the high-ID May tickets (k+1 through k+5) appear in full rows
        for i in range(1, 6):
            high_id = f"T-{k + i:04d}"
            self.assertTrue(
                any(high_id in row for row in full_rows),
                f"{high_id} should be in full rows (above floor), not in digest",
            )

    # -----------------------------------------------------------------------
    # Idempotency through _rebuild_readme_table with decay active
    # -----------------------------------------------------------------------

    def test_idempotency_with_decay(self) -> None:
        """Running _rebuild_readme_table twice with >40 done tickets produces
        identical README output both times.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for s in ticket.STATES:
            (root / s).mkdir(parents=True)

        k = ticket.README_DONE_ROW_FLOOR
        # Create k+10 done tickets across two months
        for i in range(1, k + 11):
            month = "2026-05" if i <= k else "2026-06"
            _write_ticket(
                root / "done" / f"T-{i:04d}.md",
                tid=f"T-{i:04d}",
                state="done",
                title=f"Ticket {i}",
                created=f"{month}-01T00:00:00Z",
            )

        readme_content = (
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
        )
        readme = root / "README.md"
        readme.write_text(readme_content, encoding="utf-8")

        ticket._rebuild_readme_table(root, readme)
        first = readme.read_text(encoding="utf-8")
        ticket._rebuild_readme_table(root, readme)
        second = readme.read_text(encoding="utf-8")
        self.assertEqual(first, second, "rebuild must be idempotent with decay active")

    # -----------------------------------------------------------------------
    # Inbox and active tickets never decayed regardless of done count
    # -----------------------------------------------------------------------

    def test_inbox_active_never_decayed(self) -> None:
        """Inbox and active tickets always appear as full rows, never in digest,
        even when there are many done tickets.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for s in ticket.STATES:
            (root / s).mkdir(parents=True)

        k = ticket.README_DONE_ROW_FLOOR
        # More done tickets than floor
        for i in range(1, k + 5):
            _write_ticket(
                root / "done" / f"T-{i:04d}.md",
                tid=f"T-{i:04d}",
                state="done",
                title=f"Done {i}",
                created="2026-05-01T00:00:00Z",
            )
        # One inbox and one active ticket with high IDs
        _write_ticket(
            root / "inbox" / f"T-{k + 10:04d}.md",
            tid=f"T-{k + 10:04d}",
            state="inbox",
            title="Inbox ticket",
            created="2026-06-01T00:00:00Z",
        )
        _write_ticket(
            root / "active" / f"T-{k + 11:04d}.md",
            tid=f"T-{k + 11:04d}",
            state="active",
            title="Active ticket",
            created="2026-06-01T00:00:00Z",
        )

        readme_content = (
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
        )
        readme = root / "README.md"
        readme.write_text(readme_content, encoding="utf-8")
        ticket._rebuild_readme_table(root, readme)
        result = readme.read_text(encoding="utf-8")

        # Inbox and active must appear as full rows
        inbox_id = f"T-{k + 10:04d}"
        active_id = f"T-{k + 11:04d}"
        self.assertIn(inbox_id, result)
        self.assertIn(active_id, result)
        # They must not appear inside a digest line
        for line in result.splitlines():
            if "digest" in line:
                self.assertNotIn(inbox_id, line, "inbox ticket must not appear in digest")
                self.assertNotIn(active_id, line, "active ticket must not appear in digest")

    # -----------------------------------------------------------------------
    # Digest format shape
    # -----------------------------------------------------------------------

    def test_digest_line_format(self) -> None:
        """Digest lines match the 5-column table shape with grep hint."""
        k = ticket.README_DONE_ROW_FLOOR
        done = self._make_done_batch(k + 2, id_start=1, created="2026-05-01T00:00:00Z")
        _, digest_lines = ticket._partition_done_rows(done)
        self.assertEqual(len(digest_lines), 1)
        line = digest_lines[0]
        # Must be a table row (starts and ends with |)
        self.assertTrue(line.startswith("|"), f"digest line must start with |: {line!r}")
        self.assertTrue(line.endswith("|"), f"digest line must end with |: {line!r}")
        # Must have the "done (digest)" state marker
        self.assertIn("done (digest)", line)
        # Must have the grep hint
        self.assertIn("grep tickets/done/", line)
        # Must have exactly 4 pipe separators for 5 columns
        # (excluding leading and trailing pipes)
        inner = line[1:-1]
        self.assertEqual(inner.count("|"), 4, f"digest line must have 5 columns: {line!r}")


class TestFullLifecycleStagesReadme(unittest.TestCase):
    """Integration test: verify that lifecycle commands stage README.md in the
    same commit as the ticket transition.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._tickets_patch = patch.object(
            ticket, "TICKETS_ROOT", self.root / "tickets"
        )
        self._tickets_patch.start()
        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True, exist_ok=True)

        # Write a minimal README
        readme = self.root / "tickets" / "README.md"
        readme.write_text(
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
            "## Related\n\n"
            "- Link\n",
            encoding="utf-8",
        )

        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()

        def fake_run(argv, **kwargs):
            if argv[:2] == ["git", "mv"] and len(argv) >= 4:
                src = self.root / argv[2]
                dst = self.root / argv[3]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "add"] and "--" in argv:
                idx = argv.index("--")
                paths = argv[idx + 1 :]
                missing = [p for p in paths if not (self.root / p).exists()]
                if missing:
                    return MagicMock(
                        returncode=128,
                        stdout="",
                        stderr=(
                            "fatal: pathspec '"
                            + missing[0]
                            + "' did not match any files\n"
                        ),
                    )
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "diff"]:
                self.assertIn("--no-renames", argv)
                staged_set: set[str] = set()
                for prior in self.mock_run.call_args_list:
                    pa = prior.args[0]
                    if pa[:2] == ["git", "mv"] and len(pa) >= 4:
                        staged_set.add(pa[2])
                        staged_set.add(pa[3])
                    elif pa[:2] == ["git", "add"] and "--" in pa:
                        i = pa.index("--")
                        for p in pa[i + 1 :]:
                            if (self.root / p).exists():
                                staged_set.add(p)
                    elif pa[:2] == ["git", "commit"]:
                        staged_set.clear()
                staged = sorted(staged_set)
                return MagicMock(
                    returncode=0,
                    stdout="\n".join(staged) + ("\n" if staged else ""),
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_run

    def tearDown(self) -> None:
        self._sub_patch.stop()
        self._tickets_patch.stop()
        self._gu_repo_patch.stop()
        self._repo_patch.stop()
        self._tmp.cleanup()

    def test_full_lifecycle_stages_readme(self) -> None:
        """Lifecycle command includes README.md in git add call."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            # Seed a ticket in inbox
            _write_ticket(
                self.root / "tickets" / "inbox" / "T-0042.md",
                tid="T-0042",
                state="inbox",
                title="Test ticket",
                created="2026-05-01T10:00:00Z",
            )
            mark = len(self.mock_run.call_args_list)
            claim_args = MagicMock()
            claim_args.ticket = "T-0042"
            ticket.cmd_claim(claim_args)

            # Find the git add call and verify README was included
            add_calls = [
                c.args[0]
                for c in self.mock_run.call_args_list[mark:]
                if c.args[0][:2] == ["git", "add"]
            ]
            self.assertEqual(len(add_calls), 1)
            argv = add_calls[0]
            idx = argv.index("--")
            added_paths = argv[idx + 1 :]
            readme_rel = "tickets/README.md"
            self.assertIn(
                readme_rel,
                added_paths,
                f"README.md should be staged with ticket transition; got {added_paths!r}",
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_cmd_block_depends_on_only_does_not_stage_readme(self) -> None:
        """Integration guard for T-0453 Phase 2 regression: cmd_block with a
        depends_on-only gate on an inbox ticket must NOT stage README.md.

        Regression shape (pre-a938fc0): _rebuild_readme_table always returned
        None (implicitly), so the caller unconditionally appended README to
        affected_paths and staged it.  The scope-guard then saw
        staged = [ticket, README] but expected = [ticket, README] too — WAIT,
        that would PASS.  The actual failure was the opposite: if README had no
        real diff (byte-identical before/after rebuild), git diff --cached would
        report only the ticket file, while expected included README, so the
        scope-guard raised TicketError("scope guard refused").

        Fix (a938fc0): _rebuild_readme_table returns bool; caller skips README
        when False.  This test verifies the fix holds: cmd_block completes
        without exception and README is absent from every git-add call.
        """
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            # Seed an inbox ticket
            _write_ticket(
                self.root / "tickets" / "inbox" / "T-0099.md",
                tid="T-0099",
                state="inbox",
                title="Block integration target",
                created="2026-05-01T12:00:00Z",
            )

            # Pre-sync the README so it already contains T-0099's row.
            # After cmd_block runs, the ticket stays in inbox/ with the same
            # state, so _rebuild_readme_table produces byte-identical output
            # and must return False — README must NOT be staged.
            readme = self.root / "tickets" / "README.md"
            ticket._rebuild_readme_table(self.root / "tickets", readme)

            mark = len(self.mock_run.call_args_list)

            block_args = argparse.Namespace(
                ticket="T-0099",
                not_before=None,
                depends_on=["T-0001"],
                no_push=True,
            )
            rc = ticket.cmd_block(block_args)

            self.assertEqual(rc, 0, "cmd_block should return 0")

            # Collect every path passed to git add after the mark
            added_paths: list[str] = []
            for call in self.mock_run.call_args_list[mark:]:
                argv = call.args[0]
                if argv[:2] == ["git", "add"] and "--" in argv:
                    idx = argv.index("--")
                    added_paths.extend(argv[idx + 1:])

            readme_rel = "tickets/README.md"
            self.assertNotIn(
                readme_rel,
                added_paths,
                f"README.md must NOT be staged for a depends_on-only block "
                f"(scope-guard regression guard for T-0453 Ph2); got {added_paths!r}",
            )

            # Correctness: frontmatter must carry the new dep
            path = self.root / "tickets" / "inbox" / "T-0099.md"
            fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
            self.assertIn(
                "T-0001",
                fields.get("depends_on") or [],
                "depends_on frontmatter must include the newly added dep",
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]

    # --- T-1975: --clear-dep / --clear-deps (the manual inverse of --depends-on) ---

    def _seed_with_deps(self, deps: list[str]) -> Path:
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        p = self.root / "tickets" / "inbox" / "T-0099.md"
        _write_ticket(p, tid="T-0099", state="inbox", title="dep-clear target",
                      created="2026-05-01T12:00:00Z", depends_on=deps)
        return p

    def _deps_of(self, p: Path) -> list[str]:
        fields, _ = ticket.parse_front_matter(p.read_text(encoding="utf-8"))
        return fields.get("depends_on") or []

    def _block_args(self, **kw) -> argparse.Namespace:
        base = dict(ticket="T-0099", not_before=None, depends_on=None,
                    clear_not_before=False, clear_dep=None, clear_deps=False, no_push=True)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_clear_dep_removes_one_named_dep(self) -> None:
        try:
            p = self._seed_with_deps(["T-0002", "T-0003"])
            rc = ticket.cmd_block(self._block_args(clear_dep=["T-0002"]))
            self.assertEqual(rc, 0)
            self.assertEqual(self._deps_of(p), ["T-0003"])
            # transition log records the removal
            self.assertIn("depends_on gate(s) cleared: T-0002", p.read_text(encoding="utf-8"))
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_clear_deps_removes_all(self) -> None:
        try:
            p = self._seed_with_deps(["T-0002", "T-0003"])
            rc = ticket.cmd_block(self._block_args(clear_deps=True))
            self.assertEqual(rc, 0)
            self.assertEqual(self._deps_of(p), [])
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_clear_dep_absent_target_is_clean_error(self) -> None:
        """Named-but-absent dep → TicketError, not a silent no-op (T-1975 ruling)."""
        try:
            p = self._seed_with_deps(["T-0002"])
            with self.assertRaises(ticket.TicketError):
                ticket.cmd_block(self._block_args(clear_dep=["T-9999"]))
            # ticket unchanged
            self.assertEqual(self._deps_of(p), ["T-0002"])
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_clear_deps_on_empty_is_noop_no_commit(self) -> None:
        """Idempotent: --clear-deps names no target, so empty is a clean no-op."""
        try:
            self._seed_with_deps([])
            mark = len(self.mock_run.call_args_list)
            rc = ticket.cmd_block(self._block_args(clear_deps=True))
            self.assertEqual(rc, 0)
            # no commit attempted (no git ops after the mark)
            self.assertEqual(self.mock_run.call_args_list[mark:], [])
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_clear_flags_mutually_exclusive_with_add_and_each_other(self) -> None:
        try:
            self._seed_with_deps(["T-0002"])
            for bad in (
                dict(clear_dep=["T-0002"], depends_on=["T-0003"]),   # clear + add
                dict(clear_dep=["T-0002"], not_before="2027-01-01"),  # clear + add
                dict(clear_dep=["T-0002"], clear_deps=True),          # two clears
                dict(clear_deps=True, clear_not_before=True),         # two clears
            ):
                with self.assertRaises(ticket.TicketError):
                    ticket.cmd_block(self._block_args(**bad))
        finally:
            del os.environ["PANTHEON_ACTOR"]


# ---------------------------------------------------------------------------
# _next_ticket_id race-condition scenarios (T-0068)
# ---------------------------------------------------------------------------


class TestNextTicketIdRaceScenarios(unittest.TestCase):
    """Tests for the fetch-origin + scan-both-trees fix in _next_ticket_id.

    All five coverage-spec scenarios from T-0068:
      (a) local == origin
      (b) local < origin  (the observed race/bug)
      (c) local > origin  (post-pull-not-pushed window)
      (d) fetch failure
      (e) ls-tree non-zero / parse failure (fresh-clone edge case)

    Plus two retry-path scenarios from cmd_file's push-race wrapper.
    """

    def _make_ls_tree_output(self, *ticket_ids: str) -> str:
        """Build the stdout that `git ls-tree -r --name-only origin/main tickets/`
        would return for the given ticket IDs (all placed in inbox/ for simplicity).
        """
        lines = [f"tickets/inbox/{tid}.md" for tid in ticket_ids]
        return "\n".join(lines) + ("\n" if lines else "")

    def _git_mock(
        self,
        fetch_rc: int = 0,
        ls_tree_rc: int = 0,
        ls_tree_stdout: str = "",
    ):
        """Return a mock for `tools._git_utils.subprocess.run` that handles
        `git fetch origin main` and `git ls-tree` with the supplied return values,
        and returns success for everything else.
        """
        def fake_run(argv, **kwargs):
            if argv[:3] == ["git", "fetch", "origin"]:
                return MagicMock(returncode=fetch_rc, stdout="", stderr="fetch error" if fetch_rc != 0 else "")
            if argv[:2] == ["git", "ls-tree"]:
                return MagicMock(returncode=ls_tree_rc, stdout=ls_tree_stdout, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        return fake_run

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()

    def tearDown(self) -> None:
        self._tickets_patch.stop()
        self._tmp.cleanup()

    # --- scenario (a): local == origin ---

    def test_local_equals_origin_returns_max_plus_one(self) -> None:
        """Both local and origin see T-0041; expect T-0042."""
        (self.root / "inbox" / "T-0041.md").write_text("stub")
        ls_out = self._make_ls_tree_output("T-0041")
        with patch("tools._git_utils.subprocess.run", side_effect=self._git_mock(ls_tree_stdout=ls_out)):
            result = ticket._next_ticket_id()
        self.assertEqual(result, "T-0042")

    # --- scenario (b): local < origin (the race/bug) ---

    def test_origin_ahead_of_local_uses_origin_max(self) -> None:
        """Local sees T-0041; origin sees T-0042 (another agent filed first).
        Must return T-0043, NOT T-0042 (which would re-collide).
        """
        (self.root / "inbox" / "T-0041.md").write_text("stub")
        ls_out = self._make_ls_tree_output("T-0041", "T-0042")
        with patch("tools._git_utils.subprocess.run", side_effect=self._git_mock(ls_tree_stdout=ls_out)):
            result = ticket._next_ticket_id()
        self.assertEqual(result, "T-0043")

    # --- scenario (c): local > origin (post-pull-not-pushed window) ---

    def test_local_ahead_of_origin_uses_local_max(self) -> None:
        """Local has T-0043 (filed but not yet pushed); origin only sees T-0041.
        Must return T-0044, not T-0042.
        """
        (self.root / "inbox" / "T-0043.md").write_text("stub")
        ls_out = self._make_ls_tree_output("T-0041")
        with patch("tools._git_utils.subprocess.run", side_effect=self._git_mock(ls_tree_stdout=ls_out)):
            result = ticket._next_ticket_id()
        self.assertEqual(result, "T-0044")

    # --- scenario (d): fetch failure ---

    def test_fetch_failure_raises_ticket_error(self) -> None:
        """If `git fetch origin main` fails, _next_ticket_id must raise TicketError.
        Proceeding with a stale local view would recreate the race.
        """
        with patch("tools._git_utils.subprocess.run", side_effect=self._git_mock(fetch_rc=1)):
            with self.assertRaises(ticket.TicketError) as ctx:
                ticket._next_ticket_id()
        self.assertIn("git fetch failed", str(ctx.exception))

    # --- scenario (e): ls-tree non-zero (fresh clone / origin/main absent) ---

    def test_ls_tree_parse_failure_silently_uses_zero(self) -> None:
        """If `git ls-tree` returns non-zero (e.g. origin/main doesn't exist),
        origin_max falls back to 0 silently; local max wins.
        """
        (self.root / "inbox" / "T-0005.md").write_text("stub")
        with patch("tools._git_utils.subprocess.run", side_effect=self._git_mock(ls_tree_rc=128)):
            result = ticket._next_ticket_id()
        self.assertEqual(result, "T-0006")

    # --- empty-repo baseline (both sides zero) ---

    def test_empty_local_and_empty_origin_returns_T0001(self) -> None:
        """Both local and origin have no tickets; expect T-0001."""
        with patch("tools._git_utils.subprocess.run", side_effect=self._git_mock(ls_tree_stdout="")):
            result = ticket._next_ticket_id()
        self.assertEqual(result, "T-0001")


# ---------------------------------------------------------------------------
# cmd_file push-race retry scenarios (T-0068)
# ---------------------------------------------------------------------------


class TestCmdFilePushRaceRetry(unittest.TestCase):
    """Tests for the push-race retry wrapper in cmd_file.

    Verifies that on non-FF push failure, cmd_file:
      - resets the uncommitted local commit (git reset HEAD~1)
      - deletes the locally-written ticket file
      - re-allocates an ID via _next_ticket_id (which fetches again)
      - succeeds on a subsequent attempt

    And that after MAX_FILE_RETRIES exhausted, TicketError is raised.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)

        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._tickets_patch = patch.object(
            ticket, "TICKETS_ROOT", self.root / "tickets"
        )
        self._tickets_patch.start()
        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True, exist_ok=True)

        # Minimal README so table rebuild doesn't fail.
        readme = self.root / "tickets" / "README.md"
        readme.write_text(
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
            "## Related\n\n"
            "- Link\n",
            encoding="utf-8",
        )

        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()

    def tearDown(self) -> None:
        self._sub_patch.stop()
        self._tickets_patch.stop()
        self._gu_repo_patch.stop()
        self._repo_patch.stop()
        self._tmp.cleanup()

    def _make_fake_run(self, push_fail_count: int):
        """Return a fake_run that fails push `push_fail_count` times then succeeds.

        Also handles the git mv (for transition tests), git add (validates
        paths exist), git diff --cached (reflects staged files), and
        git reset HEAD~1 (no-op in working tree — just returns success).

        Crucially: after the first push failure, ls-tree starts returning
        `tickets/inbox/T-0001.md` to simulate a racing agent's push landing
        on origin during our failure window. This forces re-allocation to
        T-0002 on the retry, making the higher-ID invariant verifiable.
        """
        push_attempts = [0]

        def fake_ls_tree_stdout() -> str:
            # Before any push failure: origin appears empty (no tickets yet).
            # After first push failure: origin has T-0001 (the racing agent
            # pushed before we could; ours failed non-FF).
            if push_attempts[0] >= 1:
                return "tickets/inbox/T-0001.md\n"
            return ""

        def fake_run(argv, **kwargs):
            # fetch / ls-tree from _next_ticket_id
            if argv[:3] == ["git", "fetch", "origin"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "ls-tree"]:
                return MagicMock(returncode=0, stdout=fake_ls_tree_stdout(), stderr="")
            if argv[:2] in (["git", "pull"], ["git", "fetch"], ["git", "merge"]):
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "add"] and "--" in argv:
                idx = argv.index("--")
                paths = argv[idx + 1:]
                missing = [p for p in paths if not (self.root / p).exists()]
                if missing:
                    return MagicMock(
                        returncode=128, stdout="",
                        stderr=f"fatal: pathspec '{missing[0]}' did not match any files\n",
                    )
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "diff"]:
                # Reflect staged paths from prior add calls.
                staged_set: set[str] = set()
                for prior in self.mock_run.call_args_list:
                    pa = prior.args[0]
                    if pa[:2] == ["git", "add"] and "--" in pa:
                        i = pa.index("--")
                        for p in pa[i + 1:]:
                            if (self.root / p).exists():
                                staged_set.add(p)
                    elif pa[:2] == ["git", "commit"]:
                        staged_set.clear()
                    elif pa[:2] == ["git", "reset"]:
                        staged_set.clear()
                staged = sorted(staged_set)
                return MagicMock(
                    returncode=0,
                    stdout="\n".join(staged) + ("\n" if staged else ""),
                    stderr="",
                )
            if argv[:2] == ["git", "commit"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "reset"]:
                # Soft reset HEAD~1 — working tree untouched in real git;
                # our mock just returns success (file deletion is done by
                # cmd_file before calling reset).
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "push"]:
                push_attempts[0] += 1
                if push_attempts[0] <= push_fail_count:
                    return MagicMock(
                        returncode=1, stdout="",
                        stderr="! [rejected] main -> main (non-fast-forward)\n",
                    )
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        return fake_run

    def _make_file_args(self) -> MagicMock:
        args = MagicMock()
        args.requester = "orchestrator"
        args.target = "recovery"
        args.title = "Race retry test ticket"
        args.labels = None
        args.depends_on = None
        args.stale_after = None
        args.not_before = None
        args.ratifiable_file = None
        args.ask = "test"
        args.context = "test context"
        args.parent_ticket = None
        args.no_push = False
        args.spawn_impl = False
        args.terminal = False
        args.impl_title = None
        args.impl_ask = None
        args.appetite = None  # T-3440/ORG-089: getattr-read default
        args.bet_ref = None
        args.impl_target = None
        # T-3328: this class tests push-race retry mechanics, orthogonal to
        # DAG connectivity — orphan escape hatch (individual tests override
        # args.parent_ticket where a real edge is the thing under test, which
        # coexists harmlessly with this default).
        args.orphan = "test fixture: push-race retry mechanics"
        return args

    def test_parent_ticket_flag_lands_in_front_matter(self) -> None:
        """--parent-ticket T-NNNN value writes parent_ticket: T-NNNN in front-matter."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            self.mock_run.side_effect = self._make_fake_run(0)
            args = self._make_file_args()
            args.parent_ticket = "T-0042"
            ticket.cmd_file(args)
            inbox_files = list((self.root / "tickets" / "inbox").iterdir())
            self.assertEqual(len(inbox_files), 1)
            text = inbox_files[0].read_text(encoding="utf-8")
            self.assertIn("parent_ticket: T-0042", text)
        finally:
            os.environ.pop("PANTHEON_ACTOR", None)

    def test_parent_ticket_flag_default_none(self) -> None:
        """No --parent-ticket flag → parent_ticket stays null in front-matter."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            self.mock_run.side_effect = self._make_fake_run(0)
            args = self._make_file_args()
            # args.parent_ticket = None (default)
            ticket.cmd_file(args)
            inbox_files = list((self.root / "tickets" / "inbox").iterdir())
            text = inbox_files[0].read_text(encoding="utf-8")
            self.assertIn("parent_ticket: null", text)
        finally:
            os.environ.pop("PANTHEON_ACTOR", None)

    def test_parent_ticket_flag_malformed_raises(self) -> None:
        """--parent-ticket with non-T-NNNN value raises TicketError."""
        self.mock_run.side_effect = self._make_fake_run(0)
        args = self._make_file_args()
        args.parent_ticket = "not-a-ticket-id"
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_file(args)
        self.assertIn("does not match the T-NNNN format", str(ctx.exception))

    def test_cmd_file_retries_on_push_race_and_succeeds(self) -> None:
        """Push fails once (non-FF), succeeds on second attempt.

        The mock simulates a racing agent: on attempt 1 origin looks empty
        (local_max=0, origin_max=0 → T-0001 allocated, push fails non-FF).
        After that failure, ls-tree returns T-0001 (the racer's ticket now
        on origin). Retry re-allocates T-0002 and pushes successfully.

        Assertions:
          - return value is 0
          - exactly one .md file survives in inbox
          - surviving file stem is T-0002 (higher ID — proves re-allocation
            happened, not a silent re-push of the colliding T-0001)
          - exactly two git push calls were made (first failed, second ok)
        """
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            self.mock_run.side_effect = self._make_fake_run(push_fail_count=1)
            result = ticket.cmd_file(self._make_file_args())
            self.assertEqual(result, 0)
            inbox = list((self.root / "tickets" / "inbox").iterdir())
            # Exactly one ticket file survives (first attempt's file was cleaned up).
            md_files = [f for f in inbox if f.suffix == ".md" and f.name != "README.md"]
            self.assertEqual(len(md_files), 1, f"expected 1 ticket in inbox, got {md_files!r}")
            # Surviving file must be T-0002 — re-allocation produced a higher ID.
            self.assertEqual(
                md_files[0].stem, "T-0002",
                f"expected T-0002 after retry (origin had T-0001); got {md_files[0].stem!r}",
            )
            # Exactly two push calls: first failed, second succeeded.
            push_calls = [
                c for c in self.mock_run.call_args_list
                if c.args[0][:2] == ["git", "push"]
            ]
            self.assertEqual(len(push_calls), 2, f"expected 2 push attempts, got {len(push_calls)}")
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_push_race_restores_readme_so_next_op_is_not_wedged(self) -> None:
        """T-2172: the undone commit also carried the regenerated
        tickets/README.md table, and a soft reset leaves that regeneration in
        the working tree. The scope guard then refuses EVERY subsequent ticket
        op in that clone until someone cleans it by hand — one push race
        silently wedges the whole board.

        Assert the retry path restores the README after the reset. This test
        FAILS without the fix (no checkout call is issued at all).
        """
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            self.mock_run.side_effect = self._make_fake_run(push_fail_count=1)
            self.assertEqual(ticket.cmd_file(self._make_file_args()), 0)

            calls = [c.args[0] for c in self.mock_run.call_args_list]
            reset_idx = next(
                (i for i, a in enumerate(calls) if a[:2] == ["git", "reset"]
                 and "HEAD~1" in a), None,
            )
            self.assertIsNotNone(reset_idx, "expected a soft reset on the race path")

            restored = [
                i for i, a in enumerate(calls)
                if a[:2] == ["git", "checkout"] and "tickets/README.md" in a
            ]
            self.assertTrue(
                restored,
                "README was never restored after the race reset — the clone is "
                "left dirty and the scope guard will refuse all later ticket ops",
            )
            self.assertTrue(
                any(i > reset_idx for i in restored),
                "README restore must come AFTER the reset, or the reset "
                "re-dirties it",
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_spawn_impl_respawns_child_against_reallocated_tid_on_race(self) -> None:
        """T-0852 + push race: a ratify file with --spawn-impl that loses a push
        race must re-spawn the impl child against the RE-ALLOCATED ratify-tid (and
        unlink the stale child), so the surviving child's depends_on points at the
        final ratify id — never the abandoned one.

        attempt 1: ratify T-0001 / impl T-0002 → push fails → both unlinked.
        attempt 2: origin now shows T-0001 (racer) → ratify T-0002 / impl T-0003 → ok.
        """
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            self.mock_run.side_effect = self._make_fake_run(push_fail_count=1)
            args = self._make_file_args()
            args.target = "operator"
            args.ratifiable_file = ["pantheon-config:sop/standard/x.md"]
            args.intent = None  # auto-defaults to ratify
            args.spawn_impl = True
            args.impl_title = "Implement x"
            args.impl_target = "recovery"
            result = ticket.cmd_file(args)
            self.assertEqual(result, 0)
            inbox = list((self.root / "tickets" / "inbox").iterdir())
            md = sorted(f.stem for f in inbox if f.suffix == ".md" and f.name != "README.md")
            self.assertEqual(md, ["T-0002", "T-0003"], f"got {md!r}")
            ratify_text = (self.root / "tickets" / "inbox" / "T-0002.md").read_text()
            impl_text = (self.root / "tickets" / "inbox" / "T-0003.md").read_text()
            self.assertIn("intent: ratify", ratify_text)
            # Child re-bound to the FINAL ratify-tid, not the abandoned attempt-1 id.
            self.assertIn("depends_on: [T-0002]", impl_text)
            self.assertIn("parent_ticket: T-0002", impl_text)
            self.assertIn("target: recovery", impl_text)
            self.assertNotIn("T-0001", impl_text)
            push_calls = [
                c for c in self.mock_run.call_args_list
                if c.args[0][:2] == ["git", "push"]
            ]
            self.assertEqual(len(push_calls), 2)
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_cmd_file_raises_after_max_retries_exhausted(self) -> None:
        """Push fails all MAX_FILE_RETRIES times; TicketError must be raised.
        No ticket file should remain on disk after exhaustion.
        """
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            self.mock_run.side_effect = self._make_fake_run(push_fail_count=99)
            with self.assertRaises(ticket.TicketError) as ctx:
                ticket.cmd_file(self._make_file_args())
            self.assertIn("git push failed", str(ctx.exception))
            # Inbox should be empty (each attempt's file is cleaned up on failure).
            inbox = list((self.root / "tickets" / "inbox").iterdir())
            md_files = [f for f in inbox if f.suffix == ".md" and f.name != "README.md"]
            self.assertEqual(len(md_files), 0, f"stale files left after exhaustion: {md_files!r}")
        finally:
            del os.environ["PANTHEON_ACTOR"]


class TestRetarget(unittest.TestCase):
    """T-0181: retarget subcommand — front-matter target swap + transition log."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()
        self.mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()

    def tearDown(self) -> None:
        self._tickets_patch.stop()
        self._repo_patch.stop()
        self._gu_repo_patch.stop()
        self._sub_patch.stop()
        self._commit_patch.stop()
        self._tmp.cleanup()

    def _seed(self, tid: str, state: str, target: str = "recovery") -> Path:
        path = self.root / state / f"{tid}.md"
        _write_ticket(path, tid=tid, state=state, target=target)
        return path

    def _make_args(self, tid: str, to: str, note: str | None = None, no_push: bool = False, keep_state: bool = False) -> argparse.Namespace:
        ns = argparse.Namespace()
        ns.ticket = tid
        ns.to = to
        ns.note = note
        ns.no_push = no_push
        ns.keep_state = keep_state
        return ns

    def test_retarget_inbox_to_inbox_target_change(self) -> None:
        path = self._seed("T-0001", "inbox", target="recovery")
        rc = ticket.cmd_retarget(self._make_args("T-0001", "orchestrator"))
        self.assertEqual(rc, 0)
        # Frontmatter target updated
        text = path.read_text(encoding="utf-8")
        fields, _body = ticket.parse_front_matter(text)
        self.assertEqual(fields["target"], "orchestrator")
        # State unchanged (still in inbox/)
        self.assertTrue(path.exists())
        # Commit called
        self.mock_commit.assert_called_once()

    def test_retarget_active_to_active_target_change(self) -> None:
        path = self._seed("T-0002", "active", target="orchestrator")
        rc = ticket.cmd_retarget(self._make_args("T-0002", "recovery", keep_state=True))
        self.assertEqual(rc, 0)
        fields, _body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "recovery")
        self.assertTrue(path.exists())  # still in active/ (--keep-state preserves)

    def test_retarget_inbox_target_change(self) -> None:
        # ORG-019: blocked/ retired; retarget from inbox/ instead.
        path = self._seed("T-0003", "inbox", target="recovery")
        rc = ticket.cmd_retarget(self._make_args("T-0003", "specialist"))
        self.assertEqual(rc, 0)
        fields, _body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "specialist")
        self.assertTrue(path.exists())  # still in inbox/ (same target; no state change needed)

    def _seed_with_auto_delivered(self, tid: str, state: str, target: str) -> Path:
        """Seed a ticket carrying the T-0864 dispatch mutex stamps."""
        path = self._seed(tid, state, target=target)
        text = path.read_text(encoding="utf-8").replace(
            f"target: {target}",
            f"target: {target}\nauto_delivered: true"
            "\nauto_delivered_at: 2026-08-01T00:00:00Z",
            1,
        )
        path.write_text(text, encoding="utf-8")
        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        # Known-positive: prove the stamps are actually THERE before asserting
        # that a later step removed them. Without this the test would pass just
        # as happily against a seed that never carried them — the (h3) shape.
        assert fields.get("auto_delivered") is not None, "seed did not carry the stamp"
        return path

    def test_retarget_clears_the_auto_delivered_dispatch_mutex(self) -> None:
        """T-2350: retargeting must drop the PREVIOUS target's delivery stamp.

        `auto_delivered` is a dispatch mutex (T-0864 / T-0768): while it is set,
        inbox delivery for that ticket is suppressed. The cascade-unblock path
        pops it unconditionally for exactly this reason (T-0971 arm-A), but
        cmd_retarget did not — so a ticket that had already been auto-delivered
        to one agent and was then handed to another arrived carrying the old
        agent's stamp, and could sit undispatched for the new target with
        nothing reporting it. Silent by construction: the retarget succeeds, the
        target field is correct, and only delivery quietly never happens.
        """
        path = self._seed_with_auto_delivered("T-0009", "inbox", "recovery")
        rc = ticket.cmd_retarget(self._make_args("T-0009", "orchestrator"))
        self.assertEqual(rc, 0)
        fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "orchestrator")
        self.assertNotIn("auto_delivered", fields)
        self.assertNotIn("auto_delivered_at", fields)
        self.assertIn(
            "auto_delivered stamp cleared", body,
            "the clear must be visible in the transition log — a silent mutation "
            "of a dispatch-affecting field is what made this hard to find",
        )

    def test_retarget_without_the_stamp_is_unaffected(self) -> None:
        """Complement: the common case must not change, and popping an absent
        key must not raise. Guards the fix against being a behaviour change for
        every ordinary retarget."""
        path = self._seed("T-0010", "inbox", target="recovery")
        rc = ticket.cmd_retarget(self._make_args("T-0010", "orchestrator"))
        self.assertEqual(rc, 0)
        fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "orchestrator")
        self.assertNotIn("auto_delivered", fields)
        self.assertNotIn("auto_delivered stamp cleared", body)

    def test_retarget_done_refused(self) -> None:
        self._seed("T-0004", "done", target="recovery")
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_retarget(self._make_args("T-0004", "orchestrator"))
        self.assertIn("done", str(ctx.exception))
        self.mock_commit.assert_not_called()

    def test_retarget_unknown_persona_refused(self) -> None:
        self._seed("T-0005", "inbox", target="recovery")
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_retarget(self._make_args("T-0005", "nonexistent_agent"))
        self.assertIn("unknown target", str(ctx.exception))
        self.mock_commit.assert_not_called()

    def test_retarget_role_id_accepted(self) -> None:
        # T-0492 Amendment 3: role-IDs accepted as input but normalized to persona on write.
        path = self._seed("T-0006", "inbox", target="recovery")
        # "orchestrator" accepted and normalized to "orchestrator"
        rc = ticket.cmd_retarget(self._make_args("T-0006", "orchestrator"))
        self.assertEqual(rc, 0)
        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "orchestrator")
        # "specialist" accepted and normalized to "specialist"
        rc2 = ticket.cmd_retarget(self._make_args("T-0006", "specialist"))
        self.assertEqual(rc2, 0)
        fields2, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields2["target"], "specialist")

    def test_retarget_appends_transition_log_with_actor(self) -> None:
        path = self._seed("T-0007", "inbox", target="recovery")
        os.environ["PANTHEON_ACTOR"] = "test_actor"
        try:
            ticket.cmd_retarget(self._make_args("T-0007", "orchestrator"))
        finally:
            del os.environ["PANTHEON_ACTOR"]
        body = path.read_text(encoding="utf-8")
        self.assertIn("[test_actor] retargeted; recovery → orchestrator", body)

    def test_retarget_with_note_inline(self) -> None:
        path = self._seed("T-0008", "active", target="recovery")
        ticket.cmd_retarget(
            self._make_args("T-0008", "orchestrator", note="reply on design call", keep_state=True)
        )
        body = path.read_text(encoding="utf-8")
        self.assertIn("recovery → orchestrator", body)
        self.assertIn("reply on design call", body)

    def test_retarget_idempotence_no_change(self) -> None:
        path = self._seed("T-0009", "inbox", target="recovery")
        rc = ticket.cmd_retarget(self._make_args("T-0009", "recovery"))
        self.assertEqual(rc, 0)
        # No commit on no-op
        self.mock_commit.assert_not_called()
        # Body unchanged (no transition entry appended)
        body = path.read_text(encoding="utf-8")
        self.assertNotIn("retargeted", body)

    def test_retarget_no_push_threads_through_to_commit_and_push(self) -> None:
        """T-0236: --no-push on retarget threads to _commit_and_push kwarg."""
        self._seed("T-0010", "inbox", target="recovery")
        ticket.cmd_retarget(self._make_args("T-0010", "orchestrator", no_push=True))
        self.mock_commit.assert_called_once()
        self.assertTrue(self.mock_commit.call_args.kwargs.get("no_push"))

    def test_retarget_default_pushes_when_no_push_unset(self) -> None:
        """T-0236: --no-push absent (False default) → _commit_and_push called without no_push=True."""
        self._seed("T-0011", "inbox", target="recovery")
        ticket.cmd_retarget(self._make_args("T-0011", "orchestrator", no_push=False))
        self.mock_commit.assert_called_once()
        # no_push kwarg either absent or False — both satisfy "default pushes"
        self.assertFalse(self.mock_commit.call_args.kwargs.get("no_push", False))

    # -- T-3020 BT2: retargeted_at frontmatter field ----------------------

    def test_retarget_writes_retargeted_at_utc_iso8601(self) -> None:
        """Fresh retarget on a ticket with no prior retargeted_at writes one,
        UTC ISO-8601 second-precision Z-suffixed (matching `_now_iso()` shape)."""
        path = self._seed("T-0012", "inbox", target="recovery")
        fields_before, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertNotIn("retargeted_at", fields_before)

        rc = ticket.cmd_retarget(self._make_args("T-0012", "orchestrator"))
        self.assertEqual(rc, 0)

        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertRegex(
            fields["retargeted_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        )

    def test_retarget_second_retarget_updates_retargeted_at(self) -> None:
        """A second retarget UPDATES the field to the new write's timestamp —
        current value, not an append-only history.

        `_now_iso` is patched with `return_value` (not `side_effect`) per
        call, since cmd_retarget calls it more than once internally
        (append_transition stamps the log entry too) — pinning every call
        within one invocation to the same value keeps the assertion about
        call *order* inside cmd_retarget, not about it."""
        path = self._seed("T-0013", "inbox", target="recovery")

        with patch.object(ticket, "_now_iso", return_value="2026-08-01T00:00:00Z"):
            ticket.cmd_retarget(self._make_args("T-0013", "orchestrator"))
        fields_1, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields_1["retargeted_at"], "2026-08-01T00:00:00Z")

        with patch.object(ticket, "_now_iso", return_value="2026-08-02T00:00:00Z"):
            ticket.cmd_retarget(self._make_args("T-0013", "specialist"))
        fields_2, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields_2["retargeted_at"], "2026-08-02T00:00:00Z")

    def test_retarget_ticket_without_existing_field_is_retargetable(self) -> None:
        """Backwards-compat: a historic ticket with no retargeted_at field
        (the `_seed` fixture never writes one) reads fine and retargets
        cleanly — the field's absence never raises."""
        path = self._seed("T-0014", "active", target="recovery")
        text_before = path.read_text(encoding="utf-8")
        self.assertNotIn("retargeted_at", text_before)

        rc = ticket.cmd_retarget(self._make_args("T-0014", "orchestrator", keep_state=True))
        self.assertEqual(rc, 0)
        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "orchestrator")
        self.assertIn("retargeted_at", fields)

    def test_retarget_retargeted_at_lands_in_same_commit_as_target_change(self) -> None:
        """Transactional invariant: the retargeted_at write and the target
        change land in the SAME commit, not two. cmd_retarget performs one
        `path.write_text` + one `_commit_and_push` call per invocation, so
        proving single-commit + both-fields-present-post-call is sufficient
        evidence there was no separate second write/commit for the stamp."""
        path = self._seed("T-0015", "inbox", target="recovery")
        rc = ticket.cmd_retarget(self._make_args("T-0015", "orchestrator"))
        self.assertEqual(rc, 0)

        self.mock_commit.assert_called_once()
        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["target"], "orchestrator")
        self.assertRegex(
            fields["retargeted_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        )


# ---------------------------------------------------------------------------
# --no-push flag (T-0211 Phase A)
# ---------------------------------------------------------------------------



class TestAnnotate(unittest.TestCase):
    """T-1972: annotate — append a transition-log note with NO state change."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for st in ticket.STATES:
            (self.root / st).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()
        self.mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()

    def tearDown(self) -> None:
        self._tickets_patch.stop()
        self._repo_patch.stop()
        self._gu_repo_patch.stop()
        self._sub_patch.stop()
        self._commit_patch.stop()
        self._tmp.cleanup()

    def _seed(self, tid: str, state: str, target: str = "metis") -> Path:
        path = self.root / state / f"{tid}.md"
        _write_ticket(path, tid=tid, state=state, target=target)
        return path

    def _args(self, tid: str, note, no_push: bool = False) -> argparse.Namespace:
        ns = argparse.Namespace()
        ns.ticket = tid
        ns.note = note
        ns.no_push = no_push
        return ns

    def test_appends_note_transition_line(self) -> None:
        path = self._seed("T-0001", "inbox")
        rc = ticket.cmd_annotate(self._args("T-0001", "a free-form observation"))
        self.assertEqual(rc, 0)
        body = path.read_text(encoding="utf-8")
        # verb="note" so it is distinguishable from a state event
        self.assertRegex(body, r"\[[^\]]+\] note; a free-form observation")
        self.mock_commit.assert_called_once()

    def test_no_state_change_stays_in_inbox(self) -> None:
        self._seed("T-0002", "inbox")
        ticket.cmd_annotate(self._args("T-0002", "still inbox after this"))
        self.assertTrue((self.root / "inbox" / "T-0002.md").exists())
        self.assertFalse((self.root / "active" / "T-0002.md").exists())

    def test_annotate_active_ticket_ok(self) -> None:
        self._seed("T-0003", "active")
        rc = ticket.cmd_annotate(self._args("T-0003", "mid-work note"))
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "active" / "T-0003.md").exists())

    def test_empty_note_refused_no_commit(self) -> None:
        path = self._seed("T-0004", "inbox")
        before = path.read_text(encoding="utf-8")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_annotate(self._args("T-0004", ""))
        self.mock_commit.assert_not_called()
        self.assertEqual(path.read_text(encoding="utf-8"), before, "file must be untouched")

    def test_whitespace_only_note_refused(self) -> None:
        self._seed("T-0005", "inbox")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_annotate(self._args("T-0005", "   \t  "))
        self.mock_commit.assert_not_called()

    def test_none_note_refused(self) -> None:
        """Defensive: even if --note somehow arrives as None, refuse cleanly."""
        self._seed("T-0006", "inbox")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_annotate(self._args("T-0006", None))
        self.mock_commit.assert_not_called()

    def test_done_refused_no_write(self) -> None:
        path = self._seed("T-0007", "done")
        before = path.read_text(encoding="utf-8")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_annotate(self._args("T-0007", "should refuse on done"))
        self.mock_commit.assert_not_called()
        self.assertEqual(path.read_text(encoding="utf-8"), before, "done ticket untouched")

    def test_commit_message_shape(self) -> None:
        self._seed("T-0008", "inbox")
        ticket.cmd_annotate(self._args("T-0008", "the note text here"))
        args, _kwargs = self.mock_commit.call_args
        commit_message = args[1]
        self.assertTrue(
            commit_message.splitlines()[0].startswith("tickets: annotate T-0008"),
            commit_message,
        )


class TestAddendumPostResolution(unittest.TestCase):
    """T-2205: a resolved ticket's record had NO correction path.

    annotate hard-refuses on done/ and there is no reopen/amend, so a resolve
    note that later turned out to be wrong stayed confidently wrong on the
    surface a reader actually reads.
    """

    def _entry(self, note="a later correction"):
        body = (
            "# t\n\n## Transition log\n\n"
            "- 2026-07-29T00:00:00Z [recovery] resolved @abc1234; original outcome\n"
        )
        return ticket.append_transition(body, "recovery", ticket._ADDENDUM_VERB, note)

    def test_addendum_is_append_only_and_preserves_resolve_evidence(self) -> None:
        """(a) of the ask: never edits an existing line."""
        new_body = self._entry()
        self.assertIn(
            "- 2026-07-29T00:00:00Z [recovery] resolved @abc1234; original outcome",
            new_body,
            "the original resolve line must survive byte-for-byte",
        )

    def test_addendum_renders_after_the_resolve_line(self) -> None:
        """(b): reader sees original outcome and correction as ordered events."""
        new_body = self._entry()
        self.assertLess(
            new_body.index("original outcome"),
            new_body.index("a later correction"),
        )

    def test_addendum_is_visibly_marked_post_resolution(self) -> None:
        """(b): 'visibly marked' has to be legible to a human, not inferred."""
        line = [ln for ln in self._entry().splitlines() if ln.startswith("- ")][-1]
        self.assertIn("post-resolution", line)

    def test_addendum_is_not_miscounted_as_a_second_resolve(self) -> None:
        """The regression this verb was chosen to avoid.

        activity_digest.py and weekly_ticket_summary.py both scan these lines
        for resolves. A verb containing 'resolved' in verb position would make
        every correction register as an extra resolve and silently inflate all
        throughput reporting — a wrong number is worse than a missing one.
        """
        line = [ln for ln in self._entry().splitlines() if ln.startswith("- ")][-1]
        digest_re = re.compile(
            r"- (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \[.*?\] resolved")
        weekly_re = re.compile(
            r"^- (?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+\[(?P<actor>[^\]]+)\]\s+"
            r"(?P<action>resolved|resolve|close-action|close-noodle)\b")
        self.assertIsNone(digest_re.search(line), "addendum matched the digest resolve regex")
        self.assertIsNone(weekly_re.search(line), "addendum matched the weekly resolve regex")

    def test_addendum_carries_the_head_stamp(self) -> None:
        """(c): vintage legible — pairs with T-2203."""
        line = [ln for ln in self._entry().splitlines() if ln.startswith("- ")][-1]
        if "@" not in line:
            self.skipTest("no git HEAD available; stamp is fail-open here")
        self.assertRegex(line, r"@[0-9a-f]{7,40};")

    def test_addendum_refuses_when_not_done(self) -> None:
        """Mirror image of annotate's guard — the two domains stay disjoint."""
        args = argparse.Namespace(ticket="T-0001", note="x", no_push=True, no_pull=True)
        with patch.object(ticket, "_find_ticket",
                          return_value=Path("/repo/tickets/active/T-0001.md")):
            with self.assertRaises(ticket.TicketError) as cm:
                ticket.cmd_addendum(args)
        msg = str(cm.exception)
        self.assertIn("not `done`", msg)
        self.assertIn("annotate", msg, "must point the caller at the right tool")

    def test_addendum_refuses_empty_note(self) -> None:
        args = argparse.Namespace(ticket="T-0001", note="   ", no_push=True, no_pull=True)
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_addendum(args)

    def test_addendum_verb_is_known_to_the_commit_message_builder(self) -> None:
        """Caught by running it for real, not by the suite.

        _build_commit_message raises on an unrecognised verb, and the first
        live `addendum` invocation died there — AFTER the file had been
        written, leaving the ticket dirty with no commit. Both halves are
        pinned: the verb resolves, and (below) nothing is written before it.
        """
        msg = ticket._build_commit_message("addendum", "T-0001", "some title", "the note")
        self.assertTrue(msg.splitlines()[0].startswith("tickets: addendum T-0001"))

    def test_addendum_writes_nothing_when_the_commit_message_fails(self) -> None:
        """Write-then-abort is the T-0244 shape: refuse before touching disk."""
        with tempfile.TemporaryDirectory() as td:
            # Front matter must be COMPLETE. An incomplete fixture makes
            # _build_ticket_text raise "missing required field `created`"
            # BEFORE the write — which assertRaises(TicketError) happily
            # swallows, so the test passes without ever exercising the
            # ordering. That is exactly how the first version of this test
            # survived the mutation that should have killed it.
            original = (
                "---\n"
                "id: T-0001\n"
                "created: 2026-07-29T00:00:00Z\n"
                "requester: recovery\n"
                "target: recovery\n"
                "state: done\n"
                "parent_ticket: null\n"
                "depends_on: []\n"
                "---\n\n# t\n\n## Ask\n\nx\n\n## Transition log\n\n"
                "- 2026-07-29T00:00:00Z [recovery] resolved; done\n"
            )
            done_path = Path(td) / "done" / "T-0001.md"
            done_path.parent.mkdir(parents=True, exist_ok=True)
            done_path.write_text(original, encoding="utf-8")
            args = argparse.Namespace(
                ticket="T-0001", note="x", no_push=True, no_pull=True)
            with patch.object(ticket, "_find_ticket", return_value=done_path), \
                 patch.object(ticket, "_build_commit_message",
                              side_effect=ticket.TicketError("boom")), \
                 patch.object(ticket, "git_mutation_lock"), \
                 patch.object(ticket, "_pull_if_needed"):
                with self.assertRaises(ticket.TicketError) as cm:
                    ticket.cmd_addendum(args)
            # Pin the SPECIFIC failure, so an unrelated TicketError raised
            # earlier can never satisfy this test.
            self.assertEqual(str(cm.exception), "boom",
                             "test must fail on the commit-message step, not earlier")
            self.assertEqual(
                done_path.read_text(encoding="utf-8"), original,
                "file was modified despite the commit-message step failing",
            )

    def test_annotate_still_refuses_on_done(self) -> None:
        """The property T-2205 explicitly must NOT weaken."""
        args = argparse.Namespace(ticket="T-0001", note="x", no_push=True, no_pull=True)
        with patch.object(ticket, "_find_ticket",
                          return_value=Path("/repo/tickets/done/T-0001.md")):
            with self.assertRaises(ticket.TicketError) as cm:
                ticket.cmd_annotate(args)
        self.assertIn("done", str(cm.exception))


class TestPullFailureDegradation(unittest.TestCase):
    """T-2204: a dirty tree must not take the REPORTING path down with the
    remediation path.

    Before this, ticket.py's internal pull aborted on a dirty tree and every
    write in the clone failed — including annotate, the one command an agent
    would use to record what it had hit. The tool for reporting the problem
    died the same way as the tool for fixing it.

    T-2333 (this ticket): `pull_or_rebase_strict` fails in TWO distinguishable
    shapes with OPPOSITE truth about local state (ff-refused/not-ahead vs.
    rebase-conflict-on-local-commits), and the hint text must match which one
    actually happened — the sub-classes below exercise each shape plus the
    unrecognised-failure fallback.
    """

    def _fail_pull(self, detail: str = "error: Your local changes would be overwritten. Aborting"):
        """ff-refused / not-ahead shape — the one T-2204's tests were written
        against. Must carry `_git_utils.PULL_NOT_AHEAD_PREFIX` so the T-2333
        selector routes it to the (correct, unchanged) dirty-tree hint."""
        return unittest.mock.patch.object(
            ticket, "pull_or_rebase_strict",
            return_value=(False, f"{ticket.PULL_NOT_AHEAD_PREFIX}{detail}"),
        )

    def _fail_pull_rebase_conflict(
        self, detail: str = "Could not apply d181467e3... tickets: file T-2331",
    ):
        """rebase-conflict-on-local-commits shape (T-0846 self-heal path) —
        reachable only when a PRIOR op already committed locally. Must carry
        `_git_utils.PULL_REBASE_CONFLICT_PREFIX`."""
        return unittest.mock.patch.object(
            ticket, "pull_or_rebase_strict",
            return_value=(False, f"{ticket.PULL_REBASE_CONFLICT_PREFIX}{detail}"),
        )

    def _fail_pull_unknown(self, detail: str = "unexpected git state: bisect in progress"):
        """A failure matching NO prefix — must not inherit any invariant.

        T-2339 CHANGED THIS DEFAULT, and the reason matters more than the
        string. It used to be "git fetch failed: could not resolve host",
        which was a correct UNKNOWN example only because the fetch shape had
        no prefix of its own yet. Now it does
        (`PULL_FETCH_FAILED_PREFIX`), so that string routes to the FETCH hint
        and this fixture would have gone on passing while testing a case that
        is no longer unknown — a test that keeps its green by quietly
        changing what it covers. The default is now a shape with no prefix at
        all, which is what this fixture is actually for.
        """
        return unittest.mock.patch.object(
            ticket, "pull_or_rebase_strict", return_value=(False, detail),
        )

    def test_strict_pull_failure_still_raises(self) -> None:
        """The guardrail: ID-allocating / state-moving ops must NOT be relaxed."""
        with self._fail_pull():
            with self.assertRaises(ticket.TicketError):
                ticket._pull_if_needed(False)                 # default strict=True
            with self.assertRaises(ticket.TicketError):
                ticket._pull_if_needed(False, strict=True)

    def test_nonstrict_pull_failure_warns_and_continues(self) -> None:
        buf = io.StringIO()
        with self._fail_pull(), contextlib.redirect_stderr(buf):
            ticket._pull_if_needed(False, strict=False)       # must NOT raise
        err = buf.getvalue()
        self.assertIn("WARNING", err)
        self.assertIn("Aborting", err, "the underlying git error must survive")

    def test_failure_message_names_cause_and_fix_in_both_paths(self) -> None:
        """Option 3 of the ask: git's own text never says ticket.py pulled.

        A message that reproduces git's wording alone sends the reader looking
        for a merge they never ran, which is why this is asserted rather than
        left to the docstring. This is the ff/dirty-tree shape (T-2204's
        original scenario) — T-2333 requires this arm's wording NOT regress.
        """
        with self._fail_pull():
            with self.assertRaises(ticket.TicketError) as cm:
                ticket._pull_if_needed(False)
            strict_msg = str(cm.exception)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                ticket._pull_if_needed(False, strict=False)
            warn_msg = buf.getvalue()
        for msg, label in ((strict_msg, "strict"), (warn_msg, "non-strict")):
            self.assertIn("DIRTY WORKING TREE", msg, f"{label}: cause not named")
            self.assertIn("stash", msg, f"{label}: fix not named")
            self.assertIn("ticket.py runs an ff-only pull", msg,
                          f"{label}: does not say ticket.py pulled")
            self.assertNotIn("STILL EXIST", msg,
                             f"{label}: must not claim a local commit exists on this path")

    def test_rebase_conflict_message_says_commits_exist_and_forbids_rerun(self) -> None:
        """T-2333 core regression: the false-message defect from the metis
        incident. On the rebase-conflict-on-local-commits shape, the message
        must assert the OPPOSITE of the ff-path hint — a local commit exists —
        and must explicitly forbid re-running (the compounding action that
        allocates a second ticket id for already-committed content)."""
        with self._fail_pull_rebase_conflict():
            with self.assertRaises(ticket.TicketError) as cm:
                ticket._pull_if_needed(False)
            strict_msg = str(cm.exception)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                ticket._pull_if_needed(False, strict=False)
            warn_msg = buf.getvalue()
        for msg, label in ((strict_msg, "strict"), (warn_msg, "non-strict")):
            self.assertIn("STILL EXIST", msg, f"{label}: must assert the commit exists")
            self.assertIn("DO NOT RE-RUN", msg, f"{label}: must forbid re-running")
            self.assertIn("log --oneline origin/main..HEAD", msg,
                          f"{label}: must name how to see the surviving commit(s)")
            self.assertNotIn(
                "nothing has been written yet", msg,
                f"{label}: must NOT assert the false ff-path invariant",
            )
            self.assertNotIn("DIRTY WORKING TREE", msg,
                             f"{label}: must NOT suggest the wrong cause (tree was clean)")

    def test_unknown_failure_message_asserts_neither_invariant(self) -> None:
        """A failure this hint cannot classify must claim NEITHER 'nothing
        written' NOR 'commit exists' — inheriting either would be a guess."""
        with self._fail_pull_unknown():
            with self.assertRaises(ticket.TicketError) as cm:
                ticket._pull_if_needed(False)
            msg = str(cm.exception)
        self.assertNotIn("means nothing has been written yet", msg)
        self.assertNotIn("STILL EXIST", msg)

    def _fail_pull_fetch(self, detail: str = "could not resolve host github.com"):
        """T-2339: the THIRD return point — fetch failed before any merge."""
        return unittest.mock.patch.object(
            ticket, "pull_or_rebase_strict",
            return_value=(False, f"{ticket.PULL_FETCH_FAILED_PREFIX}{detail}"),
        )

    def test_fetch_failure_says_network_and_that_rerunning_is_safe(self) -> None:
        """T-2339: the fetch arm must say what is TRUE for it specifically.

        The two things that distinguish it from every other arm: nothing was
        written (shared with the ff arm, different cause), and re-running is
        SAFE — the direct opposite of the rebase-conflict arm's advice. If it
        silently fell back to UNKNOWN this test would fail on both counts.
        """
        with self._fail_pull_fetch():
            with self.assertRaises(ticket.TicketError) as cm:
                ticket._pull_if_needed(False)
            msg = str(cm.exception)
        self.assertIn("NOTHING HAS BEEN WRITTEN", msg)
        self.assertIn("RE-RUNNING IS SAFE", msg)
        self.assertIn("could not resolve host github.com", msg,
                      "the underlying git error must survive")
        # must NOT inherit the two hints whose advice is wrong here
        self.assertNotIn("DIRTY WORKING TREE", msg)
        self.assertNotIn("DO NOT RE-RUN", msg)
        self.assertNotIn("cannot classify", msg)

    def test_hint_selector_matches_prefix_directly(self) -> None:
        """Unit-level pin on the selector itself, independent of the
        subprocess-mocking fixtures above."""
        self.assertIs(
            ticket._pull_failure_hint(f"{ticket.PULL_FETCH_FAILED_PREFIX}x"),
            ticket._PULL_FAILURE_HINT_FETCH,
        )
        self.assertIs(
            ticket._pull_failure_hint(f"{ticket.PULL_NOT_AHEAD_PREFIX}x"),
            ticket._PULL_FAILURE_HINT_FF,
        )
        self.assertIs(
            ticket._pull_failure_hint(f"{ticket.PULL_REBASE_CONFLICT_PREFIX}x"),
            ticket._PULL_FAILURE_HINT_REBASE_CONFLICT,
        )
        self.assertIs(
            ticket._pull_failure_hint("some other unrecognised failure text"),
            ticket._PULL_FAILURE_HINT_UNKNOWN,
        )

    def test_no_push_still_short_circuits_before_pulling(self) -> None:
        with unittest.mock.patch.object(ticket, "pull_or_rebase_strict") as p:
            ticket._pull_if_needed(True, strict=False)
            ticket._pull_if_needed(True, strict=True)
            p.assert_not_called()


class TestNoPushFlag(unittest.TestCase):
    """Assert that --no-push skips `git push` but still commits locally.

    setUp mirrors TestFullLifecycleGitCommands: same tmpdir structure,
    same TICKETS_ROOT / REPO_ROOT / _git_utils.REPO_ROOT patches, same
    fake_run side-effect that emulates git mv + git add path-existence
    checking + git diff staged-set tracking. No new mocking infrastructure
    per §Locked-4.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._tickets_patch = patch.object(
            ticket, "TICKETS_ROOT", self.root / "tickets"
        )
        self._tickets_patch.start()
        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True, exist_ok=True)

        readme = self.root / "tickets" / "README.md"
        readme.write_text(
            "# Tickets\n\n"
            "## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n"
            "\n"
            "## Related\n\n"
            "- Link\n",
            encoding="utf-8",
        )

        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()

        def fake_run(argv, **kwargs):
            if argv[:2] == ["git", "mv"] and len(argv) >= 4:
                src = self.root / argv[2]
                dst = self.root / argv[3]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "add"] and "--" in argv:
                idx = argv.index("--")
                paths = argv[idx + 1:]
                missing = [p for p in paths if not (self.root / p).exists()]
                if missing:
                    return MagicMock(
                        returncode=128,
                        stdout="",
                        stderr=(
                            "fatal: pathspec '"
                            + missing[0]
                            + "' did not match any files\n"
                        ),
                    )
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "diff"]:
                self.assertIn("--no-renames", argv)
                staged_set: set[str] = set()
                for prior in self.mock_run.call_args_list:
                    pa = prior.args[0]
                    if pa[:2] == ["git", "mv"] and len(pa) >= 4:
                        staged_set.add(pa[2])
                        staged_set.add(pa[3])
                    elif pa[:2] == ["git", "add"] and "--" in pa:
                        i = pa.index("--")
                        for p in pa[i + 1:]:
                            if (self.root / p).exists():
                                staged_set.add(p)
                    elif pa[:2] == ["git", "commit"]:
                        staged_set.clear()
                staged = sorted(staged_set)
                return MagicMock(
                    returncode=0,
                    stdout="\n".join(staged) + ("\n" if staged else ""),
                    stderr="",
                )
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_run
        os.environ["PANTHEON_ACTOR"] = "orchestrator"

    def tearDown(self) -> None:
        del os.environ["PANTHEON_ACTOR"]
        self._sub_patch.stop()
        self._tickets_patch.stop()
        self._gu_repo_patch.stop()
        self._repo_patch.stop()
        self._tmp.cleanup()

    def _git_kinds_after(self, n_before: int) -> list[str]:
        argvs = [c.args[0] for c in self.mock_run.call_args_list[n_before:]]
        return [a[1] for a in argvs if a and a[0] == "git"]

    def _seed(self, tid: str, state: str) -> None:
        _write_ticket(
            self.root / "tickets" / state / f"{tid}.md", tid=tid, state=state
        )

    def test_ticket_claim_no_push_skips_git_push(self) -> None:
        self._seed("T-0001", "inbox")
        mark = len(self.mock_run.call_args_list)
        args = MagicMock()
        args.ticket = "T-0001"
        args.no_push = True
        ticket.cmd_claim(args)
        kinds = self._git_kinds_after(mark)
        self.assertNotIn("push", kinds)
        self.assertIn("commit", kinds)
        self.assertTrue(
            (self.root / "tickets" / "active" / "T-0001.md").exists()
        )

    def test_ticket_claim_no_push_also_skips_git_pull(self) -> None:
        """T-0243: --no-push must skip git pull too so hermetic/offline use is possible."""
        self._seed("T-0001", "inbox")
        mark = len(self.mock_run.call_args_list)
        args = MagicMock()
        args.ticket = "T-0001"
        args.no_push = True
        ticket.cmd_claim(args)
        kinds = self._git_kinds_after(mark)
        self.assertNotIn("pull", kinds)
        self.assertNotIn("push", kinds)
        self.assertIn("commit", kinds)

    def test_ticket_claim_default_does_call_git_pull(self) -> None:
        """T-0243: --no-push=False (default) preserves the pre-commit ff-only pull.
        T-0442: the "pull" is now pull_repo_strict's fetch+ff-merge two-step
        rather than the legacy `git pull --ff-only origin main`."""
        self._seed("T-0001", "inbox")
        mark = len(self.mock_run.call_args_list)
        args = MagicMock()
        args.ticket = "T-0001"
        args.no_push = False
        ticket.cmd_claim(args)
        kinds = self._git_kinds_after(mark)
        self.assertIn("fetch", kinds)
        self.assertIn("merge", kinds)
        self.assertIn("commit", kinds)

    def test_ticket_claim_pull_failure_leaves_filesystem_untouched(self) -> None:
        """T-0244: pull-fail BEFORE git mv → no disk mutation, file stays at original lane."""
        self._seed("T-0001", "inbox")
        original_inbox = self.root / "tickets" / "inbox" / "T-0001.md"
        self.assertTrue(original_inbox.exists())

        # Override mock to fail on the new fetch step of pull_repo_strict
        # (T-0442: the legacy `git pull --ff-only` was replaced by the
        # resilient fetch+ff-merge two-step; the failure point is now `fetch`).
        original_side_effect = self.mock_run.side_effect

        def fail_on_pull(argv, *a, **kw):
            if argv[:3] == ["git", "fetch", "origin"]:
                return MagicMock(returncode=1, stdout="", stderr="fatal: no remote")
            if callable(original_side_effect):
                return original_side_effect(argv, *a, **kw)
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fail_on_pull

        args = MagicMock()
        args.ticket = "T-0001"
        args.no_push = False
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_claim(args)

        # T-0244 acceptance: filesystem is UNTOUCHED post pull-fail.
        self.assertTrue(original_inbox.exists(),
                        "T-0001 must remain in inbox/ after pull-fail (T-0244 fix)")
        self.assertFalse((self.root / "tickets" / "active" / "T-0001.md").exists(),
                         "T-0001 must NOT be in active/ if pull failed (T-0244 fix)")
        # And `git mv` must never have been called (proves pull-first ordering).
        for call in self.mock_run.call_args_list:
            self.assertNotEqual(call.args[0][:2], ["git", "mv"],
                                "git mv called despite pull-fail — T-0244 regression")

    def test_ticket_resolve_no_push_skips_git_push(self) -> None:
        self._seed("T-0001", "active")
        mark = len(self.mock_run.call_args_list)
        args = MagicMock()
        args.ticket = "T-0001"
        args.no_push = True
        args.with_summary = None
        ticket.cmd_resolve(args)
        kinds = self._git_kinds_after(mark)
        self.assertNotIn("push", kinds)
        self.assertIn("commit", kinds)
        self.assertTrue(
            (self.root / "tickets" / "done" / "T-0001.md").exists()
        )

    def test_ticket_block_no_push_skips_git_push(self) -> None:
        # ORG-019: block is now a frontmatter-edit (no state change / no dir move).
        self._seed("T-0001", "active")
        mark = len(self.mock_run.call_args_list)
        # T-1979: argparse.Namespace, NOT MagicMock. cmd_block reads every flag via
        # getattr(args, <flag>, <default>), so a Namespace returns the default for any
        # attr it doesn't carry — which is why only the flags THIS scenario needs are
        # listed (a not_before gate under --no-push), and every other flag, current
        # (clear_dep/clear_deps) or future, falls through to its getattr default with
        # no fixture edit. A MagicMock instead returned a truthy child mock for any
        # unset attr, so every new block flag (T-0663 clear_not_before, T-1975
        # clear_dep/clear_deps) silently broke this test until someone patched the mock.
        args = argparse.Namespace(
            ticket="T-0001",
            no_push=True,
            not_before="2026-06-01",
            not_before_anchor="operator:tg:5644",  # T-2833: date gates need an anchor
            depends_on=None,
            clear_not_before=False,
        )
        ticket.cmd_block(args)
        kinds = self._git_kinds_after(mark)
        self.assertNotIn("push", kinds)
        self.assertIn("commit", kinds)
        # File stays in active/ (no dir move)
        self.assertTrue(
            (self.root / "tickets" / "active" / "T-0001.md").exists()
        )

    def test_ticket_file_no_push_skips_git_push(self) -> None:
        mark = len(self.mock_run.call_args_list)
        args = MagicMock()
        args.requester = "orchestrator"
        args.target = "recovery"
        args.title = "Test no-push filing"
        args.labels = None
        args.depends_on = None
        args.stale_after = None
        args.not_before = None
        args.ratifiable_file = None
        args.ask = ""
        args.context = ""
        args.no_push = True
        args.parent_ticket = None
        args.spawn_impl = False
        args.terminal = False
        args.impl_title = None
        args.impl_ask = None
        args.appetite = None  # T-3440/ORG-089: getattr-read default
        args.bet_ref = None
        args.impl_target = None
        # T-3328: this test is about --no-push behavior, not DAG connectivity.
        args.orphan = "test fixture: --no-push local-commit-skip-push behavior"
        ticket.cmd_file(args)
        kinds = self._git_kinds_after(mark)
        self.assertNotIn("push", kinds)
        self.assertIn("commit", kinds)
        inbox_files = list((self.root / "tickets" / "inbox").iterdir())
        self.assertEqual(len(inbox_files), 1)

    def test_ticket_no_push_local_commit_succeeds(self) -> None:
        self._seed("T-0002", "inbox")
        mark = len(self.mock_run.call_args_list)
        args = MagicMock()
        args.ticket = "T-0002"
        args.no_push = True
        ticket.cmd_claim(args)
        kinds = self._git_kinds_after(mark)
        commit_calls = [k for k in kinds if k == "commit"]
        self.assertEqual(len(commit_calls), 1, f"expected exactly one commit, got {kinds!r}")
        self.assertNotIn("push", kinds)

    def test_ticket_default_still_pushes(self) -> None:
        self._seed("T-0003", "inbox")
        mark = len(self.mock_run.call_args_list)
        args = MagicMock()
        args.ticket = "T-0003"
        args.no_push = False
        ticket.cmd_claim(args)
        kinds = self._git_kinds_after(mark)
        self.assertIn("push", kinds)
        self.assertEqual(kinds[-1], "push", f"push must be last git op; got {kinds!r}")


class TestCascadeUnblockOnResolve(unittest.TestCase):
    """ORG-013 (amended by ORG-019): resolving a parent ticket removes it from
    depends_on on inbox/+active/ tickets whose other deps are also done. No file
    moves — frontmatter edit only. (blocked/ directory retired per ORG-019.)
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()

        def fake_run(argv, **kwargs):
            if argv[:2] == ["git", "mv"] and len(argv) >= 4:
                src = self.root / argv[2]
                dst = self.root / argv[3]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_run
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()
        self._pull_patch = patch.object(ticket, "_pull_if_needed")
        self.mock_pull = self._pull_patch.start()

    def tearDown(self) -> None:
        self._pull_patch.stop()
        self._commit_patch.stop()
        self._sub_patch.stop()
        self._repo_patch.stop()
        self._gu_repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _seed(self, tid: str, state: str, depends_on=None, log_entries=None,
              target: str = "recovery") -> Path:
        path = self.root / state / f"{tid}.md"
        _write_ticket(path, tid=tid, state=state, target=target,
                      depends_on=depends_on, log_entries=log_entries)
        return path

    def _resolve_args(self, tid: str) -> argparse.Namespace:
        ns = argparse.Namespace()
        ns.ticket = tid
        ns.with_summary = "ok"
        ns.no_push = False
        return ns

    def test_inbox_dep_cleared_when_parent_resolves(self) -> None:
        # Post-ORG-019: dependant in inbox/ with only this dep → dep cleared from depends_on.
        self._seed("T-0001", "active")  # parent
        dep_path = self._seed("T-0002", "inbox", depends_on=["T-0001"])
        ticket.cmd_resolve(self._resolve_args("T-0001"))
        # T-0001 → done; T-0002 stays in inbox/ (file not moved)
        self.assertTrue((self.root / "done" / "T-0001.md").exists())
        self.assertTrue(dep_path.exists())  # still in inbox/; no dir move
        # depends_on should now be empty
        body = dep_path.read_text(encoding="utf-8")
        fields, _ = ticket.parse_front_matter(body)
        self.assertEqual(fields.get("depends_on"), [])
        # Transition log contains the auto-unblock entry
        self.assertIn("dep T-0001 resolved; removed from depends_on", body)

    def test_ratify_spawn_then_resolve_unblocks_impl_end_to_end(self) -> None:
        """T-0852 full loop: file a ratify with --spawn-impl (impl child gated on
        the ratify), then resolve the ratify → the impl child's depends_on clears,
        so it becomes deliverable. This is the 'ratify triggers impl-unblock' path
        the operator asked for end-to-end.
        """
        args = MagicMock()
        args.requester = "specialist"
        args.target = "operator"
        args.title = "ratify the SOP"
        args.labels = None
        args.depends_on = None
        args.stale_after = None
        args.not_before = None
        args.parent_ticket = None
        args.ratifiable_file = ["pantheon-config:sop/standard/foo.md"]
        args.intent = None  # auto-ratify
        args.ask = ""
        args.context = ""
        args.no_push = False
        args.spawn_impl = True
        args.terminal = False
        args.impl_title = "Build the SOP follow-up"
        args.impl_ask = None
        args.appetite = None  # T-3440/ORG-089: getattr-read default
        args.bet_ref = None
        args.impl_target = "recovery"
        # T-3328: the ratify PARENT itself carries no lineage edge in this
        # fixture (the CHILD gets depends_on/parent_ticket automatically via
        # _spawn_child_ticket, which is what's under test) — orphan escape
        # hatch for the parent.
        args.orphan = "test fixture: T-0852 ratify-spawns-impl end-to-end cascade"
        ticket.cmd_file(args)
        ratify = self.root / "inbox" / "T-0001.md"
        impl = self.root / "inbox" / "T-0002.md"
        self.assertTrue(ratify.exists() and impl.exists())
        impl_fields, _ = ticket.parse_front_matter(impl.read_text())
        self.assertEqual(impl_fields.get("depends_on"), ["T-0001"])
        # Ratify gets picked up to active, then the operator resolves (ratifies) it.
        shutil.move(str(ratify), str(self.root / "active" / "T-0001.md"))
        ticket.cmd_resolve(self._resolve_args("T-0001"))
        # Cascade cleared the impl's gate → depends_on now empty → deliverable.
        impl_fields2, body2 = ticket.parse_front_matter(impl.read_text())
        self.assertEqual(impl_fields2.get("depends_on"), [])
        self.assertIn("dep T-0001 resolved", body2)

    def test_active_dep_cleared_when_parent_resolves(self) -> None:
        # Dependant in active/ also gets depends_on cleared (stays in active/).
        self._seed("T-0001", "active")
        dep_path = self._seed("T-0002", "active", depends_on=["T-0001"])
        ticket.cmd_resolve(self._resolve_args("T-0001"))
        self.assertTrue(dep_path.exists())  # still in active/
        body = dep_path.read_text(encoding="utf-8")
        fields, _ = ticket.parse_front_matter(body)
        self.assertEqual(fields.get("depends_on"), [])
        self.assertIn("dep T-0001 resolved", body)

    def test_multi_dep_partial_resolve_does_not_clear(self) -> None:
        # T-0003 depends on [T-0001, T-0002]. Resolving T-0001 alone should not fire cascade.
        self._seed("T-0001", "active")
        self._seed("T-0002", "active")  # not yet done
        dep_path = self._seed("T-0003", "inbox", depends_on=["T-0001", "T-0002"])
        ticket.cmd_resolve(self._resolve_args("T-0001"))
        # T-0003 depends_on unchanged (T-0002 still not done)
        body = dep_path.read_text(encoding="utf-8")
        fields, _ = ticket.parse_front_matter(body)
        self.assertEqual(sorted(fields.get("depends_on") or []), ["T-0001", "T-0002"])
        self.assertNotIn("auto-unblock", body)

    def test_multi_dep_all_done_clears(self) -> None:
        # Parent resolves; all OTHER deps already in done/ → depends_on is fully pruned.
        #
        # T-2177: this test previously ASSERTED THE DEFECT — it required T-0002 to
        # SURVIVE in depends_on, justified by "a future Phase-3 sweep cleans it".
        # No such sweep exists; the only mention of it in the repo was that comment.
        # Meanwhile ticket_lint flags exactly this state as "Stale done-deps —
        # resolved parent not stripped (cascade-unblock should have)". Three
        # components, three different beliefs about whose job it was, and the test
        # locked in the one that produced hand-work.
        self._seed("T-0001", "active")
        self._seed("T-0002", "done")  # already done
        dep_path = self._seed("T-0003", "inbox", depends_on=["T-0001", "T-0002"])
        ticket.cmd_resolve(self._resolve_args("T-0001"))
        body = dep_path.read_text(encoding="utf-8")
        fields, _ = ticket.parse_front_matter(body)
        remaining = fields.get("depends_on") or []
        self.assertNotIn("T-0001", remaining)
        self.assertNotIn("T-0002", remaining, "already-done dep must be pruned too (T-2177)")
        self.assertEqual(remaining, [])
        self.assertIn("dep T-0001 resolved", body)

    def test_sequential_two_dep_resolve_leaves_no_stranded_dep(self) -> None:
        """T-2177's own repro: resolve dep A, then dep B, in that order.

        The defect made the SECOND resolve leave depends_on == ["T-0001"] — a done
        ticket listed as a live gate, with no remaining cascade opportunity to clear
        it, so the dependant looked dep-blocked permanently.
        """
        self._seed("T-0001", "active")
        self._seed("T-0002", "active")
        dep_path = self._seed("T-0003", "inbox", depends_on=["T-0001", "T-0002"])

        # A resolves first: cascade must SKIP (B not done), depends_on untouched.
        ticket.cmd_resolve(self._resolve_args("T-0001"))
        fields, _ = ticket.parse_front_matter(dep_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(fields.get("depends_on") or []), ["T-0001", "T-0002"])

        # B resolves second: cascade fires and must clear BOTH, not just B.
        ticket.cmd_resolve(self._resolve_args("T-0002"))
        fields, _ = ticket.parse_front_matter(dep_path.read_text(encoding="utf-8"))
        self.assertEqual(fields.get("depends_on") or [], [],
                         "stranded already-done dep left behind (T-2177)")

    def test_malformed_dep_survives_the_prune(self) -> None:
        """Fail-closed on malformed dep ids must survive T-2177's widened prune.

        A dep id that is not T-NNNN cannot be checked against done/, so the guard
        refuses to fire at all — and if it ever did, the prune must not silently
        discard the unparseable entry.
        """
        self._seed("T-0001", "active")
        dep_path = self._seed("T-0003", "inbox", depends_on=["T-0001", "not-a-tid"])
        ticket.cmd_resolve(self._resolve_args("T-0001"))
        fields, _ = ticket.parse_front_matter(dep_path.read_text(encoding="utf-8"))
        remaining = fields.get("depends_on") or []
        self.assertIn("not-a-tid", remaining, "malformed dep must never be dropped")
        self.assertIn("T-0001", remaining, "cascade must not fire when a dep is unparseable")

    def test_no_dependants_normal_resolve(self) -> None:
        # No dependants; resolve proceeds normally.
        self._seed("T-0001", "active")
        ticket.cmd_resolve(self._resolve_args("T-0001"))
        self.assertTrue((self.root / "done" / "T-0001.md").exists())
        self.mock_commit.assert_called_once()
        affected_paths = self.mock_commit.call_args.args[0]
        # +1: T-3451 appetite-outcome shard now folded into the same commit
        # (parent's old + new, plus the resolve's appetite-outcome shard path).
        self.assertEqual(len(affected_paths), 3)

    def test_cascade_paths_threaded_into_commit(self) -> None:
        # When cascade fires, _commit_and_push receives parent paths + dependant paths.
        self._seed("T-0001", "active")
        self._seed("T-0002", "inbox", depends_on=["T-0001"])
        self._seed("T-0003", "inbox", depends_on=["T-0001"])
        ticket.cmd_resolve(self._resolve_args("T-0001"))
        affected = self.mock_commit.call_args.args[0]
        # Parent: 2 paths (old active + new done). Each cascade dep: 1 path each (in-place edit).
        # Total: 2 + 2*1 = 4, +1: T-3451 appetite-outcome shard now folded into
        # the same commit.
        self.assertEqual(len(affected), 5)
        # Commit message should mention the cascade
        commit_msg = self.mock_commit.call_args.args[1]
        self.assertIn("Auto-unblock cascade", commit_msg)
        self.assertIn("2 dependants", commit_msg)

    def test_malformed_dep_id_fails_closed(self) -> None:
        # Dependant has a malformed dep id alongside the parent — fail closed.
        self._seed("T-0001", "active")
        dep_path = self._seed("T-0002", "inbox", depends_on=["T-0001", "garbage"])
        ticket.cmd_resolve(self._resolve_args("T-0001"))
        # T-0002's depends_on unchanged (malformed "garbage" dep treated as "not done")
        body = dep_path.read_text(encoding="utf-8")
        fields, _ = ticket.parse_front_matter(body)
        self.assertEqual(sorted(fields.get("depends_on") or []), ["T-0001", "garbage"])
        self.assertNotIn("auto-unblock", body)

    def test_cascade_unblock_strips_auto_delivered_stamp(self) -> None:
        """T-0971 regression: cascade must strip auto_delivered/auto_delivered_at stamps.

        A dependant that was previously delivered (auto_delivered: true) but
        still dep-gated must not keep its stamp after the dep is removed.
        If the stamp survives, check_dispatch_locked() misidentifies the ticket
        as in-flight (T-0864 arm-B only excluded dep-blocked tickets; this test
        verifies the arm-A fix applied at unblock time).
        """
        import tools.inbox_consumer as ic

        # Seed parent in active.
        self._seed("T-0001", "active")

        # Seed dependant in inbox with depends_on=["T-0001"] AND auto_delivered stamps.
        dep_path = self.root / "inbox" / "T-0002.md"
        dep_path.parent.mkdir(parents=True, exist_ok=True)
        dep_content = (
            "---\n"
            "id: T-0002\n"
            "created: 2026-06-06T10:00:00Z\n"
            "requester: orchestrator\n"
            "target: recovery\n"
            "state: inbox\n"
            "parent_ticket: null\n"
            "depends_on: [T-0001]\n"
            "auto_delivered: true\n"
            "auto_delivered_at: 2026-06-06T09:00:00Z\n"
            "---\n"
            "\n"
            "# Dependant ticket\n"
            "\n"
            "## Ask\n\nDo the thing.\n\n"
            "## Context\n\n\n"
            "## Transition log\n\n"
            "- 2026-06-06T09:00:00Z [orchestrator] filed in inbox\n"
        )
        dep_path.write_text(dep_content, encoding="utf-8")

        # Resolve parent — triggers cascade.
        ticket.cmd_resolve(self._resolve_args("T-0001"))

        # (a) depends_on cleared.
        body = dep_path.read_text(encoding="utf-8")
        fields, _ = ticket.parse_front_matter(body)
        self.assertEqual(fields.get("depends_on"), [],
                         "depends_on must be empty after cascade unblock")

        # (b) auto_delivered and auto_delivered_at must be GONE.
        self.assertNotIn("auto_delivered", fields,
                         "auto_delivered stamp must be stripped on cascade unblock")
        self.assertNotIn("auto_delivered_at", fields,
                         "auto_delivered_at stamp must be stripped on cascade unblock")

        # (c) transition log notes the stamp clear.
        self.assertIn("auto_delivered stamp cleared", body,
                      "transition log must note that auto_delivered stamp was cleared")

        # (c2) check_dispatch_locked must NOT raise SkipCycle for the agent.
        # After cascade, the ticket has no stamp → no in-flight mutex held.
        with patch("tools.inbox_consumer.TICKETS_ROOT", self.root):
            # Telegram alert path must not fire; mock it out.
            with patch("tools.inbox_consumer._send_telegram_alert"):
                # No exception should be raised.
                ic.check_dispatch_locked("recovery")


# ---------------------------------------------------------------------------
# T-0653: noodle/action close lifecycle (target=operator queue)
# ---------------------------------------------------------------------------


def _write_operator_ticket(
    path: Path,
    *,
    tid: str,
    state: str,
    intent: str,
    requester: str = "orchestrator",
    title: str = "Test operator-target ticket",
    ratifiable_files: list[str] | None = None,
) -> None:
    """Write a target=operator ticket with the given intent. Hand-rolled
    because _write_ticket doesn't accept intent (target=operator-specific)."""
    fm_lines = [
        "---",
        f"id: {tid}",
        "created: 2026-05-28T00:00:00Z",
        f"requester: {requester}",
        "target: operator",
        f"state: {state}",
        "parent_ticket: null",
        "depends_on: []",
        f"intent: {intent}",
    ]
    if ratifiable_files:
        fm_lines.append(f"ratifiable_files: [{', '.join(ratifiable_files)}]")
    fm_lines.append("---")
    body = (
        f"\n# {title}\n\n## Ask\n\nWhatever.\n\n## Context\n\nReasons.\n\n"
        "## Transition log\n\n"
        f"- 2026-05-28T00:00:00Z [{requester}] filed in inbox\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm_lines) + body, encoding="utf-8")


class TestCloseNoodleAndAction(unittest.TestCase):
    """T-0653: close-noodle + close-action verbs for target=operator queue.

    - close-noodle: moves to done; --spawn-to creates a child action ticket
      atomically in the same commit.
    - close-action: moves to done AND auto-spawns a review ticket back to
      the original requester ("did this unblock the downstream work?").
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()

        def fake_run(argv, **kwargs):
            if argv[:2] == ["git", "mv"] and len(argv) >= 4:
                src = self.root / argv[2]
                dst = self.root / argv[3]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_run
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()
        self._pull_patch = patch.object(ticket, "_pull_if_needed")
        self.mock_pull = self._pull_patch.start()
        # Pin the next-id allocator so spawned children are deterministic.
        self._next_id_patch = patch.object(ticket, "_next_ticket_id", return_value="T-9999")
        self.mock_next_id = self._next_id_patch.start()

    def tearDown(self) -> None:
        self._next_id_patch.stop()
        self._pull_patch.stop()
        self._commit_patch.stop()
        self._sub_patch.stop()
        self._repo_patch.stop()
        self._gu_repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _args(self, ticket_id: str, *, with_summary="ok", spawn_to=None, as_action=False):
        ns = argparse.Namespace()
        ns.ticket = ticket_id
        ns.with_summary = with_summary
        ns.spawn_to = spawn_to
        ns.as_action = as_action
        ns.no_push = False
        return ns

    # --- close-noodle ----------------------------------------------------

    def test_close_noodle_direct_moves_to_done(self) -> None:
        p = self.root / "inbox" / "T-0001.md"
        _write_operator_ticket(p, tid="T-0001", state="inbox", intent="noodle")
        ticket.cmd_close_noodle(self._args("T-0001"))
        self.assertTrue((self.root / "done" / "T-0001.md").exists())
        self.assertFalse(p.exists())
        # Transition log carries the noodle-closed verb.
        body = (self.root / "done" / "T-0001.md").read_text(encoding="utf-8")
        self.assertIn("noodle-closed", body)

    def test_close_noodle_spawn_creates_child_action(self) -> None:
        p = self.root / "inbox" / "T-0010.md"
        _write_operator_ticket(p, tid="T-0010", state="inbox", intent="noodle",
                              title="Decide OPTION A vs OPTION B")
        ticket.cmd_close_noodle(
            self._args("T-0010", spawn_to="recovery", as_action=True,
                       with_summary="going with A; recovery execute")
        )
        # Parent moved.
        self.assertTrue((self.root / "done" / "T-0010.md").exists())
        # Child filed at the pinned next-id.
        child = self.root / "inbox" / "T-9999.md"
        self.assertTrue(child.exists())
        cbody = child.read_text(encoding="utf-8")
        fields, _ = ticket.parse_front_matter(cbody)
        self.assertEqual(fields.get("target"), "recovery")
        self.assertEqual(fields.get("parent_ticket"), "T-0010")
        # Child references the parent in its body for lineage-readability.
        self.assertIn("T-0010", cbody)
        self.assertIn("spawned-from-noodle", fields.get("labels") or [])

    def test_close_noodle_spawn_requires_as_action(self) -> None:
        p = self.root / "inbox" / "T-0011.md"
        _write_operator_ticket(p, tid="T-0011", state="inbox", intent="noodle")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_close_noodle(self._args("T-0011", spawn_to="recovery", as_action=False))

    def test_close_noodle_refuses_non_operator_target(self) -> None:
        # Recovery-target ticket — close-noodle must refuse.
        p = self.root / "inbox" / "T-0020.md"
        _write_ticket(p, tid="T-0020", state="inbox", target="recovery")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_close_noodle(self._args("T-0020"))

    def test_close_noodle_refuses_wrong_intent(self) -> None:
        # target=operator but intent=action — must refuse.
        p = self.root / "inbox" / "T-0021.md"
        _write_operator_ticket(p, tid="T-0021", state="inbox", intent="action")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_close_noodle(self._args("T-0021"))

    # --- close-action ----------------------------------------------------

    def test_close_action_moves_to_done_and_spawns_review(self) -> None:
        p = self.root / "inbox" / "T-0030.md"
        _write_operator_ticket(
            p, tid="T-0030", state="inbox", intent="action",
            requester="orchestrator", title="Run the calibration"
        )
        ticket.cmd_close_action(self._args("T-0030", with_summary="ran it 2026-05-28"))
        # Parent in done/.
        self.assertTrue((self.root / "done" / "T-0030.md").exists())
        # Review child auto-spawned at the pinned next-id; target=requester.
        child = self.root / "inbox" / "T-9999.md"
        self.assertTrue(child.exists())
        cbody = child.read_text(encoding="utf-8")
        fields, _ = ticket.parse_front_matter(cbody)
        self.assertEqual(fields.get("target"), "orchestrator")
        self.assertEqual(fields.get("parent_ticket"), "T-0030")
        labels = fields.get("labels") or []
        self.assertIn("review", labels)
        self.assertIn("auto-spawned-from-action-close", labels)
        # Body references the parent + carries the close comment so the
        # reviewer can see what was claimed done.
        self.assertIn("T-0030", cbody)
        self.assertIn("ran it 2026-05-28", cbody)

    def test_close_action_refuses_non_operator(self) -> None:
        p = self.root / "inbox" / "T-0040.md"
        _write_ticket(p, tid="T-0040", state="inbox", target="recovery")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_close_action(self._args("T-0040"))

    def test_close_action_refuses_wrong_intent(self) -> None:
        p = self.root / "inbox" / "T-0041.md"
        _write_operator_ticket(p, tid="T-0041", state="inbox", intent="noodle")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_close_action(self._args("T-0041"))

    def test_close_action_works_from_active_state(self) -> None:
        # Tickets in inbox/ OR active/ should be closable.
        p = self.root / "active" / "T-0050.md"
        _write_operator_ticket(p, tid="T-0050", state="active", intent="action")
        ticket.cmd_close_action(self._args("T-0050"))
        self.assertTrue((self.root / "done" / "T-0050.md").exists())


class TestCmdClaimConcurrencyGuard(unittest.TestCase):
    """T-0463: cmd_claim refuses when origin/main shows the ticket already past inbox.

    Tests the guard logic in isolation by mocking _do_transition — we only care
    here whether _do_transition is reached (guard let it through) vs not reached
    (guard raised). The full claim mechanics are exercised by TestNoPushFlag.
    """

    def setUp(self) -> None:
        # This class tests the T-0463 origin-state guard in isolation and does
        # NOT patch TICKETS_ROOT, so the orthogonal T-0877 one-active WIP cap
        # would read the real tickets dir. Disable the cap via its documented
        # env override for the duration of these tests.
        self._prev_cap = os.environ.get("PANTHEON_MAX_ACTIVE_TICKETS")
        os.environ["PANTHEON_MAX_ACTIVE_TICKETS"] = "100000"

    def tearDown(self) -> None:
        if self._prev_cap is None:
            os.environ.pop("PANTHEON_MAX_ACTIVE_TICKETS", None)
        else:
            os.environ["PANTHEON_MAX_ACTIVE_TICKETS"] = self._prev_cap

    def _make_args(self, tid: str, no_push: bool = True):
        args = argparse.Namespace()
        args.ticket = tid
        args.no_push = no_push
        return args

    def _git_factory(self, *, fetch_ok: bool, found_in: str | None):
        """Build a fake _git callable that simulates origin/main state.

        fetch_ok: whether `git fetch origin main` returns 0.
        found_in: which state dir at origin/main:tickets/<state>/T-NNNN.md returns 0
                  (None = not found in any state, all show calls return 128).
        """
        def fake_git(*argv):
            if argv[:2] == ("fetch", "origin"):
                return MagicMock(returncode=0 if fetch_ok else 128, stdout="", stderr="")
            if argv[:1] == ("show",) and len(argv) >= 2:
                spec = argv[1]
                if found_in is not None and spec.startswith(f"origin/main:tickets/{found_in}/"):
                    return MagicMock(returncode=0, stdout="ticket content", stderr="")
                return MagicMock(returncode=128, stdout="", stderr="fatal: path not in tree")
            return MagicMock(returncode=0, stdout="", stderr="")
        return fake_git

    def test_claim_proceeds_when_origin_shows_inbox(self) -> None:
        with patch.object(ticket, "_git", side_effect=self._git_factory(fetch_ok=True, found_in="inbox")):
            with patch.object(ticket, "_do_transition", return_value=0) as mock_trans:
                ticket.cmd_claim(self._make_args("T-0001"))
                mock_trans.assert_called_once()

    def test_claim_refused_when_origin_shows_active(self) -> None:
        with patch.object(ticket, "_git", side_effect=self._git_factory(fetch_ok=True, found_in="active")):
            with patch.object(ticket, "_do_transition") as mock_trans:
                with self.assertRaises(ticket.TicketError) as cm:
                    ticket.cmd_claim(self._make_args("T-0001"))
                self.assertIn("concurrent-claim conflict", str(cm.exception))
                self.assertIn("active", str(cm.exception))
                mock_trans.assert_not_called()

    def test_claim_refused_when_origin_shows_done(self) -> None:
        with patch.object(ticket, "_git", side_effect=self._git_factory(fetch_ok=True, found_in="done")):
            with patch.object(ticket, "_do_transition") as mock_trans:
                with self.assertRaises(ticket.TicketError) as cm:
                    ticket.cmd_claim(self._make_args("T-0001"))
                self.assertIn("concurrent-claim conflict", str(cm.exception))
                self.assertIn("done", str(cm.exception))
                mock_trans.assert_not_called()

    def test_claim_proceeds_when_ticket_not_on_origin(self) -> None:
        """Ticket not yet on origin (freshly filed local) → claim proceeds (fail-open)."""
        with patch.object(ticket, "_git", side_effect=self._git_factory(fetch_ok=True, found_in=None)):
            with patch.object(ticket, "_do_transition", return_value=0) as mock_trans:
                ticket.cmd_claim(self._make_args("T-0001"))
                mock_trans.assert_called_once()

    def test_claim_proceeds_when_fetch_fails(self) -> None:
        """Fetch failure (offline) → fall back to local origin/main ref; proceed if it shows inbox."""
        with patch.object(ticket, "_git", side_effect=self._git_factory(fetch_ok=False, found_in="inbox")):
            with patch.object(ticket, "_do_transition", return_value=0) as mock_trans:
                ticket.cmd_claim(self._make_args("T-0001"))
                mock_trans.assert_called_once()


class TestWipCap(unittest.TestCase):
    """T-0877: one-active-ticket WIP cap on manual `claim`.

    cmd_claim refuses if the claiming persona already holds an active ticket
    (target==persona, state==active). operator is exempt; PANTHEON_MAX_ACTIVE_TICKETS
    overrides the cap. The origin-state pre-check and the actual transition are
    mocked — these tests exercise only the cap decision.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True, exist_ok=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._prev_actor = os.environ.get("PANTHEON_ACTOR")
        self._prev_cap = os.environ.get("PANTHEON_MAX_ACTIVE_TICKETS")
        os.environ["PANTHEON_ACTOR"] = "recovery"
        os.environ.pop("PANTHEON_MAX_ACTIVE_TICKETS", None)

    def tearDown(self) -> None:
        self._tickets_patch.stop()
        for key, prev in (
            ("PANTHEON_ACTOR", self._prev_actor),
            ("PANTHEON_MAX_ACTIVE_TICKETS", self._prev_cap),
        ):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        self._tmp.cleanup()

    def _args(self, tid: str):
        a = argparse.Namespace()
        a.ticket = tid
        a.no_push = True
        return a

    def _claim(self, tid: str):
        """Claim with origin-check + transition mocked; return the _do_transition mock."""
        with patch.object(ticket, "_origin_state_of_ticket", return_value=None):
            with patch.object(ticket, "_do_transition", return_value=0) as mt:
                ticket.cmd_claim(self._args(tid))
                return mt

    def _claim_expect_raise(self, tid: str):
        with patch.object(ticket, "_origin_state_of_ticket", return_value=None):
            with patch.object(ticket, "_do_transition", return_value=0) as mt:
                with self.assertRaises(ticket.TicketError) as cm:
                    ticket.cmd_claim(self._args(tid))
                mt.assert_not_called()
                return cm.exception

    def test_refuses_second_active_claim(self) -> None:
        _write_ticket(self.root / "active" / "T-0001.md", tid="T-0001", state="active", target="recovery")
        _write_ticket(self.root / "inbox" / "T-0002.md", tid="T-0002", state="inbox", target="recovery")
        exc = self._claim_expect_raise("T-0002")
        self.assertIn("WIP cap", str(exc))
        self.assertIn("T-0001", str(exc))

    def test_allows_claim_when_no_active(self) -> None:
        _write_ticket(self.root / "inbox" / "T-0002.md", tid="T-0002", state="inbox", target="recovery")
        self._claim("T-0002").assert_called_once()

    def test_other_agents_active_does_not_block(self) -> None:
        # recovery's claim is not blocked by orchestrator holding an active ticket.
        _write_ticket(self.root / "active" / "T-0001.md", tid="T-0001", state="active", target="orchestrator")
        _write_ticket(self.root / "inbox" / "T-0002.md", tid="T-0002", state="inbox", target="recovery")
        self._claim("T-0002").assert_called_once()

    def test_done_active_does_not_count(self) -> None:
        # A resolved (done) ticket does not count toward the cap.
        _write_ticket(self.root / "done" / "T-0001.md", tid="T-0001", state="done", target="recovery")
        _write_ticket(self.root / "inbox" / "T-0002.md", tid="T-0002", state="inbox", target="recovery")
        self._claim("T-0002").assert_called_once()

    def test_operator_is_exempt(self) -> None:
        os.environ["PANTHEON_ACTOR"] = "operator"
        _write_ticket(self.root / "active" / "T-0001.md", tid="T-0001", state="active", target="operator")
        _write_ticket(self.root / "inbox" / "T-0002.md", tid="T-0002", state="inbox", target="operator")
        self._claim("T-0002").assert_called_once()

    def test_env_override_raises_cap(self) -> None:
        os.environ["PANTHEON_MAX_ACTIVE_TICKETS"] = "2"
        _write_ticket(self.root / "active" / "T-0001.md", tid="T-0001", state="active", target="recovery")
        _write_ticket(self.root / "inbox" / "T-0002.md", tid="T-0002", state="inbox", target="recovery")
        self._claim("T-0002").assert_called_once()  # 1 active < cap 2 → allowed

    def test_os_user_resolves_to_persona(self) -> None:
        # PANTHEON_ACTOR unset → OS user claude-fallback resolves to recovery.
        os.environ.pop("PANTHEON_ACTOR", None)
        _write_ticket(self.root / "active" / "T-0001.md", tid="T-0001", state="active", target="recovery")
        _write_ticket(self.root / "inbox" / "T-0002.md", tid="T-0002", state="inbox", target="recovery")
        with patch.object(ticket.getpass, "getuser", return_value="claude-fallback"):
            exc = self._claim_expect_raise("T-0002")
        self.assertIn("recovery", str(exc))


class TestCmdBlock(unittest.TestCase):
    """ORG-019: block sets not_before / depends_on frontmatter in place; no dir move."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()
        self._pull_patch = patch.object(ticket, "_pull_if_needed")
        self.mock_pull = self._pull_patch.start()

    def tearDown(self) -> None:
        self._pull_patch.stop()
        self._commit_patch.stop()
        self._repo_patch.stop()
        self._gu_repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _seed(self, tid: str, state: str = "active") -> Path:
        path = self.root / state / f"{tid}.md"
        _write_ticket(path, tid=tid, state=state)
        return path

    def _block_args(self, tid: str, not_before=None, depends_on=None,
                    not_before_anchor=None, no_push=True):
        ns = argparse.Namespace()
        ns.ticket = tid
        ns.not_before = not_before
        # T-2833: a not_before gate needs an anchor; default a valid tg-form one so
        # existing not_before tests don't each have to spell it out. Tests that
        # exercise the anchor rule pass an explicit value (or None to omit).
        ns.not_before_anchor = (
            not_before_anchor if not_before_anchor is not None
            else ("operator:tg:5644" if not_before else None)
        )
        ns.depends_on = depends_on
        ns.no_push = no_push
        return ns

    def test_block_sets_not_before_on_active_ticket(self) -> None:
        path = self._seed("T-0001", "active")
        ticket.cmd_block(self._block_args("T-0001", not_before="2026-06-01"))
        # File stays in active/
        self.assertTrue(path.exists())
        fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["not_before"], "2026-06-01T00:00:00Z")
        self.assertEqual(fields["not_before_anchor"], "operator:tg:5644")  # T-2833
        self.assertIn("not_before:", body)

    # --- T-2833: not_before date gates require an operator:<ref> anchor. ---
    def _seed_bare_block_args(self, tid, **kw):
        """Namespace with not_before_anchor NOT auto-filled (pass explicitly)."""
        ns = argparse.Namespace(ticket=tid, not_before=None, not_before_anchor=None,
                                depends_on=None, no_push=True)
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def test_block_not_before_requires_anchor(self) -> None:
        self._seed("T-0001", "active")
        with self.assertRaises(ticket.TicketError) as cm:
            ticket.cmd_block(self._seed_bare_block_args("T-0001", not_before="2026-09-01"))
        self.assertIn("not-before-anchor", str(cm.exception))

    def test_block_rejects_freetext_event_anchor(self) -> None:
        # The exact hole the operator flagged (TG 5644): event:fresh_session must NOT pass.
        self._seed("T-0001", "active")
        with self.assertRaises(ticket.TicketError) as cm:
            ticket.cmd_block(self._seed_bare_block_args(
                "T-0001", not_before="2026-09-01", not_before_anchor="event:fresh_session"))
        self.assertIn("operator:", str(cm.exception))

    def test_block_rejects_operator_freetext_anchor(self) -> None:
        # Even claiming operator: with free text (operator:fresh_session) is refused.
        self._seed("T-0001", "active")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_block(self._seed_bare_block_args(
                "T-0001", not_before="2026-09-01", not_before_anchor="operator:fresh_session"))

    def test_block_rejects_dangling_ticket_anchor(self) -> None:
        self._seed("T-0001", "active")
        with self.assertRaises(ticket.TicketError) as cm:
            ticket.cmd_block(self._seed_bare_block_args(
                "T-0001", not_before="2026-09-01", not_before_anchor="operator:T-7777"))
        self.assertIn("does not exist", str(cm.exception))

    def test_block_accepts_resolvable_ticket_anchor(self) -> None:
        self._seed("T-0001", "active")
        self._seed("T-0002", "inbox")  # the cited decision ticket exists
        ticket.cmd_block(self._seed_bare_block_args(
            "T-0001", not_before="2026-09-01", not_before_anchor="operator:T-0002"))
        fields, _ = ticket.parse_front_matter(
            (self.root / "active" / "T-0001.md").read_text(encoding="utf-8"))
        self.assertEqual(fields["not_before_anchor"], "operator:T-0002")

    def test_block_accepts_five_digit_ticket_anchor(self) -> None:
        """T-3215: operator:<T-NNNNN> (5+ digits) must not be rejected as
        malformed once the cited ticket exists."""
        five_digit_id = "T-" + "12345"
        self._seed("T-0001", "active")
        self._seed(five_digit_id, "inbox")  # the cited decision ticket exists
        ticket.cmd_block(self._seed_bare_block_args(
            "T-0001", not_before="2026-09-01",
            not_before_anchor=f"operator:{five_digit_id}"))
        fields, _ = ticket.parse_front_matter(
            (self.root / "active" / "T-0001.md").read_text(encoding="utf-8"))
        self.assertEqual(fields["not_before_anchor"], f"operator:{five_digit_id}")

    def test_block_rejects_short_digit_ticket_anchor(self) -> None:
        """T-3215: widening to 4+ digits must not open the door to a
        still-malformed (fewer than 4 digit) id."""
        self._seed("T-0001", "active")
        short_id = "T-" + "123"
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_block(self._seed_bare_block_args(
                "T-0001", not_before="2026-09-01",
                not_before_anchor=f"operator:{short_id}"))

    def test_block_anchor_without_not_before_refused(self) -> None:
        self._seed("T-0001", "active")
        with self.assertRaises(ticket.TicketError) as cm:
            ticket.cmd_block(self._seed_bare_block_args(
                "T-0001", not_before_anchor="operator:tg:5644"))
        self.assertIn("requires --not-before", str(cm.exception))

    def test_block_accepts_event_cert_expiry_anchor(self) -> None:
        # T-2833(b): a real-world-event anchor (closed enum) is valid.
        self._seed("T-0001", "active")
        ticket.cmd_block(self._seed_bare_block_args(
            "T-0001", not_before="2026-09-10", not_before_anchor="event:cert-expiry:snowis.me"))
        fields, _ = ticket.parse_front_matter(
            (self.root / "active" / "T-0001.md").read_text(encoding="utf-8"))
        self.assertEqual(fields["not_before_anchor"], "event:cert-expiry:snowis.me")

    def test_block_accepts_event_cadence_anchor(self) -> None:
        self._seed("T-0001", "active")
        ticket.cmd_block(self._seed_bare_block_args(
            "T-0001", not_before="2026-10-01", not_before_anchor="event:cadence:quarterly-review"))
        fields, _ = ticket.parse_front_matter(
            (self.root / "active" / "T-0001.md").read_text(encoding="utf-8"))
        self.assertEqual(fields["not_before_anchor"], "event:cadence:quarterly-review")

    def test_block_rejects_bad_event_kind(self) -> None:
        # event:<not-a-kind> must fail — the closed enum is what blocks fresh_session.
        self._seed("T-0001", "active")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_block(self._seed_bare_block_args(
                "T-0001", not_before="2026-09-10", not_before_anchor="event:whenever:x"))

    def test_block_rejects_event_kind_without_param(self) -> None:
        self._seed("T-0001", "active")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_block(self._seed_bare_block_args(
                "T-0001", not_before="2026-09-10", not_before_anchor="event:cert-expiry"))

    # T-3103: the event:<kind>:<param> form is regex-valid but the param is still
    # gameable — a pseudo-window that names a HOW ("focused session") not a WHEN
    # laundered arbitrary defers of actionable work 3x (2026-08-11/20/23). The
    # denylist must reject the HOW-word at block/file SET time.
    def test_block_rejects_pseudo_window_focused_session(self) -> None:
        """The exact 3x-recurring bypass: event:window:...-focused-session."""
        self._seed("T-0001", "active")
        with self.assertRaises(ticket.TicketError) as cm:
            ticket.cmd_block(self._seed_bare_block_args(
                "T-0001", not_before="2026-09-10",
                not_before_anchor="event:window:passport-hardening-focused-session"))
        self.assertIn("T-3103", str(cm.exception))
        self.assertIn("HOW", str(cm.exception))

    def test_block_rejects_pseudo_window_how_words(self) -> None:
        """Each HOW-word in the denylist is rejected across event kinds."""
        for anchor in (
            "event:window:dedicated-pass",
            "event:window:robustness-review",
            "event:window:careful-pass",
            "event:window:batch-cleanup",
            "event:cadence:unhurried",          # gaming via a different kind
            "event:window:when-im-focused",
        ):
            with self.subTest(anchor=anchor):
                self._seed("T-0001", "active")
                with self.assertRaises(ticket.TicketError):
                    ticket.cmd_block(self._seed_bare_block_args(
                        "T-0001", not_before="2026-09-10", not_before_anchor=anchor))
                # reset for the next subTest (block writes into active/)
                (self.root / "active" / "T-0001.md").unlink(missing_ok=True)

    def test_block_accepts_genuine_dated_window(self) -> None:
        """A real maintenance window (dated param, no HOW-word) still passes."""
        self._seed("T-0001", "active")
        ticket.cmd_block(self._seed_bare_block_args(
            "T-0001", not_before="2026-09-02",
            not_before_anchor="event:window:2026-09-02-pve2-maintenance"))
        fields, _ = ticket.parse_front_matter(
            (self.root / "active" / "T-0001.md").read_text(encoding="utf-8"))
        self.assertEqual(fields["not_before_anchor"],
                         "event:window:2026-09-02-pve2-maintenance")

    def test_block_denylist_no_false_positive_on_passport(self) -> None:
        """Guard: a genuine param must not trip a substring false-positive.
        'passport-renewal' contains 'pass' but NOT the denied 'careful-pass'."""
        self._seed("T-0001", "active")
        ticket.cmd_block(self._seed_bare_block_args(
            "T-0001", not_before="2026-09-02",
            not_before_anchor="event:window:passport-renewal-2026-09"))
        fields, _ = ticket.parse_front_matter(
            (self.root / "active" / "T-0001.md").read_text(encoding="utf-8"))
        self.assertEqual(fields["not_before_anchor"],
                         "event:window:passport-renewal-2026-09")

    def test_block_sets_depends_on_on_inbox_ticket(self) -> None:
        path = self._seed("T-0001", "inbox")
        ticket.cmd_block(self._block_args("T-0001", depends_on=["T-0002"]))
        self.assertTrue(path.exists())  # stays in inbox/
        fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertIn("T-0002", fields.get("depends_on") or [])
        self.assertIn("blocked", body)  # transition log

    def test_block_no_args_error(self) -> None:
        self._seed("T-0001", "active")
        with self.assertRaises(ticket.TicketError) as cm:
            ticket.cmd_block(self._block_args("T-0001"))
        self.assertIn("must specify at least one", str(cm.exception))

    def test_block_on_done_ticket_error(self) -> None:
        self._seed("T-0001", "done")
        with self.assertRaises(ticket.TicketError) as cm:
            ticket.cmd_block(self._block_args("T-0001", not_before="2026-06-01"))
        self.assertIn("done", str(cm.exception))

    def test_block_merges_depends_on_with_existing(self) -> None:
        # Ticket already has depends_on: [T-0003]; adding T-0004 should union them.
        path = self._seed("T-0001", "active")
        # Manually set existing depends_on
        text = path.read_text(encoding="utf-8")
        fields, body = ticket.parse_front_matter(text)
        fields["depends_on"] = ["T-0003"]
        path.write_text(ticket._build_ticket_text(fields, body), encoding="utf-8")
        # Now block with another dep
        ticket.cmd_block(self._block_args("T-0001", depends_on=["T-0004"]))
        fields_after, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        deps = fields_after.get("depends_on") or []
        self.assertIn("T-0003", deps)
        self.assertIn("T-0004", deps)

    def test_block_not_before_normalized_to_iso(self) -> None:
        path = self._seed("T-0001", "active")
        ticket.cmd_block(self._block_args("T-0001", not_before="2026-07-04"))
        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["not_before"], "2026-07-04T00:00:00Z")

    def test_block_depends_on_forward_reference_allowed(self) -> None:
        # T-9999 doesn't exist yet; forward reference must not fail.
        path = self._seed("T-0001", "active")
        rc = ticket.cmd_block(self._block_args("T-0001", depends_on=["T-9999"]))
        self.assertEqual(rc, 0)
        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertIn("T-9999", fields.get("depends_on") or [])

    # T-3081: block-that-ADDS-a-gate must strip auto_delivered / auto_delivered_at.
    # A ticket that was auto_delivered then PARKED via block keeps the stamp;
    # when the gate later clears + it becomes un-gated + 8h-stale,
    # `check_dispatch_locked`'s backstop force-releases the lock + operator-alerts.
    # Mirrors T-2350 (retarget-clears) + T-0971 arm-A (cascade-unblock).

    def _seed_with_auto_delivered(self, tid: str, state: str) -> Path:
        path = self._seed(tid, state)
        text = path.read_text(encoding="utf-8").replace(
            "target: recovery",
            "target: recovery\nauto_delivered: true\nauto_delivered_at: 2026-08-01T00:00:00Z",
            1,
        )
        path.write_text(text, encoding="utf-8")
        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        assert fields.get("auto_delivered") is not None, "seed did not carry the stamp"
        return path

    def test_block_with_not_before_strips_auto_delivered_stamp(self) -> None:
        path = self._seed_with_auto_delivered("T-0007", "active")
        ticket.cmd_block(self._block_args("T-0007", not_before="2026-09-01"))
        fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["not_before"], "2026-09-01T00:00:00Z")
        self.assertNotIn("auto_delivered", fields)
        self.assertNotIn("auto_delivered_at", fields)
        self.assertIn("auto_delivered stamp cleared", body,
                      "clear must be visible in transition log per T-2350 audit-trail rationale")

    def test_block_with_depends_on_strips_auto_delivered_stamp(self) -> None:
        path = self._seed_with_auto_delivered("T-0008", "active")
        ticket.cmd_block(self._block_args("T-0008", depends_on=["T-9998"]))
        fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertIn("T-9998", fields.get("depends_on") or [])
        self.assertNotIn("auto_delivered", fields)
        self.assertNotIn("auto_delivered_at", fields)
        self.assertIn("auto_delivered stamp cleared", body)

    def test_block_without_stamp_is_unaffected(self) -> None:
        """Complement: popping an absent key must not raise or spuriously log."""
        path = self._seed("T-0009", "active")
        ticket.cmd_block(self._block_args("T-0009", not_before="2026-09-01"))
        fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields["not_before"], "2026-09-01T00:00:00Z")
        self.assertNotIn("auto_delivered", fields)
        self.assertNotIn("auto_delivered stamp cleared", body,
                         "no spurious log entry when there was no stamp to clear")

    # T-3112: the INVERSE of T-3081 — block that CLEARS a gate (--clear-not-before /
    # --clear-dep / --clear-deps) must ALSO strip auto_delivered / auto_delivered_at.
    # De-gating re-enters a ticket into the workable set, so a lingering stamp
    # becomes a live in-flight dispatch (holds the dispatch mutex + fires the 8h
    # backstop). Exact cause of the 8 spurious lock-release notifs on 2026-08-24:
    # a --clear-not-before de-gate batch left stale stamps that flipped
    # cosmetic→active. Mirrors T-0971 arm-A (cascade-unblock strip).

    def _seed_gated_with_stamp(self, tid, state="inbox", not_before=None,
                               depends_on=None) -> Path:
        path = self.root / state / f"{tid}.md"
        _write_ticket(path, tid=tid, state=state, not_before=not_before,
                      depends_on=depends_on)
        text = path.read_text(encoding="utf-8").replace(
            "target: recovery",
            "target: recovery\nauto_delivered: true\nauto_delivered_at: 2026-08-01T00:00:00Z",
            1,
        )
        path.write_text(text, encoding="utf-8")
        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        assert fields.get("auto_delivered") is not None, "seed did not carry the stamp"
        return path

    def _clear_args(self, tid, clear_not_before=False, clear_dep=None,
                    clear_deps=False, no_push=True) -> argparse.Namespace:
        ns = argparse.Namespace()
        ns.ticket = tid
        ns.not_before = None
        ns.not_before_anchor = None
        ns.depends_on = None
        ns.clear_not_before = clear_not_before
        ns.clear_dep = clear_dep
        ns.clear_deps = clear_deps
        ns.no_push = no_push
        return ns

    def test_clear_not_before_strips_auto_delivered_stamp(self) -> None:
        """T-3112: reproduction of 2026-08-24 — de-gating must strip the stamp."""
        path = self._seed_gated_with_stamp("T-0011", not_before="2026-09-01T00:00:00Z")
        ticket.cmd_block(self._clear_args("T-0011", clear_not_before=True))
        fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertNotIn("not_before", fields)
        self.assertNotIn("auto_delivered", fields)
        self.assertNotIn("auto_delivered_at", fields)
        self.assertIn("auto_delivered stamp cleared", body,
                      "clear must be visible in the transition log (audit trail)")

    def test_clear_dep_strips_auto_delivered_stamp(self) -> None:
        path = self._seed_gated_with_stamp("T-0012", depends_on=["T-9998"])
        ticket.cmd_block(self._clear_args("T-0012", clear_dep=["T-9998"]))
        fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields.get("depends_on") or [], [])
        self.assertNotIn("auto_delivered", fields)
        self.assertNotIn("auto_delivered_at", fields)
        self.assertIn("auto_delivered stamp cleared", body)

    def test_clear_deps_strips_auto_delivered_stamp(self) -> None:
        path = self._seed_gated_with_stamp("T-0013", depends_on=["T-9998", "T-9997"])
        ticket.cmd_block(self._clear_args("T-0013", clear_deps=True))
        fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertEqual(fields.get("depends_on") or [], [])
        self.assertNotIn("auto_delivered", fields)
        self.assertNotIn("auto_delivered_at", fields)
        self.assertIn("auto_delivered stamp cleared", body)

    def test_clear_not_before_without_stamp_is_unaffected(self) -> None:
        """Complement: popping an absent stamp must not raise or spuriously log."""
        path = self.root / "inbox" / "T-0014.md"
        _write_ticket(path, tid="T-0014", state="inbox",
                      not_before="2026-09-01T00:00:00Z")
        ticket.cmd_block(self._clear_args("T-0014", clear_not_before=True))
        fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertNotIn("not_before", fields)
        self.assertNotIn("auto_delivered stamp cleared", body,
                         "no spurious log entry when there was no stamp to clear")


class TestUnclaim(unittest.TestCase):
    """T-0663: unclaim moves ticket active/ → inbox/ (inverse of claim)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()
        self._pull_patch = patch.object(ticket, "_pull_if_needed")
        self.mock_pull = self._pull_patch.start()
        # Patch git mv to actually move the file (mirrors TestCmdBlock pattern).
        self._git_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._git_patch.start()

        def fake_run(argv, **kwargs):
            if argv[:2] == ["git", "mv"] and len(argv) >= 4:
                src = self.root / argv[2]
                dst = self.root / argv[3]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        self.mock_run.side_effect = fake_run
        os.environ["PANTHEON_ACTOR"] = "orchestrator"

    def tearDown(self) -> None:
        del os.environ["PANTHEON_ACTOR"]
        self._git_patch.stop()
        self._pull_patch.stop()
        self._commit_patch.stop()
        self._repo_patch.stop()
        self._gu_repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _seed(self, tid: str, state: str) -> Path:
        path = self.root / state / f"{tid}.md"
        _write_ticket(path, tid=tid, state=state)
        return path

    def _unclaim_args(self, tid: str, no_push: bool = True) -> argparse.Namespace:
        ns = argparse.Namespace()
        ns.ticket = tid
        ns.no_push = no_push
        return ns

    def test_unclaim_moves_active_to_inbox(self) -> None:
        """Happy path: active ticket lands in inbox after unclaim."""
        active_path = self._seed("T-0001", "active")
        inbox_path = self.root / "inbox" / "T-0001.md"

        rc = ticket.cmd_unclaim(self._unclaim_args("T-0001"))

        self.assertEqual(rc, 0)
        self.assertFalse(active_path.exists(), "file must leave active/")
        self.assertTrue(inbox_path.exists(), "file must land in inbox/")
        # State field updated in frontmatter.
        fields, _ = ticket.parse_front_matter(inbox_path.read_text(encoding="utf-8"))
        self.assertEqual(fields["state"], "inbox")
        # Transition log entry appended.
        _, body = ticket.parse_front_matter(inbox_path.read_text(encoding="utf-8"))
        self.assertIn("unclaimed", body)

    def test_unclaim_refuses_inbox_state(self) -> None:
        """unclaim on a ticket already in inbox must raise TicketError."""
        self._seed("T-0001", "inbox")
        with self.assertRaises(ticket.TicketError) as cm:
            ticket.cmd_unclaim(self._unclaim_args("T-0001"))
        self.assertIn("state-mismatch", str(cm.exception))
        self.assertIn("active", str(cm.exception))

    def test_unclaim_refuses_done_state(self) -> None:
        """unclaim on a done ticket must raise TicketError."""
        self._seed("T-0001", "done")
        with self.assertRaises(ticket.TicketError) as cm:
            ticket.cmd_unclaim(self._unclaim_args("T-0001"))
        self.assertIn("state-mismatch", str(cm.exception))

    def test_unclaim_no_push_flag(self) -> None:
        """--no-push passes no_push=True to _pull_if_needed (skips pull + push)."""
        self._seed("T-0001", "active")
        ticket.cmd_unclaim(self._unclaim_args("T-0001", no_push=True))
        # _pull_if_needed is called with no_push=True.
        self.mock_pull.assert_called_once_with(True)

    # T-3081: unclaim (active→inbox) must strip auto_delivered / auto_delivered_at
    # for the same dispatch-mutex reason as cmd_block above + cmd_retarget (T-2350)
    # + cascade-unblock (T-0971 arm-A). This test lives in TestUnclaim (not
    # TestRetarget) per the T-3081 gotcha — TestRetarget mocks subprocess.run so
    # git-mv is a no-op and the state-move never happens; TestUnclaim uses the
    # real-git-mv-shim shape via fake_run in setUp.

    def _seed_with_auto_delivered(self, tid: str, state: str) -> Path:
        _write_ticket(self.root / state / f"{tid}.md", tid=tid, state=state)
        path = self.root / state / f"{tid}.md"
        text = path.read_text(encoding="utf-8").replace(
            "target: recovery",
            "target: recovery\nauto_delivered: true\nauto_delivered_at: 2026-08-01T00:00:00Z",
            1,
        )
        path.write_text(text, encoding="utf-8")
        fields, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        assert fields.get("auto_delivered") is not None, "seed did not carry the stamp"
        return path

    def test_unclaim_strips_auto_delivered_stamp(self) -> None:
        active_path = self._seed_with_auto_delivered("T-0002", "active")
        inbox_path = self.root / "inbox" / "T-0002.md"

        rc = ticket.cmd_unclaim(self._unclaim_args("T-0002"))

        self.assertEqual(rc, 0)
        self.assertFalse(active_path.exists())
        self.assertTrue(inbox_path.exists())
        fields, body = ticket.parse_front_matter(inbox_path.read_text(encoding="utf-8"))
        self.assertEqual(fields["state"], "inbox")
        self.assertNotIn("auto_delivered", fields)
        self.assertNotIn("auto_delivered_at", fields)
        self.assertIn("auto_delivered stamp cleared", body,
                      "clear must be visible in transition log per T-2350 audit-trail rationale")

    def test_unclaim_without_stamp_is_unaffected(self) -> None:
        """Complement: popping an absent key must not raise or spuriously log."""
        self._seed("T-0003", "active")
        rc = ticket.cmd_unclaim(self._unclaim_args("T-0003"))
        self.assertEqual(rc, 0)
        inbox_path = self.root / "inbox" / "T-0003.md"
        fields, body = ticket.parse_front_matter(inbox_path.read_text(encoding="utf-8"))
        self.assertNotIn("auto_delivered", fields)
        self.assertNotIn("auto_delivered stamp cleared", body,
                         "no spurious log entry when there was no stamp to clear")


class TestBlockClearNotBefore(unittest.TestCase):
    """T-0663: block --clear-not-before removes not_before frontmatter field."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()
        self._pull_patch = patch.object(ticket, "_pull_if_needed")
        self.mock_pull = self._pull_patch.start()
        os.environ["PANTHEON_ACTOR"] = "orchestrator"

    def tearDown(self) -> None:
        del os.environ["PANTHEON_ACTOR"]
        self._pull_patch.stop()
        self._commit_patch.stop()
        self._repo_patch.stop()
        self._gu_repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _seed(self, tid: str, state: str = "active", not_before: str | None = None) -> Path:
        path = self.root / state / f"{tid}.md"
        _write_ticket(path, tid=tid, state=state)
        if not_before is not None:
            text = path.read_text(encoding="utf-8")
            fields, body = ticket.parse_front_matter(text)
            fields["not_before"] = not_before
            path.write_text(ticket._build_ticket_text(fields, body), encoding="utf-8")
        return path

    def _block_args(
        self,
        tid: str,
        not_before=None,
        depends_on=None,
        clear_not_before: bool = False,
        no_push: bool = True,
    ) -> argparse.Namespace:
        ns = argparse.Namespace()
        ns.ticket = tid
        ns.not_before = not_before
        ns.depends_on = depends_on
        ns.clear_not_before = clear_not_before
        ns.no_push = no_push
        return ns

    def test_block_clear_not_before_removes_field(self) -> None:
        """Happy path: not_before field is absent after --clear-not-before."""
        path = self._seed("T-0001", "active", not_before="2026-06-01T00:00:00Z")
        fields_before, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertIn("not_before", fields_before)

        rc = ticket.cmd_block(self._block_args("T-0001", clear_not_before=True))

        self.assertEqual(rc, 0)
        fields_after, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertNotIn("not_before", fields_after)

    def test_block_clear_not_before_idempotent(self) -> None:
        """--clear-not-before on a ticket with no not_before is a no-op (no error, no commit)."""
        path = self._seed("T-0001", "active")  # no not_before set
        fields_before, _ = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertNotIn("not_before", fields_before)

        rc = ticket.cmd_block(self._block_args("T-0001", clear_not_before=True))

        self.assertEqual(rc, 0)
        # No commit should have been issued (nothing changed).
        self.mock_commit.assert_not_called()

    def test_block_clear_not_before_preserves_lane(self) -> None:
        """Ticket stays in its current lane (active or inbox) after --clear-not-before."""
        active_path = self._seed("T-0001", "active", not_before="2026-06-01T00:00:00Z")
        inbox_path = self._seed("T-0002", "inbox", not_before="2026-06-01T00:00:00Z")

        ticket.cmd_block(self._block_args("T-0001", clear_not_before=True))
        ticket.cmd_block(self._block_args("T-0002", clear_not_before=True))

        # Files stay where they were.
        self.assertTrue(active_path.exists(), "T-0001 must stay in active/")
        self.assertTrue(inbox_path.exists(), "T-0002 must stay in inbox/")
        self.assertFalse((self.root / "inbox" / "T-0001.md").exists())
        self.assertFalse((self.root / "active" / "T-0002.md").exists())

    def test_block_clear_not_before_mutually_exclusive_with_set(self) -> None:
        """--clear-not-before together with --not-before must raise TicketError."""
        self._seed("T-0001", "active", not_before="2026-06-01T00:00:00Z")
        with self.assertRaises(ticket.TicketError) as cm:
            ticket.cmd_block(self._block_args(
                "T-0001",
                not_before="2026-07-01",
                clear_not_before=True,
            ))
        self.assertIn("--clear-not-before", str(cm.exception))

    def test_block_clear_not_before_transition_log_appended(self) -> None:
        """Transition log gets a 'not_before gate cleared' entry after --clear-not-before."""
        path = self._seed("T-0001", "active", not_before="2026-06-01T00:00:00Z")

        ticket.cmd_block(self._block_args("T-0001", clear_not_before=True))

        _, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        self.assertIn("not_before gate cleared", body)
        # Log line includes the actor.
        self.assertIn("[orchestrator]", body)


# ---------------------------------------------------------------------------
# Concurrent allocation lock — race-safety (T-0786)
# ---------------------------------------------------------------------------

# Module-level worker: must be at module scope (not a closure) so that
# multiprocessing can pickle it for subprocess dispatch.
def _alloc_worker(
    barrier: "multiprocessing.Barrier",
    tickets_root_str: str,
    lockfile_path: str,
    result_queue: "multiprocessing.Queue",
) -> None:
    """Worker process: barrier-sync, then allocate+write under _allocation_lock().

    Each worker:
    1. Patches ticket.TICKETS_ROOT and ticket._git so no real git runs.
    2. Sets PANTHEON_TICKET_ALLOC_LOCKFILE env-override to the shared temp lockfile.
    3. Waits on the barrier so all workers start allocation simultaneously.
    4. Calls _ticket._allocation_lock() then _next_ticket_id() inside it.
    5. Writes a stub T-NNNN.md into tickets_root/inbox/ WHILE holding the lock
       (so subsequent workers scanning the shared dir see it — this is what
       makes the flock load-bearing: without serialization two workers scan
       before either writes and both see the same max).
    6. Releases the lock, then puts the allocated ID onto result_queue.
    """
    import os
    import sys
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    # Bootstrap sys.path so `from tools.ticket import ...` resolves.
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import tools.ticket as _ticket

    tickets_root = Path(tickets_root_str)

    # Stub _git: fetch succeeds; ls-tree returns empty stdout (no origin tickets).
    def _git_stub(argv, **kwargs):
        if argv[:3] == ["git", "fetch", "origin"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if argv[:2] == ["git", "ls-tree"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    os.environ["PANTHEON_TICKET_ALLOC_LOCKFILE"] = lockfile_path

    with (
        patch.object(_ticket, "TICKETS_ROOT", tickets_root),
        patch("tools._git_utils.subprocess.run", side_effect=_git_stub),
    ):
        barrier.wait()  # sync: all workers arrive before any acquires the lock
        with _ticket._allocation_lock():
            tid = _ticket._next_ticket_id()
            # Write stub while holding the lock so subsequent workers scanning
            # the shared dir see this ID — this is what makes the lock
            # load-bearing for the test.
            (tickets_root / "inbox" / f"{tid}.md").write_text("stub")
        result_queue.put(tid)


class TestAllocationLockConcurrency(unittest.TestCase):
    """Race-safety acceptance test for _allocation_lock() + _next_ticket_id() (T-0786).

    Launches N concurrent processes, barrier-synchronizes their start so all
    arrive at the lock acquisition simultaneously, and asserts every process
    receives a unique T-NNNN.  Without the lock spanning scan→write, two or
    more workers scan the same max before either writes a file and both return
    the same ID.

    Uses multiprocessing (not threads) because fcntl.flock is process-level;
    threads in the same process share the fd and share the lock trivially
    (justifies via D11 — test must exercise the real cross-process surface).
    """

    WORKER_COUNT = 6  # enough to make the race highly likely without the fix

    def setUp(self) -> None:
        import multiprocessing
        self._mp = multiprocessing
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self._root / s).mkdir(parents=True)
        # Seed a few existing tickets so max > 0 (non-trivial starting point).
        for n in (1, 3, 5):
            (self._root / "inbox" / f"T-{n:04d}.md").write_text("stub")

    def tearDown(self) -> None:
        self._tmp.cleanup()
        # Clean up PANTHEON_TICKET_ALLOC_LOCKFILE from env if set by workers.
        os.environ.pop("PANTHEON_TICKET_ALLOC_LOCKFILE", None)

    def test_no_duplicate_ids_under_contention(self) -> None:
        """N concurrent processes must each receive a unique T-NNNN."""
        mp = self._mp
        ctx = mp.get_context("fork")
        barrier = ctx.Barrier(self.WORKER_COUNT)
        result_queue: "multiprocessing.Queue" = ctx.Queue()
        lockfile = os.path.join(self._tmp.name, "alloc.lock")

        procs = [
            ctx.Process(
                target=_alloc_worker,
                args=(barrier, str(self._root), lockfile, result_queue),
            )
            for _ in range(self.WORKER_COUNT)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)

        # Collect results.
        ids = []
        while not result_queue.empty():
            ids.append(result_queue.get_nowait())

        self.assertEqual(
            len(ids),
            self.WORKER_COUNT,
            f"Expected {self.WORKER_COUNT} IDs; got {len(ids)}: {ids}. "
            "Worker(s) may have crashed — check stderr above.",
        )
        self.assertEqual(
            len(ids),
            len(set(ids)),
            f"Duplicate IDs allocated under contention: {ids}",
        )


# ---------------------------------------------------------------------------
# _open_world_writable open-logic unit tests (T-0823)
# ---------------------------------------------------------------------------


class TestOpenWorldWritable(unittest.TestCase):
    """Unit tests for the protected_regular-safe open logic in _open_world_writable.

    These tests verify the three open-logic paths:
      (a) Existing file → opened WITHOUT O_CREAT (protected_regular-safe path).
      (b) Absent file   → created with mode 0o666 (world-writable).
      (c) Create race   → O_CREAT raises PermissionError → falls back to plain
                          O_WRONLY (another uid created the file between our
                          ENOENT and our O_CREAT).

    fs.protected_regular=2 cannot be set in a unit test, so (a) and (c) are
    verified by inspecting the os.open flags actually passed.  (b) is verified
    by checking the real filesystem mode of the created file.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # (a) existing file → no O_CREAT flag
    # ------------------------------------------------------------------
    def test_existing_file_opens_without_o_creat(self) -> None:
        """When the lockfile already exists, _open_world_writable must NOT pass
        O_CREAT to os.open.  Under fs.protected_regular=2 an O_CREAT open of a
        file owned by another uid would be refused; verifying the flag is absent
        proves the protected_regular-safe path is taken."""
        path = os.path.join(self._tmp.name, "existing.lock")
        # Pre-create the file so the primary O_WRONLY branch fires.
        open(path, "w").close()

        calls: list[tuple] = []
        real_os_open = os.open

        def spy_os_open(p, flags, mode=0o777, **kwargs):
            calls.append((p, flags, mode))
            return real_os_open(p, flags, mode, **kwargs)

        with patch("tools.ticket.os.open", side_effect=spy_os_open):
            fobj = ticket._open_world_writable(path)
            fobj.close()

        # Exactly one call; O_CREAT must not be set.
        self.assertEqual(len(calls), 1, f"Expected 1 os.open call, got: {calls}")
        _, flags, _ = calls[0]
        self.assertFalse(
            flags & os.O_CREAT,
            f"O_CREAT should be absent when file exists; flags={oct(flags)}",
        )

    # ------------------------------------------------------------------
    # (b) absent file → created with mode 0o666
    # ------------------------------------------------------------------
    def test_absent_file_created_world_writable(self) -> None:
        """When the lockfile does not exist, _open_world_writable creates it
        with mode 0o666 (world-writable) so all four uids can open it later."""
        path = os.path.join(self._tmp.name, "new.lock")
        self.assertFalse(os.path.exists(path))

        fobj = ticket._open_world_writable(path)
        fobj.close()

        self.assertTrue(os.path.exists(path))
        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(
            mode,
            0o666,
            f"Expected mode 0o666, got {oct(mode)}",
        )

    # ------------------------------------------------------------------
    # (c) create-race fallback: O_CREAT raises PermissionError
    # ------------------------------------------------------------------
    def test_create_race_falls_back_to_plain_wronly(self) -> None:
        """Simulates the create race: the no-O_CREAT open raises FileNotFoundError
        (file absent), but then the O_CREAT open raises PermissionError (another
        uid created the file in the window).  The implementation must fall back
        to a plain O_WRONLY open and succeed without raising."""
        path = os.path.join(self._tmp.name, "race.lock")
        real_os_open = os.open

        call_flags: list[int] = []

        def mock_os_open(p, flags, mode=0o777, **kwargs):
            call_flags.append(flags)
            if flags == os.O_WRONLY and not call_flags[:-1]:
                # First call: plain O_WRONLY → file absent
                raise FileNotFoundError(2, "No such file or directory")
            if flags & os.O_CREAT:
                # O_CREAT call → simulate another uid won the race
                raise PermissionError(1, "Operation not permitted")
            # Final fallback plain O_WRONLY → pre-create and open for real
            open(path, "w").close()
            return real_os_open(path, os.O_WRONLY)

        with patch("tools.ticket.os.open", side_effect=mock_os_open):
            fobj = ticket._open_world_writable(path)
            fobj.close()

        # Three calls: plain O_WRONLY (→ ENOENT), O_CREAT (→ EPERM), plain O_WRONLY (→ ok).
        self.assertEqual(len(call_flags), 3, f"Expected 3 os.open calls, got: {call_flags}")
        self.assertFalse(call_flags[0] & os.O_CREAT, "First call must not have O_CREAT")
        self.assertTrue(call_flags[1] & os.O_CREAT, "Second call must have O_CREAT")
        self.assertFalse(call_flags[2] & os.O_CREAT, "Third call must not have O_CREAT")


class TestServesField(unittest.TestCase):
    """T-1103: --serves flag on file and retarget subcommands writes serves: frontmatter field."""

    def _file_args(self, *, target: str = "orchestrator", serves: str | None = None,
                   appetite: str | None = None):
        args = MagicMock()
        args.requester = "orchestrator"
        args.target = target
        args.title = "test ticket"
        args.labels = None
        args.depends_on = None
        args.stale_after = None
        args.not_before = None
        args.parent_ticket = None
        args.ratifiable_file = None
        args.intent = None
        args.ask = ""
        args.context = ""
        args.no_push = False
        args.spawn_impl = False
        args.terminal = False
        args.impl_title = None
        args.impl_ask = None
        args.appetite = appetite  # T-3440/ORG-089: a non-hygiene serves is a bet
        args.bet_ref = None
        args.impl_target = None
        args.serves = serves
        # T-3328: tests that omit `serves` (retest_status / serves-omission
        # coverage) need SOME lineage edge or the orphan escape hatch — this
        # class is testing serves-field mechanics specifically, not DAG
        # connectivity, so orphan is the honest fixture choice. Harmless when
        # `serves` is also set (both may be present at once).
        args.orphan = "test fixture: T-1103/T-2613 serves+retest_status field mechanics"
        return args

    def _file_under_tmp(self, args) -> str:
        """Run cmd_file in a temp dir; return the filed ticket text."""
        with (
            patch("tools.ticket._next_ticket_id", return_value="T-0099"),
            patch("tools.ticket._pull_if_needed"),
            patch("tools.ticket._commit_and_push"),
        ):
            import tempfile as _tf
            with _tf.TemporaryDirectory() as tmp:
                root = Path(tmp)
                orig_root = ticket.TICKETS_ROOT
                ticket.TICKETS_ROOT = root / "tickets"
                (root / "tickets" / "inbox").mkdir(parents=True)
                try:
                    ticket.cmd_file(args)
                    filed = list((root / "tickets" / "inbox").iterdir())
                    self.assertEqual(len(filed), 1)
                    return filed[0].read_text()
                finally:
                    ticket.TICKETS_ROOT = orig_root

    def test_file_writes_serves_when_provided(self) -> None:
        """--serves value appears in frontmatter."""
        text = self._file_under_tmp(self._file_args(
            serves="ORG-034 arc-intent structural", appetite="5"))
        self.assertIn("serves: ORG-034 arc-intent structural", text)

    def test_file_writes_retest_status_when_provided(self) -> None:
        """T-2613: --retest-status value appears in frontmatter and round-trips."""
        args = self._file_args()
        args.retest_status = "pending"
        text = self._file_under_tmp(args)
        self.assertIn("retest_status: pending", text)
        fields, _body = ticket.parse_front_matter(text)
        self.assertEqual(fields.get("retest_status"), "pending")

    def test_file_omits_retest_status_when_not_provided(self) -> None:
        """T-2613: retest_status: field absent when --retest-status not given."""
        args = self._file_args()
        args.retest_status = None
        text = self._file_under_tmp(args)
        self.assertNotIn("retest_status:", text)

    def test_file_omits_serves_when_not_provided(self) -> None:
        """serves: field absent when --serves not given."""
        text = self._file_under_tmp(self._file_args(serves=None))
        self.assertNotIn("serves:", text)

    def test_file_serves_roundtrips_through_frontmatter(self) -> None:
        """serves: value survives serialize/parse round-trip."""
        text = self._file_under_tmp(self._file_args(serves="my-arc-intent", appetite="5"))
        fields, _body = ticket.parse_front_matter(text)
        self.assertEqual(fields.get("serves"), "my-arc-intent")

    def test_retarget_writes_serves_when_provided(self) -> None:
        """retarget --serves writes serves: field into frontmatter."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "inbox").mkdir()
            path = root / "inbox" / "T-0001.md"
            _write_ticket(path, tid="T-0001", state="inbox", target="recovery")

            with (
                patch.object(ticket, "TICKETS_ROOT", root),
                patch.object(ticket, "REPO_ROOT", root),
                patch("tools._git_utils.subprocess.run",
                      return_value=MagicMock(returncode=0, stdout="", stderr="")),
                patch.object(ticket, "_commit_and_push"),
                patch.object(ticket, "_pull_if_needed"),
            ):
                ns = argparse.Namespace(
                    ticket="T-0001",
                    to="orchestrator",
                    note=None,
                    no_push=False,
                    keep_state=False,
                    intent=None,
                    serves="ORG-034 test",
                )
                ticket.cmd_retarget(ns)

            text = path.read_text(encoding="utf-8")
            fields, _body = ticket.parse_front_matter(text)
            self.assertEqual(fields.get("serves"), "ORG-034 test")

    def test_retarget_preserves_existing_serves_when_flag_absent(self) -> None:
        """retarget without --serves leaves existing serves: untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "inbox").mkdir()
            path = root / "inbox" / "T-0002.md"
            # Manually write a ticket that already has serves:
            content = (
                "---\n"
                "id: T-0002\n"
                "created: 2026-06-12T00:00:00Z\n"
                "requester: orchestrator\n"
                "target: recovery\n"
                "state: inbox\n"
                "parent_ticket: null\n"
                "depends_on: []\n"
                "serves: existing-arc\n"
                "---\n"
                "\n# Test\n\n## Ask\n\nDo it.\n\n## Transition log\n\n"
                "- 2026-06-12T00:00:00Z [orchestrator] filed in inbox\n"
            )
            path.write_text(content, encoding="utf-8")

            with (
                patch.object(ticket, "TICKETS_ROOT", root),
                patch.object(ticket, "REPO_ROOT", root),
                patch("tools._git_utils.subprocess.run",
                      return_value=MagicMock(returncode=0, stdout="", stderr="")),
                patch.object(ticket, "_commit_and_push"),
                patch.object(ticket, "_pull_if_needed"),
            ):
                ns = argparse.Namespace(
                    ticket="T-0002",
                    to="orchestrator",
                    note=None,
                    no_push=False,
                    keep_state=False,
                    intent=None,
                    serves=None,
                )
                ticket.cmd_retarget(ns)

            fields, _body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
            self.assertEqual(fields.get("serves"), "existing-arc")


class TestFileTimeLineageGuardT3328(unittest.TestCase):
    """T-3328: a new ticket must carry >=1 lineage edge (parent_ticket,
    depends_on, serves, implements, verifies, supersedes, informs) or an
    explicit --orphan justification — closes the file-time orphan-bleed.
    """

    def _file_args(self, **overrides) -> MagicMock:
        args = MagicMock()
        args.requester = "orchestrator"
        args.target = "recovery"
        args.title = "T-3328 guard test ticket"
        args.labels = None
        args.depends_on = None
        args.stale_after = None
        args.not_before = None
        args.parent_ticket = None
        args.ratifiable_file = None
        args.intent = None
        args.ask = ""
        args.context = ""
        args.no_push = False
        args.spawn_impl = False
        args.terminal = False
        args.impl_title = None
        args.impl_ask = None
        args.appetite = None  # T-3440/ORG-089: getattr-read default
        args.bet_ref = None
        args.impl_target = None
        args.serves = None
        args.implements = None
        args.verifies = None
        args.supersedes = None
        args.informs = None
        args.orphan = None
        for k, v in overrides.items():
            setattr(args, k, v)
        return args

    def _file_under_tmp(self, args) -> str:
        """Run cmd_file inside a fresh tmpdir; return the filed ticket text."""
        with (
            patch("tools.ticket._next_ticket_id", return_value="T-0099"),
            patch("tools.ticket._pull_if_needed"),
            patch("tools.ticket._commit_and_push"),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                orig_root = ticket.TICKETS_ROOT
                ticket.TICKETS_ROOT = root / "tickets"
                (root / "tickets" / "inbox").mkdir(parents=True)
                try:
                    ticket.cmd_file(args)
                    filed = list((root / "tickets" / "inbox").iterdir())
                    self.assertEqual(len(filed), 1)
                    return filed[0].read_text(encoding="utf-8")
                finally:
                    ticket.TICKETS_ROOT = orig_root

    def test_refuses_when_no_edge_and_no_orphan(self) -> None:
        with self.assertRaises(ticket.TicketError) as cm:
            self._file_under_tmp(self._file_args())
        msg = str(cm.exception)
        self.assertIn("orphan", msg.lower())
        self.assertIn("--serves", msg)
        self.assertIn("--orphan", msg)

    def test_accepts_each_of_the_seven_lineage_edges(self) -> None:
        cases = {
            "parent_ticket": "T-0042",
            "depends_on": "T-0042",
            "serves": "goal-neutral-maintenance",
            "implements": "T-0042",
            "verifies": "T-0042",
            "supersedes": "T-0042",
            "informs": "T-0042",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                text = self._file_under_tmp(self._file_args(**{field: value}))
                self.assertIn("id: T-0099", text)

    def test_accepts_orphan_with_valid_justification(self) -> None:
        text = self._file_under_tmp(
            self._file_args(orphan="a genuinely rootless one-off ticket")
        )
        self.assertIn("orphan_reason: a genuinely rootless one-off ticket", text)

    def test_refuses_orphan_with_empty_justification(self) -> None:
        with self.assertRaises(ticket.TicketError) as cm:
            self._file_under_tmp(self._file_args(orphan=""))
        self.assertIn("--orphan justification too short", str(cm.exception))

    def test_refuses_orphan_with_short_justification(self) -> None:
        with self.assertRaises(ticket.TicketError) as cm:
            self._file_under_tmp(self._file_args(orphan="too short"))
        self.assertIn("--orphan justification too short", str(cm.exception))
        self.assertIn("minimum 20", str(cm.exception))

    def test_orphan_reason_roundtrips_through_frontmatter(self) -> None:
        text = self._file_under_tmp(
            self._file_args(orphan="a genuinely rootless one-off ticket")
        )
        fields, _body = ticket.parse_front_matter(text)
        self.assertEqual(fields.get("orphan_reason"), "a genuinely rootless one-off ticket")
        serialized = ticket.serialize_front_matter(fields)
        self.assertIn("orphan_reason: a genuinely rootless one-off ticket", serialized)
        fields2, _ = ticket.parse_front_matter(serialized + "\n")
        self.assertEqual(fields2.get("orphan_reason"), fields.get("orphan_reason"))

    def test_orphan_reason_absent_when_lineage_edge_present(self) -> None:
        """No --orphan given + a real edge present → no orphan_reason field."""
        text = self._file_under_tmp(self._file_args(serves="goal-neutral-maintenance"))
        self.assertNotIn("orphan_reason:", text)


class TestParseNotBefore(unittest.TestCase):
    """T-1383 / ORG-A4: not_before parsing must be portable to py3.10, whose
    fromisoformat rejects the trailing 'Z' the validator's own regex requires."""

    def test_full_utc_z_accepted(self):
        self.assertEqual(ticket._parse_not_before("2026-07-01T00:00:00Z"), "2026-07-01T00:00:00Z")

    def test_bare_date_normalized(self):
        self.assertEqual(ticket._parse_not_before("2026-07-01"), "2026-07-01T00:00:00Z")

    def test_invalid_shape_rejected(self):
        with self.assertRaises(ticket.TicketError):
            ticket._parse_not_before("2026/07/01")

    def test_impossible_date_rejected(self):
        with self.assertRaises(ticket.TicketError):
            ticket._parse_not_before("2026-13-40")


class TestConcurrentTransitionRecheck(unittest.TestCase):
    """T-2140 finding (1): the state check runs OUTSIDE the mutation lock, so two
    writers can both pass it and both arrive. The pull INSIDE the lock can also
    bring in another writer's transition of the same ticket."""

    def test_state_is_reverified_inside_the_lock_after_the_pull(self):
        """Simulates the race: state is correct at the pre-lock check and WRONG by
        the time the lock is held. Must refuse, and must not mutate.

        Synthetic paths so the assertion does not depend on where a real ticket
        happens to sit today — an earlier draft of this test silently exercised
        the PRE-lock check instead, because the ticket it picked was already done.
        """
        import tools.ticket as tk
        calls = {"n": 0}
        inbox_p = tk.TICKETS_ROOT / "inbox" / "T-9001.md"
        done_p = tk.TICKETS_ROOT / "done" / "T-9001.md"

        def flaky_find(tid):
            calls["n"] += 1
            # 1st = pre-lock check (looks claimable); 2nd = in-lock re-verify
            # (another writer advanced it while we waited for the lock).
            return inbox_p if calls["n"] == 1 else done_p

        moved = []
        with patch.object(tk, "_find_ticket", side_effect=flaky_find), \
             patch.object(tk, "_pull_if_needed", lambda *a, **k: None), \
             patch.object(tk, "git_mutation_lock", _nullcontext), \
             patch.object(tk, "_git", lambda *a, **k: moved.append(a) or _ok()):
            with self.assertRaises(tk.TicketError) as ctx:
                tk._do_transition(
                    tid="T-9001", expected_state="inbox", new_state="active",
                    verb="claim", detail=None, no_push=True)
        self.assertIn("concurrent-transition conflict", str(ctx.exception))
        self.assertEqual(calls["n"], 2, "must re-resolve the ticket inside the lock")
        self.assertEqual(moved, [], "must not git-mv after detecting the conflict")

    def test_no_conflict_when_state_holds_through_the_lock(self):
        """Control: the re-verify must not reject the ordinary case.

        Deliberately narrow — it asserts only that the CONFLICT is not raised.
        Letting the transition run to completion would require a real ticket on
        disk, and a control that needs more scaffolding than the case it guards
        tends to end up asserting the scaffolding.
        """
        import tools.ticket as tk
        inbox_p = tk.TICKETS_ROOT / "inbox" / "T-9002.md"
        with patch.object(tk, "_find_ticket", lambda tid: inbox_p), \
             patch.object(tk, "_pull_if_needed", lambda *a, **k: None), \
             patch.object(tk, "git_mutation_lock", _nullcontext), \
             patch.object(tk, "_git", lambda *a, **k: _ok()):
            try:
                tk._do_transition(
                    tid="T-9002", expected_state="inbox", new_state="active",
                    verb="claim", detail=None, no_push=True)
            except Exception as e:
                self.assertNotIn("concurrent-transition conflict", str(e),
                                 "the control case must not trip the re-verify")


def _ok():
    import types
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


class TestOsUserPersonaMapCoversTheFleet(unittest.TestCase):
    """T-2255: the WIP-cap OS-account -> persona map must cover EVERY seat.

    `_claiming_persona()` fails OPEN by design — an unrecognised identity passes
    through unchanged and the cap silently does not apply. That is the right
    posture (never block a claim you cannot attribute) and it is why a MISSING
    entry is invisible: no error, no warning, just an uncapped seat.

    exocortex/mnemosyne (ORG-042) was absent while the LATER ORG-043 seats were
    present, so one seat ran uncapped. Pinning every account explicitly is what
    makes the next omission fail here instead of in production.

    ADDING A PERSONA: add it to `_OS_USER_PERSONA` *and* to this test.
    """

    # OS account -> persona, for every Claude Code seat on the VPS
    # (README.md Topology: six as of ORG-042 + ORG-043).
    FLEET = {
        "claude": "orchestrator",
        "claude-research": "specialist",
        "claude-fallback": "recovery",
        "exocortex": "mnemosyne",
        "suborch1": "metis",
        "suborch2": "atlas",
    }

    def test_every_fleet_account_resolves_to_its_persona(self):
        for account, persona in self.FLEET.items():
            with self.subTest(account=account):
                self.assertEqual(
                    ticket._OS_USER_PERSONA.get(account), persona,
                    f"{account} missing or mismapped -> the WIP cap fails OPEN "
                    f"for that seat and nothing surfaces it",
                )

    def test_no_account_resolves_to_itself(self):
        """The fail-open signature: an unmapped account returns its own name.

        If `_claiming_persona` ever hands back the OS account, the cap is
        keying on a value no ticket carries, so it can never match.
        """
        for account in self.FLEET:
            with self.subTest(account=account):
                self.assertNotEqual(
                    ticket._claiming_persona.__globals__["_OS_USER_PERSONA"].get(account),
                    account,
                    f"{account} resolves to itself -> uncapped",
                )


class _nullcontext:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False





class TestNoFlagAbbreviationT2302(unittest.TestCase):
    """T-2302 (specialist): argparse's allow_abbrev defaults True, so `file --label a
    --label b` was silently parsed as the single-valued `--labels` with last-wins
    semantics — exit 0, no warning, three of four labels GONE.

    Measured cost: T-2295 was filed with four labels and stored one. The dropped
    `post-office-rename` is the label someone sweeps for BEFORE performing that
    cut, so the ticket was unfindable at the one moment it exists to be found.
    Labels are the DISCOVERY surface.
    """

    def setUp(self):
        self.p = ticket._build_parser()
        self.base = ["file", "--requester", "recovery", "--target", "recovery", "--title", "t"]

    def _rejects(self, argv):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.p.parse_args(argv)

    def test_CONTROL_the_real_flag_still_works(self):
        """Without this, 'abbreviations are rejected' could pass because
        everything is rejected."""
        ns = self.p.parse_args(self.base + ["--labels", "a,b"])
        self.assertEqual(ns.labels, "a,b")

    def test_the_exact_bug_is_now_a_hard_error(self):
        self._rejects(self.base + ["--label", "a", "--label", "b"])

    def test_arbitrary_prefixes_are_rejected_too(self):
        """The class is wider than labels — every long flag was abbreviable, so
        any near-miss typo that happened to be a unique prefix was accepted."""
        self._rejects(self.base + ["--tit", "x"])

    def test_REPEATED_full_form_option_is_rejected_not_last_wins(self):
        """T-2315 (specialist): the defect my abbreviation fix did NOT close.

        `--labels a --labels b` on the canonical spelling still kept only `b`
        after b0404bcae — exit 0, no warning. allow_abbrev=False removed the
        ROUTE (--label as a prefix) and left the DEFECT, so the loss stayed
        reachable by anyone who never abbreviated anything. Worse, it pointed
        readers at the canonical spelling as if it were safe.
        """
        self._rejects(self.base + ["--labels", "a", "--labels", "b"])

    def test_REPEATED_depends_on_is_rejected_because_it_UNGATES_a_ticket(self):
        """The severe case: a dropped dependency does not lose metadata, it
        silently un-gates a ticket."""
        self._rejects(self.base + ["--depends-on", "T-1111", "--depends-on", "T-2222"])

    def test_CONTROL_single_use_and_comma_form_still_accepted(self):
        """Without this, 'repeats are rejected' could pass because everything
        is rejected."""
        ns = self.p.parse_args(self.base + ["--labels", "a,b", "--depends-on", "T-1"])
        self.assertEqual(ns.labels, "a,b")
        self.assertEqual(ns.depends_on, "T-1")

    def test_error_text_makes_no_claim_false_on_the_subcommand_printing_it(self):
        """T-2315 follow-up (specialist): the error prints on EVERY subcommand.

        Its first version ended '...and how a --depends-on repeat would un-gate
        a ticket. Pass ONE value' — emitted verbatim by `block`, where
        --depends-on is an append action and repeating it is the CORRECT idiom.
        So the error argued the reader out of the right usage, on the very
        subcommand where it is right. Third site of one overclaim, and the only
        one that shipped.
        """
        import io as _io, contextlib as _cl
        err = _io.StringIO()
        with self.assertRaises(SystemExit):
            with _cl.redirect_stderr(err):
                self.p.parse_args(["block", "T-1", "--not-before", "x", "--not-before", "y"])
        text = " ".join(err.getvalue().split())
        self.assertNotIn("un-gate a ticket", text,
                         "claims a --depends-on repeat un-gates, which is FALSE on block")
        self.assertIn("action='append'", text,
                      "must name the exception so it does not argue against `block --depends-on` x2")

    def test_CONTROL_block_depends_on_repeat_is_still_ACCEPTED(self):
        """The idiom the error must not discourage — and proof the no-repeat
        rule declines to touch options with an explicit action."""
        ns = self.p.parse_args(["block", "T-1", "--depends-on", "A", "--depends-on", "B"])
        self.assertEqual(ns.depends_on, ["A", "B"])

    def test_store_true_flags_are_untouched_by_the_no_repeat_rule(self):
        """--no-push twice is harmless and must not become an error — the rule
        targets value-taking options, where last-wins DISCARDS data."""
        ns = self.p.parse_args(self.base + ["--no-push", "--no-push"])
        self.assertTrue(ns.no_push)

    def test_list_label_still_works_because_it_is_a_REAL_flag_there(self):
        """The collision that made the silent case so plausible: --label is a
        genuine flag on `list` and an abbreviation on `file`."""
        ns = self.p.parse_args(["list", "--label", "mailbox"])
        self.assertEqual(ns.label, "mailbox")


class TestReadyExplainsWhyItIsEmpty(unittest.TestCase):
    """T-2220: an empty `ready` must distinguish rest from deadlock.

    The original output was `(no ready tickets)` in every case, so it read identically
    whether the queue was clear or wedged. A three-way depends_on cycle made
    `ready --target specialist` return nothing, specialist read it as correctly-gated, and
    the arc stalled.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _ready(self, target="recovery"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # T-2720: cmd_ready reads args.show_unblocks; a real Namespace
            # (unlike MagicMock) raises AttributeError if it's absent.
            rc = ticket.cmd_ready(
                argparse.Namespace(target=target, show_unblocks=False)
            )
        return rc, buf.getvalue()

    def test_genuinely_empty_says_so(self):
        rc, out = self._ready()
        self.assertEqual(rc, 0)
        self.assertIn("genuinely EMPTY", out)

    def test_dep_gated_queue_is_not_reported_as_empty(self):
        _write_ticket(self.root / "inbox" / "T-9001.md", tid="T-9001",
                      state="inbox", depends_on=["T-9002"])
        _write_ticket(self.root / "inbox" / "T-9002.md", tid="T-9002",
                      state="inbox", not_before="2099-01-01T00:00:00Z")
        rc, out = self._ready()
        self.assertEqual(rc, 0)
        self.assertNotIn("genuinely EMPTY", out)
        self.assertIn("NOT empty", out)
        self.assertIn("T-9001", out)   # dep-gated
        self.assertIn("T-9002", out)   # date-gated

    def test_deadlock_is_named_as_a_deadlock(self):
        """The case that cost the arc: a cycle reads as 'correctly gated'."""
        for a, b in (("T-9001", "T-9002"), ("T-9002", "T-9003"), ("T-9003", "T-9001")):
            _write_ticket(self.root / "inbox" / f"{a}.md", tid=a, state="inbox",
                          depends_on=[b])
        rc, out = self._ready()
        self.assertEqual(rc, 0)
        self.assertIn("DEADLOCK", out)

    def test_ready_tickets_still_list_normally(self):
        _write_ticket(self.root / "inbox" / "T-9001.md", tid="T-9001", state="inbox")
        rc, out = self._ready()
        self.assertEqual(rc, 0)
        self.assertIn("T-9001", out)
        self.assertNotIn("genuinely EMPTY", out)
        self.assertNotIn("DEADLOCK", out)


class TestResolveWarnsOnUnmetDeps(unittest.TestCase):
    """T-2220: closing with an unmet gate must WARN, never block."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / s).mkdir(parents=True)
        self._patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _warn(self, tid):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ticket._warn_unmet_deps(tid, "resolving")
        return buf.getvalue()

    def test_unmet_dep_warns(self):
        # The measured instance: T-2113 landed in done/ gated on a still-inbox dep.
        _write_ticket(self.root / "active" / "T-9001.md", tid="T-9001",
                      state="active", depends_on=["T-9002"])
        _write_ticket(self.root / "inbox" / "T-9002.md", tid="T-9002", state="inbox")
        out = self._warn("T-9001")
        self.assertIn("UNMET", out)
        self.assertIn("T-9002", out)

    def test_met_dep_is_silent(self):
        _write_ticket(self.root / "active" / "T-9001.md", tid="T-9001",
                      state="active", depends_on=["T-9002"])
        _write_ticket(self.root / "done" / "T-9002.md", tid="T-9002", state="done")
        self.assertEqual(self._warn("T-9001"), "")

    def test_no_deps_is_silent(self):
        _write_ticket(self.root / "active" / "T-9001.md", tid="T-9001", state="active")
        self.assertEqual(self._warn("T-9001"), "")

    def test_unknown_dep_id_counts_as_unmet(self):
        # "Cannot tell" must not read as satisfied.
        _write_ticket(self.root / "active" / "T-9001.md", tid="T-9001",
                      state="active", depends_on=["T-9999"])
        out = self._warn("T-9001")
        self.assertIn("UNMET", out)

    def test_cmd_resolve_ACTUALLY_CALLS_the_warning(self):
        """Wiring, not just the helper.

        Caught by mutation: deleting the call site from cmd_resolve left every
        other test in this class green, because they invoke _warn_unmet_deps
        directly. A helper that works and is never called is the same defect as
        one that does not work, and it is the harder one to see.
        """
        with patch.object(ticket, "_warn_unmet_deps") as w, \
             patch.object(ticket, "_do_transition", return_value=0):
            ticket.cmd_resolve(argparse.Namespace(
                ticket="T-9001", with_summary="x", no_push=True))
        w.assert_called_once()
        self.assertEqual(w.call_args.args[0], "T-9001")

    def test_warning_never_raises_on_a_bad_read(self):
        # Fail-soft: a close must not fail because its advisory could not compute.
        self.assertEqual(self._warn("T-9404"), "")


class TestSupersedeAsk(unittest.TestCase):
    """T-2247: the Ask is the field most likely to be acted on and, until this
    verb, the only one with no correction path.

    The load-bearing property under test is NOT formatting. It is that
    `inbox_consumer._ask_body()` extracts exactly the `## Ask` section and
    injects it as the auto-delivered prompt — so a correction anywhere else on
    the ticket is not delivered LATE, it is not delivered AT ALL.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for st in ticket.STATES:
            (self.root / st).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._tickets_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._sub_patch = patch("tools._git_utils.subprocess.run")
        self.mock_run = self._sub_patch.start()
        self.mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self.mock_commit = self._commit_patch.start()

    def tearDown(self) -> None:
        for p in (self._tickets_patch, self._repo_patch, self._gu_repo_patch,
                  self._sub_patch, self._commit_patch):
            p.stop()
        self._tmp.cleanup()

    def _seed(self, tid: str, state: str, ask: str = "Go look at the stale render.") -> Path:
        path = self.root / state / f"{tid}.md"
        _write_ticket(path, tid=tid, state=state, ask=ask, context="some context")
        return path

    def _args(self, tid: str, correction) -> argparse.Namespace:
        ns = argparse.Namespace()
        ns.ticket = tid
        ns.with_correction = correction
        ns.no_push = True
        ns.no_pull = True
        return ns

    # -- the property that matters -------------------------------------------

    def test_correction_reaches_the_auto_delivered_prompt(self) -> None:
        """THE POINT OF THE VERB. Same extractor the consumer uses."""
        from tools.inbox_consumer import _ask_body

        path = self._seed("T-0001", "inbox")
        ticket.cmd_supersede_ask(self._args("T-0001", "Use the CURRENT render instead."))
        _, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        delivered = _ask_body(body)
        self.assertIn("Use the CURRENT render instead.", delivered)

    def test_correction_is_delivered_BEFORE_the_original(self) -> None:
        """Read-order is the defect being fixed: a correction below the stale
        instruction is read second, and on a long ticket, much later."""
        from tools.inbox_consumer import _ask_body

        path = self._seed("T-0002", "inbox")
        ticket.cmd_supersede_ask(self._args("T-0002", "CORRECTED INSTRUCTION"))
        _, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        delivered = _ask_body(body)
        self.assertLess(
            delivered.index("CORRECTED INSTRUCTION"),
            delivered.index("stale render"),
            "the correction must precede the text it corrects in the injected prompt",
        )

    def test_the_original_ask_is_ALSO_delivered_not_truncated_away(self) -> None:
        """The marker is bold text, not a `###` subheading, on purpose:
        `_ask_body` terminates on `\\n##`, which `###` matches too. A subheading
        here would cut the original out of the prompt, leaving the correction's
        'not the text below it' pointing at nothing the reader can see."""
        from tools.inbox_consumer import _ask_body

        path = self._seed("T-0003", "inbox")
        ticket.cmd_supersede_ask(self._args("T-0003", "new instruction"))
        _, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
        delivered = _ask_body(body)
        self.assertIn("stale render", delivered)
        self.assertIn("Original Ask", delivered)

    # -- record preservation --------------------------------------------------

    def test_original_ask_text_is_preserved_verbatim(self) -> None:
        path = self._seed("T-0004", "inbox", ask="EXACT ORIGINAL WORDS")
        ticket.cmd_supersede_ask(self._args("T-0004", "replacement"))
        self.assertIn("EXACT ORIGINAL WORDS", path.read_text(encoding="utf-8"))

    def test_other_sections_are_untouched(self) -> None:
        path = self._seed("T-0005", "inbox")
        before = path.read_text(encoding="utf-8")
        ticket.cmd_supersede_ask(self._args("T-0005", "replacement"))
        after = path.read_text(encoding="utf-8")
        self.assertIn("some context", after)
        self.assertIn("## Transition log", after)
        self.assertIn("## Context", before)

    def test_it_also_lands_a_transition_log_line(self) -> None:
        """In-section block is for the reader; the log line is for anyone
        scanning the ticket's history."""
        path = self._seed("T-0006", "inbox")
        ticket.cmd_supersede_ask(self._args("T-0006", "replacement text"))
        body = path.read_text(encoding="utf-8")
        self.assertRegex(body, r"\[[^\]]+\] note; \[SUPERSEDED ASK\] replacement text")

    def test_repeated_supersedes_stack_newest_first_with_one_original(self) -> None:
        path = self._seed("T-0007", "inbox")
        ticket.cmd_supersede_ask(self._args("T-0007", "FIRST correction"))
        ticket.cmd_supersede_ask(self._args("T-0007", "SECOND correction"))
        body = path.read_text(encoding="utf-8")
        self.assertEqual(body.count("Original Ask (superseded"), 1,
                         "the original must be demoted exactly once, not once per correction")
        self.assertLess(body.index("SECOND correction"), body.index("FIRST correction"))
        self.assertLess(body.index("FIRST correction"), body.index("stale render"))

    # -- refusals -------------------------------------------------------------

    def test_refuses_on_done(self) -> None:
        self._seed("T-0008", "done")
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_supersede_ask(self._args("T-0008", "too late"))
        self.assertIn("addendum", str(ctx.exception))

    def test_refuses_an_empty_correction(self) -> None:
        path = self._seed("T-0009", "inbox")
        before = path.read_text(encoding="utf-8")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_supersede_ask(self._args("T-0009", "   "))
        self.assertEqual(path.read_text(encoding="utf-8"), before,
                         "a refused supersede must not touch the file")

    def test_refuses_a_ticket_with_no_ask_section(self) -> None:
        path = self.root / "inbox" / "T-0010.md"
        path.write_text(
            "---\nid: T-0010\ncreated: 2026-05-02T01:30:00Z\nrequester: orchestrator\n"
            "target: orchestrator\nstate: inbox\nparent_ticket: null\ndepends_on: []\n"
            "labels: []\n---\n\n# No ask here\n\n## Context\n\nctx\n",
            encoding="utf-8",
        )
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_supersede_ask(self._args("T-0010", "correction"))
        self.assertIn("annotate", str(ctx.exception))

    def test_active_ticket_is_allowed(self) -> None:
        """The likeliest case: a worker is mid-arc on a stale instruction."""
        self._seed("T-0011", "active")
        self.assertEqual(ticket.cmd_supersede_ask(self._args("T-0011", "corrected")), 0)


class TestNearDuplicateWarning(unittest.TestCase):
    """T-2260: twice in one night two agents filed the same finding four minutes
    apart. Both times each had looked at the same surface and written up what
    they found rather than assuming the other would — the behaviour we want. The
    gap is that `file` had no awareness of what landed a moment ago."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for st in ticket.STATES:
            (self.root / st).mkdir(parents=True)
        self._p = patch.object(ticket, "TICKETS_ROOT", self.root)
        self._p.start()

    def tearDown(self) -> None:
        self._p.stop()
        self._tmp.cleanup()

    def _seed(self, tid: str, labels: list[str], created: str, state: str = "inbox") -> None:
        _write_ticket(self.root / state / f"{tid}.md", tid=tid, state=state,
                      labels=labels, created=created, title=f"Title of {tid}")

    NOW = "2026-08-02T12:00:00Z"

    def test_surfaces_a_recent_label_overlapping_neighbour(self) -> None:
        self._seed("T-0001", ["quantization", "echo", "onnx"], "2026-08-02T11:56:00Z")
        got = ticket._recent_neighbours(["quantization", "echo"], self.NOW)
        self.assertEqual([r[0] for r in got], ["T-0001"])
        self.assertEqual(got[0][2], ["echo", "quantization"])

    def test_ignores_a_neighbour_outside_the_window(self) -> None:
        self._seed("T-0002", ["quantization", "echo"], "2026-08-02T11:00:00Z")
        self.assertEqual(ticket._recent_neighbours(["quantization", "echo"], self.NOW), [])

    def test_one_shared_label_is_not_enough(self) -> None:
        """Sibling tickets split from one arc share a label constantly. A
        threshold of one would fire on the behaviour this is meant to protect."""
        self._seed("T-0003", ["echo", "unrelated"], "2026-08-02T11:56:00Z")
        self.assertEqual(ticket._recent_neighbours(["echo", "quantization"], self.NOW), [])

    def test_a_ticket_with_too_few_labels_scans_nothing(self) -> None:
        self._seed("T-0004", ["echo", "quantization"], "2026-08-02T11:56:00Z")
        self.assertEqual(ticket._recent_neighbours(["echo"], self.NOW), [])

    def test_excludes_the_ticket_just_filed(self) -> None:
        """Otherwise every filing warns about itself."""
        self._seed("T-0005", ["echo", "quantization"], self.NOW)
        self.assertEqual(
            ticket._recent_neighbours(["echo", "quantization"], self.NOW, exclude="T-0005"),
            [],
        )

    def test_scans_every_state_not_just_inbox(self) -> None:
        """A near-duplicate that someone already claimed is still a duplicate."""
        self._seed("T-0006", ["echo", "quantization"], "2026-08-02T11:56:00Z", state="active")
        self.assertEqual([r[0] for r in ticket._recent_neighbours(
            ["echo", "quantization"], self.NOW)], ["T-0006"])

    def test_newest_first(self) -> None:
        self._seed("T-0007", ["echo", "quantization"], "2026-08-02T11:50:00Z")
        self._seed("T-0008", ["echo", "quantization"], "2026-08-02T11:58:00Z")
        self.assertEqual([r[0] for r in ticket._recent_neighbours(
            ["echo", "quantization"], self.NOW)], ["T-0008", "T-0007"])

    def test_a_malformed_ticket_does_not_break_the_scan(self) -> None:
        """A scan failure must never cost a filing."""
        (self.root / "inbox" / "T-0009.md").write_text("not a ticket at all", encoding="utf-8")
        self._seed("T-0010", ["echo", "quantization"], "2026-08-02T11:56:00Z")
        self.assertEqual([r[0] for r in ticket._recent_neighbours(
            ["echo", "quantization"], self.NOW)], ["T-0010"])

    def test_warning_goes_to_stderr_and_returns_none(self) -> None:
        """It is a note, not a gate: no exception, no exit code, stderr only."""
        self._seed("T-0011", ["echo", "quantization"], "2026-08-02T11:56:00Z")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(ticket._warn_near_duplicates(["echo", "quantization"], self.NOW))
        self.assertIn("T-0011", err.getvalue())
        self.assertIn("Not a duplicate check", err.getvalue())


class TestAnnotateRecentOtherActorWarning(unittest.TestCase):
    """T-2260's 'one thing to check before building'. On annotate the target is
    already one specific ticket, so the question collapses from 'is this a
    similar ticket' (needs a label proxy) to 'did someone else write here
    recently' — a precise check with no false-positive class."""

    def _body(self, entries: list[str]) -> str:
        return "# T\n\n## Ask\n\na\n\n## Transition log\n\n" + "\n".join(entries) + "\n"

    def test_warns_on_another_actor_within_the_window(self) -> None:
        now = ticket._now_iso()
        body = self._body([f"- {now} [recovery] note; found the same thing"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ticket._warn_recent_other_annotators(body, "orchestrator")
        self.assertIn("recovery", err.getvalue())

    def test_silent_when_the_only_recent_actor_is_me(self) -> None:
        now = ticket._now_iso()
        body = self._body([f"- {now} [orchestrator] note; my own earlier line"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ticket._warn_recent_other_annotators(body, "orchestrator")
        self.assertEqual(err.getvalue(), "")

    def test_silent_on_an_old_entry_from_another_actor(self) -> None:
        body = self._body(["- 2020-01-01T00:00:00Z [recovery] note; ancient"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ticket._warn_recent_other_annotators(body, "orchestrator")
        self.assertEqual(err.getvalue(), "")

    def test_unparseable_log_lines_do_not_raise(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(
                ticket._warn_recent_other_annotators("garbage\n- not a log line\n", "orchestrator"))


class TestRecurrencePredictionWarning(unittest.TestCase):
    """T-2273: a finding recorded in a RESOLVED ticket's prose is on nobody's
    queue — a resolve note enters the audit chain and exits every queue in the
    same act.

    The class definition, from atlas's reviewer: **"silently skipped" and
    "loudly recorded where nobody will look again" produce the same outcome.**
    """

    # The literal sentence from tickets/done/T-2188.md:46 (Recovery, 2026-07-28).
    # Amendment H requires the instrument be shown firing on THIS, not on a
    # phrase invented to match the patterns. Two agents rediscovered the defect
    # from scratch within a day of it being written down correctly.
    T2188 = (
        "exocortex had NO ~/.claude/hooks directory at all, and deploy_hook.py does "
        "not create the destination dir, so the first attempt reported "
        "MISMATCH/unverified. It failed LOUDLY rather than claiming success — "
        "correct behaviour — but THE NEXT NEW-AGENT DEPLOY WILL HIT THE SAME THING."
    )

    def test_amendment_H_fires_on_the_real_T2188_sentence(self) -> None:
        hits = ticket._recurrence_predictions(self.T2188)
        self.assertTrue(hits, "the instrument does not fire on the case that motivated it")
        self.assertIn("SAME THING", hits[0].upper())

    def test_it_is_case_insensitive(self) -> None:
        self.assertTrue(ticket._recurrence_predictions("the next deploy will hit the same wall"))

    def test_ordinary_resolve_prose_does_not_fire(self) -> None:
        """A warning that fires on every resolve is a warning nobody reads."""
        for benign in (
            "Landed main abc1234 (PR #99). Suite 4917 passed / 0 failed.",
            "Measured before building: 76 messages, 76 distinct pids. Refuted.",
            "Remediated, not closed — closure needs an independent re-verification.",
        ):
            with self.subTest(benign=benign[:40]):
                self.assertEqual(ticket._recurrence_predictions(benign), [])

    def test_several_shapes_all_fire(self) -> None:
        for s in ("this will recur when the cache is cold",
                  "whoever does this next will hit the same thing",
                  "the same thing will happen on the next box",
                  "this will bite the next person who deploys"):
            with self.subTest(s=s):
                self.assertTrue(ticket._recurrence_predictions(s), s)

    def test_warning_goes_to_stderr_and_never_raises(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(ticket._warn_recurrence_prediction(self.T2188, "T-2188"))
        out = err.getvalue()
        self.assertIn("T-2188", out)
        self.assertIn("nobody's queue", out)

    def test_empty_and_none_notes_are_silent(self) -> None:
        for val in ("", None):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                ticket._warn_recurrence_prediction(val, "T-0001")
            self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
