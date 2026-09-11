"""Tests for tools/ticket_lint.py.

Run from repo root:
    python3 -m unittest tools.test_ticket_lint -v

Covers:
  - Unit: each malformation class detected correctly; auto-fix produces correct
    frontmatter; ratify-without-ratifiable_files is FLAGGED not fixed; well-formed
    tickets are left untouched (idempotent on clean).
  - Integration (Layer-3 T-0259 fixture): seed tmp repo with malformed + clean
    tickets; run the tool; assert auto-fixes land, flagged items are reported,
    real repo HEAD unmoved, no push to real origin.

HERMES byte-sequence discipline: the contiguous trigger is never written in this
source file. Where test fixtures need to reference the filename concept, they use
ticket ids and prose that don't contain the trigger.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools._test_fixtures import (
    IntegrationEnv,
    assert_real_repo_untouched,
    git_head,
    setup_integration_env,
    snapshot_real_repo,
    teardown_integration_env,
)

# ---------------------------------------------------------------------------
# Lazy import guard — ticket_lint doesn't exist yet; failing import is the
# "red" state for TDD. Once the module exists, these become live imports.
# ---------------------------------------------------------------------------
try:
    from tools import ticket_lint
    _LINT_AVAILABLE = True
except ImportError:
    _LINT_AVAILABLE = False
    ticket_lint = None  # type: ignore[assignment]


def _require_lint(test_case: unittest.TestCase) -> None:
    if not _LINT_AVAILABLE:
        test_case.skipTest("ticket_lint module not yet available")


# ---------------------------------------------------------------------------
# Fixture helpers.
# ---------------------------------------------------------------------------


def _make_ticket(
    path: Path,
    *,
    tid: str,
    state: str = "inbox",
    target: str = "orchestrator",
    requester: str = "operator",
    intent: str | None = None,
    ratifiable_files: list[str] | None = None,
    extra_fields: dict | None = None,
) -> None:
    """Write a minimal valid ticket file for testing."""
    fm_lines = [
        "---",
        f"id: {tid}",
        "created: 2026-05-20T10:00:00Z",
        f"requester: {requester}",
        f"target: {target}",
        f"state: {state}",
        "parent_ticket: null",
        "depends_on: []",
    ]
    if ratifiable_files is not None:
        items = ", ".join(ratifiable_files)
        fm_lines.append(f"ratifiable_files: [{items}]")
    if intent is not None:
        fm_lines.append(f"intent: {intent}")
    if extra_fields:
        for k, v in extra_fields.items():
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")

    body = (
        f"\n# Test ticket {tid}\n\n"
        "## Ask\n\nDo the thing.\n\n"
        "## Context\n\nBecause reasons.\n\n"
        "## Transition log\n\n"
        f"- 2026-05-20T10:00:00Z [operator] filed in inbox\n"
    )
    path.write_text("\n".join(fm_lines) + body, encoding="utf-8")


def _make_malformed_ticket_no_state(path: Path, *, tid: str) -> None:
    """Write a ticket missing the required 'state' field."""
    content = (
        "---\n"
        f"id: {tid}\n"
        "created: 2026-05-20T10:00:00Z\n"
        "requester: operator\n"
        "target: orchestrator\n"
        "parent_ticket: null\n"
        "depends_on: []\n"
        "---\n"
        "\n# Malformed ticket\n\n"
        "## Ask\n\nfoo\n\n"
        "## Transition log\n\n"
        "- 2026-05-20T10:00:00Z [operator] filed in inbox\n"
    )
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit: detect + classify malformation classes.
# ---------------------------------------------------------------------------


class TestLintDetect(unittest.TestCase):
    """Test that detect_malformations returns the right LintResult for each class."""

    def setUp(self) -> None:
        _require_lint(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "inbox").mkdir()
        (self.root / "active").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_detect(self, path: Path):
        return ticket_lint.detect_malformations(path)

    def test_autofix_target_operator_no_intent_no_ratifiable(self) -> None:
        """target=operator, intent absent, no ratifiable_files → AUTO-FIX intent=action."""
        p = self.root / "inbox" / "T-0001.md"
        _make_ticket(p, tid="T-0001", target="operator")
        result = self._run_detect(p)
        self.assertEqual(result.action, ticket_lint.LintAction.AUTO_FIX)
        self.assertIn("intent", result.description.lower())

    def test_autofix_target_operator_no_intent_with_ratifiable(self) -> None:
        """target=operator, intent absent, ratifiable_files present → AUTO-FIX intent=ratify."""
        p = self.root / "inbox" / "T-0002.md"
        _make_ticket(
            p,
            tid="T-0002",
            target="operator",
            ratifiable_files=["pantheon-config:architecture/adr/ORG-001-test.md"],
        )
        result = self._run_detect(p)
        self.assertEqual(result.action, ticket_lint.LintAction.AUTO_FIX)
        self.assertIn("ratify", result.description.lower())

    def test_autofix_non_operator_target_with_intent(self) -> None:
        """target=recovery, intent=action → AUTO-FIX (drop intent)."""
        p = self.root / "active" / "T-0003.md"
        _make_ticket(p, tid="T-0003", target="recovery", intent="action")
        result = self._run_detect(p)
        self.assertEqual(result.action, ticket_lint.LintAction.AUTO_FIX)
        self.assertIn("drop", result.description.lower())

    def test_flag_ratify_without_ratifiable_files(self) -> None:
        """target=operator, intent=ratify, no ratifiable_files → FLAG (unratifiable class)."""
        p = self.root / "inbox" / "T-0004.md"
        _make_ticket(p, tid="T-0004", target="operator", intent="ratify")
        result = self._run_detect(p)
        self.assertEqual(result.action, ticket_lint.LintAction.FLAG)
        self.assertIn("ratifiable", result.description.lower())

    def test_flag_ratify_stale_path(self) -> None:
        """target=operator, intent=ratify, pantheon-config path absent, requester=operator → FLAG.

        requester defaults to operator here, so there is no originator to bounce
        to → STALE-PATH stays FLAG (the no-bounceable-originator exception).
        """
        p = self.root / "inbox" / "T-0005.md"
        _make_ticket(
            p,
            tid="T-0005",
            target="operator",
            intent="ratify",
            ratifiable_files=["pantheon-config:architecture/adr/NONEXISTENT-FILE.md"],
        )
        # Run with a repo_root where that path doesn't exist.
        result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.FLAG)
        self.assertIn("stale-path", result.description.lower())

    def test_flag_missing_required_field(self) -> None:
        """Ticket missing 'state' required field → FLAG (parse error)."""
        p = self.root / "inbox" / "T-0006.md"
        _make_malformed_ticket_no_state(p, tid="T-0006")
        result = self._run_detect(p)
        self.assertEqual(result.action, ticket_lint.LintAction.FLAG)
        self.assertIn("state", result.description.lower())

    def test_clean_operator_ticket_with_intent_noop(self) -> None:
        """target=operator, intent=action, no ratifiable_files → CLEAN."""
        p = self.root / "inbox" / "T-0007.md"
        _make_ticket(p, tid="T-0007", target="operator", intent="action")
        result = self._run_detect(p)
        self.assertEqual(result.action, ticket_lint.LintAction.CLEAN)

    def test_clean_non_operator_ticket_no_intent_noop(self) -> None:
        """target=recovery, no intent → CLEAN."""
        p = self.root / "active" / "T-0008.md"
        _make_ticket(p, tid="T-0008", target="recovery")
        result = self._run_detect(p)
        self.assertEqual(result.action, ticket_lint.LintAction.CLEAN)

    def test_clean_operator_ratify_with_valid_path(self) -> None:
        """target=operator, intent=ratify, ratifiable_files path exists on disk → CLEAN."""
        # Create the referenced file so the path-exists check passes.
        (self.root / "architecture" / "adr").mkdir(parents=True)
        (self.root / "architecture" / "adr" / "ORG-TEST.md").write_text("# test\n")
        p = self.root / "inbox" / "T-0009.md"
        _make_ticket(
            p,
            tid="T-0009",
            target="operator",
            intent="ratify",
            ratifiable_files=["pantheon-config:architecture/adr/ORG-TEST.md"],
        )
        result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.CLEAN)


# ---------------------------------------------------------------------------
# Unit: repos: field structural gate (T-0761).
# ---------------------------------------------------------------------------


class TestReposFieldGate(unittest.TestCase):
    """detect_malformations validates the repos: field against repos.yaml (T-0761).

    FLAG (never auto-fix) on unknown repo / ungranted rw / no grant / bad access;
    FAIL-OPEN (CLEAN) when the registry is absent; untouched when no repos: field.
    """

    def setUp(self) -> None:
        _require_lint(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "inbox").mkdir()
        (self.root / "active").mkdir()
        self._write_registry()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_registry(self) -> None:
        (self.root / "repos.yaml").write_text(
            "schema_version: 1\n"
            "repos:\n"
            "  - name: pantheon-config\n"
            "    grants: {orchestrator: rw, specialist: rw, recovery: rw}\n"
            "  - name: revenue-project\n"
            "    grants: {orchestrator: rw, specialist: ro}\n"
            "  - name: pantheon-pwa\n"
            "    grants: {orchestrator: rw}\n",
            encoding="utf-8",
        )

    def _ticket(self, tid: str, repos: str, target: str = "specialist") -> Path:
        p = self.root / "active" / f"{tid}.md"
        _make_ticket(
            p, tid=tid, target=target, state="active",
            extra_fields={"repos": repos},
        )
        return p

    def test_valid_ro_grant_clean(self) -> None:
        p = self._ticket("T-0001", "[revenue-project:ro]")  # specialist has ro
        r = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(r.action, ticket_lint.LintAction.CLEAN)

    def test_valid_rw_grant_clean(self) -> None:
        p = self._ticket("T-0002", "[pantheon-config:rw]")  # specialist has rw
        r = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(r.action, ticket_lint.LintAction.CLEAN)

    def test_default_access_is_ro(self) -> None:
        p = self._ticket("T-0003", "[revenue-project]")  # no :access → ro; specialist has ro
        r = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(r.action, ticket_lint.LintAction.CLEAN)

    def test_unknown_repo_flag(self) -> None:
        p = self._ticket("T-0004", "[nonexistent-repo:ro]")
        r = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(r.action, ticket_lint.LintAction.FLAG)
        self.assertIn("unknown repo", r.description.lower())

    def test_ungranted_rw_flag(self) -> None:
        p = self._ticket("T-0005", "[revenue-project:rw]")  # specialist has only ro
        r = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(r.action, ticket_lint.LintAction.FLAG)
        self.assertIn("rw", r.description.lower())

    def test_no_grant_flag(self) -> None:
        p = self._ticket("T-0006", "[pantheon-pwa:ro]")  # specialist has no grant
        r = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(r.action, ticket_lint.LintAction.FLAG)
        self.assertIn("no grant", r.description.lower())

    def test_bad_access_flag(self) -> None:
        p = self._ticket("T-0007", "[revenue-project:xx]")
        r = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(r.action, ticket_lint.LintAction.FLAG)
        self.assertIn("access", r.description.lower())

    def test_fail_open_no_registry(self) -> None:
        (self.root / "repos.yaml").unlink()  # no registry present
        p = self._ticket("T-0008", "[revenue-project:rw]")  # would be invalid IF a registry existed
        r = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(r.action, ticket_lint.LintAction.CLEAN)  # fail-open

    def test_no_repos_field_clean(self) -> None:
        p = self.root / "active" / "T-0009.md"
        _make_ticket(p, tid="T-0009", target="specialist", state="active")
        r = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(r.action, ticket_lint.LintAction.CLEAN)

    # ORG-030 Phase 1: role-keyed grants resolution tests.

    def test_role_key_grant_lookup_clean(self) -> None:
        """target=specialist resolves to specialist; registry has specialist:rw → CLEAN."""
        p = self._ticket("T-0010", "[pantheon-config:rw]", target="specialist")
        r = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(r.action, ticket_lint.LintAction.CLEAN)

    def test_orchestrator_target_resolves_to_orchestrator_role_clean(self) -> None:
        """target=orchestrator resolves to orchestrator; registry has orchestrator:rw → CLEAN."""
        p = self._ticket("T-0011", "[pantheon-config:rw]", target="orchestrator")
        r = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(r.action, ticket_lint.LintAction.CLEAN)

    def test_no_role_grant_flag(self) -> None:
        """target=specialist (→specialist); repo has only orchestrator:rw → FLAG no grant."""
        p = self._ticket("T-0012", "[pantheon-pwa:ro]", target="specialist")
        r = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(r.action, ticket_lint.LintAction.FLAG)
        self.assertIn("no grant", r.description.lower())


# ---------------------------------------------------------------------------
# Unit: auto-fix correctness.
# ---------------------------------------------------------------------------


class TestAutoFix(unittest.TestCase):
    """Test that apply_auto_fix rewrites the ticket correctly."""

    def setUp(self) -> None:
        _require_lint(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "inbox").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fix_adds_intent_action_when_no_ratifiable(self) -> None:
        """target=operator, no intent, no ratifiable_files → intent: action written to disk."""
        p = self.root / "inbox" / "T-0010.md"
        _make_ticket(p, tid="T-0010", target="operator")
        ticket_lint.apply_auto_fix(p)
        text = p.read_text(encoding="utf-8")
        self.assertIn("intent: action", text)

    def test_fix_adds_intent_ratify_when_ratifiable_present(self) -> None:
        """target=operator, no intent, ratifiable_files present → intent: ratify written."""
        p = self.root / "inbox" / "T-0011.md"
        _make_ticket(
            p,
            tid="T-0011",
            target="operator",
            ratifiable_files=["pantheon-config:architecture/adr/ORG-001-test.md"],
        )
        ticket_lint.apply_auto_fix(p)
        text = p.read_text(encoding="utf-8")
        self.assertIn("intent: ratify", text)

    def test_fix_drops_intent_for_non_operator_target(self) -> None:
        """target=recovery, intent=action → intent field removed from frontmatter."""
        p = self.root / "inbox" / "T-0012.md"
        _make_ticket(p, tid="T-0012", target="recovery", intent="action")
        # Verify intent is present before fix.
        self.assertIn("intent: action", p.read_text(encoding="utf-8"))
        ticket_lint.apply_auto_fix(p)
        text = p.read_text(encoding="utf-8")
        self.assertNotIn("intent:", text)

    def test_fix_preserves_body(self) -> None:
        """Auto-fix only touches frontmatter — body is preserved byte-for-byte."""
        from tools.ticket import parse_front_matter
        p = self.root / "inbox" / "T-0013.md"
        _make_ticket(p, tid="T-0013", target="operator")
        original_text = p.read_text(encoding="utf-8")
        _original_fields, original_body = parse_front_matter(original_text)
        ticket_lint.apply_auto_fix(p)
        fixed_text = p.read_text(encoding="utf-8")
        _fixed_fields, fixed_body = parse_front_matter(fixed_text)
        self.assertEqual(original_body, fixed_body)

    def test_fix_is_idempotent(self) -> None:
        """Running apply_auto_fix twice on the same ticket yields the same result."""
        p = self.root / "inbox" / "T-0014.md"
        _make_ticket(p, tid="T-0014", target="operator")
        ticket_lint.apply_auto_fix(p)
        after_first = p.read_text(encoding="utf-8")
        ticket_lint.apply_auto_fix(p)
        after_second = p.read_text(encoding="utf-8")
        self.assertEqual(after_first, after_second)

    def test_flag_class_not_auto_fixed(self) -> None:
        """FLAG class (ratify-without-ratifiable_files) is not touched by apply_auto_fix."""
        p = self.root / "inbox" / "T-0015.md"
        _make_ticket(p, tid="T-0015", target="operator", intent="ratify")
        original = p.read_text(encoding="utf-8")
        # apply_auto_fix should refuse or no-op on FLAG class tickets.
        with self.assertRaises(ticket_lint.LintError):
            ticket_lint.apply_auto_fix(p)
        # File must be unchanged.
        self.assertEqual(p.read_text(encoding="utf-8"), original)


# ---------------------------------------------------------------------------
# Unit: digest rendering.
# ---------------------------------------------------------------------------


class TestBuildLintDigest(unittest.TestCase):
    """Test that build_lint_digest formats fixed + flagged items correctly."""

    def setUp(self) -> None:
        _require_lint(self)

    def test_all_clean_renders_proof_of_life(self) -> None:
        msg = ticket_lint.build_lint_digest(
            now_iso="2026-05-20 10:00 UTC",
            auto_fixed=[],
            flagged=[],
            total_scanned=5,
        )
        self.assertIn("clean", msg.lower())
        self.assertIn("5", msg)

    def test_auto_fixed_listed(self) -> None:
        msg = ticket_lint.build_lint_digest(
            now_iso="2026-05-20 10:00 UTC",
            auto_fixed=[
                ticket_lint.LintResult(
                    tid="T-0001",
                    path=Path("/tmp/T-0001.md"),
                    action=ticket_lint.LintAction.AUTO_FIX,
                    description="added intent: action",
                ),
            ],
            flagged=[],
            total_scanned=3,
        )
        self.assertIn("T-0001", msg)
        self.assertIn("auto-fix", msg.lower())

    def test_flagged_listed(self) -> None:
        msg = ticket_lint.build_lint_digest(
            now_iso="2026-05-20 10:00 UTC",
            auto_fixed=[],
            flagged=[
                ticket_lint.LintResult(
                    tid="T-0002",
                    path=Path("/tmp/T-0002.md"),
                    action=ticket_lint.LintAction.FLAG,
                    description="intent=ratify but no ratifiable_files",
                ),
            ],
            total_scanned=3,
        )
        self.assertIn("T-0002", msg)
        self.assertIn("flag", msg.lower())

    def test_unverified_elements_section_rendered(self) -> None:
        """T-2843: the element-lineage leak-detector's findings render in
        their own digest section when passed through typed_graph_findings."""
        msg = ticket_lint.build_lint_digest(
            now_iso="2026-08-24 10:00 UTC",
            auto_fixed=[],
            flagged=[],
            total_scanned=1,
            typed_graph_findings=[
                ticket_lint.TypedGraphFinding(
                    kind="unverified_element",
                    detail="ORG-065#E1 has no ticket verifies: edge (open or "
                           "done) — unverified obligation, ORG-065 §3.4",
                ),
            ],
        )
        self.assertIn("Unverified ADR elements", msg)
        self.assertIn("ORG-065#E1", msg)

    def test_implemented_unverified_elements_section_rendered_distinctly(self) -> None:
        """T-3179: a true leak and an implemented-but-unverified element
        render in two DISTINCT sections, each with its own correct count —
        the true-leak section must not absorb the implemented one."""
        msg = ticket_lint.build_lint_digest(
            now_iso="2026-08-25 10:00 UTC",
            auto_fixed=[],
            flagged=[],
            total_scanned=1,
            typed_graph_findings=[
                ticket_lint.TypedGraphFinding(
                    kind="unverified_element",
                    detail="ORG-065#E1 has no ticket verifies: edge (open or "
                           "done) — unverified obligation, ORG-065 §3.4",
                ),
                ticket_lint.TypedGraphFinding(
                    kind="implemented_unverified_element",
                    detail="ORG-065#E2 implemented by T-0001 but has no "
                           "ticket verifies: edge — expected transitional "
                           "shape, ORG-065 §3.4",
                ),
            ],
        )
        self.assertIn("Unverified ADR elements", msg)
        self.assertIn("Implemented, evidence pending", msg)
        self.assertIn("ORG-065#E1", msg)
        self.assertIn("ORG-065#E2", msg)
        self.assertIn("T-0001", msg)
        # Distinct sections: each header's own count is 1, not 2.
        self.assertIn("(1):", msg)
        self.assertNotIn("(2):", msg)

    def test_no_markdown_parse_mode(self) -> None:
        """Digest must be plain text — no Markdown parse_mode-triggering chars."""
        msg = ticket_lint.build_lint_digest(
            now_iso="2026-05-20 10:00 UTC",
            auto_fixed=[],
            flagged=[
                ticket_lint.LintResult(
                    tid="T-0003",
                    path=Path("/tmp/T-0003.md"),
                    action=ticket_lint.LintAction.FLAG,
                    description="test_flag",
                ),
            ],
            total_scanned=1,
        )
        self.assertNotIn("**", msg)


# ---------------------------------------------------------------------------
# Unit: CLI flags.
# ---------------------------------------------------------------------------


class TestCLIFlags(unittest.TestCase):
    """Test the default-safe contract and CLI flag behavior."""

    def setUp(self) -> None:
        _require_lint(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "tickets" / "inbox").mkdir(parents=True)
        (self.root / "tickets" / "active").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_flags_refuses(self) -> None:
        """Default-safe contract: bare invocation without explicit mode must refuse."""
        rc = ticket_lint.main([])
        self.assertEqual(rc, 64)

    def test_dry_run_no_writes_no_commit_no_digest(self) -> None:
        """--dry-run: scan + report, no filesystem writes, no git ops, no Telegram send."""
        p = self.root / "tickets" / "inbox" / "T-0020.md"
        _make_ticket(p, tid="T-0020", target="operator")
        original = p.read_text(encoding="utf-8")

        with patch.object(ticket_lint, "_send_lint_digest") as mock_send, \
             patch.object(ticket_lint, "_git_commit_fixes") as mock_commit:
            rc = ticket_lint.main(["--dry-run", "--repo-root", str(self.root)])
        self.assertEqual(rc, 0)
        mock_send.assert_not_called()
        mock_commit.assert_not_called()
        # File must be untouched.
        self.assertEqual(p.read_text(encoding="utf-8"), original)

    def test_repo_root_override_used_for_scan(self) -> None:
        """--repo-root <path> redirects the scan to the tmp repo (safe mode)."""
        p = self.root / "tickets" / "inbox" / "T-0021.md"
        _make_ticket(p, tid="T-0021", target="operator")

        with patch.object(ticket_lint, "_send_lint_digest") as mock_send, \
             patch.object(ticket_lint, "_git_commit_fixes") as mock_commit:
            rc = ticket_lint.main(["--repo-root", str(self.root)])
        self.assertEqual(rc, 0)
        # In safe --repo-root mode: digest is sent but no production push.
        mock_commit.assert_not_called()


# ---------------------------------------------------------------------------
# Unit: scan_tickets helper.
# ---------------------------------------------------------------------------


class TestScanTickets(unittest.TestCase):
    """Test the scan loop over inbox + active dirs."""

    def setUp(self) -> None:
        _require_lint(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "inbox").mkdir()
        (self.root / "active").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_scan_covers_inbox_and_active(self) -> None:
        """scan_tickets covers both inbox/ and active/ dirs."""
        p1 = self.root / "inbox" / "T-0030.md"
        p2 = self.root / "active" / "T-0031.md"
        _make_ticket(p1, tid="T-0030", target="operator")
        _make_ticket(p2, tid="T-0031", target="recovery")
        results = ticket_lint.scan_tickets(self.root)
        tids = {r.tid for r in results}
        self.assertIn("T-0030", tids)
        self.assertIn("T-0031", tids)

    def test_scan_skips_non_ticket_files(self) -> None:
        """scan_tickets ignores .gitkeep and non-T-NNNN files."""
        (self.root / "inbox" / ".gitkeep").write_text("")
        (self.root / "inbox" / "README.md").write_text("# readme\n")
        results = ticket_lint.scan_tickets(self.root)
        for r in results:
            self.assertRegex(r.tid, r"T-\d{4}")

    def test_scan_empty_dirs_returns_empty(self) -> None:
        results = ticket_lint.scan_tickets(self.root)
        self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# Unit: floating-ratification detection (T-0784).
# ---------------------------------------------------------------------------


def _write_artifact(path: Path, *, body: str) -> None:
    """Write a ratifiable artifact (.md) with the given body."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _write_ratify_ticket(
    path: Path,
    *,
    tid: str,
    ratifiable_files: list[str],
    state: str = "active",
) -> None:
    """Write an OPEN target=operator intent=ratify ticket naming ratifiable_files."""
    _make_ticket(
        path,
        tid=tid,
        target="operator",
        intent="ratify",
        state=state,
        ratifiable_files=ratifiable_files,
    )


class TestOrphanRatifications(unittest.TestCase):
    """detect_orphan_ratifications flags awaiting-ratification artifacts with no
    open ratify ticket (T-0784). Report-only; tolerant status-convention match."""

    def setUp(self) -> None:
        _require_lint(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        # Artifact substrates + ticket dirs.
        (self.repo / "architecture" / "adr").mkdir(parents=True)
        (self.repo / "sop" / "standard").mkdir(parents=True)
        (self.repo / "rulebook").mkdir()
        self.tickets = self.repo / "tickets"
        (self.tickets / "inbox").mkdir(parents=True)
        (self.tickets / "active").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _detect(self):
        return ticket_lint.detect_orphan_ratifications(self.repo, self.tickets)

    # ---- the three required cases (handoff spec) ----

    def test_awaiting_with_open_ratify_ticket_no_finding(self) -> None:
        """(a) awaiting + an open ratify ticket naming it → NOT an orphan."""
        art = self.repo / "architecture" / "adr" / "ORG-900-test.md"
        _write_artifact(
            art,
            body="# ORG-900\n\n**Status:** Drafted 2026-06-01. Awaiting the operator ratification.\n",
        )
        _write_ratify_ticket(
            self.tickets / "active" / "T-0900.md",
            tid="T-0900",
            ratifiable_files=["pantheon-config:architecture/adr/ORG-900-test.md"],
        )
        orphans = self._detect()
        self.assertEqual([o.rel_path for o in orphans], [])

    def test_awaiting_without_ratify_ticket_is_orphan(self) -> None:
        """(b) awaiting + no open ratify ticket → orphan finding."""
        art = self.repo / "sop" / "standard" / "floating-sop.md"
        _write_artifact(
            art,
            body="# Floating SOP\n\n**Status:** Authored 2026-05-11. Awaiting ratification.\n",
        )
        orphans = self._detect()
        self.assertEqual([o.rel_path for o in orphans], ["sop/standard/floating-sop.md"])
        self.assertIn("awaiting", orphans[0].status_excerpt.lower())

    def test_ratified_artifact_no_finding(self) -> None:
        """(c) a Ratified artifact → never an orphan (even with no ticket)."""
        art = self.repo / "architecture" / "adr" / "ORG-901-done.md"
        _write_artifact(
            art,
            body="# ORG-901\n\n**Status:** Ratified 2026-05-20 by the operator (msg_x).\n",
        )
        orphans = self._detect()
        self.assertEqual([o.rel_path for o in orphans], [])

    # ---- robustness: status-convention variety ----

    def test_frontmatter_status_pending(self) -> None:
        """Frontmatter `status:` awaiting → orphan; body prose is not consulted."""
        art = self.repo / "architecture" / "adr" / "ORG-902-fm.md"
        _write_artifact(
            art,
            body=(
                "---\n"
                "title: test\n"
                "status: Authored 2026-05-19 awaiting the operator ratification\n"
                "---\n"
                "# ORG-902\n\nBody.\n"
            ),
        )
        orphans = self._detect()
        self.assertEqual([o.rel_path for o in orphans], ["architecture/adr/ORG-902-fm.md"])

    def test_frontmatter_ratified_overrides_body_awaiting_mention(self) -> None:
        """Frontmatter Ratified is canonical even if body prose mentions awaiting."""
        art = self.repo / "architecture" / "adr" / "ORG-903-fm-ratified.md"
        _write_artifact(
            art,
            body=(
                "---\n"
                "status: Ratified 2026-05-17\n"
                "---\n"
                "# ORG-903\n\n"
                "Related: T-0387 (ratification ticket, awaiting the operator).\n"
            ),
        )
        orphans = self._detect()
        self.assertEqual([o.rel_path for o in orphans], [])

    def test_negated_awaiting_not_flagged(self) -> None:
        """'NOT yet awaiting ratification' (pre-draft) → not an orphan."""
        art = self.repo / "sop" / "standard" / "not-yet.md"
        _write_artifact(
            art,
            body=(
                "# Not yet\n\n"
                "**Status:** Drafted 2026-05-27. **Drafted, NOT yet awaiting "
                "ratification** — phase 1 isn't closed.\n"
            ),
        )
        orphans = self._detect()
        self.assertEqual([o.rel_path for o in orphans], [])

    def test_multiline_status_continuation(self) -> None:
        """A wrapped multi-line **Status:** paragraph is matched as one block."""
        art = self.repo / "sop" / "standard" / "wrapped.md"
        _write_artifact(
            art,
            body=(
                "# Wrapped\n\n"
                "**Status:** Drafted 2026-05-13 by Recovery per the operator msg_1209\n"
                "(ratifying Recovery msg_1210 diagnosis).\n"
                "Awaiting the operator ratification.\n"
            ),
        )
        orphans = self._detect()
        self.assertEqual([o.rel_path for o in orphans], ["sop/standard/wrapped.md"])

    def test_status_section_heading(self) -> None:
        """A `## Status` section heading's paragraph is consulted."""
        art = self.repo / "sop" / "standard" / "section.md"
        _write_artifact(
            art,
            body=(
                "# Section\n\n## When\n\nstuff\n\n## Status\n\n"
                "**Drafted 2026-05-12 by Recovery.** Awaiting the operator ratification "
                "before becoming precedent.\n"
            ),
        )
        orphans = self._detect()
        self.assertEqual([o.rel_path for o in orphans], ["sop/standard/section.md"])

    def test_readme_excluded(self) -> None:
        """README.md index files are excluded even if they mention awaiting ratification."""
        _write_artifact(
            self.repo / "sop" / "standard" / "README.md",
            body="# Index\n\nArtifacts move draft → awaiting ratification → ratified.\n",
        )
        orphans = self._detect()
        self.assertEqual([o.rel_path for o in orphans], [])

    def test_ratify_ticket_in_inbox_also_covers(self) -> None:
        """An open ratify ticket in inbox/ (not just active/) provides coverage."""
        art = self.repo / "architecture" / "adr" / "ORG-904.md"
        _write_artifact(
            art,
            body="# ORG-904\n\n**Status:** Drafted. Awaiting ratification.\n",
        )
        _write_ratify_ticket(
            self.tickets / "inbox" / "T-0904.md",
            tid="T-0904",
            ratifiable_files=["pantheon-config:architecture/adr/ORG-904.md"],
            state="inbox",
        )
        orphans = self._detect()
        self.assertEqual([o.rel_path for o in orphans], [])

    def test_non_ratify_ticket_does_not_cover(self) -> None:
        """A ticket that is NOT target=operator intent=ratify provides no coverage."""
        art = self.repo / "architecture" / "adr" / "ORG-905.md"
        _write_artifact(
            art,
            body="# ORG-905\n\n**Status:** Drafted. Awaiting ratification.\n",
        )
        # A plain action ticket that happens to mention the file — no coverage.
        _make_ticket(
            self.tickets / "active" / "T-0905.md",
            tid="T-0905",
            target="recovery",
            state="active",
        )
        orphans = self._detect()
        self.assertEqual([o.rel_path for o in orphans], ["architecture/adr/ORG-905.md"])

    def test_digest_includes_orphan_section(self) -> None:
        """build_lint_digest renders a floating-ratifications section."""
        msg = ticket_lint.build_lint_digest(
            now_iso="2026-06-04 18:00 UTC",
            auto_fixed=[],
            flagged=[],
            total_scanned=2,
            orphan_ratifications=[
                ticket_lint.OrphanRatification(
                    rel_path="architecture/adr/ORG-906.md",
                    status_excerpt="**Status:** Awaiting ratification.",
                ),
            ],
        )
        self.assertIn("architecture/adr/ORG-906.md", msg)
        self.assertIn("floating ratification", msg.lower())


# ---------------------------------------------------------------------------
# Integration: Layer-3 fixture.
# ---------------------------------------------------------------------------


class TestTicketLintInteg(unittest.TestCase):
    """Layer-3 integration test: real subprocess + tmp git repo.

    Seeded with malformed + well-formed tickets; asserts auto-fixes land in
    the worktree commit, flagged items appear in digest output, real repo HEAD
    unmoved, no push to real origin.

    Uses --repo-root (safe mode) so with_worktree's real git fetch/worktree-add
    is not exercised (those require a live remote); worktree isolation is covered
    at the unit level by patching _with_worktree (mirroring ticket_grooming pattern).
    """

    def setUp(self) -> None:
        _require_lint(self)
        self._real_snapshot = snapshot_real_repo()
        self._env = setup_integration_env()
        # Set up tickets dirs in tmp repo.
        (self._env.repo_path / "tickets" / "inbox").mkdir(parents=True)
        (self._env.repo_path / "tickets" / "active").mkdir(parents=True)

    def tearDown(self) -> None:
        teardown_integration_env(self._env)
        assert_real_repo_untouched(self._real_snapshot)

    def _run_lint(self, extra_args: list[str] | None = None) -> tuple[int, str, str]:
        from tools._test_fixtures import run_script
        args = ["--repo-root", str(self._env.repo_path)] + (extra_args or [])
        return run_script("ticket_lint", args)

    def test_autofix_ticket_written_in_tmp_repo(self) -> None:
        """A malformed ticket (no intent) gets fixed; tmp repo unchanged otherwise."""
        p = self._env.repo_path / "tickets" / "inbox" / "T-0050.md"
        _make_ticket(p, tid="T-0050", target="operator")
        # Verify malformation before run.
        self.assertNotIn("intent:", p.read_text())

        rc, stdout, stderr = self._run_lint(["--no-digest"])
        self.assertEqual(rc, 0, f"stdout={stdout!r} stderr={stderr!r}")
        # Auto-fix should have written intent.
        self.assertIn("intent:", p.read_text())

    def test_flagged_ticket_not_modified(self) -> None:
        """FLAG class ticket (ratify-without-ratifiable_files) is not touched."""
        p = self._env.repo_path / "tickets" / "inbox" / "T-0051.md"
        _make_ticket(p, tid="T-0051", target="operator", intent="ratify")
        original = p.read_text()

        rc, stdout, stderr = self._run_lint(["--no-digest"])
        self.assertEqual(rc, 0, f"stdout={stdout!r} stderr={stderr!r}")
        # File must be unchanged.
        self.assertEqual(p.read_text(), original)

    def test_clean_ticket_not_modified(self) -> None:
        """Well-formed ticket is left unchanged."""
        p = self._env.repo_path / "tickets" / "active" / "T-0052.md"
        _make_ticket(p, tid="T-0052", target="recovery")
        original = p.read_text()

        rc, stdout, stderr = self._run_lint(["--no-digest"])
        self.assertEqual(rc, 0, f"stdout={stdout!r} stderr={stderr!r}")
        self.assertEqual(p.read_text(), original)

    def test_real_repo_head_unmoved(self) -> None:
        """Real repo HEAD must not move — tmp run doesn't commit to real repo."""
        p = self._env.repo_path / "tickets" / "inbox" / "T-0053.md"
        _make_ticket(p, tid="T-0053", target="operator")
        self._run_lint(["--no-digest"])
        # assert_real_repo_untouched is called in tearDown — this test just
        # exercises a realistic scenario; tearDown is the structural guard.

    def test_dry_run_leaves_files_untouched(self) -> None:
        """--dry-run leaves all files untouched even if malformations exist."""
        p = self._env.repo_path / "tickets" / "inbox" / "T-0054.md"
        _make_ticket(p, tid="T-0054", target="operator")
        original = p.read_text()

        rc, stdout, stderr = self._run_lint(["--dry-run"])
        self.assertEqual(rc, 0, f"stdout={stdout!r} stderr={stderr!r}")
        self.assertEqual(p.read_text(), original)


# ---------------------------------------------------------------------------
# Unit: T-0556 — cross-repo ratifiable resolution (fail-open) + RETARGET class.
# ---------------------------------------------------------------------------


class TestRatifiableResolutionAndRetarget(unittest.TestCase):
    """detect_malformations classifies ratify tickets via ratifiable_path_status.

    All cases monkeypatch ratifiable_path_status — NO real sudo/git is touched.
    Fail-open property: only positively-confirmed-absent (False) is a defect;
    True (present) and None (unverifiable) are both non-defects (T-0549 fix).
    """

    def setUp(self) -> None:
        _require_lint(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "inbox").mkdir()
        (self.root / "active").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ticket(self, tid: str, *, requester: str, entry: str) -> Path:
        p = self.root / "inbox" / f"{tid}.md"
        _make_ticket(
            p,
            tid=tid,
            target="operator",
            requester=requester,
            intent="ratify",
            ratifiable_files=[entry],
        )
        return p

    def test_pantheon_config_present_clean(self) -> None:
        """Case 1: pantheon-config entry exists → CLEAN."""
        p = self._ticket("T-0560", requester="orchestrator", entry="pantheon-config:adr/X.md")
        with patch.object(ticket_lint, "ratifiable_path_status", return_value=True):
            result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.CLEAN)

    def test_pantheon_config_missing_requester_set_retargets(self) -> None:
        """Case 2: pantheon-config entry absent, requester set (not operator) → RETARGET."""
        p = self._ticket("T-0561", requester="specialist", entry="pantheon-config:adr/Y.md")
        with patch.object(ticket_lint, "ratifiable_path_status", return_value=False):
            result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.RETARGET)
        self.assertEqual(result.requester, "specialist")
        self.assertIn("stale-path", result.description.lower())

    def test_foreign_repo_true_clean_regression(self) -> None:
        """Case 3: foreign repo status True → CLEAN (T-0549 false-positive regression)."""
        p = self._ticket(
            "T-0562", requester="orchestrator", entry="revenue-project:docs/spec.md"
        )
        with patch.object(ticket_lint, "ratifiable_path_status", return_value=True):
            result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.CLEAN)

    def test_foreign_repo_none_unresolvable_clean_failopen(self) -> None:
        """Case 4: foreign repo status None (unverifiable) → CLEAN, no flag, no retarget."""
        p = self._ticket(
            "T-0563", requester="orchestrator", entry="echo-prismatic:adr/Z.md"
        )
        with patch.object(ticket_lint, "ratifiable_path_status", return_value=None):
            result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.CLEAN)

    def test_foreign_repo_false_requester_set_retargets(self) -> None:
        """Case 5: foreign repo status False (confirmed absent), requester set → RETARGET."""
        p = self._ticket(
            "T-0564", requester="orchestrator", entry="pantheon-pwa:src/missing.ts"
        )
        with patch.object(ticket_lint, "ratifiable_path_status", return_value=False):
            result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.RETARGET)
        self.assertEqual(result.requester, "orchestrator")

    def test_status_mapping_true_none_false_to_action(self) -> None:
        """Case 6: True→CLEAN, None→CLEAN (fail-open), False→RETARGET via monkeypatch.

        Verifies the fail-open is STRUCTURAL: each resolver return value maps to
        the expected LintAction regardless of how None arises internally.
        """
        p = self._ticket(
            "T-0565", requester="orchestrator", entry="revenue-project:docs/spec.md"
        )
        for status, expected in (
            (True, ticket_lint.LintAction.CLEAN),
            (None, ticket_lint.LintAction.CLEAN),
            (False, ticket_lint.LintAction.RETARGET),
        ):
            with patch.object(ticket_lint, "ratifiable_path_status", return_value=status):
                result = ticket_lint.detect_malformations(p, repo_root=self.root)
            self.assertEqual(
                result.action, expected,
                f"status={status!r} should map to {expected}",
            )

    def test_no_ratifiable_files_requester_set_retargets(self) -> None:
        """Case 7a: operator+ratify+no ratifiable_files, requester set → RETARGET."""
        p = self.root / "inbox" / "T-0566.md"
        _make_ticket(p, tid="T-0566", target="operator", requester="orchestrator", intent="ratify")
        result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.RETARGET)
        self.assertEqual(result.requester, "orchestrator")
        self.assertIn("unratifiable", result.description.lower())

    def test_no_ratifiable_files_requester_operator_flags(self) -> None:
        """Case 7b: operator+ratify+no ratifiable_files, requester == operator → FLAG."""
        p = self.root / "inbox" / "T-0567.md"
        _make_ticket(p, tid="T-0567", target="operator", requester="operator", intent="ratify")
        result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.FLAG)
        self.assertIsNone(result.requester)

    def test_action_with_participation_waiting_body_soft_flags(self) -> None:
        """T-0655: intent=action target=operator with body phrasing suggesting
        participation-waiting (e.g. 'the operator will drive this when he has time')
        is a calibration-task gated on the operator's body, NOT a decision queue
        item. Soft-FLAG with retarget suggestion."""
        p = self.root / "inbox" / "T-0700.md"
        _make_ticket(
            p, tid="T-0700", target="operator", requester="orchestrator",
            intent="action",
        )
        # Append participation-waiting phrasing to the body.
        p.write_text(
            p.read_text(encoding="utf-8")
            + "\nthe operator will drive this when he has time available.\n",
            encoding="utf-8",
        )
        result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.FLAG)
        self.assertIn("participation-waiting", result.description)
        self.assertIn("blocked_by:event", result.description)

    def test_action_with_co_speaker_waiting_soft_flags(self) -> None:
        """T-0655: same heuristic, different phrasing match (T-0618 shape)."""
        p = self.root / "inbox" / "T-0701.md"
        _make_ticket(
            p, tid="T-0701", target="operator", requester="orchestrator",
            intent="action",
        )
        p.write_text(
            p.read_text(encoding="utf-8")
            + "\nthe operator will drive this once a willing co-speaker is available.\n",
            encoding="utf-8",
        )
        result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.FLAG)

    def test_action_without_participation_waiting_stays_clean(self) -> None:
        """Negative: intent=action with normal action-request body is CLEAN."""
        p = self.root / "inbox" / "T-0702.md"
        _make_ticket(
            p, tid="T-0702", target="operator", requester="orchestrator",
            intent="action",
        )
        # Body says nothing about waiting on the operator's body — should not flag.
        result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.CLEAN)

    def test_noodle_with_decision_gate_body_soft_flags(self) -> None:
        """T-0655: intent=noodle target=operator with body asking for a
        decision (DECISION-GATE / OPTION A / OPTION B) is a mismatch —
        noodle means 'no urgent action' but the body asks the operator to
        pick a path. Soft-FLAG."""
        p = self.root / "inbox" / "T-0703.md"
        _make_ticket(
            p, tid="T-0703", target="operator", requester="orchestrator",
            intent="noodle",
        )
        p.write_text(
            p.read_text(encoding="utf-8")
            + "\nDECISION-GATE: OPTION A or OPTION B?\n",
            encoding="utf-8",
        )
        result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.FLAG)
        self.assertIn("decision-gate", result.description.lower())

    def test_noodle_without_decision_gate_stays_clean(self) -> None:
        """Negative: intent=noodle as a discussion-anchor (T-0563 shape) is
        properly-used noodle and stays CLEAN."""
        p = self.root / "inbox" / "T-0704.md"
        _make_ticket(
            p, tid="T-0704", target="operator", requester="recovery",
            intent="noodle",
        )
        # Body is a discussion anchor — no decision-gate phrasing.
        result = ticket_lint.detect_malformations(p, repo_root=self.root)
        self.assertEqual(result.action, ticket_lint.LintAction.CLEAN)

    def test_retarget_apply_refused_by_auto_fix(self) -> None:
        """apply_auto_fix must refuse RETARGET-class tickets (not a mechanical fix)."""
        p = self._ticket("T-0568", requester="orchestrator", entry="pantheon-config:adr/Q.md")
        with patch.object(ticket_lint, "ratifiable_path_status", return_value=False):
            with self.assertRaises(ticket_lint.LintError):
                ticket_lint.apply_auto_fix(p, repo_root=self.root)


# ---------------------------------------------------------------------------
# T-0558: faithful detached-HEAD integration test for the retarget commit path.
#
# The pre-T-0558 integration test ran on a normal `main` branch, which did NOT
# reproduce the production path. Production uses `with_worktree(..., detach=True)`,
# so the lint's commit lands on a DETACHED HEAD. ticket.py retarget's own
# `git push origin main` is a silent no-op from detached HEAD (commit is on
# detached HEAD; local refs/heads/main is unmoved), and a retarget-ONLY run
# (no co-occurring auto-fix) never reaches origin/main.
#
# This test reproduces the detached HEAD via `git worktree add --detach`, runs
# _do_lint with that worktree path, and asserts the retarget commit actually
# lands on the bare origin's main ref. Pre-T-0558 the test FAILS under the bug;
# post-T-0558 it passes because _do_lint runs a UNIFIED push_with_retry
# (HEAD:main) covering retarget + auto-fix commits.
# ---------------------------------------------------------------------------


class TestRetargetDetachedHeadPush(unittest.TestCase):

    def setUp(self) -> None:
        _require_lint(self)
        self._real_snapshot = snapshot_real_repo()
        self._env = setup_integration_env()
        (self._env.repo_path / "tickets" / "inbox").mkdir(parents=True)
        (self._env.repo_path / "tickets" / "active").mkdir(parents=True)
        # Copy the real tools/ticket.py + its imports so _apply_retarget's
        # subprocess shell-out (python3 <_cwd>/tools/ticket.py retarget ...)
        # finds the tool inside the worktree.
        import shutil
        repo_tools = Path(__file__).resolve().parent
        wt_tools = self._env.repo_path / "tools"
        wt_tools.mkdir(parents=True, exist_ok=True)
        for fname in ("ticket.py", "_git_utils.py", "agent_role_map.py", "hermes_action.py",
                      "_md_table.py",  # T-1936: ticket.py's README-row emitter imports it
                      "priority_list_parse.py"):  # T-3020 BT3: ticket.py imports it at module scope
            src = repo_tools / fname
            if src.exists():
                shutil.copy2(src, wt_tools / fname)
        # ticket.py imports things from rulebook/ etc. — for the retarget code
        # path it just needs to read frontmatter + write transition log + git
        # commit, so we only need the helper modules above.
        from tools._test_fixtures import _run
        (self._env.repo_path / "tickets" / "inbox" / ".gitkeep").touch()
        (self._env.repo_path / "tickets" / "active" / ".gitkeep").touch()
        # ticket.py's _commit_and_push regenerates tickets/README.md; seed
        # the file with the required "## Current state" section header so
        # the rebuild path doesn't refuse on a missing-section guard.
        (self._env.repo_path / "tickets" / "README.md").write_text(
            "# Tickets\n\n## Current state\n\n<!-- AUTOGEN -->\n", encoding="utf-8"
        )
        # Add a done/ dir too — ticket.py scans across states.
        (self._env.repo_path / "tickets" / "done").mkdir(exist_ok=True)
        (self._env.repo_path / "tickets" / "done" / ".gitkeep").touch()
        _run(["git", "add", "tickets/", "tools/"], cwd=self._env.repo_path)
        _run(["git", "commit", "-m", "seed tickets + tools"], cwd=self._env.repo_path)
        _run(["git", "push", "origin", "main"], cwd=self._env.repo_path)

    def tearDown(self) -> None:
        teardown_integration_env(self._env)
        assert_real_repo_untouched(self._real_snapshot)

    def _make_detached_worktree(self) -> Path:
        """Create a detached-HEAD worktree of the repo — the same shape
        with_worktree(..., detach=True) produces in production.
        """
        from tools._test_fixtures import _run
        wt = self._env.base_dir / "lint_wt"
        _run(["git", "worktree", "add", "--detach", str(wt), "origin/main"],
             cwd=self._env.repo_path)
        # Set identity inside the worktree (workspaces don't auto-inherit config).
        _run(["git", "config", "user.email", "lint-wt-test@example"], cwd=wt)
        _run(["git", "config", "user.name", "Lint Worktree Test"], cwd=wt)
        return wt

    def test_retarget_only_run_pushes_to_origin_via_unified_push(self):
        """Seed a RETARGET-class ticket (operator+ratify with positively-absent
        ratifiable_files + non-operator requester). Run _do_lint on a real
        detached-HEAD worktree with push=True. Assert the retarget commit
        landed on the bare origin's main ref AND the ticket's frontmatter
        target flipped to the requester.
        """
        wt = self._make_detached_worktree()
        tid = "T-9558"
        # Ticket: operator+ratify+no ratifiable_files+requester=orchestrator → RETARGET
        # per Case 7a. Lives in the worktree's tickets/inbox/.
        p = wt / "tickets" / "inbox" / f"{tid}.md"
        _make_ticket(p, tid=tid, target="operator", requester="orchestrator", intent="ratify")

        # Snapshot bare origin's HEAD pre-run.
        from tools._test_fixtures import _run as _fxr
        pre_head_proc = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=self._env.bare_origin, capture_output=True, text=True, check=True,
        )
        pre_head = pre_head_proc.stdout.strip()

        # Call _do_lint directly with the worktree path so we exercise the
        # is_commit_path == True branch with a REAL detached HEAD.
        rc = ticket_lint._do_lint(wt, push=True, send_digest=False)
        self.assertEqual(rc, 0)

        # Bare origin's main MUST have advanced — the retarget commit landed.
        post_head_proc = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=self._env.bare_origin, capture_output=True, text=True, check=True,
        )
        post_head = post_head_proc.stdout.strip()
        self.assertNotEqual(
            pre_head, post_head,
            "bare origin main is unmoved — retarget commit did not reach origin "
            "(pre-T-0558 detached-HEAD no-op push bug regression)",
        )

        # Confirm the ticket's frontmatter target flipped to its requester.
        # Re-read from the worktree since that's where the mutation landed.
        text = p.read_text(encoding="utf-8")
        self.assertIn("target: orchestrator", text)
        self.assertNotIn("target: operator", text)


class TestUnanchoredGates(unittest.TestCase):
    """detect_unanchored_gates: T-2833 not_before-anchor scan (unanchored / malformed / dangling)."""

    def setUp(self) -> None:
        _require_lint(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.tickets = Path(self._tmp.name)
        for st in ("inbox", "active", "done"):
            (self.tickets / st).mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mk(self, tid, state, not_before=None, anchor=None):
        lines = ["---", f"id: {tid}", "target: recovery", f"state: {state}"]
        if not_before:
            lines.append(f"not_before: {not_before}")
        if anchor:
            lines.append(f"not_before_anchor: {anchor}")
        fm = "\n".join(lines) + "\n---\n\n" + f"# {tid}\n\n## Transition log\n\n- x\n"
        (self.tickets / state / f"{tid}.md").write_text(fm, encoding="utf-8")

    def _detect(self):
        return ticket_lint.detect_unanchored_gates(self.tickets)

    def test_no_not_before_not_flagged(self):
        self._mk("T-0001", "inbox")
        self.assertEqual(self._detect(), [])

    def test_unanchored_flagged(self):
        self._mk("T-0001", "inbox", not_before="2026-09-01T00:00:00Z")
        fs = self._detect()
        self.assertEqual([(f.tid, f.kind) for f in fs], [("T-0001", "unanchored")])

    def test_freetext_anchor_flagged_malformed(self):
        self._mk("T-0001", "active", not_before="2026-09-01T00:00:00Z",
                 anchor="event:fresh_session")
        self.assertEqual([f.kind for f in self._detect()], ["malformed"])

    def test_dangling_ticket_anchor_flagged(self):
        self._mk("T-0001", "inbox", not_before="2026-09-01T00:00:00Z",
                 anchor="operator:T-7777")
        self.assertEqual([f.kind for f in self._detect()], ["dangling"])

    def test_valid_ticket_anchor_not_flagged(self):
        self._mk("T-0002", "done")  # the cited decision ticket exists (resolved)
        self._mk("T-0001", "inbox", not_before="2026-09-01T00:00:00Z",
                 anchor="operator:T-0002")
        self.assertEqual(self._detect(), [])

    def test_valid_five_digit_ticket_anchor_not_flagged(self):
        """T-3215: a 5-digit operator: anchor citing an existing ticket is clean."""
        five_digit_id = "T-" + "12345"
        self._mk(five_digit_id, "done")  # the cited decision ticket exists (resolved)
        self._mk("T-0001", "inbox", not_before="2026-09-01T00:00:00Z",
                 anchor=f"operator:{five_digit_id}")
        self.assertEqual(self._detect(), [])

    def test_dangling_five_digit_ticket_anchor_flagged(self):
        """T-3215: widening the anchor regex must not blind the dangling-ref
        check — a 5-digit id citing a ticket that does NOT exist is still
        flagged, same as the 4-digit case above."""
        five_digit_id = "T-" + "99999"
        self._mk("T-0001", "inbox", not_before="2026-09-01T00:00:00Z",
                 anchor=f"operator:{five_digit_id}")
        self.assertEqual([f.kind for f in self._detect()], ["dangling"])

    def test_valid_tg_anchor_not_flagged(self):
        self._mk("T-0001", "inbox", not_before="2026-09-01T00:00:00Z",
                 anchor="operator:tg:5644")
        self.assertEqual(self._detect(), [])

    def test_valid_event_anchor_not_flagged(self):
        # T-2833(b): a closed-enum real-world-event anchor is compliant.
        self._mk("T-0001", "inbox", not_before="2026-09-10T00:00:00Z",
                 anchor="event:cert-expiry:snowis.me")
        self.assertEqual(self._detect(), [])

    def test_bad_event_kind_flagged_malformed(self):
        self._mk("T-0001", "inbox", not_before="2026-09-10T00:00:00Z",
                 anchor="event:whenever:x")
        self.assertEqual([f.kind for f in self._detect()], ["malformed"])

    def test_done_gate_not_scanned(self):
        # done/ gates are inert — not in STATES_TO_LINT.
        self._mk("T-0001", "done", not_before="2026-09-01T00:00:00Z")
        self.assertEqual(self._detect(), [])

    def test_digest_section_rendered_when_present(self):
        g = ticket_lint.UnanchoredGateFinding("T-0001", "unanchored", "T-0001: no anchor")
        digest = ticket_lint.build_lint_digest(
            "2026-08-11", auto_fixed=[], flagged=[], total_scanned=1,
            unanchored_gates=[g])
        self.assertIn("Unanchored/bad date gates", digest)
        self.assertIn("T-0001", digest)

    def test_digest_section_omitted_when_empty(self):
        digest = ticket_lint.build_lint_digest(
            "2026-08-11", auto_fixed=[], flagged=[], total_scanned=1,
            unanchored_gates=[])
        self.assertNotIn("Unanchored/bad date gates", digest)


class TestDepGraphIssues(unittest.TestCase):
    """detect_dep_graph_issues: corpus-level cycle + stale-done-dep detection (T-0975)."""

    def setUp(self) -> None:
        _require_lint(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.tickets = Path(self._tmp.name)
        for st in ("inbox", "active", "blocked", "done"):
            (self.tickets / st).mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mk(self, tid: str, state: str, deps: list[str] | None = None) -> None:
        deps = deps or []
        fm = (
            "---\n"
            f"id: {tid}\n"
            "target: recovery\n"
            f"state: {state}\n"
            f"depends_on: [{', '.join(deps)}]\n"
            "---\n\n"
            f"# {tid}\n\n## Ask\n\nx.\n\n## Transition log\n\n"
            "- 2026-06-08T00:00:00Z [recovery] filed\n"
        )
        (self.tickets / state / f"{tid}.md").write_text(fm, encoding="utf-8")

    def _detect(self):
        return ticket_lint.detect_dep_graph_issues(self.tickets)

    def test_clean_acyclic_no_findings(self) -> None:
        # A -> B -> C, C is a leaf; no cycle, no stale deps.
        self._mk("T-0001", "inbox", ["T-0002"])
        self._mk("T-0002", "active", ["T-0003"])
        self._mk("T-0003", "blocked", [])
        self.assertEqual(self._detect(), [])

    def test_stale_done_dep_flagged(self) -> None:
        # T-0001 (open) depends on T-0009 which is DONE → stale_done_dep finding.
        self._mk("T-0001", "inbox", ["T-0009"])
        self._mk("T-0009", "done", [])
        findings = self._detect()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "stale_done_dep")
        self.assertIn("T-0001", findings[0].detail)
        self.assertIn("T-0009", findings[0].detail)

    def test_done_dep_with_another_dep_still_open_is_NOT_flagged(self) -> None:
        """T-2396: the partial-resolve case is EXPECTED state, not a defect.

        ORG-013 cascade-unblock guards on _all_other_deps_done() and deliberately
        skips a partial resolve ("multi-dep partial-resolve; wait for the rest"), and
        T-2177 then prunes EVERY done dep at once when the last one lands. So a done
        dep sitting beside a still-open dep is self-healing by construction — there is
        nothing to fix, and flagging it accused ticket.py of a bug it does not have.
        Live false positives this closes: T-2089 [T-2009 done, T-2306 open] and
        T-2219 [T-2222 done, T-2250 open], both reported every grooming run.
        """
        self._mk("T-0001", "inbox", ["T-0009", "T-0010"])
        self._mk("T-0009", "done", [])
        self._mk("T-0010", "inbox", [])
        stale = [f for f in self._detect() if f.kind == "stale_done_dep"]
        self.assertEqual(
            stale, [],
            "flagged a done dep on a ticket that is still legitimately gated by "
            "another OPEN dep — the cascade correctly declined to fire here",
        )

    def test_all_deps_done_is_still_flagged_and_names_every_dep(self) -> None:
        """The complement, and the case that must NOT be lost: when EVERY dep is done
        the cascade should have fired and left depends_on empty, so this is a real
        miss — the T-2177 bug class. Both deps are reported, not just one."""
        self._mk("T-0001", "inbox", ["T-0009", "T-0010"])
        self._mk("T-0009", "done", [])
        self._mk("T-0010", "done", [])
        stale = [f for f in self._detect() if f.kind == "stale_done_dep"]
        self.assertEqual(len(stale), 2)
        self.assertEqual(
            sorted(d for f in stale for d in ("T-0009", "T-0010") if d in f.detail),
            ["T-0009", "T-0010"],
        )

    def test_unknown_dep_does_not_resurrect_the_false_positive(self) -> None:
        """A dep id that resolves to no ticket must keep the finding SUPPRESSED, not
        act as a done-equivalent. The cascade is fail-closed on ids it cannot reason
        about, so it would not have fired here either; treating unknown as satisfied
        would reintroduce exactly the false positive this ticket removed."""
        self._mk("T-0001", "inbox", ["T-0009", "T-9999"])
        self._mk("T-0009", "done", [])
        stale = [f for f in self._detect() if f.kind == "stale_done_dep"]
        self.assertEqual(stale, [])

    def test_done_dep_is_not_a_cycle_edge(self) -> None:
        # T-0001 -> T-0002 (open) and T-0002 -> T-0001 BUT T-0002 is done.
        # The done dep is satisfied → NOT a live cycle; only a stale_done_dep
        # (T-0001 -> T-0002-done). No cycle finding.
        self._mk("T-0001", "inbox", ["T-0002"])
        self._mk("T-0002", "done", ["T-0001"])
        findings = self._detect()
        kinds = sorted(f.kind for f in findings)
        self.assertEqual(kinds, ["stale_done_dep"])

    def test_direct_cycle_flagged(self) -> None:
        # T-0001 <-> T-0002, both open → one cycle finding.
        self._mk("T-0001", "inbox", ["T-0002"])
        self._mk("T-0002", "active", ["T-0001"])
        cycles = [f for f in self._detect() if f.kind == "cycle"]
        self.assertEqual(len(cycles), 1)
        self.assertIn("T-0001", cycles[0].detail)
        self.assertIn("T-0002", cycles[0].detail)

    def test_transitive_cycle_flagged(self) -> None:
        # T-0001 -> T-0002 -> T-0003 -> T-0001 → one cycle finding.
        self._mk("T-0001", "inbox", ["T-0002"])
        self._mk("T-0002", "active", ["T-0003"])
        self._mk("T-0003", "blocked", ["T-0001"])
        cycles = [f for f in self._detect() if f.kind == "cycle"]
        self.assertEqual(len(cycles), 1)
        for t in ("T-0001", "T-0002", "T-0003"):
            self.assertIn(t, cycles[0].detail)

    def test_cycle_reported_once_regardless_of_entry(self) -> None:
        # Two separate components, one cyclic — the cycle is reported exactly once
        # (canonicalised), not once per node in it.
        self._mk("T-0001", "inbox", ["T-0002"])
        self._mk("T-0002", "active", ["T-0001"])
        self._mk("T-0010", "inbox", ["T-0011"])   # acyclic neighbour
        self._mk("T-0011", "active", [])
        cycles = [f for f in self._detect() if f.kind == "cycle"]
        self.assertEqual(len(cycles), 1)

    def test_missing_dep_is_not_a_finding(self) -> None:
        # Dep pointing at a non-existent ticket is neither a cycle nor a stale
        # done-dep (it is a different, out-of-scope class) → no finding here.
        self._mk("T-0001", "inbox", ["T-9999"])
        self.assertEqual(self._detect(), [])


class TestServesWarn(unittest.TestCase):
    """T-1103: warn_tickets() emits WARN (never FLAG/AUTO_FIX) for inbox tickets missing serves:."""

    def setUp(self) -> None:
        _require_lint(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "inbox").mkdir()
        (self.root / "active").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _warn(self, path: Path):
        return ticket_lint.warn_tickets([path])

    def test_inbox_ticket_missing_serves_emits_warn(self) -> None:
        """inbox ticket without serves: → WARN result."""
        p = self.root / "inbox" / "T-0001.md"
        _make_ticket(p, tid="T-0001", target="orchestrator")
        results = self._warn(p)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].action, ticket_lint.LintAction.WARN)
        self.assertIn("serves", results[0].description.lower())

    def test_inbox_ticket_with_serves_no_warn(self) -> None:
        """inbox ticket with serves: present → no WARN."""
        p = self.root / "inbox" / "T-0002.md"
        _make_ticket(p, tid="T-0002", target="orchestrator", extra_fields={"serves": "ORG-034 arc-intent"})
        results = self._warn(p)
        self.assertEqual(results, [])

    def test_goal_neutral_maintenance_value_is_valid(self) -> None:
        """serves: goal-neutral-maintenance is an accepted value → no WARN."""
        p = self.root / "inbox" / "T-0003.md"
        _make_ticket(p, tid="T-0003", target="orchestrator",
                     extra_fields={"serves": "goal-neutral-maintenance"})
        results = self._warn(p)
        self.assertEqual(results, [])

    def test_warn_action_is_not_flag_or_autofix(self) -> None:
        """WARN must be softer than FLAG — action is LintAction.WARN specifically."""
        p = self.root / "inbox" / "T-0004.md"
        _make_ticket(p, tid="T-0004", target="orchestrator")
        results = self._warn(p)
        self.assertEqual(len(results), 1)
        self.assertNotEqual(results[0].action, ticket_lint.LintAction.FLAG)
        self.assertNotEqual(results[0].action, ticket_lint.LintAction.AUTO_FIX)
        self.assertEqual(results[0].action, ticket_lint.LintAction.WARN)

    def test_active_ticket_missing_serves_no_warn(self) -> None:
        """active tickets are NOT warned (warn targets filers, not in-flight workers)."""
        p = self.root / "active" / "T-0005.md"
        _make_ticket(p, tid="T-0005", target="orchestrator", state="active")
        results = self._warn(p)
        self.assertEqual(results, [])

    def test_warn_tickets_list_accepts_multiple_paths(self) -> None:
        """warn_tickets accepts a list of paths and returns warns for each missing serves:."""
        p1 = self.root / "inbox" / "T-0010.md"
        p2 = self.root / "inbox" / "T-0011.md"
        p3 = self.root / "inbox" / "T-0012.md"
        _make_ticket(p1, tid="T-0010", target="orchestrator")
        _make_ticket(p2, tid="T-0011", target="orchestrator",
                     extra_fields={"serves": "some-arc"})
        _make_ticket(p3, tid="T-0012", target="orchestrator")
        results = ticket_lint.warn_tickets([p1, p2, p3])
        warned_ids = {r.tid for r in results}
        self.assertIn("T-0010", warned_ids)
        self.assertNotIn("T-0011", warned_ids)
        self.assertIn("T-0012", warned_ids)


class TestBuildLintDigestWarn(unittest.TestCase):
    """T-1105: build_lint_digest includes a warn section when warranted."""

    def setUp(self) -> None:
        _require_lint(self)

    def _warn_result(self, tid: str) -> "ticket_lint.LintResult":
        return ticket_lint.LintResult(
            tid=tid,
            path=Path(f"/tmp/{tid}.md"),
            action=ticket_lint.LintAction.WARN,
            description=f"{tid}: missing serves: field",
        )

    def test_warn_section_appears_when_warned_non_empty(self) -> None:
        """digest includes a warn section listing warned ticket ids."""
        msg = ticket_lint.build_lint_digest(
            now_iso="2026-06-12 10:00 UTC",
            auto_fixed=[],
            flagged=[],
            total_scanned=3,
            warned=[self._warn_result("T-0010"), self._warn_result("T-0011")],
        )
        self.assertIn("T-0010", msg)
        self.assertIn("T-0011", msg)
        self.assertIn("nudge", msg.lower())

    def test_warn_section_absent_when_warned_empty(self) -> None:
        """digest has no warn section when warned=[] (no empty-section noise)."""
        msg = ticket_lint.build_lint_digest(
            now_iso="2026-06-12 10:00 UTC",
            auto_fixed=[],
            flagged=[],
            total_scanned=2,
            warned=[],
        )
        self.assertNotIn("nudge", msg.lower())
        self.assertNotIn("serves", msg.lower())

    def test_warn_section_absent_when_warned_none(self) -> None:
        """digest has no warn section when warned omitted (backward-compat)."""
        msg = ticket_lint.build_lint_digest(
            now_iso="2026-06-12 10:00 UTC",
            auto_fixed=[],
            flagged=[],
            total_scanned=2,
        )
        self.assertNotIn("nudge", msg.lower())

    def test_all_clean_suppressed_when_warns_present(self) -> None:
        """'all clean' summary is absent when there are warn results (would be false claim)."""
        msg = ticket_lint.build_lint_digest(
            now_iso="2026-06-12 10:00 UTC",
            auto_fixed=[],
            flagged=[],
            total_scanned=3,
            warned=[self._warn_result("T-0050")],
        )
        # There is content to report, so the all-clean short-circuit must not fire.
        self.assertNotIn("all clean", msg.lower())

    def test_warns_do_not_affect_malformation_counts(self) -> None:
        """warned items are listed separately; malformation sections unchanged."""
        msg = ticket_lint.build_lint_digest(
            now_iso="2026-06-12 10:00 UTC",
            auto_fixed=[],
            flagged=[
                ticket_lint.LintResult(
                    tid="T-0001",
                    path=Path("/tmp/T-0001.md"),
                    action=ticket_lint.LintAction.FLAG,
                    description="real malformation",
                ),
            ],
            total_scanned=5,
            warned=[self._warn_result("T-0002")],
        )
        # Both appear but in separate sections.
        self.assertIn("T-0001", msg)
        self.assertIn("T-0002", msg)
        # The flag section name appears
        self.assertIn("flag", msg.lower())
        # The warn section appears too
        self.assertIn("nudge", msg.lower())


class TestDoLintWarn(unittest.TestCase):
    """T-1105: _do_lint calls warn_tickets() and includes results in digest."""

    def setUp(self) -> None:
        _require_lint(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "tickets" / "inbox").mkdir(parents=True)
        (self.root / "tickets" / "active").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_digest_includes_warn_section_for_inbox_missing_serves(self) -> None:
        """--repo-root mode: inbox ticket without serves: appears in the digest warn section."""
        # No serves: field → should trigger warn
        p = self.root / "tickets" / "inbox" / "T-0030.md"
        _make_ticket(p, tid="T-0030", target="orchestrator")

        captured_digest: list[str] = []

        def fake_send(text: str) -> bool:
            captured_digest.append(text)
            return True

        with patch.object(ticket_lint, "_send_lint_digest", side_effect=fake_send):
            rc = ticket_lint.main(["--repo-root", str(self.root)])

        self.assertEqual(rc, 0, "exit code must be 0 — warns don't count as failures")
        self.assertEqual(len(captured_digest), 1)
        digest = captured_digest[0]
        self.assertIn("T-0030", digest)
        self.assertIn("nudge", digest.lower())

    def test_digest_has_no_warn_section_when_serves_present(self) -> None:
        """--repo-root mode: ticket with serves: present → no warn section in digest."""
        p = self.root / "tickets" / "inbox" / "T-0031.md"
        _make_ticket(p, tid="T-0031", target="orchestrator",
                     extra_fields={"serves": "some-arc"})

        captured_digest: list[str] = []

        def fake_send(text: str) -> bool:
            captured_digest.append(text)
            return True

        with patch.object(ticket_lint, "_send_lint_digest", side_effect=fake_send):
            rc = ticket_lint.main(["--repo-root", str(self.root)])

        self.assertEqual(rc, 0)
        self.assertEqual(len(captured_digest), 1)
        self.assertNotIn("nudge", captured_digest[0].lower())

    def test_warn_does_not_affect_exit_code(self) -> None:
        """warns alone never cause a non-zero exit code."""
        # Multiple inbox tickets all missing serves:
        for i in range(3):
            p = self.root / "tickets" / "inbox" / f"T-00{80 + i}.md"
            _make_ticket(p, tid=f"T-00{80 + i}", target="orchestrator")

        with patch.object(ticket_lint, "_send_lint_digest", return_value=True):
            rc = ticket_lint.main(["--repo-root", str(self.root)])

        self.assertEqual(rc, 0)

    def test_no_digest_mode_suppresses_warn_send(self) -> None:
        """--no-digest bypasses warn emission (same as it bypasses rest of digest)."""
        p = self.root / "tickets" / "inbox" / "T-0040.md"
        _make_ticket(p, tid="T-0040", target="orchestrator")

        with patch.object(ticket_lint, "_send_lint_digest") as mock_send:
            rc = ticket_lint.main(["--repo-root", str(self.root), "--no-digest"])

        self.assertEqual(rc, 0)
        mock_send.assert_not_called()


class TestGrandProjectProgramIndexLint(unittest.TestCase):
    """T-2426: the standing ticket lint surfaces paired index integrity."""

    def test_real_repository_program_indexes_are_clean(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        self.assertEqual(ticket_lint.detect_program_index_findings(repo_root), [])

    def test_partial_index_pair_is_a_known_positive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            tickets = repo_root / "tickets"
            tickets.mkdir()
            (tickets / "grand-project-stage-evidence.yaml").write_text(
                "schema_version: 1\nstages: []\n"
            )
            findings = ticket_lint.detect_program_index_findings(repo_root)
        self.assertEqual([finding.code for finding in findings], ["program-index-pair-incomplete"])

    def test_digest_names_program_index_finding(self) -> None:
        finding = ticket_lint.ProgramIndexFinding(
            code="obligation-source-digest-drift",
            record_id="ORG-061:R1",
            description="source digest changed",
        )
        digest = ticket_lint.build_lint_digest(
            now_iso="2026-08-25 00:00 UTC",
            auto_fixed=[],
            flagged=[],
            total_scanned=0,
            program_index_findings=[finding],
        )
        self.assertIn("ORG-061:R1", digest)
        self.assertIn("obligation-source-digest-drift", digest)


class AdrDirEaccesGuardTests(unittest.TestCase):
    """T-3477 EACCES guard: an ADR dir under another agent's 700-mode home
    (echo-prismatic's adr/ under /home/claude, unreadable to the recovery lint
    runner) makes Path.exists() RAISE PermissionError instead of returning
    False — which crashed the entire hourly lint. scan_adr_elements /
    load_adr_obligation_index must skip such a dir like a missing one, never
    propagate (mirrors ticket_grooming.py's existing try/except OSError guard)."""

    def _inaccessible_dir(self) -> MagicMock:
        d = MagicMock(spec=Path)
        d.exists.side_effect = PermissionError(13, "Permission denied")
        return d

    def test_scannable_returns_false_on_eacces_and_can_warn(self) -> None:
        import contextlib
        import io

        d = self._inaccessible_dir()
        self.assertFalse(ticket_lint._adr_dir_scannable(d))  # silent by default
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertFalse(ticket_lint._adr_dir_scannable(d, warn=True))
        self.assertIn("INACCESSIBLE", buf.getvalue())

    def test_scan_adr_elements_skips_inaccessible_dir_without_crashing(self) -> None:
        # DISCRIMINATING: pre-fix, scan_adr_elements did a bare
        # `if not one_dir.exists()` which propagated PermissionError and failed
        # the whole lint run. Post-fix it skips the inaccessible dir.
        import contextlib
        import io

        with contextlib.redirect_stderr(io.StringIO()):
            known_ids, findings = ticket_lint.scan_adr_elements([self._inaccessible_dir()])
        self.assertEqual(known_ids, set())
        self.assertEqual(findings, [])

    def test_load_adr_obligation_index_skips_inaccessible_dir(self) -> None:
        # Must not raise on an inaccessible dir (defensive; silent guard).
        idx = ticket_lint.load_adr_obligation_index(self._inaccessible_dir())
        self.assertEqual(idx.details, {})


class IsEchoScopedIdTests(unittest.TestCase):
    """T-3495: is_echo_scoped_id's numeric ADR-id >= 200 discriminator —
    the echo-prismatic agent-authored range vs. pantheon's own ORG-NNN ids
    and the vault's 001-199 ADR range (a different, permanently-excluded
    corpus)."""

    def test_echo_range_element_id_in_scope(self) -> None:
        self.assertTrue(ticket_lint.is_echo_scoped_id("ADR-207#E1"))

    def test_echo_range_bare_adr_id_in_scope(self) -> None:
        self.assertTrue(ticket_lint.is_echo_scoped_id("ADR-207"))

    def test_vault_range_out_of_scope(self) -> None:
        self.assertFalse(ticket_lint.is_echo_scoped_id("ADR-050#E1"))

    def test_pantheon_org_id_out_of_scope(self) -> None:
        self.assertFalse(ticket_lint.is_echo_scoped_id("ORG-065#E1"))

    def test_plain_prose_out_of_scope(self) -> None:
        self.assertFalse(ticket_lint.is_echo_scoped_id("no adr id here at all"))

    def test_embedded_in_larger_detail_string(self) -> None:
        self.assertTrue(ticket_lint.is_echo_scoped_id(
            "T-0001 implements -> ADR-207#E1, which does not resolve to "
            "any ADR elements: id"
        ))

    def test_open_ended_above_299_still_in_scope(self) -> None:
        # Deliberately open-ended (real numeric comparison, not a fixed-width
        # string match) — a hypothetical future ADR-2345 is still >=200.
        self.assertTrue(ticket_lint.is_echo_scoped_id("ADR-2345#E1"))

    def test_three_digit_id_below_200_out_of_scope(self) -> None:
        self.assertFalse(ticket_lint.is_echo_scoped_id("ADR-199#E1"))


if __name__ == "__main__":
    unittest.main()
