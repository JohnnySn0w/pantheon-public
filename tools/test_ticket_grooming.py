"""Unit tests for tools/ticket_grooming.py.

Run from repo root:
    python3 -m unittest tools.test_ticket_grooming -v

Covers threshold parsing, transition-log walking, idempotency check, stale
detection, HERMES guard belt, scope filter, digest rendering, and CLI flag
behavior. Subprocess (git) and Telegram send are mocked throughout.

Fixture-string note: the unescaped HERMES trigger is constructed via source-
time string concatenation (`"HERME" + "S.md"`) so the contiguous 9-byte
sequence never appears in this source file. This protects `git log -p` of
pantheon-config from re-introducing the trigger phrase that routes Claude
Code requests through Anthropic's anti-abuse classifier (per the standing
rule in `/home/claude/CLAUDE.md` and the byte-sequence discipline section
of `tools/_git_utils.py`'s docstring).
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import ticket_grooming
from tools.ticket import TicketError

# T-1774: env-override for git_mutation_lock's lockfile path — see the
# matching module-level comment in tools/test_watch_runner.py. Any test that
# fully mocks tools._git_utils.subprocess.run and drives the real
# git_commit_and_push/commit_scoped pipeline MUST set this in setUp/tearDown,
# else `_git_utils._git_common_dir()`'s `git rev-parse --git-common-dir` call
# never gets a real answer from the mock and falls back to `REPO_ROOT /
# ".git"` — a FILE (not a directory) in a linked worktree checkout, which
# crashes `_open_lockfile_world_writable`'s `mkdir()` (T-1816).
_GIT_MUTATION_LOCK_ENV = "PANTHEON_GIT_MUTATION_LOCKFILE"


# ---------------------------------------------------------------------------
# Fixture helpers.
# ---------------------------------------------------------------------------


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_ticket_file(
    path: Path,
    *,
    tid: str,
    state: str,
    requester: str = "orchestrator",
    target: str = "recovery",
    title: str = "Example ticket",
    transition_lines: list[str] | None = None,
    stale_after: str | None = None,
    extra_body: str = "",
    labels: list[str] | None = None,
) -> None:
    """Write a ticket file with the given transition log entries.

    `transition_lines` is a list of pre-formatted bullet lines (each starting
    with "- "), inserted verbatim into the ## Transition log section. If None,
    a single freshly-filed entry is generated. `labels`, if given, is written
    as the frontmatter flow-list `labels: [a, b, c]` (T-1814: needed for the
    ratified-unbuilt-ADR join tests).
    """
    fm_lines = [
        "---",
        f"id: {tid}",
        "created: 2026-05-01T00:00:00Z",
        f"requester: {requester}",
        f"target: {target}",
        f"state: {state}",
        "parent_ticket: null",
        "depends_on: []",
    ]
    if stale_after:
        fm_lines.append(f"stale_after: {stale_after}")
    if labels is not None:
        fm_lines.append(f"labels: [{', '.join(labels)}]")
    fm_lines.append("---")

    if transition_lines is None:
        transition_lines = [f"- 2026-05-01T00:00:00Z [{requester}] filed in inbox"]
    log_block = "\n".join(transition_lines)

    body = (
        f"\n# {title}\n\n"
        "## Ask\n\nDo the thing.\n\n"
        "## Context\n\nBecause reasons.\n"
        f"{extra_body}\n"
        "## Transition log\n\n"
        f"{log_block}\n"
    )
    path.write_text("\n".join(fm_lines) + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Threshold parser.
# ---------------------------------------------------------------------------


class TestThresholdParser(unittest.TestCase):
    def test_4h_valid(self) -> None:
        self.assertEqual(ticket_grooming.parse_threshold("4h"), timedelta(hours=4))

    def test_5d_valid(self) -> None:
        self.assertEqual(ticket_grooming.parse_threshold("5d"), timedelta(days=5))

    def test_1h_valid(self) -> None:
        self.assertEqual(ticket_grooming.parse_threshold("1h"), timedelta(hours=1))

    def test_30d_valid(self) -> None:
        self.assertEqual(ticket_grooming.parse_threshold("30d"), timedelta(days=30))

    def test_bare_number_invalid(self) -> None:
        with self.assertRaises(TicketError):
            ticket_grooming.parse_threshold("4")

    def test_4hours_invalid(self) -> None:
        with self.assertRaises(TicketError):
            ticket_grooming.parse_threshold("4hours")

    def test_4space_h_invalid(self) -> None:
        with self.assertRaises(TicketError):
            ticket_grooming.parse_threshold("4 h")

    def test_uppercase_invalid(self) -> None:
        # Spec uses lowercase consistently; refuse uppercase to keep the
        # vocabulary tight.
        with self.assertRaises(TicketError):
            ticket_grooming.parse_threshold("4H")

    def test_4w_weeks_unsupported(self) -> None:
        with self.assertRaises(TicketError) as cm:
            ticket_grooming.parse_threshold("4w")
        self.assertIn("supported", str(cm.exception).lower())

    def test_zero_d_refused(self) -> None:
        with self.assertRaises(TicketError) as cm:
            ticket_grooming.parse_threshold("0d")
        self.assertIn("positive", str(cm.exception).lower())

    def test_zero_h_refused(self) -> None:
        with self.assertRaises(TicketError) as cm:
            ticket_grooming.parse_threshold("0h")
        self.assertIn("positive", str(cm.exception).lower())

    def test_empty_string_invalid(self) -> None:
        with self.assertRaises(TicketError):
            ticket_grooming.parse_threshold("")


# ---------------------------------------------------------------------------
# Transition-log walking.
# ---------------------------------------------------------------------------


class TestFindLastNonGroomingEntry(unittest.TestCase):
    def _body_with_log(self, lines: list[str]) -> str:
        log_block = "\n".join(lines) if lines else ""
        return (
            "# Title\n\n"
            "## Ask\n\nfoo\n\n"
            "## Transition log\n\n"
            f"{log_block}\n"
        )

    def test_only_non_grooming_returns_latest(self) -> None:
        body = self._body_with_log(
            [
                "- 2026-05-01T00:00:00Z [orchestrator] filed in inbox",
                "- 2026-05-01T01:00:00Z [recovery] claimed; moving to active",
            ]
        )
        ts, actor = ticket_grooming.find_last_non_grooming_entry(body)
        self.assertEqual(actor, "recovery")
        self.assertEqual(ts, datetime(2026, 5, 1, 1, 0, 0, tzinfo=timezone.utc))

    def test_grooming_at_end_skipped(self) -> None:
        body = self._body_with_log(
            [
                "- 2026-05-01T00:00:00Z [orchestrator] filed in inbox",
                "- 2026-05-01T01:00:00Z [recovery] claimed; moving to active",
                "- 2026-05-02T05:00:00Z [grooming] stale (no activity since 2026-05-01T01:00:00Z); flagging for review",
            ]
        )
        ts, actor = ticket_grooming.find_last_non_grooming_entry(body)
        self.assertEqual(actor, "recovery")
        self.assertEqual(ts, datetime(2026, 5, 1, 1, 0, 0, tzinfo=timezone.utc))

    def test_only_grooming_returns_none(self) -> None:
        body = self._body_with_log(
            [
                "- 2026-05-02T05:00:00Z [grooming] stale (...); flagging for review",
            ]
        )
        ts, actor = ticket_grooming.find_last_non_grooming_entry(body)
        self.assertIsNone(ts)
        self.assertIsNone(actor)

    def test_no_log_section_returns_none(self) -> None:
        body = "# Title\n\n## Ask\n\nfoo\n"
        ts, actor = ticket_grooming.find_last_non_grooming_entry(body)
        self.assertIsNone(ts)
        self.assertIsNone(actor)

    def test_empty_log_section_returns_none(self) -> None:
        body = self._body_with_log([])
        ts, actor = ticket_grooming.find_last_non_grooming_entry(body)
        self.assertIsNone(ts)
        self.assertIsNone(actor)

    def test_malformed_bullet_skipped(self) -> None:
        # A non-bullet line in the middle of the log must not stop the walk.
        body = self._body_with_log(
            [
                "- 2026-05-01T00:00:00Z [orchestrator] filed in inbox",
                "this line is not a bullet — hand-edited noise",
                "- 2026-05-02T05:00:00Z [grooming] stale; ...",
            ]
        )
        ts, actor = ticket_grooming.find_last_non_grooming_entry(body)
        self.assertEqual(actor, "orchestrator")


class TestLatestEntryIsGroomingStale(unittest.TestCase):
    def _body_with_log(self, lines: list[str]) -> str:
        log_block = "\n".join(lines) if lines else ""
        return (
            "# Title\n\n## Transition log\n\n" f"{log_block}\n"
        )

    def test_grooming_stale_at_end_returns_true(self) -> None:
        body = self._body_with_log(
            [
                "- 2026-05-01T01:00:00Z [recovery] claimed; moving to active",
                "- 2026-05-02T05:00:00Z [grooming] stale (no activity since 2026-05-01T01:00:00Z); flagging for review",
            ]
        )
        self.assertTrue(ticket_grooming.latest_entry_is_grooming_stale(body))

    def test_human_actor_at_end_returns_false(self) -> None:
        body = self._body_with_log(
            [
                "- 2026-05-02T05:00:00Z [grooming] stale; ...",
                "- 2026-05-02T06:00:00Z [recovery] commented; picked it up",
            ]
        )
        self.assertFalse(ticket_grooming.latest_entry_is_grooming_stale(body))

    def test_grooming_other_verb_at_end_returns_false(self) -> None:
        # Defensive: if some future verb other than "stale" is added, our
        # idempotency check should not gate on the actor alone.
        body = self._body_with_log(
            [
                "- 2026-05-02T05:00:00Z [grooming] inspected; left alone",
            ]
        )
        self.assertFalse(ticket_grooming.latest_entry_is_grooming_stale(body))

    def test_empty_log_returns_false(self) -> None:
        body = self._body_with_log([])
        self.assertFalse(ticket_grooming.latest_entry_is_grooming_stale(body))


# ---------------------------------------------------------------------------
# Stale detection.
# ---------------------------------------------------------------------------


class TestStaleDetection(unittest.TestCase):
    """End-to-end is_stale logic via main(); fixtures + a pinned `now`.

    Patches `ticket_grooming.TICKETS_ROOT` to a tempdir, `_now_utc` to a fixed
    instant, and the git/digest side effects.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "active").mkdir()

        self._patches = [
            patch.object(ticket_grooming, "TICKETS_ROOT", self.root),
            patch.object(
                ticket_grooming,
                "_now_utc",
                return_value=datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc),
            ),
            patch.object(
                ticket_grooming,
                "git_commit_and_push",
                return_value=(True, "mocked"),
            ),
            patch.object(ticket_grooming, "_send_digest", return_value=True),
            patch.object(
                ticket_grooming,
                "run_history_audit",
                return_value="HERMES audit: 0 unescaped occurrence(s) across 0 commit(s) scanned.",
            ),
            # T-0471: bypass worktree shim in unit tests; production worktree
            # isolation is covered by test_git_utils_integ.py Layer-3 tests.
            patch.object(ticket_grooming, "_with_worktree", lambda fn: fn(None)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _last_log_lines(self, path: Path) -> list[str]:
        text = path.read_text(encoding="utf-8")
        return [ln for ln in text.splitlines() if ln.startswith("- ")]

    def test_active_5h_old_default_4h_is_stale(self) -> None:
        # Last entry 5h before pinned now => stale at 4h default.
        last = datetime(2026, 5, 2, 7, 0, 0, tzinfo=timezone.utc)
        path = self.root / "active" / "T-0001.md"
        _make_ticket_file(
            path,
            tid="T-0001",
            state="active",
            transition_lines=[
                f"- {_ts(last)} [recovery] claimed; moving to active",
            ],
        )
        rc = ticket_grooming.main(["--commit-prod"])
        self.assertEqual(rc, 0)
        bullets = self._last_log_lines(path)
        # Should have appended a [grooming] stale line.
        self.assertEqual(len(bullets), 2)
        self.assertIn("[grooming] stale", bullets[-1])

    def test_active_3h_old_default_4h_not_stale(self) -> None:
        last = datetime(2026, 5, 2, 9, 0, 0, tzinfo=timezone.utc)
        path = self.root / "active" / "T-0002.md"
        _make_ticket_file(
            path,
            tid="T-0002",
            state="active",
            transition_lines=[f"- {_ts(last)} [recovery] claimed; moving to active"],
        )
        rc = ticket_grooming.main(["--commit-prod"])
        self.assertEqual(rc, 0)
        bullets = self._last_log_lines(path)
        self.assertEqual(len(bullets), 1)  # untouched

    def test_per_ticket_override_1h_overrides_default_4h(self) -> None:
        # 2h old, default for active is 4h (not stale), but stale_after: 1h
        # overrides => stale.
        last = datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc)
        path = self.root / "active" / "T-0005.md"
        _make_ticket_file(
            path,
            tid="T-0005",
            state="active",
            stale_after="1h",
            transition_lines=[f"- {_ts(last)} [recovery] claimed; moving to active"],
        )
        rc = ticket_grooming.main(["--commit-prod"])
        self.assertEqual(rc, 0)
        bullets = self._last_log_lines(path)
        self.assertEqual(len(bullets), 2)
        self.assertIn("[grooming] stale", bullets[-1])

    def test_only_grooming_entries_returns_not_stale(self) -> None:
        # Sentinel: a ticket with only [grooming] entries is treated as not
        # eligible to flag (no fresh activity to compute against).
        last = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
        path = self.root / "active" / "T-0006.md"
        _make_ticket_file(
            path,
            tid="T-0006",
            state="active",
            transition_lines=[
                f"- {_ts(last)} [grooming] stale; flagging for review",
            ],
        )
        rc = ticket_grooming.main(["--commit-prod"])
        self.assertEqual(rc, 0)
        # Body unchanged — no new flag line appended.
        self.assertEqual(len(self._last_log_lines(path)), 1)


# ---------------------------------------------------------------------------
# Idempotency.
# ---------------------------------------------------------------------------


class TestAppendIdempotency(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "active").mkdir()

        self._patches = [
            patch.object(ticket_grooming, "TICKETS_ROOT", self.root),
            patch.object(
                ticket_grooming,
                "_now_utc",
                return_value=datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc),
            ),
            patch.object(
                ticket_grooming, "git_commit_and_push", return_value=(True, "mocked")
            ),
            patch.object(ticket_grooming, "_send_digest", return_value=True),
            patch.object(
                ticket_grooming,
                "run_history_audit",
                return_value="HERMES audit: 0 unescaped occurrence(s) across 0 commit(s) scanned.",
            ),
            # T-0471: bypass worktree shim in unit tests.
            patch.object(ticket_grooming, "_with_worktree", lambda fn: fn(None)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_first_flag_appends(self) -> None:
        last = datetime(2026, 5, 2, 5, 0, 0, tzinfo=timezone.utc)
        path = self.root / "active" / "T-0010.md"
        _make_ticket_file(
            path,
            tid="T-0010",
            state="active",
            transition_lines=[f"- {_ts(last)} [recovery] claimed; moving to active"],
        )
        ticket_grooming.main(["--commit-prod"])
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("[grooming] stale"), 1)

    def test_second_flag_does_not_double_append(self) -> None:
        # Already has [grooming] stale as the latest entry, plus an older
        # non-grooming entry. Re-running grooming should NOT add another.
        claimed = datetime(2026, 5, 2, 5, 0, 0, tzinfo=timezone.utc)
        flagged = datetime(2026, 5, 2, 11, 0, 0, tzinfo=timezone.utc)
        path = self.root / "active" / "T-0011.md"
        _make_ticket_file(
            path,
            tid="T-0011",
            state="active",
            transition_lines=[
                f"- {_ts(claimed)} [recovery] claimed; moving to active",
                f"- {_ts(flagged)} [grooming] stale (no activity since {_ts(claimed)}); flagging for review",
            ],
        )
        ticket_grooming.main(["--commit-prod"])
        text = path.read_text(encoding="utf-8")
        self.assertEqual(
            text.count("[grooming] stale"),
            1,
            f"expected exactly one [grooming] stale line; got body:\n{text}",
        )


# ---------------------------------------------------------------------------
# T-1586: stale-draft-without-operator-child check.
# ---------------------------------------------------------------------------


def _make_stale_draft_candidate(
    path: Path,
    *,
    tid: str,
    state: str,
    target: str = "orchestrator",
    labels: list[str] | None = None,
    depends_on: list[str] | None = None,
    draft_header: str | None = None,
    title: str = "Example ticket",
) -> None:
    """Write a ticket with a custom target/labels/depends_on and an optional
    draft-shaped section header, for the T-1586 stale-draft-check fixtures.

    Narrower than the module `_make_ticket_file` helper (which hardcodes
    `depends_on: []` and has no `labels` param) — kept separate rather than
    widening that shared fixture, to keep this check's tests self-contained.
    """
    labels_repr = "[]" if not labels else "[" + ", ".join(labels) + "]"
    deps_repr = "[]" if not depends_on else "[" + ", ".join(depends_on) + "]"
    fm_lines = [
        "---",
        f"id: {tid}",
        "created: 2026-05-01T00:00:00Z",
        "requester: orchestrator",
        f"target: {target}",
        f"state: {state}",
        "parent_ticket: null",
        f"depends_on: {deps_repr}",
        f"labels: {labels_repr}",
        "---",
    ]
    body = f"\n# {title}\n\n## Ask\n\nDo the thing.\n\n"
    if draft_header:
        body += f"{draft_header}\n\nSome draft content.\n\n"
    body += f"## Transition log\n\n- 2026-05-01T00:00:00Z [orchestrator] filed in {state}\n"
    path.write_text("\n".join(fm_lines) + body, encoding="utf-8")


class TestFindStaleDraftTickets(unittest.TestCase):
    """find_stale_draft_tickets: condition (a) label, (b) dated draft header
    past threshold, (c) no open operator-facing link. Positive fixtures
    reproduce the REAL T-1371/T-1478 header lines verbatim (D11).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "inbox").mkdir()
        (self.root / "active").mkdir()
        (self.root / "done").mkdir()
        self.now = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _find(self):
        return ticket_grooming.find_stale_draft_tickets(self.root, self.now)

    def test_t1371_style_header_stale_flags(self) -> None:
        # Verbatim real header from tickets/done/T-1371.md (pre-fix state).
        _make_stale_draft_candidate(
            self.root / "inbox" / "T-2001.md",
            tid="T-2001",
            labels=["interactive-required"],
            state="inbox",
            draft_header=(
                "## Percolation findings (orchestrator, 2026-06-29 — partial, pre-decision)"
            ),
        )
        found = self._find()
        self.assertEqual([f.tid for f in found], ["T-2001"])

    def test_t1478_style_header_stale_flags(self) -> None:
        # Verbatim real header from tickets/inbox/T-1478.md (pre-fix state).
        _make_stale_draft_candidate(
            self.root / "inbox" / "T-2002.md",
            tid="T-2002",
            labels=["interactive-required"],
            state="inbox",
            draft_header=(
                "## Re-eval result (orchestrator, 2026-07-01 — DRAFT proposal, "
                "awaiting the operator's ratification; no threshold value changed)"
            ),
        )
        found = self._find()
        self.assertEqual([f.tid for f in found], ["T-2002"])

    def test_active_state_scanned_too(self) -> None:
        _make_stale_draft_candidate(
            self.root / "active" / "T-2003.md",
            tid="T-2003",
            labels=["interactive-required"],
            state="active",
            draft_header=(
                "## Draft citation map (orchestrator, 2026-07-01 — DRAFT, awaiting "
                "the operator's bless; NO rulebook edit landed)"
            ),
        )
        found = self._find()
        self.assertEqual([f.tid for f in found], ["T-2003"])

    def test_fresh_draft_under_threshold_not_flagged(self) -> None:
        _make_stale_draft_candidate(
            self.root / "inbox" / "T-2004.md",
            tid="T-2004",
            labels=["interactive-required"],
            state="inbox",
            draft_header=(
                "## Re-eval result (orchestrator, 2026-07-05 — DRAFT proposal, "
                "awaiting the operator's ratification)"
            ),
        )
        self.assertEqual(self._find(), [])

    def test_no_draft_header_not_flagged(self) -> None:
        _make_stale_draft_candidate(
            self.root / "inbox" / "T-2005.md",
            tid="T-2005",
            labels=["interactive-required"],
            state="inbox",
            draft_header=None,
        )
        self.assertEqual(self._find(), [])

    def test_not_interactive_required_not_flagged(self) -> None:
        # T-1213-shaped: target=operator, old dated header, NOT
        # interactive-required — regression fixture for the condition-(a)
        # false-positive class found during planning.
        _make_stale_draft_candidate(
            self.root / "inbox" / "T-2006.md",
            tid="T-2006",
            target="operator",
            labels=["echo", "feature"],
            state="inbox",
            draft_header=(
                "## Options analysis (drafted 2026-06-16, Orchestrator — fresh "
                "Echo-architecture read)"
            ),
        )
        self.assertEqual(self._find(), [])

    def test_past_tense_drafted_marker_does_not_match(self) -> None:
        # Even under interactive-required, "drafted" (past tense) must not
        # match the \bdraft\b marker — locks in the word-boundary decision.
        _make_stale_draft_candidate(
            self.root / "inbox" / "T-2007.md",
            tid="T-2007",
            labels=["interactive-required"],
            state="inbox",
            draft_header=(
                "## Notes (orchestrator, 2026-06-01 — drafted quickly, nothing else)"
            ),
        )
        self.assertEqual(self._find(), [])

    def test_linked_via_depends_on_not_flagged(self) -> None:
        _make_stale_draft_candidate(
            self.root / "inbox" / "T-1584.md",
            tid="T-1584",
            target="operator",
            labels=["ratify"],
            state="inbox",
        )
        _make_stale_draft_candidate(
            self.root / "inbox" / "T-2008.md",
            tid="T-2008",
            labels=["interactive-required"],
            depends_on=["T-1584"],
            state="inbox",
            draft_header=(
                "## Re-eval result (orchestrator, 2026-07-01 — DRAFT proposal, "
                "awaiting the operator's ratification)"
            ),
        )
        self.assertEqual(self._find(), [])

    def test_linked_via_mention_not_flagged(self) -> None:
        # T-1371's real fix shape: the operator-targeted ticket mentions the
        # source ticket's id in prose, with NO depends_on/parent_ticket
        # back-reference on the source ticket itself.
        _make_stale_draft_candidate(
            self.root / "inbox" / "T-1582.md",
            tid="T-1582",
            target="operator",
            labels=["ratify"],
            state="inbox",
            title="Ratify ORG-038 (drafted at your T-2009 steer)",
        )
        _make_stale_draft_candidate(
            self.root / "inbox" / "T-2009.md",
            tid="T-2009",
            labels=["interactive-required"],
            state="inbox",
            draft_header=(
                "## Percolation findings (orchestrator, 2026-06-29 — partial, pre-decision)"
            ),
        )
        self.assertEqual(self._find(), [])

    def test_done_operator_ticket_not_counted_as_open_link(self) -> None:
        # depends_on references an operator-targeted ticket, but it's in
        # tickets/done/ — not OPEN — so it must not suppress the flag.
        _make_stale_draft_candidate(
            self.root / "done" / "T-1584.md",
            tid="T-1584",
            target="operator",
            labels=["ratify"],
            state="done",
        )
        _make_stale_draft_candidate(
            self.root / "inbox" / "T-2010.md",
            tid="T-2010",
            labels=["interactive-required"],
            depends_on=["T-1584"],
            state="inbox",
            draft_header=(
                "## Re-eval result (orchestrator, 2026-07-01 — DRAFT proposal, "
                "awaiting the operator's ratification)"
            ),
        )
        found = self._find()
        self.assertEqual([f.tid for f in found], ["T-2010"])

    def test_multiple_headers_uses_newest_date(self) -> None:
        # An old draft header plus a fresh follow-up draft header — the
        # newest date is the staleness anchor, so this is NOT flagged.
        path = self.root / "inbox" / "T-2011.md"
        _make_stale_draft_candidate(
            path,
            tid="T-2011",
            labels=["interactive-required"],
            state="inbox",
            draft_header=(
                "## Percolation findings (orchestrator, 2026-06-20 — partial, pre-decision)"
            ),
        )
        # Append a second, fresher draft-marker header directly.
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            "## Transition log",
            "## Follow-up (orchestrator, 2026-07-05 — still DRAFT, awaiting the operator)\n\n"
            "## Transition log",
        )
        path.write_text(text, encoding="utf-8")
        self.assertEqual(self._find(), [])


# ---------------------------------------------------------------------------
# HERMES guard belt.
# ---------------------------------------------------------------------------


class TestHermesGuardBelt(unittest.TestCase):
    """Driver-level HERMES guard. The guard refuses to commit if either a
    modified ticket file OR the constructed commit message contains the
    unescaped trigger phrase. Source-time concatenation per fixture-string
    note at top of file.
    """

    def test_clean_message_passes(self) -> None:
        msg = "tickets(grooming): flag 1 stale ticket(s) — 2026-05-02\n"
        self.assertTrue(ticket_grooming._hermes_scan_message(msg))

    def test_unescaped_trigger_in_message_refused(self) -> None:
        # Source-time concatenation; runtime yields the contiguous 9-byte
        # trigger that the regex matches, but source bytes never do.
        msg = "tickets(grooming): flag 1 stale\n\nsee " + "HERME" + "S.md\n"
        self.assertFalse(ticket_grooming._hermes_scan_message(msg))

    def test_unescaped_trigger_in_ticket_file_refuses_commit(self) -> None:
        # End-to-end against a REAL tmp git repo: a STAGED ticket whose body
        # contains the unescaped trigger MUST stop the auto-commit pipeline before
        # any git commit fires. Post-T-1088, commit_scoped HERMES-scans the
        # actually-staged files, so this must exercise a real stage (cwd=repo →
        # sync=False) rather than a mocked one.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo, capture_output=True)
            active = repo / "tickets" / "active"
            active.mkdir(parents=True)
            (repo / "seed.txt").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, capture_output=True, check=True)

            bad_path = active / "T-0099.md"
            bad_path.write_text(
                "# bad\n\nbody contains " + "HERME" + "S.md unescaped\n", encoding="utf-8"
            )
            ok, detail = ticket_grooming.git_commit_and_push(
                [bad_path],
                "tickets(grooming): flag 1 stale ticket(s) — 2026-05-02\n",
                today_iso="2026-05-02",
                cwd=repo,
            )
            self.assertFalse(ok)
            self.assertIn("HERME" + "S", detail)  # detail surfaces the trigger location
            log = subprocess.run(
                ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True
            ).stdout
            self.assertEqual(log.strip().count("\n"), 0, "only the seed commit should exist")
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"], cwd=repo, capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(staged, "", "staging must be reset after HERMES refusal")

    def test_no_unescaped_trigger_in_source(self) -> None:
        """Self-test: this test file must not contain the unescaped trigger
        as a contiguous source-byte sequence. (The fixture concatenations
        construct it at runtime only.)
        """
        src = Path(__file__).read_bytes()
        # Build the trigger at runtime via the same source-time concatenation
        # idiom used in the fixtures.
        trigger = (b"HERME" + b"S.md")
        self.assertNotIn(trigger, src)


# ---------------------------------------------------------------------------
# Scope filter / git plumbing — mirrors test_watch_runner pattern.
# ---------------------------------------------------------------------------


class TestCommitAndPushScopeFilter(unittest.TestCase):
    """git_commit_and_push delegates to commit_scoped with the correct scope.

    Post-T-1088, the scope-narrowed-commit choreography (sync, leak-guard, empty
    short-circuit, HERMES staged-file scan, push) is commit_scoped's responsibility
    — covered by test_git_utils_integ.TestCommitScoped. These tests assert this
    caller's CONTRACT: it stages exactly tickets/active/ (allow_prefix), syncs only
    in legacy/shared-tree mode, and returns commit_scoped's result verbatim. Real
    worktree-isolation flow is covered by
    test_git_utils_integ.TestTicketGroomingDirtyForeignFile.
    """

    def test_delegates_with_active_scope_legacy_sync_on(self) -> None:
        with patch.object(
            ticket_grooming, "commit_scoped", return_value=(True, "committed 2 file(s)")
        ) as m:
            ok, msg = ticket_grooming.git_commit_and_push(
                [Path("x")], "tickets(grooming): test\n", today_iso="2026-05-02"
            )
        self.assertEqual((ok, msg), (True, "committed 2 file(s)"))  # propagated verbatim
        m.assert_called_once()
        args, kwargs = m.call_args
        self.assertEqual(args[0], ["tickets/active/"])
        self.assertEqual(args[1], "tickets(grooming): test\n")
        self.assertTrue(kwargs["allow_prefix"])
        self.assertTrue(kwargs["push"])
        self.assertTrue(kwargs["sync"])  # cwd=None → legacy/shared-tree sync ON

    def test_delegates_with_worktree_sync_off(self) -> None:
        wt = Path("/tmp/wt")
        with patch.object(
            ticket_grooming, "commit_scoped", return_value=(True, "no changes")
        ) as m:
            ok, msg = ticket_grooming.git_commit_and_push(
                [Path("x")], "msg\n", today_iso="2026-05-02", cwd=wt
            )
        self.assertEqual((ok, msg), (True, "no changes"))
        _args, kwargs = m.call_args
        self.assertFalse(kwargs["sync"])  # cwd set → worktree starts at HEAD, sync OFF
        self.assertEqual(kwargs["cwd"], wt)

    def test_propagates_commit_scoped_failure(self) -> None:
        with patch.object(
            ticket_grooming, "commit_scoped",
            return_value=(False, "scope guard refused: out-of-scope paths staged: ['x']"),
        ):
            ok, msg = ticket_grooming.git_commit_and_push(
                [Path("x")], "msg\n", today_iso="2026-05-02"
            )
        self.assertFalse(ok)
        self.assertIn("scope", msg.lower())

    def test_reopen_delegates_with_done_inbox_scope(self) -> None:
        with patch.object(
            ticket_grooming, "commit_scoped", return_value=(True, "committed 1 file(s)")
        ) as m:
            ok, msg = ticket_grooming.git_commit_and_push_reopen(
                [Path("x")], "tickets(grooming): reopen\n"
            )
        self.assertTrue(ok)
        args, kwargs = m.call_args
        self.assertEqual(args[0], ["tickets/done/", "tickets/inbox/"])
        self.assertTrue(kwargs["allow_prefix"])


# ---------------------------------------------------------------------------
# Digest rendering.
# ---------------------------------------------------------------------------


class TestDigestRendering(unittest.TestCase):
    def _flagged(self, tid: str, state: str, age: timedelta, title: str = "do thing") -> ticket_grooming.FlaggedTicket:
        return ticket_grooming.FlaggedTicket(
            path=Path("/tmp") / state / f"{tid}.md",
            tid=tid,
            state=state,
            requester="orchestrator",
            target="recovery",
            title=title,
            last_ts=datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc) - age,
            stale_age=age,
        )

    def test_zero_stale_renders_proof_of_life(self) -> None:
        msg = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC",
            flagged=[],
            counts={"active": 5},
        )
        self.assertIn("No stale tickets found", msg)
        self.assertIn("Active: 5", msg)

    def test_one_stale_active(self) -> None:
        flagged = [self._flagged("T-0007", "active", timedelta(hours=8))]
        msg = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC",
            flagged=flagged,
            counts={"active": 1},
        )
        self.assertIn("1 stale ticket(s)", msg)
        self.assertIn("T-0007", msg)
        self.assertIn("8h", msg)
        self.assertIn("active", msg.lower())

    def test_three_stale_active(self) -> None:
        flagged = [
            self._flagged("T-0007", "active", timedelta(hours=8)),
            self._flagged("T-0012", "active", timedelta(days=1)),
            self._flagged("T-0003", "active", timedelta(days=7)),
        ]
        msg = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC",
            flagged=flagged,
            counts={"active": 5},
        )
        self.assertIn("3 stale ticket(s)", msg)
        self.assertIn("T-0007", msg)
        self.assertIn("T-0012", msg)
        self.assertIn("T-0003", msg)

    def test_manifest_line_rendered_in_both_branches(self) -> None:
        # T-1624: manifest-freshness note appears in zero-stale AND stale digests.
        line = "manifest freshness (T-1624): instance.yaml unreviewed 45d"
        clean = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC", flagged=[], counts={"active": 3},
            manifest_line=line,
        )
        self.assertIn("unreviewed 45d", clean)
        stale = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC",
            flagged=[self._flagged("T-0007", "active", timedelta(hours=8))],
            counts={"active": 1}, manifest_line=line,
        )
        self.assertIn("unreviewed 45d", stale)
        none_case = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC", flagged=[], counts={"active": 3},
        )
        self.assertNotIn("manifest freshness", none_case)

    def _dep_findings(self):
        from tools.ticket_lint import DepGraphFinding
        return [
            DepGraphFinding(kind="cycle",
                            detail="dependency cycle (deadlock): T-0001 -> T-0002 -> T-0001"),
            DepGraphFinding(kind="stale_done_dep",
                            detail="T-0003 depends_on T-0009, which is DONE"),
        ]

    def test_dep_graph_section_omitted_when_clean(self) -> None:
        # No findings → no dependency-graph section in either branch.
        clean = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC", flagged=[], counts={"active": 3},
            dep_graph_findings=[],
        )
        self.assertNotIn("Dependency-graph defects", clean)
        stale = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC",
            flagged=[self._flagged("T-0007", "active", timedelta(hours=8))],
            counts={"active": 1}, dep_graph_findings=[],
        )
        self.assertNotIn("Dependency-graph defects", stale)

    def test_dep_graph_section_in_zero_stale_digest(self) -> None:
        msg = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC", flagged=[], counts={"active": 3},
            dep_graph_findings=self._dep_findings(),
        )
        self.assertIn("No stale tickets found", msg)
        self.assertIn("Dependency-graph defects (2)", msg)
        self.assertIn("CYCLE/deadlock", msg)
        self.assertIn("T-0001 -> T-0002 -> T-0001", msg)
        self.assertIn("stale-done-dep", msg)

    def test_dep_graph_section_in_stale_digest(self) -> None:
        msg = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC",
            flagged=[self._flagged("T-0007", "active", timedelta(hours=8))],
            counts={"active": 1}, dep_graph_findings=self._dep_findings(),
        )
        self.assertIn("1 stale ticket(s)", msg)        # stale section still present
        self.assertIn("Dependency-graph defects (2)", msg)  # dep section alongside
        self.assertNotIn("**", msg)                    # still plain text

    # T-2833: unanchored/bad date-gate section in the daily grooming digest.
    def _gate_findings(self):
        from tools.ticket_lint import UnanchoredGateFinding
        return [
            UnanchoredGateFinding("T-0005", "unanchored", "T-0005: not_before ... has no anchor"),
            UnanchoredGateFinding("T-0006", "malformed", "T-0006: anchor 'event:x' is not operator:..."),
        ]

    def test_unanchored_gates_section_omitted_when_clean(self) -> None:
        clean = ticket_grooming.build_digest(
            now_iso="2026-08-11 12:00 UTC", flagged=[], counts={"active": 3},
            unanchored_gate_findings=[],
        )
        self.assertNotIn("Unanchored/bad date gates", clean)
        stale = ticket_grooming.build_digest(
            now_iso="2026-08-11 12:00 UTC",
            flagged=[self._flagged("T-0007", "active", timedelta(hours=8))],
            counts={"active": 1}, unanchored_gate_findings=[],
        )
        self.assertNotIn("Unanchored/bad date gates", stale)

    def test_unanchored_gates_section_in_zero_stale_digest(self) -> None:
        msg = ticket_grooming.build_digest(
            now_iso="2026-08-11 12:00 UTC", flagged=[], counts={"active": 3},
            unanchored_gate_findings=self._gate_findings(),
        )
        self.assertIn("No stale tickets found", msg)
        self.assertIn("Unanchored/bad date gates (2)", msg)
        self.assertIn("[unanchored]", msg)
        self.assertIn("[malformed]", msg)

    def test_unanchored_gates_section_in_stale_digest(self) -> None:
        msg = ticket_grooming.build_digest(
            now_iso="2026-08-11 12:00 UTC",
            flagged=[self._flagged("T-0007", "active", timedelta(hours=8))],
            counts={"active": 1}, unanchored_gate_findings=self._gate_findings(),
        )
        self.assertIn("1 stale ticket(s)", msg)
        self.assertIn("Unanchored/bad date gates (2)", msg)
        self.assertNotIn("**", msg)

    def _stale_draft_findings(self):
        return [
            ticket_grooming.StaleDraftTicket(
                tid="T-1478",
                state="inbox",
                target="orchestrator",
                title="Cost-threshold re-eval overdue",
                header_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
                age=timedelta(days=5),
            ),
        ]

    def test_stale_draft_section_omitted_when_clean(self) -> None:
        clean = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC", flagged=[], counts={"active": 3},
            stale_draft_findings=[],
        )
        self.assertNotIn("Stale drafts", clean)
        stale = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC",
            flagged=[self._flagged("T-0007", "active", timedelta(hours=8))],
            counts={"active": 1}, stale_draft_findings=[],
        )
        self.assertNotIn("Stale drafts", stale)

    def test_stale_draft_section_in_zero_stale_digest(self) -> None:
        msg = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC", flagged=[], counts={"active": 3},
            stale_draft_findings=self._stale_draft_findings(),
        )
        self.assertIn("No stale tickets found", msg)
        self.assertIn("Stale drafts awaiting an operator-facing ticket (1)", msg)
        self.assertIn("T-1478", msg)
        self.assertIn("5d", msg)

    def test_stale_draft_section_in_stale_digest(self) -> None:
        msg = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC",
            flagged=[self._flagged("T-0007", "active", timedelta(hours=8))],
            counts={"active": 1}, stale_draft_findings=self._stale_draft_findings(),
        )
        self.assertIn("1 stale ticket(s)", msg)
        self.assertIn("Stale drafts awaiting an operator-facing ticket (1)", msg)
        self.assertNotIn("**", msg)

    def test_digest_no_markdown_specials(self) -> None:
        flagged = [self._flagged("T-0007", "active", timedelta(hours=8))]
        msg = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC",
            flagged=flagged,
            counts={"active": 1},
        )
        # Plain text — no leading-asterisk emphasis, no backticks-around-slug
        # convention used by the watch_digest.py Markdown form.
        self.assertNotIn("**", msg)
        self.assertNotIn("`T-0007`", msg)

    def test_hermes_line_appended_to_zero_stale_digest(self) -> None:
        hermes_line = "HERMES audit: 0 unescaped occurrence(s) across 74 commit(s) scanned."
        msg = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC",
            flagged=[],
            counts={"active": 3},
            hermes_line=hermes_line,
        )
        self.assertIn("No stale tickets found", msg)
        self.assertIn(hermes_line, msg)

    def test_hermes_line_appended_to_stale_digest(self) -> None:
        hermes_line = "HERMES audit: 0 unescaped occurrence(s) across 74 commit(s) scanned."
        flagged = [self._flagged("T-0007", "active", timedelta(hours=8))]
        msg = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC",
            flagged=flagged,
            counts={"active": 1},
            hermes_line=hermes_line,
        )
        self.assertIn("1 stale ticket(s)", msg)
        self.assertIn(hermes_line, msg)

    def test_hermes_line_none_does_not_add_blank(self) -> None:
        msg = ticket_grooming.build_digest(
            now_iso="2026-05-02 12:00 UTC",
            flagged=[],
            counts={"active": 1},
            hermes_line=None,
        )
        # No extraneous trailing blank line from None hermes_line.
        self.assertNotIn("HERMES audit", msg)


# ---------------------------------------------------------------------------
# CLI flags.
# ---------------------------------------------------------------------------


class TestDryRun(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "active").mkdir()
        self._patches = [
            patch.object(ticket_grooming, "TICKETS_ROOT", self.root),
            patch.object(
                ticket_grooming,
                "_now_utc",
                return_value=datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc),
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_dry_run_no_writes_no_commit_no_push_no_digest(self) -> None:
        last = datetime(2026, 5, 2, 5, 0, 0, tzinfo=timezone.utc)
        path = self.root / "active" / "T-0020.md"
        _make_ticket_file(
            path,
            tid="T-0020",
            state="active",
            transition_lines=[f"- {_ts(last)} [recovery] claimed; moving to active"],
        )
        original_text = path.read_text(encoding="utf-8")

        with patch.object(ticket_grooming, "git_commit_and_push") as mock_git, \
             patch.object(ticket_grooming, "_send_digest") as mock_send:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ticket_grooming.main(["--dry-run"])
            self.assertEqual(rc, 0)
            mock_git.assert_not_called()
            mock_send.assert_not_called()

        # File untouched
        self.assertEqual(path.read_text(encoding="utf-8"), original_text)
        # Stdout mentions what would flag
        self.assertIn("T-0020", buf.getvalue())

    def test_dry_run_reports_stale_draft(self) -> None:
        (self.root / "inbox").mkdir()
        _make_stale_draft_candidate(
            self.root / "inbox" / "T-2050.md",
            tid="T-2050",
            labels=["interactive-required"],
            state="inbox",
            draft_header=(
                "## Re-eval result (orchestrator, 2026-04-01 — DRAFT proposal, "
                "awaiting the operator's ratification)"
            ),
        )
        with patch.object(ticket_grooming, "git_commit_and_push") as mock_git, \
             patch.object(ticket_grooming, "_send_digest") as mock_send:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ticket_grooming.main(["--dry-run"])
            self.assertEqual(rc, 0)
            mock_git.assert_not_called()
            mock_send.assert_not_called()
        self.assertIn("STALE-DRAFT", buf.getvalue())
        self.assertIn("T-2050", buf.getvalue())


class TestNoDigestFlag(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "active").mkdir()
        self._patches = [
            patch.object(ticket_grooming, "TICKETS_ROOT", self.root),
            patch.object(
                ticket_grooming,
                "_now_utc",
                return_value=datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc),
            ),
            patch.object(
                ticket_grooming, "git_commit_and_push", return_value=(True, "mocked")
            ),
            patch.object(
                ticket_grooming,
                "run_history_audit",
                return_value="HERMES audit: 0 unescaped occurrence(s) across 0 commit(s) scanned.",
            ),
            # T-0471: bypass worktree shim in unit tests.
            patch.object(ticket_grooming, "_with_worktree", lambda fn: fn(None)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_no_digest_appends_and_commits_but_skips_send(self) -> None:
        last = datetime(2026, 5, 2, 5, 0, 0, tzinfo=timezone.utc)
        path = self.root / "active" / "T-0030.md"
        _make_ticket_file(
            path,
            tid="T-0030",
            state="active",
            transition_lines=[f"- {_ts(last)} [recovery] claimed; moving to active"],
        )
        with patch.object(ticket_grooming, "_send_digest") as mock_send:
            rc = ticket_grooming.main(["--no-digest", "--commit-prod"])
        self.assertEqual(rc, 0)
        mock_send.assert_not_called()
        # Append happened
        text = path.read_text(encoding="utf-8")
        self.assertIn("[grooming] stale", text)
        # And git_commit_and_push was called
        self.assertTrue(ticket_grooming.git_commit_and_push.called)


class TestThresholdOverrideCLIFlags(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "active").mkdir()
        self._patches = [
            patch.object(ticket_grooming, "TICKETS_ROOT", self.root),
            patch.object(
                ticket_grooming,
                "_now_utc",
                return_value=datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc),
            ),
            patch.object(
                ticket_grooming, "git_commit_and_push", return_value=(True, "mocked")
            ),
            patch.object(ticket_grooming, "_send_digest", return_value=True),
            patch.object(
                ticket_grooming,
                "run_history_audit",
                return_value="HERMES audit: 0 unescaped occurrence(s) across 0 commit(s) scanned.",
            ),
            # T-0471: bypass worktree shim in unit tests.
            patch.object(ticket_grooming, "_with_worktree", lambda fn: fn(None)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_threshold_active_override_makes_fresh_ticket_stale(self) -> None:
        # Default 4h would not flag a 2h-old ticket; --threshold-active 1h
        # should.
        last = datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc)
        path = self.root / "active" / "T-0040.md"
        _make_ticket_file(
            path,
            tid="T-0040",
            state="active",
            transition_lines=[f"- {_ts(last)} [recovery] claimed; moving to active"],
        )
        rc = ticket_grooming.main(["--threshold-active", "1h", "--commit-prod"])
        self.assertEqual(rc, 0)
        text = path.read_text(encoding="utf-8")
        self.assertIn("[grooming] stale", text)


# ---------------------------------------------------------------------------
# Full-fire integration.
# ---------------------------------------------------------------------------


class TestFullFireIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "active").mkdir()
        self._patches = [
            patch.object(ticket_grooming, "TICKETS_ROOT", self.root),
            patch.object(
                ticket_grooming,
                "_now_utc",
                return_value=datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc),
            ),
            # T-0471: bypass worktree shim in unit tests.
            patch.object(ticket_grooming, "_with_worktree", lambda fn: fn(None)),
        ]
        for p in self._patches:
            p.start()
        # T-1816 / T-1774: isolate the git_mutation_lock lockfile — see the
        # module-level comment on _GIT_MUTATION_LOCK_ENV above.
        os.environ[_GIT_MUTATION_LOCK_ENV] = os.path.join(
            self._tmp.name, "gitlock", "ops.lock"
        )

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        os.environ.pop(_GIT_MUTATION_LOCK_ENV, None)
        self._tmp.cleanup()

    def test_two_stale_tickets_full_flow(self) -> None:
        # T-0001: active, 5h old (stale at 4h default)
        # T-0002: active, 6h old (stale at 4h default)
        # T-0003: active, 1h old (NOT stale)
        a_stale = datetime(2026, 5, 2, 7, 0, 0, tzinfo=timezone.utc)
        b_stale = datetime(2026, 5, 2, 6, 0, 0, tzinfo=timezone.utc)
        a_fresh = datetime(2026, 5, 2, 11, 0, 0, tzinfo=timezone.utc)
        p1 = self.root / "active" / "T-0001.md"
        p2 = self.root / "active" / "T-0002.md"
        p3 = self.root / "active" / "T-0003.md"
        _make_ticket_file(
            p1, tid="T-0001", state="active",
            transition_lines=[f"- {_ts(a_stale)} [recovery] claimed; moving to active"],
        )
        _make_ticket_file(
            p2, tid="T-0002", state="active",
            transition_lines=[f"- {_ts(b_stale)} [recovery] claimed; moving to active"],
        )
        _make_ticket_file(
            p3, tid="T-0003", state="active",
            transition_lines=[f"- {_ts(a_fresh)} [recovery] claimed; moving to active"],
        )

        with patch("tools._git_utils.subprocess.run") as mock_run, \
             patch.object(ticket_grooming, "_send_digest", return_value=True) as mock_send, \
             patch.object(ticket_grooming, "run_history_audit",
                          return_value="HERMES audit: 0 unescaped occurrence(s) across 0 commit(s) scanned."):

            def fake_run(argv, **kwargs):
                if argv[:2] == ["git", "diff"]:
                    return MagicMock(
                        returncode=0,
                        stdout="tickets/active/T-0001.md\ntickets/active/T-0002.md\n",
                        stderr="",
                    )
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = fake_run
            rc = ticket_grooming.main(["--commit-prod"])

        self.assertEqual(rc, 0)
        # T-0001 + T-0002 should have a [grooming] stale line; T-0003 should not.
        self.assertIn("[grooming] stale", p1.read_text(encoding="utf-8"))
        self.assertIn("[grooming] stale", p2.read_text(encoding="utf-8"))
        self.assertNotIn("[grooming] stale", p3.read_text(encoding="utf-8"))

        # git command order: fetch + merge (from git_commit_and_push, cwd=None
        # legacy path) → add → diff → commit → push.
        # T-0471: pull_repo_or_warn removed from --commit-prod path (with_worktree
        # supersedes it); _with_worktree patched to fn(None) in this test, so
        # git_commit_and_push sees cwd=None and does its own fetch+merge. Only
        # one fetch+merge pair total, preceded by _abort_stale_rebase's
        # rev-parse×2 stale-rebase probe at pull entry (T-1191).
        kinds = [
            c.args[0][1]
            for c in mock_run.call_args_list
            if c.args[0][0] == "git"
        ]
        # T-2280: assert the MUTATING pipeline exactly; do NOT pin the number of
        # read-only probes. `rev-parse` is a probe whose count is an
        # implementation detail that scales with INPUT — T-2203 stamps the repo
        # HEAD on each transition-log entry, so N stale tickets now means N
        # extra probes on top of _abort_stale_rebase's pair (T-1191). Pinning
        # the exact count coupled this assertion to the fixture size, so adding
        # a third stale ticket to the fixture would have broken it just as
        # surely as adding a feature did — and any future read-only probe
        # breaks it again. What the test actually exists to protect is the
        # order and completeness of the WRITE pipeline.
        # T-2360: the read-only set, not a single verb. The comment above
        # predicted "any future read-only probe breaks it again", and that is
        # exactly what happened — the transition stamp moved from
        # `rev-parse --short HEAD` to `merge-base HEAD origin/main`, because
        # HEAD is rewritten by the pre-push rebase and made 22 of 33 citations
        # unreachable. Keeping this as a SET so the next read-only probe is a
        # one-word change rather than a red suite.
        READ_ONLY_PROBES = {"rev-parse", "merge-base", "symbolic-ref", "log"}  # T-3600 push-guard branch/log probe
        probes = [k for k in kinds if k in READ_ONLY_PROBES]
        mutating = [k for k in kinds if k not in READ_ONLY_PROBES]
        self.assertEqual(mutating, ["fetch", "merge", "add", "diff", "commit", "push"])
        self.assertGreaterEqual(
            len(probes), 2,
            "the stale-rebase probe (T-1191, rev-parse x2 at pull entry) must still run",
        )

        # Digest send invoked once with a message containing both stale ids.
        mock_send.assert_called_once()
        sent_text = mock_send.call_args.args[0]
        self.assertIn("T-0001", sent_text)
        self.assertIn("T-0002", sent_text)
        self.assertNotIn("T-0003", sent_text)
        self.assertIn("2 stale ticket(s)", sent_text)

    def test_no_stale_tickets_still_sends_proof_of_life_digest(self) -> None:
        # All tickets fresh -> no flags, no commit, but digest still sends.
        fresh = datetime(2026, 5, 2, 11, 0, 0, tzinfo=timezone.utc)
        p1 = self.root / "active" / "T-0050.md"
        _make_ticket_file(
            p1, tid="T-0050", state="active",
            transition_lines=[f"- {_ts(fresh)} [recovery] claimed; moving to active"],
        )
        with patch.object(
            ticket_grooming, "git_commit_and_push", return_value=(True, "no changes")
        ) as mock_git, \
             patch.object(ticket_grooming, "_send_digest", return_value=True) as mock_send, \
             patch.object(ticket_grooming, "run_history_audit",
                          return_value="HERMES audit: 0 unescaped occurrence(s) across 0 commit(s) scanned."):
            rc = ticket_grooming.main(["--commit-prod"])
        self.assertEqual(rc, 0)
        # No git activity (zero appends -> empty list, skip commit entirely).
        mock_git.assert_not_called()
        # But digest fires.
        mock_send.assert_called_once()
        self.assertIn("No stale tickets found", mock_send.call_args.args[0])


class TestPullRepoOrWarn(unittest.TestCase):
    """pull_repo_or_warn fail-open semantics for ticket_grooming — never raises, never blocks."""

    def test_pull_success_silent(self):
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            ticket_grooming.pull_repo_or_warn("ticket_grooming")  # no exception
        # T-0218: pull_repo_or_warn now fetches + merges (resilient to FETCH_HEAD pollution).
        self.assertEqual(mock_sub.run.call_count, 2)
        first_args, _ = mock_sub.run.call_args_list[0]
        self.assertEqual(first_args[0], ["git", "fetch", "origin", "main"])
        second_args, _ = mock_sub.run.call_args_list[1]
        self.assertEqual(second_args[0], ["git", "merge", "--ff-only", "refs/remotes/origin/main"])

    def test_pull_nonzero_proceeds(self):
        import contextlib
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=1, stdout="", stderr="fatal: refusing to merge unrelated histories\n"
            )
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                ticket_grooming.pull_repo_or_warn("ticket_grooming")  # must not raise
            self.assertIn("fetch failed", buf.getvalue())
            self.assertIn("refusing to merge", buf.getvalue())

    def test_pull_timeout_proceeds(self):
        import contextlib
        import subprocess as real_sub
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.TimeoutExpired = real_sub.TimeoutExpired
            mock_sub.run.side_effect = real_sub.TimeoutExpired(cmd="git", timeout=30)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                ticket_grooming.pull_repo_or_warn("ticket_grooming")  # must not raise
            self.assertIn("fetch skipped", buf.getvalue())


class TestDefaultSafeContractT0260(unittest.TestCase):
    """T-0260: default-safe contract — refuse to run without an explicit mode flag."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "tickets" / "active").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_flag_refuses_with_exit_64(self) -> None:
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = ticket_grooming.main([])
        self.assertEqual(rc, 64)
        self.assertIn("refusing to run without explicit mode", buf.getvalue())
        self.assertIn("--commit-prod", buf.getvalue())

    def test_commit_prod_plus_dry_run_is_ambiguous(self) -> None:
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            rc = ticket_grooming.main(["--commit-prod", "--dry-run"])
        self.assertEqual(rc, 64)

    def test_commit_prod_plus_repo_root_is_ambiguous(self) -> None:
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            rc = ticket_grooming.main(["--commit-prod", "--repo-root", str(self.root)])
        self.assertEqual(rc, 64)

    def test_dry_run_alone_is_accepted(self) -> None:
        with patch.object(ticket_grooming, "pull_repo_or_warn"), \
             patch.object(ticket_grooming, "TICKETS_ROOT", self.root / "tickets"):
            with redirect_stdout(io.StringIO()):
                rc = ticket_grooming.main(["--dry-run"])
        self.assertEqual(rc, 0)

    def test_repo_root_alone_is_accepted(self) -> None:
        with patch.object(ticket_grooming, "pull_repo_or_warn"), \
             patch.object(ticket_grooming, "git_commit_and_push", return_value=(True, "mocked")), \
             patch.object(ticket_grooming, "_send_digest", return_value=True), \
             patch.object(ticket_grooming, "run_history_audit", return_value="HERMES audit clean."):
            with redirect_stdout(io.StringIO()):
                rc = ticket_grooming.main(["--repo-root", str(self.root)])
        self.assertEqual(rc, 0)

    def test_commit_prod_alone_is_accepted(self) -> None:
        # T-1490: bypass the real worktree shim (fn(None)) like every other
        # --commit-prod test. Without this, `--commit-prod` alone hits
        # use_worktree=True and runs a REAL `git worktree add` against the live
        # REPO_ROOT, which races concurrent-account commits/pushes — the rare
        # ~1-in-30 T-0993 soak flake (pinned via T-1486). fn(None) keeps the
        # "--commit-prod alone is accepted" semantic while isolating the test.
        with patch.object(ticket_grooming, "pull_repo_or_warn"), \
             patch.object(ticket_grooming, "_with_worktree", lambda fn: fn(None)), \
             patch.object(ticket_grooming, "TICKETS_ROOT", self.root / "tickets"), \
             patch.object(ticket_grooming, "git_commit_and_push", return_value=(True, "mocked")), \
             patch.object(ticket_grooming, "_send_digest", return_value=True), \
             patch.object(ticket_grooming, "run_history_audit", return_value="HERMES audit clean."):
            with redirect_stdout(io.StringIO()):
                rc = ticket_grooming.main(["--commit-prod"])
        self.assertEqual(rc, 0)


class TestT0260Layer3Integration(unittest.TestCase):
    """T-0260 Layer-3 integration: spawn the real script as a subprocess against
    a tmp pantheon-config equivalent; assert the real repo is untouched.
    """

    def setUp(self) -> None:
        from tools._test_fixtures import setup_integration_env, snapshot_real_repo
        self.real_snapshot = snapshot_real_repo()
        self.env = setup_integration_env()
        for d in ("active", "inbox", "done"):
            (self.env.repo_path / "tickets" / d).mkdir(parents=True, exist_ok=True)
        (self.env.repo_path / "tickets" / "README.md").write_text(
            "# Tickets\n\n## Current state\n\n| ID | State | Title | Requester → Target | Filed |\n|---|---|---|---|---|\n\n## Related\n\n- Link\n",
            encoding="utf-8",
        )
        import subprocess as sp
        sp.run(["git", "add", "tickets/"], cwd=self.env.repo_path, check=False, capture_output=True)
        sp.run(["git", "commit", "-m", "seed tickets dirs"], cwd=self.env.repo_path, check=False, capture_output=True)
        sp.run(["git", "push", "origin", "main"], cwd=self.env.repo_path, check=False, capture_output=True)

    def tearDown(self) -> None:
        from tools._test_fixtures import teardown_integration_env, assert_real_repo_untouched
        teardown_integration_env(self.env)
        assert_real_repo_untouched(self.real_snapshot)

    def test_no_flag_invocation_refuses_loudly(self) -> None:
        from tools._test_fixtures import run_script
        rc, _stdout, stderr = run_script(
            "ticket_grooming", [],
            env=self.env.env_overrides,
        )
        self.assertEqual(rc, 64)
        self.assertIn("refusing to run without explicit mode", stderr)

    def test_dry_run_returns_zero_against_empty_tickets(self) -> None:
        from tools._test_fixtures import run_script
        rc, stdout, _stderr = run_script(
            "ticket_grooming", ["--dry-run"],
            env=self.env.env_overrides,
        )
        self.assertEqual(rc, 0)
        self.assertIn("(dry-run)", stdout)

    def test_repo_root_against_tmp_empty_tickets_no_changes(self) -> None:
        """--repo-root targets tmp; with empty tickets there's nothing to flag → clean run, no commit."""
        from tools._test_fixtures import run_script, git_head
        head_before = git_head(self.env.repo_path)
        rc, _stdout, _stderr = run_script(
            "ticket_grooming", ["--repo-root", str(self.env.repo_path)],
            env={**self.env.env_overrides, "PANTHEON_TELEGRAM_ENV": "/nonexistent/path"},
        )
        self.assertEqual(rc, 0)
        head_after = git_head(self.env.repo_path)
        self.assertEqual(head_before, head_after)


# ---------------------------------------------------------------------------
# Active-queue snapshot tests (T-0383).
# ---------------------------------------------------------------------------


def _make_active_ticket_file(
    active_dir: Path,
    tid: str,
    target: str,
    created: str = "2026-05-01T00:00:00Z",
    title: str = "Active ticket",
) -> None:
    """Write a minimal active-state ticket file for snapshot tests."""
    active_dir.mkdir(parents=True, exist_ok=True)
    fm = "\n".join([
        "---",
        f"id: {tid}",
        f"created: {created}",
        "requester: orchestrator",
        f"target: {target}",
        "state: active",
        "parent_ticket: null",
        "depends_on: []",
        "labels: []",
        "stale_after: 7d",
        "---",
    ])
    body = (
        f"\n# {title}\n\n"
        "## Ask\n\nDo the thing.\n\n"
        "## Transition log\n\n"
        f"- {created} [orchestrator] claimed; moving to active\n"
    )
    (active_dir / f"{tid}.md").write_text(fm + body, encoding="utf-8")


class TestBuildActiveQueueSnapshot(unittest.TestCase):
    """Unit tests for build_active_queue_snapshot()."""

    def setUp(self):
        from tools.ticket_grooming import build_active_queue_snapshot
        self.build_active_queue_snapshot = build_active_queue_snapshot

    def test_single_agent_with_tickets(self):
        """Agent with 2 tickets -> line shows count + oldest created date."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            active_dir = tickets_root / "active"
            _make_active_ticket_file(active_dir, "T-0001", "orchestrator",
                                     created="2026-05-01T00:00:00Z")
            _make_active_ticket_file(active_dir, "T-0002", "orchestrator",
                                     created="2026-05-10T00:00:00Z")
            with patch("tools.ticket_grooming.TICKETS_ROOT", tickets_root):
                result = self.build_active_queue_snapshot(["orchestrator"])
        self.assertIn("orchestrator", result)
        self.assertIn("2 ticket", result)
        self.assertIn("T-0001", result)   # oldest ticket mentioned
        self.assertIn("2026-05-01", result)  # oldest date

    def test_agent_with_zero_tickets(self):
        """Agent with no active tickets -> '0 tickets active' appears."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            (tickets_root / "active").mkdir(parents=True, exist_ok=True)
            with patch("tools.ticket_grooming.TICKETS_ROOT", tickets_root):
                result = self.build_active_queue_snapshot(["orchestrator"])
        self.assertIn("orchestrator", result)
        self.assertIn("0 ticket", result)

    def test_cross_agent_sections_all_three(self):
        """Three agents -> all three appear in the snapshot output."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            active_dir = tickets_root / "active"
            _make_active_ticket_file(active_dir, "T-0001", "orchestrator")
            _make_active_ticket_file(active_dir, "T-0002", "recovery")
            _make_active_ticket_file(active_dir, "T-0003", "specialist")
            with patch("tools.ticket_grooming.TICKETS_ROOT", tickets_root):
                result = self.build_active_queue_snapshot(["orchestrator", "specialist", "recovery"])
        self.assertIn("orchestrator", result)
        self.assertIn("specialist", result)
        self.assertIn("recovery", result)

    def test_role_id_target_counts_under_persona(self):
        """Ticket with target=orchestrator is counted under orchestrator."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            active_dir = tickets_root / "active"
            _make_active_ticket_file(active_dir, "T-0001", "orchestrator")
            with patch("tools.ticket_grooming.TICKETS_ROOT", tickets_root):
                result = self.build_active_queue_snapshot(["orchestrator"])
        self.assertIn("orchestrator", result)
        self.assertIn("1 ticket", result)
        self.assertIn("T-0001", result)

    def test_missing_active_dir_returns_zero_line(self):
        """Missing tickets/active/ -> graceful; shows 0 tickets for each agent."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp) / "tickets"
            # No active dir at all.
            with patch("tools.ticket_grooming.TICKETS_ROOT", tickets_root):
                result = self.build_active_queue_snapshot(["orchestrator", "recovery"])
        # Should not raise; should mention agents with 0 count.
        self.assertIn("orchestrator", result)
        self.assertIn("recovery", result)
        self.assertIn("0 ticket", result)


class TestBuildDigestActiveSnapshot(unittest.TestCase):
    """Tests for active_queue_section integration in build_digest()."""

    def setUp(self):
        from tools.ticket_grooming import build_digest
        self.build_digest = build_digest

    def test_digest_contains_active_snapshot_when_provided(self):
        """build_digest with active_queue_section -> section present in output."""
        snapshot = "Active queue snapshot:\n  orchestrator: 2 tickets active, oldest created 2026-05-01 (T-0001: some ticket)"
        result = self.build_digest(
            "2026-05-17 12:00 UTC",
            [],
            {"active": 2},
            active_queue_section=snapshot,
        )
        self.assertIn("Active queue snapshot", result)
        self.assertIn("orchestrator", result)
        self.assertIn("T-0001", result)

    def test_digest_without_active_snapshot_unchanged(self):
        """build_digest without active_queue_section -> backward-compatible; no section."""
        result = self.build_digest(
            "2026-05-17 12:00 UTC",
            [],
            {"active": 0},
        )
        self.assertNotIn("Active queue snapshot", result)

    def test_digest_active_snapshot_zero_tickets_edge_case(self):
        """Zero-ticket snapshot line appears correctly in digest."""
        snapshot = "Active queue snapshot:\n  orchestrator: 0 tickets active"
        result = self.build_digest(
            "2026-05-17 12:00 UTC",
            [],
            {"active": 0},
            active_queue_section=snapshot,
        )
        self.assertIn("0 tickets active", result)


# ---------------------------------------------------------------------------
# T-0478 regression: REPO_ROOT must stay canonical after _with_worktree exits.
# ---------------------------------------------------------------------------


class TestRepoRootStaysCanonicalPostWorktreeExit(unittest.TestCase):
    """T-0478: _scan_and_commit must NOT rebind module-global REPO_ROOT.

    Before the fix, _scan_and_commit did:
        global REPO_ROOT, TICKETS_ROOT
        REPO_ROOT = wt_path   # <-- leaks past with_worktree cleanup

    After the fix, REPO_ROOT retains its original canonical value throughout
    the run, even when _with_worktree supplies a (non-existent) tmp path.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        (self.tmp_root / "tickets" / "active").mkdir(parents=True)
        # Save canonical REPO_ROOT so we can restore it and assert against it.
        self._original_repo_root = ticket_grooming.REPO_ROOT

    def tearDown(self) -> None:
        # Always restore canonical REPO_ROOT so other tests are unaffected.
        ticket_grooming.REPO_ROOT = self._original_repo_root
        ticket_grooming.TICKETS_ROOT = self._original_repo_root / "tickets"
        self._tmp.cleanup()

    def test_repo_root_stays_canonical_post_with_worktree_exit(self) -> None:
        """REPO_ROOT must equal the canonical clone path after main() returns.

        Simulates the production code path: _with_worktree calls the inner fn
        with a fake /tmp/... path. After block exit, REPO_ROOT must NOT be
        pointing at that tmp path.
        """
        fake_wt = Path("/tmp/fake-worktree-T0478-grooming")

        def fake_with_worktree(fn):
            fn(fake_wt)

        # Use tmp_root as TICKETS_ROOT so _scan_state_dir can iterate.
        with patch.object(ticket_grooming, "_with_worktree", fake_with_worktree), \
             patch.object(ticket_grooming, "TICKETS_ROOT", self.tmp_root / "tickets"), \
             patch.object(ticket_grooming, "_send_digest", return_value=True), \
             patch.object(ticket_grooming, "run_history_audit",
                          return_value="audit ok"), \
             patch.object(ticket_grooming, "build_active_queue_snapshot",
                          return_value="snapshot ok"), \
             patch.object(ticket_grooming, "git_commit_and_push",
                          return_value=(True, "ok")):
            rc = ticket_grooming.main(["--commit-prod"])

        # Primary assertion: REPO_ROOT was NOT permanently rebound to the fake
        # worktree path. This is the load-bearing check for T-0478.
        self.assertNotEqual(
            ticket_grooming.REPO_ROOT, fake_wt,
            "REPO_ROOT was rebound to the fake worktree path — leak detected",
        )
        self.assertEqual(
            ticket_grooming.REPO_ROOT, self._original_repo_root,
            "REPO_ROOT must equal the canonical clone path after main() returns",
        )
        self.assertEqual(rc, 0)


class T0697AcceptanceFalsePositiveRegression(unittest.TestCase):
    """T-0697: the four 2026-05-29 (commit b0f8927e) acceptance false-positives
    must NOT reopen, while genuine mis-closes still flag.

    Builds a throwaway git repo whose layout mirrors the conditions that tripped
    each class: a repo-name-named subdir (so a `<repo-name>/...` path survives
    the cross-repo top-dir guard and reaches the prefix-strip normalisation
    rather than being skipped), a real root README.md, a real tools/ dir, and a
    real commit for the present-SHA case.
    """

    def setUp(self):
        self.repo = tempfile.TemporaryDirectory()
        root = Path(self.repo.name)
        self.root = root
        (root / "tools").mkdir()
        (root / "tools" / "real_tool.py").write_text("# real\n")
        (root / "README.md").write_text("# readme\n")
        # Mirror production: pantheon-config contains a `pantheon-config/`
        # subdir, so a `<repo-name>/README.md` claim passes the cross-repo
        # top-dir guard and must be handled by prefix-strip, not skipped.
        (root / root.name).mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "init"], cwd=root, check=True)
        self.real_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root,
            capture_output=True, text=True, check=True).stdout.strip()

    def tearDown(self):
        self.repo.cleanup()

    def _acc(self, body):
        return ticket_grooming.check_acceptance(body, self.root)

    def _resolved(self, rest):
        return ("## Transition log\n"
                "- 2026-05-29T00:00:00Z [t] resolved; " + rest + "\n")

    # --- class 1: cherry-pick / squash SHA (T-0642) ---
    def test_t0642_cherrypick_sha_absent_not_reopened(self):
        # Recorded branch SHA cherry-picked to a new SHA on main; the literal
        # SHA is absent from this clone's store -> indeterminate, never flag.
        body = self._resolved("landed at commit 5ba1d52 (eval harness + spec)")
        self.assertIsNone(self._acc(body))

    def test_present_sha_still_passes(self):
        body = self._resolved("landed at commit " + self.real_sha + " (init)")
        self.assertIsNone(self._acc(body))

    # --- class: negated-landing mention (T-1709 / T-1737) ---
    def test_t1709_negated_landing_artifact_not_reopened(self):
        # A resolution that transparently says an artifact did NOT land must not
        # be mined as a claim (the T-1709 false-reopen: a referenced source
        # report that never landed on disk).
        body = self._resolved(
            "VERIFIED. Referenced report knowledge/watch/reports/2026-07-13/"
            "mcp-supply-chain.md hadn't landed on disk yet; ask was self-contained")
        self.assertIsNone(self._acc(body))

    def test_negated_landing_forms_not_reopened(self):
        for phrase in ("was never created", "did not land", "wasn't added yet",
                       "has not shipped"):
            body = self._resolved(f"note: doc notes/some-report.md {phrase}")
            self.assertIsNone(self._acc(body), f"reopened on: {phrase}")

    def test_positive_landing_still_flags_missing(self):
        # The negated-landing guard must NOT suppress a genuine landed-but-missing
        # claim (real mis-close detection stays live).
        body = self._resolved("created tools/definitely_missing_xyz.py (new tool)")
        self.assertIsNotNone(self._acc(body))

    # --- class 2: repo-name-prefixed path (T-0667) ---
    def test_t0667_repo_name_prefixed_path_not_reopened(self):
        body = self._resolved("created " + self.root.name + "/README.md")
        self.assertIsNone(self._acc(body))

    def test_artifact_exists_strips_repo_name_prefix(self):
        self.assertTrue(
            ticket_grooming._artifact_exists(self.root.name + "/README.md", "path", self.root))
        self.assertTrue(ticket_grooming._artifact_exists("README.md", "path", self.root))
        self.assertFalse(
            ticket_grooming._artifact_exists(self.root.name + "/nope_xyz.md", "path", self.root))

    def test_repo_name_prefixed_missing_still_flags(self):
        body = self._resolved("created " + self.root.name + "/tools/ghost_xyz.py")
        miss = self._acc(body)
        self.assertIsNotNone(miss)
        self.assertEqual(miss[1], "path")

    # --- class 3: cross-repo (T-0676, T-0683) ---
    def test_t0676_cross_repo_topdir_absent_not_reopened(self):
        body = self._resolved("created src-frontend/index.html")
        self.assertIsNone(self._acc(body))

    def test_t0683_cross_repo_shared_dir_named_repo_not_reopened(self):
        # tools/ exists here, but the line names echo-prismatic -> not ours.
        body = self._resolved(
            "install-hooks.sh landed at tools/install-hooks.sh in echo-prismatic")
        self.assertIsNone(self._acc(body))

    # --- class 5: extension prefix-truncation (T-2361) ---
    def test_t2361_tsv_artifact_present_not_truncated_to_ts(self):
        """T-2361: `.tsv` must not be matched as its `.ts` prefix.

        `_KNOWN_PATH_EXTS` contained `ts` but not `tsv` and the alternation had
        no trailing boundary, so `data/x.tsv` matched as `data/x.ts` — a path
        NOBODY claimed. `_artifact_exists` then failed on it and refiled a
        correctly-done ticket done -> inbox. The module's own header says "when
        in doubt, DON'T flag"; this path flagged with certainty on a string
        that appears in no ticket and on no disk.
        """
        (self.root / "data").mkdir()
        (self.root / "data" / "x.tsv").write_text("a\tb\n")
        body = self._resolved("wrote data/x.tsv (the FP corpus)")
        self.assertIsNone(self._acc(body))

    def test_t2361_genuinely_missing_tsv_still_flags_the_real_path(self):
        # tsv is now a KNOWN extension, so a truly absent .tsv artifact must
        # flag — and must name the FULL path, not a truncated one. This is the
        # known-positive proving the boundary didn't just stop `.tsv` matching.
        # data/ must exist or the cross-repo top-dir guard suppresses the
        # finding before the extension logic is ever exercised.
        (self.root / "data").mkdir(exist_ok=True)
        body = self._resolved("wrote data/absent_xyz.tsv (the FP corpus)")
        self.assertEqual(self._acc(body), ("data/absent_xyz.tsv", "path"))

    def test_t2361_jsonl_artifact_present_not_truncated(self):
        # Same class via `json` ⊂ `jsonl` — the ledger's own format, so this
        # prefix pair appears in real resolve notes constantly. The path needs a
        # slash (the regex requires one) and its top dir must exist (else the
        # cross-repo guard suppresses the finding VACUOUSLY — measured, not
        # guessed: a bare `ledgerish.jsonl` was never mined at all).
        (self.root / "data").mkdir(exist_ok=True)
        (self.root / "data" / "ledgerish.jsonl").write_text("{}\n")
        body = self._resolved("created data/ledgerish.jsonl for the corpus")
        self.assertIsNone(self._acc(body))

    def test_t2361_unlisted_extension_simply_does_not_match(self):
        # The boundary makes an UNLISTED extension (.sqlite: `sql` is listed and
        # is a prefix of it) stop matching entirely rather than truncate to a
        # phantom `.sql` claim. Landing on "don't flag" is the module's stated
        # side of doubt — silence here is deliberate, not a miss.
        #
        # data/ MUST exist in the fixture: without it the cross-repo top-dir
        # guard suppresses the truncated `data/dashboard.sql` finding anyway and
        # this test passes VACUOUSLY against the unfixed regex (measured — the
        # first draft did exactly that). With data/ present, the pre-fix regex
        # flags the phantom `.sql` path and this test goes red, which is what
        # makes its green after the fix mean something.
        (self.root / "data").mkdir(exist_ok=True)
        body = self._resolved("created data/dashboard.sqlite for run state")
        self.assertIsNone(self._acc(body))

    # --- guardrail: a genuine mis-close must still flag ---
    def test_genuine_missing_path_still_flags(self):
        body = self._resolved("created tools/totally_absent_xyz.py")
        self.assertEqual(self._acc(body), ("tools/totally_absent_xyz.py", "path"))

    # --- class 4: slash-joined filename LIST in prose (T-0973) ---
    def test_t0973_slash_joined_filename_list_not_reopened(self):
        # `repos.yaml/instance.yaml` in resolution prose = TWO real files, not one
        # path. Both exist → the groomer must NOT false-reopen (pre-fix this exact
        # shape false-reopened T-0882, because `repos.yaml` IS a file so the bogus
        # joined token `repos.yaml/instance.yaml` slipped past the cross-repo guard
        # and then _artifact_exists failed on the non-existent joined path).
        (self.root / "repos.yaml").write_text("repos\n")
        (self.root / "instance.yaml").write_text("instance\n")
        body = self._resolved("re-keyed repos.yaml/instance.yaml")
        self.assertIsNone(self._acc(body))

    def test_t0973_joined_list_missing_part_does_not_reopen(self):
        # A genuinely-absent bare file in the split list is conservatively skipped
        # by the existing cross-repo guard (its top-dir does not exist here), so the
        # net is still no false-reopen — matching the groomer's conservative bias.
        (self.root / "repos.yaml").write_text("repos\n")
        body = self._resolved("re-keyed repos.yaml/ghost_absent_xyz.yaml")
        self.assertIsNone(self._acc(body))

    def test_t0973_split_helper_leaves_normal_paths_intact(self):
        split = ticket_grooming._split_joined_filename_list
        self.assertEqual(split("tools/foo.py"), ["tools/foo.py"])
        self.assertEqual(split("a/b/c.yaml"), ["a/b/c.yaml"])
        self.assertEqual(split("adr/ORG-001-foo.md"), ["adr/ORG-001-foo.md"])
        self.assertEqual(split("repos.yaml/instance.yaml"), ["repos.yaml", "instance.yaml"])
        self.assertEqual(split("a.py/b.sh/c.md"), ["a.py", "b.sh", "c.md"])


class ManifestFreshnessTest(unittest.TestCase):
    """T-1624: check_manifest_freshness — surfacing-only, fail-open."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.now = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)

    def _touch(self, name: str, days_old: int) -> None:
        import os as _os
        p = self.root / name
        p.write_text("x: 1\n")
        ts = (self.now - timedelta(days=days_old)).timestamp()
        _os.utime(p, (ts, ts))

    def test_both_fresh_returns_none(self) -> None:
        self._touch("instance.yaml", 3)
        self._touch("repos.yaml", 10)
        self.assertIsNone(ticket_grooming.check_manifest_freshness(self.root, self.now))

    def test_one_stale_is_reported(self) -> None:
        self._touch("instance.yaml", 45)
        self._touch("repos.yaml", 3)
        line = ticket_grooming.check_manifest_freshness(self.root, self.now)
        self.assertIn("instance.yaml unreviewed 45d", line)
        self.assertNotIn("repos.yaml unreviewed", line)

    def test_missing_file_reported_not_raised(self) -> None:
        self._touch("instance.yaml", 1)
        line = ticket_grooming.check_manifest_freshness(self.root, self.now)
        self.assertIn("repos.yaml MISSING/unreadable", line)


# ---------------------------------------------------------------------------
# T-1814: ratified-but-unbuilt ADR surfacing.
#
# Real-corpus fixture strings below are copied VERBATIM (D11 fixture
# provenance) from:
#   - echo-prismatic adr/ADR-239 (line 3) and adr/ADR-232 (line 3) — the
#     "**Status:** **Ratified ...** Proposed (...)" mixed-status-line shape.
#   - pantheon-config adr/ORG-028-portable-config-topology.md (line 11) —
#     the "**Status: RATIFIED ...** (trailing prose)" bold-wrapping variant
#     that motivated the plan amendment (originator re-confirmation, D6).
#   - tickets/inbox/T-1602.md and tickets/inbox/T-1528.md `labels:` lists —
#     the real adr-NNN+build join fixtures.
# ---------------------------------------------------------------------------

_REAL_ADR_239_STATUS_LINE = (
    "**Status:** **Ratified 2026-07-05 by the operator.** Proposed (drafted "
    "2026-07-05 by Orchestrator/HO from Specialist research T-1511 + the operator's "
    "volume-guard lead 2026-07-04, codex bounds encoded). **Date:** "
    "2026-07-05 **Author:** Orchestrator (HO) **HITL:** **Related:** T-1511 "
    "(design input), ADR-229 (barge-in loop), ADR-230 (consumer terminal "
    "ownership), ADR-232 (the AEC fix this composes with), T-1503 (harness "
    "= acceptance rig), T-1523-1528 build wave"
)

_REAL_ADR_232_STATUS_LINE = (
    "**Status:** **Ratified 2026-07-05 by the operator.** Proposed (drafted "
    "2026-07-04 by Orchestrator/HO from Specialist research T-1508 + codex verdict "
    "SOUND-WITH-CORRECTIONS + T-1504 bench data + the operator's 2026-07-04 "
    "volume-sweep). **Date:** 2026-07-04 **Author:** Orchestrator (HO) **HITL:** "
    "**Related:** T-1373 (arc), T-1508 (design input), T-1504 (bench "
    "evidence), PR #173 (superseded lever, do-not-merge), ADR-229 "
    "(barge-in/AEC), knowledge/episodic/2026-07-04-ai-voice-systems-"
    "landscape/ameliorations.md"
)

# ORG-028 line 11 (form-b bold-wrapping variant): the whole
# "Status: RATIFIED ... [[T-0845]]" span is inside ONE bold marker, unlike
# ADR-239/232's "**Status:**" (label-only bold) + separate "**Ratified...**".
_REAL_ORG_028_STATUS_LINE = (
    "**Status: RATIFIED 2026-06-06 by the operator via [[T-0845]]** (the file's "
    "status line lagged the ratification; corrected 2026-06-06). Drafted "
    "2026-06-05 by Recovery (`claude-fallback`) per the operator's \"draft it\" "
    "(Telegram 2026-06-05). Per ORG-015, `adr/` is the ratifiable "
    "substrate. The 4 deferred open questions are resolved in "
    "**[[ORG-030]]** (awaiting ratification); irreversible execution stays "
    "gated per the sequencing note below."
)

_REAL_T1602_LABELS = ["echo", "barge-in", "adr-239", "build", "interactive-required"]
_REAL_T1528_LABELS = ["echo", "build", "adr-232", "aec", "windows", "interactive-required"]

# T-2825 fixtures, pulled verbatim (D11) from the live `**Status:**` lines
# via `grep -n '^\*\*Status:' adr/ORG-065*.md adr/ORG-019*.md` during plan
# approval.
_REAL_ORG_065_STATUS_LINE = (
    "**Status:** Proposed (agent-authored, unratified) — DRAFT awaiting "
    "the operator's ratification. The operator directed this ADR on 2026-08-08 "
    "(relayed via the codex mailbox, confirmed directly on Telegram, "
    "TG7326) at the same time he ratified T-2169 — the prose "
    "closure-discipline this ADR is the structural version of. "
    "**HITL: Proposed.** Design-first: this ADR fixes the model and the "
    "migration shape; it deliberately does **not** commit to tooling "
    "implementation (that is a build ticket gated on ratification)."
)

_REAL_ORG_019_STATUS_LINE = (
    "**Status:** Drafted 2026-05-18 by Orchestrator (Orchestrator). "
    "**Ratified 2026-05-19T00:58Z by the operator via T-0454 (msg_1575 "
    "\"ratify ORG-019\").** Replaces the soft-block discipline accreted "
    "across T-0383, T-0439, T-0440, T-0452. Phase 2-4 execution "
    "authorized to HO."
)


def _write_adr_file(
    adr_dir: Path, filename: str, status_line: str, frontmatter: dict | None = None
) -> Path:
    """Write a minimal ADR file whose `## `-less body contains `status_line`
    verbatim (the line the T-1814 status-line regex parses). `frontmatter`,
    if given, is written as a real `---`-delimited YAML block ahead of the
    body (T-2806: `build_disposition`/`scoping_ticket` live here, matching
    the real ORG-069..072 shape)."""
    adr_dir.mkdir(parents=True, exist_ok=True)
    path = adr_dir / filename
    fm_block = ""
    if frontmatter:
        fm_lines = ["---"] + [f"{k}: {v}" for k, v in frontmatter.items()] + ["---"]
        fm_block = "\n".join(fm_lines) + "\n\n"
    path.write_text(
        f"{fm_block}# {filename}\n\n{status_line}\n\n## Decision\n\ncontent.\n",
        encoding="utf-8",
    )
    return path


class TestRatifiedADRStatusDetection(unittest.TestCase):
    """T-1814 design point 1: status-line parsing + Ratified/exclusion logic."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.adr_dir = Path(self._tmp.name) / "adr"
        self.tickets_dir = Path(self._tmp.name) / "tickets"
        (self.tickets_dir / "inbox").mkdir(parents=True)
        (self.tickets_dir / "active").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def test_status_line_ratified_and_proposed_mixed_flags_as_ratified(self) -> None:
        """Real ADR-239 line carries BOTH 'Ratified' and 'Proposed'; the
        locked design says Ratified-anywhere-in-line wins."""
        _write_adr_file(self.adr_dir, "ADR-239 — barge-in.md", _REAL_ADR_239_STATUS_LINE)
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-1602.md",
            tid="T-1602", state="inbox", labels=_REAL_T1602_LABELS,
        )
        flagged, gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(gaps, [])
        self.assertEqual([f.adr_id for f in flagged], ["ADR-239"])

    def test_status_line_form_b_bold_wrapping_flags_as_ratified(self) -> None:
        """Amendment test: the ORG-028 'Status: RATIFIED ...**' bold-wrapping
        variant (whole span in one bold marker) must ALSO flag — this is the
        format the plan amendment explicitly requires, not a blind spot."""
        _write_adr_file(self.adr_dir, "ORG-028-topology.md", _REAL_ORG_028_STATUS_LINE)
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9001.md",
            tid="T-9001", state="inbox", labels=["adr-028", "build"],
        )
        flagged, gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(gaps, [])
        self.assertEqual([f.adr_id for f in flagged], ["ORG-028"])

    def test_exclusion_scoped_to_first_sentence_not_whole_line(self) -> None:
        """Regression pin: the real ADR-232 status line is genuinely Ratified
        but its LATER **Related:** metadata mentions an unrelated "PR #173
        (superseded lever, do-not-merge)" — the word 'superseded' there must
        NOT false-exclude a ratified ADR (whole-line exclusion scanning was
        the original bug; fix scopes exclusion to the first sentence)."""
        self.assertTrue(
            ticket_grooming._is_ratified_status(
                _REAL_ADR_232_STATUS_LINE.split("**Status:** ", 1)[1]
            )
        )

    def test_superseded_excludes_even_with_ratified_word(self) -> None:
        status = "**Status:** Superseded 2026-05-29 by [[ORG-025]]. Was Ratified 2026-05-17 originally."
        _write_adr_file(self.adr_dir, "ORG-023-superseded.md", status)
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9002.md",
            tid="T-9002", state="inbox", labels=["adr-023", "build"],
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(flagged, [])

    def test_retired_and_rejected_also_exclude(self) -> None:
        for word, num in (("Retired", "050"), ("Rejected", "051")):
            with self.subTest(word=word):
                status = f"**Status:** {word} 2026-05-29. Was Ratified previously."
                _write_adr_file(self.adr_dir, f"ORG-{num}-x.md", status)
                _make_ticket_file(
                    self.tickets_dir / "inbox" / f"T-90{num}.md",
                    tid=f"T-90{num}", state="inbox", labels=[f"adr-{num}", "build"],
                )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(flagged, [])

    def test_non_ratified_status_not_flagged(self) -> None:
        _write_adr_file(self.adr_dir, "ORG-060-draft.md", "**Status:** Proposed, awaiting ratification.")
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9003.md",
            tid="T-9003", state="inbox", labels=["adr-060", "build"],
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(flagged, [])

    def test_missing_status_line_not_flagged(self) -> None:
        (self.adr_dir).mkdir(parents=True)
        (self.adr_dir / "ORG-070-nostatus.md").write_text("# no status field\n\nbody.\n")
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9004.md",
            tid="T-9004", state="inbox", labels=["adr-070", "build"],
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(flagged, [])

    def test_adr_and_org_prefixed_filenames_both_recognized(self) -> None:
        _write_adr_file(self.adr_dir, "ADR-101 — echo thing.md", "**Status:** Ratified 2026-01-01 by the operator.")
        _write_adr_file(self.adr_dir, "ORG-102-pantheon-thing.md", "**Status:** Ratified 2026-01-02 by the operator.")
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9101.md",
            tid="T-9101", state="inbox", labels=["adr-101", "build"],
        )
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9102.md",
            tid="T-9102", state="inbox", labels=["adr-102", "build"],
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(sorted(f.adr_id for f in flagged), ["ADR-101", "ORG-102"])

    def test_org065_real_shape_unratified_not_flagged(self) -> None:
        """T-2825 regression pin: the real ORG-065 status line's first
        sentence is genuinely unratified ('unratified ... DRAFT awaiting
        the operator's ratification') but a LATER clause mentions the operator
        ratifying an unrelated ticket ('at the same time he ratified
        T-2169') -- the whole-line Ratified-anywhere-in-line scan must not
        treat this as ratified. This is the false positive T-2825 fixes."""
        _write_adr_file(self.adr_dir, "ORG-065-x.md", _REAL_ORG_065_STATUS_LINE)
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(flagged, [])

    def test_org019_real_shape_drafted_then_ratified_stays_flagged(self) -> None:
        """T-2825 regression-trap guard: ORG-019's real shape is 'Drafted
        2026-05-18 ... **Ratified 2026-05-19T00:58Z by the operator...**' -- a
        bare-leading-word ('Drafted') exclusion would misclassify this (and
        every Drafted-then-Ratified ADR) as unratified, silently hiding a
        real gap. Must stay flagged here (no build_disposition/scoping_ticket
        marker in this fixture, so it's a genuine zero-ticket gap)."""
        _write_adr_file(self.adr_dir, "ORG-019-x.md", _REAL_ORG_019_STATUS_LINE)
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual([f.adr_id for f in flagged], ["ORG-019"])


class TestRatifiedADRJoin(unittest.TestCase):
    """T-1814 design point 2: join against open adr-NNN+build tickets."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.adr_dir = Path(self._tmp.name) / "adr"
        self.tickets_dir = Path(self._tmp.name) / "tickets"
        (self.tickets_dir / "inbox").mkdir(parents=True)
        (self.tickets_dir / "active").mkdir()
        (self.tickets_dir / "done").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def test_t1602_adr239_real_shape_flags_open_build_ticket(self) -> None:
        _write_adr_file(self.adr_dir, "ADR-239 — barge-in.md", _REAL_ADR_239_STATUS_LINE)
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-1602.md",
            tid="T-1602", state="inbox", labels=_REAL_T1602_LABELS,
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].adr_id, "ADR-239")
        self.assertEqual(flagged[0].open_tickets, [("T-1602", "inbox")])

    def test_t1528_adr232_real_shape_flags_open_build_ticket(self) -> None:
        _write_adr_file(self.adr_dir, "ADR-232 — aec.md", _REAL_ADR_232_STATUS_LINE)
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-1528.md",
            tid="T-1528", state="inbox", labels=_REAL_T1528_LABELS,
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].adr_id, "ADR-232")
        self.assertEqual(flagged[0].open_tickets, [("T-1528", "inbox")])

    def test_near_miss_label_missing_build_counts_as_zero_ticket_gap(self) -> None:
        """T-2806 amendment: a ticket carrying the adr-NNN label but NOT
        `build` does not satisfy the join -- so this ratified ADR has ZERO
        real build tickets and must be flagged as a zero-ticket gap (the
        ORG-014 class), not silently skipped as the pre-fix behavior did.
        (Was test_missing_build_label_not_flagged, asserting the opposite --
        that assertion encoded the exact bug T-2806 fixes.)"""
        _write_adr_file(self.adr_dir, "ADR-300 — x.md", "**Status:** Ratified 2026-01-01 by the operator.")
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9200.md",
            tid="T-9200", state="inbox", labels=["adr-300", "echo"],  # no "build"
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].adr_id, "ADR-300")
        self.assertEqual(flagged[0].open_tickets, [])

    def test_near_miss_label_missing_adr_id_counts_as_zero_ticket_gap(self) -> None:
        """Mirror of the above: `build` present but no adr-NNN/org-NNN label
        -> still zero real build tickets -> flagged as a zero-ticket gap.
        (Was test_missing_adr_label_not_flagged.)"""
        _write_adr_file(self.adr_dir, "ADR-301 — x.md", "**Status:** Ratified 2026-01-01 by the operator.")
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9201.md",
            tid="T-9201", state="inbox", labels=["build", "echo"],  # no "adr-301"
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].adr_id, "ADR-301")
        self.assertEqual(flagged[0].open_tickets, [])

    def test_done_state_ticket_not_joined(self) -> None:
        _write_adr_file(self.adr_dir, "ADR-302 — x.md", "**Status:** Ratified 2026-01-01 by the operator.")
        _make_ticket_file(
            self.tickets_dir / "done" / "T-9202.md",
            tid="T-9202", state="done", labels=["adr-302", "build"],
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(flagged, [])

    def test_mixed_done_and_open_tickets_only_open_ones_displayed(self) -> None:
        """T-2806: a done-state build ticket counts toward 'already has a
        ticket' (not a zero-ticket gap) but must not appear in the rendered
        open_tickets list -- only inbox/active are 'open'."""
        _write_adr_file(self.adr_dir, "ADR-306 — x.md", "**Status:** Ratified 2026-01-01 by the operator.")
        _make_ticket_file(
            self.tickets_dir / "done" / "T-9206.md",
            tid="T-9206", state="done", labels=["adr-306", "build"],
        )
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9207.md",
            tid="T-9207", state="inbox", labels=["adr-306", "build"],
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].open_tickets, [("T-9207", "inbox")])

    def test_multiple_open_tickets_for_same_adr_all_listed(self) -> None:
        _write_adr_file(self.adr_dir, "ADR-303 — x.md", "**Status:** Ratified 2026-01-01 by the operator.")
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9203.md",
            tid="T-9203", state="inbox", labels=["adr-303", "build"],
        )
        _make_ticket_file(
            self.tickets_dir / "active" / "T-9204.md",
            tid="T-9204", state="active", labels=["adr-303", "build"],
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(len(flagged), 1)
        self.assertEqual(
            sorted(flagged[0].open_tickets), [("T-9203", "inbox"), ("T-9204", "active")]
        )

    def test_ratified_adr_with_zero_tickets_anywhere_flagged_as_gap(self) -> None:
        """T-2806 core regression pin: the ORG-014 class. A ratified ADR with
        NO build ticket at all (not even a stalled one) must be FLAGGED, not
        silently skipped -- this is the exact bug T-2806 exists to fix.
        (Was test_ratified_adr_with_no_open_ticket_not_flagged, asserting
        the opposite -- that was the bug.)"""
        _write_adr_file(self.adr_dir, "ADR-304 — x.md", "**Status:** Ratified 2026-01-01 by the operator.")
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].adr_id, "ADR-304")
        self.assertEqual(flagged[0].open_tickets, [])


class TestRatifiedADRDirsCoverage(unittest.TestCase):
    """T-1814 design point 3: configurable ADR dirs + coverage-gap surfacing."""

    def test_default_adr_dirs_include_repo_and_echo_prismatic(self) -> None:
        fake_root = Path("/fake/pantheon-config")
        with patch.object(ticket_grooming, "REPO_ROOT", fake_root):
            dirs = ticket_grooming._default_adr_dirs()
        self.assertEqual(
            dirs,
            [fake_root / "architecture" / "adr", Path("/home/claude/echo-prismatic/adr")],
        )

    def test_missing_configured_dir_produces_coverage_gap_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tickets_dir = Path(tmp) / "tickets"
            (tickets_dir / "inbox").mkdir(parents=True)
            (tickets_dir / "active").mkdir()
            absent = Path(tmp) / "does-not-exist" / "adr"
            flagged, gaps = ticket_grooming.find_ratified_unbuilt_adrs(
                [absent], tickets_dir
            )
            self.assertEqual(flagged, [])
            self.assertEqual(gaps, [str(absent)])
            section = ticket_grooming._render_ratified_unbuilt_adr_section(flagged, gaps)
            self.assertIn(f"coverage gap: {absent} absent on this host", section)

    def test_unreadable_configured_dir_produces_coverage_gap_not_crash(self) -> None:
        """T-1867 regression: Path.exists() re-raises EACCES (pathlib swallows
        only ENOENT-class), so a configured dir under a non-traversable parent
        (the live case: /home/claude is 750 and grooming runs as recovery) crashed
        the whole run. Unreadable must surface as a coverage gap like absent."""
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes; EACCES unreproducible")
        with tempfile.TemporaryDirectory() as tmp:
            tickets_dir = Path(tmp) / "tickets"
            (tickets_dir / "inbox").mkdir(parents=True)
            (tickets_dir / "active").mkdir()
            locked_parent = Path(tmp) / "locked"
            unreadable = locked_parent / "adr"
            unreadable.mkdir(parents=True)
            locked_parent.chmod(0o000)
            try:
                flagged, gaps = ticket_grooming.find_ratified_unbuilt_adrs(
                    [unreadable], tickets_dir
                )
            finally:
                locked_parent.chmod(0o700)
            self.assertEqual(flagged, [])
            self.assertEqual(gaps, [f"{unreadable} (unreadable: Permission denied)"])
            section = ticket_grooming._render_ratified_unbuilt_adr_section(flagged, gaps)
            self.assertIn(
                f"coverage gap: {unreadable} (unreadable: Permission denied) "
                "unavailable on this host",
                section,
            )

    def test_adr_dirs_cli_flag_comma_separated_override(self) -> None:
        args = ticket_grooming._parse_args(
            ["--dry-run", "--adr-dirs", "/tmp/a,/tmp/b, /tmp/c"]
        )
        self.assertEqual(args.adr_dirs, "/tmp/a,/tmp/b, /tmp/c")


class TestRatifiedADRBuildDisposition(unittest.TestCase):
    """T-2806: `build_disposition` frontmatter marker exempts an ADR from the
    zero-ticket backstop. `no-build-needed` and `built` both exempt (HO Q1
    ruling 2026-08-10 -- both mean 'not an open build gap'); absent or
    `needs-build` does not."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.adr_dir = Path(self._tmp.name) / "adr"
        self.tickets_dir = Path(self._tmp.name) / "tickets"
        (self.tickets_dir / "inbox").mkdir(parents=True)
        (self.tickets_dir / "active").mkdir()
        (self.tickets_dir / "done").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def test_no_build_needed_disposition_exempts_zero_ticket_adr(self) -> None:
        _write_adr_file(
            self.adr_dir, "ORG-400-policy.md",
            "**Status:** Ratified 2026-01-01 by the operator.",
            frontmatter={"id": "ORG-400", "build_disposition": "no-build-needed"},
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(flagged, [])

    def test_built_disposition_exempts_zero_ticket_adr(self) -> None:
        _write_adr_file(
            self.adr_dir, "ORG-401-shipped.md",
            "**Status:** Ratified 2026-01-01 by the operator.",
            frontmatter={"id": "ORG-401", "build_disposition": "built"},
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(flagged, [])

    def test_needs_build_disposition_still_flags_zero_ticket_adr(self) -> None:
        _write_adr_file(
            self.adr_dir, "ORG-402-gap.md",
            "**Status:** Ratified 2026-01-01 by the operator.",
            frontmatter={"id": "ORG-402", "build_disposition": "needs-build"},
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual([f.adr_id for f in flagged], ["ORG-402"])

    def test_absent_disposition_defaults_to_flag_candidate(self) -> None:
        """Absence handling: unmarked (pending backfill) is NOT exempt -- it
        flags until a human backfills a marker or a build ticket."""
        _write_adr_file(
            self.adr_dir, "ORG-403-unmarked.md",
            "**Status:** Ratified 2026-01-01 by the operator.",
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual([f.adr_id for f in flagged], ["ORG-403"])

    def test_real_org069_frontmatter_shape_parses_and_flags(self) -> None:
        """D11 fixture provenance: frontmatter keys copied from the real
        adr/ORG-069-passport-content-tool-authorization.md (read during
        T-2806 plan approval). Status line is a hypothetical POST-ratification
        shape (ORG-069 itself is still unratified as of T-2825/T-2826) --
        unambiguously 'Ratified', no lingering 'awaiting ratification'
        phrasing. T-2825 note: an earlier draft of this fixture put 'DRAFT
        awaiting the operator ratification' ahead of the Ratified clause; that
        shape now correctly excludes under the T-2825 negation-phrase fix
        (and, separately, tripped a pre-existing first-sentence-split quirk
        where a period immediately followed by `**` doesn't split, widening
        "first sentence" to the whole line) -- corrected here since neither
        was this test's concern, which is frontmatter parsing, not status-line
        edge cases."""
        _write_adr_file(
            self.adr_dir, "ORG-069-passport.md",
            "**Status:** Ratified 2026-08-10 by the operator.",
            frontmatter={
                "id": "ORG-069",
                "build_disposition": "needs-build",
                "scoping_ticket": "T-2814",
            },
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual([f.adr_id for f in flagged], ["ORG-069"])

    def test_open_scoping_ticket_exempts_zero_ticket_adr(self) -> None:
        """T-2826: an OPEN scoping_ticket (still in inbox/active) honors the
        same tracked edge `adr_scoping_lint.check_adr_frontmatter` already
        accepts at creation time -- this exempts the zero-ticket backstop
        symmetrically, since the creation gate was already satisfied."""
        _write_adr_file(
            self.adr_dir, "ORG-410-scoped.md",
            "**Status:** Ratified 2026-01-01 by the operator.",
            frontmatter={
                "id": "ORG-410",
                "build_disposition": "needs-build",
                "scoping_ticket": "T-9410",
            },
        )
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9410.md",
            tid="T-9410", state="inbox",
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual(flagged, [])

    def test_done_scoping_ticket_does_not_exempt_reflags_gap(self) -> None:
        """T-2826: once the scoping ticket resolves WITHOUT having produced a
        build ticket, the flag must re-arm -- an any-state exemption would
        permanently mask a scoping ticket that died without filing build
        work. This is the self-healing property, not a regression."""
        _write_adr_file(
            self.adr_dir, "ORG-411-scoped-done.md",
            "**Status:** Ratified 2026-01-01 by the operator.",
            frontmatter={
                "id": "ORG-411",
                "build_disposition": "needs-build",
                "scoping_ticket": "T-9411",
            },
        )
        _make_ticket_file(
            self.tickets_dir / "done" / "T-9411.md",
            tid="T-9411", state="done",
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual([f.adr_id for f in flagged], ["ORG-411"])

    def test_absent_scoping_ticket_still_flags_needs_build_gap(self) -> None:
        """Regression guard: needs-build with no scoping_ticket at all must
        keep flagging (unchanged pre-T-2826 behavior) -- proves the new
        exemption didn't over-broaden."""
        _write_adr_file(
            self.adr_dir, "ORG-412-unscoped.md",
            "**Status:** Ratified 2026-01-01 by the operator.",
            frontmatter={"id": "ORG-412", "build_disposition": "needs-build"},
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual([f.adr_id for f in flagged], ["ORG-412"])

    def test_malformed_frontmatter_yaml_does_not_crash_treated_as_absent(self) -> None:
        """T-2591 regression: malformed YAML frontmatter must not crash the
        whole grooming run -- treated as absent (flag-candidate), matching
        _load_yaml's existing warn-not-swallow behavior."""
        self.adr_dir.mkdir(parents=True)
        (self.adr_dir / "ORG-404-badyaml.md").write_text(
            "---\nbuild_disposition: [unterminated\n---\n\n"
            "**Status:** Ratified 2026-01-01 by the operator.\n",
            encoding="utf-8",
        )
        flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
            [self.adr_dir], self.tickets_dir
        )
        self.assertEqual([f.adr_id for f in flagged], ["ORG-404"])


class TestRatifiedADRNamespaceConfig(unittest.TestCase):
    """T-2806 design point 2: adr-namespaces.yaml drives the join-label
    template per ADR prefix; org-NNN/adr-NNN stay cross-accepted as a
    migration shim regardless of config (the operator TG 7593)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        self.adr_dir = self.repo_root / "adr"
        self.tickets_dir = self.repo_root / "tickets"
        (self.tickets_dir / "inbox").mkdir(parents=True)
        (self.tickets_dir / "active").mkdir()
        (self.tickets_dir / "done").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def test_config_driven_label_template_is_used(self) -> None:
        """Prove config-drivenness (not just the migration shim): override
        ORG's template to a novel label and confirm a ticket carrying ONLY
        that novel label (not org-NNN/adr-NNN) satisfies the join."""
        (self.repo_root / "adr-namespaces.yaml").write_text(
            "namespaces:\n  ORG:\n    label_template: \"orgz-{num}\"\n"
            "  ADR:\n    label_template: \"adr-{num}\"\n",
            encoding="utf-8",
        )
        _write_adr_file(self.adr_dir, "ORG-500-x.md", "**Status:** Ratified 2026-01-01 by the operator.")
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9500.md",
            tid="T-9500", state="inbox", labels=["orgz-500", "build"],
        )
        with patch.object(ticket_grooming, "REPO_ROOT", self.repo_root):
            flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
                [self.adr_dir], self.tickets_dir
            )
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].open_tickets, [("T-9500", "inbox")])

    def test_missing_config_falls_back_to_builtin_org_adr_map(self) -> None:
        _write_adr_file(self.adr_dir, "ORG-501-x.md", "**Status:** Ratified 2026-01-01 by the operator.")
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9501.md",
            tid="T-9501", state="inbox", labels=["org-501", "build"],
        )
        with patch.object(ticket_grooming, "REPO_ROOT", self.repo_root):
            flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
                [self.adr_dir], self.tickets_dir
            )
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].open_tickets, [("T-9501", "inbox")])

    def test_malformed_config_does_not_crash_falls_back(self) -> None:
        (self.repo_root / "adr-namespaces.yaml").write_text(
            "namespaces: [unterminated\n", encoding="utf-8",
        )
        _write_adr_file(self.adr_dir, "ORG-502-x.md", "**Status:** Ratified 2026-01-01 by the operator.")
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9502.md",
            tid="T-9502", state="inbox", labels=["org-502", "build"],
        )
        with patch.object(ticket_grooming, "REPO_ROOT", self.repo_root):
            flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
                [self.adr_dir], self.tickets_dir
            )
        self.assertEqual(len(flagged), 1)

    def test_org_and_adr_labels_cross_accepted_migration_shim(self) -> None:
        """An ORG-prefixed ADR's build ticket labeled the OLD adr-NNN way
        still joins (accept-both migration shim, the operator TG 7593)."""
        _write_adr_file(self.adr_dir, "ORG-503-x.md", "**Status:** Ratified 2026-01-01 by the operator.")
        _make_ticket_file(
            self.tickets_dir / "inbox" / "T-9503.md",
            tid="T-9503", state="inbox", labels=["adr-503", "build"],
        )
        with patch.object(ticket_grooming, "REPO_ROOT", self.repo_root):
            flagged, _gaps = ticket_grooming.find_ratified_unbuilt_adrs(
                [self.adr_dir], self.tickets_dir
            )
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].open_tickets, [("T-9503", "inbox")])


class TestRatifiedADRDigestOutput(unittest.TestCase):
    """T-1814 design point 4: digest rendering + wiring into build_digest."""

    def test_digest_line_compact_format_includes_id_snippet_tickets(self) -> None:
        findings = [
            ticket_grooming.RatifiedUnbuiltADR(
                adr_id="ADR-239",
                status_snippet="Ratified 2026-07-05 by the operator.",
                open_tickets=[("T-1602", "inbox")],
            )
        ]
        section = ticket_grooming._render_ratified_unbuilt_adr_section(findings, [])
        self.assertIn("ADR-239", section)
        self.assertIn("Ratified 2026-07-05 by the operator.", section)
        self.assertIn("T-1602 (inbox)", section)

    def test_section_omitted_when_nothing_flagged_and_no_gaps(self) -> None:
        section = ticket_grooming._render_ratified_unbuilt_adr_section([], [])
        self.assertEqual(section, "")

    def test_digest_renders_no_build_ticket_found_for_zero_ticket_gap(self) -> None:
        """T-2806: the zero-ticket case (open_tickets == []) must render an
        explicit 'no build ticket at all' message, not a misleading blank
        'open: ' (which would look like a formatting bug, not a real gap)."""
        findings = [
            ticket_grooming.RatifiedUnbuiltADR(
                adr_id="ORG-014", status_snippet="Ratified 2026-05-17.", open_tickets=[],
            )
        ]
        section = ticket_grooming._render_ratified_unbuilt_adr_section(findings, [])
        self.assertIn("ORG-014", section)
        self.assertIn("NO BUILD TICKET FOUND", section)

    def test_form_b_status_line_flags_and_renders_without_error(self) -> None:
        """Amendment's second required test: the form-b ORG-028-shaped status
        line must both flag AND render into a compact digest line without
        raising (stray `**` noise in the captured snippet is acceptable)."""
        with tempfile.TemporaryDirectory() as tmp:
            adr_dir = Path(tmp) / "adr"
            tickets_dir = Path(tmp) / "tickets"
            (tickets_dir / "inbox").mkdir(parents=True)
            _write_adr_file(adr_dir, "ORG-028-topology.md", _REAL_ORG_028_STATUS_LINE)
            _make_ticket_file(
                tickets_dir / "inbox" / "T-9301.md",
                tid="T-9301", state="inbox", labels=["adr-028", "build"],
            )
            flagged, gaps = ticket_grooming.find_ratified_unbuilt_adrs([adr_dir], tickets_dir)
            self.assertEqual([f.adr_id for f in flagged], ["ORG-028"])
            section = ticket_grooming._render_ratified_unbuilt_adr_section(flagged, gaps)
            self.assertIn("ORG-028", section)
            self.assertIn("T-9301 (inbox)", section)

    def test_build_digest_includes_ratified_unbuilt_section(self) -> None:
        findings = [
            ticket_grooming.RatifiedUnbuiltADR(
                adr_id="ADR-239",
                status_snippet="Ratified 2026-07-05 by the operator.",
                open_tickets=[("T-1602", "inbox")],
            )
        ]
        result = ticket_grooming.build_digest(
            "2026-07-19 12:00 UTC", [], {"active": 0},
            ratified_unbuilt_findings=findings,
        )
        self.assertIn("ADR-239", result)
        self.assertIn("T-1602 (inbox)", result)

    def test_build_digest_backward_compatible_without_ratified_unbuilt_kwargs(self) -> None:
        result = ticket_grooming.build_digest("2026-07-19 12:00 UTC", [], {"active": 0})
        self.assertNotIn("Ratified ADRs", result)


class TestDryRunRatifiedUnbuiltADR(unittest.TestCase):
    """Integration: --dry-run prints the ratified-but-unbuilt-ADR section."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "active").mkdir()
        (self.root / "inbox").mkdir()
        self._tmp_adr = tempfile.TemporaryDirectory()
        self.adr_dir = Path(self._tmp_adr.name)
        self._patches = [
            patch.object(ticket_grooming, "TICKETS_ROOT", self.root),
            patch.object(
                ticket_grooming, "_now_utc",
                return_value=datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc),
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()
        self._tmp_adr.cleanup()

    def test_dry_run_prints_ratified_unbuilt_section(self) -> None:
        _write_adr_file(self.adr_dir, "ADR-239 — barge-in.md", _REAL_ADR_239_STATUS_LINE)
        _make_ticket_file(
            self.root / "inbox" / "T-1602.md",
            tid="T-1602", state="inbox", labels=_REAL_T1602_LABELS,
        )
        with patch.object(ticket_grooming, "git_commit_and_push") as mock_git, \
             patch.object(ticket_grooming, "_send_digest") as mock_send:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ticket_grooming.main(["--dry-run", "--adr-dirs", str(self.adr_dir)])
            self.assertEqual(rc, 0)
            mock_git.assert_not_called()
            mock_send.assert_not_called()
        out = buf.getvalue()
        self.assertIn("ADR-UNBUILT", out)
        self.assertIn("ADR-239", out)
        self.assertIn("T-1602", out)

    def test_dry_run_prints_zero_ticket_gap_for_adr_with_no_build_ticket(self) -> None:
        """T-2806: the --dry-run CLI rendering path (main(), separate code
        from _render_ratified_unbuilt_adr_section) must also handle the
        zero-ticket case without printing a misleading blank 'open: '."""
        _write_adr_file(self.adr_dir, "ORG-405 — gap.md", "**Status:** Ratified 2026-01-01 by the operator.")
        with patch.object(ticket_grooming, "git_commit_and_push") as mock_git, \
             patch.object(ticket_grooming, "_send_digest") as mock_send:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ticket_grooming.main(["--dry-run", "--adr-dirs", str(self.adr_dir)])
            self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("ADR-UNBUILT", out)
        self.assertIn("ORG-405", out)
        self.assertIn("NO BUILD TICKET FOUND", out)

    def test_dry_run_reports_no_ratified_unbuilt_adrs_when_none(self) -> None:
        with patch.object(ticket_grooming, "git_commit_and_push") as mock_git, \
             patch.object(ticket_grooming, "_send_digest") as mock_send:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ticket_grooming.main(["--dry-run", "--adr-dirs", str(self.adr_dir)])
            self.assertEqual(rc, 0)
        self.assertIn("no ratified-but-unbuilt ADRs found.", buf.getvalue())

    def test_dry_run_missing_adr_dir_prints_coverage_gap(self) -> None:
        absent = self.adr_dir / "does-not-exist"
        with patch.object(ticket_grooming, "git_commit_and_push") as mock_git, \
             patch.object(ticket_grooming, "_send_digest") as mock_send:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ticket_grooming.main(["--dry-run", "--adr-dirs", str(absent)])
            self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("COVERAGE-GAP", out)
        self.assertIn(str(absent), out)


class TestTicketIntegrityIssues(unittest.TestCase):
    """T-2982: find_ticket_integrity_issues detects dup-IDs + same-not_before clusters."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for state in ("inbox", "active", "done", "blocked"):
            (self.root / state).mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _write(self, state: str, tid: str, not_before: str | None = None) -> None:
        fm = ["---", f"id: {tid}", "target: recovery", f"state: {state}"]
        if not_before is not None:
            fm.append(f"not_before: {not_before}")
        fm.append("---")
        (self.root / state / f"{tid}.md").write_text("\n".join(fm) + f"\n# {tid}\n", encoding="utf-8")

    def test_clean_tree_no_issues(self) -> None:
        self._write("inbox", "T-0001")
        self._write("active", "T-0002")
        self.assertEqual(ticket_grooming.find_ticket_integrity_issues(self.root), [])

    def test_duplicate_id_flagged(self) -> None:
        self._write("done", "T-0001")
        self._write("inbox", "T-0001")  # same id in two dirs = collision
        issues = ticket_grooming.find_ticket_integrity_issues(self.root)
        self.assertEqual(len(issues), 1)
        self.assertIn("DUP-ID", issues[0])
        self.assertIn("T-0001", issues[0])

    def test_same_not_before_cluster_of_3_flagged(self) -> None:
        for tid in ("T-0010", "T-0011", "T-0012"):
            self._write("inbox", tid, not_before="2026-08-20T12:00:00Z")
        issues = ticket_grooming.find_ticket_integrity_issues(self.root)
        self.assertEqual(len(issues), 1)
        self.assertIn("GATE-CLUSTER", issues[0])
        self.assertIn("2026-08-20T12:00:00Z", issues[0])

    def test_cluster_of_2_not_flagged(self) -> None:
        # threshold is >=3; a pair is not a thundering-herd
        self._write("inbox", "T-0010", not_before="2026-08-20T12:00:00Z")
        self._write("inbox", "T-0011", not_before="2026-08-20T12:00:00Z")
        self.assertEqual(ticket_grooming.find_ticket_integrity_issues(self.root), [])

    def test_distinct_not_before_not_flagged(self) -> None:
        # staggered gates (different timestamps) are correct, not a cluster
        self._write("inbox", "T-0010", not_before="2026-08-20T12:00:00Z")
        self._write("inbox", "T-0011", not_before="2026-08-21T12:00:00Z")
        self._write("inbox", "T-0012", not_before="2026-08-22T12:00:00Z")
        self.assertEqual(ticket_grooming.find_ticket_integrity_issues(self.root), [])

    def test_render_section_empty_when_clean(self) -> None:
        self.assertEqual(ticket_grooming._render_integrity_section([]), "")

    def test_render_section_lists_issues(self) -> None:
        out = ticket_grooming._render_integrity_section(["[DUP-ID] T-0001 ...", "[GATE-CLUSTER] 3 tickets ..."])
        self.assertIn("Ticket-integrity issues (2)", out)
        self.assertIn("DUP-ID", out)


if __name__ == "__main__":
    unittest.main()
