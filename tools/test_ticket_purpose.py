"""Tests for T-3168 structured ticket-purpose links."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import argparse
import contextlib
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tools import ticket, ticket_lint, ticket_purpose as tp


REPO_ROOT = Path(__file__).resolve().parents[1]


def _purpose(
    tid: str,
    *,
    project_id: str | None = None,
    present: bool = True,
    nearest: str | None = "maintenance:ticket-board",
    serves: str | None = "keep ticket purpose observable",
    labels: frozenset[str] = frozenset(),
    state: str = "inbox",
) -> tp.TicketPurpose:
    return tp.TicketPurpose(
        ticket_id=tid,
        project_id=project_id,
        project_id_present=present,
        nearest_intent_ref=nearest,
        serves=serves,
        labels=labels,
        state=state,
    )


class TicketPurposeTests(unittest.TestCase):
    def test_ticket_frontmatter_round_trip_orders_structured_fields(self) -> None:
        fields = {
            "id": "T-4000",
            "created": "2026-09-01T00:00:00Z",
            "requester": "recovery",
            "target": "orchestrator",
            "state": "inbox",
            "project_id": None,
            "nearest_intent_ref": "maintenance:ticket-board",
            "parent_ticket": None,
            "depends_on": [],
            "serves": "ticket-board health",
        }
        rendered = ticket.serialize_front_matter(fields)
        self.assertLess(rendered.index("project_id:"), rendered.index("parent_ticket:"))
        parsed, _ = ticket.parse_front_matter(rendered + "\n# title\n")
        self.assertIn("project_id", parsed)
        self.assertIsNone(parsed["project_id"])
        self.assertEqual(parsed["nearest_intent_ref"], "maintenance:ticket-board")

    def test_file_cli_accepts_explicit_null_and_typed_nearest_ref(self) -> None:
        args = ticket._build_parser().parse_args([
            "file", "--requester", "recovery", "--target", "orchestrator",
            "--title", "Maintain the board", "--serves", "ticket board health",
            "--project-id", "null",
            "--nearest-intent-ref", "maintenance:ticket-board",
        ])
        self.assertEqual(
            ticket._purpose_values_from_args(args),
            (True, None, "maintenance:ticket-board"),
        )

    def test_goal_neutral_maintenance_without_named_asset_is_rejected(self) -> None:
        purpose = _purpose("T-4000", nearest=None, serves="goal-neutral-maintenance")
        issues = tp.validate_new_ticket_purpose(
            purpose, {purpose.ticket_id: purpose}, {}, frozenset(), tp.PurposeEnforcement.ENFORCE
        )
        self.assertIn("missing-structured-purpose", {issue.code for issue in issues})

    def test_warn_first_policy_has_a_real_enforcement_flip(self) -> None:
        issue = tp.PurposeIssue("dangling-ticket-purpose", "T-4000", "missing T-4999")
        ticket._apply_purpose_policy((issue,), mode=tp.PurposeEnforcement.WARN)
        with self.assertRaisesRegex(ticket.TicketError, "dangling-ticket-purpose"):
            ticket._apply_purpose_policy((issue,), mode=tp.PurposeEnforcement.ENFORCE)

    def test_parse_all_ref_kinds_and_reject_unsafe_maintenance(self) -> None:
        self.assertEqual(tp.parse_intent_ref("ticket:T-3168").value, "T-3168")
        self.assertEqual(tp.parse_intent_ref("project:ticket-system").value, "ticket-system")
        self.assertEqual(tp.parse_intent_ref("goal:durable-memory").value, "durable-memory")
        self.assertEqual(tp.parse_intent_ref("maintenance:tools/ticket.py").value, "tools/ticket.py")
        for raw in (
            "maintenance:",
            "maintenance:/absolute",
            "maintenance:tools/../secret",
            "maintenance:two words",
            "ticket:T-12",
            "unknown:value",
        ):
            with self.subTest(raw=raw), self.assertRaises(tp.PurposeError):
                tp.parse_intent_ref(raw)

    def test_explicit_null_is_distinct_from_absent_historic_field(self) -> None:
        explicit = _purpose("T-4000", present=True)
        absent = replace(explicit, ticket_id="T-4001", project_id_present=False)
        self.assertFalse(tp.validate_intent_link(explicit, {explicit.ticket_id: explicit}, {}, frozenset()))
        issues = tp.validate_intent_link(absent, {absent.ticket_id: absent}, {}, frozenset())
        self.assertIn("missing-structured-purpose", {issue.code for issue in issues})

    def test_ticket_chain_resolves_and_dangling_is_symmetric(self) -> None:
        root = _purpose("T-4000")
        child = _purpose("T-4001", nearest="ticket:T-4000")
        tickets = {p.ticket_id: p for p in (root, child)}
        self.assertEqual(
            tp.resolve_ticket_purpose_chain("T-4001", tickets, {}, frozenset()),
            ("T-4001", "T-4000", "maintenance:ticket-board"),
        )
        dangling = replace(root, nearest_intent_ref="ticket:T-4999")
        tickets = {dangling.ticket_id: dangling, child.ticket_id: child}
        self.assertEqual(
            {issue.code for issue in tp.validate_intent_link(dangling, tickets, {}, frozenset())},
            {"dangling-ticket-purpose"},
        )
        self.assertEqual(
            {issue.code for issue in tp.validate_intent_link(child, tickets, {}, frozenset())},
            {"dangling-ticket-purpose"},
        )

    def test_ticket_chain_rejects_conflicting_project_membership(self) -> None:
        parent = _purpose(
            "T-4000", project_id="alpha-project", nearest="project:alpha-project"
        )
        child = _purpose(
            "T-4001", project_id="beta-project", nearest="ticket:T-4000"
        )
        records = {"alpha-project": object(), "beta-project": object()}
        tickets = {p.ticket_id: p for p in (parent, child)}
        issues = tp.validate_intent_link(child, tickets, records, frozenset())
        self.assertIn("ticket-project-mismatch", {issue.code for issue in issues})

    def test_cycle_is_reported_from_every_member_and_resolver_refuses(self) -> None:
        left = _purpose("T-4000", nearest="ticket:T-4001")
        right = _purpose("T-4001", nearest="ticket:T-4000")
        tickets = {p.ticket_id: p for p in (left, right)}
        for purpose in tickets.values():
            issues = tp.validate_intent_link(purpose, tickets, {}, frozenset())
            self.assertEqual({issue.code for issue in issues}, {"purpose-cycle"})
            with self.assertRaisesRegex(tp.PurposeError, "purpose cycle"):
                tp.resolve_ticket_purpose_chain(purpose.ticket_id, tickets, {}, frozenset())

    def test_self_reference_requires_null_label_and_serves(self) -> None:
        legal = _purpose(
            "T-4000",
            nearest="ticket:T-4000",
            labels=frozenset({"project-formation"}),
        )
        self.assertEqual(tp.validate_intent_link(legal, {legal.ticket_id: legal}, {}, frozenset()), ())
        invalid = replace(legal, labels=frozenset())
        self.assertIn(
            "invalid-self-purpose",
            {issue.code for issue in tp.validate_intent_link(invalid, {invalid.ticket_id: invalid}, {}, frozenset())},
        )

    def test_goal_validation_stays_dormant_without_canonical_keys(self) -> None:
        purpose = _purpose("T-4000", nearest="goal:durable-memory")
        issues = tp.validate_new_ticket_purpose(
            purpose, {purpose.ticket_id: purpose}, {}, frozenset(), tp.PurposeEnforcement.ENFORCE
        )
        self.assertEqual({issue.code for issue in issues}, {"goal-registry-unavailable"})
        self.assertEqual(
            tp.validate_intent_link(
                purpose, {purpose.ticket_id: purpose}, {}, frozenset({"durable-memory"})
            ),
            (),
        )

    def test_project_membership_and_direct_project_ref_must_agree(self) -> None:
        purpose = _purpose(
            "T-4000", project_id="alpha-project", nearest="project:beta-project"
        )
        records = {"alpha-project": object(), "beta-project": object()}
        issues = tp.validate_intent_link(purpose, {purpose.ticket_id: purpose}, records, frozenset())
        self.assertEqual({issue.code for issue in issues}, {"project-purpose-mismatch"})

    def test_done_legacy_ticket_with_serves_remains_compatible(self) -> None:
        legacy = _purpose(
            "T-4000", present=False, nearest=None, state="done", serves="historic arc"
        )
        self.assertEqual(
            tp.validate_intent_link(legacy, {legacy.ticket_id: legacy}, {}, frozenset()), ()
        )
        self.assertEqual(
            tp.resolve_ticket_purpose_chain(legacy.ticket_id, {legacy.ticket_id: legacy}, {}, frozenset()),
            (legacy.ticket_id,),
        )

    def test_done_legacy_ticket_without_any_purpose_fields_is_quiet(self) -> None:
        legacy = _purpose(
            "T-4000", present=False, nearest=None, state="done", serves=None
        )
        self.assertEqual(
            tp.validate_intent_link(legacy, {legacy.ticket_id: legacy}, {}, frozenset()), ()
        )

    def test_purpose_command_preserves_state_body_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "tickets"
            path = root / "active" / "T-4000.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "---\nid: T-4000\ncreated: 2026-09-01T00:00:00Z\n"
                "requester: recovery\ntarget: orchestrator\nstate: active\n"
                "parent_ticket: null\ndepends_on: []\ncustom_future: keep-me\n"
                "serves: old purpose\n---\n\n# Work\n\n## Transition log\n\n"
                "- 2026-09-01T00:00:00Z [recovery] filed in inbox\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                ticket="T-4000",
                project_id="null",
                nearest_intent_ref="maintenance:ticket-board",
                serves="new purpose",
                no_push=True,
            )
            with patch.object(ticket, "TICKETS_ROOT", root), patch.object(
                ticket, "_purpose_issues_for_fields", return_value=()
            ), patch.object(ticket, "_pull_if_needed"), patch.object(
                ticket, "git_mutation_lock", return_value=contextlib.nullcontext()
            ), patch.object(ticket, "_commit_and_push") as commit:
                self.assertEqual(ticket.cmd_purpose(args), 0)
            fields, body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
            self.assertEqual(fields["state"], "active")
            self.assertEqual(fields["custom_future"], "keep-me")
            self.assertIsNone(fields["project_id"])
            self.assertEqual(fields["nearest_intent_ref"], "maintenance:ticket-board")
            self.assertIn("# Work", body)
            self.assertIn("purpose-set", body)
            commit.assert_called_once()

    def test_loader_round_trips_explicit_null_and_absence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "inbox").mkdir()
            (root / "done").mkdir()
            (root / "inbox" / "T-4000.md").write_text(
                "---\nid: T-4000\nstate: inbox\nproject_id: null\n"
                "nearest_intent_ref: maintenance:ticket-board\nserves: board health\n---\n",
                encoding="utf-8",
            )
            (root / "done" / "T-4001.md").write_text(
                "---\nid: T-4001\nstate: done\nserves: historic arc\n---\n",
                encoding="utf-8",
            )
            index = tp.load_ticket_purpose_index(root)
            self.assertTrue(index["T-4000"].project_id_present)
            self.assertIsNone(index["T-4000"].project_id)
            self.assertFalse(index["T-4001"].project_id_present)

    def test_lint_scans_structured_purpose_for_inbox_and_active_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            paths = []
            for state, tid in (("inbox", "T-4000"), ("active", "T-4001"), ("done", "T-4002")):
                path = repo / "tickets" / state / f"{tid}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    f"---\nid: {tid}\ncreated: 2026-09-01T00:00:00Z\n"
                    f"requester: recovery\ntarget: orchestrator\nstate: {state}\n"
                    "parent_ticket: null\ndepends_on: []\nserves: historic arc\n---\n",
                    encoding="utf-8",
                )
                paths.append(path)
            warnings = ticket_lint.warn_ticket_purposes(paths)
            self.assertEqual({warning.tid for warning in warnings}, {"T-4000", "T-4001"})
            digest = ticket_lint.build_lint_digest(
                "2026-09-01 00:00 UTC", [], [], 3, purpose_warned=warnings
            )
            self.assertIn("Structured ticket-purpose warnings (2)", digest)
            self.assertIn("ticket.py purpose <tid>", digest)

    def test_automated_filer_inventory_and_omission_threat(self) -> None:
        self.assertEqual(tp.audit_automated_filers(REPO_ROOT), ())
        self.assertNotIn("tools/cascade_diff_scan.py", tp.AUTOMATED_FILER_EXPECTATIONS)
        self.assertNotIn("tools/ticket_state_sweep.py", tp.AUTOMATED_FILER_EXPECTATIONS)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for relative in tp.AUTOMATED_FILER_EXPECTATIONS:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO_ROOT / relative, target)
            victim = root / "tools" / "knowledge_sweep.py"
            victim.write_text(
                victim.read_text(encoding="utf-8").replace(
                    '"--nearest-intent-ref"', '"--removed-purpose-ref"', 1
                ),
                encoding="utf-8",
            )
            issues = tp.audit_automated_filers(root)
            self.assertEqual({issue.ticket_id for issue in issues}, {"tools/knowledge_sweep.py"})


class TestAppetiteValidation(unittest.TestCase):
    """T-3440 / ORG-089 appetite + substate frontmatter shape validation."""

    def test_normalize_int_and_str_int(self) -> None:
        self.assertEqual(tp.normalize_appetite(7), 7)
        self.assertEqual(tp.normalize_appetite("7"), 7)
        self.assertEqual(tp.normalize_appetite("  10 "), 10)

    def test_normalize_none_passes_through(self) -> None:
        self.assertIsNone(tp.normalize_appetite(None))

    def test_normalize_refuses_non_int(self) -> None:
        for bad in ("high", "7.5", "", "seven", 7.5, True):
            with self.assertRaises(tp.AppetiteError):
                tp.normalize_appetite(bad)

    def test_normalize_refuses_out_of_range(self) -> None:
        for bad in (0, 11, "0", "11", -3):
            with self.assertRaises(tp.AppetiteError):
                tp.normalize_appetite(bad)

    def test_appetite_error_is_purpose_error(self) -> None:
        # Subclassing keeps existing PurposeError catch sites intact.
        self.assertTrue(issubclass(tp.AppetiteError, tp.PurposeError))

    def test_validate_fields_accepts_clean_block(self) -> None:
        tp.validate_appetite_fields({"appetite": "7", "bet_ref": "ORG-089"})
        tp.validate_appetite_fields({"substate": "appetite-overrun"})
        tp.validate_appetite_fields({
            "substate": "dropped",
            "dropped_reason": "not worth it",
            "dropped_from_state": "active",
        })
        tp.validate_appetite_fields({})  # all absent is valid

    def test_validate_fields_refuses_unknown_substate(self) -> None:
        with self.assertRaises(tp.AppetiteError):
            tp.validate_appetite_fields({"substate": "paused"})

    def test_validate_fields_dropped_requires_reason(self) -> None:
        with self.assertRaises(tp.AppetiteError):
            tp.validate_appetite_fields({"substate": "dropped"})
        with self.assertRaises(tp.AppetiteError):
            tp.validate_appetite_fields({"substate": "dropped", "dropped_reason": "   "})

    def test_validate_fields_refuses_empty_bet_ref(self) -> None:
        with self.assertRaises(tp.AppetiteError):
            tp.validate_appetite_fields({"bet_ref": "  "})

    def test_validate_fields_refuses_bad_dropped_from_state(self) -> None:
        with self.assertRaises(tp.AppetiteError):
            tp.validate_appetite_fields({
                "substate": "dropped",
                "dropped_reason": "x",
                "dropped_from_state": "done",
            })


if __name__ == "__main__":
    unittest.main()
