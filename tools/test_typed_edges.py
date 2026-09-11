"""Tests for the T-2841 / ORG-065 BT1 typed-graph model (warn-only).

Run from repo root:
    python3 -m unittest tools.test_typed_edges -v

Covers:
  - tools/ticket.py: the four new typed-edge frontmatter fields (implements /
    verifies / supersedes / informs) — shape validation, schema round-trip,
    cmd_file CLI wiring, and cycle-rejection-at-insertion for both cmd_file
    and cmd_block (ORG-065 §3.6, the binding acyclicity invariant).
  - tools/ticket_lint.py: the generalized typed-edge graph builder
    (edge_node_id / build_typed_edge_graph / find_path / _find_all_cycles),
    the ADR `elements:` block parser (scan_adr_elements), and the corpus-wide
    report-only scan (detect_typed_graph_issues).

Fixture-string discipline: no test constructs the contiguous unescaped HERMES
trigger bytes. Per /home/claude/CLAUDE.md "HERMES\\.md byte-sequence
discipline" (escape form in prose, mentally drop the backslash) — moot here
since nothing in this module references that filename at all, but noted per
the dispatch brief's standing instruction to workers.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import ticket, ticket_lint


# ---------------------------------------------------------------------------
# Fixture helpers (local to this module — mirrors the per-file-local-helper
# convention already used by test_ticket.py's _write_ticket and
# test_ticket_lint.py's _make_ticket, rather than importing across test
# modules).
# ---------------------------------------------------------------------------


def _write_ticket(
    path: Path,
    *,
    tid: str,
    state: str = "inbox",
    target: str = "recovery",
    requester: str = "operator",
    created: str = "2026-06-08T00:00:00Z",
    depends_on: list[str] | None = None,
    parent_ticket: str | None = None,
    implements: list[str] | None = None,
    verifies: list[str] | None = None,
    supersedes: list[str] | None = None,
    informs: list[str] | None = None,
    ratifies: list[str] | None = None,
) -> None:
    """Write a minimal, well-formed ticket file — including the T-2841 typed-
    edge fields when given — directly to disk (bypassing ticket.py's own
    insertion-time validation, which is the point for fixtures that need to
    pre-seed a forward-reference or a hand-edited/legacy defect)."""
    lines = [
        "---",
        f"id: {tid}",
        f"created: {created}",
        f"requester: {requester}",
        f"target: {target}",
        f"state: {state}",
    ]
    lines.append(f"parent_ticket: {parent_ticket}" if parent_ticket else "parent_ticket: null")
    lines.append(f"depends_on: [{', '.join(depends_on)}]" if depends_on else "depends_on: []")
    if implements:
        lines.append(f"implements: [{', '.join(implements)}]")
    if verifies:
        lines.append(f"verifies: [{', '.join(verifies)}]")
    if supersedes:
        lines.append(f"supersedes: [{', '.join(supersedes)}]")
    if informs:
        lines.append(f"informs: [{', '.join(informs)}]")
    if ratifies:
        lines.append(f"ratifies: [{', '.join(ratifies)}]")
    lines.append("---")
    body = (
        f"\n# Test ticket {tid}\n\n"
        "## Ask\n\nx.\n\n"
        "## Transition log\n\n"
        f"- {created} [{requester}] filed\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + body, encoding="utf-8")


def _write_adr(path: Path, *, adr_id: str, elements_block: str | None) -> None:
    """Write a minimal ADR file with an optional literal `elements:` YAML
    block (caller supplies the pre-indented block body so tests can exercise
    both well-formed and malformed shapes)."""
    fm = ["---", f"id: {adr_id}", f"title: Test ADR {adr_id}"]
    if elements_block is not None:
        fm.append("elements:")
        fm.append(elements_block)
    fm.append("---")
    body = f"\n# {adr_id} — test ADR\n\nBody text.\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm) + body, encoding="utf-8")


def _make_file_args(**overrides) -> MagicMock:
    """A fully-populated cmd_file MagicMock args fixture, extended with the
    T-2841 typed-edge flags. Mirrors test_ticket.py's TestRatifiableFilesField
    pattern (every attribute cmd_file may touch is explicitly set — a MagicMock
    auto-vivifies unset attributes, which is exactly the trap _str_arg guards
    against; this fixture still sets them all explicitly for clarity)."""
    args = MagicMock()
    args.requester = "operator"
    args.target = "recovery"
    args.title = "typed-edge test ticket"
    args.labels = None
    args.depends_on = None
    args.stale_after = None
    args.not_before = None
    args.not_before_anchor = None
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
    args.retest_status = None
    args.implements = None
    args.verifies = None
    args.supersedes = None
    args.informs = None
    args.ratifies = None  # T-3476 / ORG-065 Call 1
    # T-3328: explicit default (None), matching this fixture's own "every
    # attribute explicit" convention. Most tests here already set a real
    # typed edge via overrides; the one test that deliberately omits every
    # edge overrides this to a real orphan justification (see
    # test_fields_omitted_when_flags_absent).
    args.orphan = None
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


class _TmpTicketsRootMixin:
    """setUp/tearDown for a tmp TICKETS_ROOT with inbox/active/done, patched
    onto tools.ticket.TICKETS_ROOT for the duration of the test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for st in ticket.STATES:
            (self.root / "tickets" / st).mkdir(parents=True)
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root / "tickets")
        self._tickets_patch.start()

    def tearDown(self) -> None:
        self._tickets_patch.stop()
        self._tmp.cleanup()

    @property
    def tickets_root(self) -> Path:
        return self.root / "tickets"


# ---------------------------------------------------------------------------
# ticket.py: _parse_edge_targets shape validation.
# ---------------------------------------------------------------------------


class TestEdgeTargetShape(unittest.TestCase):
    def test_ticket_id_accepted(self) -> None:
        self.assertEqual(ticket._parse_edge_targets("implements", ["T-1234"]), ["T-1234"])

    def test_obligation_element_id_accepted(self) -> None:
        self.assertEqual(
            ticket._parse_edge_targets("implements", ["ORG-065#E2"]), ["ORG-065#E2"]
        )

    def test_obligation_clause_id_accepted(self) -> None:
        self.assertEqual(
            ticket._parse_edge_targets("verifies", ["ORG-065#E2.C1"]), ["ORG-065#E2.C1"]
        )

    def test_adr_prefix_accepted(self) -> None:
        self.assertEqual(
            ticket._parse_edge_targets("implements", ["ADR-062#E3.C2"]), ["ADR-062#E3.C2"]
        )

    def test_mixed_list_accepted(self) -> None:
        # The plan's own worked example: implements: [ORG-065#E2.C1, T-2840].
        result = ticket._parse_edge_targets("implements", ["ORG-065#E2.C1", "T-2840"])
        self.assertEqual(result, ["ORG-065#E2.C1", "T-2840"])

    def test_malformed_rejected(self) -> None:
        # T-3215: "T-12345" dropped from this list — under the widened
        # _EDGE_TARGET_RE (4+ digits) it is now well-formed (see
        # test_five_digit_ticket_id_accepted below); "T-123" (3 digits) stays
        # malformed and keeps exercising the too-short boundary.
        bad = ["garbage", "T-123", "ORG-065", "ORG-065#X1",
               "org-065#e1", "ORG-065#E1.X1", ""]
        for b in bad:
            with self.subTest(b=b):
                with self.assertRaises(ticket.TicketError):
                    ticket._parse_edge_targets("implements", [b])

    def test_five_digit_ticket_id_accepted(self) -> None:
        """T-3215: an edge target must accept a 5+ digit ticket id as
        well-formed (existence is a separate check, not this function's job)."""
        five_digit_id = "T-" + "12345"
        self.assertEqual(
            ticket._parse_edge_targets("implements", [five_digit_id]), [five_digit_id]
        )

    def test_empty_entry_rejected_with_whitespace(self) -> None:
        with self.assertRaises(ticket.TicketError):
            ticket._parse_edge_targets("verifies", ["   "])

    def test_ratifies_target_regex(self) -> None:
        """T-3476: `ratifies` is validated by the SAME `_EDGE_TARGET_RE` as
        the other four fields (no new regex needed — `E0` already matches
        `E\\d+`). Well-formed whole-ADR and element targets accepted;
        a bare ADR id (no `#E<k>`) stays rejected, same as for implements."""
        self.assertEqual(
            ticket._parse_edge_targets("ratifies", ["ORG-062#E0"]), ["ORG-062#E0"]
        )
        self.assertEqual(
            ticket._parse_edge_targets("ratifies", ["ORG-070#E1", "ORG-070#E10"]),
            ["ORG-070#E1", "ORG-070#E10"],
        )
        with self.assertRaises(ticket.TicketError):
            ticket._parse_edge_targets("ratifies", ["ORG-062"])


# ---------------------------------------------------------------------------
# ticket.py: schema round-trip.
# ---------------------------------------------------------------------------


class TestSchemaRoundTrip(unittest.TestCase):
    def test_all_four_fields_roundtrip(self) -> None:
        text = (
            "---\n"
            "id: T-0001\n"
            "created: 2026-06-08T00:00:00Z\n"
            "requester: operator\n"
            "target: recovery\n"
            "state: inbox\n"
            "implements: [ORG-065#E2.C1, T-2840]\n"
            "verifies: [ORG-065#E3.C2]\n"
            "supersedes: [T-1234]\n"
            "informs: [T-2027]\n"
            "---\n"
            "\n"
            "# title\n"
        )
        fields, _body = ticket.parse_front_matter(text)
        self.assertEqual(fields["implements"], ["ORG-065#E2.C1", "T-2840"])
        self.assertEqual(fields["verifies"], ["ORG-065#E3.C2"])
        self.assertEqual(fields["supersedes"], ["T-1234"])
        self.assertEqual(fields["informs"], ["T-2027"])

        serialized = ticket.serialize_front_matter(fields)
        self.assertIn("implements: [ORG-065#E2.C1, T-2840]", serialized)
        self.assertIn("verifies: [ORG-065#E3.C2]", serialized)
        self.assertIn("supersedes: [T-1234]", serialized)
        self.assertIn("informs: [T-2027]", serialized)

        fields2, _ = ticket.parse_front_matter(serialized + "\n")
        self.assertEqual(fields2["implements"], fields["implements"])
        self.assertEqual(fields2["verifies"], fields["verifies"])
        self.assertEqual(fields2["supersedes"], fields["supersedes"])
        self.assertEqual(fields2["informs"], fields["informs"])

    def test_fields_absent_by_default_no_data_churn(self) -> None:
        """A ticket that never set the new fields round-trips with ZERO
        change — the blast-radius-control property from the ADR (§5:
        "zero existing tickets change at land-time")."""
        text = (
            "---\n"
            "id: T-0002\n"
            "created: 2026-06-08T00:00:00Z\n"
            "requester: operator\n"
            "target: recovery\n"
            "state: inbox\n"
            "depends_on: []\n"
            "---\n"
            "\n"
            "# title\n"
        )
        fields, _body = ticket.parse_front_matter(text)
        for f in ("implements", "verifies", "supersedes", "informs", "ratifies"):
            self.assertNotIn(f, fields)
        serialized = ticket.serialize_front_matter(fields)
        for f in ("implements:", "verifies:", "supersedes:", "informs:", "ratifies:"):
            self.assertNotIn(f, serialized)

    def test_ratifies_field_roundtrips(self) -> None:
        """T-3476: `ratifies` round-trips through parse -> serialize -> parse,
        same shape as the four T-2841 fields — distinct frontmatter key from
        `implements` (the operator's invariant: a ratifies edge is never an
        implements edge, starting at the schema level)."""
        text = (
            "---\n"
            "id: T-0003\n"
            "created: 2026-06-08T00:00:00Z\n"
            "requester: operator\n"
            "target: recovery\n"
            "state: inbox\n"
            "ratifies: [ORG-070#E1, ORG-062#E0]\n"
            "---\n"
            "\n"
            "# title\n"
        )
        fields, _body = ticket.parse_front_matter(text)
        self.assertEqual(fields["ratifies"], ["ORG-070#E1", "ORG-062#E0"])
        self.assertNotIn("implements", fields)
        serialized = ticket.serialize_front_matter(fields)
        self.assertIn("ratifies: [ORG-070#E1, ORG-062#E0]", serialized)
        fields2, _ = ticket.parse_front_matter(serialized + "\n")
        self.assertEqual(fields2["ratifies"], fields["ratifies"])


# ---------------------------------------------------------------------------
# ticket.py: cmd_file CLI wiring for the four new flags.
# ---------------------------------------------------------------------------


class TestCmdFileTypedEdgeFlags(_TmpTicketsRootMixin, unittest.TestCase):
    def _file(self, **overrides):
        with (
            patch("tools.ticket._next_ticket_id", return_value="T-0099"),
            patch("tools.ticket._pull_if_needed"),
            patch("tools.ticket._commit_and_push"),
        ):
            args = _make_file_args(**overrides)
            rc = ticket.cmd_file(args)
        return rc

    def test_all_four_flags_land_in_frontmatter(self) -> None:
        rc = self._file(
            implements="ORG-065#E2.C1,T-2840",
            verifies="ORG-065#E3.C2",
            supersedes="T-1234",
            informs="T-2027",
            appetite="5",  # T-3440: a non-vault-ADR implements edge is a bet
        )
        self.assertEqual(rc, 0)
        text = (self.tickets_root / "inbox" / "T-0099.md").read_text(encoding="utf-8")
        self.assertIn("implements: [ORG-065#E2.C1, T-2840]", text)
        self.assertIn("verifies: [ORG-065#E3.C2]", text)
        self.assertIn("supersedes: [T-1234]", text)
        self.assertIn("informs: [T-2027]", text)

    def test_fields_omitted_when_flags_absent(self) -> None:
        # T-3328: this test deliberately omits every typed edge (that's the
        # thing under test) — the file-time lineage guard needs the orphan
        # escape hatch, not a real edge (a real edge would defeat the
        # "fields absent" assertion below).
        rc = self._file(orphan="test fixture: T-2841 typed-edge omission (no flags passed)")
        self.assertEqual(rc, 0)
        text = (self.tickets_root / "inbox" / "T-0099.md").read_text(encoding="utf-8")
        for f in ("implements:", "verifies:", "supersedes:", "informs:", "ratifies:"):
            self.assertNotIn(f, text)

    def test_malformed_target_refuses_and_writes_no_file(self) -> None:
        with self.assertRaises(ticket.TicketError):
            self._file(implements="not-a-valid-target")
        self.assertEqual(list((self.tickets_root / "inbox").iterdir()), [])

    def test_ratifies_flag_lands_in_frontmatter(self) -> None:
        """T-3476: `--ratifies` wires through cmd_file the same way the four
        T-2841 flags do — a comma-separated CSV of edge targets lands as a
        flow-list frontmatter field."""
        rc = self._file(
            ratifies="ORG-062#E0",
            appetite="5",  # T-3440: a non-vault-ADR ratifies edge is a bet
        )
        self.assertEqual(rc, 0)
        text = (self.tickets_root / "inbox" / "T-0099.md").read_text(encoding="utf-8")
        self.assertIn("ratifies: [ORG-062#E0]", text)

    def test_ratifies_malformed_target_refuses(self) -> None:
        with self.assertRaises(ticket.TicketError):
            self._file(ratifies="ORG-062")  # bare ADR id, no #E<k> — malformed
        self.assertEqual(list((self.tickets_root / "inbox").iterdir()), [])


# ---------------------------------------------------------------------------
# ticket.py: cycle-rejection-at-insertion (ORG-065 §3.6), cmd_file.
# ---------------------------------------------------------------------------


class TestCmdFileCycleRejection(_TmpTicketsRootMixin, unittest.TestCase):
    def _file(self, **overrides):
        with (
            patch("tools.ticket._next_ticket_id", return_value="T-0099"),
            patch("tools.ticket._pull_if_needed"),
            patch("tools.ticket._commit_and_push") as mock_commit,
        ):
            args = _make_file_args(**overrides)
            rc = ticket.cmd_file(args)
        return rc, mock_commit

    def test_depends_on_forward_reference_cycle_rejected(self) -> None:
        # T-0050 already depends_on the not-yet-allocated T-0099 (a legal
        # forward-reference). Filing T-0099 with --depends-on T-0050 closes
        # the loop: T-0099 -> T-0050 -> T-0099.
        _write_ticket(self.tickets_root / "inbox" / "T-0050.md",
                       tid="T-0050", depends_on=["T-0099"])
        with self.assertRaises(ticket.TicketError) as ctx:
            self._file(depends_on="T-0050")
        self.assertIn("cycle", str(ctx.exception).lower())
        self.assertEqual(list((self.tickets_root / "inbox").glob("T-0099*")), [])

    def test_self_referential_depends_on_rejected(self) -> None:
        with self.assertRaises(ticket.TicketError) as ctx:
            self._file(depends_on="T-0099")
        self.assertIn("self-referential", str(ctx.exception))

    def test_implements_and_verifies_same_domain_cycle_rejected(self) -> None:
        # T-0060 implements T-0099. Filing T-0099 with --verifies T-0060
        # closes a SAME-domain loop (implements/verifies/depends_on/supersedes
        # are ONE unioned acyclicity domain per ACYCLICITY_DOMAINS):
        # T-0099 -verifies-> T-0060 -implements-> T-0099.
        _write_ticket(self.tickets_root / "inbox" / "T-0060.md",
                       tid="T-0060", implements=["T-0099"])
        with self.assertRaises(ticket.TicketError) as ctx:
            self._file(verifies="T-0060")
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_depends_on_and_implements_mixed_type_2cycle_rejected(self) -> None:
        """Round-2 correction: depends_on/implements/verifies/supersedes share
        ONE unioned acyclicity domain (only parent_of is its own, exempt-from-
        the-union domain — see ticket_lint.py's module note). A MIXED-type
        loop across depends_on+implements is therefore a real defect, not a
        false positive: T-0060 implements T-0099; filing T-0099 with
        --depends-on T-0060 closes T-0099 -depends_on-> T-0060 -implements->
        T-0099. (Reviewer-identified blind spot in the interim 4-way-split
        design; this is the regression test for the fix.)"""
        _write_ticket(self.tickets_root / "inbox" / "T-0060.md",
                       tid="T-0060", implements=["T-0099"])
        with self.assertRaises(ticket.TicketError) as ctx:
            self._file(depends_on="T-0060")
        self.assertIn("cycle", str(ctx.exception).lower())
        self.assertEqual(list((self.tickets_root / "inbox").glob("T-0099*")), [])

    def test_depends_on_and_parent_of_cross_domain_not_a_cycle(self) -> None:
        """The LIVE-corpus shape itself (found 15 times on the wet-run): a
        parent depends_on its own child, whose parent_ticket correctly names
        the parent. parent_of is the ONE edge type kept out of the unioned
        domain (the ADR itself marks it behaviorless — "no cascade, no
        validation", same framing as the informs exemption) — this must keep
        passing or the guard is unusable for the most common ticket-filing
        idiom in the system."""
        _write_ticket(self.tickets_root / "inbox" / "T-0065.md",
                       tid="T-0065", parent_ticket="T-0099")
        rc, mock_commit = self._file(depends_on="T-0065")
        self.assertEqual(rc, 0)
        mock_commit.assert_called_once()

    def test_parent_ticket_cycle_rejected(self) -> None:
        # T-0070's parent is the not-yet-allocated T-0099. Filing T-0099 with
        # --parent-ticket T-0070 closes the parent_of loop.
        _write_ticket(self.tickets_root / "inbox" / "T-0070.md",
                       tid="T-0070", parent_ticket="T-0099")
        with self.assertRaises(ticket.TicketError) as ctx:
            self._file(parent_ticket="T-0070")
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_ratifies_and_implements_same_domain_cycle_rejected(self) -> None:
        """T-3476: `ratifies` joins the SAME unioned acyclicity domain as
        depends_on/implements/verifies/supersedes (not its own domain like
        parent_of) — a mixed ratifies+implements loop is a real defect.
        T-0080 implements T-0099; filing T-0099 with --ratifies T-0080
        closes T-0099 -ratifies-> T-0080 -implements-> T-0099."""
        _write_ticket(self.tickets_root / "inbox" / "T-0080.md",
                       tid="T-0080", implements=["T-0099"])
        with self.assertRaises(ticket.TicketError) as ctx:
            self._file(ratifies="T-0080")
        self.assertIn("cycle", str(ctx.exception).lower())
        self.assertEqual(list((self.tickets_root / "inbox").glob("T-0099*")), [])

    def test_supersedes_cycle_rejected(self) -> None:
        _write_ticket(self.tickets_root / "inbox" / "T-0080.md",
                       tid="T-0080", supersedes=["T-0099"])
        with self.assertRaises(ticket.TicketError) as ctx:
            self._file(supersedes="T-0080")
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_depends_on_and_supersedes_mixed_type_cycle_rejected(self) -> None:
        """The same unioned-domain coverage as the implements+depends_on
        2-cycle, exercised for the remaining pairing (supersedes+depends_on).
        T-0075 supersedes T-0099; filing T-0099 with --depends-on T-0075
        closes T-0099 -depends_on-> T-0075 -supersedes-> T-0099."""
        _write_ticket(self.tickets_root / "inbox" / "T-0075.md",
                       tid="T-0075", supersedes=["T-0099"])
        with self.assertRaises(ticket.TicketError) as ctx:
            self._file(depends_on="T-0075")
        self.assertIn("cycle", str(ctx.exception).lower())
        self.assertEqual(list((self.tickets_root / "inbox").glob("T-0099*")), [])

    def test_informs_is_acyclicity_exempt(self) -> None:
        # T-0090 depends_on the not-yet-allocated T-0099. Filing T-0099 with
        # --informs T-0090 is topologically a loop through informs+depends_on,
        # but informs must NEVER be checked (advisory, exempt per §3.6) — the
        # file succeeds.
        _write_ticket(self.tickets_root / "inbox" / "T-0090.md",
                       tid="T-0090", depends_on=["T-0099"])
        rc, mock_commit = self._file(informs="T-0090")
        self.assertEqual(rc, 0)
        mock_commit.assert_called_once()
        text = (self.tickets_root / "inbox" / "T-0099.md").read_text(encoding="utf-8")
        self.assertIn("informs: [T-0090]", text)

    def test_clean_no_cycle_succeeds(self) -> None:
        _write_ticket(self.tickets_root / "inbox" / "T-0040.md", tid="T-0040")
        rc, mock_commit = self._file(depends_on="T-0040", implements="ORG-065#E1",
                                     appetite="5")  # T-3440: ADR edge is a bet
        self.assertEqual(rc, 0)
        mock_commit.assert_called_once()


# ---------------------------------------------------------------------------
# ticket.py: cycle-rejection-at-insertion, cmd_block (the ADR's other named
# insertion point: "depends_on via block").
# ---------------------------------------------------------------------------


class TestCmdBlockCycleRejection(_TmpTicketsRootMixin, unittest.TestCase):
    def test_depends_on_cycle_via_block_rejected(self) -> None:
        _write_ticket(self.tickets_root / "inbox" / "T-0010.md", tid="T-0010")
        _write_ticket(self.tickets_root / "inbox" / "T-0011.md",
                       tid="T-0011", depends_on=["T-0010"])
        original = (self.tickets_root / "inbox" / "T-0010.md").read_text(encoding="utf-8")

        args = _make_block_args(ticket="T-0010", depends_on=["T-0011"])
        with (
            patch("tools.ticket._pull_if_needed"),
            patch("tools.ticket._commit_and_push") as mock_commit,
        ):
            with self.assertRaises(ticket.TicketError) as ctx:
                ticket.cmd_block(args)
        self.assertIn("cycle", str(ctx.exception).lower())
        mock_commit.assert_not_called()
        # The ticket file must be untouched — the rejection happens before
        # the frontmatter edit is written to disk.
        after = (self.tickets_root / "inbox" / "T-0010.md").read_text(encoding="utf-8")
        self.assertEqual(original, after)


def _make_block_args(**overrides) -> MagicMock:
    args = MagicMock()
    args.ticket = None
    args.not_before = None
    args.not_before_anchor = None
    args.depends_on = None
    args.clear_not_before = False
    args.clear_dep = None
    args.clear_deps = False
    args.no_push = True
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


# ---------------------------------------------------------------------------
# ticket_lint.py: the generalized typed-edge graph primitives.
# ---------------------------------------------------------------------------


class TestEdgeNodeId(unittest.TestCase):
    def test_ticket_id_classified_as_ticket(self) -> None:
        self.assertEqual(ticket_lint.edge_node_id("T-1234"), ("ticket", "T-1234"))

    def test_obligation_id_classified_as_obligation(self) -> None:
        self.assertEqual(
            ticket_lint.edge_node_id("ORG-065#E2.C1"), ("obligation", "ORG-065#E2.C1")
        )


class TestBuildTypedEdgeGraph(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tickets = Path(self._tmp.name)
        for st in ("inbox", "active", "done"):
            (self.tickets / st).mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _graph(self):
        return ticket_lint.build_typed_edge_graph(self.tickets)

    def test_depends_on_edge_present(self) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md", tid="T-0001", depends_on=["T-0002"])
        g = self._graph()
        self.assertIn(("ticket", "T-0002"), g[("ticket", "T-0001")])

    def test_parent_of_edge_present(self) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md", tid="T-0001", parent_ticket="T-0003")
        g = self._graph()
        self.assertIn(("ticket", "T-0003"), g[("ticket", "T-0001")])

    def test_implements_edge_to_obligation_present(self) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md", tid="T-0001",
                       implements=["ORG-065#E1"])
        g = self._graph()
        self.assertIn(("obligation", "ORG-065#E1"), g[("ticket", "T-0001")])

    def test_verifies_and_supersedes_edges_present(self) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md", tid="T-0001",
                       verifies=["ORG-065#E2"], supersedes=["T-0004"])
        g = self._graph()
        self.assertIn(("obligation", "ORG-065#E2"), g[("ticket", "T-0001")])
        self.assertIn(("ticket", "T-0004"), g[("ticket", "T-0001")])

    def test_ratifies_edge_to_obligation_present(self) -> None:
        """T-3476: `ratifies` dispatches through build_typed_edge_graph's
        field-name loop exactly like implements/verifies/supersedes."""
        _write_ticket(self.tickets / "inbox" / "T-0001.md", tid="T-0001",
                       ratifies=["ORG-062#E0"])
        g = self._graph()
        self.assertIn(("obligation", "ORG-062#E0"), g[("ticket", "T-0001")])

    def test_informs_edge_excluded(self) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md", tid="T-0001", informs=["T-0005"])
        g = self._graph()
        self.assertNotIn(("ticket", "T-0005"), g.get(("ticket", "T-0001"), []))

    def test_done_ticket_edges_still_included(self) -> None:
        """Unlike detect_dep_graph_issues's open-only depends_on scope, the
        typed-edge graph is state-independent (see module docstring note in
        ticket_lint.py)."""
        _write_ticket(self.tickets / "done" / "T-0001.md", tid="T-0001",
                       state="done", parent_ticket="T-0006")
        g = self._graph()
        self.assertIn(("ticket", "T-0006"), g[("ticket", "T-0001")])

    def test_edge_fields_param_restricts_to_one_domain(self) -> None:
        """A parent depends_on its own child — the live-corpus shape. The
        default (full union) graph carries BOTH edges; a single-domain graph
        (as ACYCLICITY_DOMAINS/cycle-detection callers must use) carries only
        the one it was asked for."""
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", depends_on=["T-0002"])
        _write_ticket(self.tickets / "inbox" / "T-0002.md",
                       tid="T-0002", parent_ticket="T-0001")
        full = ticket_lint.build_typed_edge_graph(self.tickets)
        self.assertIn(("ticket", "T-0002"), full[("ticket", "T-0001")])
        self.assertIn(("ticket", "T-0001"), full[("ticket", "T-0002")])

        deps_only = ticket_lint.build_typed_edge_graph(self.tickets, ("depends_on",))
        self.assertEqual(deps_only.get(("ticket", "T-0002"), []), [])

        parent_only = ticket_lint.build_typed_edge_graph(self.tickets, ("parent_of",))
        self.assertEqual(parent_only.get(("ticket", "T-0001"), []), [])

    def test_acyclicity_domains_partition_matches_module_constant(self) -> None:
        # Round-2 addendum: parent_of is NOT exempt from checking (unlike
        # informs) — it's its own separately-checked domain, just never
        # unioned with the other four, which share one domain so a
        # mixed-type loop across them is still caught.
        self.assertEqual(
            ticket_lint.ACYCLICITY_DOMAINS,
            (("parent_of",),
             ("depends_on", "implements", "verifies", "supersedes", "ratifies")),
        )


class TestFindPath(unittest.TestCase):
    def test_direct_edge(self) -> None:
        g = {("ticket", "A"): [("ticket", "B")]}
        self.assertEqual(
            ticket_lint.find_path(g, ("ticket", "A"), ("ticket", "B")),
            [("ticket", "A"), ("ticket", "B")],
        )

    def test_transitive_path(self) -> None:
        g = {
            ("ticket", "A"): [("ticket", "B")],
            ("ticket", "B"): [("ticket", "C")],
        }
        self.assertEqual(
            ticket_lint.find_path(g, ("ticket", "A"), ("ticket", "C")),
            [("ticket", "A"), ("ticket", "B"), ("ticket", "C")],
        )

    def test_unreachable_returns_none(self) -> None:
        g = {("ticket", "A"): [("ticket", "B")]}
        self.assertIsNone(ticket_lint.find_path(g, ("ticket", "A"), ("ticket", "Z")))

    def test_same_start_and_goal(self) -> None:
        g: dict = {}
        self.assertEqual(
            ticket_lint.find_path(g, ("ticket", "A"), ("ticket", "A")),
            [("ticket", "A")],
        )

    def test_leaf_node_absent_from_graph_is_a_valid_start(self) -> None:
        g = {("ticket", "A"): [("obligation", "ORG-065#E1")]}
        # ("obligation", "ORG-065#E1") is a leaf never added as a graph key.
        self.assertIsNone(
            ticket_lint.find_path(g, ("obligation", "ORG-065#E1"), ("ticket", "A"))
        )


# ---------------------------------------------------------------------------
# ticket_lint.py: scan_adr_elements (the ADR obligation-identity parser).
# ---------------------------------------------------------------------------


class TestScanAdrElements(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.adr_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_well_formed_elements_block(self) -> None:
        _write_adr(
            self.adr_dir / "ORG-065-test.md",
            adr_id="ORG-065",
            elements_block=(
                "  - id: E1\n"
                "    title: first obligation\n"
                "    clauses:\n"
                "      - {id: E1.C1, text: \"clause one\"}\n"
                "      - {id: E1.C2, text: \"clause two\"}\n"
                "  - id: E2\n"
                "    title: second obligation\n"
            ),
        )
        known, findings = ticket_lint.scan_adr_elements(self.adr_dir)
        self.assertEqual(findings, [])
        self.assertEqual(
            known,
            {"ORG-065#E1", "ORG-065#E1.C1", "ORG-065#E1.C2", "ORG-065#E2"},
        )

    def test_malformed_id_flagged(self) -> None:
        _write_adr(
            self.adr_dir / "ORG-066-test.md",
            adr_id="ORG-066",
            elements_block="  - id: X1\n    title: bad id shape\n",
        )
        known, findings = ticket_lint.scan_adr_elements(self.adr_dir)
        self.assertEqual(known, set())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "malformed_id")
        self.assertIn("ORG-066", findings[0].detail)

    def test_duplicate_id_flagged(self) -> None:
        _write_adr(
            self.adr_dir / "ORG-067-test.md",
            adr_id="ORG-067",
            elements_block=(
                "  - id: E1\n    title: first\n"
                "  - id: E1\n    title: duplicate\n"
            ),
        )
        known, findings = ticket_lint.scan_adr_elements(self.adr_dir)
        dup = [f for f in findings if f.kind == "duplicate_id"]
        self.assertEqual(len(dup), 1)
        self.assertIn("E1", dup[0].detail)

    def test_no_elements_block_yields_nothing(self) -> None:
        _write_adr(self.adr_dir / "ORG-068-test.md", adr_id="ORG-068", elements_block=None)
        known, findings = ticket_lint.scan_adr_elements(self.adr_dir)
        self.assertEqual(known, set())
        self.assertEqual(findings, [])

    def test_missing_adr_dir_yields_nothing(self) -> None:
        known, findings = ticket_lint.scan_adr_elements(self.adr_dir / "does-not-exist")
        self.assertEqual(known, set())
        self.assertEqual(findings, [])

    def test_readme_skipped(self) -> None:
        (self.adr_dir / "README.md").write_text(
            "---\nelements:\n  - id: X1\n---\n# index\n", encoding="utf-8"
        )
        known, findings = ticket_lint.scan_adr_elements(self.adr_dir)
        self.assertEqual(known, set())
        self.assertEqual(findings, [])


# ---------------------------------------------------------------------------
# ticket_lint.py: detect_typed_graph_issues (corpus-wide, report-only).
# ---------------------------------------------------------------------------


class TestDetectTypedGraphIssues(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tickets = self.root / "tickets"
        self.adr_dir = self.root / "adr"
        for st in ("inbox", "active", "done"):
            (self.tickets / st).mkdir(parents=True)
        self.adr_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scan(self):
        # T-3495: mirrors this module's own production call sites, which
        # pass exempt_dangling_target=is_echo_scoped_id (their adr_dir is
        # pantheon-only — see _ECHO_PRISMATIC_ADR_DIR's module note).
        return ticket_lint.detect_typed_graph_issues(
            self.tickets, self.adr_dir,
            exempt_dangling_target=ticket_lint.is_echo_scoped_id,
        )

    def test_clean_corpus_no_findings(self) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md", tid="T-0001")
        self.assertEqual(self._scan(), [])

    def test_dangling_ticket_target_warned(self) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", supersedes=["T-9999"])
        findings = self._scan()
        dangling = [f for f in findings if f.kind == "dangling_target"]
        self.assertEqual(len(dangling), 1)
        self.assertIn("T-9999", dangling[0].detail)

    def test_dangling_obligation_target_warned(self) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ORG-999#E1"])
        findings = self._scan()
        dangling = [f for f in findings if f.kind == "dangling_target"]
        self.assertEqual(len(dangling), 1)
        self.assertIn("ORG-999#E1", dangling[0].detail)

    def test_resolvable_obligation_target_not_warned(self) -> None:
        _write_adr(self.adr_dir / "ORG-065-test.md", adr_id="ORG-065",
                   elements_block="  - id: E1\n    title: t\n")
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ORG-065#E1"])
        self.assertEqual(self._scan(), [])

    def test_informs_dangling_target_also_warned(self) -> None:
        """Acyclicity-exemption is orthogonal to existence-checking —
        resolved-decision 1 covers all four fields uniformly."""
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", informs=["T-8888"])
        findings = self._scan()
        dangling = [f for f in findings if f.kind == "dangling_target"]
        self.assertEqual(len(dangling), 1)
        self.assertIn("T-8888", dangling[0].detail)

    def test_ratifies_dangling_target_warned(self) -> None:
        """T-3476: `ratifies` participates in the dangling-target scan the
        same as implements/verifies/supersedes/informs — an unresolvable
        ratifies target (e.g. an ADR whose elements: block hasn't landed
        yet) is a WARN, never a refusal."""
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", ratifies=["ORG-062#E0"])
        findings = self._scan()
        dangling = [f for f in findings if f.kind == "dangling_target"]
        self.assertEqual(len(dangling), 1)
        self.assertIn("ORG-062#E0", dangling[0].detail)

    def test_ratifies_resolvable_target_not_warned(self) -> None:
        _write_adr(self.adr_dir / "ORG-062-test.md", adr_id="ORG-062",
                   elements_block="  - id: E0\n    title: whole ADR\n")
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", ratifies=["ORG-062#E0"])
        self.assertEqual(self._scan(), [])

    def test_echo_scoped_dangling_target_skipped(self) -> None:
        """T-3495: an edge targeting an echo-prismatic obligation id
        (ADR-2NN range) is exempted from this scan's dangling-target check —
        that judgment is delegated to tools/echo_corpus_lint.py's companion
        scan, which DOES read the echo corpus. `self.adr_dir` here is a
        pantheon-only dir (this module's own production shape post-T-3495),
        so without the exemption every echo-targeting edge would be a FALSE
        dangling_target."""
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ADR-207#E1"])
        self.assertEqual(self._scan(), [])

    def test_vault_range_dangling_target_still_warned(self) -> None:
        """The echo-scope exemption is narrowly ADR-id >= 200 — a vault-range
        id (001-199, a DIFFERENT, permanently-excluded corpus per the
        _ECHO_PRISMATIC_ADR_DIR module note) is NOT exempted and still flags
        as dangling, same as before T-3495."""
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ADR-050#E1"])
        findings = self._scan()
        dangling = [f for f in findings if f.kind == "dangling_target"]
        self.assertEqual(len(dangling), 1)
        self.assertIn("ADR-050#E1", dangling[0].detail)

    def test_exempt_dangling_target_is_opt_in_not_default(self) -> None:
        """T-3495: the exemption is caller-supplied, not baked into
        detect_typed_graph_issues itself — a caller that omits
        exempt_dangling_target (tools/echo_corpus_lint.py's companion call,
        which unions BOTH adr dirs itself) must still see a genuinely-
        dangling echo-shaped target flagged, or that companion's own
        detection purpose is defeated."""
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ADR-207#E1"])
        findings = ticket_lint.detect_typed_graph_issues(self.tickets, self.adr_dir)
        dangling = [f for f in findings if f.kind == "dangling_target"]
        self.assertEqual(len(dangling), 1)
        self.assertIn("ADR-207#E1", dangling[0].detail)

    def test_preexisting_cycle_flagged(self) -> None:
        """A hand-edited (not ticket.py-inserted) cycle — the corpus-wide
        backstop for data the insertion guard never saw."""
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", depends_on=["T-0002"])
        _write_ticket(self.tickets / "inbox" / "T-0002.md",
                       tid="T-0002", depends_on=["T-0001"])
        findings = self._scan()
        cycles = [f for f in findings if f.kind == "cycle"]
        self.assertEqual(len(cycles), 1)
        self.assertIn("T-0001", cycles[0].detail)
        self.assertIn("T-0002", cycles[0].detail)

    def test_mixed_type_3hop_cycle_flagged(self) -> None:
        """Reviewer-identified blind spot in the interim 4-way-split design:
        A implements B, B verifies C, C depends_on A is a 3-hop cycle mixing
        three of the four unioned-domain edge types. Must be caught
        corpus-wide (the insertion-time guard only ever sees ONE new edge at
        a time, so a multi-hop loop assembled across several separate
        inserts is exactly what this corpus-wide backstop is for)."""
        _write_ticket(self.tickets / "inbox" / "T-0100.md",
                       tid="T-0100", implements=["T-0101"])
        _write_ticket(self.tickets / "inbox" / "T-0101.md",
                       tid="T-0101", verifies=["T-0102"])
        _write_ticket(self.tickets / "inbox" / "T-0102.md",
                       tid="T-0102", depends_on=["T-0100"])
        findings = self._scan()
        cycles = [f for f in findings if f.kind == "cycle"]
        self.assertEqual(len(cycles), 1)
        for t in ("T-0100", "T-0101", "T-0102"):
            self.assertIn(t, cycles[0].detail)

    def test_informs_never_produces_a_cycle_finding(self) -> None:
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", depends_on=["T-0002"])
        _write_ticket(self.tickets / "inbox" / "T-0002.md",
                       tid="T-0002", informs=["T-0001"])
        findings = self._scan()
        self.assertEqual([f for f in findings if f.kind == "cycle"], [])

    def test_parent_depends_on_child_not_flagged_as_cycle(self) -> None:
        """The exact live-corpus shape (T-0173/T-0191-style, 15 instances
        found on the wet-run) — a parent depends_on its own child, whose
        parent_ticket correctly names the parent. depends_on and parent_of
        are independent acyclicity domains, so this is NOT a cycle finding."""
        _write_ticket(self.tickets / "done" / "T-0001.md", tid="T-0001",
                       state="done", depends_on=["T-0002"])
        _write_ticket(self.tickets / "done" / "T-0002.md", tid="T-0002",
                       state="done", parent_ticket="T-0001")
        self.assertEqual(self._scan(), [])

    def test_element_findings_folded_in(self) -> None:
        _write_adr(self.adr_dir / "ORG-070-test.md", adr_id="ORG-070",
                   elements_block="  - id: bad\n    title: t\n")
        findings = self._scan()
        self.assertEqual([f.kind for f in findings], ["malformed_id"])


# ---------------------------------------------------------------------------
# ticket_lint.py: detect_unverified_elements (T-2843, corpus-wide,
# report-only element-lineage leak detector).
# ---------------------------------------------------------------------------


class TestDetectUnverifiedElements(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tickets = self.root / "tickets"
        self.adr_dir = self.root / "adr"
        for st in ("inbox", "active", "done"):
            (self.tickets / st).mkdir(parents=True)
        self.adr_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scan(self):
        return ticket_lint.detect_unverified_elements(self.tickets, self.adr_dir)

    def test_no_elements_no_findings(self) -> None:
        _write_adr(self.adr_dir / "ORG-065-test.md", adr_id="ORG-065", elements_block=None)
        self.assertEqual(self._scan(), [])

    def test_element_covered_by_open_ticket_not_flagged(self) -> None:
        _write_adr(self.adr_dir / "ORG-065-test.md", adr_id="ORG-065",
                   elements_block="  - id: E1\n    title: t\n")
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", verifies=["ORG-065#E1"])
        self.assertEqual(self._scan(), [])

    def test_element_covered_by_done_ticket_not_flagged(self) -> None:
        """Proves STATES (not STATES_TO_LINT) is scanned — a done ticket's
        verifies: edge still counts as coverage."""
        _write_adr(self.adr_dir / "ORG-065-test.md", adr_id="ORG-065",
                   elements_block="  - id: E1\n    title: t\n")
        _write_ticket(self.tickets / "done" / "T-0001.md",
                       tid="T-0001", state="done", verifies=["ORG-065#E1"])
        self.assertEqual(self._scan(), [])

    def test_element_with_no_verifies_flagged(self) -> None:
        _write_adr(self.adr_dir / "ORG-065-test.md", adr_id="ORG-065",
                   elements_block="  - id: E1\n    title: t\n")
        findings = self._scan()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "unverified_element")
        self.assertIn("ORG-065#E1", findings[0].detail)

    def test_clause_level_element_tracked_independently(self) -> None:
        """Verifying the parent element does not roll up to satisfy its
        clause — element/clause ids are tracked independently, mirroring
        scan_adr_elements's own flat id-set."""
        _write_adr(
            self.adr_dir / "ORG-065-test.md",
            adr_id="ORG-065",
            elements_block=(
                "  - id: E1\n"
                "    title: t\n"
                "    clauses:\n"
                "      - {id: E1.C1, text: \"clause one\"}\n"
            ),
        )
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", verifies=["ORG-065#E1"])
        findings = self._scan()
        self.assertEqual(len(findings), 1)
        self.assertIn("ORG-065#E1.C1", findings[0].detail)

    def test_malformed_element_id_excluded_from_universe(self) -> None:
        """A malformed element id never enters known_ids (scan_adr_elements's
        own concern, surfaced separately by detect_typed_graph_issues) — so
        this detector has nothing to check and flags nothing."""
        _write_adr(self.adr_dir / "ORG-065-test.md", adr_id="ORG-065",
                   elements_block="  - id: bad\n    title: t\n")
        self.assertEqual(self._scan(), [])

    def test_malformed_verifies_entry_does_not_crash_or_falsely_cover(self) -> None:
        _write_adr(self.adr_dir / "ORG-065-test.md", adr_id="ORG-065",
                   elements_block="  - id: E1\n    title: t\n")
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", verifies=["not-a-real-obligation-id"])
        findings = self._scan()
        self.assertEqual(len(findings), 1)
        self.assertIn("ORG-065#E1", findings[0].detail)

    def test_multiple_tickets_verifying_same_element_not_duplicated(self) -> None:
        _write_adr(self.adr_dir / "ORG-065-test.md", adr_id="ORG-065",
                   elements_block="  - id: E1\n    title: t\n")
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", verifies=["ORG-065#E1"])
        _write_ticket(self.tickets / "active" / "T-0002.md",
                       tid="T-0002", verifies=["ORG-065#E1"])
        self.assertEqual(self._scan(), [])

    # -- T-3179: true-leak vs implemented-unverified split -----------------

    def test_element_with_implements_only_flagged_as_implemented_unverified(self) -> None:
        """>=1 implements:, no verifies: — expected transitional shape, not
        a true leak. Detail names the implementing ticket."""
        _write_adr(self.adr_dir / "ORG-065-test.md", adr_id="ORG-065",
                   elements_block="  - id: E1\n    title: t\n")
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ORG-065#E1"])
        findings = self._scan()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "implemented_unverified_element")
        self.assertIn("ORG-065#E1", findings[0].detail)
        self.assertIn("T-0001", findings[0].detail)

    def test_element_with_neither_edge_stays_true_leak(self) -> None:
        """No implements: and no verifies: — kind stays the true-leak
        'unverified_element' (T-3179 must not reclassify the untouched
        case)."""
        _write_adr(self.adr_dir / "ORG-065-test.md", adr_id="ORG-065",
                   elements_block="  - id: E1\n    title: t\n")
        findings = self._scan()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "unverified_element")

    def test_element_with_verifies_and_implements_not_flagged(self) -> None:
        """A verifies: edge covers the element regardless of whether an
        implements: edge also exists — implements: never substitutes for
        verifies: (ADR §3.4 reading (i)), but its presence alongside a real
        verifies: edge is simply the fully-covered case: no finding."""
        _write_adr(self.adr_dir / "ORG-065-test.md", adr_id="ORG-065",
                   elements_block="  - id: E1\n    title: t\n")
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ORG-065#E1"],
                       verifies=["ORG-065#E1"])
        self.assertEqual(self._scan(), [])

    def test_implementing_done_ticket_still_counts(self) -> None:
        """A done-state ticket's implements: edge still counts — mirrors
        test_element_covered_by_done_ticket_not_flagged's proof that
        build_typed_edge_graph scans STATES (not STATES_TO_LINT)."""
        _write_adr(self.adr_dir / "ORG-065-test.md", adr_id="ORG-065",
                   elements_block="  - id: E1\n    title: t\n")
        _write_ticket(self.tickets / "done" / "T-0001.md",
                       tid="T-0001", state="done", implements=["ORG-065#E1"])
        findings = self._scan()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "implemented_unverified_element")
        self.assertIn("T-0001", findings[0].detail)

    def test_multiple_implementing_tickets_named_in_detail(self) -> None:
        _write_adr(self.adr_dir / "ORG-065-test.md", adr_id="ORG-065",
                   elements_block="  - id: E1\n    title: t\n")
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ORG-065#E1"])
        _write_ticket(self.tickets / "active" / "T-0002.md",
                       tid="T-0002", implements=["ORG-065#E1"])
        findings = self._scan()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "implemented_unverified_element")
        self.assertIn("T-0001", findings[0].detail)
        self.assertIn("T-0002", findings[0].detail)

    def test_clause_level_implements_tracked_independently(self) -> None:
        """implements: on a clause id does not roll up to cover the parent
        element — mirrors test_clause_level_element_tracked_independently."""
        _write_adr(
            self.adr_dir / "ORG-065-test.md",
            adr_id="ORG-065",
            elements_block=(
                "  - id: E1\n"
                "    title: t\n"
                "    clauses:\n"
                "      - {id: E1.C1, text: \"clause one\"}\n"
            ),
        )
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", implements=["ORG-065#E1.C1"])
        findings = self._scan()
        # Detail strings always lead with "{oid} " — anchor on that prefix
        # rather than substring "in", since "ORG-065#E1" is itself a prefix
        # of "ORG-065#E1.C1" and a bare `in` check would conflate the two.
        kinds_by_oid = {}
        for f in findings:
            for oid in ("ORG-065#E1", "ORG-065#E1.C1"):
                if f.detail.startswith(oid + " "):
                    kinds_by_oid[oid] = f.kind
        self.assertEqual(len(findings), 2)
        self.assertEqual(kinds_by_oid["ORG-065#E1"], "unverified_element")
        self.assertEqual(kinds_by_oid["ORG-065#E1.C1"], "implemented_unverified_element")


# ---------------------------------------------------------------------------
# ticket_lint.py: detect_unverified_elements — T-3476 / ORG-065 Call 1
# `ratifies` split. The load-bearing invariant the operator ratified (T-3206 TG
# 6734): the leak-detector never counts a ratification as an implementation.
# ---------------------------------------------------------------------------


class TestDetectUnverifiedElementsRatifies(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tickets = self.root / "tickets"
        self.adr_dir = self.root / "adr"
        for st in ("inbox", "active", "done"):
            (self.tickets / st).mkdir(parents=True)
        self.adr_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scan(self):
        return ticket_lint.detect_unverified_elements(self.tickets, self.adr_dir)

    def test_ratifies_only_element_stays_unverified(self) -> None:
        """THE INVARIANT TEST (T-3206 TG 6734): a non-E0 content element
        (ORG-999#E5) with ONLY a ratifies: edge — no implements:, no
        verifies: — stays 'unverified_element', unchanged from the
        no-edges-at-all case. A ratifies edge alone never satisfies an
        implements-obligation."""
        _write_adr(self.adr_dir / "ORG-999-test.md", adr_id="ORG-999",
                   elements_block="  - id: E5\n    title: t\n")
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", ratifies=["ORG-999#E5"])
        findings = self._scan()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "unverified_element")
        self.assertIn("ORG-999#E5", findings[0].detail)

    def test_e0_ratified_element_excluded_from_leak_scan(self) -> None:
        """T-3476 exclusion: the E0 'ADR as a whole' reserved sentinel
        (T-3205 policy) is NOT an implementation obligation in the code
        sense — a ratify-class ticket's ratifies: edge IS its terminal
        completion signal. ORG-070#E0 ratified-only produces NO finding at
        all (not unverified_element, not implemented_unverified_element).
        Scoped narrowly to local id "E0" — see
        test_ratifies_only_element_stays_unverified for proof this does NOT
        extend to ordinary content elements."""
        _write_adr(self.adr_dir / "ORG-070-test.md", adr_id="ORG-070",
                   elements_block="  - id: E0\n    title: whole ADR\n")
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", ratifies=["ORG-070#E0"])
        self.assertEqual(self._scan(), [])

    def test_e0_implemented_and_ratified_still_implemented_unverified(self) -> None:
        """Guards the ORG-053 shape: an E0 that already has a T-2413-class
        plan ticket's implements: edge, and is ALSO ratifies-targeted by an
        unrelated ticket, stays 'implemented_unverified_element' — the
        implements-priority branch is unchanged; the E0 exclusion only fires
        in the implements-absent branch."""
        _write_adr(self.adr_dir / "ORG-053-test.md", adr_id="ORG-053",
                   elements_block="  - id: E0\n    title: whole ADR\n")
        _write_ticket(self.tickets / "inbox" / "T-2413.md",
                       tid="T-2413", implements=["ORG-053#E0"])
        _write_ticket(self.tickets / "inbox" / "T-2875.md",
                       tid="T-2875", ratifies=["ORG-053#E0"])
        findings = self._scan()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "implemented_unverified_element")
        self.assertIn("T-2413", findings[0].detail)

    def test_e0_verified_element_not_flagged_regardless_of_ratifies(self) -> None:
        """A verifies: edge covers E0 exactly like any other element — the
        ratifies exclusion is irrelevant once real coverage exists (verified
        wins over every other branch, unchanged ordering)."""
        _write_adr(self.adr_dir / "ORG-053-test.md", adr_id="ORG-053",
                   elements_block="  - id: E0\n    title: whole ADR\n")
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                       tid="T-0001", verifies=["ORG-053#E0"],
                       ratifies=["ORG-053#E0"])
        self.assertEqual(self._scan(), [])


if __name__ == "__main__":
    unittest.main()
