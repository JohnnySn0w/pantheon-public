#!/usr/bin/env python3
"""Tests for tools/adr_reeval_walk.py (T-2527).

Run: python3 -m unittest tools.test_adr_reeval_walk -v
"""

from __future__ import annotations

import datetime as dt
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import adr_reeval_walk as W  # noqa: E402


def _write(d: Path, name: str, text: str) -> Path:
    p = d / name
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class ParseStructured(unittest.TestCase):
    def test_structured_types_and_fires_default(self):
        with TemporaryDirectory() as td:
            p = _write(Path(td), "ORG-099-x.md", """\
                ---
                id: ORG-099
                re_evaluate:
                  - {id: R1, type: calendar, when: "2026-01-01"}
                  - {id: R2, type: event, when: "second project online"}
                  - {id: R3, type: event, when: "resolves:T-1234"}
                  - {id: R4, type: metric, when: "fp_count >= 500"}
                ---
                # body
                """)
            ts = {t.trigger_id: t for t in W.parse_adr(p)}
        # calendar defaults to auto-ticket (deterministic)
        self.assertEqual(ts["ORG-099:R1"].ttype, "calendar")
        self.assertEqual(ts["ORG-099:R1"].fires, "auto-ticket")
        # plain event -> surface (not deterministic)
        self.assertEqual(ts["ORG-099:R2"].fires, "surface")
        # event with resolves:T-NNNN is reclassified to ticket -> deterministic
        self.assertEqual(ts["ORG-099:R3"].ttype, "ticket")
        self.assertEqual(ts["ORG-099:R3"].fires, "auto-ticket")
        # metric -> auto-ticket type but only fires if instrumented (tested below)
        self.assertEqual(ts["ORG-099:R4"].ttype, "metric")

    def test_auto_ticket_on_nondeterministic_is_forced_to_surface(self):
        with TemporaryDirectory() as td:
            p = _write(Path(td), "ORG-099-x.md", """\
                ---
                id: ORG-099
                re_evaluate:
                  - {id: R1, type: event, when: "vibes", fires: auto-ticket}
                ---
                """)
            t = W.parse_adr(p)[0]
        # a non-deterministic type may never auto-file, even if it asks to
        self.assertEqual(t.fires, "surface")


class ParseProse(unittest.TestCase):
    def test_bold_id_bullets(self):
        with TemporaryDirectory() as td:
            p = _write(Path(td), "ORG-053-x.md", """\
                ---
                id: ORG-053
                ---
                ## Re-evaluate when
                - **ORG-053:R1** — the first Bridge runs;
                - **ORG-053:R2** — a clean seed restores a persona.
                """)
            ts = {t.trigger_id: t for t in W.parse_adr(p)}
        self.assertIn("ORG-053:R1", ts)
        self.assertIn("ORG-053:R2", ts)
        self.assertEqual(ts["ORG-053:R1"].source, "prose")
        self.assertEqual(ts["ORG-053:R1"].fires, "surface")

    def test_plain_bullets_get_synthesized_ids(self):
        with TemporaryDirectory() as td:
            p = _write(Path(td), "ORG-013-x.md", """\
                ---
                id: ORG-013
                ---
                ## Re-evaluate when
                - Manual-block usage emerges as a regular pattern.
                - Cross-repo depends_on becomes a thing.
                """)
            ts = {t.trigger_id: t for t in W.parse_adr(p)}
        self.assertEqual(set(ts), {"ORG-013:S1", "ORG-013:S2"})
        self.assertTrue(ts["ORG-013:S1"].when.startswith("Manual-block"))

    def test_revisit_and_sunset_section_headers(self):
        for header in ("## Revisit when", "## Sunset"):
            with TemporaryDirectory() as td:
                p = _write(Path(td), "ORG-020-x.md", f"""\
                    ---
                    id: ORG-020
                    ---
                    {header}
                    - some real trigger condition here.
                    """)
                ts = W.parse_adr(p)
            self.assertEqual(len(ts), 1, f"header {header!r} should be found")

    def test_section_with_no_bullets_yields_nothing(self):
        # ORG-010-style: the section is a schema DESCRIPTION, not triggers.
        with TemporaryDirectory() as td:
            p = _write(Path(td), "ORG-010-x.md", """\
                ---
                id: ORG-010
                ---
                ## Re-evaluate when
                The trigger (event, date, or condition) that prompts a review.

                ## Next section
                - this bullet is in a different section and must be ignored.
                """)
            ts = W.parse_adr(p)
        self.assertEqual(ts, [])

    def test_no_reeval_section_yields_nothing(self):
        # inline "revisit when" prose is deliberately NOT surfaced (precision).
        with TemporaryDirectory() as td:
            p = _write(Path(td), "ORG-001-x.md", """\
                ---
                id: ORG-001
                ---
                v0 assumes single ledger; revisit when the second project comes online.
                """)
            self.assertEqual(W.parse_adr(p), [])

    def test_structured_wins_over_prose_same_id(self):
        with TemporaryDirectory() as td:
            p = _write(Path(td), "ORG-062-x.md", """\
                ---
                id: ORG-062
                re_evaluate:
                  - {id: R1, type: event, when: "PMB-1 built", fires: surface}
                ---
                ## Re-evaluate when
                - **ORG-062:R1** — PMB-1 is first built and run (prose version).
                """)
            ts = W.parse_adr(p)
        self.assertEqual(len(ts), 1)
        self.assertEqual(ts[0].source, "structured")


# ---------------------------------------------------------------------------
# Deterministic evaluation
# ---------------------------------------------------------------------------

class TriggerFired(unittest.TestCase):
    def _t(self, ttype, when, fires="auto-ticket"):
        return W.Trigger("ORG-099", "ORG-099:R1", ttype, when, fires, "structured", when)

    def test_calendar_past_due_fires(self):
        t = self._t("calendar", "2026-01-01")
        self.assertTrue(W.trigger_fired(t, now=dt.date(2026, 8, 3), resolved=set()))

    def test_calendar_future_does_not_fire(self):
        t = self._t("calendar", "2027-01-01")
        self.assertFalse(W.trigger_fired(t, now=dt.date(2026, 8, 3), resolved=set()))

    def test_ticket_resolved_fires(self):
        t = self._t("ticket", "resolves:T-1234")
        self.assertTrue(W.trigger_fired(t, now=dt.date(2026, 8, 3), resolved={"T-1234"}))

    def test_ticket_unresolved_does_not_fire(self):
        t = self._t("ticket", "resolves:T-1234")
        self.assertFalse(W.trigger_fired(t, now=dt.date(2026, 8, 3), resolved={"T-9999"}))

    def test_metric_crossed_fires_only_when_instrumented(self):
        t = self._t("metric", "fp_count >= 500")
        # not instrumented -> never fires (surface, not auto-fire)
        self.assertFalse(W.trigger_fired(t, now=dt.date(2026, 8, 3), resolved=set()))
        self.assertFalse(W.trigger_fired(t, now=dt.date(2026, 8, 3), resolved=set(), metrics={"fp_count": 200}))
        self.assertTrue(W.trigger_fired(t, now=dt.date(2026, 8, 3), resolved=set(), metrics={"fp_count": 500}))

    def test_surface_trigger_never_fires(self):
        t = self._t("calendar", "2026-01-01", fires="surface")
        self.assertFalse(W.trigger_fired(t, now=dt.date(2026, 8, 3), resolved=set()))

    def test_event_never_fires_deterministically(self):
        t = self._t("event", "second project online")
        self.assertFalse(W.trigger_fired(t, now=dt.date(2026, 8, 3), resolved=set()))


# ---------------------------------------------------------------------------
# Dedup (the load-bearing correctness point)
# ---------------------------------------------------------------------------

class Dedup(unittest.TestCase):
    def _make_tickets_root(self, td: Path, *, with_open_key: str | None = None) -> Path:
        root = td / "tickets"
        for sub in ("inbox", "active", "done"):
            (root / sub).mkdir(parents=True)
        if with_open_key is not None:
            _write(root / "inbox", "T-9001.md", f"""\
                ---
                id: T-9001
                created: 2026-08-03T00:00:00Z
                requester: recovery
                target: orchestrator
                state: inbox
                labels: [adr-reeval, "reeval:{with_open_key}", adr, process]
                ---
                # Re-evaluate

                ## Ask
                x

                ## Context

                ## Transition log
                - filed
                """)
        return root

    def test_finds_open_ticket_by_key(self):
        with TemporaryDirectory() as td:
            root = self._make_tickets_root(Path(td), with_open_key="ORG-062:R1")
            self.assertEqual(W.existing_open_reeval_ticket("ORG-062:R1", root), "T-9001")

    def test_absent_key_returns_none(self):
        with TemporaryDirectory() as td:
            root = self._make_tickets_root(Path(td), with_open_key="ORG-062:R1")
            self.assertIsNone(W.existing_open_reeval_ticket("ORG-062:R2", root))

    def test_done_ticket_does_not_dedup(self):
        # a RESOLVED re-eval ticket must not suppress a re-fire (only open ones dedup)
        with TemporaryDirectory() as td:
            root = self._make_tickets_root(Path(td))
            _write(root / "done", "T-9002.md", """\
                ---
                id: T-9002
                created: 2026-08-03T00:00:00Z
                requester: recovery
                target: orchestrator
                state: done
                labels: [adr-reeval, "reeval:ORG-062:R1"]
                ---
                # x
                ## Ask
                x
                ## Context
                ## Transition log
                - done
                """)
            self.assertIsNone(W.existing_open_reeval_ticket("ORG-062:R1", root))


# ---------------------------------------------------------------------------
# run_walk orchestration
# ---------------------------------------------------------------------------

class RunWalk(unittest.TestCase):
    def _adrs(self, d: Path):
        _write(d, "ORG-100-cal.md", """\
            ---
            id: ORG-100
            re_evaluate:
              - {id: R1, type: calendar, when: "2026-01-01", fires: auto-ticket}
            ---
            """)
        _write(d, "ORG-101-surface.md", """\
            ---
            id: ORG-101
            ---
            ## Re-evaluate when
            - a fuzzy human-judged condition about scaling.
            """)

    def test_fired_deterministic_autofiles_surface_listed(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            adrs = tdp / "adr"; adrs.mkdir()
            self._adrs(adrs)
            root = tdp / "tickets"
            for sub in ("inbox", "active", "done"):
                (root / sub).mkdir(parents=True)
            with mock.patch.object(W, "file_reeval_ticket", return_value="T-1234") as filer:
                res = W.run_walk(now=dt.date(2026, 8, 3), adr_dir=adrs,
                                 tickets_root=root, week_activity="scaling happened")
            filer.assert_called_once()
            self.assertEqual(res.filed, ["T-1234"])
            self.assertEqual(res.fired, ["ORG-100:R1"])
            self.assertEqual(res.deduped, [])
            self.assertIn("ORG-101", res.watch_section)
            self.assertIn("scaling", res.watch_section.lower())
            self.assertNotIn("ORG-100", res.watch_section)  # deterministic not surfaced

    def test_dedup_skips_autofile_on_rerun(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            adrs = tdp / "adr"; adrs.mkdir()
            self._adrs(adrs)
            root = tdp / "tickets"
            for sub in ("inbox", "active", "done"):
                (root / sub).mkdir(parents=True)
            # pre-existing open re-eval ticket for the same trigger
            _write(root / "active", "T-8000.md", """\
                ---
                id: T-8000
                created: 2026-08-03T00:00:00Z
                requester: recovery
                target: orchestrator
                state: active
                labels: [adr-reeval, "reeval:ORG-100:R1"]
                ---
                # x
                ## Ask
                x
                ## Context
                ## Transition log
                - claimed
                """)
            with mock.patch.object(W, "file_reeval_ticket") as filer:
                res = W.run_walk(now=dt.date(2026, 8, 3), adr_dir=adrs, tickets_root=root)
            filer.assert_not_called()
            self.assertEqual(res.filed, [])
            self.assertEqual(res.deduped, ["ORG-100:R1"])

    def _done_reeval(self, root: Path, tid: str, created: str) -> None:
        _write(root / "done", f"{tid}.md", f"""\
            ---
            id: {tid}
            created: {created}
            requester: recovery
            target: orchestrator
            state: done
            labels: [adr-reeval, "reeval:ORG-100:R1"]
            ---
            # x
            ## Ask
            x
            ## Context
            ## Transition log
            - resolved
            """)

    def test_resolved_past_due_calendar_trigger_does_not_refile(self):
        # T-4827: a calendar date stays past-due forever, so a DONE review
        # ticket filed on/after the trigger date means this firing is handled.
        with TemporaryDirectory() as td:
            tdp = Path(td)
            adrs = tdp / "adr"; adrs.mkdir()
            self._adrs(adrs)
            root = tdp / "tickets"
            for sub in ("inbox", "active", "done"):
                (root / sub).mkdir(parents=True)
            self._done_reeval(root, "T-8001", "2026-01-01T09:00:00Z")
            with mock.patch.object(W, "file_reeval_ticket") as filer:
                res = W.run_walk(now=dt.date(2026, 8, 3), adr_dir=adrs, tickets_root=root)
            filer.assert_not_called()
            self.assertEqual(res.filed, [])
            self.assertEqual(res.deduped, ["ORG-100:R1"])

    def test_done_review_from_before_the_trigger_date_does_not_suppress(self):
        # A review resolved BEFORE this trigger's date answered an earlier
        # firing (e.g. a moved date), not this one: the walk files.
        with TemporaryDirectory() as td:
            tdp = Path(td)
            adrs = tdp / "adr"; adrs.mkdir()
            self._adrs(adrs)
            root = tdp / "tickets"
            for sub in ("inbox", "active", "done"):
                (root / sub).mkdir(parents=True)
            self._done_reeval(root, "T-8002", "2025-12-31T23:59:59Z")
            with mock.patch.object(W, "file_reeval_ticket", return_value="T-1235") as filer:
                res = W.run_walk(now=dt.date(2026, 8, 3), adr_dir=adrs, tickets_root=root)
            filer.assert_called_once()
            self.assertEqual(res.filed, ["T-1235"])

    def test_dry_run_never_files(self):
        with TemporaryDirectory() as td:
            tdp = Path(td)
            adrs = tdp / "adr"; adrs.mkdir()
            self._adrs(adrs)
            root = tdp / "tickets"
            for sub in ("inbox", "active", "done"):
                (root / sub).mkdir(parents=True)
            with mock.patch.object(W, "file_reeval_ticket") as filer:
                res = W.run_walk(now=dt.date(2026, 8, 3), adr_dir=adrs,
                                 tickets_root=root, dry_run=True)
            filer.assert_not_called()
            self.assertTrue(res.filed and res.filed[0].startswith("(dry-run)"))

    def test_owned_adapter_noops_when_index_pair_is_incomplete(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            adrs = root / "adr"; adrs.mkdir()
            tickets = root / "tickets"
            for sub in ("inbox", "active", "done"):
                (tickets / sub).mkdir(parents=True)
            (tickets / "grand-project-stage-evidence.yaml").write_text(
                "schema_version: 1\nstages: []\n"
            )
            with mock.patch.object(W, "file_owned_review_ticket") as filer:
                result = W.run_walk(now=dt.date(2026, 8, 25), adr_dir=adrs, tickets_root=tickets)
            filer.assert_not_called()
            self.assertEqual(result.owned_status, "incomplete")
            self.assertEqual(result.owned_filed, ())

    def test_real_owned_indexes_load_surface_51_and_file_nothing_without_new_evidence(self):
        root = Path(__file__).resolve().parent.parent
        with TemporaryDirectory() as td:
            tickets = Path(td) / "tickets"
            for sub in ("inbox", "active", "done"):
                (tickets / sub).mkdir(parents=True)
            with mock.patch.object(W, "file_owned_review_ticket") as filer:
                result = W.run_walk(
                    now=dt.date(2026, 8, 25),
                    adr_dir=root / "architecture" / "adr",
                    tickets_root=tickets,
                    stage_index=root / "tickets" / "grand-project-stage-evidence.yaml",
                    obligation_index=root / "tickets" / "grand-project-reevaluation-obligations.yaml",
                )
            filer.assert_not_called()
            self.assertEqual(result.owned_status, "loaded")
            self.assertEqual(result.owned_due, ())
            self.assertEqual(result.owned_filed, ())
            owned_section = result.watch_section.split(
                "## Grand Project owned re-evaluation watch", 1
            )[1]
            self.assertEqual(owned_section.count("| ORG-0"), 51)

    def test_clean_owned_pair_suppresses_same_id_from_legacy_autofile_lane(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            adrs = root / "adr"; adrs.mkdir()
            _write(adrs, "ORG-053-owned.md", """\
                ---
                id: ORG-053
                re_evaluate:
                  - {id: R1, type: calendar, when: "2026-01-01", fires: auto-ticket}
                ---
                """)
            tickets = root / "tickets"
            for sub in ("inbox", "active", "done"):
                (tickets / sub).mkdir(parents=True)
            stage_path = tickets / "grand-project-stage-evidence.yaml"
            obligation_path = tickets / "grand-project-reevaluation-obligations.yaml"
            stage_path.write_text("paired")
            obligation_path.write_text("paired")
            obligation = W.grand_program.ReevaluationObligation(
                obligation_id="ORG-053:R1",
                source_ref="adr:ORG-053#R1",
                source_text_sha256="sha256:" + "0" * 64,
                owner_ref="role:steward",
                review_target="orchestrator",
                trigger_kind="calendar",
                operational_predicate="next_check",
                observable_source="observable:test",
                state="armed",
                armed_ticket=None,
                last_evaluated=None,
                next_check="2026-01-01T00:00:00Z",
                evidence_refs=(),
                outcome_ref=None,
            )
            report = W.grand_program.EvaluationReport((), (), (), (), ())
            with (
                mock.patch.object(W.grand_program, "load_stage_index", return_value=()),
                mock.patch.object(W.grand_program, "load_obligation_index", return_value=(obligation,)),
                mock.patch.object(W.grand_program, "scan_source_obligations", return_value=()),
                mock.patch.object(W.grand_program, "lint_obligation_completeness", return_value=()),
                mock.patch.object(W.grand_program, "lint_stage_evidence", return_value=()),
                mock.patch.object(W.grand_program, "evaluate_indexes", return_value=report),
                mock.patch.object(W, "file_reeval_ticket") as legacy_filer,
            ):
                result = W.run_walk(
                    now=dt.date(2026, 8, 25), adr_dir=adrs, tickets_root=tickets,
                    stage_index=stage_path, obligation_index=obligation_path,
                )
            legacy_filer.assert_not_called()
            self.assertEqual(result.fired, [])


class WatchSection(unittest.TestCase):
    def _t(self, tid, when):
        return W.Trigger("ORG-101", tid, "prose", when, "surface", "prose", when)

    def test_empty(self):
        self.assertIn("No prose/event", W.build_watch_section([]))

    def test_keyword_flag_intersects_activity(self):
        t = self._t("ORG-101:S1", "cascade firing rate becomes load-significant")
        flagged = W.build_watch_section([t], week_activity="the cascade path was touched")
        self.assertIn("⚑", flagged)
        unflagged = W.build_watch_section([t], week_activity="unrelated work")
        # the row exists but without the flag
        self.assertIn("ORG-101:S1", unflagged)
        self.assertNotIn("⚑", unflagged.split("|---")[-1])


# ---------------------------------------------------------------------------
# Real-repo smoke: the walk parses the live adr/ without crashing
# ---------------------------------------------------------------------------

class RealRepoSmoke(unittest.TestCase):
    def test_live_adrs_parse_and_org062_structured(self):
        triggers = W.scan_adrs()  # real adr/ dir
        by_id = {t.trigger_id: t for t in triggers}
        # ORG-062 was seeded with structured R1-R4
        for r in ("R1", "R2", "R3", "R4"):
            self.assertIn(f"ORG-062:{r}", by_id)
            self.assertEqual(by_id[f"ORG-062:{r}"].source, "structured")

    def test_every_adr_frontmatter_parses_as_yaml(self):
        """T-2591 lint: every ADR's frontmatter must be valid YAML. _load_yaml
        returns {} on a parse failure, so a malformed title:/status: (an unquoted
        value containing a colon) silently makes that ADR's frontmatter — id,
        status, any structured re_evaluate: key — invisible to the walk. This is
        the commit-time guard so that can't recur unnoticed (13 ADRs had drifted
        into this state before it was caught). Shown to fail on a malformed ADR."""
        import yaml
        broken = []
        for path in sorted(W.ADR_DIR.glob("*.md")):
            if path.name.upper().startswith("README"):
                continue
            fm, _ = W._split_frontmatter(path.read_text(encoding="utf-8"))
            if not fm.strip():
                continue  # no frontmatter is legitimate; unparseable is not
            try:
                yaml.safe_load(fm)
            except Exception as exc:  # noqa: BLE001 — any YAML error is a finding
                broken.append(f"{path.name}: {type(exc).__name__}")
        self.assertEqual(
            broken, [],
            "ADR(s) with unparseable frontmatter (invisible to every frontmatter "
            "tool). Quote any title:/status: value containing a colon:\n  "
            + "\n  ".join(broken))


# ---------------------------------------------------------------------------
# Policy / SOP roots (T-4556)
# ---------------------------------------------------------------------------

def _gov_tree(td: str, files: dict[str, str]) -> Path:
    root = Path(td)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


class GovernanceDocs(unittest.TestCase):
    def test_header_line_split_on_semicolon(self):
        with TemporaryDirectory() as td:
            root = _gov_tree(td, {"rulebook/policies/alpha.md": """\
                # Alpha

                **Status:** Ratified
                **Re-evaluate when:** a new class appears; a credential is exposed; a seat is added.

                ## Body
                text
                """})
            ts = W.scan_governance_docs(root)
        self.assertEqual([t.trigger_id for t in ts],
                         ["policy:alpha:H1", "policy:alpha:H2", "policy:alpha:H3"])
        self.assertEqual(ts[0].when, "a new class appears")
        self.assertEqual(ts[2].when, "a seat is added")
        self.assertTrue(all(t.fires == "surface" and t.ttype == "prose" for t in ts))
        self.assertEqual(ts[0].adr_id, "policy:alpha")

    def test_section_form_bullets_sop(self):
        with TemporaryDirectory() as td:
            root = _gov_tree(td, {"sop/standard/beta.md": """\
                # Beta
                ## Re-evaluate when

                - first thing changes
                - second thing changes

                ## Related
                - not a trigger
                """})
            ts = W.scan_governance_docs(root)
        self.assertEqual([t.trigger_id for t in ts],
                         ["sop:standard/beta:S1", "sop:standard/beta:S2"])
        self.assertEqual(ts[1].when, "second thing changes")
        self.assertEqual(ts[0].adr_id, "sop:standard/beta")

    def test_bulletless_section_yields_plain_lines(self):
        with TemporaryDirectory() as td:
            root = _gov_tree(td, {"rulebook/policies/gamma.md": """\
                # Gamma
                ## Re-evaluate when

                A new project is added.
                Escalation rate deviates.

                ## Next
                """})
            ts = W.scan_governance_docs(root)
        self.assertEqual([t.when for t in ts],
                         ["A new project is added.", "Escalation rate deviates."])

    def test_metadata_header_dropped_when_section_exists(self):
        with TemporaryDirectory() as td:
            root = _gov_tree(td, {"rulebook/policies/delta.md": """\
                # Delta
                **Re-evaluate when:** x changes; y changes.

                ## Re-evaluate when
                - x changes
                - y changes
                """})
            ts = W.scan_governance_docs(root)
        self.assertEqual([t.trigger_id for t in ts], ["policy:delta:S1", "policy:delta:S2"])

    def test_pointer_header_plus_midbody_plus_section(self):
        with TemporaryDirectory() as td:
            root = _gov_tree(td, {"rulebook/policies/eps.md": """\
                # Eps
                **Re-evaluate when:** see "Re-evaluate when" section below - many things.

                ## Calibration
                **Re-evaluate when:** reversals recur 3x; the opposite error appears.

                ## Re-evaluate when
                - sec one
                - sec two
                """})
            ts = W.scan_governance_docs(root)
        ids = {t.trigger_id: t.when for t in ts}
        self.assertEqual(ids, {
            "policy:eps:S1": "sec one", "policy:eps:S2": "sec two",
            "policy:eps:H1": "reversals recur 3x",
            "policy:eps:H2": "the opposite error appears",
        })

    def test_pointer_header_alone_yields_nothing(self):
        with TemporaryDirectory() as td:
            root = _gov_tree(td, {"sop/standard/zeta.md": """\
                # Zeta
                **Re-evaluate when:** see "Re-evaluate when" section below.
                """})
            self.assertEqual(W.scan_governance_docs(root), [])

    def test_non_matches_ignored_and_readme_skipped(self):
        with TemporaryDirectory() as td:
            root = _gov_tree(td, {
                "sop/standard/eta.md": """\
                    # Eta
                    **Re-evaluated 2026-09-27 (T-1):** the old trigger fired.
                    Per close: Re-evaluate trigger: 30 days post-B8.
                    Re-evaluate at <trigger>: if <observation>.
                    """,
                "sop/standard/README.md": "# Idx\n**Re-evaluate when:** a; b.\n",
                "rulebook/policies/README.md": "# Idx\n**Re-evaluate when:** a; b.\n",
            })
            self.assertEqual(W.scan_governance_docs(root), [])

    def test_ids_unique_across_same_stem_and_remediation_recursion(self):
        with TemporaryDirectory() as td:
            root = _gov_tree(td, {
                "rulebook/policies/same.md": "# P\n**Re-evaluate when:** one.\n",
                "sop/standard/same.md": "# S\n**Re-evaluate when:** one.\n",
                "sop/remediation/same.md": "# R\n**Re-evaluate when:** one.\n",
            })
            ts = W.scan_governance_docs(root)
        ids = [t.trigger_id for t in ts]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {"policy:same:H1", "sop:standard/same:H1",
                                    "sop:remediation/same:H1"})

    def test_adr_scan_unchanged_by_new_roots(self):
        """ADR invariance: scan_adrs ignores policy/SOP roots and its output on
        a fixture ADR dir is identical whether or not governance docs exist."""
        with TemporaryDirectory() as td:
            root = _gov_tree(td, {
                "architecture/adr/ORG-900-x.md": """\
                    ---
                    id: ORG-900
                    ---
                    # x
                    **Re-evaluate when:** header line must stay unparsed for ADRs; b.
                    ## Re-evaluate when
                    - **ORG-900:R1** - one
                    - plain
                    """,
            })
            before = W.scan_adrs(root / "architecture" / "adr")
            _gov_tree(td, {"rulebook/policies/p.md": "# P\n**Re-evaluate when:** q.\n"})
            after = W.scan_adrs(root / "architecture" / "adr")
        self.assertEqual(before, after)
        self.assertEqual([t.trigger_id for t in before], ["ORG-900:R1", "ORG-900:S1"])

    def test_run_walk_surfaces_governance_triggers_without_filing(self):
        with TemporaryDirectory() as td:
            root = _gov_tree(td, {
                "architecture/adr/ORG-901-x.md": "---\nid: ORG-901\n---\n# x\n",
                "rulebook/policies/p.md": "# P\n**Re-evaluate when:** the bridge changes.\n",
            })
            (root / "tickets").mkdir()
            res = W.run_walk(now=dt.date(2026, 8, 3), adr_dir=root / "architecture" / "adr",
                             tickets_root=root / "tickets", governance_root=root,
                             dry_run=True, push=False)
        self.assertEqual(res.surface_count, 1)
        self.assertEqual(res.filed, [])
        self.assertIn("policy:p:H1", res.watch_section)
        self.assertIn("the bridge changes", res.watch_section)

    def test_watch_section_heading_covers_policies(self):
        t = W.Trigger("policy:p", "policy:p:H1", "prose", "x", "surface", "prose", "x")
        self.assertIn("## ADR / policy / SOP re-eval watch", W.build_watch_section([t]))
        self.assertIn("## ADR / policy / SOP re-eval watch", W.build_watch_section([]))

    def test_live_tree_surfaces_known_policy_triggers(self):
        ts = W.scan_governance_docs()
        blob = "\n".join(f"{t.adr_id} {t.when}" for t in ts)
        self.assertIn("last_inbound_at", blob)  # autonomy.md bridge trigger
        self.assertTrue(any(t.adr_id == "policy:access-by-operation-risk" for t in ts))
        self.assertTrue(all(t.fires == "surface" for t in ts))


if __name__ == "__main__":
    unittest.main()
