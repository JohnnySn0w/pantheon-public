"""T-5633: the declared ``legacy_units`` instance-binding field.

Intent: on the ORG-052 broker path no plan launches a seat before its legacy
ho-resumer is decommissioned, and E40 is not lost on seats that never had one.
The installer plans ``service.ho-resumer.decommission`` ONLY on a binding that
declares ``legacy_units: [ho-resumer]`` (a closed vocabulary, default empty);
in the real instance.yaml that is orchestrator's binding alone.

Pinned here:
(a) no field -> no decommission op; the field -> exactly the T-5448 op, with
    today's id, kind, target, desired and position;
(b) the closed vocabulary: unknown names, a bare string, non-string entries and
    duplicates are refused; absent, null and [] mean empty;
(c) the real instance binds legacy_units only on orchestrator;
(d) plan equality: on every real persona, toggling the field changes the plan
    by exactly the decommission op and nothing else.

Plan inspection only: tmp roots, no host, no backend.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from tools.persona_installer import (
    CODEX_KEEP_GOING_UNITS,
    HO_RESUMER_UNITS,
    LEGACY_UNIT_SETS,
    Manifest,
    ManifestError,
    PersonaInstaller,
)
from tools.test_persona_installer import REPO_ROOT, _manifest_dict

OP_ID = "service.ho-resumer.decommission"
KIND = "seat-service-decommission"
RECOVERY_ROLE = "recovery"


class _ManifestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.path = self.root / "instance.yaml"
        self.installer = PersonaInstaller(REPO_ROOT)

    def load(self, data: dict) -> Manifest:
        self.path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return Manifest.load(self.path)

    def fixture(self, legacy_units=None, *, set_field: bool = True) -> dict:
        data = _manifest_dict(second_persona=True)
        if set_field:
            data["roles"][RECOVERY_ROLE]["legacy_units"] = legacy_units
        return data

    def plan(self, manifest: Manifest, persona: str = "recovery"):
        return self.installer.plan(manifest, persona, root=self.root / "projection")


class EmissionTests(_ManifestCase):
    def test_vocabulary_is_closed_to_the_declared_migration_sets(self) -> None:
        self.assertEqual(
            dict(LEGACY_UNIT_SETS),
            {
                "ho-resumer": HO_RESUMER_UNITS,
                "codex-keep-going": CODEX_KEEP_GOING_UNITS,
            },
        )

    def test_no_field_plans_no_decommission(self) -> None:
        manifest = self.load(self.fixture(set_field=False))
        for persona in ("recovery", "consultant"):
            with self.subTest(persona=persona):
                self.assertEqual(manifest.binding(persona).legacy_units, ())
                plan = self.plan(manifest, persona)
                self.assertNotIn(OP_ID, [op.operation_id for op in plan.operations])
                self.assertNotIn(KIND, [op.kind for op in plan.operations])

    def test_the_field_plans_exactly_the_t5448_op_in_its_place(self) -> None:
        manifest = self.load(self.fixture(["ho-resumer"]))
        self.assertEqual(manifest.binding("recovery").legacy_units, ("ho-resumer",))
        plan = self.plan(manifest)
        ids = [op.operation_id for op in plan.operations]
        self.assertEqual(ids.count(OP_ID), 1)
        op = plan.operations[ids.index(OP_ID)]
        home = self.root / "projection" / "home" / "claude-fallback"
        self.assertEqual(op.kind, KIND)
        self.assertEqual(op.layer, 1)
        self.assertEqual(op.target, "claude-fallback")
        self.assertEqual(op.owner, "claude-fallback")
        self.assertEqual(op.privilege, "operator-required")
        self.assertEqual(dict(op.precondition), {"state": "must-pass"})
        self.assertIsNone(op.inverse)
        self.assertEqual(
            dict(op.desired),
            {
                "units": "pantheon-ho-resumer.timer,pantheon-ho-resumer.service",
                "unit_dir": str(home / ".config" / "systemd" / "user"),
                "quarantine_dir": str(
                    home / ".local" / "state" / "pantheon" / "persona-installer" / "recovery" / "quarantine" / "ho-resumer"
                ),
            },
        )
        self.assertEqual(op.provenance["required_capability"], "systemd.user:claude-fallback")
        # same slot as before T-5633: right after the phase-Q quarantine, before any fire
        self.assertEqual(ids[ids.index(OP_ID) - 1], "service.runtime-residue.quarantine")
        self.assertLess(ids.index(OP_ID), ids.index("harness.direct-fire"))

    def test_the_field_on_one_binding_does_not_leak_to_another(self) -> None:
        manifest = self.load(self.fixture(["ho-resumer"]))
        self.assertEqual(manifest.binding("consultant").legacy_units, ())
        self.assertNotIn(OP_ID, [op.operation_id for op in self.plan(manifest, "consultant").operations])

    def test_the_field_is_not_in_provenance(self) -> None:
        # D-3: provenance (and so every op's provenance) is unchanged by the field
        declared = self.plan(self.load(self.fixture(["ho-resumer"])))
        plain = self.plan(self.load(self.fixture(set_field=False)))
        self.assertEqual(dict(declared.provenance), dict(plain.provenance))
        self.assertNotIn("legacy_units", declared.provenance)


class VocabularyTests(_ManifestCase):
    def test_empty_spellings_mean_no_units(self) -> None:
        for value in (None, []):
            with self.subTest(value=value):
                manifest = self.load(self.fixture(value))
                self.assertEqual(manifest.binding("recovery").legacy_units, ())
                self.assertNotIn(OP_ID, [op.operation_id for op in self.plan(manifest).operations])

    def test_out_of_vocabulary_values_are_refused(self) -> None:
        cases = {
            "unknown name": (["bogus"], "unknown legacy unit set 'bogus'"),
            "unknown beside a known": (["ho-resumer", "ho-resumer-v2"], "unknown legacy unit set"),
            "bare string": ("ho-resumer", "legacy_units must be a list"),
            "mapping": ({"ho-resumer": True}, "legacy_units must be a list"),
            "non-string entry": ([1], "entries must be strings"),
            "duplicate": (["ho-resumer", "ho-resumer"], "duplicate 'ho-resumer'"),
        }
        for name, (value, message) in cases.items():
            with self.subTest(case=name):
                manifest = self.load(self.fixture(value))
                with self.assertRaisesRegex(ManifestError, message):
                    manifest.binding("recovery")
                with self.assertRaisesRegex(ManifestError, message):
                    self.plan(manifest)

    def test_a_bad_row_does_not_break_another_bindings_load(self) -> None:
        # D-1: validation is per binding, so a bad value on recovery's row leaves
        # consultant's binding (and the manifest load itself) working
        manifest = self.load(self.fixture(["bogus"]))
        self.assertEqual(manifest.binding("consultant").legacy_units, ())


class RealInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = yaml.safe_load((REPO_ROOT / "instance.yaml").read_text(encoding="utf-8"))
        cls.manifest = Manifest.load(REPO_ROOT / "instance.yaml")
        cls.personas = sorted(
            {raw["persona"] for raw in cls.manifest.roles.values() if isinstance(raw, dict) and raw.get("persona")}
        )

    def test_real_bindings_declare_only_their_known_legacy_units(self) -> None:
        expected_by_persona = {
            "orchestrator": ("ho-resumer",),
            "codex": ("codex-keep-going",),
        }
        for persona in self.personas:
            with self.subTest(persona=persona):
                expected = expected_by_persona.get(persona, ())
                self.assertEqual(self.manifest.binding(persona).legacy_units, expected)
        self.assertEqual(
            self.document["roles"]["head-engineering-orchestrator"]["legacy_units"], ["ho-resumer"]
        )
        self.assertEqual(
            self.document["roles"]["peer-daemon-codex"]["legacy_units"], ["codex-keep-going"]
        )

    def test_toggling_the_field_changes_each_plan_by_exactly_the_decommission(self) -> None:
        installer = PersonaInstaller(REPO_ROOT)
        roles = {raw["persona"]: role for role, raw in self.document["roles"].items() if isinstance(raw, dict)}
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "instance.yaml"
            root = Path(work) / "projection"

            def plan_with(persona: str, declared: bool):
                data = yaml.safe_load((REPO_ROOT / "instance.yaml").read_text(encoding="utf-8"))
                row = data["roles"][roles[persona]]
                row.pop("legacy_units", None)
                if declared:
                    row["legacy_units"] = ["ho-resumer"]
                # same path for both variants: the manifest path is a plan input
                path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
                return installer.plan(Manifest.load(path), persona, root=root)

            for persona in self.personas:
                with self.subTest(persona=persona):
                    with_op = plan_with(persona, True)
                    without = plan_with(persona, False)
                    with_ids = [op.operation_id for op in with_op.operations]
                    self.assertEqual(with_ids.count(OP_ID), 1)
                    stripped = tuple(op for op in with_op.operations if op.operation_id != OP_ID)
                    self.assertEqual(stripped, without.operations)
                    self.assertEqual(len(with_op.operations), len(without.operations) + 1)
                    for name in ("operator_actions", "provenance", "role", "persona", "account",
                                 "configured_harness", "selected_harness", "root", "instance", "schema"):
                        self.assertEqual(getattr(with_op, name), getattr(without, name), name)


if __name__ == "__main__":
    unittest.main()
