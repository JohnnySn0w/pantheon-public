"""Tests for T-3168 structured ticket-purpose links."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import argparse
import contextlib
import io
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

    def test_operational_root_label_is_a_purpose_terminal(self) -> None:
        """T-3332 (b): a child of a labelled operational root (T-5677 shape:
        no serves, no intent ref) is rooted; an ordinary unterminated parent
        still raises missing-purpose-terminal."""
        root = _purpose("T-5677", nearest=None, serves=None,
                        labels=frozenset({tp.OPERATIONAL_ROOT_LABEL, "meta", "tracker"}))
        child = _purpose("T-5678", nearest="ticket:T-5677")
        tickets = {p.ticket_id: p for p in (root, child)}
        codes = {i.code for i in tp.validate_intent_link(child, tickets, {}, frozenset())}
        self.assertNotIn("missing-purpose-terminal", codes)
        self.assertEqual(
            tp.resolve_ticket_purpose_chain("T-5678", tickets, {}, frozenset()),
            ("T-5678", "T-5677"),
        )
        plain = replace(root, labels=frozenset({"meta", "tracker"}))
        tickets = {plain.ticket_id: plain, child.ticket_id: child}
        codes = {i.code for i in tp.validate_intent_link(child, tickets, {}, frozenset())}
        self.assertIn("missing-purpose-terminal", codes)
        with self.assertRaises(tp.PurposeError):
            tp.resolve_ticket_purpose_chain("T-5678", tickets, {}, frozenset())

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

    def test_purpose_command_auto_adds_justifying_label_when_unjustified(self) -> None:
        """T-4090: `cmd_purpose` sets project_id + nearest_intent_ref
        together -- same failure class as `file` (T-4080). `purpose` has no
        --labels flag, so when neither the ticket's existing labels nor the
        new nearest_intent_ref justify the project_id, the project id is
        auto-added as a label rather than landing unjustified."""
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            tickets_root = repo_root / "tickets"
            path = tickets_root / "active" / "T-4000.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "---\nid: T-4000\ncreated: 2026-09-01T00:00:00Z\n"
                "requester: recovery\ntarget: orchestrator\nstate: active\n"
                "parent_ticket: null\ndepends_on: []\n"
                "serves: old purpose\n---\n\n# Work\n\n## Transition log\n\n"
                "- 2026-09-01T00:00:00Z [recovery] filed in inbox\n",
                encoding="utf-8",
            )
            records_root = repo_root / "projects" / "records"
            records_root.mkdir(parents=True)
            (records_root / "alpha-project.yaml").write_text(
                "schema_version: 1\n"
                "project_id: alpha-project\n"
                "telos: test fixture\n"
                "parent_goal_ref: null\n"
                "classification: independent\n"
                "boundary: {inside: [x], outside: [y], not_owned: [z]}\n"
                "interfaces: {provides: [a], requires: [b]}\n"
                "stewardship: {role: project-steward, authority_surface: 'decision:x'}\n"
                "lifecycle: {developmental_mode: shaping, "
                "formed_at: '2026-08-26T00:00:00Z', transition_refs: []}\n"
                "evidence: {health: [], user_value: [], safety: [], developmental: []}\n"
                "tensions: [{pole_a: a, pole_b: b, rebalance_trigger: c}]\n"
                "operational_contract:\n"
                "  obligations: {deployment: [], data: [], security: [], cost: [], "
                "maintenance: [], recovery: []}\n"
                "  lifecycle: {install: [], configure: [], health_monitor: [], "
                "update: [], remove: []}\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                ticket="T-4000",
                project_id="alpha-project",
                nearest_intent_ref="maintenance:ticket-board",
                serves=None,
                no_push=True,
            )
            with patch.object(ticket, "TICKETS_ROOT", tickets_root), patch.object(
                ticket, "REPO_ROOT", repo_root
            ), patch.object(
                ticket, "_purpose_issues_for_fields", return_value=()
            ), patch.object(ticket, "_pull_if_needed"), patch.object(
                ticket, "git_mutation_lock", return_value=contextlib.nullcontext()
            ), patch.object(ticket, "_commit_and_push") as commit:
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    self.assertEqual(ticket.cmd_purpose(args), 0)
            fields, _body = ticket.parse_front_matter(path.read_text(encoding="utf-8"))
            self.assertEqual(fields.get("labels"), ["alpha-project"])
            self.assertIn("auto-added label", buf.getvalue())
            commit.assert_called_once()

    def test_purpose_command_refuses_nonexistent_project_before_any_auto_add_note(
        self,
    ) -> None:
        """T-4090 REBOUND-1 finding 2: a nonexistent --project-id record
        must refuse cleanly and EARLY -- before `_ensure_project_label_
        justifies_filing` ever prints its WARN-mode "WARNING" / "auto-added
        label ... to justify" note (default TICKET_PURPOSE_ENFORCEMENT is
        "warn", left un-patched here). Deliberately does NOT stub
        `_purpose_issues_for_fields`, so the real interplay between the new
        early check and the pre-existing hardcoded-ENFORCE downstream graph
        check is exercised, not mocked away."""
        with tempfile.TemporaryDirectory() as td:
            repo_root = Path(td)
            tickets_root = repo_root / "tickets"
            path = tickets_root / "active" / "T-4001.md"
            path.parent.mkdir(parents=True)
            original_text = (
                "---\nid: T-4001\ncreated: 2026-09-01T00:00:00Z\n"
                "requester: recovery\ntarget: orchestrator\nstate: active\n"
                "parent_ticket: null\ndepends_on: []\n"
                "serves: old purpose\n---\n\n# Work\n\n## Transition log\n\n"
                "- 2026-09-01T00:00:00Z [recovery] filed in inbox\n"
            )
            path.write_text(original_text, encoding="utf-8")
            # No projects/records/ directory at all -- load_project_records
            # degrades to {} for a missing root, so "ghost-project" is
            # unambiguously not a real project record.
            args = argparse.Namespace(
                ticket="T-4001",
                project_id="ghost-project",
                nearest_intent_ref="maintenance:ticket-board",
                serves=None,
                no_push=True,
            )
            with patch.object(ticket, "TICKETS_ROOT", tickets_root), patch.object(
                ticket, "REPO_ROOT", repo_root
            ), patch.object(ticket, "_pull_if_needed"), patch.object(
                ticket, "git_mutation_lock", return_value=contextlib.nullcontext()
            ), patch.object(ticket, "_commit_and_push") as commit:
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    with self.assertRaises(ticket.TicketError) as ctx:
                        ticket.cmd_purpose(args)
            self.assertIn("ghost-project", str(ctx.exception))
            self.assertIn("projects/records", str(ctx.exception))
            self.assertNotIn("auto-added label", buf.getvalue())
            self.assertNotIn("WARNING", buf.getvalue())
            commit.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), original_text)

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

    def test_initial_backfill_never_infers_project_from_repo_or_persona(self) -> None:
        """T-2414 plan's named T-3169 acceptance test: a ticket's project_id is
        legitimate only via an exact-label match on a real project id, or via
        a ticket:<parent> nearest_intent_ref chain hop landing on a ticket
        that already carries that project_id -- never merely because a
        subject, repository, or persona string happens to contain the
        project's name (T-3169's own live check found exactly this false
        positive: 'pantheon-config' and 'pantheon-pwa-release-mirror' both
        word-boundary-match 'pantheon' without being that project).

        T-4090: the predicate itself now lives in tools.ticket_purpose as
        `project_id_justified` (shared with ticket.py's file-time /
        purpose-set / label-remove enforcement) -- this test exercises the
        same 4 fixture cases against the shared function rather than a local
        copy, so the two can never drift apart the way they did in T-4080."""

        justified = tp.project_id_justified

        tracker = _purpose(
            "T-9000",
            project_id="biometrics",
            nearest=None,
            labels=frozenset({"biometrics", "project-tracker"}),
        )
        unjustified = _purpose(
            "T-9001",
            project_id="biometrics",
            nearest=None,
            labels=frozenset({"hardware"}),
        )
        chained = _purpose(
            "T-9002",
            project_id="biometrics",
            nearest="ticket:T-9000",
            labels=frozenset({"hardware"}),
        )
        broken_chain = _purpose(
            "T-9003",
            project_id="biometrics",
            nearest="ticket:T-9999",
            labels=frozenset({"hardware"}),
        )
        index = {"T-9000": tracker, "T-9002": chained, "T-9003": broken_chain}
        self.assertTrue(justified(tracker, index))
        self.assertFalse(justified(unjustified, index))
        self.assertTrue(justified(chained, index))
        self.assertFalse(justified(broken_chain, index))

        # Live-corpus enforcement: every currently-committed non-done ticket
        # whose project_id is set must be justified the same way. The corpus
        # now legitimately carries hundreds of justified project_ids (the
        # project-mapping backfill landed) -- this is no longer a
        # starts-empty-stays-empty check. As of T-4090, `ticket.py file` /
        # `purpose` auto-add the justifying label at write time and `label
        # remove` refuses a removal that would break it, so this assertion
        # is now a backstop rather than the sole enforcement: it still
        # catches any filing path that bypasses ticket.py entirely (direct
        # file writes, migrations, hand edits) -- which is exactly how
        # T-4080's 8 unjustified tickets got onto main in the first place.
        live = tp.load_ticket_purpose_index(REPO_ROOT / "tickets")
        violations = [
            pid
            for pid, purpose in live.items()
            if purpose.state != "done" and not justified(purpose, live)
        ]
        self.assertEqual(violations, [])

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


class TestPurposeFromText(unittest.TestCase):
    """T-4402: `purpose_from_text` parses ONE ticket's text (e.g. a staged
    blob) with exactly the parser and field rules `load_ticket_purpose_index`
    uses, so the pre-commit content gate and the nightly live-corpus test can
    never disagree about what a ticket says."""

    TEXT = (
        "---\n"
        "id: T-9100\n"
        "state: active\n"
        "project_id: biometrics\n"
        "nearest_intent_ref: ticket:T-9000\n"
        "labels: [hardware, biometrics]\n"
        "serves: keep the parity test honest\n"
        "---\n\n# title\n"
    )

    def test_matches_the_index_loader_for_the_same_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "active").mkdir()
            (root / "active" / "T-9100.md").write_text(self.TEXT, encoding="utf-8")
            loaded = tp.load_ticket_purpose_index(root)["T-9100"]
        parsed = tp.purpose_from_text(
            self.TEXT, source="tickets/active/T-9100.md", default_state="active"
        )
        self.assertEqual(parsed, loaded)
        self.assertEqual(parsed.project_id, "biometrics")
        self.assertEqual(parsed.labels, frozenset({"hardware", "biometrics"}))

    def test_default_state_used_only_when_frontmatter_has_none(self) -> None:
        text = self.TEXT.replace("state: active\n", "")
        parsed = tp.purpose_from_text(text, source="x", default_state="inbox")
        self.assertEqual(parsed.state, "inbox")
        explicit = tp.purpose_from_text(self.TEXT, source="x", default_state="inbox")
        self.assertEqual(explicit.state, "active")

    def test_bad_frontmatter_raises_purpose_error_naming_the_source(self) -> None:
        with self.assertRaises(tp.PurposeError) as cm:
            tp.purpose_from_text("no frontmatter\n", source="tickets/inbox/T-1.md",
                                 default_state="inbox")
        self.assertIn("tickets/inbox/T-1.md", str(cm.exception))
        with self.assertRaises(tp.PurposeError):
            tp.purpose_from_text("---\nstate: inbox\n---\n", source="x",
                                 default_state="inbox")  # no id


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

    def test_validate_fields_accepts_awaiting_merge_substate(self) -> None:
        # T-4119: awaiting-merge is a recognized substate (symmetric with the
        # appetite-overrun / dropped acceptances above) — no awaiting_merge_ref
        # shape rule lives here (validate_appetite_fields stays scoped to the
        # ORG-089 fields it already owned; ref/substate consistency is a
        # ticket_lint.py corpus check, not this file's concern).
        tp.validate_appetite_fields({"substate": "awaiting-merge"})

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


class TestGoalRegistryWiringT5679(unittest.TestCase):
    """T-5679: purpose validation reads the canonical goals.md goal keys."""

    FIXTURE = REPO_ROOT / "tools" / "fixtures" / "goal_registry" / "goal_headings_ok.md"

    def _repo(self, td: str, *, with_registry: bool) -> Path:
        from tools import goal_registry as gr

        repo = Path(td)
        (repo / "tickets" / "inbox").mkdir(parents=True)
        if with_registry:
            target = repo / gr.GOALS_RELPATH
            target.parent.mkdir(parents=True)
            target.write_bytes(self.FIXTURE.read_bytes())
        return repo

    @staticmethod
    def _fields(nearest: str) -> dict:
        return {
            "id": "T-4000",
            "state": "inbox",
            "project_id": None,
            "nearest_intent_ref": nearest,
            "serves": "fixture purpose",
            "labels": [],
        }

    def _file_time_codes(self, repo: Path, nearest: str) -> dict[str, str]:
        with patch.object(ticket, "REPO_ROOT", repo), patch.object(
            ticket, "TICKETS_ROOT", repo / "tickets"
        ):
            issues = ticket._purpose_issues_for_fields(
                self._fields(nearest), mode=tp.PurposeEnforcement.WARN
            )
        return {issue.code: issue.detail for issue in issues}

    def test_file_time_validation_sees_registered_goal_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(td, with_registry=True)
            self.assertEqual(self._file_time_codes(repo, "goal:alpha-goal"), {})
            self.assertEqual(self._file_time_codes(repo, "goal:gamma-site"), {})
            codes = self._file_time_codes(repo, "goal:operator-bandwidth")
            self.assertEqual(set(codes), {"dangling-goal-purpose"})

    def test_file_time_registry_unavailable_only_when_missing_and_names_reason(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(td, with_registry=False)
            codes = self._file_time_codes(repo, "goal:alpha-goal")
            self.assertEqual(set(codes), {"goal-registry-unavailable"})
            self.assertIn("goal registry missing", codes["goal-registry-unavailable"])
            # A non-goal purpose is unaffected by the missing registry.
            self.assertEqual(self._file_time_codes(repo, "maintenance:ticket-board"), {})

    def _lint_codes(self, repo: Path, nearest: str) -> dict[str, str]:
        path = repo / "tickets" / "inbox" / "T-4000.md"
        path.write_text(
            "---\nid: T-4000\ncreated: 2026-09-01T00:00:00Z\nrequester: recovery\n"
            f"target: orchestrator\nstate: inbox\nproject_id: null\nnearest_intent_ref: {nearest}\n"
            "parent_ticket: null\ndepends_on: []\nserves: fixture purpose\n---\n",
            encoding="utf-8",
        )
        found: dict[str, str] = {}
        for result in ticket_lint.warn_ticket_purposes([path]):
            code, _, detail = result.description.removeprefix("structured purpose ").partition(": ")
            found[code] = detail
        return found

    def test_lint_digest_sees_registered_goal_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(td, with_registry=True)
            self.assertEqual(self._lint_codes(repo, "goal:beta-site"), {})
            self.assertEqual(
                set(self._lint_codes(repo, "goal:operator-bandwidth")), {"dangling-goal-purpose"}
            )

    def test_lint_digest_registry_unavailable_names_reason(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(td, with_registry=False)
            codes = self._lint_codes(repo, "goal:beta-site")
            self.assertEqual(set(codes), {"goal-registry-unavailable"})
            self.assertIn("goal registry missing", codes["goal-registry-unavailable"])

    def test_with_goal_registry_reason_only_rewrites_unavailable(self) -> None:
        issues = (
            tp.PurposeIssue("goal-registry-unavailable", "T-1", "old"),
            tp.PurposeIssue("missing-serves", "T-1", "keep"),
        )
        self.assertEqual(tp.with_goal_registry_reason(issues, None), issues)
        rewritten = tp.with_goal_registry_reason(issues, "goal registry missing: x")
        self.assertEqual(rewritten[0].detail, "goal registry unavailable: goal registry missing: x")
        self.assertEqual(rewritten[1], issues[1])

    def test_live_repo_has_no_goal_registry_unavailable(self) -> None:
        index = tp.load_ticket_purpose_index(REPO_ROOT / "tickets")
        from tools import goal_registry as gr
        from tools.project_catalogue import load_project_records

        keys, error = gr.registry_status(REPO_ROOT)
        self.assertIsNone(
            error,
            "T-5679: the tracked goals.md must carry the ratified goal-key markers",
        )
        records = load_project_records(REPO_ROOT / "projects" / "records")
        unavailable = [
            purpose.ticket_id
            for purpose in index.values()
            for issue in tp.validate_intent_link(purpose, index, records, keys)
            if issue.code == "goal-registry-unavailable"
        ]
        self.assertEqual(
            unavailable,
            [],
            "T-5679: goal-registry-unavailable fires on the live corpus; the tracked "
            "goals.md must carry the ratified goal-key markers",
        )


if __name__ == "__main__":
    unittest.main()
