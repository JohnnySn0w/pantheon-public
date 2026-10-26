"""Tests for seat_context_bundle (T-4065, ORG-075 BT1a / E2 (a)).

Covers the slice-file schema parser (+ invalid rejects), provenance stamping, the
edit-detection hash, EACH per-pin state-machine branch (absent→seed / unchanged→no-op
/ source-changed+unedited→update / locally-edited→CONFLICT / same-name-no-seed→
COLLISION), the delimited-section index rewrite (idempotent + preserves hand-written
lines), and an end-to-end export→import→re-import→source-change→local-edit→collision
integration seam that asserts memory_lint is CLEAN (no ERRORs) on the imported store.

No real accounts touched — everything runs in tempfile.TemporaryDirectory dirs.
"""
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from tools import memory_lint as ml  # noqa: E402
from tools import seat_context_bundle as scb  # noqa: E402


# --- a realistic source pin (nested metadata.type, top-level name/description) ---
def source_pin(name="reference-fact-one",
               desc="A one-fact reference pin used by the seat-context tests.",
               body="This is the body of the fact.\n",
               verified="2024-01-01") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f'description: "{desc}"\n'
        "metadata:\n"
        "  node_type: memory\n"
        "  type: reference\n"
        f"  verified: {verified}\n"
        "---\n"
        f"{body}"
    )


def lint_errors(memory_dir: Path) -> list[str]:
    """All memory_lint ERROR strings over a dir (WARNs ignored)."""
    errs: list[str] = []
    for name, e, _w in ml.lint_dir(memory_dir, stale_days=120, today_ord=None):
        errs.extend(f"{name}: {x}" for x in e)
    return errs


class SliceParseTest(unittest.TestCase):
    def test_valid_slice_file(self):
        text = (
            "slices:\n"
            "  eng:\n"
            "    description: for engineers\n"
            "    pins:\n"
            "      - a.md\n"
            "      - b.md\n"
            "  bare:\n"
            "    pins: [c.md]\n"
        )
        parsed = scb.parse_slice_file(text)
        self.assertEqual(parsed["eng"]["pins"], ["a.md", "b.md"])
        self.assertEqual(parsed["eng"]["description"], "for engineers")
        self.assertEqual(parsed["bare"]["pins"], ["c.md"])
        self.assertIsNone(parsed["bare"]["description"])

    def test_reject_empty(self):
        with self.assertRaises(scb.SliceError):
            scb.parse_slice_file("")

    def test_reject_unknown_top_level_key(self):
        with self.assertRaises(scb.SliceError):
            scb.parse_slice_file("slices:\n  x:\n    pins: [a.md]\nextra: 1\n")

    def test_reject_missing_slices(self):
        with self.assertRaises(scb.SliceError):
            scb.parse_slice_file("other: {}\n")

    def test_reject_slices_not_mapping(self):
        with self.assertRaises(scb.SliceError):
            scb.parse_slice_file("slices:\n  - a.md\n")

    def test_reject_slice_not_mapping(self):
        with self.assertRaises(scb.SliceError):
            scb.parse_slice_file("slices:\n  eng: [a.md]\n")

    def test_reject_pins_not_list(self):
        with self.assertRaises(scb.SliceError):
            scb.parse_slice_file("slices:\n  eng:\n    pins: a.md\n")

    def test_reject_non_string_pin(self):
        with self.assertRaises(scb.SliceError):
            scb.parse_slice_file("slices:\n  eng:\n    pins:\n      - 3\n")

    def test_reject_unknown_slice_key(self):
        with self.assertRaises(scb.SliceError):
            scb.parse_slice_file(
                "slices:\n  eng:\n    pins: [a.md]\n    bogus: 1\n")

    def test_reject_non_string_description(self):
        with self.assertRaises(scb.SliceError):
            scb.parse_slice_file(
                "slices:\n  eng:\n    pins: [a.md]\n    description: [1,2]\n")


class ResolveSelectionTest(unittest.TestCase):
    def _slice_file(self, tmp: Path) -> Path:
        p = tmp / "slices.yaml"
        p.write_text("slices:\n  eng:\n    pins: [a.md, b.md]\n", encoding="utf-8")
        return p

    def test_slice_only(self):
        with tempfile.TemporaryDirectory() as d:
            sf = self._slice_file(Path(d))
            self.assertEqual(scb.resolve_selection(sf, "eng", None), ["a.md", "b.md"])

    def test_pins_only(self):
        self.assertEqual(scb.resolve_selection(None, None, ["x.md", "y.md"]),
                         ["x.md", "y.md"])

    def test_union_and_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            sf = self._slice_file(Path(d))
            self.assertEqual(
                scb.resolve_selection(sf, "eng", ["b.md", "c.md"]),
                ["a.md", "b.md", "c.md"])

    def test_empty_selection_rejected(self):
        with self.assertRaises(scb.SliceError):
            scb.resolve_selection(None, None, None)

    def test_slice_without_file_rejected(self):
        with self.assertRaises(scb.SliceError):
            scb.resolve_selection(None, "eng", None)

    def test_unknown_slice_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            sf = self._slice_file(Path(d))
            with self.assertRaises(scb.SliceError):
                scb.resolve_selection(sf, "nope", None)


class StampAndHashTest(unittest.TestCase):
    def test_stamp_adds_provenance_and_preserves_source(self):
        src = source_pin(verified="2020-05-05")
        seeded = scb.stamp_seeded(
            src, exported_by="orchestrator", seeded_from="reference-fact-one.md",
            seeded_at="2026-09-28T00:00:00Z", seed_sha256="abc123")
        fields = scb._read_seeded_fields(seeded)
        self.assertEqual(fields[scb.SEEDED_BY], "orchestrator")
        self.assertEqual(fields[scb.SEEDED_FROM], "reference-fact-one.md")
        self.assertEqual(fields[scb.SEED_SHA256], "abc123")
        self.assertTrue(fields[scb.SEED_APPLIED_SHA256])
        # Source content preserved: name, nested type, verified verbatim, body.
        self.assertEqual(fields["name"], "reference-fact-one")
        self.assertEqual(fields["_type"], "reference")
        self.assertEqual(fields["metadata"]["verified"], "2020-05-05")
        self.assertIn("This is the body of the fact.", seeded)

    def test_applied_hash_commits_to_post_seed_content(self):
        seeded = scb.stamp_seeded(
            source_pin(), exported_by="orchestrator", seeded_from="p.md",
            seeded_at="2026-09-28T00:00:00Z", seed_sha256="deadbeef")
        recorded = scb._read_seeded_fields(seeded)[scb.SEED_APPLIED_SHA256]
        # Recomputing the applied hash on the untouched text matches the recorded one.
        self.assertEqual(scb.applied_hash(seeded), recorded)

    def test_applied_hash_changes_on_body_edit(self):
        seeded = scb.stamp_seeded(
            source_pin(), exported_by="orchestrator", seeded_from="p.md",
            seeded_at="2026-09-28T00:00:00Z", seed_sha256="deadbeef")
        edited = seeded + "\nlocal edit\n"
        self.assertNotEqual(scb.applied_hash(edited),
                            scb._read_seeded_fields(seeded)[scb.SEED_APPLIED_SHA256])

    def test_seeded_pin_lints_clean(self):
        with tempfile.TemporaryDirectory() as d:
            into = Path(d)
            seeded = scb.stamp_seeded(
                source_pin(), exported_by="orchestrator", seeded_from="reference-fact-one.md",
                seeded_at="2026-09-28T00:00:00Z", seed_sha256="abc")
            (into / "reference-fact-one.md").write_text(seeded, encoding="utf-8")
            self.assertEqual(lint_errors(into), [])


class StateMachineTest(unittest.TestCase):
    """decide_pin — each branch, and the write behavior via run_import in isolation."""

    def _bundle_with_one_pin(self, tmp: Path, content: str, filename="p.md") -> Path:
        bundle = tmp / "bundle"
        bundle.mkdir(parents=True)
        (bundle / filename).write_text(content, encoding="utf-8")
        import json
        manifest = {
            "schema_version": scb.SCHEMA_VERSION,
            "exported_by": "orchestrator",
            "exported_at": "2026-09-28T00:00:00Z",
            "slice": None,
            "pins": [{"filename": filename,
                      "source_sha256": scb.sha256_text(content)}],
        }
        (bundle / scb.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        return bundle

    def test_absent_seeds(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            bundle = self._bundle_with_one_pin(tmp, source_pin())
            into = tmp / "into"
            res = scb.run_import(bundle, into)
            self.assertEqual([r.status for r in res], [scb.SEEDED])
            self.assertTrue((into / "p.md").exists())

    def test_reimport_unchanged_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            bundle = self._bundle_with_one_pin(tmp, source_pin())
            into = tmp / "into"
            scb.run_import(bundle, into)
            before = (into / "p.md").read_text(encoding="utf-8")
            res = scb.run_import(bundle, into)
            self.assertEqual([r.status for r in res], [scb.UNCHANGED])
            self.assertEqual((into / "p.md").read_text(encoding="utf-8"), before)

    def test_source_change_updates_when_unedited(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            bundle = self._bundle_with_one_pin(tmp, source_pin(body="v1\n"))
            into = tmp / "into"
            scb.run_import(bundle, into)
            # New export of a changed source (new bundle, new sha).
            bundle2 = self._bundle_with_one_pin(
                tmp / "b2", source_pin(body="v2 changed\n"))
            res = scb.run_import(bundle2, into)
            self.assertEqual([r.status for r in res], [scb.UPDATED])
            self.assertIn("v2 changed", (into / "p.md").read_text(encoding="utf-8"))

    def test_local_edit_conflicts_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            bundle = self._bundle_with_one_pin(tmp, source_pin(body="v1\n"))
            into = tmp / "into"
            scb.run_import(bundle, into)
            target = into / "p.md"
            edited = target.read_text(encoding="utf-8") + "\nSEAT LOCAL EDIT\n"
            target.write_text(edited, encoding="utf-8")
            # Source ALSO changes — conflict still wins; never overwrite an edited copy.
            bundle2 = self._bundle_with_one_pin(tmp / "b2", source_pin(body="v2\n"))
            res = scb.run_import(bundle2, into)
            self.assertEqual([r.status for r in res], [scb.CONFLICT])
            self.assertEqual(target.read_text(encoding="utf-8"), edited)

    def test_seat_authored_same_name_collides(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            bundle = self._bundle_with_one_pin(tmp, source_pin())
            into = tmp / "into"
            into.mkdir()
            # A seat-authored pin with the same filename, NO seed provenance.
            seat_pin = source_pin(body="seat authored, do not touch\n")
            (into / "p.md").write_text(seat_pin, encoding="utf-8")
            res = scb.run_import(bundle, into)
            self.assertEqual([r.status for r in res], [scb.COLLISION])
            self.assertEqual((into / "p.md").read_text(encoding="utf-8"), seat_pin)

    def test_decide_pin_branches_directly(self):
        with tempfile.TemporaryDirectory() as d:
            into = Path(d)
            target = into / "p.md"
            src = source_pin()
            src_sha = scb.sha256_text(src)
            # absent
            self.assertEqual(scb.decide_pin(target, src_sha)[0], scb.SEEDED)
            # seed it
            seeded = scb.stamp_seeded(src, exported_by="orchestrator", seeded_from="p.md",
                                      seeded_at="t", seed_sha256=src_sha)
            target.write_text(seeded, encoding="utf-8")
            self.assertEqual(scb.decide_pin(target, src_sha)[0], scb.UNCHANGED)
            # source changed
            self.assertEqual(scb.decide_pin(target, "different-sha")[0], scb.UPDATED)
            # local edit
            target.write_text(seeded + "\nedit\n", encoding="utf-8")
            self.assertEqual(scb.decide_pin(target, src_sha)[0], scb.CONFLICT)
            # seat-authored (no provenance)
            target.write_text(src, encoding="utf-8")
            self.assertEqual(scb.decide_pin(target, src_sha)[0], scb.COLLISION)


class SafetyRegressionTest(unittest.TestCase):
    """REBOUND-1 blocking-gap regressions (RED against the pre-fix code)."""

    def _bundle(self, tmp: Path, content: str, filename="p.md", *, name="bundle"):
        import json
        bundle = tmp / name
        bundle.mkdir(parents=True)
        (bundle / filename).write_text(content, encoding="utf-8")
        manifest = {
            "schema_version": scb.SCHEMA_VERSION, "exported_by": "orchestrator",
            "exported_at": "t", "slice": None,
            "pins": [{"filename": filename,
                      "source_sha256": scb.sha256_text(content)}],
        }
        (bundle / scb.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        return bundle

    def test_body_edit_with_hash_token_is_a_conflict_not_overwrite(self):
        # BLOCKING-1: a seat edit that appends a `seed_applied_sha256:`-prefixed BODY
        # line must NOT be cancelled out by the strip → must CONFLICT, never overwrite.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            bundle = self._bundle(tmp, source_pin(body="v1\n"))
            into = tmp / "into"
            scb.run_import(bundle, into)
            target = into / "p.md"
            edited = (target.read_text(encoding="utf-8")
                      + "seed_applied_sha256: i-was-here\n")
            target.write_text(edited, encoding="utf-8")
            # Source changes too — CONFLICT must still win.
            bundle2 = self._bundle(tmp, source_pin(body="v2\n"), name="bundle2")
            res = scb.run_import(bundle2, into)
            self.assertEqual([r.status for r in res], [scb.CONFLICT])
            self.assertEqual(target.read_text(encoding="utf-8"), edited)

    def test_import_rejects_traversal_filename(self):
        # BLOCKING-2: `../ESCAPED.md` must be rejected loud, nothing written outside.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            bundle = self._bundle(tmp, source_pin(), filename="normal.md")
            import json
            manifest = json.loads(
                (bundle / scb.MANIFEST_NAME).read_text(encoding="utf-8"))
            manifest["pins"][0]["filename"] = "../ESCAPED.md"
            (bundle / scb.MANIFEST_NAME).write_text(
                json.dumps(manifest), encoding="utf-8")
            into = tmp / "seat" / "into"
            with self.assertRaises(ValueError):
                scb.run_import(bundle, into)
            self.assertFalse((tmp / "seat" / "ESCAPED.md").exists())
            self.assertFalse((into / "../ESCAPED.md").exists())

    def test_import_rejects_absolute_filename(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            bundle = self._bundle(tmp, source_pin(), filename="normal.md")
            import json
            escaped = str(tmp / "ABS_ESCAPED.md")
            manifest = json.loads(
                (bundle / scb.MANIFEST_NAME).read_text(encoding="utf-8"))
            manifest["pins"][0]["filename"] = escaped
            (bundle / scb.MANIFEST_NAME).write_text(
                json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                scb.run_import(bundle, tmp / "into")
            self.assertFalse(Path(escaped).exists())

    def test_export_rejects_traversal_selection(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            source = tmp / "src"
            source.mkdir()
            with self.assertRaises(ValueError):
                scb.run_export(source, tmp / "out", ["../ESCAPED.md"], None, "orchestrator")
            self.assertFalse((tmp / "ESCAPED.md").exists())

    def test_export_rejects_empty_body_source(self):
        # Finding 3: an empty-body source pin would seed a memory_lint ERROR.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            source = tmp / "src"
            source.mkdir()
            (source / "empty.md").write_text(
                "---\nname: x\ndescription: y\nmetadata:\n  type: reference\n---\n\n",
                encoding="utf-8")
            with self.assertRaises(ValueError):
                scb.run_export(source, tmp / "out", ["empty.md"], None, "orchestrator")


class IndexRewriteTest(unittest.TestCase):
    def _seeded(self, name, filename):
        return scb.stamp_seeded(
            source_pin(name=name), exported_by="orchestrator", seeded_from=filename,
            seeded_at="t", seed_sha256="s")

    def test_render_and_splice_append_when_absent(self):
        block = scb.render_index_block(
            [{"filename": "a.md", "name": "a", "seeded_from": "a.md",
              "seeded_by": "orchestrator"}])
        hand = "# MEMORY\n\nMy hand-written notes.\n"
        out = scb.splice_index_block(hand, block)
        self.assertIn("My hand-written notes.", out)
        self.assertIn(scb.INDEX_BEGIN, out)
        self.assertIn("[a](a.md)", out)

    def test_splice_is_idempotent(self):
        block = scb.render_index_block(
            [{"filename": "a.md", "name": "a", "seeded_from": "a.md",
              "seeded_by": "orchestrator"}])
        hand = "# MEMORY\n\nnotes\n"
        once = scb.splice_index_block(hand, block)
        twice = scb.splice_index_block(once, block)
        self.assertEqual(once, twice)

    def test_splice_preserves_handwritten_and_replaces_block(self):
        block1 = scb.render_index_block(
            [{"filename": "a.md", "name": "a", "seeded_from": "a.md",
              "seeded_by": "orchestrator"}])
        hand = "# MEMORY\n\nHAND LINE\n"
        out1 = scb.splice_index_block(hand, block1)
        block2 = scb.render_index_block(
            [{"filename": "b.md", "name": "b", "seeded_from": "b.md",
              "seeded_by": "orchestrator"}])
        out2 = scb.splice_index_block(out1, block2)
        self.assertIn("HAND LINE", out2)
        self.assertIn("[b](b.md)", out2)
        self.assertNotIn("[a](a.md)", out2)  # old seeded line replaced, not duplicated
        self.assertEqual(out2.count(scb.INDEX_BEGIN), 1)

    def test_refresh_index_over_store(self):
        with tempfile.TemporaryDirectory() as d:
            into = Path(d)
            (into / "MEMORY.md").write_text("# MEMORY\n\nkeep me\n", encoding="utf-8")
            (into / "a.md").write_text(self._seeded("a", "a.md"), encoding="utf-8")
            scb.refresh_index(into)
            text = (into / "MEMORY.md").read_text(encoding="utf-8")
            self.assertIn("keep me", text)
            self.assertIn("[a](a.md)", text)
            self.assertEqual(text.count(scb.INDEX_BEGIN), 1)


class IntegrationSeamTest(unittest.TestCase):
    """export → import → re-import no-op → source-change update → local-edit conflict
    → same-name collision — asserting memory_lint CLEAN on the imported store."""

    def test_full_seam(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            source = tmp / "orchestrator-mem"
            source.mkdir()
            (source / "reference-fact-one.md").write_text(
                source_pin(name="reference-fact-one", body="fact one v1\n"),
                encoding="utf-8")
            (source / "reference-fact-two.md").write_text(
                source_pin(name="reference-fact-two", body="fact two\n"),
                encoding="utf-8")
            slice_file = tmp / "slices.yaml"
            slice_file.write_text(
                "slices:\n  eng:\n    pins:\n"
                "      - reference-fact-one.md\n"
                "      - reference-fact-two.md\n",
                encoding="utf-8")

            # EXPORT (as "orchestrator")
            bundle = tmp / "bundle"
            selection = scb.resolve_selection(slice_file, "eng", None)
            manifest = scb.run_export(source, bundle, selection, "eng", "orchestrator")
            self.assertEqual(len(manifest["pins"]), 2)
            self.assertEqual(manifest["exported_by"], "orchestrator")

            # IMPORT into a fresh seat store (dir absent → defensively created)
            into = tmp / "seat" / "memory"
            (into.parent).mkdir()
            res1 = scb.run_import(bundle, into)
            self.assertEqual(sorted(r.status for r in res1),
                             [scb.SEEDED, scb.SEEDED])
            self.assertEqual(lint_errors(into), [])

            # RE-IMPORT same bundle → all no-op
            res2 = scb.run_import(bundle, into)
            self.assertEqual(set(r.status for r in res2), {scb.UNCHANGED})

            # SOURCE CHANGES for fact-one → re-export → import updates only it
            (source / "reference-fact-one.md").write_text(
                source_pin(name="reference-fact-one", body="fact one v2\n"),
                encoding="utf-8")
            bundle2 = tmp / "bundle2"
            scb.run_export(source, bundle2, selection, "eng", "orchestrator")
            res3 = scb.run_import(bundle2, into)
            by = {r.filename: r.status for r in res3}
            self.assertEqual(by["reference-fact-one.md"], scb.UPDATED)
            self.assertEqual(by["reference-fact-two.md"], scb.UNCHANGED)
            self.assertIn("fact one v2",
                          (into / "reference-fact-one.md").read_text(encoding="utf-8"))

            # LOCAL EDIT on fact-two → next import conflicts (never overwrites)
            two = into / "reference-fact-two.md"
            edited = two.read_text(encoding="utf-8") + "\nSEAT NOTE\n"
            two.write_text(edited, encoding="utf-8")
            (source / "reference-fact-two.md").write_text(
                source_pin(name="reference-fact-two", body="fact two v2\n"),
                encoding="utf-8")
            bundle3 = tmp / "bundle3"
            scb.run_export(source, bundle3, selection, "eng", "orchestrator")
            res4 = scb.run_import(bundle3, into)
            by4 = {r.filename: r.status for r in res4}
            self.assertEqual(by4["reference-fact-two.md"], scb.CONFLICT)
            self.assertEqual(two.read_text(encoding="utf-8"), edited)

            # SAME-NAME COLLISION: a seat-authored pin the bundle also carries
            (source / "reference-fact-three.md").write_text(
                source_pin(name="reference-fact-three", body="three\n"),
                encoding="utf-8")
            (into / "reference-fact-three.md").write_text(
                source_pin(name="reference-fact-three",
                           body="SEAT AUTHORED three\n"),
                encoding="utf-8")
            sel3 = ["reference-fact-three.md"]
            bundle4 = tmp / "bundle4"
            scb.run_export(source, bundle4, sel3, None, "orchestrator")
            res5 = scb.run_import(bundle4, into)
            self.assertEqual([r.status for r in res5], [scb.COLLISION])
            self.assertIn("SEAT AUTHORED",
                          (into / "reference-fact-three.md").read_text(encoding="utf-8"))

            # Final: memory_lint CLEAN (no ERRORs) on the whole imported store.
            self.assertEqual(lint_errors(into), [])
            # And the MEMORY.md seeded-context section lists the seeded pins.
            mem = (into / "MEMORY.md").read_text(encoding="utf-8")
            self.assertIn(scb.INDEX_BEGIN, mem)
            self.assertIn("[reference-fact-one](reference-fact-one.md)", mem)


if __name__ == "__main__":
    unittest.main()
