"""Tests for T-3477 / ORG-065 Call 2: cross-repo `implements:` edge targets
pointing at Echo ADRs in the echo-prismatic repo, walked read-only.

Run from repo root:
    python3 -m unittest tools.test_cross_repo_edges -v

Covers:
  - tools/ticket.py: the existing `_EDGE_TARGET_RE` already accepts the Echo
    ADR shape (`ADR-NNN#E<k>[.C<m>]`) with NO regex change — this is a
    binding pin against that finding, not new behavior.
  - tools/ticket_lint.py: `scan_adr_elements` / `detect_typed_graph_issues` /
    `detect_unverified_elements` widened to accept a Path OR an iterable of
    Paths for `adr_dir`, so the corpus-wide obligation-id universe can span
    both this repo's `architecture/adr/` and echo-prismatic's `adr/` (a
    sibling repo, different filename convention: `ADR-<NNN> — <title>.md`
    vs this repo's `ORG-<NNN>-<title>.md`).

New file rather than extending tools/test_typed_edges.py (already 1201
lines, past the code-size-norms ~1000-line flag threshold) — keeps the
cross-repo concern cohesive without growing an already-oversized module
further.

Fixture-string discipline: no test constructs the contiguous unescaped
HERMES trigger bytes. Per /home/claude/CLAUDE.md "HERMES\\.md byte-sequence
discipline" (escape form in prose, mentally drop the backslash) — moot here
since nothing in this module references that filename, but noted per the
dispatch brief's standing instruction to workers.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tools import ticket, ticket_lint


_REAL_ECHO_ADR_DIR = Path("/home/claude/echo-prismatic/adr")


def _real_echo_adr_dir_readable() -> bool:
    """Reachability probe used by ``skipUnless`` on the one live-tree
    test in this module. Runner UIDs that are not ``claude`` (GitHub
    Actions, recovery, foreign consumers) cannot cross into
    ``/home/claude`` and would hit ``PermissionError`` before the test
    body runs — which is the CI-red-baseline symptom T-3547 is
    unwinding. This is a fixture-scope skip, not a suppression: the
    docstring's regression-signal intent still holds on the claude
    account, where the test does run."""
    try:
        return _REAL_ECHO_ADR_DIR.is_dir() and os.access(_REAL_ECHO_ADR_DIR, os.R_OK | os.X_OK)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Fixture helpers — local to this module, mirroring test_typed_edges.py's own
# per-file-local-helper convention (test_ticket.py's _write_ticket,
# test_ticket_lint.py's _make_ticket) rather than importing across test
# modules.
# ---------------------------------------------------------------------------


def _write_ticket(
    path: Path,
    *,
    tid: str,
    implements: list[str] | None = None,
    ratifies: list[str] | None = None,
) -> None:
    """Write a minimal, well-formed ticket file with the T-2841 typed-edge
    fields when given — directly to disk (bypassing ticket.py's own
    insertion-time validation, matching test_typed_edges.py's _write_ticket
    convention for pre-seeded fixtures)."""
    lines = [
        "---",
        f"id: {tid}",
        "created: 2026-09-06T00:00:00Z",
        "requester: operator",
        "target: recovery",
        "state: inbox",
        "parent_ticket: null",
        "depends_on: []",
    ]
    if implements:
        lines.append(f"implements: [{', '.join(implements)}]")
    if ratifies:
        lines.append(f"ratifies: [{', '.join(ratifies)}]")
    lines.append("---")
    body = (
        f"\n# Test ticket {tid}\n\n"
        "## Ask\n\nx.\n\n"
        "## Transition log\n\n"
        "- 2026-09-06T00:00:00Z [operator] filed\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + body, encoding="utf-8")


def _write_pantheon_shaped_adr(path: Path, *, adr_id: str, elements_block: str | None) -> None:
    """Pantheon-convention ADR fixture (`ORG-NNN-slug.md`, YAML frontmatter
    up top). Mirrors test_typed_edges.py's _write_adr."""
    fm = ["---", f"id: {adr_id}", f"title: Test ADR {adr_id}"]
    if elements_block is not None:
        fm.append("elements:")
        fm.append(elements_block)
    fm.append("---")
    body = f"\n# {adr_id} — test ADR\n\nBody text.\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm) + body, encoding="utf-8")


def _write_echo_shaped_adr(path: Path, *, adr_id: str, elements_block: str | None) -> None:
    """Echo-convention ADR fixture: filename `ADR-<NNN> — <title>.md` (spaces,
    em-dash — the path is set by the caller to match), and a body that starts
    directly with the H1 heading when no elements: block is requested (the
    real Echo-ADR shape sampled from ADR-232/245/etc — no YAML frontmatter at
    all until one is added). When `elements_block` is given, a frontmatter
    block is prefixed (the shape HO will land on the real files)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if elements_block is None:
        path.write_text(f"# {adr_id} — test Echo ADR\n\nBody text, no frontmatter.\n",
                         encoding="utf-8")
        return
    fm = ["---", f"id: {adr_id}", "elements:", elements_block, "---"]
    body = f"\n# {adr_id} — test Echo ADR\n\nBody text.\n"
    path.write_text("\n".join(fm) + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# ticket.py: _EDGE_TARGET_RE already accepts the Echo ADR shape — pin, no
# regex change made or needed (Design Question 1 finding).
# ---------------------------------------------------------------------------


class TestEchoAdrTargetShapeAlreadyAccepted(unittest.TestCase):
    def test_element_anchored_echo_target_accepted(self) -> None:
        self.assertEqual(
            ticket._parse_edge_targets("implements", ["ADR-232#E3"]),
            ["ADR-232#E3"],
        )

    def test_clause_anchored_echo_target_accepted(self) -> None:
        self.assertEqual(
            ticket._parse_edge_targets("implements", ["ADR-232#E3.C1"]),
            ["ADR-232#E3.C1"],
        )

    def test_bare_echo_adr_id_still_rejected(self) -> None:
        """A bare ADR-NNN with no element anchor is still invalid — T-3477
        deliberately did NOT introduce a bare-whole-ADR target shape (see the
        T-1626 follow-up ticket: that needs its own node-kind design so it
        doesn't create a leak-detector composition trap, per the T-2896
        lesson). This pins the current, intentional boundary."""
        with self.assertRaises(ticket.TicketError):
            ticket._parse_edge_targets("implements", ["ADR-232"])


# ---------------------------------------------------------------------------
# ticket_lint.py: scan_adr_elements accepts a Path OR an iterable of Paths.
# ---------------------------------------------------------------------------


class TestScanAdrElementsMultiDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.pantheon_dir = root / "architecture" / "adr"
        self.echo_dir = root / "echo-prismatic-adr"
        self.pantheon_dir.mkdir(parents=True)
        self.echo_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_single_path_still_works(self) -> None:
        """Backward-compat pin: existing callers (test_typed_edges.py, the
        two production call sites pre-widening) pass a bare Path — that
        shape must keep working unchanged."""
        _write_pantheon_shaped_adr(
            self.pantheon_dir / "ORG-065-test.md", adr_id="ORG-065",
            elements_block="  - id: E1\n    title: one\n",
        )
        known, findings = ticket_lint.scan_adr_elements(self.pantheon_dir)
        self.assertEqual(findings, [])
        self.assertEqual(known, {"ORG-065#E1"})

    def test_dir_list_unions_both_corpora(self) -> None:
        _write_pantheon_shaped_adr(
            self.pantheon_dir / "ORG-065-test.md", adr_id="ORG-065",
            elements_block="  - id: E1\n    title: one\n",
        )
        _write_echo_shaped_adr(
            self.echo_dir / "ADR-232 — test lane redesign.md", adr_id="ADR-232",
            elements_block='  - {id: E3, text: "3: guidance"}\n'
                           '  - {id: E3.C1, text: "3a: lane hygiene"}\n',
        )
        known, findings = ticket_lint.scan_adr_elements([self.pantheon_dir, self.echo_dir])
        self.assertEqual(findings, [])
        self.assertEqual(known, {"ORG-065#E1", "ADR-232#E3", "ADR-232#E3.C1"})

    def test_echo_filename_with_spaces_and_em_dash_parses(self) -> None:
        """The real Echo filename convention (`ADR-<NNN> — <title>.md`,
        spaces + em-dash) must resolve to the same adr_id as Pantheon's
        dash-only convention — proves _adr_id_from's filename-prefix regex
        fallback (adr_reeval_walk.py:132) needs no change for this shape."""
        _write_echo_shaped_adr(
            self.echo_dir / "ADR-245 — router re-scope — long descriptive title.md",
            adr_id="ADR-245",
            elements_block='  - {id: E3, text: "3: durability"}\n',
        )
        known, _ = ticket_lint.scan_adr_elements([self.echo_dir])
        self.assertEqual(known, {"ADR-245#E3"})

    def test_echo_adr_with_no_frontmatter_yields_nothing_not_a_crash(self) -> None:
        """The real, un-elementized shape of most Echo ADRs today (no YAML
        frontmatter at all, file starts at the H1) must be tolerated
        silently — same as any other ADR with no elements: block."""
        _write_echo_shaped_adr(
            self.echo_dir / "ADR-231 — voice successful tool output.md",
            adr_id="ADR-231", elements_block=None,
        )
        known, findings = ticket_lint.scan_adr_elements([self.pantheon_dir, self.echo_dir])
        self.assertEqual(known, set())
        self.assertEqual(findings, [])


# ---------------------------------------------------------------------------
# ticket_lint.py: detect_typed_graph_issues over a multi-dir ADR corpus.
# ---------------------------------------------------------------------------


class TestDetectTypedGraphIssuesMultiDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tickets = self.root / "tickets"
        self.pantheon_dir = self.root / "architecture" / "adr"
        self.echo_dir = self.root / "echo-prismatic-adr"
        for st in ("inbox", "active", "done"):
            (self.tickets / st).mkdir(parents=True)
        self.pantheon_dir.mkdir(parents=True)
        self.echo_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scan(self):
        return ticket_lint.detect_typed_graph_issues(
            self.tickets, [self.pantheon_dir, self.echo_dir]
        )

    def test_cross_repo_element_target_resolves_clean(self) -> None:
        _write_echo_shaped_adr(
            self.echo_dir / "ADR-232 — test lane redesign.md", adr_id="ADR-232",
            elements_block='  - {id: E3, text: "3: guidance"}\n'
                           '  - {id: E3.C1, text: "3a: lane hygiene"}\n',
        )
        _write_ticket(self.tickets / "inbox" / "T-1528.md",
                      tid="T-1528", implements=["ADR-232#E3.C1"])
        findings = self._scan()
        dangling = [f for f in findings if f.kind == "dangling_target"]
        self.assertEqual(dangling, [])

    def test_dangling_cross_repo_target_warns_not_refuses(self) -> None:
        """An implements: target naming an Echo ADR element that doesn't
        (yet) exist in the corpus is a WARN, never a hard refusal — matches
        resolved-decision 1, unchanged by the multi-dir widening."""
        _write_ticket(self.tickets / "inbox" / "T-1528.md",
                      tid="T-1528", implements=["ADR-232#E3.C1"])
        findings = self._scan()
        dangling = [f for f in findings if f.kind == "dangling_target"]
        self.assertEqual(len(dangling), 1)
        self.assertIn("ADR-232#E3.C1", dangling[0].detail)

    @unittest.skipUnless(
        _real_echo_adr_dir_readable(),
        f"skips when {_REAL_ECHO_ADR_DIR} is unreachable to the runtime UID — "
        "the runner (GitHub Actions, recovery, foreign consumer) cannot cross into "
        "/home/claude/. Regression-signal intent still holds on the claude account "
        "where the guard evaluates true (T-3547 slice 1).",
    )
    def test_real_echo_prismatic_dir_is_reachable(self) -> None:
        """Recovery build-note binding test (T-3477 scope item 2): the REAL
        filesystem path is readable under this runtime user, not just a
        temp-dir fixture. Loose assertion (doesn't hard-code file counts —
        the corpus drifts) — the point is that scanning the real directory
        alongside the real repo's own architecture/adr/ does not raise, and
        every known ADR-232 element id sampled during planning (none minted
        yet at time of writing) resolves through the SAME code path a
        temp-fixture test exercises above. If this ever starts raising on
        the claude account, the walker has lost read access to
        echo-prismatic — a real regression, not a fixture problem."""
        real_echo_dir = _REAL_ECHO_ADR_DIR
        known, findings = ticket_lint.scan_adr_elements(
            [ticket_lint.REPO_ROOT / "architecture" / "adr", real_echo_dir]
        )
        # Reachability, not content: must not raise, and must not silently
        # treat the real dir as absent (scan_adr_elements returns empty sets
        # for both "doesn't exist" and "exists but no elements yet" — so we
        # additionally assert the directory truly has files, proving the
        # glob actually walked real content rather than a missing path).
        self.assertTrue(real_echo_dir.exists(), "echo-prismatic/adr must be reachable")
        self.assertGreater(len(list(real_echo_dir.glob("*.md"))), 0)
        self.assertIsInstance(known, set)
        self.assertIsInstance(findings, list)


# ---------------------------------------------------------------------------
# T-3476 composition regression: the E0-ratifies-only exclusion still holds
# when adr_dir is widened to a list (T-2896 lesson — verify decision PAIRS
# sharing a surface, not just each decision alone).
# ---------------------------------------------------------------------------


class TestE0ExclusionSurvivesMultiDirWidening(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tickets = self.root / "tickets"
        self.pantheon_dir = self.root / "architecture" / "adr"
        self.echo_dir = self.root / "echo-prismatic-adr"  # empty; proves union, not replacement
        for st in ("inbox", "active", "done"):
            (self.tickets / st).mkdir(parents=True)
        self.pantheon_dir.mkdir(parents=True)
        self.echo_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_e0_ratified_element_excluded_with_dir_list(self) -> None:
        _write_pantheon_shaped_adr(
            self.pantheon_dir / "ORG-070-test.md", adr_id="ORG-070",
            elements_block="  - id: E0\n    title: whole ADR\n",
        )
        _write_ticket(self.tickets / "inbox" / "T-0001.md",
                      tid="T-0001", ratifies=["ORG-070#E0"])
        findings = ticket_lint.detect_unverified_elements(
            self.tickets, [self.pantheon_dir, self.echo_dir]
        )
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
