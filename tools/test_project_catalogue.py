"""Tests for the ORG-054 project catalogue (T-3167)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import yaml

from tools import project_catalogue as pc


REPO_ROOT = Path(__file__).resolve().parents[1]


def _record(
    project_id: str = "alpha-project",
    *,
    parent_goal_ref: str | None = None,
    classification: str = "independent",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "telos": f"Produce the outcome owned by {project_id}",
        "parent_goal_ref": parent_goal_ref,
        "classification": classification,
        "boundary": {
            "inside": ["the bounded project outcome"],
            "outside": ["unrelated systems"],
            "not_owned": ["external policy"],
        },
        "interfaces": {
            "provides": ["project:outcome"],
            "requires": ["authority:ratification"],
        },
        "stewardship": {
            "role": "project-steward",
            "authority_surface": "decision:T-3167",
        },
        "lifecycle": {
            "developmental_mode": "shaping",
            "formed_at": "2026-08-26T00:00:00Z",
            "transition_refs": [],
        },
        "evidence": {
            "health": [],
            "user_value": [],
            "safety": [],
            "developmental": [],
        },
        "tensions": [
            {
                "pole_a": "plasticity",
                "pole_b": "reliability",
                "rebalance_trigger": "measured operating evidence",
            }
        ],
        "operational_contract": {
            "obligations": {
                "deployment": ["evidence:install", "evidence:update"],
                "data": [],
                "security": ["evidence:configure"],
                "cost": [],
                "maintenance": ["evidence:health"],
                "recovery": ["evidence:remove"],
            },
            "lifecycle": {
                "install": ["evidence:install"],
                "configure": ["evidence:configure"],
                "health_monitor": ["evidence:health"],
                "update": ["evidence:update"],
                "remove": ["evidence:remove"],
            },
        },
    }


def _write_record(records_root: Path, data: dict[str, object]) -> Path:
    records_root.mkdir(parents=True, exist_ok=True)
    path = records_root / f"{data['project_id']}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _write_generated(repo_root: Path, goal_keys: frozenset[str] = frozenset()) -> None:
    records = pc.load_project_records(repo_root / "projects" / "records")
    (repo_root / "schemas").mkdir(parents=True, exist_ok=True)
    (repo_root / "projects").mkdir(parents=True, exist_ok=True)
    (repo_root / "schemas" / "project-record.schema.json").write_bytes(
        pc.generate_project_record_schema()
    )
    (repo_root / "projects" / "index.json").write_bytes(
        pc.generate_project_index(records, goal_keys)
    )


class ProjectCatalogueTests(unittest.TestCase):
    def test_project_catalogue_empty_rootless_instance_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "projects" / "records").mkdir(parents=True)
            _write_generated(repo)
            records = pc.load_project_records(repo / "projects" / "records")
            self.assertEqual(records, {})
            self.assertEqual(pc.validate_project_graph(records, frozenset()), ())
            self.assertEqual(pc.check_project_catalogue(repo, frozenset()), ())
            index = json.loads((repo / "projects" / "index.json").read_text())
            self.assertEqual(index["projects"], {})

    def test_project_record_rootless_parent_null_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            records_root = Path(td) / "records"
            _write_record(records_root, _record())
            records = pc.load_project_records(records_root)
            self.assertIsNone(records["alpha-project"].parent_goal_ref)
            self.assertEqual(pc.validate_project_graph(records, frozenset()), ())
            self.assertEqual(
                pc.resolve_upward_chain("alpha-project", records, frozenset()),
                ("project:alpha-project",),
            )

    def test_project_graph_two_independent_roots_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_record(root, _record("alpha-project"))
            _write_record(root, _record("beta-project"))
            records = pc.load_project_records(root)
            self.assertEqual(pc.validate_project_graph(records, frozenset()), ())
            self.assertEqual(
                pc.resolve_upward_chain("beta-project", records, frozenset()),
                ("project:beta-project",),
            )

    def test_project_graph_recursive_composition_resolves_nearest_to_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_record(root, _record("root-project", parent_goal_ref="goal:root-goal"))
            _write_record(
                root,
                _record(
                    "middle-project",
                    parent_goal_ref="project:root-project",
                    classification="organ",
                ),
            )
            _write_record(
                root,
                _record(
                    "leaf-project",
                    parent_goal_ref="project:middle-project",
                    classification="organ",
                ),
            )
            records = pc.load_project_records(root)
            goals = frozenset({"root-goal"})
            self.assertEqual(pc.validate_project_graph(records, goals), ())
            self.assertEqual(
                pc.resolve_upward_chain("leaf-project", records, goals),
                (
                    "project:leaf-project",
                    "project:middle-project",
                    "project:root-project",
                    "goal:root-goal",
                ),
            )

    def test_project_graph_cycle_and_dangling_parent_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_record(root, _record("alpha-project", parent_goal_ref="project:beta-project"))
            _write_record(root, _record("beta-project", parent_goal_ref="project:alpha-project"))
            _write_record(root, _record("orphan-project", parent_goal_ref="project:missing-project"))
            _write_record(root, _record("self-project", parent_goal_ref="project:self-project"))
            _write_record(root, _record("goal-project", parent_goal_ref="goal:missing-goal"))
            _write_record(root, _record("invalid-ref-project", parent_goal_ref="repo:not-a-parent"))
            records = pc.load_project_records(root)
            issues = pc.validate_project_graph(records, frozenset())
            self.assertIn("parent-cycle", {issue.code for issue in issues})
            self.assertIn("dangling-parent", {issue.code for issue in issues})
            self.assertIn("self-parent", {issue.code for issue in issues})
            self.assertIn("dangling-goal", {issue.code for issue in issues})
            self.assertIn("invalid-parent-ref", {issue.code for issue in issues})
            with self.assertRaisesRegex(pc.ProjectCatalogueError, "invalid project graph"):
                pc.generate_project_index(records, frozenset())

    def test_project_identity_survives_repo_path_and_name_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            first = base / "first-repository-name" / "projects" / "records"
            second = base / "renamed-and-moved" / "projects" / "records"
            _write_record(first, _record())
            second.parent.mkdir(parents=True)
            shutil.copytree(first, second)
            first_records = pc.load_project_records(first)
            second_records = pc.load_project_records(second)
            self.assertEqual(first_records, second_records)
            self.assertEqual(
                pc.generate_project_index(first_records, frozenset()),
                pc.generate_project_index(second_records, frozenset()),
            )

    def test_generated_index_rejects_manual_or_stale_content(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write_record(repo / "projects" / "records", _record())
            _write_generated(repo)
            index_path = repo / "projects" / "index.json"
            index_path.write_bytes(index_path.read_bytes().replace(b'"schema_version": 1', b'"schema_version": 9'))
            issues = pc.check_project_catalogue(repo, frozenset())
            self.assertEqual({issue.code for issue in issues}, {"index-stale"})
            original = pc.generate_project_index(
                pc.load_project_records(repo / "projects" / "records"), frozenset()
            )
            changed = _record()
            changed["telos"] = "A semantically different purpose"
            _write_record(repo / "projects" / "records", changed)
            self.assertNotEqual(
                original,
                pc.generate_project_index(
                    pc.load_project_records(repo / "projects" / "records"), frozenset()
                ),
            )

    def test_project_record_tensions_require_exact_poles_and_rebalance_trigger(self) -> None:
        invalid_cases = []
        missing = _record()
        del missing["tensions"][0]["rebalance_trigger"]  # type: ignore[index]
        invalid_cases.append(missing)
        unknown = _record()
        unknown["tensions"][0]["resolution"] = "collapse the tension"  # type: ignore[index]
        invalid_cases.append(unknown)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for number, data in enumerate(invalid_cases):
                case_root = root / str(number)
                _write_record(case_root, data)
                with self.subTest(number=number), self.assertRaisesRegex(
                    pc.ProjectCatalogueError, "tensions"
                ):
                    pc.load_project_records(case_root)

    def test_operational_contract_maps_lifecycle_refs_to_org054_obligations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = _record()
            data["operational_contract"]["lifecycle"]["install"] = ["evidence:not-an-obligation"]  # type: ignore[index]
            _write_record(root, data)
            records = pc.load_project_records(root)
            issues = pc.validate_project_graph(records, frozenset())
            self.assertEqual({issue.code for issue in issues}, {"unmapped-lifecycle-ref"})
            self.assertIn("evidence:not-an-obligation", issues[0].detail)

    def test_org068_lifecycle_gate_keys_are_exact_and_empty_means_gap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = _record()
            data["operational_contract"]["lifecycle"] = {  # type: ignore[index]
                "install": [],
                "configure": [],
                "health_monitor": [],
                "update": [],
                "remove": [],
            }
            _write_record(root, data)
            record = pc.load_project_records(root)["alpha-project"]
            self.assertEqual(
                record.operational_contract.lifecycle.gaps,
                ("install", "configure", "health_monitor", "update", "remove"),
            )
            invalid = copy.deepcopy(data)
            invalid["project_id"] = "invalid-stage-project"
            invalid["operational_contract"]["lifecycle"]["monitor"] = []  # type: ignore[index]
            invalid_root = root / "invalid"
            _write_record(invalid_root, invalid)
            with self.assertRaisesRegex(pc.ProjectCatalogueError, "lifecycle"):
                pc.load_project_records(invalid_root)

    def test_strict_yaml_rejects_duplicate_alias_tag_unknown_and_symlink_records(self) -> None:
        base = yaml.safe_dump(_record(), sort_keys=False)
        cases = {
            "duplicate": base + "project_id: duplicate-project\n",
            "alias": base.replace(
                "telos: Produce the outcome owned by alpha-project",
                "telos: &telos Produce the outcome owned by alpha-project",
            ).replace("role: project-steward", "role: *telos"),
            "tag": base.replace("telos: ", "telos: !!str ", 1),
            "unknown": base + "repository: must-not-define-identity\n",
            "merge": base.replace(
                "boundary:\n",
                "boundary:\n  <<: {inside: [], outside: [], not_owned: []}\n",
                1,
            ),
            "invalid-time": base.replace("2026-08-26T00:00:00Z", "2026-08-26"),
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, content in cases.items():
                case_root = root / name
                case_root.mkdir()
                (case_root / "alpha-project.yaml").write_text(content, encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(pc.ProjectCatalogueError):
                    pc.load_project_records(case_root)
            symlink_root = root / "symlink"
            symlink_root.mkdir()
            target = root / "target.yaml"
            target.write_text(base, encoding="utf-8")
            (symlink_root / "alpha-project.yaml").symlink_to(target)
            with self.assertRaisesRegex(pc.ProjectCatalogueError, "symlink"):
                pc.load_project_records(symlink_root)
            oversized_root = root / "oversized"
            oversized_root.mkdir()
            (oversized_root / "alpha-project.yaml").write_bytes(
                b"x" * (pc.MAX_RECORD_BYTES + 1)
            )
            with self.assertRaisesRegex(pc.ProjectCatalogueError, "exceeds"):
                pc.load_project_records(oversized_root)
            mismatch_root = root / "filename-mismatch"
            mismatch_root.mkdir()
            (mismatch_root / "wrong-name.yaml").write_text(base, encoding="utf-8")
            with self.assertRaisesRegex(pc.ProjectCatalogueError, "does not match"):
                pc.load_project_records(mismatch_root)

    def test_generated_schema_matches_typed_parser_contract(self) -> None:
        schema = json.loads(pc.generate_project_record_schema())
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(pc.PROJECT_RECORD_KEYS))
        self.assertEqual(
            set(schema["properties"]["classification"]["enum"]),
            {item.value for item in pc.ProjectClassification},
        )
        lifecycle = schema["properties"]["operational_contract"]["properties"]["lifecycle"]
        self.assertFalse(lifecycle["additionalProperties"])
        self.assertEqual(set(lifecycle["required"]), set(pc.LIFECYCLE_COVERAGE_KEYS))

        def assert_objects_are_closed(value: object) -> None:
            if isinstance(value, dict):
                if value.get("type") == "object":
                    self.assertIs(value.get("additionalProperties"), False)
                for child in value.values():
                    assert_objects_are_closed(child)
            elif isinstance(value, list):
                for child in value:
                    assert_objects_are_closed(child)

        assert_objects_are_closed(schema)

    def test_cli_generate_then_check_real_tmp_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write_record(
                repo / "projects" / "records",
                _record("goal-owned-project", parent_goal_ref="goal:durable-goal"),
            )
            args = ["generate", "--repo-root", str(repo), "--goal-key", "durable-goal"]
            self.assertEqual(pc.main(args), 0)
            self.assertEqual(
                pc.main(["check", "--repo-root", str(repo), "--goal-key", "durable-goal"]),
                0,
            )
            self.assertTrue((repo / "schemas" / "project-record.schema.json").is_file())
            self.assertTrue((repo / "projects" / "index.json").is_file())

    def test_live_project_catalogue_is_valid(self) -> None:
        self.assertEqual(pc.check_project_catalogue(REPO_ROOT, frozenset()), ())

    def test_ci_paths_cover_project_records_and_schema(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # T-5001: ci.yml has no push trigger (the nightly covers main); only the PR list.
        _, pull_request = workflow.split("  pull_request:", 1)
        for section in (pull_request,):
            self.assertIn("'projects/**'", section)
            self.assertIn("'schemas/project-record.schema.json'", section)

    def test_new_project_bootstrap_orders_formation_before_git_init(self) -> None:
        """T-2414 plan's named T-3166 acceptance test: sop/standard/new-project-bootstrap.md
        Procedure must open with a mandatory formation precondition that
        precedes the first `git init` step, AND the repository safeguards
        (`.gitignore first`, source-only tracking) must remain present after
        `git init` — a regression that deleted safeguards to accommodate the
        formation-first insertion would break bootstrap integrity, so both
        halves are checked in-place."""
        sop = (REPO_ROOT / "sop" / "standard" / "new-project-bootstrap.md").read_text(
            encoding="utf-8"
        )
        procedure = sop.split("## Procedure", 1)[1].split("## Boundary", 1)[0]

        # The Procedure section MUST split cleanly at the first `git init` step so
        # we can independently assert what comes BEFORE it (formation precondition)
        # vs what comes AFTER it (surviving repository safeguards).
        first_git_init = "1. **`git init`**"
        self.assertIn(
            first_git_init, procedure,
            "new-project-bootstrap.md Procedure lost its numbered `1. git init` "
            "step (T-3166 T-2414-plan-named contract requires the git-init step "
            "to be identifiable as the first *repository* action)",
        )
        before_git_init, after_git_init = procedure.split(first_git_init, 1)

        # BEFORE git init: formation precondition + project-lifecycle SOP pointer.
        self.assertIn(
            "project-formation section of the",
            before_git_init,
            "new-project-bootstrap.md Procedure lost its mandatory formation "
            "precondition pointing at project-lifecycle.md — T-3166 T-2414-plan-"
            "named contract requires formation to precede git init",
        )
        self.assertIn(
            "project-lifecycle",
            before_git_init,
            "new-project-bootstrap.md Procedure's formation precondition lost "
            "its pointer to project-lifecycle.md (SOP-of-record for §Formation)",
        )
        self.assertIn(
            "project record",
            before_git_init,
            "new-project-bootstrap.md Procedure's formation precondition lost "
            "the canonical-project-record requirement",
        )

        # AFTER git init: repository safeguards (`.gitignore first`, source-only
        # tracking) must survive the T-3166 amendment — deleting them to
        # accommodate the formation-first insertion would be the exact
        # regression this test guards against.
        self.assertIn(
            "`.gitignore` first",
            after_git_init,
            "new-project-bootstrap.md Procedure lost its `.gitignore first` "
            "safeguard — the T-3166 formation-precedes-bootstrap amendment must "
            "NOT delete the pre-existing repository safeguards",
        )
        self.assertIn(
            "Source only is tracked",
            after_git_init,
            "new-project-bootstrap.md Procedure lost its source-only-tracked "
            "safeguard note — same reason as above (T-3166 amendment must not "
            "delete existing safeguards)",
        )

    def test_brief_and_reviewer_contract_require_mechanical_chain_check(self) -> None:
        """T-2414 plan's named T-3166 acceptance test: BOTH D2 surfaces
        (team-orchestrator brief-construction + pre-implementation reviewer
        discipline) must name the mechanically-resolvable project/purpose chain
        fields — `project_id`, `nearest_intent_ref`, chain terminus (including
        the rootless-project case), and current developmental mode. Each
        surface is checked in its own section so a regression in one file
        cannot hide behind a whole-file substring match on the other."""
        brief = (
            REPO_ROOT / "sop" / "standard" / "team-orchestrator-brief-construction.md"
        ).read_text(encoding="utf-8")
        reviewer = (
            REPO_ROOT / "sop" / "standard" / "pre-implementation-reviewer-discipline.md"
        ).read_text(encoding="utf-8")

        for label, surface in (("brief-construction", brief), ("reviewer-discipline", reviewer)):
            self.assertIn(
                "project_id",
                surface,
                f"D2 surface {label} lost its `project_id` field naming (T-3166 "
                f"T-2414-plan-named contract requires both surfaces name the "
                f"mechanically-resolvable project/purpose chain fields)",
            )
            self.assertIn(
                "nearest_intent_ref",
                surface,
                f"D2 surface {label} lost its `nearest_intent_ref` field naming",
            )
            self.assertIn(
                "developmental mode",
                surface,
                f"D2 surface {label} lost its `developmental mode` naming — the "
                f"mode-in-chain requirement is part of the ORG-054 mechanical "
                f"resolve gate",
            )

        # Rootless-terminus semantic: appears in the brief's canonical-structure
        # section verbatim; in the reviewer's chain-check as the "invented/default
        # root" negation (a chain that terminates WITHOUT an invented root is the
        # rootless case). Checking each surface's own phrasing so we don't crypto-
        # equivalence-collapse them (they're deliberately different-worded).
        self.assertIn(
            "Rootless projects terminate at their own telos",
            brief,
            "brief-construction lost its rootless-terminus rule (the sentence "
            "naming that rootless projects terminate at their own telos)",
        )
        self.assertIn(
            "without an invented/default root",
            reviewer,
            "reviewer-discipline lost its 'without an invented/default root' "
            "chain-terminus check (the negation form of the rootless-terminus "
            "rule brief-construction names positively)",
        )

    def test_split_merge_and_retire_require_obligation_disposition(self) -> None:
        """T-2414 plan's named T-3169 acceptance test: sop/standard/project-lifecycle.md
        must mandate a non-empty obligation_dispositions transition receipt for
        replace/split/merge/retire alike (ORG-054 closure continuity), each
        checked in its own section so a regression in one section cannot hide
        behind a whole-file substring match."""
        sop = (REPO_ROOT / "sop" / "standard" / "project-lifecycle.md").read_text(
            encoding="utf-8"
        )
        replacement = sop.split("## 4. Replacement", 1)[1].split("## 5. Split", 1)[0]
        split = sop.split("## 5. Split", 1)[1].split("## 6. Merge", 1)[0]
        merge = sop.split("## 6. Merge", 1)[1].split("## 7. Retirement", 1)[0]
        retirement = sop.split("## 7. Retirement", 1)[1].split("## 8.", 1)[0]
        self.assertIn(
            "cannot complete with an empty `obligation_dispositions` list", replacement
        )
        self.assertIn(
            "`obligation_dispositions` non-empty unless a reviewer verifies", split
        )
        self.assertIn(
            "`obligation_dispositions` covering every merged project's obligations", merge
        )
        self.assertIn(
            "`obligation_dispositions` non-empty unless a reviewer verifies", retirement
        )


if __name__ == "__main__":
    unittest.main()
