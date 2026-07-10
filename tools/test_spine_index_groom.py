"""Tests for tools/spine_index_groom.py — 15 brief-named tests.

Handler tests (1-8), cache + sentinel + idempotence (9-12),
no-op + fail-open (13-14), integration seam (15).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Boot path for direct execution.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.spine_index_groom as sig

def build_spine_fixture(root: Path) -> None:
    """Build a faithful, self-documenting copy of the spine-index-groom fixture
    tree under ``root``.

    This recreates — programmatically — what used to live in the static mirror
    ``tests/fixtures/spine_index_groom/``. Every subdir below corresponds to one
    handler in ``tools/spine_index_groom.py`` and reproduces the exact content
    that handler parses (first paragraphs, module docstrings, frontmatter
    symptom/domain fields, ADR title/status/date lines, dated-subdir names, the
    ledger ``2026.jsonl`` + seeded ``INDEX.md``, skills ``SKILL.md`` frontmatter,
    pinned-rules prose). The handler tests assert against this content, so it
    must stay faithful.

    Tests that MUTATE their fixture (the ledger handler writes ``INDEX.md``) get
    a fresh tree per test via ``_SpineFixtureTestCase.setUp`` — never share one.
    """

    def w(rel: str, text: str) -> None:
        # Dedent the source-indented block, trim framing newlines, and write a
        # single trailing newline — matching the original static files byte-for-
        # byte for the substrings the handlers parse.
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).strip("\n") + "\n")

    # research/ — dated subdirs; research_handler lists each with the first
    # paragraph of its README.md.
    w("research/2026-04-29-survey-alpha/README.md", """
        # Survey alpha

        Alpha survey on orchestration patterns observed in the wild.

        Subsequent paragraphs are ignored by the handler.
    """)
    w("research/2026-05-01-survey-beta/README.md", """
        # Survey beta

        Beta survey on cybernetics lenses applied to org design.

        Subsequent paragraphs are ignored by the handler.
    """)

    # tools_dir/ — scripts; tools_handler reads the module docstring as purpose.
    w("tools_dir/alpha.py", '''
        """Alpha script for testing the tools_handler fixture."""


        def main() -> int:
            return 0


        if __name__ == "__main__":
            raise SystemExit(main())
    ''')
    w("tools_dir/beta.py", '''
        """Beta script for testing the tools_handler fixture."""


        def main() -> int:
            return 0


        if __name__ == "__main__":
            raise SystemExit(main())
    ''')

    # notes/ — flat .md files; notes_handler reads the first paragraph.
    w("notes/alpha.md", """
        # Alpha note

        Alpha note purpose captured as the first paragraph after the title.

        Additional content beyond the first paragraph is ignored by the handler.
    """)
    w("notes/beta.md", """
        # Beta note

        Beta note purpose captured as the first paragraph after the title.

        Additional content beyond the first paragraph is ignored by the handler.
    """)

    # bugs/ — frontmatter symptom + domain; bugs_handler reads both.
    w("bugs/bug-alpha.md", """
        ---
        symptom: Alpha symptom — example failure shape for testing
        domain: python
        ---

        # Alpha bug

        Body content describing the alpha bug class.
    """)
    w("bugs/bug-beta.md", """
        ---
        symptom: Beta symptom — example failure shape for testing
        domain: systemd
        ---

        # Beta bug

        Body content describing the beta bug class.
    """)

    # incidents/ — dated subdirs; incidents_handler reads the general-failure shape.
    w("incidents/2026-04-01-incident-alpha/README.md", """
        # Incident alpha

        ## General failure shape

        Discipline-tier rule violation: worker emitted artifacts contradicting briefed constraints under load.

        ## Chronology

        (Synthetic fixture content; not a real incident.)
    """)
    w("incidents/2026-05-01-incident-beta/README.md", """
        # Incident beta

        ## General failure shape

        First-run teething pattern: tooling-shape and routing-shape gaps that brief prose alone couldn't anticipate.

        ## Chronology

        (Synthetic fixture content; not a real incident.)
    """)

    # watch_reports/ — dated subdirs; watch_reports_handler lists by date.
    w("watch_reports/2026-04-29/topic-alpha.md", """
        # 2026-04-29 — topic alpha

        Synthetic watch report fixture for testing the watch_reports_handler dated-subdir listing.
    """)
    w("watch_reports/2026-05-01/topic-beta.md", """
        # 2026-05-01 — topic beta

        Synthetic watch report fixture for testing the watch_reports_handler dated-subdir listing.
    """)

    # adr/ — frontmatter title/status/date; adr_handler reads all three.
    w("adr/ORG-001-alpha.md", """
        ---
        title: Alpha ADR title for testing
        status: Ratified
        date: 2026-04-01
        ---

        # ORG-001 — Alpha ADR

        Body content describing the alpha ADR decision.
    """)
    w("adr/ORG-002-beta.md", """
        ---
        title: Beta ADR title for testing
        status: Autonomous
        date: 2026-05-01
        ---

        # ORG-002 — Beta ADR

        Body content describing the beta ADR decision.
    """)

    # ledger/ — 2026.jsonl entries + a seeded INDEX.md missing the 2nd entry
    # (ledger_handler must add it). This subdir is MUTATED by its test.
    w("ledger/2026.jsonl", """
        {"id": "2026-01-01T00:00:00Z-001", "ts": "2026-01-01T00:00:00Z", "agent": "test_actor", "action": "synthetic_seed_entry_1", "justifies_via": ["P5"]}
        {"id": "2026-01-02T00:00:00Z-001", "ts": "2026-01-02T00:00:00Z", "agent": "test_actor", "action": "synthetic_seed_entry_2", "justifies_via": ["P5"]}
    """)
    w("ledger/INDEX.md", """
        # Synthetic ledger index — fixture for ledger_handler

        ## 2026

        | ID | Action | Agent | Justifies-via |
        |---|---|---|---|
        | `2026-01-01T00:00:00Z-001` | synthetic_seed_entry_1 | test_actor | P5 |
    """)

    # lessons/ — flat .md (notes_handler); first paragraph is the purpose.
    w("lessons/alpha-lesson.md", """
        # Alpha Lesson

        Alpha lesson purpose for testing.

        ## Grounding

        Test grounding.
    """)
    w("lessons/beta-lesson.md", """
        # Beta Lesson

        Beta lesson purpose for testing.

        ## Grounding

        Test grounding.
    """)

    # rulebook/ — flat .md (notes_handler); top-level principles + directives.
    w("rulebook/principles.md", """
        # Principles

        Alpha principles fixture for testing.

        ## P1

        Test principle one.
    """)
    w("rulebook/directives.md", """
        # Directives

        Beta directives fixture for testing.

        ## D1

        Test directive one.
    """)

    # rulebook_policies/ — flat .md (notes_handler).
    w("rulebook_policies/alpha-policy.md", """
        # Alpha Policy

        Alpha policy purpose for testing.

        ## Policy

        Test policy body.
    """)
    w("rulebook_policies/beta-policy.md", """
        # Beta Policy

        Beta policy purpose for testing.

        ## Policy

        Test policy body.
    """)

    # skills/ — subdirs each with a SKILL.md; skills_handler reads the
    # frontmatter description.
    w("skills/dispatching-parallel-agents/SKILL.md", """
        ---
        name: dispatching-parallel-agents
        description: Use when facing 2+ independent tasks that can run without shared state or sequential dependency.
        ---
    """)
    w("skills/subagent-driven-development/SKILL.md", """
        ---
        name: subagent-driven-development
        description: Use when delegating implementation, review, research, or orchestration work to subagents.
        ---
    """)
    w("skills/verification-before-completion/SKILL.md", """
        ---
        name: verification-before-completion
        description: Use before claiming any work complete, fixed, or passing.
        ---
    """)

    # sop_remediation/ — flat .md (notes_handler).
    w("sop_remediation/alpha-remediation.md", """
        # Alpha Remediation SOP

        Alpha remediation SOP purpose for testing.

        ## Failure shape

        Test failure shape.
    """)
    w("sop_remediation/beta-remediation.md", """
        # Beta Remediation SOP

        Beta remediation SOP purpose for testing.

        ## Failure shape

        Test failure shape.
    """)

    # sop_standard/ — flat .md (notes_handler).
    w("sop_standard/alpha-sop.md", """
        # Alpha Standard SOP

        Alpha standard SOP purpose for testing.

        ## Procedure

        Test procedure steps.
    """)
    w("sop_standard/beta-sop.md", """
        # Beta Standard SOP

        Beta standard SOP purpose for testing.

        ## Procedure

        Test procedure steps.
    """)

    # pinned_rules/ — agent .md files; pinned_rules_handler reads the first
    # paragraph (and skips README.md).
    w("pinned_rules/recovery.md", """
        # Recovery — pinned rules

        Recovery is the recovery Claude instance. These are Recovery's always-loaded rules for operating as claude-fallback.
    """)


class _SpineFixtureTestCase(unittest.TestCase):
    """Base for the handler tests: builds a fresh, faithful spine fixture tree
    into a per-test ``TemporaryDirectory`` exposed as ``self.fixtures``.

    Per-test isolation is required because the ledger handler test MUTATES its
    fixture (writes ``INDEX.md``); a shared read-only tree would not survive.
    """

    def setUp(self) -> None:
        self._fixture_tmp = tempfile.TemporaryDirectory()
        self.fixtures = Path(self._fixture_tmp.name)
        build_spine_fixture(self.fixtures)

    def tearDown(self) -> None:
        self._fixture_tmp.cleanup()


# ---------------------------------------------------------------------------
# Handler tests.
# ---------------------------------------------------------------------------


class TestResearchHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_research_handler_lists_dated_subdirs_with_first_line_description(self):
        rows = sig.research_handler(self.fixtures / "research")
        self.assertEqual(len(rows), 2)
        slugs = [r for r in rows if "2026-04-29-survey-alpha" in r]
        self.assertEqual(len(slugs), 1)
        # First paragraph text from README.md
        self.assertIn("Alpha survey on orchestration patterns", rows[0])
        beta = [r for r in rows if "2026-05-01-survey-beta" in r]
        self.assertEqual(len(beta), 1)
        self.assertIn("Beta survey on cybernetics", beta[0])


class TestToolsHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_tools_handler_lists_scripts_with_purpose_from_module_docstring(self):
        rows = sig.tools_handler(self.fixtures / "tools_dir")
        self.assertEqual(len(rows), 2)
        alpha = [r for r in rows if "alpha.py" in r]
        self.assertEqual(len(alpha), 1)
        self.assertIn("Alpha script for testing", alpha[0])
        beta = [r for r in rows if "beta.py" in r]
        self.assertEqual(len(beta), 1)
        self.assertIn("Beta script for testing", beta[0])


class TestNotesHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_notes_handler_lists_md_files_with_purpose_from_first_paragraph(self):
        rows = sig.notes_handler(self.fixtures / "notes")
        self.assertEqual(len(rows), 2)
        alpha = [r for r in rows if "alpha.md" in r]
        self.assertEqual(len(alpha), 1)
        self.assertIn("Alpha note purpose", alpha[0])
        beta = [r for r in rows if "beta.md" in r]
        self.assertEqual(len(beta), 1)
        self.assertIn("Beta note purpose", beta[0])


class TestBugsHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_bugs_handler_reads_symptom_and_domain_from_frontmatter(self):
        rows = sig.bugs_handler(self.fixtures / "bugs")
        self.assertEqual(len(rows), 2)
        alpha = [r for r in rows if "bug-alpha.md" in r]
        self.assertEqual(len(alpha), 1)
        # Must contain backtick-quoted filename (matching bugs/README.md §Contents table shape)
        self.assertIn("`bug-alpha.md`", alpha[0])
        self.assertIn("Alpha symptom", alpha[0])
        self.assertIn("python", alpha[0])
        beta = [r for r in rows if "bug-beta.md" in r]
        self.assertEqual(len(beta), 1)
        self.assertIn("systemd", beta[0])


class TestIncidentsHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_incidents_handler_lists_dated_subfolders_with_general_failure_shape(self):
        rows = sig.incidents_handler(self.fixtures / "incidents")
        self.assertEqual(len(rows), 2)
        alpha = [r for r in rows if "2026-04-01-incident-alpha" in r]
        self.assertEqual(len(alpha), 1)
        self.assertIn("Discipline-tier rule", alpha[0])
        beta = [r for r in rows if "2026-05-01-incident-beta" in r]
        self.assertEqual(len(beta), 1)
        self.assertIn("First-run teething", beta[0])


class TestWatchReportsHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_watch_reports_handler_lists_dated_subfolders(self):
        rows = sig.watch_reports_handler(self.fixtures / "watch_reports")
        self.assertEqual(len(rows), 2)
        dates = {"2026-04-29", "2026-05-01"}
        for d in dates:
            matching = [r for r in rows if d in r]
            self.assertEqual(len(matching), 1, f"Missing row for date {d}")


class TestAdrHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_adr_handler_lists_files_with_title_status_date(self):
        rows = sig.adr_handler(self.fixtures / "adr")
        self.assertEqual(len(rows), 2)
        alpha = [r for r in rows if "ORG-001-alpha.md" in r]
        self.assertEqual(len(alpha), 1)
        self.assertIn("Alpha ADR title for testing", alpha[0])
        self.assertIn("Ratified", alpha[0])
        self.assertIn("2026-04-01", alpha[0])
        beta = [r for r in rows if "ORG-002-beta.md" in r]
        self.assertEqual(len(beta), 1)
        self.assertIn("Autonomous", beta[0])


class TestLedgerHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_ledger_handler_preserves_existing_index_md_logic(self):
        fixture_ledger = self.fixtures / "ledger"
        index_path = fixture_ledger / "INDEX.md"
        original = index_path.read_text()

        with patch.object(sig, "_ledger_commit_and_push") as mock_commit:
            sig.ledger_handler(fixture_ledger)
            self.assertEqual(mock_commit.call_count, 1)

        after = index_path.read_text()
        # Both entries must be present; the second was missing before.
        self.assertIn("2026-01-01T00:00:00Z-001", after)
        self.assertIn("2026-01-02T00:00:00Z-001", after)
        # Reset fixture for repeatability.
        index_path.write_text(original)


class TestLedgerHandlerWeeklyShards(unittest.TestCase):
    """T-0148: union-aware ledger_handler + per-segment SPINE.md."""

    INDEX_SEED = (
        "# Ledger Index\n\n## 2026\n\n"
        "| ID | Action | Agent | Justifies-via |\n|---|---|---|---|\n"
    )

    def _entry(self, eid: str, ts: str, action: str = "x") -> str:
        return json.dumps({
            "id": eid, "ts": ts, "agent": "recovery",
            "action": action, "justifies_via": ["P5"],
        })

    def test_pre_migration_reads_flat_file_no_spine_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger"
            ledger.mkdir()
            (ledger / "INDEX.md").write_text(self.INDEX_SEED)
            (ledger / "2026.jsonl").write_text(
                self._entry("2026-04-30T00:00:00Z-001", "2026-04-30T00:00:00Z") + "\n"
            )
            with patch.object(sig, "_ledger_commit_and_push") as mock_commit:
                sig.ledger_handler(ledger)
                self.assertEqual(mock_commit.call_count, 1)
                # No subfolder existed → extra_rel_paths must be empty.
                _, kwargs = mock_commit.call_args
                self.assertEqual(kwargs.get("extra_rel_paths"), [])
            after = (ledger / "INDEX.md").read_text()
            self.assertIn("2026-04-30T00:00:00Z-001", after)
            # No SPINE.md anywhere (no segment subfolders).
            self.assertEqual(list(ledger.glob("*/SPINE.md")), [])

    def test_post_migration_indexes_shard_entries_and_writes_spine(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger"
            ledger.mkdir()
            (ledger / "INDEX.md").write_text(self.INDEX_SEED)
            seg = "2026-W18"
            (ledger / seg).mkdir()
            (ledger / seg / f"{seg}.jsonl").write_text(
                self._entry("2026-04-30T00:00:00Z-001", "2026-04-30T00:00:00Z", "land_a") + "\n"
                + self._entry("2026-04-30T01:00:00Z-001", "2026-04-30T01:00:00Z", "land_b") + "\n"
            )
            with patch.object(sig, "_ledger_commit_and_push") as mock_commit:
                sig.ledger_handler(ledger, cwd=Path(tmp))
                self.assertEqual(mock_commit.call_count, 1)
                _, kwargs = mock_commit.call_args
                self.assertEqual(kwargs.get("extra_rel_paths"), ["ledger/2026-W18/SPINE.md"])
            # INDEX picked up both shard entries.
            after = (ledger / "INDEX.md").read_text()
            self.assertIn("2026-04-30T00:00:00Z-001", after)
            self.assertIn("2026-04-30T01:00:00Z-001", after)
            # SPINE.md generated with both rows + ts column.
            spine = (ledger / seg / "SPINE.md").read_text()
            self.assertIn("Ledger segment 2026-W18", spine)
            self.assertIn("2026-04-30T00:00:00Z-001", spine)
            self.assertIn("land_b", spine)
            self.assertIn("| TS |", spine)

    def test_spine_regenerated_when_only_spine_stale_index_current(self):
        # INDEX already has the entry but SPINE.md is missing → commit fires
        # for the SPINE alone (new_count 0).
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger"
            ledger.mkdir()
            seg = "2026-W18"
            (ledger / seg).mkdir()
            (ledger / seg / f"{seg}.jsonl").write_text(
                self._entry("2026-04-30T00:00:00Z-001", "2026-04-30T00:00:00Z") + "\n"
            )
            (ledger / "INDEX.md").write_text(
                self.INDEX_SEED
                + "| `2026-04-30T00:00:00Z-001` | x | recovery | P5 |\n"
            )
            with patch.object(sig, "_ledger_commit_and_push") as mock_commit:
                sig.ledger_handler(ledger, cwd=Path(tmp))
                self.assertEqual(mock_commit.call_count, 1)
                _, kwargs = mock_commit.call_args
                self.assertEqual(kwargs.get("new_count"), 0)
                self.assertEqual(kwargs.get("extra_rel_paths"), ["ledger/2026-W18/SPINE.md"])

    def test_idempotent_no_commit_when_index_and_spine_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger"
            ledger.mkdir()
            seg = "2026-W18"
            (ledger / seg).mkdir()
            (ledger / seg / f"{seg}.jsonl").write_text(
                self._entry("2026-04-30T00:00:00Z-001", "2026-04-30T00:00:00Z") + "\n"
            )
            (ledger / "INDEX.md").write_text(
                self.INDEX_SEED
                + "| `2026-04-30T00:00:00Z-001` | x | recovery | P5 |\n"
            )
            # Pre-write the exact SPINE.md the handler would produce.
            entries = sig._read_jsonl_entries(ledger / seg / f"{seg}.jsonl")
            (ledger / seg / "SPINE.md").write_text(sig._build_segment_spine(seg, entries))
            with patch.object(sig, "_ledger_commit_and_push") as mock_commit:
                sig.ledger_handler(ledger, cwd=Path(tmp))
                self.assertEqual(mock_commit.call_count, 0)

    def test_spine_path_helper(self):
        self.assertEqual(
            sig._segment_spine_path(Path("/l"), "2026-W18"),
            Path("/l") / "2026-W18" / "SPINE.md",
        )


# ---------------------------------------------------------------------------
# Cache + sentinel + idempotence tests.
# ---------------------------------------------------------------------------


class TestCacheHit(unittest.TestCase):
    def test_spine_index_groom_skips_dirs_with_unchanged_mtime_via_cache_hit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "research"
            d.mkdir()
            (d / "README.md").write_text("# R\n<!-- AUTOGEN-START spine-index -->\n<!-- AUTOGEN-END spine-index -->\n")
            # Max mtime of anything in d.
            current_mtime = max(p.stat().st_mtime for p in d.rglob("*"))
            warm_cache = {str(d): current_mtime + 100.0}

            call_counter = {"n": 0}
            original = sig.research_handler

            def counting_handler(path):
                call_counter["n"] += 1
                return original(path)

            handler_map = {d: counting_handler}

            with patch.object(sig, "_load_cache", return_value={sig.CACHE_SCHEMA_VERSION: True, "dirs": warm_cache}), \
                 patch.object(sig, "_save_cache"):
                sig.groom_dirs(handler_map, dry_run=True)

            self.assertEqual(call_counter["n"], 0, "Handler should not be called on cache hit")


class TestSubdirContentMtime(unittest.TestCase):
    """T-1466: the mtime used to invalidate the cache for subdir-handler dirs
    (incidents/research/skills) must catch CONTENT edits to a subdir's files,
    not just structural (subdir add/remove) changes."""

    def test_content_edit_in_subdir_readme_bumps_handler_mtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "incidents"
            d.mkdir()
            sub = d / "2026-01-01-some-incident"
            sub.mkdir()
            readme = sub / "README.md"
            readme.write_text("# Title\n\n## General failure shape\n\nold\n")

            # Pin all mtimes to a known base so the test is wall-clock-free.
            base = 1_000_000.0
            for p in (readme, sub, d):
                os.utime(p, (base, base))
            before = sig._dir_mtime_for_handler(d, sig.incidents_handler)

            # Edit ONLY the file content + advance ONLY the file mtime; keep the
            # subdir + parent dir mtimes pinned, exactly as the OS behaves when a
            # file is modified in place (parent-dir mtime is untouched).
            readme.write_text("# Title\n\n## General failure shape\n\nNEW\n")
            os.utime(readme, (base + 100, base + 100))
            os.utime(sub, (base, base))
            os.utime(d, (base, base))
            after = sig._dir_mtime_for_handler(d, sig.incidents_handler)

            self.assertGreater(
                after, before,
                "content-aware mtime must catch a subdir README content edit (T-1466)")
            # Lock in the contrast: the dir-only scan (used pre-fix) does NOT
            # catch it — that gap is the bug this fixes.
            self.assertEqual(
                sig._max_mtime_subdirs(d), base,
                "_max_mtime_subdirs is dir-mtime-only by design")

    def test_watch_reports_stays_structure_only(self):
        # watch_reports_handler emits subdir NAMES only — it must keep using the
        # cheaper dir-only mtime, not the content-aware one.
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "watch"
            d.mkdir()
            self.assertEqual(
                sig._dir_mtime_for_handler(d, sig.watch_reports_handler),
                sig._max_mtime_subdirs(d))


class TestCacheSchemaVersionBump(unittest.TestCase):
    def test_spine_index_groom_invalidates_cache_on_schema_version_bump(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "notes"
            d.mkdir()
            (d / "README.md").write_text("# N\n<!-- AUTOGEN-START spine-index -->\n<!-- AUTOGEN-END spine-index -->\n")

            # Write cache file with old schema version (0, not current 1).
            cache_file = Path(tmpdir) / "index_groom_cache.json"
            cache_file.write_text(json.dumps({
                "schema_version": 0,
                "dirs": {str(d): 9999999999.0},  # future mtime — would be cache-hit if schema matched
            }))

            # Save a reference to the real function BEFORE any patching.
            real_load_cache = sig._load_cache.__func__ if hasattr(sig._load_cache, "__func__") else sig._load_cache

            # Verify schema invalidation via the real function.
            loaded = real_load_cache(cache_file)
            self.assertEqual(loaded["dirs"], {}, "Old schema_version must wipe cached dirs")

            call_counter = {"n": 0}

            def counting_handler(path):
                call_counter["n"] += 1
                return []

            handler_map = {d: counting_handler}

            # groom_dirs with the schema-invalidated (empty) cache must call handler.
            with patch.object(sig, "_load_cache", return_value=loaded), \
                 patch.object(sig, "_save_cache"), \
                 patch.object(sig, "_commit_dir_change"):
                sig.groom_dirs(handler_map, dry_run=True)

            self.assertGreater(call_counter["n"], 0, "Handler must be called when schema version mismatches")


class TestSentinelRegionProtection(unittest.TestCase):
    def test_spine_index_groom_only_writes_between_sentinel_regions_never_outside(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(
                "Hand-curated before.\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                "old row\n"
                "<!-- AUTOGEN-END spine-index -->\n"
                "Hand-curated after.\n"
            )
            path = Path(f.name)

        try:
            sig.write_sentinel_region(path, ["| new-row | data |"])
            after = path.read_text()
            # Prose outside sentinels must be byte-identical.
            self.assertTrue(after.startswith("Hand-curated before.\n"))
            self.assertIn("Hand-curated after.\n", after)
            # New row must be between sentinels.
            self.assertIn("new-row", after)
            # Old row must be replaced.
            self.assertNotIn("old row", after)
        finally:
            path.unlink()


class TestIdempotence(unittest.TestCase):
    def test_spine_index_groom_idempotent_on_repeat_run_with_no_disk_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "bugs"
            d.mkdir()
            bug = d / "example.md"
            bug.write_text(
                "---\nsymptom: \"Test symptom\"\nfix: \"Fix\"\ndomain: \"python\"\n---\n## Body\n"
            )
            readme = d / "README.md"
            readme.write_text(
                "# Bugs\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                "<!-- AUTOGEN-END spine-index -->\n"
                "## Authoring\n"
            )

            handler_map = {d: sig.bugs_handler}

            with patch.object(sig, "_load_cache", return_value={"schema_version": sig.CACHE_SCHEMA_VERSION, "dirs": {}}), \
                 patch.object(sig, "_save_cache"), \
                 patch.object(sig, "_commit_dir_change"):
                sig.groom_dirs(handler_map, dry_run=False)
                after_first = readme.read_text()

                # Update cache to reflect current mtime so second run is cache-hit.
                current_mtime = max(p.stat().st_mtime for p in d.rglob("*"))
                warm = {"schema_version": sig.CACHE_SCHEMA_VERSION, "dirs": {str(d): current_mtime + 100.0}}
                with patch.object(sig, "_load_cache", return_value=warm), \
                     patch.object(sig, "_save_cache"), \
                     patch.object(sig, "_commit_dir_change"):
                    sig.groom_dirs(handler_map, dry_run=False)
                    after_second = readme.read_text()

            self.assertEqual(after_first, after_second, "Second run must produce identical README")


# ---------------------------------------------------------------------------
# No-op + fail-open tests.
# ---------------------------------------------------------------------------


class TestNoCommitWhenNoDiff(unittest.TestCase):
    def test_spine_index_groom_skips_commit_when_no_diff_after_regen(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "notes"
            d.mkdir()
            (d / "alpha.md").write_text("# Alpha Note\nAlpha note purpose.\n")
            # Pre-populate README with exactly the rows the handler would produce.
            readme = d / "README.md"
            # First generate what rows would look like.
            rows = sig.notes_handler(d)
            region = "\n".join(rows) + ("\n" if rows else "")
            readme.write_text(
                "# Notes\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                + region
                + "<!-- AUTOGEN-END spine-index -->\n"
            )

            commit_calls = []

            def fake_commit(*args, **kwargs):
                commit_calls.append(args)

            handler_map = {d: sig.notes_handler}

            with patch.object(sig, "_load_cache", return_value={"schema_version": sig.CACHE_SCHEMA_VERSION, "dirs": {}}), \
                 patch.object(sig, "_save_cache"), \
                 patch.object(sig, "_commit_dir_change", side_effect=fake_commit):
                sig.groom_dirs(handler_map, dry_run=False)

            self.assertEqual(len(commit_calls), 0, "No commit should fire when regenerated rows match existing region")


class TestFailOpenOnPullError(unittest.TestCase):
    def test_spine_index_groom_fails_open_on_transient_git_pull_error_via_pull_repo_or_warn(self):
        """T-0471 behavior change: with_worktree replaces pull_repo_or_warn.

        Pre-T-0471: fetch timeout → fail-open (logged to stderr + continued).
        Post-T-0471: fetch timeout → WorktreeError raised (fail-closed per P1/D5).

        Fail-closed is the correct contract: if origin/main is unreachable the
        worktree cannot be created with a fresh tip; running against a stale or
        shared working tree defeats the structural isolation guarantee. Operator
        triages and retries; stale-but-running is no longer preferred over
        halted-on-network (the isolation value exceeds the availability cost).
        """
        import subprocess as real_sub
        from tools._git_utils import WorktreeError
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "notes"
            d.mkdir()
            (d / "README.md").write_text(
                "# N\n<!-- AUTOGEN-START spine-index -->\n<!-- AUTOGEN-END spine-index -->\n"
            )

            handler_map = {d: sig.notes_handler}

            with patch("tools._git_utils.subprocess") as mock_sub, \
                 patch("tools._git_utils.Path.exists", return_value=False):
                mock_sub.TimeoutExpired = real_sub.TimeoutExpired
                mock_sub.OSError = OSError
                mock_sub.run.side_effect = real_sub.TimeoutExpired(cmd="git", timeout=30)
                with self.assertRaises(WorktreeError) as ctx:
                    sig.run_all(handler_map=handler_map, dry_run=False, push=False)

            self.assertIn("fetch", str(ctx.exception).lower(),
                          "WorktreeError must mention fetch failure")


# ---------------------------------------------------------------------------
# Integration seam test.
# ---------------------------------------------------------------------------


class TestEndToEndFixtureRepo(unittest.TestCase):
    def test_spine_index_groom_end_to_end_with_fixture_repo_and_synthetic_dir_landings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)

            # Minimal git repo.
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)

            # research/ with sentinel README + one subdir.
            research = repo / "research"
            research.mkdir()
            survey = research / "2026-04-29-test-survey"
            survey.mkdir()
            (survey / "README.md").write_text("# Test Survey\n\nTest survey description.\n")
            (research / "README.md").write_text(
                "# Research\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                "<!-- AUTOGEN-END spine-index -->\n"
                "## Convention\n"
            )

            # bugs/ with sentinel README + one bug file.
            bugs = repo / "bugs"
            bugs.mkdir()
            (bugs / "test-bug.md").write_text(
                "---\nsymptom: \"Test bug symptom\"\nfix: \"Fix it\"\ndomain: \"rust\"\n---\n## Body\n"
            )
            (bugs / "README.md").write_text(
                "# Bugs\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                "<!-- AUTOGEN-END spine-index -->\n"
                "## Authoring\n"
            )

            # notes/ with sentinel README + one note.
            notes = repo / "notes"
            notes.mkdir()
            (notes / "test-note.md").write_text("# Test Note\n\nTest note purpose here.\n")
            (notes / "README.md").write_text(
                "# Notes\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                "<!-- AUTOGEN-END spine-index -->\n"
            )

            # Initial commit.
            subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True, check=True)

            handler_map = {
                research: sig.research_handler,
                bugs: sig.bugs_handler,
                notes: sig.notes_handler,
            }

            with patch.object(sig, "_load_cache", return_value={"schema_version": sig.CACHE_SCHEMA_VERSION, "dirs": {}}), \
                 patch.object(sig, "_save_cache"), \
                 patch.object(sig, "_commit_dir_change"), \
                 patch.object(sig, "pull_repo_or_warn"):
                sig.groom_dirs(handler_map, dry_run=False)

            # research README: row for 2026-04-29-test-survey.
            research_text = (research / "README.md").read_text()
            self.assertIn("2026-04-29-test-survey", research_text)
            self.assertIn("Test survey description", research_text)
            # Prose outside sentinels untouched.
            self.assertIn("## Convention", research_text)

            # bugs README: row for test-bug.md.
            bugs_text = (bugs / "README.md").read_text()
            self.assertIn("test-bug.md", bugs_text)
            self.assertIn("Test bug symptom", bugs_text)
            self.assertIn("## Authoring", bugs_text)

            # notes README: row for test-note.md.
            notes_text = (notes / "README.md").read_text()
            self.assertIn("test-note.md", notes_text)
            self.assertIn("Test note purpose", notes_text)


# ---------------------------------------------------------------------------
# New handler tests — lessons/, rulebook/, rulebook/policies/,
# sop/standard/, sop/remediation/ (T-0434).
# All five dirs use notes_handler (flat .md alphabetical list).
# ---------------------------------------------------------------------------


class TestLessonsHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_lessons_handler_in_map_and_produces_alphabetical_md_list(self):
        # Verify handler is registered in _HANDLER_MAP.
        lessons_path = sig.REPO_ROOT / "lessons"
        self.assertIn(lessons_path, sig._HANDLER_MAP, "lessons/ must be in _HANDLER_MAP")
        self.assertIs(sig._HANDLER_MAP[lessons_path], sig.notes_handler)

        # Verify handler produces expected rows from fixture.
        rows = sig.notes_handler(self.fixtures / "lessons")
        self.assertEqual(len(rows), 2)
        alpha = [r for r in rows if "alpha-lesson.md" in r]
        self.assertEqual(len(alpha), 1)
        self.assertIn("Alpha lesson purpose for testing", alpha[0])
        beta = [r for r in rows if "beta-lesson.md" in r]
        self.assertEqual(len(beta), 1)
        self.assertIn("Beta lesson purpose for testing", beta[0])

    def test_spine_index_groom_lessons_handler_idempotent_on_repeat_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "lessons"
            d.mkdir()
            (d / "alpha-lesson.md").write_text("# Alpha\nAlpha lesson idempotence test.\n")
            readme = d / "README.md"
            rows = sig.notes_handler(d)
            region = "\n".join(rows) + ("\n" if rows else "")
            readme.write_text(
                "# Lessons\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                + region
                + "<!-- AUTOGEN-END spine-index -->\n"
            )
            commit_calls = []
            handler_map = {d: sig.notes_handler}
            with patch.object(sig, "_load_cache", return_value={"schema_version": sig.CACHE_SCHEMA_VERSION, "dirs": {}}), \
                 patch.object(sig, "_save_cache"), \
                 patch.object(sig, "_commit_dir_change", side_effect=lambda *a, **kw: commit_calls.append(a)):
                sig.groom_dirs(handler_map, dry_run=False)
            self.assertEqual(len(commit_calls), 0, "No commit should fire when region already matches")


class TestRulebookHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_rulebook_handler_in_map_and_lists_top_level_md_files(self):
        # Verify handler is registered in _HANDLER_MAP.
        rulebook_path = sig.REPO_ROOT / "rulebook"
        self.assertIn(rulebook_path, sig._HANDLER_MAP, "rulebook/ must be in _HANDLER_MAP")
        self.assertIs(sig._HANDLER_MAP[rulebook_path], sig.notes_handler)

        # Fixture: principles.md + directives.md (no README.md, no subdir).
        rows = sig.notes_handler(self.fixtures / "rulebook")
        self.assertEqual(len(rows), 2)
        principles = [r for r in rows if "principles.md" in r]
        self.assertEqual(len(principles), 1)
        self.assertIn("Alpha principles fixture", principles[0])
        directives = [r for r in rows if "directives.md" in r]
        self.assertEqual(len(directives), 1)
        self.assertIn("Beta directives fixture", directives[0])

    def test_spine_index_groom_rulebook_handler_excludes_subdirs(self):
        # notes_handler uses glob("*.md") — subdirs are naturally excluded.
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "rulebook"
            d.mkdir()
            (d / "principles.md").write_text("# Principles\nTest principles.\n")
            (d / "policies").mkdir()  # subdir — must not appear in rows
            rows = sig.notes_handler(d)
            self.assertEqual(len(rows), 1)
            self.assertIn("principles.md", rows[0])


class TestRulebookPoliciesHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_rulebook_policies_handler_in_map_and_lists_policy_md_files(self):
        # Verify handler is registered in _HANDLER_MAP.
        policies_path = sig.REPO_ROOT / "rulebook" / "policies"
        self.assertIn(policies_path, sig._HANDLER_MAP, "rulebook/policies/ must be in _HANDLER_MAP")
        self.assertIs(sig._HANDLER_MAP[policies_path], sig.notes_handler)

        rows = sig.notes_handler(self.fixtures / "rulebook_policies")
        self.assertEqual(len(rows), 2)
        alpha = [r for r in rows if "alpha-policy.md" in r]
        self.assertEqual(len(alpha), 1)
        self.assertIn("Alpha policy purpose", alpha[0])
        beta = [r for r in rows if "beta-policy.md" in r]
        self.assertEqual(len(beta), 1)
        self.assertIn("Beta policy purpose", beta[0])

    def test_spine_index_groom_rulebook_policies_handler_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "policies"
            d.mkdir()
            (d / "alpha-policy.md").write_text("# Alpha\nAlpha policy idempotence.\n")
            readme = d / "README.md"
            rows = sig.notes_handler(d)
            region = "\n".join(rows) + ("\n" if rows else "")
            readme.write_text(
                "# Policies\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                + region
                + "<!-- AUTOGEN-END spine-index -->\n"
            )
            commit_calls = []
            handler_map = {d: sig.notes_handler}
            with patch.object(sig, "_load_cache", return_value={"schema_version": sig.CACHE_SCHEMA_VERSION, "dirs": {}}), \
                 patch.object(sig, "_save_cache"), \
                 patch.object(sig, "_commit_dir_change", side_effect=lambda *a, **kw: commit_calls.append(a)):
                sig.groom_dirs(handler_map, dry_run=False)
            self.assertEqual(len(commit_calls), 0)


class TestSopStandardHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_sop_standard_handler_in_map_and_lists_standard_sop_md_files(self):
        sop_standard_path = sig.REPO_ROOT / "sop" / "standard"
        self.assertIn(sop_standard_path, sig._HANDLER_MAP, "sop/standard/ must be in _HANDLER_MAP")
        self.assertIs(sig._HANDLER_MAP[sop_standard_path], sig.notes_handler)

        rows = sig.notes_handler(self.fixtures / "sop_standard")
        self.assertEqual(len(rows), 2)
        alpha = [r for r in rows if "alpha-sop.md" in r]
        self.assertEqual(len(alpha), 1)
        self.assertIn("Alpha standard SOP purpose", alpha[0])
        beta = [r for r in rows if "beta-sop.md" in r]
        self.assertEqual(len(beta), 1)
        self.assertIn("Beta standard SOP purpose", beta[0])

    def test_spine_index_groom_sop_standard_handler_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "standard"
            d.mkdir()
            (d / "alpha-sop.md").write_text("# Alpha SOP\nAlpha SOP idempotence.\n")
            readme = d / "README.md"
            rows = sig.notes_handler(d)
            region = "\n".join(rows) + ("\n" if rows else "")
            readme.write_text(
                "# Standard SOPs\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                + region
                + "<!-- AUTOGEN-END spine-index -->\n"
            )
            commit_calls = []
            handler_map = {d: sig.notes_handler}
            with patch.object(sig, "_load_cache", return_value={"schema_version": sig.CACHE_SCHEMA_VERSION, "dirs": {}}), \
                 patch.object(sig, "_save_cache"), \
                 patch.object(sig, "_commit_dir_change", side_effect=lambda *a, **kw: commit_calls.append(a)):
                sig.groom_dirs(handler_map, dry_run=False)
            self.assertEqual(len(commit_calls), 0)


class TestSopRemediationHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_sop_remediation_handler_in_map_and_lists_remediation_sop_md_files(self):
        sop_remediation_path = sig.REPO_ROOT / "sop" / "remediation"
        self.assertIn(sop_remediation_path, sig._HANDLER_MAP, "sop/remediation/ must be in _HANDLER_MAP")
        self.assertIs(sig._HANDLER_MAP[sop_remediation_path], sig.notes_handler)

        rows = sig.notes_handler(self.fixtures / "sop_remediation")
        self.assertEqual(len(rows), 2)
        alpha = [r for r in rows if "alpha-remediation.md" in r]
        self.assertEqual(len(alpha), 1)
        self.assertIn("Alpha remediation SOP purpose", alpha[0])
        beta = [r for r in rows if "beta-remediation.md" in r]
        self.assertEqual(len(beta), 1)
        self.assertIn("Beta remediation SOP purpose", beta[0])

    def test_spine_index_groom_sop_remediation_handler_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "remediation"
            d.mkdir()
            (d / "alpha-remediation.md").write_text("# Alpha Remediation\nAlpha remediation idempotence.\n")
            readme = d / "README.md"
            rows = sig.notes_handler(d)
            region = "\n".join(rows) + ("\n" if rows else "")
            readme.write_text(
                "# Remediation SOPs\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                + region
                + "<!-- AUTOGEN-END spine-index -->\n"
            )
            commit_calls = []
            handler_map = {d: sig.notes_handler}
            with patch.object(sig, "_load_cache", return_value={"schema_version": sig.CACHE_SCHEMA_VERSION, "dirs": {}}), \
                 patch.object(sig, "_save_cache"), \
                 patch.object(sig, "_commit_dir_change", side_effect=lambda *a, **kw: commit_calls.append(a)):
                sig.groom_dirs(handler_map, dry_run=False)
            self.assertEqual(len(commit_calls), 0)


class TestPinnedRulesHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_pinned_rules_handler_in_map_and_lists_agent_md_files(self):
        # Verify handler is registered in _HANDLER_MAP.
        pinned_rules_path = sig.REPO_ROOT / "pinned-rules"
        self.assertIn(pinned_rules_path, sig._HANDLER_MAP, "pinned-rules/ must be in _HANDLER_MAP")
        self.assertIs(sig._HANDLER_MAP[pinned_rules_path], sig.pinned_rules_handler)

        # Verify handler produces expected rows from fixture.
        rows = sig.pinned_rules_handler(self.fixtures / "pinned_rules")
        self.assertEqual(len(rows), 1)
        recovery = [r for r in rows if "recovery.md" in r]
        self.assertEqual(len(recovery), 1)
        self.assertIn("Recovery is the recovery", recovery[0])

    def test_spine_index_groom_pinned_rules_handler_excludes_readme(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "pinned-rules"
            d.mkdir()
            (d / "README.md").write_text("# Pinned Rules\nThis is the README.\n")
            (d / "recovery.md").write_text("# Recovery\nWard description.\n")
            rows = sig.pinned_rules_handler(d)
            self.assertEqual(len(rows), 1)
            self.assertIn("recovery.md", rows[0])
            # README must not appear
            readme_rows = [r for r in rows if "README.md" in r]
            self.assertEqual(len(readme_rows), 0)

    def test_spine_index_groom_pinned_rules_handler_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "pinned-rules"
            d.mkdir()
            (d / "recovery.md").write_text("# Recovery\nWard idempotence test.\n")
            readme = d / "README.md"
            rows = sig.pinned_rules_handler(d)
            region = "\n".join(rows) + ("\n" if rows else "")
            readme.write_text(
                "# Pinned Rules\nCurated prose.\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                + region
                + "<!-- AUTOGEN-END spine-index -->\n"
            )
            commit_calls = []
            handler_map = {d: sig.pinned_rules_handler}
            with patch.object(sig, "_load_cache", return_value={"schema_version": sig.CACHE_SCHEMA_VERSION, "dirs": {}}), \
                 patch.object(sig, "_save_cache"), \
                 patch.object(sig, "_commit_dir_change", side_effect=lambda *a, **kw: commit_calls.append(a)):
                sig.groom_dirs(handler_map, dry_run=False)
            self.assertEqual(len(commit_calls), 0, "No commit should fire when region already matches")


class TestSkillsHandler(_SpineFixtureTestCase):
    def test_spine_index_groom_skills_handler_in_map_and_lists_skill_subdirs_with_description(self):
        # Verify handler is registered in _HANDLER_MAP.
        skills_path = sig.REPO_ROOT / "skills"
        self.assertIn(skills_path, sig._HANDLER_MAP, "skills/ must be in _HANDLER_MAP")
        self.assertIs(sig._HANDLER_MAP[skills_path], sig.skills_handler)

        # Verify handler produces expected rows from fixture (3 skill subdirs).
        rows = sig.skills_handler(self.fixtures / "skills")
        self.assertEqual(len(rows), 3)
        parallel = [r for r in rows if "dispatching-parallel-agents" in r]
        self.assertEqual(len(parallel), 1)
        self.assertIn("2+ independent tasks", parallel[0])
        subagent = [r for r in rows if "subagent-driven-development" in r]
        self.assertEqual(len(subagent), 1)
        self.assertIn("delegating implementation", subagent[0])
        verify = [r for r in rows if "verification-before-completion" in r]
        self.assertEqual(len(verify), 1)
        self.assertIn("claiming any work complete", verify[0])

    def test_spine_index_groom_skills_handler_skips_subdirs_without_skill_md(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "skills"
            d.mkdir()
            (d / "skill-with-skillmd").mkdir()
            ((d / "skill-with-skillmd") / "SKILL.md").write_text(
                "---\nname: skill-with-skillmd\ndescription: Has a SKILL.md file.\n---\n"
            )
            (d / "skill-without-skillmd").mkdir()  # no SKILL.md — must be skipped
            rows = sig.skills_handler(d)
            self.assertEqual(len(rows), 1)
            self.assertIn("skill-with-skillmd", rows[0])

    def test_spine_index_groom_skills_handler_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir) / "skills"
            d.mkdir()
            skill = d / "my-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: My skill idempotence description.\n---\n"
            )
            readme = d / "README.md"
            rows = sig.skills_handler(d)
            region = "\n".join(rows) + ("\n" if rows else "")
            readme.write_text(
                "# Skills\nCurated prose.\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                + region
                + "<!-- AUTOGEN-END spine-index -->\n"
            )
            commit_calls = []
            handler_map = {d: sig.skills_handler}
            with patch.object(sig, "_load_cache", return_value={"schema_version": sig.CACHE_SCHEMA_VERSION, "dirs": {}}), \
                 patch.object(sig, "_save_cache"), \
                 patch.object(sig, "_commit_dir_change", side_effect=lambda *a, **kw: commit_calls.append(a)):
                sig.groom_dirs(handler_map, dry_run=False)
            self.assertEqual(len(commit_calls), 0, "No commit should fire when region already matches")


if __name__ == "__main__":
    unittest.main()
