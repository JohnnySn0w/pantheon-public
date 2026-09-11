#!/usr/bin/env python3
"""Standing-suite contract tests for the ORG-061 operational indexes (T-2426)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools import grand_project_program as program


def _tickets(root: Path) -> Path:
    tickets = root / "tickets"
    for state in ("inbox", "active", "done", "blocked"):
        (tickets / state).mkdir(parents=True)
    return tickets


def _ticket(
    tickets: Path,
    ticket_id: str,
    *,
    state: str = "done",
    labels: tuple[str, ...] = (),
    depends_on: tuple[str, ...] = (),
    receipts: tuple[str, ...] = (),
    target: str = "orchestrator",
    body: str = "fixture",
) -> Path:
    path = tickets / state / f"{ticket_id}.md"
    path.write_text(
        textwrap.dedent(
            f"""\
            ---
            id: {ticket_id}
            created: 2026-08-25T00:00:00Z
            requester: codex
            target: {target}
            state: {state}
            depends_on: [{', '.join(depends_on)}]
            receipts: [{', '.join(receipts)}]
            labels: [{', '.join(labels)}]
            serves: test
            ---

            # Fixture

            ## Ask

            {body}

            ## Context

            ## Transition log
            - fixture
            """
        )
    )
    return path


def _source(obligation_id: str = "ORG-061:R1", text: str = "a trigger") -> program.SourceTrigger:
    return program.SourceTrigger(
        obligation_id=obligation_id,
        source_ref=f"adr:{obligation_id.split(':')[0]}#{obligation_id.split(':')[1]}",
        canonical_text=text,
    )


def _obligation(
    source: program.SourceTrigger | None = None,
    **changes: object,
) -> program.ReevaluationObligation:
    source = source or _source()
    values: dict[str, object] = {
        "obligation_id": source.obligation_id,
        "source_ref": source.source_ref,
        "source_text_sha256": program.canonical_trigger_digest(source),
        "owner_ref": "role:grand-project-program-steward",
        "review_target": "orchestrator",
        "trigger_kind": "proposal",
        "operational_predicate": "surface-only",
        "observable_source": "tickets/grand-project-stage-evidence.yaml",
        "state": "armed",
        "armed_ticket": None,
        "last_evaluated": None,
        "next_check": None,
        "evidence_refs": (),
        "outcome_ref": None,
    }
    values.update(changes)
    return program.ReevaluationObligation(**values)


def _stage(ordinal: int, **changes: object) -> program.StageRecord:
    names = {
        1: "Harness parity and control recovery",
        2: "Installation and seed viability",
        3: "Persona restoration",
        4: "Bridge establishment",
        5: "Echo conversational interface",
    }
    values: dict[str, object] = {
        "stage_id": f"ORG-061:STAGE-{ordinal}",
        "ordinal": ordinal,
        "name": names.get(ordinal, f"Stage {ordinal}"),
        "outcome_ref": f"adr:ORG-061#stage-{ordinal}",
        "state": "shaping",
        "steward_ref": "role:grand-project-program-steward",
        "prerequisite_stage_ids": () if ordinal == 1 else (f"ORG-061:STAGE-{ordinal - 1}",),
        "research_probe_allowed": True,
        "research_probe_boundary_ref": "adr:ORG-061#section-1",
        "evidence_refs": program.EvidenceRefs((), (), (), ()),
        "denial_failure_rollback_refs": (),
        "acceptance_ticket": None,
        "acceptance_authority_ref": None,
        "reorientation_gate_ticket": None,
        "reorientation_receipt": None,
        "next_production_tickets": (),
        "applicable_obligation_ids": (),
        "outstanding_risk_refs": (),
        "last_reorientation": None,
    }
    values.update(changes)
    return program.StageRecord(**values)


class PlannedApiContract(unittest.TestCase):
    def test_planned_api_is_present(self) -> None:
        expected = {
            "load_stage_index",
            "load_obligation_index",
            "scan_source_obligations",
            "canonical_trigger_digest",
            "lint_obligation_completeness",
            "lint_stage_evidence",
            "resolve_ticket_receipt",
            "due_obligations",
            "existing_review_ticket",
            "build_review_ticket_args",
            "evaluate_indexes",
            "lint_repository_indexes",
        }
        self.assertEqual(expected - set(dir(program)), set())


class TriggerCensusAndDigest(unittest.TestCase):
    def test_digest_is_domain_separated_and_whitespace_stable(self) -> None:
        a = _source(text="first\n  live\tgate")
        b = _source(text="first live gate")
        self.assertEqual(program.canonical_trigger_digest(a), program.canonical_trigger_digest(b))
        self.assertNotEqual(
            program.canonical_trigger_digest(a),
            program.canonical_trigger_digest(_source(text="first live gate!")),
        )

    def test_real_corpus_has_exact_51_unique_owned_source_ids(self) -> None:
        triggers = program.scan_source_obligations(Path(__file__).resolve().parent.parent / "architecture" / "adr")
        counts: dict[str, int] = {}
        for trigger in triggers:
            source = trigger.obligation_id.split(":", 1)[0]
            counts[source] = counts.get(source, 0) + 1
        self.assertEqual(
            counts,
            {
                "ORG-053": 7,
                "ORG-054": 4,
                "ORG-055": 5,
                "ORG-056": 5,
                "ORG-057": 5,
                "ORG-058": 7,
                "ORG-059": 6,
                "ORG-060": 6,
                "ORG-061": 6,
            },
        )
        self.assertEqual(len({trigger.obligation_id for trigger in triggers}), 51)


class CompletenessKnownPositives(unittest.TestCase):
    def _codes(
        self,
        sources: tuple[program.SourceTrigger, ...],
        obligations: tuple[program.ReevaluationObligation, ...],
        tickets: Path,
    ) -> set[str]:
        return {f.code for f in program.lint_obligation_completeness(sources, obligations, tickets)}

    def test_missing_owned_record_is_red_then_corrected_green(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            source = _source()
            self.assertIn("obligation-source-missing-owned", self._codes((source,), (), tickets))
            self.assertNotIn(
                "obligation-source-missing-owned",
                self._codes((source,), (_obligation(source),), tickets),
            )

    def test_owned_record_without_source_is_red(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            self.assertIn("obligation-owned-missing-source", self._codes((), (_obligation(),), tickets))

    def test_duplicate_owned_id_is_red_even_when_identical(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            source = _source()
            owned = _obligation(source)
            self.assertIn(
                "obligation-owned-duplicate-id",
                self._codes((source,), (owned, owned), tickets),
            )

    def test_digest_drift_is_red(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            source = _source()
            owned = dataclasses.replace(_obligation(source), source_text_sha256="sha256:" + "0" * 64)
            self.assertIn("obligation-source-digest-drift", self._codes((source,), (owned,), tickets))

    def test_open_obligation_requires_exactly_one_matching_open_ticket(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            source = _source()
            owned = _obligation(source, state="open")
            self.assertIn("obligation-open-ticket-count", self._codes((source,), (owned,), tickets))
            labels = ("adr-reeval", f"reeval:{source.obligation_id}")
            _ticket(tickets, "T-9001", state="inbox", labels=labels)
            self.assertNotIn("obligation-open-ticket-count", self._codes((source,), (owned,), tickets))
            _ticket(tickets, "T-9002", state="active", labels=labels)
            self.assertIn("obligation-open-ticket-count", self._codes((source,), (owned,), tickets))

    def test_stale_open_obligation_is_red(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            source = _source()
            owned = _obligation(source, state="open", armed_ticket="T-9001")
            self.assertIn("obligation-open-ticket-stale", self._codes((source,), (owned,), tickets))


class StrictLoaderKnownPositives(unittest.TestCase):
    def test_duplicate_yaml_mapping_key_refuses(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "index.yaml"
            path.write_text("schema_version: 1\nschema_version: 1\nstages: []\n")
            with self.assertRaisesRegex(ValueError, "duplicate-key"):
                program.load_stage_index(path)

    def test_yaml_alias_refuses(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "index.yaml"
            path.write_text("schema_version: &v 1\nstages: []\n")
            with self.assertRaisesRegex(ValueError, "alias"):
                program.load_stage_index(path)

    def test_noncanonical_stage_id_refuses(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "index.yaml"
            path.write_text(
                textwrap.dedent(
                    """\
                    schema_version: 1
                    stages:
                      - stage_id: ORG-061:STAGE-01
                        ordinal: 1
                        name: Harness parity and control recovery
                        outcome_ref: adr:ORG-061#stage-1
                        state: shaping
                        steward_ref: role:steward
                        prerequisite_stage_ids: []
                        research_probe_policy: {allowed: true, boundary_ref: adr:ORG-061#section-1}
                        evidence_refs: {functional: [], continuity: [], trust: [], developmental: []}
                        denial_failure_rollback_refs: []
                        acceptance_ticket: null
                        acceptance_authority_ref: null
                        reorientation_gate_ticket: null
                        reorientation_receipt: null
                        next_production_tickets: []
                        applicable_obligation_ids: []
                        outstanding_risk_refs: []
                        last_reorientation: null
                    """
                )
            )
            with self.assertRaisesRegex(ValueError, "invalid-stage-id"):
                program.load_stage_index(path)

    def test_yaml_root_must_be_mapping(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "index.yaml"
            path.write_text("- schema_version\n- stages\n")
            with self.assertRaisesRegex(ValueError, "root-not-mapping"):
                program.load_stage_index(path)


class ReceiptAndStagePassage(unittest.TestCase):
    def _valid_passed(self, tickets: Path) -> program.StageRecord:
        acceptance_receipt = "ticket:T-9100#observable:T-9100/review"
        authority_receipt = "ticket:T-9101#observable:T-9101/accept"
        gate_receipt = "ticket:T-9200#observable:T-9200/gate"
        _ticket(
            tickets,
            "T-9100",
            state="done",
            receipts=("ORG-061#E6=observable:T-9100/review=reviewer",),
        )
        _ticket(
            tickets,
            "T-9101",
            state="done",
            target="operator",
            receipts=("ORG-061#E6=observable:T-9101/accept=reviewer",),
        )
        _ticket(
            tickets,
            "T-9200",
            state="done",
            depends_on=("T-9100",),
            receipts=("ORG-061#E6=observable:T-9200/gate=reviewer",),
            body="; ".join(
                (
                    "what the stage made possible",
                    "what the concrete implementation revealed",
                    "which tensions moved and which remain",
                    "current developmental mode",
                    "whether the next stage/order/contract still serves the root north star",
                    "changes to catalogue implications and upkeep",
                    "user ruling required at goal/relationship level",
                )
            ),
        )
        _ticket(tickets, "T-9300", state="inbox", depends_on=("T-9200",))
        refs = program.EvidenceRefs(
            functional=(acceptance_receipt,),
            continuity=(acceptance_receipt,),
            trust=(acceptance_receipt,),
            developmental=(acceptance_receipt,),
        )
        return _stage(
            1,
            state="passed",
            evidence_refs=refs,
            denial_failure_rollback_refs=(acceptance_receipt,),
            acceptance_ticket="T-9100",
            acceptance_authority_ref=authority_receipt,
            reorientation_gate_ticket="T-9200",
            reorientation_receipt=gate_receipt,
            next_production_tickets=("T-9300",),
            last_reorientation=gate_receipt,
        )

    def test_ticket_receipt_resolves_exact_middle_id(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            encoded = "ORG-061#E6=observable:T-9001/gate=reviewer"
            _ticket(tickets, "T-9001", receipts=(encoded,))
            got = program.resolve_ticket_receipt(
                "ticket:T-9001#observable:T-9001/gate", tickets, require_done=True
            )
            self.assertIsInstance(got, program.ReceiptResolution)
            self.assertEqual(got.encoded_receipt, encoded)

    def test_passed_stage_without_reorientation_receipt_is_red(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            stage = _stage(1, state="passed")
            codes = {f.code for f in program.lint_stage_evidence((stage,), tickets)}
            self.assertIn("stage-passed-without-reorientation-receipt", codes)
            self.assertIn("stage-passed-missing-functional-evidence", codes)
            self.assertIn("stage-passed-missing-denial-failure-rollback", codes)

    def test_stage_five_cannot_bypass_stage_four_gate(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            _ticket(tickets, "T-9400", state="done", receipts=("ORG-061#E6=gate-four=reviewer",))
            _ticket(tickets, "T-9500", state="inbox", depends_on=())
            stage4 = _stage(
                4,
                state="passed",
                reorientation_gate_ticket="T-9400",
                reorientation_receipt="ticket:T-9400#gate-four",
            )
            stage5 = _stage(5, next_production_tickets=("T-9500",))
            codes = {f.code for f in program.lint_stage_evidence((stage4, stage5), tickets)}
            self.assertIn("stage-bridge-before-echo-bypass", codes)

    def test_fully_receipted_passed_stage_has_no_passage_findings(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            stage = self._valid_passed(tickets)
            codes = {
                finding.code
                for finding in program.lint_stage_evidence((stage,), tickets)
                if finding.code.startswith("stage-passed")
                or finding.code.startswith("stage-next-production")
            }
            self.assertEqual(codes, set())

    def test_each_evidence_class_is_independently_required(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            stage = self._valid_passed(tickets)
            for class_name in ("functional", "continuity", "trust", "developmental"):
                evidence = dataclasses.replace(stage.evidence_refs, **{class_name: ()})
                changed = dataclasses.replace(stage, evidence_refs=evidence)
                codes = {f.code for f in program.lint_stage_evidence((changed,), tickets)}
                self.assertIn(f"stage-passed-missing-{class_name}-evidence", codes)

    def test_independent_review_and_operator_authority_receipts_are_required(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            stage = self._valid_passed(tickets)
            acceptance = tickets / "done" / "T-9100.md"
            acceptance.write_text(acceptance.read_text().replace("=reviewer", "=self"))
            codes = {f.code for f in program.lint_stage_evidence((stage,), tickets)}
            self.assertIn("stage-passed-missing-independent-review", codes)
            authority = tickets / "done" / "T-9101.md"
            authority.write_text(authority.read_text().replace("target: operator", "target: recovery"))
            codes = {f.code for f in program.lint_stage_evidence((stage,), tickets)}
            self.assertIn("stage-passed-wrong-acceptance-authority", codes)

    def test_gate_must_depend_on_acceptance_and_record_all_seven_questions(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            stage = self._valid_passed(tickets)
            gate = tickets / "done" / "T-9200.md"
            text = gate.read_text().replace("depends_on: [T-9100]", "depends_on: []")
            text = text.replace("user ruling required at goal/relationship level", "question omitted")
            gate.write_text(text)
            codes = {f.code for f in program.lint_stage_evidence((stage,), tickets)}
            self.assertIn("stage-passed-gate-missing-acceptance-dependency", codes)
            self.assertIn("stage-passed-gate-missing-seven-question-record", codes)


class EvaluationAndDedup(unittest.TestCase):
    def _inputs(self, resolved: frozenset[str] = frozenset()) -> program.EvaluationInputs:
        return program.EvaluationInputs(
            now=dt.datetime(2026, 8, 25, tzinfo=dt.timezone.utc),
            resolved_ticket_ids=resolved,
            instrumented_metrics={},
            metric_evidence_refs={},
            weekly_activity_ref="observable:weekly/2026-W35",
        )

    def test_fuzzy_proposal_is_surface_only(self) -> None:
        obligation = _obligation(trigger_kind="proposal")
        self.assertEqual(program.due_obligations((obligation,), self._inputs()), ())

    def test_milestone_requires_new_resolved_ticket_evidence(self) -> None:
        obligation = _obligation(
            trigger_kind="milestone",
            operational_predicate="ticket-done:T-9001",
        )
        due = program.due_obligations((obligation,), self._inputs(frozenset({"T-9001"})))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].evidence_ref, "ticket-done:T-9001")
        old = dataclasses.replace(obligation, evidence_refs=("ticket-done:T-9001",))
        self.assertEqual(program.due_obligations((old,), self._inputs(frozenset({"T-9001"}))), ())

    def test_review_ticket_args_preserve_routing_labels_and_dependency(self) -> None:
        obligation = _obligation(
            trigger_kind="milestone", operational_predicate="ticket-done:T-9001"
        )
        due = program.due_obligations((obligation,), self._inputs(frozenset({"T-9001"})))[0]
        args = program.build_review_ticket_args(obligation, due)
        self.assertEqual(args[:5], ("file", "--requester", "recovery", "--target", "orchestrator"))
        self.assertIn("adr-reeval,reeval:ORG-061:R1,adr,process", args)
        self.assertEqual(args[args.index("--depends-on") + 1], "T-9001")

    def test_threshold_needs_crossing_and_metric_evidence(self) -> None:
        obligation = _obligation(
            trigger_kind="threshold", operational_predicate="metric:latency_ms >= 500"
        )
        base = self._inputs()
        crossed_without_receipt = dataclasses.replace(
            base, instrumented_metrics={"latency_ms": 600}
        )
        self.assertEqual(program.due_obligations((obligation,), crossed_without_receipt), ())
        crossed = dataclasses.replace(
            crossed_without_receipt,
            metric_evidence_refs={"latency_ms": "observable:metric/latency/2026-W35"},
        )
        due = program.due_obligations((obligation,), crossed)
        self.assertEqual([item.evidence_ref for item in due], ["observable:metric/latency/2026-W35"])

    def test_calendar_needs_due_date_and_weekly_evidence(self) -> None:
        obligation = _obligation(
            trigger_kind="calendar",
            operational_predicate="next_check",
            next_check="2026-08-24T00:00:00Z",
        )
        no_evidence = dataclasses.replace(self._inputs(), weekly_activity_ref=None)
        self.assertEqual(program.due_obligations((obligation,), no_evidence), ())
        due = program.due_obligations((obligation,), self._inputs())
        self.assertEqual(len(due), 1)
        args = program.build_review_ticket_args(obligation, due[0])
        self.assertEqual(args[args.index("--not-before") + 1], "2026-08-24T00:00:00Z")
        self.assertEqual(
            args[args.index("--not-before-anchor") + 1],
            "event:cadence:2026-08-24T00:00:00Z",
        )

    def test_open_ticket_deduplicates(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            labels = ("adr-reeval", "reeval:ORG-061:R1")
            _ticket(tickets, "T-9001", state="active", labels=labels)
            self.assertEqual(program.existing_review_ticket("ORG-061:R1", tickets), "T-9001")

    def test_evaluator_proposes_at_most_one_ticket(self) -> None:
        with TemporaryDirectory() as td:
            tickets = _tickets(Path(td))
            obligations = tuple(
                _obligation(
                    _source(f"ORG-061:R{n}"),
                    trigger_kind="milestone",
                    operational_predicate=f"ticket-done:T-900{n}",
                )
                for n in (1, 2)
            )
            report = program.evaluate_indexes(
                program.ProgramIndexes((), obligations),
                self._inputs(frozenset({"T-9001", "T-9002"})),
                tickets,
                max_new=1,
            )
            self.assertEqual(report.due_obligation_ids, ("ORG-061:R1", "ORG-061:R2"))
            self.assertEqual(len(report.proposed_ticket_args), 1)


class TicketRootDiscovery(unittest.TestCase):
    def test_real_ticket_discovery_paths_ignore_root_indexes(self) -> None:
        from tools import ticket, ticket_grooming, ticket_lint

        with TemporaryDirectory() as td:
            root = Path(td)
            tickets = _tickets(root)
            _ticket(tickets, "T-9001", state="inbox")
            (tickets / "grand-project-stage-evidence.yaml").write_text("schema_version: 1\nstages: []\n")
            (tickets / "grand-project-reevaluation-obligations.yaml").write_text(
                "schema_version: 1\nobligations: []\n"
            )
            graph = ticket._build_depends_on_graph(tickets)
            linted = ticket_lint.scan_tickets(tickets, repo_root=root)
            grooming = ticket_grooming.find_ticket_integrity_issues(tickets)
            self.assertEqual(set(graph), {"T-9001"})
            self.assertEqual([result.tid for result in linted], ["T-9001"])
            self.assertEqual(grooming, [])


if __name__ == "__main__":
    unittest.main()
