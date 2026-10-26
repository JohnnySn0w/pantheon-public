"""Tests for tools/naming_history.py — the T-3815 rename-continuity resolver.

Coverage:
- Registry load: valid file, invalid schema_version, missing fields, unknown
  fields, invalid kind, non-string values, non-list entries.
- Resolve: single hit, multiple hits (ambiguous), kind filter narrowing,
  no-match returns empty.
- Real-registry sanity: the shipped rulebook/naming-history.yaml loads cleanly
  and contains the recent-rename seed set the acceptance criteria named.
"""
from __future__ import annotations

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools import naming_history as nh


def _write(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body))
    return path


class LoadRegistryTests(unittest.TestCase):
    def test_load_registry_minimal_valid_file(self) -> None:
        with TemporaryDirectory() as tmp:
            p = _write(
                Path(tmp) / "reg.yaml",
                """\
                schema_version: 1
                entries:
                  - old: old_role
                    new: new_role
                    kind: role_id
                    retired_date: 2026-01-15
                    ratified_by: T-0001
                    scope: pilot
                """,
            )
            reg = nh.load_registry(p)
            self.assertEqual(len(reg), 1)
            entry = reg[0]
            self.assertEqual(entry.old, "old_role")
            self.assertEqual(entry.new, "new_role")
            self.assertEqual(entry.kind, "role_id")
            self.assertEqual(entry.retired_date, "2026-01-15")
            self.assertEqual(entry.ratified_by, "T-0001")

    def test_load_registry_accepts_quoted_date(self) -> None:
        with TemporaryDirectory() as tmp:
            p = _write(
                Path(tmp) / "reg.yaml",
                """\
                schema_version: 1
                entries:
                  - old: x
                    new: y
                    kind: role_id
                    retired_date: "2026-02-20"
                    ratified_by: T-0002
                    scope: quoted-date variant
                """,
            )
            reg = nh.load_registry(p)
            self.assertEqual(reg[0].retired_date, "2026-02-20")

    def test_load_registry_missing_file_raises(self) -> None:
        with self.assertRaises(nh.NamingHistoryError) as ctx:
            nh.load_registry(Path("/nonexistent/reg.yaml"))
        self.assertIn("not found", str(ctx.exception))

    def test_load_registry_wrong_schema_version_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            p = _write(
                Path(tmp) / "reg.yaml",
                """\
                schema_version: 2
                entries: []
                """,
            )
            with self.assertRaises(nh.NamingHistoryError) as ctx:
                nh.load_registry(p)
            self.assertIn("schema_version", str(ctx.exception))

    def test_load_registry_missing_required_field_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            p = _write(
                Path(tmp) / "reg.yaml",
                """\
                schema_version: 1
                entries:
                  - old: a
                    new: b
                    kind: role_id
                    ratified_by: T-0001
                    scope: missing retired_date
                """,
            )
            with self.assertRaises(nh.NamingHistoryError) as ctx:
                nh.load_registry(p)
            self.assertIn("retired_date", str(ctx.exception))

    def test_load_registry_unknown_field_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            p = _write(
                Path(tmp) / "reg.yaml",
                """\
                schema_version: 1
                entries:
                  - old: a
                    new: b
                    kind: role_id
                    retired_date: 2026-01-15
                    ratified_by: T-0001
                    scope: s
                    invented_field: nope
                """,
            )
            with self.assertRaises(nh.NamingHistoryError) as ctx:
                nh.load_registry(p)
            self.assertIn("invented_field", str(ctx.exception))

    def test_load_registry_invalid_kind_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            p = _write(
                Path(tmp) / "reg.yaml",
                """\
                schema_version: 1
                entries:
                  - old: a
                    new: b
                    kind: badkind
                    retired_date: 2026-01-15
                    ratified_by: T-0001
                    scope: s
                """,
            )
            with self.assertRaises(nh.NamingHistoryError) as ctx:
                nh.load_registry(p)
            self.assertIn("badkind", str(ctx.exception))

    def test_load_registry_empty_string_field_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            p = _write(
                Path(tmp) / "reg.yaml",
                """\
                schema_version: 1
                entries:
                  - old: ""
                    new: b
                    kind: role_id
                    retired_date: 2026-01-15
                    ratified_by: T-0001
                    scope: s
                """,
            )
            with self.assertRaises(nh.NamingHistoryError):
                nh.load_registry(p)


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        # A fixture registry with a known-ambiguous mapping (`x` renamed
        # differently under role_id vs persona).
        self.reg = (
            nh.NamingEntry(
                old="x", new="y", kind="role_id",
                retired_date="2026-01-15", ratified_by="T-0001", scope="role-scope",
            ),
            nh.NamingEntry(
                old="x", new="z", kind="persona",
                retired_date="2026-02-15", ratified_by="T-0002", scope="persona-scope",
            ),
            nh.NamingEntry(
                old="w", new="v", kind="role_id",
                retired_date="2026-01-15", ratified_by="T-0003", scope="unrelated",
            ),
        )

    def test_resolve_single_hit(self) -> None:
        hits = nh.resolve("w", self.reg)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].new, "v")

    def test_resolve_ambiguous_returns_all_matches(self) -> None:
        hits = nh.resolve("x", self.reg)
        self.assertEqual(len(hits), 2)
        news = {h.new for h in hits}
        self.assertEqual(news, {"y", "z"})

    def test_resolve_kind_filter_narrows(self) -> None:
        hits = nh.resolve("x", self.reg, kind="role_id")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].new, "y")

    def test_resolve_no_match_returns_empty(self) -> None:
        self.assertEqual(nh.resolve("NEVER_HAPPENED", self.reg), ())


class ShippedRegistrySanityTests(unittest.TestCase):
    """Load the real rulebook/naming-history.yaml and assert it carries the
    recent-rename seed set the T-3815 acceptance criteria named."""

    def test_shipped_registry_loads_clean(self) -> None:
        reg = nh.load_registry()
        self.assertGreater(len(reg), 0)

    def test_t3807_batch_seed_entries_present(self) -> None:
        reg = nh.load_registry()
        # T-3807 batch: five renames + one retirement
        expected = {
            ("sub_orchestrator", "engineering_orchestrator"),
            ("recovery", "head_system_orchestrator"),
            ("reviewer", "head_auditor"),
            ("research", "head_research_orchestrator"),
            ("orchestrator", "retired"),
            ("orchestrator", "head_memory_orchestrator"),
        }
        present = {(e.old, e.new) for e in reg}
        missing = expected - present
        self.assertFalse(missing, f"missing T-3807 seed entries: {missing}")

    def test_t3814_head_engineering_rename_present(self) -> None:
        reg = nh.load_registry()
        matches = [e for e in reg if e.old == "orchestrator" and e.new == "head_engineering_orchestrator"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].ratified_by, "T-3814")

    def test_orchestrator_disambiguation_holds(self) -> None:
        """The `orchestrator` → orchestrator's head_engineering_orchestrator vs
        archivist's head_memory_orchestrator ambiguity is a real property of
        the fleet; a resolve for it MUST return both entries so callers know to
        disambiguate by persona context."""
        reg = nh.load_registry()
        hits = nh.resolve("orchestrator", reg)
        news = {h.new for h in hits}
        self.assertIn("head_memory_orchestrator", news)
        self.assertIn("head_engineering_orchestrator", news)


if __name__ == "__main__":
    unittest.main()
