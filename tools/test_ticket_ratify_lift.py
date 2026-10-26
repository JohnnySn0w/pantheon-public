"""Tests for T-0491: auto-lift of ratified doc Status field on ratify-resolve.

Unit tests (Layer-2): mock subprocess.run, patch REPO_ROOT + TICKETS_ROOT,
exercise _lift_ratifiable_doc_status and the resolve path in-process.

Layer-3 integration test: real tmp git repo via _test_fixtures, invoke
cmd_resolve in-process against the tmp tree, assert the committed file content
via git show HEAD:<path>.

Run from repo root:
    python3 -m unittest tools.test_ticket_ratify_lift -v

HERMES discipline: the trigger byte sequence is never written contiguously
in this source file. See HERME + S discipline in test_ticket.py module docstring.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _git_utils, ticket
from tools._test_fixtures import contained_path


# ---------------------------------------------------------------------------
# Helpers — shared across unit tests
# ---------------------------------------------------------------------------


def _write_ticket_file(
    path: Path,
    *,
    tid: str,
    state: str = "active",
    target: str = "operator",
    intent: str = "ratify",
    ratifiable_files: list[str] | None = None,
    labels: list[str] | None = None,
) -> None:
    """Write a minimal ticket file with ratify frontmatter."""
    rf_list = ratifiable_files or []
    rf_yaml = "[" + ", ".join(rf_list) + "]"
    labels_yaml = "[" + ", ".join(labels or ["ratification"]) + "]"
    fm_lines = [
        "---",
        f"id: {tid}",
        "created: 2026-05-20T10:00:00Z",
        "requester: orchestrator",
        f"target: {target}",
        f"state: {state}",
        "parent_ticket: null",
        "depends_on: []",
        f"labels: {labels_yaml}",
        f"intent: {intent}",
        f"ratifiable_files: {rf_yaml}",
        "---",
        "",
        f"# Test ratify ticket {tid}",
        "",
        "## Ask",
        "",
        "Ratify the doc.",
        "",
        "## Transition log",
        "",
        f"- 2026-05-20T10:00:00Z [orchestrator] filed in inbox",
        f"- 2026-05-20T10:01:00Z [orchestrator] claimed",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm_lines), encoding="utf-8")


def _write_doc(path: Path, status_line: str, extra_body: str = "") -> None:
    """Write a minimal ratifiable doc with the given Status line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f"# Test Doc\n\n**Status:** {status_line}\n\n## Body\n\n{extra_body}Content here.\n"
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit test base: mocks subprocess + REPO_ROOT + TICKETS_ROOT (TransitionTest
# pattern from test_ticket.py)
# ---------------------------------------------------------------------------


class RatifyLiftUnitBase(unittest.TestCase):
    """Base class with mock setup matching TransitionTest in test_ticket.py."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True, exist_ok=True)

        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()
        self._gu_repo_patch = patch.object(_git_utils, "REPO_ROOT", self.root)
        self._gu_repo_patch.start()
        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root / "tickets")
        self._tickets_patch.start()

        # Minimal README.md so _rebuild_readme_table doesn't fail.
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
                src = contained_path(self.root, argv[2])
                dst = contained_path(self.root, argv[3])
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
                        stderr=f"fatal: pathspec '{missing[0]}' did not match any files\n",
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

    def tearDown(self) -> None:
        self._sub_patch.stop()
        self._tickets_patch.stop()
        self._gu_repo_patch.stop()
        self._repo_patch.stop()
        self._tmp.cleanup()

    def _make_resolve_args(self, tid: str, summary: str | None = None) -> MagicMock:
        args = MagicMock()
        args.ticket = tid
        args.with_summary = summary
        args.no_push = True  # avoid push in unit tests
        from tools.test_ticket import with_ruling  # T-5015 register flags
        return with_ruling(args, self.root)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestRatifyLiftStatusLine(unittest.TestCase):
    """Direct unit tests of _lift_ratifiable_doc_status helper."""

    def _call(self, doc_content: str, repo_root: Path) -> tuple[str | None, list[str]]:
        """Call _lift_ratifiable_doc_status on a doc with the given content."""
        p = repo_root / "adr" / "test-doc.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(doc_content, encoding="utf-8")
        warnings: list[str] = []
        result = ticket._lift_ratifiable_doc_status(p, repo_root, warnings)
        return result, warnings

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_lifts_basic_status_line(self) -> None:
        """Plain Status line gets Ratified prefix."""
        content = "# Doc\n\n**Status:** Drafted 2026-05-01.\n\n## Body\n"
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertIn("**Ratified", result)
        self.assertIn("by the operator.", result)
        self.assertEqual(warnings, [])

    def test_lifts_and_strips_awaiting_clause(self) -> None:
        """Awaiting the operator ratification clause is stripped after Ratified prefix."""
        content = (
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01. Awaiting the operator ratification.\n\n"
            "## Body\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertIn("**Ratified", result)
        self.assertNotIn("Awaiting the operator ratification", result)
        # The drafted history should be preserved
        self.assertIn("Drafted 2026-05-01", result)

    def test_strips_awaiting_ratification_variant(self) -> None:
        """'Awaiting ratification.' (no the operator) is also stripped."""
        content = (
            "# Doc\n\n"
            "**Status:** Authored 2026-05-10. Awaiting ratification.\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertNotIn("Awaiting ratification", result)

    def test_strips_bold_awaiting_clause(self) -> None:
        """**Awaiting ... ratification** (bold markdown) is stripped."""
        content = (
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01. **Awaiting the operator ratification.**\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertNotIn("Awaiting", result)

    def test_idempotent_already_ratified(self) -> None:
        """Status already starts with Ratified → no change (returns None)."""
        content = (
            "# Doc\n\n"
            "**Status:** **Ratified 2026-05-19 by the operator.** Drafted 2026-05-01.\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNone(result)
        self.assertEqual(warnings, [])

    def test_status_line_not_found_warns(self) -> None:
        """Doc without **Status:** line → None result + warning."""
        content = "# Doc\n\nNo status here.\n\n## Body\n"
        result, warnings = self._call(content, self.root)
        self.assertIsNone(result)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Status", warnings[0])

    def test_first_status_line_only(self) -> None:
        """Only the first **Status:** line is rewritten."""
        content = (
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01.\n\n"
            "## Section\n\n"
            "**Status:** Some section status.\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        # First line rewritten; second unchanged
        lines = result.splitlines()
        status_lines = [l for l in lines if "**Status:**" in l]
        self.assertEqual(len(status_lines), 2)
        self.assertIn("Ratified", status_lines[0])
        self.assertNotIn("Ratified", status_lines[1])

    def test_preserves_drafted_history(self) -> None:
        """Authored/Amended history after the awaiting clause stays intact."""
        content = (
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01 (T-0300). "
            "Amended 2026-05-10 (T-0400). "
            "Awaiting the operator ratification.\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertIn("Drafted 2026-05-01", result)
        self.assertIn("Amended 2026-05-10", result)
        self.assertNotIn("Awaiting", result)

    # -- T-2847: marker-family strip + possessive holder ---------------------

    def test_org065_exact_shape_fully_cleaned(self) -> None:
        """T-2847 (a): the exact ORG-065 bug shape (git history verified,
        commit 72b513965e's parent) lifts to a clean Ratified line with NO
        residual Proposed/DRAFT/awaiting/HITL:Proposed, and the genuine
        lineage sentence + Design-first trailer both survive.
        """
        content = (
            "# ORG-065 — First-class dependency graph\n\n"
            "**Status:** Proposed (agent-authored, unratified) — DRAFT "
            "awaiting the operator's ratification. The operator directed this ADR on "
            "2026-08-08 (relayed via the codex mailbox, confirmed directly "
            "on Telegram, TG7326) at the same time he ratified T-2169 — "
            "the prose closure-discipline this ADR is the structural "
            "version of. **HITL: Proposed.** Design-first: this ADR fixes "
            "the model and the migration shape; it deliberately does "
            "**not** commit to tooling implementation (that is a build "
            "ticket gated on ratification).\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)

        status_line = [
            l for l in result.splitlines() if l.startswith("**Status:**")
        ][0]

        # Clean Ratified prefix, no residual pre-ratification markers.
        self.assertIn("**Ratified", status_line)
        self.assertIn("by the operator.", status_line)
        self.assertNotIn("Proposed", status_line)
        self.assertNotIn("DRAFT", status_line)
        self.assertNotIn("awaiting", status_line.lower())

        # HITL flipped, not stripped.
        self.assertIn("**HITL: Ratified.**", status_line)

        # Genuine lineage + trailer preserved verbatim.
        self.assertIn(
            "the operator directed this ADR on 2026-08-08", status_line
        )
        self.assertIn(
            "the prose closure-discipline this ADR is the structural "
            "version of.",
            status_line,
        )
        self.assertIn("Design-first:", status_line)
        self.assertIn(
            "gated on ratification).", status_line
        )

    def test_strips_possessive_awaiting_clause_alone(self) -> None:
        """T-2847 (b): possessive "Awaiting the operator's ratification." (no
        surrounding Proposed/DRAFT markers) is stripped; preceding history
        preserved. Regression guard for the bug's root cause #1 — the old
        regex required whitespace-then-"ratification" directly after an
        optional "the operator", so "the operator's ratification" never matched.
        """
        content = (
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01. Awaiting the operator's "
            "ratification.\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertIn("**Ratified", result)
        self.assertNotIn("Awaiting", result)
        self.assertNotIn("the operator's ratification", result)
        self.assertIn("Drafted 2026-05-01", result)

    def test_generic_holder_possessive_awaiting_clause(self) -> None:
        """T-2847 (b) generalization: any holder possessive (not just
        the operator), e.g. "the operator's ratification", is also stripped.
        """
        content = (
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01. Awaiting the operator's "
            "ratification.\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertNotIn("Awaiting", result)
        self.assertNotIn("operator's ratification", result)
        self.assertIn("Drafted 2026-05-01", result)

    def test_leading_drafted_lineage_not_mistaken_for_draft_marker(
        self,
    ) -> None:
        """T-2847 pre-mortem regression guard: the new leading-"DRAFT"
        marker strip must NOT fire on "Drafted <date>" lineage text at the
        start of the line (word-prefix collision — "DRAFT" is a substring
        prefix of "Drafted"). Caught by hand-trace at plan-tier; this test
        makes it a structural guarantee.
        """
        content = (
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01 by Orchestrator (T-0300). Awaiting "
            "ratification.\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertIn("Drafted 2026-05-01 by Orchestrator (T-0300).", result)
        self.assertNotIn("Awaiting", result)

    def test_leading_proposed_with_dash_draft_stripped(self) -> None:
        """A leading "Proposed — DRAFT awaiting ratification." (no
        parenthetical qualifier, em-dash separator only — the ORG-069/070/
        071/072-style convention) is fully stripped.
        """
        content = (
            "# Doc\n\n"
            "**Status:** Proposed — DRAFT awaiting the operator ratification. "
            "Author of record: Orchestrator (HO).\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertNotIn("Proposed", result)
        self.assertNotIn("DRAFT", result)
        self.assertNotIn("awaiting", result.lower())
        self.assertIn("Author of record: Orchestrator (HO).", result)

    # -- T-2847 part-3 option (b): frontmatter staleness warning -------------

    def test_frontmatter_status_stale_after_body_lift_warns(self) -> None:
        """Frontmatter `status:` still reading a pre-ratification marker
        after a successful body lift produces an actionable warning naming
        the doc + the stale value.
        """
        content = (
            "---\n"
            "aliases: [ORG-999]\n"
            "status: Proposed 2026-08-08 by Orchestrator (HO) — DRAFT awaiting "
            "the operator ratification\n"
            "---\n"
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01. Awaiting the operator's "
            "ratification.\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertEqual(len(warnings), 1)
        self.assertIn("test-doc.md", warnings[0])
        self.assertIn("frontmatter status:", warnings[0])
        self.assertIn("Proposed", warnings[0])

    def test_frontmatter_no_status_key_no_warning(self) -> None:
        """Frontmatter present but with no `status:` key: no staleness
        warning (nothing to compare against).
        """
        content = (
            "---\n"
            "aliases: [ORG-999]\n"
            "---\n"
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01. Awaiting the operator's "
            "ratification.\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertEqual(warnings, [])

    def test_frontmatter_already_ratified_no_warning(self) -> None:
        """Frontmatter `status:` already starts with "Ratified": no
        staleness warning, even though the body needed lifting.
        """
        content = (
            "---\n"
            "status: Ratified 2026-06-06 by the operator via T-0845\n"
            "---\n"
            "# Doc\n\n"
            "**Status:** Drafted 2026-05-01. Awaiting the operator's "
            "ratification.\n\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNotNone(result)
        self.assertEqual(warnings, [])


    # -- T-5373: pending / until-ratified sentence strip + residual guard ----

    T5370_LINE = (
        "**Status:** Draft 2026-10-22 (T-5111; Author: Recovery). Pending the operator "
        "ratification per the sop/remediation per-SOP ratification rule "
        "(T-0157). Gap found while revising "
        "`persona-daemon-liveness-recovery.md` (T-5061). Until ratified, "
        "every path below is applied **with a report**, and Path D is "
        "always bubble-up."
    )

    def _status_line(self, result: str) -> str:
        return [l for l in result.splitlines() if l.startswith("**Status:**")][0]

    def test_t5370_exact_line_fully_cleaned(self) -> None:
        result, warnings = self._call(f"# Doc\n\n{self.T5370_LINE}\n\n## B\n", self.root)
        self.assertIsNotNone(result)
        line = self._status_line(result)
        today = ticket.datetime.now(ticket.timezone.utc).strftime("%Y-%m-%d")
        self.assertEqual(
            line,
            f"**Status:** **Ratified {today} by the operator.** Drafted 2026-10-22 "
            "(T-5111; Author: Recovery). Gap found while revising "
            "`persona-daemon-liveness-recovery.md` (T-5061).",
        )
        self.assertEqual(warnings, [])

    def test_single_sentence_dated_draft_becomes_drafted(self) -> None:
        result, warnings = self._call("**Status:** Draft 2026-10-22 (T-1).\n", self.root)
        line = self._status_line(result)
        self.assertTrue(line.endswith(" Drafted 2026-10-22 (T-1)."), line)
        self.assertEqual(warnings, [])

    def test_bare_draft_without_date_still_stripped(self) -> None:
        result, _ = self._call("**Status:** DRAFT. Authored 2026-05-01 (T-2).\n", self.root)
        line = self._status_line(result)
        self.assertNotIn("DRAFT", line)
        self.assertIn("Authored 2026-05-01 (T-2).", line)

    def test_unratified_sentence_removed_whole_neighbours_kept(self) -> None:
        result, _ = self._call(
            "**Status:** Drafted 2026-05-01 (T-3). This doc is unratified until "
            "review. Amended 2026-05-02 (T-4).\n",
            self.root,
        )
        line = self._status_line(result)
        self.assertNotIn("unratified", line)
        self.assertIn("Drafted 2026-05-01 (T-3).", line)
        self.assertIn("Amended 2026-05-02 (T-4).", line)

    def test_all_sentences_stripped_leaves_bare_prefix(self) -> None:
        result, warnings = self._call(
            "**Status:** Pending the operator ratification.\n", self.root
        )
        line = self._status_line(result)
        self.assertRegex(line, r"^\*\*Status:\*\* \*\*Ratified \d{4}-\d{2}-\d{2} by the operator\.\*\*$")
        self.assertEqual(warnings, [])

    def test_residual_guard_skips_write_and_warns(self) -> None:
        content = (
            "# Doc\n\n**Status:** Drafted 2026-05-01. Reviewers are pending "
            "sign-off from Recovery.\n"
        )
        result, warnings = self._call(content, self.root)
        self.assertIsNone(result)
        self.assertEqual((self.root / "adr" / "test-doc.md").read_text(), content)
        self.assertEqual(len(warnings), 1)
        self.assertIn("test-doc.md", warnings[0])
        self.assertIn("HAND FLIP", warnings[0])

    def test_bare_pending_sentence_on_clean_draft_line_trips_guard(self) -> None:
        """Pins the intended false-positive: bare 'pending' (no 'ratif')
        on an otherwise-clean Draft line is refused, not silently lifted."""
        content = "**Status:** Draft 2026-10-22 (T-1). Build ticket T-1 pending.\n"
        result, warnings = self._call(content, self.root)
        self.assertIsNone(result)
        self.assertEqual((self.root / "adr" / "test-doc.md").read_text(), content)
        self.assertEqual(len(warnings), 1)
        self.assertIn("HAND FLIP", warnings[0])

    def test_t5370_lift_is_idempotent(self) -> None:
        result, _ = self._call(f"{self.T5370_LINE}\n", self.root)
        p = self.root / "adr" / "test-doc.md"
        warnings: list[str] = []
        again = ticket._lift_ratifiable_doc_status(p, self.root, warnings)
        self.assertIsNone(again)
        self.assertEqual(p.read_text(), result)
        self.assertEqual(warnings, [])

    def test_gated_on_ratification_prose_not_stripped(self) -> None:
        result, warnings = self._call(
            "**Status:** Drafted 2026-05-01. Build is a ticket gated on "
            "ratification (T-9).\n",
            self.root,
        )
        self.assertIn("gated on ratification (T-9).", self._status_line(result))
        self.assertEqual(warnings, [])


    # -- T-5405: lineage kept, bold/paren balance, awaiting-regex mangle ----

    ACP_LINE = (
        "**Status:** Draft 2026-07-26 (Author: Recovery; filed from the "
        "archivist context-wedge + specialist T-2038 cap-loop recurrences. "
        "Pending the operator ratification per the sop/remediation per-SOP "
        "ratification rule.)"
    )
    TELEGRAM_LINE = (
        "**Status:** Draft 2026-06-06 (operator-requested in-incident, msg "
        "2705 \u2014 he provided the canonical remediation; pending formal "
        "ratification per the sop/remediation per-SOP ratification rule). "
        "Author: Recovery. Filed from "
        "[`incidents/2026-06-06-orchestrator-telegram-plugin-drop`]"
        "(../../incidents/2026-06-06-orchestrator-telegram-plugin-drop/README.md)."
    )
    RECOVERY_LINE = (
        "**Status:** Draft 2026-05-21 (filed from the 2026-05-21 ~14h Recovery "
        "outage; pending the operator ratification per the sop/remediation "
        "per-SOP ratification rule). Author: Recovery."
    )
    DIAGNOSING_LINE = (
        "**Status:** Drafted 2026-05-27 by Orchestrator (Orchestrator). "
        "**Drafted, NOT yet awaiting ratification** \u2014 Phase 1 isn't "
        "fully closed; finalize the SOP after the T-0607 ceremony lands so "
        "any final-tier surprise gets folded in. Authoring per D2 (HO "
        "drafts; the operator ratifies via in-file Status update)."
    )
    ORG095_LINE = (
        "**Status:** Draft (Authored 2026-09-06 by Orchestrator (HO); V3 direction "
        "endorsed by the operator via recovery-relayed TG 6767/6768 \u2014 "
        "automated-custody DEFAULT, offline-root demoted to OPTIONAL "
        "hardened tier for technical operators; \u00a71 revised 2026-09-06 "
        "to reflect the two-tier model). **Date:** 2026-09-06 **Author:** "
        "Orchestrator (HO) **HITL:** Ratify-required. **Related:** [[ORG-047]] D5 "
        "(this ADR extracts + fully specifies), [[ORG-069]] (Split D "
        "\u2014 content/tool authorization), [[ORG-070]] (Split E \u2014 "
        "evidence & governance), [[ORG-071]] (Split B \u2014 network "
        "capabilities), [[ORG-072]] (Split C \u2014 lifecycle health & "
        "recovery), [[T-2803]] (security-considerations triage), [[T-2810]] "
        "(V1+V2 envelope \u2014 DECIDED 2026-09-06 prefer-adr TG "
        "6755/6757/6759), [[T-2811]] (this ADR's scoping ticket), [[T-3488]] "
        "(V3 redesign \u2014 direction endorsed TG 6767/6768; concrete "
        "model in \u00a71 below)."
    )
    WEEKLY_LINE = (
        "**Status:** Adopted 2026-09-06 by Orchestrator (HO) pending the operator ratify "
        "\u2014 under D3/msg-3733 this pattern would normally be "
        "HO-autonomous adoption + ledger, BUT the mechanism affects "
        "operator-visible outputs (leans in the weekly digest) + establishes "
        "ongoing HO discipline that shapes priority-list re-ordering, so "
        "surfacing to the operator for the formal ratify is the honest posture. "
        "Direction ratified by the operator 2026-09-05 via Telegram 1754 "
        "(org-planning Decision 3). Owned by HO (orchestrator) as author + "
        "operator; Specialist's S4 read feeds the input signal; no new "
        "portfolio-manager role is created."
    )

    def _lifted(self, line: str) -> str:
        result, warnings = self._call(f"# D\n\n{line}\n\n## B\n", self.root)
        self.assertIsNotNone(result, warnings)
        self.assertEqual(warnings, [])
        return self._status_line(result)

    def _prefix(self) -> str:
        today = ticket.datetime.now(ticket.timezone.utc).strftime("%Y-%m-%d")
        return f"**Status:** **Ratified {today} by the operator.** "

    def test_acp_pending_clause_in_paren_keeps_lineage(self) -> None:
        self.assertEqual(
            self._lifted(self.ACP_LINE),
            self._prefix() + "Drafted 2026-07-26 (Author: Recovery; filed from "
            "the archivist context-wedge + specialist T-2038 cap-loop "
            "recurrences)",
        )

    def test_telegram_pending_clause_in_paren_keeps_lineage(self) -> None:
        self.assertEqual(
            self._lifted(self.TELEGRAM_LINE),
            self._prefix() + "Drafted 2026-06-06 (operator-requested "
            "in-incident, msg 2705 \u2014 he provided the canonical "
            "remediation). Author: Recovery. Filed from "
            "[`incidents/2026-06-06-orchestrator-telegram-plugin-drop`]"
            "(../../incidents/2026-06-06-orchestrator-telegram-plugin-drop/README.md).",
        )

    def test_recovery_pending_clause_in_paren_keeps_lineage(self) -> None:
        self.assertEqual(
            self._lifted(self.RECOVERY_LINE),
            self._prefix() + "Drafted 2026-05-21 (filed from the 2026-05-21 "
            "~14h Recovery outage). Author: Recovery.",
        )

    def test_paren_only_pending_drops_whole_paren(self) -> None:
        self.assertEqual(
            self._lifted("**Status:** Draft 2026-05-21 (pending the operator ratification). Author: Recovery."),
            self._prefix() + "Drafted 2026-05-21. Author: Recovery.",
        )

    def test_unclosed_paren_after_cleaning_skips_and_warns(self) -> None:
        # Nested paren: pass leaves it; sentence strip would truncate -> guard.
        result, warnings = self._call(
            "**Status:** Drafted 2026-05-21 (Author: Recovery (HO); x. Pending "
            "the operator ratification.)\n", self.root)
        self.assertIsNone(result)
        self.assertTrue(any("HAND FLIP REQUIRED" in w for w in warnings), warnings)

    def test_bold_dated_draft_stays_balanced(self) -> None:
        self.assertEqual(
            self._lifted("**Status:** **Draft 2026-10-22** (T-1)."),
            self._prefix() + "**Drafted 2026-10-22** (T-1).",
        )

    def test_bold_draft_word_then_date_stays_balanced(self) -> None:
        line = self._lifted("**Status:** **Draft** 2026-10-22 (T-1).")
        self.assertEqual(line.count("**") % 2, 0, line)
        self.assertIn("Drafted", line)
        self.assertNotIn("Drafted** 2026", line.replace("**Drafted** 2026", ""))

    def test_diagnosing_negated_awaiting_skipped_not_mangled(self) -> None:
        result, warnings = self._call(self.DIAGNOSING_LINE + "\n", self.root)
        self.assertIsNone(result)
        self.assertTrue(any("HAND FLIP REQUIRED" in w for w in warnings), warnings)

    def test_org095_draft_paren_lineage_kept(self) -> None:
        line = self._lifted(self.ORG095_LINE)
        self.assertEqual(
            line,
            self._prefix() + "Drafted (Authored 2026-09-06 by Orchestrator (HO); V3 "
            "direction endorsed by the operator via recovery-relayed TG 6767/6768 "
            "\u2014 automated-custody DEFAULT, offline-root demoted to "
            "OPTIONAL hardened tier for technical operators; \u00a71 revised "
            "2026-09-06 to reflect the two-tier model). **Date:** 2026-09-06 "
            "**Author:** Orchestrator (HO) **HITL:** Ratify-required. **Related:** "
            "[[ORG-047]] D5 (this ADR extracts + fully specifies), [[ORG-069]] "
            "(Split D \u2014 content/tool authorization), [[ORG-070]] (Split E "
            "\u2014 evidence & governance), [[ORG-071]] (Split B \u2014 "
            "network capabilities), [[ORG-072]] (Split C \u2014 lifecycle "
            "health & recovery), [[T-2803]] (security-considerations triage), "
            "[[T-2810]] (V1+V2 envelope \u2014 DECIDED 2026-09-06 prefer-adr "
            "TG 6755/6757/6759), [[T-2811]] (this ADR's scoping ticket), "
            "[[T-3488]] (V3 redesign \u2014 direction endorsed TG "
            "6767/6768; concrete model in \u00a71 below).",
        )

    def test_pure_qualifier_paren_still_removed(self) -> None:
        line = self._lifted("**Status:** Proposed (agent-authored, unratified) \u2014 DRAFT. Authored 2026-05-01 (T-2).")
        self.assertNotIn("agent-authored", line)
        self.assertIn("Authored 2026-05-01 (T-2).", line)


    def _assert_skipped(self, line: str) -> None:
        result, warnings = self._call(line + "\n", self.root)
        self.assertIsNone(result)
        self.assertTrue(any("HAND FLIP REQUIRED" in w for w in warnings), warnings)

    def test_weekly_arc_dated_head_in_pending_sentence_skips(self) -> None:
        self._assert_skipped(self.WEEKLY_LINE)

    def test_semicolon_outside_parens_date_loss_skips(self) -> None:
        self._assert_skipped(
            "**Status:** Draft 2026-05-01 (T-1); pending the operator ratification. Author: X.")

    def test_nested_paren_date_loss_skips(self) -> None:
        self._assert_skipped(
            "**Status:** Draft 2026-05-01 (T-1 (HO); pending the operator ratification). Author: X.")

    def test_date_in_kept_sentence_still_lifts(self) -> None:
        line = self._lifted("**Status:** Drafted 2026-05-01 (T-3). Amended 2026-05-02 (T-4). Pending the operator ratification.")
        self.assertIn("2026-05-01", line)
        self.assertIn("2026-05-02", line)

    def test_removed_paren_leaves_no_double_space(self) -> None:
        line = self._lifted("**Status:** Drafted 2026-05-01 (pending the operator ratification) by Recovery.")
        self.assertNotIn("  ", line)


class TestRatifyLiftViaResolve(RatifyLiftUnitBase):
    """Integration-unit tests: resolve a ratify ticket and check doc is staged."""

    def test_ratify_resolve_lifts_status_in_same_repo_doc(self) -> None:
        """Resolving a ratify ticket rewrites the same-repo doc's Status line.

        Also asserts that 'auto-unblock cascade' does NOT appear on stdout
        when there are no real cascade dependants (Defect 1 regression guard).
        """
        import io
        import contextlib
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0491"
            doc_rel = "adr/test-adr.md"
            doc_path = self.root / doc_rel
            _write_doc(doc_path, "Drafted 2026-05-01. Awaiting the operator ratification.")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[f"pantheon-config:{doc_rel}"],
            )

            args = self._make_resolve_args(tid, "Ratified.")
            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)
            stdout_out = stdout_buf.getvalue()

            # Doc must be rewritten with Ratified prefix and awaiting stripped
            new_content = doc_path.read_text(encoding="utf-8")
            self.assertIn("**Ratified", new_content)
            self.assertIn("by the operator.", new_content)
            self.assertNotIn("Awaiting the operator ratification", new_content)
            self.assertIn("Drafted 2026-05-01", new_content)

            # Doc must have been staged (git add called with doc_rel)
            add_calls = [
                c.args[0] for c in self.mock_run.call_args_list
                if c.args[0][:2] == ["git", "add"]
            ]
            staged_paths: set[str] = set()
            for argv in add_calls:
                idx = argv.index("--")
                staged_paths.update(argv[idx + 1:])
            self.assertIn(doc_rel, staged_paths)

            # Regression guard for Defect 1: no cascade dependants → no cascade msg.
            self.assertNotIn(
                "auto-unblock cascade", stdout_out,
                "spurious cascade output when no real dependants were unblocked"
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_ratify_lift_plus_real_cascade_both_appear(self) -> None:
        """Ratify-lift WITH a real cascade dependant: cascade message DOES appear.

        Positive regression guard: over-correcting Defect 1 must not suppress
        legitimate cascade reporting when there ARE real depends_on dependants.
        """
        import io
        import contextlib
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0498"
            doc_rel = "adr/test-adr-cascade.md"
            doc_path = self.root / doc_rel
            _write_doc(doc_path, "Drafted 2026-05-01. Awaiting ratification.")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[f"pantheon-config:{doc_rel}"],
            )

            # Write a dependant ticket in inbox/ that depends on T-0498.
            dep_tid = "T-0499"
            dep_fm = (
                "---\n"
                f"id: {dep_tid}\n"
                "created: 2026-05-20T10:00:00Z\n"
                "requester: orchestrator\n"
                "target: recovery\n"
                "state: inbox\n"
                "parent_ticket: null\n"
                f"depends_on: [{tid}]\n"
                "labels: []\n"
                "intent: action\n"
                "ratifiable_files: []\n"
                "---\n\n"
                f"# Dependent ticket {dep_tid}\n\n"
                "## Ask\n\nWait for T-0498.\n\n"
                "## Transition log\n\n"
                f"- 2026-05-20T10:00:00Z [orchestrator] filed in inbox\n"
            )
            dep_path = self.root / "tickets" / "inbox" / f"{dep_tid}.md"
            dep_path.write_text(dep_fm, encoding="utf-8")

            args = self._make_resolve_args(tid, "Ratified.")
            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)
            stdout_out = stdout_buf.getvalue()

            # Doc lifted
            new_content = doc_path.read_text(encoding="utf-8")
            self.assertIn("**Ratified", new_content)

            # Cascade message MUST still appear (real dependant was unblocked)
            self.assertIn(
                "auto-unblock cascade", stdout_out,
                "cascade message missing even though a real dependant was unblocked"
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_reject_resolve_does_not_lift_doc_status(self) -> None:
        """T-0641 regression: a ratify-intent ticket resolved with a 'Rejected: ...'
        summary (pantheon-pwa's /reject route → shellResolveTicket --with 'Rejected: <reason>')
        MUST NOT auto-lift the artifact's Status line. Without this guard the
        T-0491 lift fired on rejected ratify tickets, surfaced by the T-0637
        smoke test 2026-05-27: dummy ratification target got 'Ratified by the operator'
        status even though the operator rejected.
        """
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0997"  # synthetic id not used elsewhere in this file
            doc_rel = "adr/test-adr-reject.md"
            doc_path = self.root / doc_rel
            _write_doc(doc_path, "Drafted 2026-05-27. Awaiting the operator ratification.")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[f"pantheon-config:{doc_rel}"],
            )

            original_content = doc_path.read_text(encoding="utf-8")

            args = self._make_resolve_args(tid, "Rejected: out of scope")
            rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)

            # Doc Status line must be UNTOUCHED (no lift on Reject).
            self.assertEqual(doc_path.read_text(encoding="utf-8"), original_content)

            # Ticket still resolved (moved to done).
            self.assertTrue(
                (self.root / "tickets" / "done" / f"{tid}.md").exists()
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_ratify_resolve_with_leading_whitespace_still_lifts(self) -> None:
        """Sanity guard for the new gate: a "Ratified." summary with leading
        whitespace must still trigger the lift (the gate uses lstrip before the
        'Rejected:' prefix check, so 'Ratified.' with leading whitespace passes).
        """
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0998"
            doc_rel = "adr/test-adr-lstrip.md"
            doc_path = self.root / doc_rel
            _write_doc(doc_path, "Drafted 2026-05-27. Awaiting the operator ratification.")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[f"pantheon-config:{doc_rel}"],
            )

            args = self._make_resolve_args(tid, "   Ratified.")  # leading spaces
            rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)

            new_content = doc_path.read_text(encoding="utf-8")
            self.assertIn("**Ratified", new_content)
            self.assertIn("by the operator.", new_content)
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_non_ratify_resolve_does_not_touch_doc(self) -> None:
        """Plain (non-ratify intent) resolve does not write any doc file."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0492"
            doc_rel = "adr/test-adr2.md"
            doc_path = self.root / doc_rel
            _write_doc(doc_path, "Drafted 2026-05-01.")

            # Non-ratify ticket (intent=action, no ratifiable_files)
            fm = (
                "---\n"
                f"id: {tid}\n"
                "created: 2026-05-20T10:00:00Z\n"
                "requester: orchestrator\n"
                "target: recovery\n"
                "state: active\n"
                "parent_ticket: null\n"
                "depends_on: []\n"
                "labels: []\n"
                "intent: action\n"
                "ratifiable_files: []\n"
                "---\n\n"
                f"# Non-ratify ticket {tid}\n\n"
                "## Ask\n\nDo something.\n\n"
                "## Transition log\n\n"
                "- 2026-05-20T10:00:00Z [orchestrator] filed in inbox\n"
                "- 2026-05-20T10:01:00Z [orchestrator] claimed\n"
            )
            p = self.root / "tickets" / "active" / f"{tid}.md"
            p.write_text(fm, encoding="utf-8")

            original_content = doc_path.read_text(encoding="utf-8")

            args = self._make_resolve_args(tid, "done")
            rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)

            # Doc must be untouched
            self.assertEqual(doc_path.read_text(encoding="utf-8"), original_content)
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_cross_repo_entry_warns_and_resolves(self) -> None:
        """Cross-repo ratifiable_files entry: warns to stderr, ticket still resolves."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0493"
            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=["revenue-project:src/some/doc.md"],
            )

            import io
            import contextlib
            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                args = self._make_resolve_args(tid, "Ratified.")
                rc = ticket.cmd_resolve(args)

            self.assertEqual(rc, 0)
            stderr_out = stderr_buf.getvalue()
            self.assertIn("revenue-project", stderr_out)
            self.assertIn("skip", stderr_out.lower())
            # Ticket moved to done
            self.assertTrue(
                (self.root / "tickets" / "done" / f"{tid}.md").exists()
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_status_line_not_found_warns_and_resolves(self) -> None:
        """Doc exists but has no **Status:** line: warn, still resolve."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0494"
            doc_rel = "adr/no-status.md"
            doc_path = self.root / doc_rel
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text("# Doc\n\nNo status line here.\n", encoding="utf-8")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[f"pantheon-config:{doc_rel}"],
            )

            import io
            import contextlib
            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                args = self._make_resolve_args(tid, "Ratified.")
                rc = ticket.cmd_resolve(args)

            self.assertEqual(rc, 0)
            self.assertIn("Status", stderr_buf.getvalue())
            self.assertTrue(
                (self.root / "tickets" / "done" / f"{tid}.md").exists()
            )
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_guard_warning_surfaced_on_stderr_and_resolve_proceeds(self) -> None:
        """T-5373: residual-guard skip warns loudly on stderr; resolve succeeds."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0495"
            doc_rel = "adr/guard.md"
            doc_path = self.root / doc_rel
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            content = "# Doc\n\n**Status:** Draft 2026-10-22 (T-1). Build ticket T-1 pending.\n"
            doc_path.write_text(content, encoding="utf-8")
            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[f"pantheon-config:{doc_rel}"],
            )
            import io
            import contextlib
            stderr_buf = io.StringIO()
            with contextlib.redirect_stderr(stderr_buf):
                rc = ticket.cmd_resolve(self._make_resolve_args(tid, "Ratified."))
            self.assertEqual(rc, 0)
            self.assertIn("warning: ratify-lift:", stderr_buf.getvalue())
            self.assertIn("HAND FLIP", stderr_buf.getvalue())
            self.assertEqual(doc_path.read_text(encoding="utf-8"), content)
            self.assertTrue((self.root / "tickets" / "done" / f"{tid}.md").exists())
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_idempotent_already_ratified_no_op(self) -> None:
        """Doc whose Status already starts with Ratified: no change, clean resolve."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0495"
            doc_rel = "adr/already-ratified.md"
            doc_path = self.root / doc_rel
            _write_doc(doc_path, "**Ratified 2026-05-19 by the operator.** Drafted 2026-05-01.")
            original_content = doc_path.read_text(encoding="utf-8")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[f"pantheon-config:{doc_rel}"],
            )

            args = self._make_resolve_args(tid, "Ratified.")
            rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)

            # Content must be identical — no-op
            self.assertEqual(doc_path.read_text(encoding="utf-8"), original_content)
        finally:
            del os.environ["PANTHEON_ACTOR"]

    def test_multi_file_ratifiable_files_all_lifted(self) -> None:
        """Multiple same-repo ratifiable_files: each doc's Status is lifted."""
        os.environ["PANTHEON_ACTOR"] = "orchestrator"
        try:
            tid = "T-0496"
            doc_rel_a = "adr/multi-a.md"
            doc_rel_b = "sop/standard/multi-b.md"
            doc_a = self.root / doc_rel_a
            doc_b = self.root / doc_rel_b
            _write_doc(doc_a, "Drafted 2026-05-01. Awaiting ratification.")
            _write_doc(doc_b, "Authored 2026-05-05. Awaiting the operator ratification.")

            _write_ticket_file(
                self.root / "tickets" / "active" / f"{tid}.md",
                tid=tid,
                ratifiable_files=[
                    f"pantheon-config:{doc_rel_a}",
                    f"pantheon-config:{doc_rel_b}",
                ],
            )

            args = self._make_resolve_args(tid, "Ratified.")
            rc = ticket.cmd_resolve(args)
            self.assertEqual(rc, 0)

            for doc_path, rel in [(doc_a, doc_rel_a), (doc_b, doc_rel_b)]:
                content = doc_path.read_text(encoding="utf-8")
                self.assertIn("**Ratified", content, msg=f"Missing Ratified in {rel}")
                self.assertIn("by the operator.", content, msg=f"Missing 'by the operator.' in {rel}")
                self.assertNotIn("Awaiting", content, msg=f"Awaiting still in {rel}")
        finally:
            del os.environ["PANTHEON_ACTOR"]


# ---------------------------------------------------------------------------
# Layer-3 integration test: real tmp git repo, assert committed file content
# ---------------------------------------------------------------------------


class TestRatifyLiftLayer3(unittest.TestCase):
    """Layer-3: real tmp git repo. Asserts committed content via git show HEAD."""

    def setUp(self) -> None:
        from tools._test_fixtures import setup_integration_env, snapshot_real_repo
        self._real_snapshot = snapshot_real_repo()
        self._env = setup_integration_env()

        repo = self._env.repo_path
        # Create tickets dirs
        for s in ticket.STATES:
            (repo / "tickets" / s).mkdir(parents=True, exist_ok=True)

        # Minimal README
        readme = repo / "tickets" / "README.md"
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
        # Commit the README so git operations see a clean tree
        _git_setup(repo, ["tickets/README.md"], "test: add tickets dirs")

    def tearDown(self) -> None:
        from tools._test_fixtures import teardown_integration_env, assert_real_repo_untouched
        teardown_integration_env(self._env)
        assert_real_repo_untouched(self._real_snapshot)

    def test_layer3_ratify_resolve_lifts_status_committed(self) -> None:
        """End-to-end: cmd_resolve on a ratify ticket commits the doc lift.

        Layer-3: real tmp git repo; cmd_resolve runs in-process with
        REPO_ROOT, TICKETS_ROOT, and _git_utils._git all patched to operate
        in the tmp tree.  Assert committed content via git show HEAD:<path>.

        Note on cwd: _git()'s default cwd parameter is baked at import time
        (Python default-arg evaluation).  We patch _git_utils._git with a
        wrapper that forces cwd=repo so real git invocations hit the tmp repo.
        """
        import functools

        repo = self._env.repo_path

        # Write the ratifiable doc in the tmp repo
        doc_rel = "adr/ORG-999-test.md"
        doc_path = repo / doc_rel
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(
            "# ORG-999 Test\n\n"
            "**Status:** Drafted 2026-05-01 by Orchestrator. Awaiting the operator ratification.\n\n"
            "## Context\n\nTest doc for T-0491 layer-3 fixture.\n",
            encoding="utf-8",
        )
        _git_setup(repo, [doc_rel], "test: add ratifiable doc")

        # Write + commit the ticket in active/
        tid = "T-0991"
        ticket_rel = f"tickets/active/{tid}.md"
        ticket_path = repo / ticket_rel
        _write_ticket_file(
            ticket_path,
            tid=tid,
            ratifiable_files=["pantheon-config:" + doc_rel],
        )
        _git_setup(repo, [ticket_rel], f"test: add {tid}")

        # Wrap _git to always use the tmp repo cwd (baked-in default override).
        real_git_fn = _git_utils._git

        def _git_in_tmp(*args: str, cwd=None) -> object:
            return real_git_fn(*args, cwd=repo)

        # Patch REPO_ROOT, TICKETS_ROOT, and _git to operate in the tmp repo.
        with (
            patch.object(ticket, "REPO_ROOT", repo),
            patch.object(_git_utils, "REPO_ROOT", repo),
            patch.object(ticket, "TICKETS_ROOT", repo / "tickets"),
            patch.object(_git_utils, "_git", _git_in_tmp),
            patch.object(ticket, "_git", _git_in_tmp),
        ):
            os.environ["PANTHEON_ACTOR"] = "orchestrator"
            try:
                args = MagicMock()
                args.ticket = tid
                args.with_summary = "Ratified."
                args.no_push = True
                from tools.test_ticket import with_ruling  # T-5015
                with_ruling(args, repo)
                _git_setup(repo, ["notes/operator-decision-register.md"], "test: register")
                rc = ticket.cmd_resolve(args)
            finally:
                del os.environ["PANTHEON_ACTOR"]

        self.assertEqual(rc, 0)

        # Assert via git show HEAD:<doc_rel> that the committed content has Ratified
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"HEAD:{doc_rel}"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, f"git show failed: {result.stderr}")
        committed_content = result.stdout
        self.assertIn("**Ratified", committed_content)
        self.assertIn("by the operator.", committed_content)
        self.assertNotIn("Awaiting the operator ratification", committed_content)
        # History preserved
        self.assertIn("Drafted 2026-05-01", committed_content)


def _git_setup(repo: Path, files: list[str], message: str) -> None:
    """Stage and commit the given relative file paths in the tmp repo."""
    subprocess.run(["git", "-C", str(repo), "add"] + files, check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        check=True, capture_output=True,
    )


if __name__ == "__main__":
    unittest.main()
