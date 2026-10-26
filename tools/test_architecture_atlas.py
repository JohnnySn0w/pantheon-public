"""Tests-first gate for T-3245 architecture atlas tooling."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import organism_topology
from tools.organism_topology import EvidenceSnapshot


class ArchitectureAtlasPlannedApiTests(unittest.TestCase):
    def test_planned_public_api_is_present(self) -> None:
        atlas = importlib.import_module("tools.architecture_atlas")
        expected = {
            "AtlasError",
            "AtlasViewKind",
            "AtlasIdentityKind",
            "AtlasAuthorityKind",
            "AtlasSourceClass",
            "AtlasContentRole",
            "AtlasRelationKind",
            "AtlasProjectionStatus",
            "AtlasIssue",
            "AtlasIdentityRef",
            "AtlasAuthorityRef",
            "AtlasFieldSelector",
            "SourcePin",
            "AtlasNodeRequest",
            "AtlasEdgeRequest",
            "AtlasStyle",
            "AtlasViewRequest",
            "AtlasProjection",
            "AtlasNode",
            "AtlasEdge",
            "AtlasViewSpec",
            "AtlasBundleEntry",
            "AtlasBundleManifest",
            "AtlasAuthorities",
            "load_atlas_authorities",
            "load_atlas_view_request",
            "load_atlas_style",
            "derive_atlas_view",
            "verify_atlas_view",
            "render_excalidraw",
            "generate_atlas_request_schema",
            "generate_atlas_view_schema",
            "generate_atlas_bundle_schema",
            "build_atlas_bundle",
            "verify_atlas_bundle",
            "write_bundle_atomic",
            "check_atlas_tooling",
            "main",
        }
        self.assertEqual(expected - set(dir(atlas)), set())


class ArchitectureAtlasJsonLoaderTests(unittest.TestCase):
    def test_load_json_object_accepts_valid_object(self) -> None:
        atlas = importlib.import_module("tools.architecture_atlas")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "valid.json"
            path.write_text('{"schema_version": 1, "name": "atlas"}', encoding="utf-8")

            self.assertEqual(
                atlas._load_json_object(path, max_bytes=128),
                {"schema_version": 1, "name": "atlas"},
            )

    def test_load_json_object_rejects_symlink(self) -> None:
        atlas = importlib.import_module("tools.architecture_atlas")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.json"
            link = root / "link.json"
            target.write_text('{"ok": true}', encoding="utf-8")
            link.symlink_to(target)

            with self.assertRaises(atlas.AtlasError):
                atlas._load_json_object(link, max_bytes=128)

    def test_load_json_object_rejects_oversized_input(self) -> None:
        atlas = importlib.import_module("tools.architecture_atlas")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oversized.json"
            path.write_text('{"long": "abcdef"}', encoding="utf-8")

            with self.assertRaises(atlas.AtlasError):
                atlas._load_json_object(path, max_bytes=8)

    def test_load_json_object_rejects_duplicate_keys(self) -> None:
        atlas = importlib.import_module("tools.architecture_atlas")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duplicate.json"
            path.write_text('{"same": 1, "same": 2}', encoding="utf-8")

            with self.assertRaises(atlas.AtlasError):
                atlas._load_json_object(path, max_bytes=128)

    def test_load_json_object_rejects_invalid_utf8(self) -> None:
        atlas = importlib.import_module("tools.architecture_atlas")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.json"
            path.write_bytes(b'{"bad": "\\xff"}\xff')

            with self.assertRaises(atlas.AtlasError):
                atlas._load_json_object(path, max_bytes=128)

    def test_load_json_object_rejects_non_object(self) -> None:
        atlas = importlib.import_module("tools.architecture_atlas")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "array.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")

            with self.assertRaises(atlas.AtlasError):
                atlas._load_json_object(path, max_bytes=128)


class ArchitectureAtlasViewRequestParsingTests(unittest.TestCase):
    def _atlas(self):
        return importlib.import_module("tools.architecture_atlas")

    def _request(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "view_id": "boundary-purpose",
            "view_kind": "boundary-purpose",
            "primary_question": "What boundary is protected?",
            "exclusions": ["runtime status"],
            "subject_refs": [{"kind": "organ", "id": "organ:harness-adapters"}],
            "supporting_refs": [
                {"kind": "binding", "id": "binding:harness-adapters-codex"}
            ],
            "nodes": [
                {
                    "node_id": "harness",
                    "identity_ref": {"kind": "organ", "id": "organ:harness-adapters"},
                    "role": "primary",
                    "selectors": [
                        {
                            "authority_ref": {
                                "kind": "organ",
                                "id": "organ:harness-adapters",
                            },
                            "field_path": "responsibility",
                        },
                        {
                            "authority_ref": {
                                "kind": "binding",
                                "id": "binding:harness-adapters-codex",
                            },
                            "field_path": "lifecycle",
                        },
                    ],
                    "rank": 0,
                    "order": 0,
                    "necessary_for": None,
                },
                {
                    "node_id": "adapter",
                    "identity_ref": {"kind": "organ", "id": "organ:harness-adapters"},
                    "role": "secondary",
                    "selectors": [
                        {
                            "authority_ref": {
                                "kind": "organ",
                                "id": "organ:harness-adapters",
                            },
                            "field_path": "boundaries.authority",
                        }
                    ],
                    "rank": 1,
                    "order": 0,
                    "necessary_for": "Clarifies authority boundary",
                },
            ],
            "edges": [
                {
                    "edge_id": "harness-adapter",
                    "source_node_id": "harness",
                    "target_node_id": "adapter",
                    "relation": "boundary",
                    "role": "primary",
                    "source_selectors": [],
                    "necessary_for": None,
                }
            ],
        }

    def _write_request(self, payload: dict[str, object]) -> object:
        atlas = self._atlas()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "request.json"
        path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return atlas, tmp, path

    def _assert_rejected(self, payload: dict[str, object]) -> None:
        atlas, tmp, path = self._write_request(payload)
        with tmp:
            with self.assertRaises(atlas.AtlasError):
                atlas.load_atlas_view_request(path)

    def test_load_atlas_view_request_accepts_valid_request(self) -> None:
        atlas, tmp, path = self._write_request(self._request())
        with tmp:
            request = atlas.load_atlas_view_request(path)

        self.assertEqual(request.view_id, "boundary-purpose")
        self.assertEqual(request.view_kind, atlas.AtlasViewKind.BOUNDARY_PURPOSE)
        self.assertEqual(request.subject_refs[0].kind, atlas.AtlasIdentityKind.ORGAN)
        self.assertEqual(request.supporting_refs[0].kind, atlas.AtlasAuthorityKind.BINDING)
        self.assertEqual(request.supporting_refs[0].id, "binding:harness-adapters-codex")
        self.assertFalse(hasattr(request.supporting_refs[0], "path"))
        self.assertFalse(hasattr(request.supporting_refs[0], "digest"))
        self.assertEqual(
            request.nodes[0].selectors[0].authority_ref,
            atlas.AtlasAuthorityRef(atlas.AtlasAuthorityKind.ORGAN, "organ:harness-adapters"),
        )
        self.assertEqual(len(request.nodes), 2)
        self.assertEqual(request.edges[0].relation, atlas.AtlasRelationKind.BOUNDARY)

    def test_load_atlas_view_request_rejects_unknown_top_level_key(self) -> None:
        payload = self._request()
        payload["unexpected"] = True

        self._assert_rejected(payload)

    def test_load_atlas_view_request_rejects_unsupported_schema_version(self) -> None:
        payload = self._request()
        payload["schema_version"] = 2

        self._assert_rejected(payload)

    def test_load_atlas_view_request_rejects_bad_view_id_and_enum(self) -> None:
        bad_id = self._request()
        bad_id["view_id"] = "Boundary Purpose"
        self._assert_rejected(bad_id)

        bad_kind = self._request()
        bad_kind["view_kind"] = "poster"
        self._assert_rejected(bad_kind)

    def test_load_atlas_view_request_rejects_question_and_exclusion_bounds(self) -> None:
        no_question_mark = self._request()
        no_question_mark["primary_question"] = "No question mark"
        self._assert_rejected(no_question_mark)

        two_question_marks = self._request()
        two_question_marks["primary_question"] = "One? Two?"
        self._assert_rejected(two_question_marks)

        long_question = self._request()
        long_question["primary_question"] = ("a" * 240) + "?"
        self._assert_rejected(long_question)

        duplicate_exclusions = self._request()
        duplicate_exclusions["exclusions"] = ["x", "x"]
        self._assert_rejected(duplicate_exclusions)

    def test_load_atlas_view_request_rejects_ref_bounds_and_duplicates(self) -> None:
        no_subject = self._request()
        no_subject["subject_refs"] = []
        self._assert_rejected(no_subject)

        duplicate_subject = self._request()
        duplicate_subject["subject_refs"] = [
            {"kind": "organ", "id": "organ:harness-adapters"},
            {"kind": "organ", "id": "organ:harness-adapters"},
        ]
        self._assert_rejected(duplicate_subject)

        too_many_supporting = self._request()
        too_many_supporting["supporting_refs"] = [
            {"kind": "binding", "id": f"binding:item-{index}"}
            for index in range(25)
        ]
        self._assert_rejected(too_many_supporting)

    def test_load_atlas_view_request_rejects_node_bounds_slots_and_necessary_for(self) -> None:
        bool_rank = self._request()
        bool_rank["nodes"][0]["rank"] = True
        self._assert_rejected(bool_rank)

        duplicate_slot = self._request()
        duplicate_slot["nodes"][1]["rank"] = 0
        duplicate_slot["nodes"][1]["order"] = 0
        self._assert_rejected(duplicate_slot)

        secondary_without_reason = self._request()
        secondary_without_reason["nodes"][1]["necessary_for"] = None
        self._assert_rejected(secondary_without_reason)

        primary_with_reason = self._request()
        primary_with_reason["nodes"][0]["necessary_for"] = "extra"
        self._assert_rejected(primary_with_reason)

        too_many_selectors = self._request()
        too_many_selectors["nodes"][0]["selectors"] = too_many_selectors["nodes"][0]["selectors"] * 7
        self._assert_rejected(too_many_selectors)

    def test_load_atlas_view_request_rejects_edge_reference_and_bounds(self) -> None:
        missing_node = self._request()
        missing_node["edges"][0]["target_node_id"] = "missing"
        self._assert_rejected(missing_node)

        duplicate_edge = self._request()
        duplicate_edge["edges"].append(dict(duplicate_edge["edges"][0]))
        self._assert_rejected(duplicate_edge)

        secondary_without_reason = self._request()
        secondary_without_reason["edges"][0]["role"] = "secondary"
        self._assert_rejected(secondary_without_reason)

        too_many_edges = self._request()
        too_many_edges["edges"] = [
            {
                "edge_id": f"edge-{index}",
                "source_node_id": "harness",
                "target_node_id": "adapter",
                "relation": "boundary",
                "role": "primary",
                "source_selectors": [],
                "necessary_for": None,
            }
            for index in range(25)
        ]
        self._assert_rejected(too_many_edges)

    def test_load_atlas_view_request_rejects_unresolved_selector_authority(self) -> None:
        payload = self._request()
        payload["nodes"][0]["selectors"][0]["authority_ref"] = {
            "kind": "component",
            "id": "component:not-supported",
        }

        self._assert_rejected(payload)

    def test_load_atlas_view_request_accepts_supporting_ref_selector_match(self) -> None:
        atlas, tmp, path = self._write_request(self._request())
        with tmp:
            request = atlas.load_atlas_view_request(path)

        self.assertEqual(
            request.nodes[0].selectors[1].authority_ref,
            request.supporting_refs[0],
        )

    def test_load_atlas_view_request_rejects_wrong_authority_id(self) -> None:
        payload = self._request()
        payload["nodes"][0]["selectors"][0]["authority_ref"] = {
            "kind": "organ",
            "id": "organ:not-a-subject",
        }

        self._assert_rejected(payload)

    def test_load_atlas_view_request_rejects_request_source_metadata(self) -> None:
        supporting = self._request()
        supporting["supporting_refs"][0] = {
            "kind": "binding",
            "id": "binding:harness-adapters-codex",
            "path": "organism/bindings/harness-adapters-codex.yaml",
            "digest": "sha256:" + ("1" * 64),
        }
        self._assert_rejected(supporting)

        selector = self._request()
        selector["nodes"][0]["selectors"][0]["authority_ref"] = {
            "kind": "organ",
            "id": "organ:harness-adapters",
            "path": "organism/organs/harness-adapters.yaml",
            "digest": "sha256:" + ("1" * 64),
        }
        self._assert_rejected(selector)


class ArchitectureAtlasStyleParsingTests(unittest.TestCase):
    def _atlas(self):
        return importlib.import_module("tools.architecture_atlas")

    def _style(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "canvas_width": 3600,
            "canvas_height": 2400,
            "background_color": "#ffffff",
            "primary_fill_color": "#DDEEFF",
            "secondary_fill_color": "#eeeeee",
            "stroke_color": "#111111",
            "text_color": "#000000",
            "font_family": 1,
            "font_size": 16,
            "stroke_width": 2,
            "rank_gap": 120,
            "order_gap": 80,
            "node_width": 300,
            "node_height": 120,
        }

    def _write_style(self, payload: dict[str, object]) -> object:
        atlas = self._atlas()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "style.json"
        path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return atlas, tmp, path

    def _assert_style_rejected(self, payload: dict[str, object]) -> None:
        atlas, tmp, path = self._write_style(payload)
        with tmp:
            with self.assertRaises(atlas.AtlasError):
                atlas.load_atlas_style(path)

    def test_load_atlas_style_accepts_valid_style(self) -> None:
        atlas, tmp, path = self._write_style(self._style())
        with tmp:
            style = atlas.load_atlas_style(path)

        self.assertEqual(style.schema_version, 1)
        self.assertEqual(style.canvas_width, 3600)
        self.assertEqual(style.canvas_height, 2400)
        self.assertEqual(style.primary_fill_color, "#DDEEFF")
        self.assertEqual(style.font_family, 1)

    def test_load_atlas_style_rejects_unknown_key_schema_and_canvas(self) -> None:
        unknown = self._style()
        unknown["extra"] = "no"
        self._assert_style_rejected(unknown)

        schema = self._style()
        schema["schema_version"] = 2
        self._assert_style_rejected(schema)

        width = self._style()
        width["canvas_width"] = 3599
        self._assert_style_rejected(width)

        height = self._style()
        height["canvas_height"] = 2401
        self._assert_style_rejected(height)

    def test_load_atlas_style_rejects_color_and_font_values(self) -> None:
        bad_color = self._style()
        bad_color["text_color"] = "#fffff"
        self._assert_style_rejected(bad_color)

        bad_font = self._style()
        bad_font["font_family"] = 4
        self._assert_style_rejected(bad_font)

        string_font = self._style()
        string_font["font_family"] = "Virgil"
        self._assert_style_rejected(string_font)

    def test_load_atlas_style_rejects_non_bool_exact_int_types(self) -> None:
        for key in (
            "schema_version",
            "canvas_width",
            "font_family",
            "font_size",
            "stroke_width",
            "rank_gap",
            "order_gap",
            "node_width",
            "node_height",
        ):
            with self.subTest(key=key):
                payload = self._style()
                payload[key] = True
                self._assert_style_rejected(payload)

    def test_load_atlas_style_rejects_numeric_bounds(self) -> None:
        cases = (
            ("font_size", 7),
            ("font_size", 65),
            ("stroke_width", 0),
            ("stroke_width", 9),
            ("rank_gap", 19),
            ("rank_gap", 1001),
            ("order_gap", 19),
            ("order_gap", 1001),
            ("node_width", 119),
            ("node_width", 1201),
            ("node_height", 59),
            ("node_height", 801),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                payload = self._style()
                payload[key] = value
                self._assert_style_rejected(payload)

    def test_load_atlas_style_rejects_grid_overflow(self) -> None:
        width = self._style()
        width["node_width"] = 600
        width["rank_gap"] = 1000
        self._assert_style_rejected(width)

        height = self._style()
        height["node_height"] = 400
        height["order_gap"] = 1000
        self._assert_style_rejected(height)


class ArchitectureAtlasAuthorityLoadingTests(unittest.TestCase):
    def _atlas(self):
        return importlib.import_module("tools.architecture_atlas")

    def test_load_atlas_authorities_accepts_current_live_base(self) -> None:
        atlas = self._atlas()

        authorities = atlas.load_atlas_authorities(Path("."), frozenset())

        organ_ref = atlas.AtlasAuthorityRef(
            atlas.AtlasAuthorityKind.ORGAN, "organ:harness-adapters"
        )
        binding_ref = atlas.AtlasAuthorityRef(
            atlas.AtlasAuthorityKind.BINDING, "binding:harness-adapters-codex"
        )
        stage_ref = atlas.AtlasAuthorityRef(
            atlas.AtlasAuthorityKind.STAGE, "ORG-061:STAGE-1"
        )
        obligation_ref = atlas.AtlasAuthorityRef(
            atlas.AtlasAuthorityKind.OBLIGATION, "ORG-053:R1"
        )

        self.assertIn(organ_ref, authorities.records)
        self.assertIn(binding_ref, authorities.records)
        self.assertIn(stage_ref, authorities.records)
        self.assertIn(obligation_ref, authorities.records)
        self.assertEqual(
            authorities.record_paths[organ_ref],
            "organism/organs/harness-adapters.yaml",
        )
        self.assertEqual(
            authorities.record_paths[binding_ref],
            "organism/bindings/harness-adapters-codex.yaml",
        )
        self.assertEqual(
            authorities.record_paths[stage_ref],
            "tickets/grand-project-stage-evidence.yaml",
        )
        self.assertEqual(
            authorities.record_paths[obligation_ref],
            "tickets/grand-project-reevaluation-obligations.yaml",
        )
        self.assertEqual(authorities.evidence, EvidenceSnapshot.empty())
        self.assertIsNotNone(authorities.now.tzinfo)

    def test_load_atlas_authorities_records_exact_digest_families(self) -> None:
        atlas = self._atlas()

        authorities = atlas.load_atlas_authorities(Path("."), frozenset())

        self.assertEqual(
            authorities.source_sha256["organism/organs/harness-adapters.yaml"],
            "sha256:" + __import__("hashlib").sha256(
                authorities.source_bytes["organism/organs/harness-adapters.yaml"]
            ).hexdigest(),
        )
        self.assertEqual(
            authorities.generated_index_sha256["organism/index.json"],
            "sha256:" + __import__("hashlib").sha256(
                authorities.generated_index_bytes["organism/index.json"]
            ).hexdigest(),
        )
        self.assertEqual(
            authorities.authority_digests[atlas.AtlasAuthorityKind.ORGAN],
            authorities.generated_index_sha256["organism/index.json"],
        )
        self.assertEqual(
            authorities.authority_digests[atlas.AtlasAuthorityKind.BINDING],
            authorities.generated_index_sha256["organism/index.json"],
        )
        self.assertEqual(
            authorities.authority_digests[atlas.AtlasAuthorityKind.STAGE],
            authorities.source_sha256["tickets/grand-project-stage-evidence.yaml"],
        )
        self.assertEqual(
            authorities.authority_digests[atlas.AtlasAuthorityKind.OBLIGATION],
            authorities.source_sha256[
                "tickets/grand-project-reevaluation-obligations.yaml"
            ],
        )

    def test_load_atlas_authorities_passes_explicit_goal_keys_to_project_checker(self) -> None:
        atlas = self._atlas()

        with mock.patch.object(
            atlas.project_catalogue, "check_project_catalogue", return_value=()
        ) as project_check, mock.patch.object(
            atlas.component_catalogue, "check_component_catalogue", return_value=()
        ), mock.patch.object(
            atlas.organism_topology, "check_organism_topology", return_value=()
        ), mock.patch.object(
            atlas.grand_project_program, "lint_repository_indexes", return_value=()
        ), mock.patch.object(
            atlas.project_catalogue, "load_project_records", return_value={}
        ), mock.patch.object(
            atlas.component_catalogue, "load_component_records", return_value={}
        ), mock.patch.object(
            atlas.organism_topology, "load_organism_topology"
        ) as load_topology, mock.patch.object(
            atlas.organism_topology, "build_reference_indexes", return_value=object()
        ), mock.patch.object(
            atlas.grand_project_program, "load_stage_index", return_value=()
        ), mock.patch.object(
            atlas.grand_project_program, "load_obligation_index", return_value=()
        ):
            load_topology.return_value = type(
                "Topology",
                (),
                {"organs": {}, "bindings": (), "dependencies": (), "surfaces": ()},
            )()
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "projects").mkdir()
                (root / "components").mkdir()
                (root / "organism").mkdir()
                (root / "tickets").mkdir()
                for rel in (
                    "projects/index.json",
                    "components/index.json",
                    "organism/index.json",
                    "tickets/grand-project-stage-evidence.yaml",
                    "tickets/grand-project-reevaluation-obligations.yaml",
                ):
                    path = root / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("authority\n", encoding="utf-8")

                atlas.load_atlas_authorities(root, frozenset({"synthetic-goal"}))

        project_check.assert_called_once_with(root, frozenset({"synthetic-goal"}))

    def test_load_atlas_authorities_refuses_validator_findings(self) -> None:
        atlas = self._atlas()
        issue = type("Issue", (), {"code": "index-stale", "project_id": "*", "detail": "bad"})()

        with mock.patch.object(
            atlas.project_catalogue, "check_project_catalogue", return_value=(issue,)
        ):
            with self.assertRaises(atlas.AtlasError):
                atlas.load_atlas_authorities(Path("."), frozenset())

    def test_load_atlas_authorities_refuses_stale_index(self) -> None:
        atlas = self._atlas()
        issue = type(
            "Issue",
            (),
            {"code": "index-stale", "component_id": "*", "detail": "generated component index bytes differ"},
        )()

        with mock.patch.object(
            atlas.project_catalogue, "check_project_catalogue", return_value=()
        ), mock.patch.object(
            atlas.component_catalogue, "check_component_catalogue", return_value=(issue,)
        ):
            with self.assertRaises(atlas.AtlasError):
                atlas.load_atlas_authorities(Path("."), frozenset())

    def test_load_atlas_authorities_refuses_symlink_source(self) -> None:
        atlas = self._atlas()
        organ = type("Organ", (), {"organ_id": "organ:test"})()
        topology = type(
            "Topology",
            (),
            {"organs": {"organ:test": organ}, "bindings": (), "dependencies": (), "surfaces": ()},
        )()

        with mock.patch.object(
            atlas.project_catalogue, "check_project_catalogue", return_value=()
        ), mock.patch.object(
            atlas.component_catalogue, "check_component_catalogue", return_value=()
        ), mock.patch.object(
            atlas.organism_topology, "check_organism_topology", return_value=()
        ), mock.patch.object(
            atlas.grand_project_program, "lint_repository_indexes", return_value=()
        ), mock.patch.object(
            atlas.project_catalogue, "load_project_records", return_value={}
        ), mock.patch.object(
            atlas.component_catalogue, "load_component_records", return_value={}
        ), mock.patch.object(
            atlas.organism_topology, "load_organism_topology", return_value=topology
        ), mock.patch.object(
            atlas.organism_topology, "build_reference_indexes", return_value=object()
        ), mock.patch.object(
            atlas.grand_project_program, "load_stage_index", return_value=()
        ), mock.patch.object(
            atlas.grand_project_program, "load_obligation_index", return_value=()
        ):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for rel in (
                    "projects/index.json",
                    "components/index.json",
                    "organism/index.json",
                    "tickets/grand-project-stage-evidence.yaml",
                    "tickets/grand-project-reevaluation-obligations.yaml",
                ):
                    path = root / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("authority\n", encoding="utf-8")
                organ_path = root / "organism" / "organs" / "test.yaml"
                organ_path.parent.mkdir(parents=True, exist_ok=True)
                target = root / "target.yaml"
                target.write_text("schema_version: 1\n", encoding="utf-8")
                organ_path.symlink_to(target)

                with self.assertRaises(atlas.AtlasError):
                    atlas.load_atlas_authorities(root, frozenset())


class ArchitectureAtlasSelectorMechanicsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.atlas = importlib.import_module("tools.architecture_atlas")
        cls.authorities = cls.atlas.load_atlas_authorities(Path("."), frozenset())

    def _selector(self, kind: object, record_id: str, field_path: str) -> object:
        return self.atlas.AtlasFieldSelector(
            self.atlas.AtlasAuthorityRef(kind, record_id), field_path
        )

    def test_projection_status_values_are_exact(self) -> None:
        self.assertEqual(self.atlas.AtlasProjectionStatus.DERIVED.value, "derived")
        self.assertEqual(self.atlas.AtlasProjectionStatus.DECLARED.value, "declared")
        self.assertEqual(
            self.atlas.AtlasProjectionStatus.EVIDENCE_PROVEN.value, "evidence-proven"
        )
        self.assertEqual(self.atlas.AtlasProjectionStatus.UNKNOWN.value, "unknown")
        self.assertFalse(hasattr(self.atlas.AtlasProjectionStatus, "EVIDENCE_GATED"))

    def test_build_ordinary_projection_accepts_live_organ_binding_stage_obligation(self) -> None:
        cases = (
            (
                self.atlas.AtlasAuthorityKind.ORGAN,
                "organ:harness-adapters",
                "responsibility",
                "organism/organs/harness-adapters.yaml",
                self.atlas.AtlasProjectionStatus.DERIVED,
            ),
            (
                self.atlas.AtlasAuthorityKind.BINDING,
                "binding:harness-adapters-codex",
                "lifecycle",
                "organism/bindings/harness-adapters-codex.yaml",
                self.atlas.AtlasProjectionStatus.DECLARED,
            ),
            (
                self.atlas.AtlasAuthorityKind.STAGE,
                "ORG-061:STAGE-1",
                "state",
                "tickets/grand-project-stage-evidence.yaml",
                self.atlas.AtlasProjectionStatus.DECLARED,
            ),
            (
                self.atlas.AtlasAuthorityKind.OBLIGATION,
                "ORG-053:R1",
                "owner_ref",
                "tickets/grand-project-reevaluation-obligations.yaml",
                self.atlas.AtlasProjectionStatus.DERIVED,
            ),
        )
        for kind, record_id, field_path, rel_path, status in cases:
            with self.subTest(kind=kind, field_path=field_path):
                selector = self._selector(kind, record_id, field_path)

                projection = self.atlas._build_ordinary_projection(
                    selector, self.authorities
                )

                self.assertEqual(projection.selector, selector)
                self.assertEqual(projection.status, status)
                self.assertEqual(len(projection.source_pins), 1)
                pin = projection.source_pins[0]
                self.assertEqual(pin.source_class, self.atlas.AtlasSourceClass.REPO_DOCUMENT)
                self.assertEqual(pin.authority_path, rel_path)
                self.assertEqual(pin.document_sha256, self.authorities.source_sha256[rel_path])
                self.assertEqual(pin.authority_digest, self.authorities.authority_digests[kind])
                self.assertEqual(pin.field_path, field_path)
                self.assertTrue(pin.canonical_ref.endswith("#" + field_path))

    def test_build_ordinary_projection_empty_repeated_leaf_returns_empty_tuple(self) -> None:
        selector = self._selector(
            self.atlas.AtlasAuthorityKind.ORGAN,
            "organ:harness-adapters",
            "ports.inputs.port_id",
        )

        projection = self.atlas._build_ordinary_projection(selector, self.authorities)

        self.assertEqual(projection.value, ())

    def test_build_ordinary_projection_binding_health_facet_stops_for_later_slice(self) -> None:
        selector = self._selector(
            self.atlas.AtlasAuthorityKind.BINDING,
            "binding:harness-adapters-codex",
            "health.facets.liveness",
        )

        with self.assertRaises(self.atlas.AtlasError):
            self.atlas._build_ordinary_projection(selector, self.authorities)

    def _synthetic_authorities(self, ref: object, record: object) -> object:
        return self.atlas.AtlasAuthorities(
            records={ref: record},
            record_paths={ref: "synthetic.yaml"},
            source_bytes={"synthetic.yaml": b"synthetic"},
            source_sha256={"synthetic.yaml": "sha256:" + ("2" * 64)},
            generated_index_bytes={"synthetic/index.json": b"index"},
            generated_index_sha256={"synthetic/index.json": "sha256:" + ("3" * 64)},
            authority_digests={ref.kind: "sha256:" + ("3" * 64)},
            evidence=EvidenceSnapshot.empty(),
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )

    def test_project_repeated_nullable_and_declared_status_projection(self) -> None:
        @dataclass(frozen=True)
        class Lifecycle:
            developmental_mode: str
            formed_at: str
            transition_refs: tuple[str, ...]

        @dataclass(frozen=True)
        class Tension:
            pole_a: str | None
            pole_b: str
            rebalance_trigger: str

        @dataclass(frozen=True)
        class Project:
            lifecycle: Lifecycle
            tensions: tuple[Tension, ...]

        ref = self.atlas.AtlasAuthorityRef(self.atlas.AtlasAuthorityKind.PROJECT, "demo")
        authorities = self._synthetic_authorities(
            ref,
            Project(
                lifecycle=Lifecycle("shaping", "2026-08-27T00:00:00Z", ()),
                tensions=(
                    Tension("one", "two", "rebalance"),
                    Tension(None, "four", "review"),
                ),
            ),
        )

        repeated = self.atlas._build_ordinary_projection(
            self.atlas.AtlasFieldSelector(ref, "tensions.pole_a"), authorities
        )
        declared = self.atlas._build_ordinary_projection(
            self.atlas.AtlasFieldSelector(ref, "lifecycle.developmental_mode"),
            authorities,
        )

        self.assertEqual(repeated.value, ("one", None))
        self.assertEqual(declared.value, "shaping")
        self.assertEqual(declared.status, self.atlas.AtlasProjectionStatus.DECLARED)

    def test_component_aliases_flatten_repeated_leaves(self) -> None:
        @dataclass(frozen=True)
        class Requirement:
            capability: str
            contract: str
            schema: str
            version: str
            feature: str | None
            enabled: bool
            degradation_ref: str | None

        @dataclass(frozen=True)
        class Component:
            hard_requirements: tuple[Requirement, ...]
            optional_requirements: tuple[Requirement, ...]

        ref = self.atlas.AtlasAuthorityRef(
            self.atlas.AtlasAuthorityKind.COMPONENT, "component:demo"
        )
        authorities = self._synthetic_authorities(
            ref,
            Component(
                hard_requirements=(
                    Requirement("cap:a", "contract:a", "schema:a", "1.0.0", None, True, None),
                    Requirement("cap:b", "contract:b", "schema:b", "1.0.0", "fast", False, "deg:b"),
                ),
                optional_requirements=(),
            ),
        )

        features = self.atlas._build_ordinary_projection(
            self.atlas.AtlasFieldSelector(ref, "requires.hard.feature"), authorities
        )
        empty = self.atlas._build_ordinary_projection(
            self.atlas.AtlasFieldSelector(ref, "requires.optional.capability"), authorities
        )

        self.assertEqual(features.value, (None, "fast"))
        self.assertEqual(empty.value, ())

    def test_selector_rejects_private_path_digest_nested_mapping_and_arbitrary_fields(self) -> None:
        selector_cases = (
            self._selector(
                self.atlas.AtlasAuthorityKind.COMPONENT,
                "component:not-loaded",
                "source.path",
            ),
            self._selector(
                self.atlas.AtlasAuthorityKind.ORGAN,
                "organ:harness-adapters",
                "source_sha256",
            ),
            self._selector(
                self.atlas.AtlasAuthorityKind.ORGAN,
                "organ:harness-adapters",
                "responsibility.extra",
            ),
        )
        for selector in selector_cases:
            with self.subTest(field_path=selector.field_path):
                with self.assertRaises(self.atlas.AtlasError):
                    self.atlas._build_ordinary_projection(selector, self.authorities)

        @dataclass(frozen=True)
        class Organ:
            responsibility: object

        ref = self.atlas.AtlasAuthorityRef(self.atlas.AtlasAuthorityKind.ORGAN, "organ:map")
        authorities = self._synthetic_authorities(ref, Organ({"private": "mapping"}))
        with self.assertRaises(self.atlas.AtlasError):
            self.atlas._build_ordinary_projection(
                self.atlas.AtlasFieldSelector(ref, "responsibility"), authorities
            )


class ArchitectureAtlasHealthProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.atlas = importlib.import_module("tools.architecture_atlas")
        cls.live = cls.atlas.load_atlas_authorities(Path("."), frozenset())
        cls.binding_ref = cls.atlas.AtlasAuthorityRef(
            cls.atlas.AtlasAuthorityKind.BINDING, "binding:harness-adapters-codex"
        )
        cls.organ_ref = cls.atlas.AtlasAuthorityRef(
            cls.atlas.AtlasAuthorityKind.ORGAN, "organ:harness-adapters"
        )

    def _selector(self, facet: str = "liveness") -> object:
        return self.atlas.AtlasFieldSelector(
            self.binding_ref, f"health.facets.{facet}"
        )

    def _evidence(
        self,
        evidence_id: str,
        *,
        result: object,
        kind: object = organism_topology.EvidenceKind.PROBE,
        subject_id: str = "binding:harness-adapters-codex",
        facet: object = organism_topology.HealthFacet.LIVENESS,
        observed_at: str = "2026-08-27T00:00:00Z",
        fresh_until: str | None = "2026-08-28T00:00:00Z",
        sha: str | None = None,
    ) -> object:
        return organism_topology.EvidenceRef(
            schema_version=1,
            evidence_id=evidence_id,
            subject_ref=organism_topology.EvidenceSubjectRef(
                organism_topology.EvidenceSubjectKind.BINDING,
                subject_id,
            ),
            facet=facet,
            evidence_kind=kind,
            canonical_ref=f"evidence:{evidence_id}",
            sha256=sha or "sha256:" + (evidence_id[-1] * 64),
            observer_ref="role:orchestrator",
            observed_at=observed_at,
            fresh_until=fresh_until,
            result=result,
        )

    def _authorities_with_evidence(
        self, records: tuple[object, ...], *, lifecycle: object | None = None
    ) -> object:
        binding = self.live.records[self.binding_ref]
        if lifecycle is not None:
            binding = replace(binding, lifecycle=lifecycle)
        binding = replace(binding, evidence_refs=tuple(record.evidence_id for record in records))
        evidence = EvidenceSnapshot(
            records={record.evidence_id: record for record in records},
            canonical_ref_sha256={
                record.canonical_ref: record.sha256 for record in records
            },
        )
        records_map = dict(self.live.records)
        records_map[self.binding_ref] = binding
        return self.atlas.AtlasAuthorities(
            records=records_map,
            record_paths=self.live.record_paths,
            source_bytes=self.live.source_bytes,
            source_sha256=self.live.source_sha256,
            generated_index_bytes=self.live.generated_index_bytes,
            generated_index_sha256=self.live.generated_index_sha256,
            authority_digests=self.live.authority_digests,
            evidence=evidence,
            now=datetime(2026, 8, 27, 12, tzinfo=timezone.utc),
        )

    def _expected_evidence_digest(self, record: object) -> str:
        payload = {
            "schema_version": record.schema_version,
            "evidence_id": record.evidence_id,
            "subject_ref": {
                "kind": record.subject_ref.kind.value,
                "id": record.subject_ref.id,
            },
            "facet": record.facet.value,
            "evidence_kind": record.evidence_kind.value,
            "canonical_ref": record.canonical_ref,
            "sha256": record.sha256,
            "observer_ref": record.observer_ref,
            "observed_at": record.observed_at,
            "fresh_until": record.fresh_until,
            "result": record.result.value,
        }
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return "sha256:" + __import__("hashlib").sha256(raw).hexdigest()

    def test_health_projection_empty_snapshot_is_unknown_with_binding_and_organ_pins(self) -> None:
        projection = self.atlas._build_health_projection(self._selector(), self.live)

        self.assertEqual(projection.value, "unknown")
        self.assertEqual(projection.status, self.atlas.AtlasProjectionStatus.UNKNOWN)
        self.assertEqual([pin.field_path for pin in projection.source_pins], ["evidence_refs", "health.required_facets"])
        self.assertEqual(projection.source_pins[0].authority_path, "organism/bindings/harness-adapters-codex.yaml")
        self.assertEqual(projection.source_pins[1].authority_path, "organism/organs/harness-adapters.yaml")
        self.assertTrue(all(pin.source_class is self.atlas.AtlasSourceClass.REPO_DOCUMENT for pin in projection.source_pins))

    def test_health_projection_passing_evidence_can_be_healthy(self) -> None:
        record = self._evidence("ev-pass", result=organism_topology.EvidenceResult.PASS)
        authorities = self._authorities_with_evidence((record,))

        projection = self.atlas._build_health_projection(self._selector(), authorities)

        self.assertEqual(projection.value, "healthy")
        self.assertEqual(projection.status, self.atlas.AtlasProjectionStatus.EVIDENCE_PROVEN)
        self.assertEqual([pin.field_path for pin in projection.source_pins], ["evidence_refs", "health.required_facets", "evidence:ev-pass/liveness"])
        self.assertEqual(projection.source_pins[2].source_class, self.atlas.AtlasSourceClass.EVIDENCE_SNAPSHOT)
        self.assertIsNone(projection.source_pins[2].authority_path)
        self.assertEqual(projection.source_pins[2].canonical_ref, record.canonical_ref)
        self.assertEqual(projection.source_pins[2].document_sha256, record.sha256)
        self.assertEqual(projection.source_pins[2].authority_digest, self._expected_evidence_digest(record))

    def test_health_projection_reports_fail_stale_and_ambiguous(self) -> None:
        cases = (
            (
                self._evidence("ev-fail", result=organism_topology.EvidenceResult.FAIL),
                "failed",
                self.atlas.AtlasProjectionStatus.EVIDENCE_PROVEN,
            ),
            (
                self._evidence(
                    "ev-stale",
                    result=organism_topology.EvidenceResult.PASS,
                    fresh_until="2026-08-26T00:00:00Z",
                ),
                "stale",
                self.atlas.AtlasProjectionStatus.EVIDENCE_PROVEN,
            ),
            (
                self._evidence("ev-ambiguous", result=organism_topology.EvidenceResult.AMBIGUOUS),
                "unknown",
                self.atlas.AtlasProjectionStatus.UNKNOWN,
            ),
        )
        for record, value, status in cases:
            with self.subTest(record=record.evidence_id):
                projection = self.atlas._build_health_projection(
                    self._selector(), self._authorities_with_evidence((record,))
                )

                self.assertEqual(projection.value, value)
                self.assertEqual(projection.status, status)
                self.assertEqual(projection.source_pins[2].field_path, f"evidence:{record.evidence_id}/liveness")

    def test_health_projection_complete_sorted_multi_pin_set(self) -> None:
        ignored = self._evidence(
            "ev-ignored",
            result=organism_topology.EvidenceResult.PASS,
            facet=organism_topology.HealthFacet.AUTHORITY,
            sha="sha256:" + ("9" * 64),
        )
        second = self._evidence("ev-b", result=organism_topology.EvidenceResult.FAIL, sha="sha256:" + ("b" * 64))
        first = self._evidence("ev-a", result=organism_topology.EvidenceResult.PASS, sha="sha256:" + ("a" * 64))
        authorities = self._authorities_with_evidence((ignored, second, first))

        projection = self.atlas._build_projection(self._selector(), authorities)

        self.assertEqual(projection.value, "inconsistent")
        self.assertEqual(
            [pin.field_path for pin in projection.source_pins],
            [
                "evidence_refs",
                "health.required_facets",
                "evidence:ev-a/liveness",
                "evidence:ev-b/liveness",
            ],
        )

    def test_live_lifecycle_without_evidence_does_not_launder_healthy(self) -> None:
        authorities = self._authorities_with_evidence(
            (), lifecycle=organism_topology.BindingLifecycle.LIVE
        )

        projection = self.atlas._build_health_projection(self._selector(), authorities)

        self.assertEqual(projection.value, "unknown")
        self.assertEqual(projection.status, self.atlas.AtlasProjectionStatus.UNKNOWN)

    def test_health_projection_refuses_wrong_and_missing_records(self) -> None:
        wrong_selector = self.atlas.AtlasFieldSelector(
            self.organ_ref, "health.facets.liveness"
        )
        with self.assertRaises(self.atlas.AtlasError):
            self.atlas._build_health_projection(wrong_selector, self.live)

        missing_binding = self.atlas.AtlasFieldSelector(
            self.atlas.AtlasAuthorityRef(
                self.atlas.AtlasAuthorityKind.BINDING, "binding:missing"
            ),
            "health.facets.liveness",
        )
        with self.assertRaises(self.atlas.AtlasError):
            self.atlas._build_health_projection(missing_binding, self.live)

        without_organ = dict(self.live.records)
        without_organ.pop(self.organ_ref)
        authorities = self.atlas.AtlasAuthorities(
            records=without_organ,
            record_paths=self.live.record_paths,
            source_bytes=self.live.source_bytes,
            source_sha256=self.live.source_sha256,
            generated_index_bytes=self.live.generated_index_bytes,
            generated_index_sha256=self.live.generated_index_sha256,
            authority_digests=self.live.authority_digests,
            evidence=EvidenceSnapshot.empty(),
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )
        with self.assertRaises(self.atlas.AtlasError):
            self.atlas._build_health_projection(self._selector(), authorities)


class ArchitectureAtlasViewDeriveVerifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.atlas = importlib.import_module("tools.architecture_atlas")
        cls.live = cls.atlas.load_atlas_authorities(Path("."), frozenset())
        cls.organ_ref = cls.atlas.AtlasAuthorityRef(
            cls.atlas.AtlasAuthorityKind.ORGAN, "organ:harness-adapters"
        )
        cls.binding_ref = cls.atlas.AtlasAuthorityRef(
            cls.atlas.AtlasAuthorityKind.BINDING, "binding:harness-adapters-codex"
        )
        cls.stage_ref = cls.atlas.AtlasAuthorityRef(
            cls.atlas.AtlasAuthorityKind.STAGE, "ORG-061:STAGE-1"
        )
        cls.obligation_ref = cls.atlas.AtlasAuthorityRef(
            cls.atlas.AtlasAuthorityKind.OBLIGATION, "ORG-053:R1"
        )

    def _selector(self, ref: object, field_path: str) -> object:
        return self.atlas.AtlasFieldSelector(ref, field_path)

    def _request(
        self,
        *,
        view_kind: object | None = None,
        selectors: tuple[object, ...] | None = None,
        supporting_refs: tuple[object, ...] = (),
        nodes: tuple[object, ...] | None = None,
        edges: tuple[object, ...] = (),
    ) -> object:
        selectors = selectors or (
            self._selector(self.organ_ref, "responsibility"),
        )
        nodes = nodes or (
            self.atlas.AtlasNodeRequest(
                "primary",
                self.atlas.AtlasIdentityRef(
                    self.atlas.AtlasIdentityKind.ORGAN, self.organ_ref.id
                ),
                self.atlas.AtlasContentRole.PRIMARY,
                selectors,
                0,
                0,
                None,
            ),
        )
        return self.atlas.AtlasViewRequest(
            1,
            "boundary-purpose",
            view_kind or self.atlas.AtlasViewKind.BOUNDARY_PURPOSE,
            "What authority is shown?",
            ("not runtime health",),
            (
                self.atlas.AtlasIdentityRef(
                    self.atlas.AtlasIdentityKind.ORGAN, self.organ_ref.id
                ),
            ),
            supporting_refs,
            nodes,
            edges,
        )

    def _with_authority(self, ref: object, record: object, base: object | None = None) -> object:
        base = base or self.live
        records = dict(base.records)
        records[ref] = record
        record_paths = dict(base.record_paths)
        record_paths[ref] = "synthetic.yaml"
        source_bytes = dict(base.source_bytes)
        source_bytes["synthetic.yaml"] = b"synthetic"
        source_sha256 = dict(base.source_sha256)
        source_sha256["synthetic.yaml"] = "sha256:" + ("4" * 64)
        authority_digests = dict(base.authority_digests)
        authority_digests[ref.kind] = "sha256:" + ("5" * 64)
        return self.atlas.AtlasAuthorities(
            records=records,
            record_paths=record_paths,
            source_bytes=source_bytes,
            source_sha256=source_sha256,
            generated_index_bytes=base.generated_index_bytes,
            generated_index_sha256=base.generated_index_sha256,
            authority_digests=authority_digests,
            evidence=base.evidence,
            now=base.now,
        )

    def test_derive_copies_canonical_values_and_request_hash(self) -> None:
        request = self._request()

        spec = self.atlas.derive_atlas_view(request, self.live)

        self.assertEqual(
            spec.nodes[0].projections[0].value,
            self.live.records[self.organ_ref].responsibility,
        )
        self.assertEqual(
            spec.request_sha256,
            self.atlas._semantic_sha256(self.atlas._serialize_request(request)),
        )
        self.assertEqual(self.atlas.verify_atlas_view(spec, request, self.live), ())

    def test_v2_binding_dependency_supporting_refs_without_graph_identity(self) -> None:
        @dataclass(frozen=True)
        class Dependency:
            dependency_id: str
            relationship: str

        dependency_ref = self.atlas.AtlasAuthorityRef(
            self.atlas.AtlasAuthorityKind.DEPENDENCY, "dependency:demo"
        )
        authorities = self._with_authority(
            dependency_ref, Dependency("dependency:demo", "hard")
        )
        request = self._request(
            view_kind=self.atlas.AtlasViewKind.INTERFACES_TRUST,
            supporting_refs=(self.binding_ref, dependency_ref),
            selectors=(
                self._selector(self.binding_ref, "lifecycle"),
                self._selector(dependency_ref, "relationship"),
            ),
        )

        spec = self.atlas.derive_atlas_view(request, authorities)

        self.assertEqual(spec.view_kind, self.atlas.AtlasViewKind.INTERFACES_TRUST)
        self.assertEqual(spec.subject_refs[0].kind, self.atlas.AtlasIdentityKind.ORGAN)
        self.assertEqual(spec.supporting_refs, (self.binding_ref, dependency_ref))
        self.assertEqual([projection.value for projection in spec.nodes[0].projections], ["partial", "hard"])

    def test_v3_degradation_recovery_and_v4_stage_obligation_supporting_refs(self) -> None:
        v3 = self.atlas.derive_atlas_view(
            self._request(
                view_kind=self.atlas.AtlasViewKind.DEGRADATION_RECOVERY,
                selectors=(
                    self._selector(self.organ_ref, "degradation.unavailable"),
                    self._selector(self.organ_ref, "recovery.owner_ref"),
                ),
            ),
            self.live,
        )
        v4 = self.atlas.derive_atlas_view(
            self._request(
                view_kind=self.atlas.AtlasViewKind.EVIDENCE_REORIENTATION,
                supporting_refs=(self.stage_ref, self.obligation_ref),
                selectors=(
                    self._selector(self.stage_ref, "state"),
                    self._selector(self.obligation_ref, "owner_ref"),
                ),
            ),
            self.live,
        )

        self.assertIsInstance(v3.nodes[0].projections[0].value, tuple)
        self.assertEqual(v3.nodes[0].projections[1].value, "role:head-system-orchestrator")
        self.assertEqual(v4.subject_refs[0].id, self.organ_ref.id)
        self.assertEqual(v4.supporting_refs, (self.stage_ref, self.obligation_ref))

    def test_derive_sorts_nodes_edges_and_deduplicates_pins(self) -> None:
        nodes = (
            self.atlas.AtlasNodeRequest(
                "second",
                self.atlas.AtlasIdentityRef(self.atlas.AtlasIdentityKind.ORGAN, self.organ_ref.id),
                self.atlas.AtlasContentRole.PRIMARY,
                (self._selector(self.organ_ref, "responsibility"),),
                1,
                0,
                None,
            ),
            self.atlas.AtlasNodeRequest(
                "first",
                self.atlas.AtlasIdentityRef(self.atlas.AtlasIdentityKind.ORGAN, self.organ_ref.id),
                self.atlas.AtlasContentRole.PRIMARY,
                (self._selector(self.organ_ref, "responsibility"),),
                0,
                0,
                None,
            ),
        )
        edges = (
            self.atlas.AtlasEdgeRequest(
                "z-edge", "first", "second", self.atlas.AtlasRelationKind.BOUNDARY,
                self.atlas.AtlasContentRole.PRIMARY, (), None,
            ),
            self.atlas.AtlasEdgeRequest(
                "a-edge", "second", "first", self.atlas.AtlasRelationKind.REQUIRES,
                self.atlas.AtlasContentRole.PRIMARY, (), None,
            ),
        )
        request = self._request(nodes=nodes, edges=edges)

        spec = self.atlas.derive_atlas_view(request, self.live)
        again = self.atlas.derive_atlas_view(request, self.live)

        self.assertEqual([node.node_id for node in spec.nodes], ["first", "second"])
        self.assertEqual([edge.edge_id for edge in spec.edges], ["a-edge", "z-edge"])
        self.assertEqual(len(spec.source_pins), 1)
        self.assertEqual(self.atlas._serialize_spec(spec), self.atlas._serialize_spec(again))

    def test_derive_rejects_unresolved_refs_and_direct_invalid_schema_bounds(self) -> None:
        missing_support = self._request(
            supporting_refs=(
                self.atlas.AtlasAuthorityRef(self.atlas.AtlasAuthorityKind.BINDING, "binding:missing"),
            ),
            selectors=(
                self._selector(
                    self.atlas.AtlasAuthorityRef(self.atlas.AtlasAuthorityKind.BINDING, "binding:missing"),
                    "lifecycle",
                ),
            ),
        )
        with self.assertRaises(self.atlas.AtlasError):
            self.atlas.derive_atlas_view(missing_support, self.live)

        bad_schema = replace(self._request(), schema_version=2)
        with self.assertRaises(self.atlas.AtlasError):
            self.atlas.derive_atlas_view(bad_schema, self.live)

        too_many_exclusions = replace(self._request(), exclusions=tuple(str(i) for i in range(13)))
        with self.assertRaises(self.atlas.AtlasError):
            self.atlas.derive_atlas_view(too_many_exclusions, self.live)

    def test_derive_rejects_visible_overflow(self) -> None:
        @dataclass(frozen=True)
        class Organ:
            responsibility: str

        big_ref = self.atlas.AtlasAuthorityRef(self.atlas.AtlasAuthorityKind.ORGAN, "organ:big")
        authorities = self._with_authority(big_ref, Organ("x" * 4050))
        request = replace(
            self._request(selectors=(self._selector(big_ref, "responsibility"),)),
            subject_refs=(
                self.atlas.AtlasIdentityRef(self.atlas.AtlasIdentityKind.ORGAN, big_ref.id),
            ),
            nodes=(
                self.atlas.AtlasNodeRequest(
                    "primary",
                    self.atlas.AtlasIdentityRef(self.atlas.AtlasIdentityKind.ORGAN, big_ref.id),
                    self.atlas.AtlasContentRole.PRIMARY,
                    (self._selector(big_ref, "responsibility"),),
                    0,
                    0,
                    None,
                ),
            ),
        )

        with self.assertRaises(self.atlas.AtlasError):
            self.atlas.derive_atlas_view(request, authorities)

    def test_verify_refuses_forged_value_status_pin_request_hash_and_source_digest(self) -> None:
        request = self._request()
        spec = self.atlas.derive_atlas_view(request, self.live)
        projection = spec.nodes[0].projections[0]

        forged_projection = replace(projection, value="forged")
        forged_node = replace(spec.nodes[0], projections=(forged_projection,))
        self.assertTrue(self.atlas.verify_atlas_view(replace(spec, nodes=(forged_node,)), request, self.live))

        status_projection = replace(projection, status=self.atlas.AtlasProjectionStatus.UNKNOWN)
        status_node = replace(spec.nodes[0], projections=(status_projection,))
        self.assertTrue(self.atlas.verify_atlas_view(replace(spec, nodes=(status_node,)), request, self.live))

        pin = projection.source_pins[0]
        forged_pin = replace(pin, canonical_ref=pin.canonical_ref + "-wrong")
        pin_projection = replace(projection, source_pins=(forged_pin,))
        pin_node = replace(spec.nodes[0], projections=(pin_projection,))
        self.assertTrue(self.atlas.verify_atlas_view(replace(spec, nodes=(pin_node,)), request, self.live))

        self.assertTrue(
            self.atlas.verify_atlas_view(
                replace(spec, request_sha256="sha256:" + ("0" * 64)), request, self.live
            )
        )

        source_sha256 = dict(self.live.source_sha256)
        source_sha256["organism/organs/harness-adapters.yaml"] = "sha256:" + ("9" * 64)
        mutated_authorities = replace(self.live, source_sha256=source_sha256)
        self.assertTrue(self.atlas.verify_atlas_view(spec, request, mutated_authorities))

    def _health_authorities(self) -> object:
        record = organism_topology.EvidenceRef(
            schema_version=1,
            evidence_id="ev-pass",
            subject_ref=organism_topology.EvidenceSubjectRef(
                organism_topology.EvidenceSubjectKind.BINDING,
                self.binding_ref.id,
            ),
            facet=organism_topology.HealthFacet.LIVENESS,
            evidence_kind=organism_topology.EvidenceKind.PROBE,
            canonical_ref="evidence:ev-pass",
            sha256="sha256:" + ("a" * 64),
            observer_ref="role:orchestrator",
            observed_at="2026-08-27T00:00:00Z",
            fresh_until="2026-08-28T00:00:00Z",
            result=organism_topology.EvidenceResult.PASS,
        )
        binding = replace(
            self.live.records[self.binding_ref], evidence_refs=(record.evidence_id,)
        )
        records = dict(self.live.records)
        records[self.binding_ref] = binding
        return replace(
            self.live,
            records=records,
            evidence=EvidenceSnapshot(
                records={record.evidence_id: record},
                canonical_ref_sha256={record.canonical_ref: record.sha256},
            ),
            now=datetime(2026, 8, 27, 12, tzinfo=timezone.utc),
        )

    def test_verify_refuses_missing_extra_wrong_and_altered_evidence_pin(self) -> None:
        authorities = self._health_authorities()
        request = self._request(
            supporting_refs=(self.binding_ref,),
            selectors=(self._selector(self.binding_ref, "health.facets.liveness"),),
        )
        spec = self.atlas.derive_atlas_view(request, authorities)
        projection = spec.nodes[0].projections[0]
        self.assertEqual(len(projection.source_pins), 3)

        missing = replace(projection, source_pins=projection.source_pins[:2])
        missing_node = replace(spec.nodes[0], projections=(missing,))
        self.assertTrue(self.atlas.verify_atlas_view(replace(spec, nodes=(missing_node,)), request, authorities))

        extra = replace(projection, source_pins=projection.source_pins + (projection.source_pins[-1],))
        extra_node = replace(spec.nodes[0], projections=(extra,))
        self.assertTrue(self.atlas.verify_atlas_view(replace(spec, nodes=(extra_node,)), request, authorities))

        wrong = replace(projection.source_pins[-1], field_path="evidence:wrong/liveness")
        wrong_projection = replace(projection, source_pins=projection.source_pins[:2] + (wrong,))
        wrong_node = replace(spec.nodes[0], projections=(wrong_projection,))
        self.assertTrue(self.atlas.verify_atlas_view(replace(spec, nodes=(wrong_node,)), request, authorities))

        altered = replace(projection.source_pins[-1], authority_digest="sha256:" + ("b" * 64))
        altered_projection = replace(projection, source_pins=projection.source_pins[:2] + (altered,))
        altered_node = replace(spec.nodes[0], projections=(altered_projection,))
        self.assertTrue(self.atlas.verify_atlas_view(replace(spec, nodes=(altered_node,)), request, authorities))

    def test_unknown_empty_health_projection_has_exact_pins_and_verifies(self) -> None:
        request = self._request(
            supporting_refs=(self.binding_ref,),
            selectors=(self._selector(self.binding_ref, "health.facets.liveness"),),
        )

        spec = self.atlas.derive_atlas_view(request, self.live)

        projection = spec.nodes[0].projections[0]
        self.assertEqual(projection.value, "unknown")
        self.assertEqual(projection.status, self.atlas.AtlasProjectionStatus.UNKNOWN)
        self.assertEqual(
            [pin.field_path for pin in projection.source_pins],
            ["evidence_refs", "health.required_facets"],
        )
        self.assertEqual(self.atlas.verify_atlas_view(spec, request, self.live), ())


class ArchitectureAtlasRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.atlas = importlib.import_module("tools.architecture_atlas")
        self.organ_ref = self.atlas.AtlasAuthorityRef(
            self.atlas.AtlasAuthorityKind.ORGAN, "organ:demo"
        )

    def _style(self, **overrides: object) -> object:
        values = {
            "schema_version": 1,
            "canvas_width": 3600,
            "canvas_height": 2400,
            "background_color": "#ffffff",
            "primary_fill_color": "#ddeeff",
            "secondary_fill_color": "#eeeeee",
            "stroke_color": "#111111",
            "text_color": "#000000",
            "font_family": 1,
            "font_size": 12,
            "stroke_width": 2,
            "rank_gap": 120,
            "order_gap": 120,
            "node_width": 420,
            "node_height": 180,
        }
        values.update(overrides)
        return self.atlas.AtlasStyle(**values)

    def _authorities(self, *, responsibility: str = "Translate bounded roles") -> object:
        @dataclass(frozen=True)
        class Organ:
            responsibility: str

        return self.atlas.AtlasAuthorities(
            records={self.organ_ref: Organ(responsibility)},
            record_paths={self.organ_ref: "organism/organs/demo.yaml"},
            source_bytes={"organism/organs/demo.yaml": b"organ"},
            source_sha256={"organism/organs/demo.yaml": "sha256:" + ("1" * 64)},
            generated_index_bytes={"organism/index.json": b"index"},
            generated_index_sha256={"organism/index.json": "sha256:" + ("2" * 64)},
            authority_digests={self.atlas.AtlasAuthorityKind.ORGAN: "sha256:" + ("2" * 64)},
            evidence=EvidenceSnapshot.empty(),
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )

    def _request(self, *, edge_label: str | None = None) -> object:
        identity = self.atlas.AtlasIdentityRef(
            self.atlas.AtlasIdentityKind.ORGAN, self.organ_ref.id
        )
        selector = self.atlas.AtlasFieldSelector(self.organ_ref, "responsibility")
        nodes = (
            self.atlas.AtlasNodeRequest(
                "source", identity, self.atlas.AtlasContentRole.PRIMARY, (selector,), 0, 0, None
            ),
            self.atlas.AtlasNodeRequest(
                "target", identity, self.atlas.AtlasContentRole.PRIMARY, (selector,), 1, 0, None
            ),
        )
        edges = (
            self.atlas.AtlasEdgeRequest(
                "source-target",
                "source",
                "target",
                self.atlas.AtlasRelationKind.PROVIDES,
                self.atlas.AtlasContentRole.SECONDARY if edge_label else self.atlas.AtlasContentRole.PRIMARY,
                (),
                edge_label,
            ),
        )
        return self.atlas.AtlasViewRequest(
            1,
            "boundary-purpose",
            self.atlas.AtlasViewKind.BOUNDARY_PURPOSE,
            "What role is bounded?",
            ("no live state",),
            (identity,),
            (),
            nodes,
            edges,
        )

    def _spec(self, *, authorities: object | None = None, edge_label: str | None = None) -> object:
        return self.atlas.derive_atlas_view(
            self._request(edge_label=edge_label), authorities or self._authorities()
        )

    def test_render_excalidraw_is_deterministic_with_exact_envelope_and_numeric_font(self) -> None:
        style = self._style(font_family=2)
        spec = self._spec()

        first = self.atlas.render_excalidraw(spec, style)
        second = self.atlas.render_excalidraw(spec, style)
        scene = json.loads(first.decode("utf-8"))

        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))
        self.assertEqual(set(scene), {"type", "version", "source", "elements", "appState", "files"})
        self.assertEqual(scene["type"], "excalidraw")
        self.assertEqual(scene["version"], 2)
        self.assertEqual(scene["source"], "https://excalidraw.com")
        self.assertEqual(scene["files"], {})
        self.assertEqual(scene["appState"]["viewBackgroundColor"], "#ffffff")
        self.assertEqual(
            [element["id"] for element in scene["elements"]],
            sorted(element["id"] for element in scene["elements"]),
        )
        self.assertTrue(
            all(
                element["fontFamily"] == 2
                for element in scene["elements"]
                if element["type"] == "text"
            )
        )

    def test_render_excalidraw_text_original_text_and_reciprocal_bindings(self) -> None:
        scene = json.loads(self.atlas.render_excalidraw(self._spec(edge_label="why"), self._style()).decode("utf-8"))
        elements = {element["id"]: element for element in scene["elements"]}

        for element in scene["elements"]:
            if element["type"] == "text":
                self.assertEqual(element["text"], element["originalText"])
                container = elements[element["containerId"]]
                self.assertTrue(
                    any(item["id"] == element["id"] for item in container["boundElements"])
                )
            if element["type"] == "arrow":
                self.assertIn(element["startBinding"]["elementId"], elements)
                self.assertIn(element["endBinding"]["elementId"], elements)
                for binding_name in ("startBinding", "endBinding"):
                    bound = elements[element[binding_name]["elementId"]]["boundElements"]
                    self.assertTrue(any(item["id"] == element["id"] for item in bound))

    def test_render_excalidraw_has_no_nondeterministic_or_forbidden_element_fields(self) -> None:
        scene = json.loads(self.atlas.render_excalidraw(self._spec(), self._style()).decode("utf-8"))
        forbidden = {"updated", "created", "timestamp", "link", "customData", "fileId"}

        for element in scene["elements"]:
            self.assertFalse(forbidden & set(element))
            self.assertFalse(element["isDeleted"])
            self.assertNotIn(element["type"], {"image", "embeddable"})
            self.assertIsInstance(element["seed"], int)
            self.assertIsInstance(element["versionNonce"], int)

    def test_render_excalidraw_refuses_canvas_collision_and_text_overflow(self) -> None:
        spec = self._spec()
        with self.assertRaises(self.atlas.AtlasError):
            self.atlas.render_excalidraw(spec, self._style(node_width=1200, rank_gap=1000))

        with self.assertRaises(self.atlas.AtlasError):
            self.atlas.render_excalidraw(
                self._spec(authorities=self._authorities(responsibility="x" * 250)),
                self._style(node_width=180, node_height=80, font_size=20),
            )

    def test_render_excalidraw_refuses_internal_spec_mismatch(self) -> None:
        spec = self._spec()

        with self.assertRaises(self.atlas.AtlasError):
            self.atlas.render_excalidraw(
                replace(spec, source_pins=spec.source_pins + spec.source_pins),
                self._style(),
            )
        with self.assertRaises(self.atlas.AtlasError):
            self.atlas.render_excalidraw(
                replace(spec, visible_text_sha256="sha256:" + ("0" * 64)),
                self._style(),
            )

    def test_render_excalidraw_same_scene_after_source_authority_path_rename(self) -> None:
        spec = self._spec()
        renamed_nodes = []
        for node in spec.nodes:
            projections = []
            for projection in node.projections:
                pins = tuple(
                    replace(pin, authority_path="organism/organs/renamed.yaml")
                    for pin in projection.source_pins
                )
                projections.append(replace(projection, source_pins=pins))
            renamed_nodes.append(replace(node, projections=tuple(projections)))
        renamed_spec = replace(
            spec,
            nodes=tuple(renamed_nodes),
            source_pins=tuple(
                replace(pin, authority_path="organism/organs/renamed.yaml")
                for pin in spec.source_pins
            ),
        )

        self.assertEqual(
            self.atlas.render_excalidraw(spec, self._style()),
            self.atlas.render_excalidraw(renamed_spec, self._style()),
        )


class ArchitectureAtlasSchemaGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.atlas = importlib.import_module("tools.architecture_atlas")
        self.repo_root = Path(__file__).resolve().parents[1]

    def _schema(self, generator_name: str) -> dict[str, object]:
        return json.loads(getattr(self.atlas, generator_name)().decode("utf-8"))

    def test_generated_schema_bytes_match_checked_in_files(self) -> None:
        cases = (
            ("schemas/architecture-atlas-request.schema.json", "generate_atlas_request_schema"),
            ("schemas/architecture-atlas-view.schema.json", "generate_atlas_view_schema"),
            ("schemas/architecture-atlas-bundle.schema.json", "generate_atlas_bundle_schema"),
        )

        for rel_path, generator_name in cases:
            with self.subTest(rel_path=rel_path):
                generated = getattr(self.atlas, generator_name)()
                checked_in = (self.repo_root / rel_path).read_bytes()
                self.assertEqual(checked_in, generated)
                self.assertTrue(checked_in.endswith(b"\n"))
                self.assertEqual(checked_in.count(b"\n"), 1)

    def test_request_schema_has_critical_parser_bounds_and_exact_objects(self) -> None:
        schema = self._schema("generate_atlas_request_schema")
        defs = schema["$defs"]

        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["schema_version"], {"const": 1})
        self.assertEqual(
            schema["properties"]["view_id"], {"$ref": "#/$defs/stableId"}
        )
        self.assertEqual(
            defs["stableId"]["pattern"], self.atlas.ATLAS_STABLE_ID_PATTERN
        )
        self.assertEqual(schema["properties"]["primary_question"]["maxLength"], 240)
        self.assertEqual(schema["properties"]["primary_question"]["pattern"], r"^[^?]*\?[^?]*$")
        self.assertEqual(schema["properties"]["exclusions"]["minItems"], 1)
        self.assertEqual(schema["properties"]["exclusions"]["maxItems"], 12)
        self.assertTrue(schema["properties"]["exclusions"]["uniqueItems"])
        self.assertEqual(schema["properties"]["subject_refs"]["minItems"], 1)
        self.assertEqual(schema["properties"]["subject_refs"]["maxItems"], 12)
        self.assertEqual(schema["properties"]["supporting_refs"]["maxItems"], 24)
        self.assertEqual(defs["nodeRequest"]["properties"]["selectors"]["minItems"], 1)
        self.assertEqual(defs["nodeRequest"]["properties"]["selectors"]["maxItems"], 6)
        self.assertEqual(defs["edgeRequest"]["properties"]["source_selectors"]["maxItems"], 6)
        self.assertEqual(defs["rankOrder"], {"type": "integer", "minimum": 0, "maximum": 5})
        self.assertEqual(schema["properties"]["edges"]["maxItems"], 24)
        self.assertEqual(schema["allOf"][0]["properties"]["nodes"]["maxContains"], 12)
        self.assertEqual(schema["allOf"][1]["properties"]["nodes"]["maxContains"], 6)

        for object_name in (
            "identityRef",
            "authorityRef",
            "fieldSelector",
            "nodeRequest",
            "edgeRequest",
        ):
            self.assertFalse(defs[object_name]["additionalProperties"])

    def test_view_schema_has_projection_pin_and_value_contracts(self) -> None:
        schema = self._schema("generate_atlas_view_schema")
        defs = schema["$defs"]
        projection = defs["projection"]
        source_pin = defs["sourcePin"]

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["request_sha256"], {"$ref": "#/$defs/sha256"})
        self.assertEqual(defs["sha256"]["pattern"], self.atlas.ATLAS_SHA256_PATTERN)
        self.assertEqual(projection["properties"]["source_pins"]["minItems"], 1)
        self.assertEqual(
            projection["properties"]["status"]["enum"],
            ["declared", "derived", "evidence-proven", "unknown"],
        )
        self.assertEqual(
            [branch.get("type") for branch in defs["projectionValue"]["anyOf"][:5]],
            ["null", "boolean", "integer", "number", "string"],
        )
        self.assertEqual(
            defs["projectionValue"]["anyOf"][5],
            {"type": "array", "items": {"$ref": "#/$defs/projectionValue"}},
        )
        self.assertFalse(source_pin["additionalProperties"])
        self.assertEqual(
            source_pin["properties"]["source_class"]["enum"],
            ["evidence-snapshot", "repo-document"],
        )
        self.assertEqual(
            source_pin["allOf"][0]["then"]["properties"]["authority_path"],
            {"type": "string", "minLength": 1},
        )
        self.assertEqual(
            source_pin["allOf"][1]["then"]["properties"]["authority_path"],
            {"type": "null"},
        )
        self.assertFalse(defs["nodeSpec"]["additionalProperties"])
        self.assertFalse(defs["edgeSpec"]["additionalProperties"])

    def test_bundle_schema_has_manifest_entry_path_and_hash_contracts(self) -> None:
        schema = self._schema("generate_atlas_bundle_schema")
        defs = schema["$defs"]
        entry = defs["bundleEntry"]

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["schema_version"], {"const": 1})
        self.assertEqual(schema["properties"]["bundle_id"], {"$ref": "#/$defs/stableId"})
        self.assertEqual(defs["bundlePath"]["pattern"], self.atlas.ATLAS_BUNDLE_PATH_PATTERN)
        self.assertEqual(defs["sha256"]["pattern"], self.atlas.ATLAS_SHA256_PATTERN)
        self.assertFalse(entry["additionalProperties"])
        self.assertEqual(
            entry["required"],
            [
                "view_id",
                "request_path",
                "spec_path",
                "scene_path",
                "request_sha256",
                "spec_sha256",
                "scene_sha256",
            ],
        )
        self.assertEqual(entry["properties"]["request_path"], {"$ref": "#/$defs/bundlePath"})
        self.assertEqual(entry["properties"]["scene_sha256"], {"$ref": "#/$defs/sha256"})


class ArchitectureAtlasBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.atlas = importlib.import_module("tools.architecture_atlas")
        self.organ_ref = self.atlas.AtlasAuthorityRef(
            self.atlas.AtlasAuthorityKind.ORGAN, "organ:demo"
        )

    def _authorities(self) -> object:
        @dataclass(frozen=True)
        class Organ:
            responsibility: str

        return self.atlas.AtlasAuthorities(
            records={self.organ_ref: Organ("Translate bounded roles")},
            record_paths={self.organ_ref: "organism/organs/demo.yaml"},
            source_bytes={"organism/organs/demo.yaml": b"organ"},
            source_sha256={"organism/organs/demo.yaml": "sha256:" + ("1" * 64)},
            generated_index_bytes={"organism/index.json": b"index"},
            generated_index_sha256={"organism/index.json": "sha256:" + ("2" * 64)},
            authority_digests={self.atlas.AtlasAuthorityKind.ORGAN: "sha256:" + ("2" * 64)},
            evidence=EvidenceSnapshot.empty(),
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )

    def _request_payload(self, view_id: str = "bundle-view") -> dict[str, object]:
        selector = {
            "authority_ref": {"kind": "organ", "id": self.organ_ref.id},
            "field_path": "responsibility",
        }
        return {
            "schema_version": 1,
            "view_id": view_id,
            "view_kind": "boundary-purpose",
            "primary_question": "What role is bounded?",
            "exclusions": ["runtime status"],
            "subject_refs": [{"kind": "organ", "id": self.organ_ref.id}],
            "supporting_refs": [],
            "nodes": [
                {
                    "node_id": "source",
                    "identity_ref": {"kind": "organ", "id": self.organ_ref.id},
                    "role": "primary",
                    "selectors": [selector],
                    "rank": 0,
                    "order": 0,
                    "necessary_for": None,
                }
            ],
            "edges": [],
        }

    def _style_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "canvas_width": 3600,
            "canvas_height": 2400,
            "background_color": "#ffffff",
            "primary_fill_color": "#ddeeff",
            "secondary_fill_color": "#eeeeee",
            "stroke_color": "#111111",
            "text_color": "#000000",
            "font_family": 1,
            "font_size": 12,
            "stroke_width": 2,
            "rank_gap": 120,
            "order_gap": 120,
            "node_width": 420,
            "node_height": 180,
        }

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def _inputs(self, root: Path, *, view_id: str = "bundle-view") -> tuple[Path, Path]:
        root.mkdir(parents=True, exist_ok=True)
        request_path = root / f"{view_id}.request.json"
        style_path = root / "atlas-style.json"
        self._write_json(request_path, self._request_payload(view_id))
        self._write_json(style_path, self._style_payload())
        return request_path, style_path

    def _payloads(self, root: Path, *, view_id: str = "bundle-view") -> Mapping[str, bytes]:
        request_path, style_path = self._inputs(root, view_id=view_id)
        return self.atlas.build_atlas_bundle(
            "demo-bundle", (request_path,), style_path, self._authorities()
        )

    def _write_bundle(self, root: Path, *, authorities: object | None = None) -> tuple[Path, object]:
        payloads = self._payloads(root)
        output_root = root / "bundle"
        self.atlas.write_bundle_atomic(output_root, payloads)
        return output_root, authorities or self._authorities()

    def _verify(self, bundle_root: Path, authorities: object) -> tuple[object, ...]:
        with mock.patch.object(
            self.atlas, "load_atlas_authorities", return_value=authorities
        ):
            return self.atlas.verify_atlas_bundle(bundle_root, Path("."), frozenset())

    def test_build_bundle_requires_explicit_id_names_and_unique_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_path, style_path = self._inputs(root)
            with self.assertRaises(self.atlas.AtlasError):
                self.atlas.build_atlas_bundle(
                    "Bad Bundle", (request_path,), style_path, self._authorities()
                )
            with self.assertRaises(self.atlas.AtlasError):
                self.atlas.build_atlas_bundle("demo-bundle", (), style_path, self._authorities())

            wrong_name = root / "wrong.request.json"
            self._write_json(wrong_name, self._request_payload())
            with self.assertRaises(self.atlas.AtlasError):
                self.atlas.build_atlas_bundle(
                    "demo-bundle", (wrong_name,), style_path, self._authorities()
                )

            duplicate = root / "copy" / "bundle-view.request.json"
            duplicate.parent.mkdir()
            self._write_json(duplicate, self._request_payload())
            with self.assertRaises(self.atlas.AtlasError):
                self.atlas.build_atlas_bundle(
                    "demo-bundle",
                    (request_path, duplicate),
                    style_path,
                    self._authorities(),
                )

    def test_build_write_and_verify_bundle_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payloads = self._payloads(root)
            self.assertEqual(
                set(payloads),
                {
                    "atlas-style.json",
                    "bundle-view.request.json",
                    "bundle-view.view.json",
                    "bundle-view.excalidraw",
                    "fixture-bundle.json",
                },
            )
            manifest = json.loads(payloads["fixture-bundle.json"].decode("utf-8"))
            entry = manifest["entries"][0]
            self.assertEqual(manifest["bundle_id"], "demo-bundle")
            self.assertEqual(entry["view_id"], "bundle-view")
            self.assertEqual(entry["request_path"], "bundle-view.request.json")
            self.assertEqual(
                entry["request_sha256"], self.atlas._sha256(payloads["bundle-view.request.json"])
            )
            self.assertEqual(
                entry["spec_sha256"], self.atlas._sha256(payloads["bundle-view.view.json"])
            )
            self.assertEqual(
                entry["scene_sha256"], self.atlas._sha256(payloads["bundle-view.excalidraw"])
            )

            output_root = root / "bundle"
            self.atlas.write_bundle_atomic(output_root, payloads)
            self.assertEqual(sorted(path.name for path in output_root.iterdir()), sorted(payloads))
            self.assertEqual(self._verify(output_root, self._authorities()), ())

    def test_verify_bundle_reports_missing_extra_symlink_path_escape_hash_spec_and_source_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_root, authorities = self._write_bundle(root / "missing")
            (bundle_root / "bundle-view.view.json").unlink()
            self.assertTrue(self._verify(bundle_root, authorities))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_root, authorities = self._write_bundle(root / "extra")
            (bundle_root / "extra.txt").write_text("extra", encoding="utf-8")
            self.assertTrue(self._verify(bundle_root, authorities))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_root, authorities = self._write_bundle(root / "symlink")
            (bundle_root / "bundle-view.excalidraw").unlink()
            (bundle_root / "bundle-view.excalidraw").symlink_to("bundle-view.request.json")
            self.assertTrue(self._verify(bundle_root, authorities))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_root, authorities = self._write_bundle(root / "escape")
            manifest_path = bundle_root / "fixture-bundle.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["entries"][0]["request_path"] = "../escape"
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(self._verify(bundle_root, authorities))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_root, authorities = self._write_bundle(root / "hash")
            manifest_path = bundle_root / "fixture-bundle.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["entries"][0]["spec_sha256"] = "sha256:" + ("0" * 64)
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(self._verify(bundle_root, authorities))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_root, authorities = self._write_bundle(root / "spec")
            (bundle_root / "bundle-view.view.json").write_text("{}\n", encoding="utf-8")
            self.assertTrue(self._verify(bundle_root, authorities))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_root, authorities = self._write_bundle(root / "source")
            source_sha256 = dict(authorities.source_sha256)
            source_sha256["organism/organs/demo.yaml"] = "sha256:" + ("9" * 64)
            drifted = replace(authorities, source_sha256=source_sha256)
            self.assertTrue(self._verify(bundle_root, drifted))

    def test_write_bundle_atomic_rejects_unsafe_targets_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payloads = self._payloads(root)
            existing = root / "existing"
            existing.mkdir()
            with self.assertRaises(self.atlas.AtlasError):
                self.atlas.write_bundle_atomic(existing, payloads)

            unsafe = dict(payloads)
            unsafe["../escape"] = b"bad"
            with self.assertRaises(self.atlas.AtlasError):
                self.atlas.write_bundle_atomic(root / "unsafe", unsafe)
            self.assertFalse(any(path.name.startswith(".unsafe.tmp-") for path in root.iterdir()))

            with mock.patch.object(self.atlas.os, "rename", side_effect=OSError("boom")):
                with self.assertRaises(self.atlas.AtlasError):
                    self.atlas.write_bundle_atomic(root / "fails", payloads)
            self.assertFalse((root / "fails").exists())
            self.assertFalse(any(path.name.startswith(".fails.tmp-") for path in root.iterdir()))

            output = root / "success"
            self.atlas.write_bundle_atomic(output, payloads)
            self.assertTrue((output / "fixture-bundle.json").is_file())


class ArchitectureAtlasCliAndCheckToolingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.atlas = importlib.import_module("tools.architecture_atlas")
        self.organ_ref = self.atlas.AtlasAuthorityRef(
            self.atlas.AtlasAuthorityKind.ORGAN, "organ:demo"
        )

    def _run_main(self, argv: list[str]) -> tuple[int, dict[str, object], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.atlas.main(argv)
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def _authorities(self) -> object:
        @dataclass(frozen=True)
        class Organ:
            responsibility: str

        return self.atlas.AtlasAuthorities(
            records={self.organ_ref: Organ("Translate bounded roles")},
            record_paths={self.organ_ref: "organism/organs/demo.yaml"},
            source_bytes={"organism/organs/demo.yaml": b"organ"},
            source_sha256={"organism/organs/demo.yaml": "sha256:" + ("1" * 64)},
            generated_index_bytes={"organism/index.json": b"index"},
            generated_index_sha256={"organism/index.json": "sha256:" + ("2" * 64)},
            authority_digests={self.atlas.AtlasAuthorityKind.ORGAN: "sha256:" + ("2" * 64)},
            evidence=EvidenceSnapshot.empty(),
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def _request_payload(self, view_id: str = "cli-view") -> dict[str, object]:
        selector = {
            "authority_ref": {"kind": "organ", "id": self.organ_ref.id},
            "field_path": "responsibility",
        }
        return {
            "schema_version": 1,
            "view_id": view_id,
            "view_kind": "boundary-purpose",
            "primary_question": "What role is bounded?",
            "exclusions": ["runtime status"],
            "subject_refs": [{"kind": "organ", "id": self.organ_ref.id}],
            "supporting_refs": [],
            "nodes": [
                {
                    "node_id": "source",
                    "identity_ref": {"kind": "organ", "id": self.organ_ref.id},
                    "role": "primary",
                    "selectors": [selector],
                    "rank": 0,
                    "order": 0,
                    "necessary_for": None,
                }
            ],
            "edges": [],
        }

    def _style_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "canvas_width": 3600,
            "canvas_height": 2400,
            "background_color": "#ffffff",
            "primary_fill_color": "#ddeeff",
            "secondary_fill_color": "#eeeeee",
            "stroke_color": "#111111",
            "text_color": "#000000",
            "font_family": 1,
            "font_size": 12,
            "stroke_width": 2,
            "rank_gap": 120,
            "order_gap": 120,
            "node_width": 420,
            "node_height": 180,
        }

    def _write_inputs(self, root: Path, view_id: str = "cli-view") -> tuple[Path, Path]:
        request = root / f"{view_id}.request.json"
        style = root / "style.json"
        self._write_json(request, self._request_payload(view_id))
        self._write_json(style, self._style_payload())
        return request, style

    def test_cli_generate_schemas_replaces_only_exact_three_schema_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            schema_root = repo_root / "schemas"
            schema_root.mkdir()
            (schema_root / "architecture-atlas-request.schema.json").write_text(
                "stale\n", encoding="utf-8"
            )

            code, payload, stderr = self._run_main(
                ["generate-schemas", "--repo-root", str(repo_root)]
            )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(payload["issues"], [])
            self.assertEqual(
                sorted(path.name for path in schema_root.iterdir()),
                [
                    "architecture-atlas-bundle.schema.json",
                    "architecture-atlas-request.schema.json",
                    "architecture-atlas-view.schema.json",
                ],
            )
            self.assertEqual(
                (schema_root / "architecture-atlas-request.schema.json").read_bytes(),
                self.atlas.generate_atlas_request_schema(),
            )

    def test_cli_derive_and_verify_bundle_positive_and_negative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request, style = self._write_inputs(root)
            output = root / "bundle"
            authorities = self._authorities()
            with mock.patch.object(
                self.atlas, "load_atlas_authorities", return_value=authorities
            ):
                code, payload, stderr = self._run_main(
                    [
                        "derive-bundle",
                        "--repo-root",
                        str(root),
                        "--bundle-id",
                        "cli-bundle",
                        "--request",
                        str(request),
                        "--style",
                        str(style),
                        "--output",
                        str(output),
                        "--goal-key",
                        "synthetic-goal",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(payload["issues"], [])
            self.assertTrue((output / "fixture-bundle.json").is_file())

            with mock.patch.object(
                self.atlas, "load_atlas_authorities", return_value=authorities
            ):
                code, payload, stderr = self._run_main(
                    [
                        "verify-bundle",
                        "--repo-root",
                        str(root),
                        "--bundle-root",
                        str(output),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(payload["issues"], [])

            (output / "cli-view.view.json").write_text("{}\n", encoding="utf-8")
            with mock.patch.object(
                self.atlas, "load_atlas_authorities", return_value=authorities
            ):
                code, payload, stderr = self._run_main(
                    [
                        "verify-bundle",
                        "--repo-root",
                        str(root),
                        "--bundle-root",
                        str(output),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(stderr, "")
            self.assertTrue(payload["issues"])

    def test_cli_goal_keys_are_explicit_and_empty_when_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request, style = self._write_inputs(root)
            output = root / "bundle"
            seen: list[frozenset[str]] = []

            def fake_load(repo_root: Path, goal_keys: frozenset[str], **kwargs: object) -> object:
                self.assertEqual(repo_root, root)
                self.assertEqual(kwargs["evidence"], EvidenceSnapshot.empty())
                seen.append(goal_keys)
                return self._authorities()

            with mock.patch.object(self.atlas, "load_atlas_authorities", side_effect=fake_load):
                code, payload, stderr = self._run_main(
                    [
                        "derive-bundle",
                        "--repo-root",
                        str(root),
                        "--bundle-id",
                        "cli-bundle",
                        "--request",
                        str(request),
                        "--style",
                        str(style),
                        "--output",
                        str(output),
                        "--goal-key",
                        "synthetic-goal",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(payload["issues"], [])
            self.assertEqual(stderr, "")
            self.assertEqual(seen, [frozenset({"synthetic-goal"})])

            with mock.patch.object(self.atlas, "verify_atlas_bundle", return_value=()) as verify:
                code, payload, stderr = self._run_main(
                    ["verify-bundle", "--repo-root", str(root), "--bundle-root", str(output)]
                )
            self.assertEqual(code, 0)
            self.assertEqual(payload["issues"], [])
            self.assertEqual(stderr, "")
            self.assertEqual(verify.call_args.args[2], frozenset())
            self.assertEqual(verify.call_args.kwargs["evidence"], EvidenceSnapshot.empty())

    def test_cli_rejects_publish_live_and_missing_required_options(self) -> None:
        for verb in ("publish", "live"):
            with self.subTest(verb=verb):
                code, payload, stderr = self._run_main([verb])
                self.assertNotEqual(code, 0)
                self.assertTrue(payload["issues"])
                self.assertIn("invalid choice", stderr)

        code, payload, stderr = self._run_main(["derive-bundle", "--repo-root", "."])
        self.assertNotEqual(code, 0)
        self.assertTrue(payload["issues"])
        self.assertIn("required", stderr)

    def test_check_atlas_tooling_reports_schema_mismatch_and_missing_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self.atlas._write_schema_files_atomic(repo_root)
            (repo_root / "schemas" / "architecture-atlas-view.schema.json").write_text(
                "{}\n", encoding="utf-8"
            )

            issues = self.atlas.check_atlas_tooling(
                repo_root, frozenset(), evidence=EvidenceSnapshot.empty()
            )

            codes = [issue.code for issue in issues]
            self.assertIn("atlas-schema-mismatch", codes)
            self.assertIn("atlas-fixture-missing", codes)

    def test_cli_check_tooling_propagates_explicit_goal_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self.atlas._write_schema_files_atomic(repo_root)
            fixture_root = repo_root / "tools" / "fixtures" / "architecture_atlas"
            fixture_root.mkdir(parents=True)

            with mock.patch.object(
                self.atlas, "verify_atlas_bundle", return_value=()
            ) as verify:
                code, payload, stderr = self._run_main(
                    [
                        "check-tooling",
                        "--repo-root",
                        str(repo_root),
                        "--goal-key",
                        "synthetic-goal",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertEqual(payload["issues"], [])
            self.assertEqual(stderr, "")
            self.assertEqual(verify.call_args.args[2], frozenset({"synthetic-goal"}))
            self.assertEqual(verify.call_args.kwargs["evidence"], EvidenceSnapshot.empty())


class ArchitectureAtlasCommittedFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.atlas = importlib.import_module("tools.architecture_atlas")
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.fixture_root = cls.repo_root / "tools" / "fixtures" / "architecture_atlas"

    def test_fixture_files_are_freshly_derived_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request_path = root / "fixture-v1.request.json"
            style_path = root / "fixture-style.json"
            request_path.write_bytes((self.fixture_root / request_path.name).read_bytes())
            style_path.write_bytes((self.fixture_root / style_path.name).read_bytes())
            payloads = self.atlas.build_atlas_bundle(
                "fixture-v1",
                (request_path,),
                style_path,
                self.atlas.load_atlas_authorities(Path("."), frozenset()),
            )

        expected_names = {
            "fixture-v1.request.json",
            "fixture-v1.view.json",
            "fixture-v1.excalidraw",
            "fixture-style.json",
            "fixture-bundle.json",
        }
        self.assertEqual(set(payloads), expected_names)
        for name in sorted(expected_names):
            with self.subTest(name=name):
                self.assertEqual((self.fixture_root / name).read_bytes(), payloads[name])

    def test_fixture_verify_and_check_tooling_are_green(self) -> None:
        self.assertEqual(
            self.atlas.verify_atlas_bundle(
                self.fixture_root,
                Path("."),
                frozenset(),
                evidence=EvidenceSnapshot.empty(),
            ),
            (),
        )
        self.assertEqual(
            self.atlas.check_atlas_tooling(
                Path("."),
                frozenset(),
                evidence=EvidenceSnapshot.empty(),
            ),
            (),
        )

    def test_fixture_scene_is_official_v2_with_editable_reciprocal_bindings(self) -> None:
        scene = json.loads((self.fixture_root / "fixture-v1.excalidraw").read_text(encoding="utf-8"))
        self.assertEqual(set(scene), {"type", "version", "source", "elements", "appState", "files"})
        self.assertEqual(scene["type"], "excalidraw")
        self.assertEqual(scene["version"], 2)
        self.assertEqual(scene["source"], "https://excalidraw.com")
        self.assertEqual(scene["files"], {})
        elements = {element["id"]: element for element in scene["elements"]}
        self.assertEqual([element["type"] for element in scene["elements"]].count("arrow"), 1)
        for element in scene["elements"]:
            if element["type"] == "text":
                self.assertEqual(element["text"], element["originalText"])
                self.assertEqual(element["fontFamily"], 1)
                container = elements[element["containerId"]]
                self.assertTrue(
                    any(item["id"] == element["id"] for item in container["boundElements"])
                )
            if element["type"] == "arrow":
                for binding_name in ("startBinding", "endBinding"):
                    bound = elements[element[binding_name]["elementId"]]["boundElements"]
                    self.assertTrue(any(item["id"] == element["id"] for item in bound))

    def test_fixture_has_no_evidence_proven_health_projection(self) -> None:
        spec = json.loads((self.fixture_root / "fixture-v1.view.json").read_text(encoding="utf-8"))
        projections = [
            projection
            for node in spec["nodes"]
            for projection in node["projections"]
        ] + [
            projection
            for edge in spec["edges"]
            for projection in edge["projections"]
        ]
        self.assertTrue(projections)
        self.assertNotIn("evidence-proven", [projection["status"] for projection in projections])
        self.assertFalse(
            any(
                projection["selector"]["field_path"].startswith("health.facets.")
                for projection in projections
            )
        )

    def test_ci_lists_each_schema_path_once_per_trigger_and_no_redundant_atlas_lines(self) -> None:
        ci = (self.repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for rel_path in (
            "schemas/architecture-atlas-request.schema.json",
            "schemas/architecture-atlas-view.schema.json",
            "schemas/architecture-atlas-bundle.schema.json",
        ):
            with self.subTest(rel_path=rel_path):
                self.assertEqual(ci.count(rel_path), 1)  # T-5001: one trigger list (pull_request)
        self.assertNotIn("tools/architecture_atlas.py", ci)
        self.assertNotIn("tools/test_architecture_atlas.py", ci)
        self.assertNotIn("tools/fixtures/architecture_atlas", ci)


if __name__ == "__main__":
    unittest.main()
