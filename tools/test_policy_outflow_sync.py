#!/usr/bin/env python3
"""Tests for tools/policy_outflow_sync.py (T-0834)."""

import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

# Bootstrap: same sys.path trick as inbox_consumer.py — pass the canonical smoke
# `python3 -m unittest tools.test_policy_outflow_sync` AND a standalone run from tools/.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import policy_outflow_sync as pos  # noqa: E402


# A realistic `git log --name-status --pretty=format:'C\t%h\t%ad\t%s'` slice,
# newest commit first.
_RAW = "\n".join([
    "C\taaa111\t2026-06-13\tadr(ORG-035): memory portability",
    "A\tadr/ORG-035-memory-portability-per-agent-baseline.md",
    "M\trulebook/directives.md",
    "C\tbbb222\t2026-06-10\tdocs(sop): tighten grooming",
    "M\tsop/standard/grooming.md",
    "M\tadr/ORG-035-memory-portability-per-agent-baseline.md",  # older touch — ignored
    "C\tccc333\t2026-06-09\tremove dead sop",
    "D\tsop/standard/dead.md",
    "C\tddd444\t2026-06-08\tunrelated code",
    "M\ttools/whatever.py",  # non-watched / non-md — ignored
])


class ParseLogTests(unittest.TestCase):
    def test_most_recent_change_per_path(self):
        out = pos.parse_log(_RAW)
        self.assertEqual(out["adr/ORG-035-memory-portability-per-agent-baseline.md"]["date"], "2026-06-13")
        self.assertEqual(out["adr/ORG-035-memory-portability-per-agent-baseline.md"]["status"], "A")
        self.assertEqual(out["rulebook/directives.md"]["hash"], "aaa111")
        self.assertEqual(out["sop/standard/grooming.md"]["date"], "2026-06-10")

    def test_deletion_drops_path(self):
        out = pos.parse_log(_RAW)
        self.assertNotIn("sop/standard/dead.md", out)

    def test_rename_drops_old_keeps_new(self):
        # T-1310: added-then-renamed in-window. Old path must NOT linger as a dead
        # pointer; the new path is the live artifact.
        raw = "\n".join([
            "C\tnn222\t2026-06-13\tadr: renumber ORG-024->ORG-025",
            "R100\tadr/ORG-024-specialist.md\tadr/ORG-025-specialist.md",
            "C\taa111\t2026-06-10\tadr: add ORG-024 specialist",
            "A\tadr/ORG-024-specialist.md",
        ])
        out = pos.parse_log(raw)
        self.assertNotIn("adr/ORG-024-specialist.md", out)   # dead old path gone
        self.assertIn("adr/ORG-025-specialist.md", out)      # live new path kept

    def test_deleted_then_older_add_not_resurrected(self):
        # T-1310: added-then-deleted in-window. The older add commit must not
        # resurrect the since-deleted path via setdefault.
        raw = "\n".join([
            "C\tnn222\t2026-06-13\tremove cross-quarter sweep sop",
            "D\tsop/standard/org-012-cross-quarter-sweep.md",
            "C\taa111\t2026-06-10\tadd cross-quarter sweep sop",
            "A\tsop/standard/org-012-cross-quarter-sweep.md",
        ])
        out = pos.parse_log(raw)
        self.assertNotIn("sop/standard/org-012-cross-quarter-sweep.md", out)

    def test_digest_has_no_dead_link_after_rename(self):
        # End-to-end: parse + render an add-then-rename slice → the digest links the
        # new path, never the dead old one.
        raw = "\n".join([
            "C\tnn222\t2026-06-13\tadr: renumber ORG-024->ORG-025",
            "R100\tarchitecture/adr/ORG-024-specialist.md\tarchitecture/adr/ORG-025-specialist.md",
            "C\taa111\t2026-06-10\tadr: add ORG-024 specialist",
            "A\tarchitecture/adr/ORG-024-specialist.md",
        ])
        out = pos.build_digest(Path("/nonexistent-root"), pos.parse_log(raw), since_days=30)
        self.assertNotIn("ORG-024-specialist.md", out)
        self.assertIn("ORG-025-specialist.md", out)

    def test_non_md_excluded(self):
        out = pos.parse_log(_RAW)
        self.assertNotIn("tools/whatever.py", out)

    def test_readme_index_files_excluded(self):
        raw = "\n".join([
            "C\taaa\t2026-06-13\tspine groom",
            "M\tadr/README.md",
            "M\tadr/ORG-099-real.md",
        ])
        out = pos.parse_log(raw)
        self.assertNotIn("adr/README.md", out)
        self.assertIn("adr/ORG-099-real.md", out)

    def test_empty_log(self):
        self.assertEqual(pos.parse_log(""), {})


class BuildDigestTests(unittest.TestCase):
    def test_empty_changes_message(self):
        with tempfile.TemporaryDirectory() as t:
            out = pos.build_digest(Path(t), {}, since_days=30)
        self.assertIn("No policy artifacts changed", out)
        self.assertIn(pos._GEN_HEADER, out)

    def test_grouped_newest_first_with_links_no_inline(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "architecture" / "adr").mkdir(parents=True)
            (root / "architecture" / "adr" / "ORG-035.md").write_text("# ORG-035 — memory portability\n\nbody here\n")
            changes = {
                "architecture/adr/ORG-035.md": {"hash": "aaa111", "date": "2026-06-13",
                                                "subject": "adopt", "status": "A"},
                "rulebook/directives.md": {"hash": "bbb222", "date": "2026-06-10",
                                           "subject": "tweak", "status": "M"},
            }
            out = pos.build_digest(root, changes, since_days=30)
        self.assertIn("## architecture/adr/", out)
        self.assertIn("## rulebook/", out)
        # Title pulled from the file's first heading; link is relative to notes/.
        self.assertIn("[ORG-035 — memory portability](../architecture/adr/ORG-035.md)", out)
        self.assertIn("new", out)       # status A → "new"
        self.assertIn("changed", out)   # status M → "changed"
        # No artifact body inlined.
        self.assertNotIn("body here", out)

    def test_dir_ordering_is_watched_order(self):
        with tempfile.TemporaryDirectory() as t:
            changes = {
                "sop/x.md": {"hash": "1", "date": "2026-06-13", "subject": "s", "status": "M"},
                "rulebook/y.md": {"hash": "2", "date": "2026-06-13", "subject": "s", "status": "M"},
            }
            out = pos.build_digest(Path(t), changes, since_days=30)
        # rulebook appears before sop (WATCHED order), regardless of dict order.
        self.assertLess(out.index("## rulebook/"), out.index("## sop/"))


class RelLinkTests(unittest.TestCase):
    def test_relative_to_notes(self):
        self.assertEqual(pos._rel_link("adr/ORG-035.md"), "../adr/ORG-035.md")


class TitleOfTests(unittest.TestCase):
    def test_first_heading_else_stem(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "a.md").write_text("\n\n## Second\n# First\n")
            self.assertEqual(pos._title_of(root, "a.md"), "Second")
            self.assertEqual(pos._title_of(root, "missing.md"), "missing")

    def test_skip_yaml_frontmatter_with_hash_comments(self):
        """YAML comment lines inside frontmatter must NOT be treated as the H1."""
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            body = (
                "---\n"
                "aliases: [ORG-076]\n"
                "title: some yaml title\n"
                "# elements: not the heading either — YAML comment inside frontmatter\n"
                "# more yaml comment\n"
                "---\n"
                "# Real Heading Below Frontmatter\n"
                "prose\n"
            )
            (root / "a.md").write_text(body)
            self.assertEqual(pos._title_of(root, "a.md"), "Real Heading Below Frontmatter")

    def test_no_frontmatter_still_works(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "a.md").write_text("prose\n# Only Heading\nmore\n")
            self.assertEqual(pos._title_of(root, "a.md"), "Only Heading")


class StandingContextFilesTests(unittest.TestCase):
    def test_rulebook_globs_all_md(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "rulebook").mkdir()
            (root / "rulebook" / "directives.md").write_text("x")
            (root / "rulebook" / "principles.md").write_text("x")
            (root / "rulebook" / "notes.txt").write_text("x")  # non-.md ignored
            files = pos.standing_context_files(root)
        rels = sorted(f.relative_to(root).as_posix() for f in files)
        self.assertEqual(rels, ["rulebook/directives.md", "rulebook/principles.md"])

    def test_sop_readme_one_level_deep_only(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "sop" / "standard").mkdir(parents=True)
            (root / "sop" / "remediation").mkdir(parents=True)
            (root / "sop" / "standard" / "README.md").write_text("x")
            (root / "sop" / "remediation" / "README.md").write_text("x")
            (root / "sop" / "README.md").write_text("x")  # top-level — excluded
            (root / "sop" / "standard" / "other.md").write_text("x")  # non-README — excluded
            files = pos.standing_context_files(root)
        rels = sorted(f.relative_to(root).as_posix() for f in files)
        self.assertEqual(rels, ["sop/remediation/README.md", "sop/standard/README.md"])

    def test_claude_md_included_only_if_present(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            files = pos.standing_context_files(root)
            self.assertFalse(any(f.name == "CLAUDE.md" for f in files))

            (root / "CLAUDE.md").write_text("x")
            files = pos.standing_context_files(root)
            self.assertTrue(any(f.name == "CLAUDE.md" for f in files))


class SizeWarningsTests(unittest.TestCase):
    def test_below_threshold_no_warn(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "rulebook").mkdir()
            (root / "rulebook" / "directives.md").write_text(
                "x" * (pos.SIZE_WARN_BYTES - 1))
            self.assertEqual(pos.size_warnings(root), [])

    def test_at_threshold_warns(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "rulebook").mkdir()
            (root / "rulebook" / "directives.md").write_text(
                "x" * pos.SIZE_WARN_BYTES)
            warnings = pos.size_warnings(root)
        self.assertEqual(len(warnings), 1)
        self.assertIn(pos.SIZE_WARN_TOKEN, warnings[0])
        self.assertIn("rulebook/directives.md", warnings[0])
        self.assertIn(str(pos.SIZE_WARN_BYTES), warnings[0])

    def test_at_drift_line_warns_over_100_percent(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "rulebook").mkdir()
            (root / "rulebook" / "directives.md").write_text(
                "x" * pos.SIZE_DRIFT_LINE_BYTES)
            warnings = pos.size_warnings(root)
        self.assertEqual(len(warnings), 1)
        self.assertIn("100.0%", warnings[0])

    def test_never_raises_warn_only(self):
        # A warning is a plain string list; nothing here touches git or raises.
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            self.assertEqual(pos.size_warnings(root), [])  # empty tree, no crash


class ExtractAnchorIdsTests(unittest.TestCase):
    def test_msg_underscore_form(self):
        msg_ids, tickets = pos.extract_anchor_ids("Ratified 2026-05-03 by the operator (msg_412)")
        self.assertEqual(msg_ids, {"412"})
        self.assertEqual(tickets, set())

    def test_msg_space_form(self):
        msg_ids, _ = pos.extract_anchor_ids("RATIFIED 2026-07-06 by the operator (T-1584, Telegram msg 4664)")
        self.assertIn("4664", msg_ids)

    def test_tg_form(self):
        msg_ids, _ = pos.extract_anchor_ids("Ratified 2026-06-10 by the operator, Telegram 3096")
        self.assertIn("3096", msg_ids)

    def test_telegram_msg_underscore_form(self):
        msg_ids, _ = pos.extract_anchor_ids("Ratified 2026-05-05 by the operator (Telegram, msg_662 Q1-Q5)")
        self.assertIn("662", msg_ids)

    def test_ticket_ref_form(self):
        _, tickets = pos.extract_anchor_ids("T-1584 ratified by the operator 2026-07-06 (supersedes T-0010/170M)")
        self.assertEqual(tickets, {"T-1584", "T-0010"})

    def test_fuzzy_ref_not_matched(self):
        # ORG-013-shaped approximate reference — deliberately not an anchor.
        msg_ids, tickets = pos.extract_anchor_ids(
            "ratified 2026-06-07 by the operator verdict msg_~1497-window")
        self.assertEqual(msg_ids, set())
        self.assertEqual(tickets, set())

    def test_compound_slash_citation_captures_first_only(self):
        # Documented limitation: "TG 4664/4666" only captures the first id.
        msg_ids, _ = pos.extract_anchor_ids("actuated via TG 4664/4666")
        self.assertEqual(msg_ids, {"4664"})

    def test_left_boundary_excludes_word_internal_match(self):
        # T-1601 [1]: REPORT-1234 must not spuriously extract a T-1234 ticket
        # ref — the T is preceded by a word char (R), not a boundary.
        _, tickets = pos.extract_anchor_ids("See REPORT-1234 for context")
        self.assertEqual(tickets, set())

    def test_left_boundary_still_matches_real_ticket(self):
        # T-1601 [1]: a genuine standalone T-NNNN ref still extracts.
        _, tickets = pos.extract_anchor_ids("See T-1234 for context")
        self.assertEqual(tickets, {"T-1234"})


class FindAuthorityMarkersTests(unittest.TestCase):
    def test_no_hit_without_operator(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "architecture" / "adr").mkdir(parents=True)
            (root / "architecture" / "adr" / "X.md").write_text("Ratified 2026-06-01 (T-9001, msg_9001)\n")
            self.assertEqual(pos.find_authority_markers(root), [])

    def test_no_hit_without_date(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "architecture" / "adr").mkdir(parents=True)
            (root / "architecture" / "adr" / "X.md").write_text("Ratified by the operator (T-9001, msg_9001)\n")
            self.assertEqual(pos.find_authority_markers(root), [])

    def test_bare_frontmatter_tag_ignored_no_date(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "architecture" / "adr").mkdir(parents=True)
            (root / "architecture" / "adr" / "X.md").write_text("ratified_by: operator\n")
            self.assertEqual(pos.find_authority_markers(root), [])

    def test_anchor_less_ratified_claim_skipped(self):
        # Mirrors the real ORG-038 body-paragraph restatement (no anchor
        # repeated) and the real 2026-04-28 founding markers ("(Telegram)").
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "architecture" / "adr").mkdir(parents=True)
            (root / "architecture" / "adr" / "X.md").write_text(
                "**Status:** **Ratified 2026-07-06 by the operator.** Some more prose.\n")
            self.assertEqual(pos.find_authority_markers(root), [])

    def test_predates_floor_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "rulebook").mkdir()
            (root / "rulebook" / "principles.md").write_text(
                "**Status:** Ratified 2026-04-28 by the operator (Telegram).\n")
            self.assertEqual(pos.find_authority_markers(root), [])

    def test_valid_marker_extracted_with_anchors(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "rulebook" / "policies").mkdir(parents=True)
            (root / "rulebook" / "policies" / "cost.md").write_text(
                "line one\n"
                "RATIFIED 2026-07-06 by the operator (T-1584, Telegram msg 4664):\n")
            markers = pos.find_authority_markers(root)
        self.assertEqual(len(markers), 1)
        m = markers[0]
        self.assertEqual(m.rel_path, "rulebook/policies/cost.md")
        self.assertEqual(m.line_no, 2)
        self.assertEqual(m.marker_date, date(2026, 7, 6))
        self.assertEqual(m.msg_ids, frozenset({"4664"}))
        self.assertEqual(m.ticket_refs, frozenset({"T-1584"}))

    def test_deterministic_sort_order(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "architecture" / "adr").mkdir(parents=True)
            (root / "architecture" / "adr" / "B.md").write_text(
                "Ratified 2026-06-01 by the operator (T-9002, msg_9002)\n")
            (root / "architecture" / "adr" / "A.md").write_text(
                "Ratified 2026-06-01 by the operator (T-9001, msg_9001)\n")
            first = pos.find_authority_markers(root)
            second = pos.find_authority_markers(root)
        self.assertEqual(first, second)
        self.assertEqual([m.rel_path for m in first], ["architecture/adr/A.md", "architecture/adr/B.md"])


def _write_ledger_entry(ledger_dir: Path, ts: str, rationale: str, links: list[dict]) -> None:
    # Directory name is arbitrary — iter_ledger_entry_files only requires
    # <dirname>/<dirname>.jsonl; a hash keeps repeated calls in one test from
    # colliding by timestamp-derived name collision.
    seg_dir = ledger_dir / f"seg-{hashlib.sha1(ts.encode()).hexdigest()[:6]}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    shard = seg_dir / f"{seg_dir.name}.jsonl"
    entry = {
        "action": "test_entry", "agent": "orchestrator", "cost_usd": None,
        "id": f"{ts}-test", "justifies_via": ["P4"], "links": links,
        "parent_id": None, "rationale": rationale, "summary": "", "supersedes": None,
        "ts": ts,
    }
    with shard.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


class AuthorityCorroborationWarningsTests(unittest.TestCase):
    def _root_with_marker(self, t, marker_line: str) -> Path:
        root = Path(t)
        (root / "architecture" / "adr").mkdir(parents=True)
        (root / "architecture" / "adr" / "X.md").write_text(marker_line + "\n")
        return root

    def test_marker_with_no_ledger_entry_warns(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._root_with_marker(
                t, "Ratified 2026-06-01 by the operator (T-9001, msg_9001)")
            warnings = pos.authority_corroboration_warnings(root)
        self.assertEqual(len(warnings), 1)
        self.assertIn(pos.AUTHORITY_WARN_TOKEN, warnings[0])
        self.assertIn("T-9001", warnings[0])

    def test_predates_floor_no_warn(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "rulebook").mkdir()
            (root / "rulebook" / "principles.md").write_text(
                "**Status:** Ratified 2026-04-28 by the operator (Telegram).\n")
            warnings = pos.authority_corroboration_warnings(root)
        self.assertEqual(warnings, [])

    def test_ticket_ref_match_outside_window_still_warns(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._root_with_marker(
                t, "Ratified 2026-06-01 by the operator (T-9002, ticket-UI verdict 12:00:00Z)")
            _write_ledger_entry(
                root / "ledger", "2026-06-20T00:00:00Z",
                "mentions T-9002 far outside the window", [{"ref": "T-9002", "type": "ticket"}])
            warnings = pos.authority_corroboration_warnings(root)
        self.assertEqual(len(warnings), 1)

    def test_ticket_ref_match_inside_window_corroborates(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._root_with_marker(
                t, "Ratified 2026-06-01 by the operator (T-9003, ticket-UI verdict 12:00:00Z)")
            _write_ledger_entry(
                root / "ledger", "2026-06-02T00:00:00Z",
                "mentions T-9003 next day", [{"ref": "T-9003", "type": "ticket"}])
            warnings = pos.authority_corroboration_warnings(root)
        self.assertEqual(warnings, [])

    def test_ticket_ref_exact_two_day_boundary_corroborates(self):
        # T-1601 [3]: pins the `<= CORROBORATION_WINDOW_DAYS` comparison at the
        # inclusive edge — exactly 2 days out DOES corroborate.
        with tempfile.TemporaryDirectory() as t:
            root = self._root_with_marker(
                t, "Ratified 2026-06-01 by the operator (T-9007, ticket-UI verdict 12:00:00Z)")
            _write_ledger_entry(
                root / "ledger", "2026-06-03T00:00:00Z",
                "mentions T-9007 exactly 2 days out", [{"ref": "T-9007", "type": "ticket"}])
            warnings = pos.authority_corroboration_warnings(root)
        self.assertEqual(warnings, [])

    def test_ticket_ref_exact_three_day_boundary_does_not_corroborate(self):
        # T-1601 [3]: the day just past the inclusive edge does NOT corroborate —
        # catches a refactor that flips <= to < or widens the window.
        with tempfile.TemporaryDirectory() as t:
            root = self._root_with_marker(
                t, "Ratified 2026-06-01 by the operator (T-9008, ticket-UI verdict 12:00:00Z)")
            _write_ledger_entry(
                root / "ledger", "2026-06-04T00:00:00Z",
                "mentions T-9008 exactly 3 days out", [{"ref": "T-9008", "type": "ticket"}])
            warnings = pos.authority_corroboration_warnings(root)
        self.assertEqual(len(warnings), 1)

    def test_msg_id_match_outside_window_still_corroborates(self):
        # Asymmetry vs ticket-ref: msg ids need no window.
        with tempfile.TemporaryDirectory() as t:
            root = self._root_with_marker(
                t, "Ratified 2026-06-01 by the operator (T-9004, msg_9004)")
            _write_ledger_entry(
                root / "ledger", "2026-06-25T00:00:00Z",
                "actuated per msg_9004 well outside any ticket-ref window",
                [{"ref": "T-9004", "type": "ticket"}])
            warnings = pos.authority_corroboration_warnings(root)
        self.assertEqual(warnings, [])

    def test_never_raises_no_markers_no_ledger(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            self.assertEqual(pos.authority_corroboration_warnings(root), [])

    def test_malformed_ledger_line_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._root_with_marker(
                t, "Ratified 2026-06-01 by the operator (T-9005, msg_9005)")
            seg_dir = root / "ledger" / "2026-W99"
            seg_dir.mkdir(parents=True)
            (seg_dir / "2026-W99.jsonl").write_text("not-json\n{\"ts\": \"bad-date\"}\n")
            warnings = pos.authority_corroboration_warnings(root)
        self.assertEqual(len(warnings), 1)  # no crash; still correctly unwarranted

    def test_deterministic_across_calls(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._root_with_marker(
                t, "Ratified 2026-06-01 by the operator (T-9006, msg_9006)")
            first = pos.authority_corroboration_warnings(root)
            second = pos.authority_corroboration_warnings(root)
        self.assertEqual(first, second)


class AuthorityCorroborationRealPairTests(unittest.TestCase):
    """D11: real captured-state fixtures — the actual live repo, targeted
    assertions only (not "zero warnings repo-wide", which would be flaky
    against unrelated future repo drift)."""

    def test_org038_t1582_real_marker_corroborates(self):
        root = pos.repo_root()
        markers = [
            m for m in pos.find_authority_markers(root)
            if m.rel_path.startswith("architecture/adr/ORG-038") and "T-1582" in m.ticket_refs
        ]
        self.assertTrue(markers, "expected the real ORG-038/T-1582 marker to be found")
        entries = pos._load_ledger_anchor_index(root)
        self.assertTrue(
            pos._marker_corroborated(markers[0], entries),
            "real T-1582 ratification should corroborate via ticket-ref + window",
        )

    def test_cost_thresholds_t1584_real_marker_corroborates(self):
        root = pos.repo_root()
        markers = [
            m for m in pos.find_authority_markers(root)
            if m.rel_path == "rulebook/policies/cost-monitoring-thresholds.md"
            and "4664" in m.msg_ids
        ]
        self.assertTrue(markers, "expected the real T-1584/msg-4664 marker to be found")
        entries = pos._load_ledger_anchor_index(root)
        self.assertTrue(
            pos._marker_corroborated(markers[0], entries),
            "real T-1584 ratification should corroborate via msg-id match",
        )


class MainDryRunTests(unittest.TestCase):
    def test_dry_run_prints_and_writes_nothing(self):
        orig = pos.generate
        pos.generate = lambda root, days: "# Recent policy changes\n\nDIGEST\n"
        try:
            with tempfile.TemporaryDirectory() as t:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = pos.main(["--dry-run", "--root", t])
                self.assertEqual(rc, 0)
                self.assertIn("DIGEST", buf.getvalue())
                # nothing written under the temp root
                self.assertFalse((Path(t) / pos.DIGEST_REL).exists())
        finally:
            pos.generate = orig

    def test_dry_run_emits_size_warn_on_stderr(self):
        # T-1580 pt.2: the WARN fires unconditionally, including --dry-run, and is
        # independent of the digest content/no-op branches.
        orig = pos.generate
        pos.generate = lambda root, days: "# Recent policy changes\n\nDIGEST\n"
        try:
            with tempfile.TemporaryDirectory() as t:
                root = Path(t)
                (root / "rulebook").mkdir()
                (root / "rulebook" / "directives.md").write_text(
                    "x" * pos.SIZE_WARN_BYTES)
                out_buf, err_buf = io.StringIO(), io.StringIO()
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = pos.main(["--dry-run", "--root", t])
                self.assertEqual(rc, 0)
                self.assertIn("DIGEST", out_buf.getvalue())  # normal dry-run behavior intact
                self.assertIn(pos.SIZE_WARN_TOKEN, err_buf.getvalue())
                self.assertIn("rulebook/directives.md", err_buf.getvalue())
        finally:
            pos.generate = orig

    def test_dry_run_emits_authority_warn_on_stderr(self):
        # T-1601 [2]: mirrors test_dry_run_emits_size_warn_on_stderr — pins the
        # AUTHORITY-WARN unconditional-emit-on-stderr invariant at the main()
        # boundary (the asymmetry the T-1589 reviewer flagged vs SIZE-WARN).
        orig = pos.generate
        pos.generate = lambda root, days: "# Recent policy changes\n\nDIGEST\n"
        try:
            with tempfile.TemporaryDirectory() as t:
                root = Path(t)
                (root / "architecture" / "adr").mkdir(parents=True)
                (root / "architecture" / "adr" / "X.md").write_text(
                    "Ratified 2026-06-01 by the operator (T-9010, msg_9010)\n")
                out_buf, err_buf = io.StringIO(), io.StringIO()
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = pos.main(["--dry-run", "--root", t])
                self.assertEqual(rc, 0)
                self.assertIn("DIGEST", out_buf.getvalue())  # normal dry-run behavior intact
                self.assertIn(pos.AUTHORITY_WARN_TOKEN, err_buf.getvalue())
                self.assertIn("T-9010", err_buf.getvalue())
        finally:
            pos.generate = orig

    def test_no_change_is_noop(self):
        digest = "# Recent policy changes\n\nSAME\n"
        orig = pos.generate
        pos.generate = lambda root, days: digest
        try:
            with tempfile.TemporaryDirectory() as t:
                target = Path(t) / pos.DIGEST_REL
                target.parent.mkdir(parents=True)
                target.write_text(digest)
                rc = pos.main(["--root", t])  # current == generated → no git, no write
                self.assertEqual(rc, 0)
        finally:
            pos.generate = orig


if __name__ == "__main__":
    unittest.main()
