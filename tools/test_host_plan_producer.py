"""T-5533: the unprivileged plan producer and the registry's plan records.

Intent (T-2652 / ORG-052 section 1 and 3): plan COMPUTATION leaves the
privileged broker. ``tools.host_plan_producer`` plans at registration time and
emits canonical plan bytes; the registry pins them by sha256 per (seat,
manifest revision); the broker only reads and checks them. Pinned here:

* the producer's bytes are exactly the in-process installer plan, canonical,
  and parse back to a digest-valid ``InstallPlan``;
* determinism: two fresh ``python3 -I`` producer runs under different TZ, LANG,
  HOME and a bogus ``GIT_DIR`` give byte-identical output (the ``GIT_*`` scrub);
* every producer refusal (own framework root, manifest sha before and after
  planning, template binding, target, framework revision and digest, a stale
  registered ``framework_sha256``, malformed records, exclusive ``--out``);
* T-5617: every broker-partition refusal (residue cap, routed one-per-kind
  bound, admitted op after residue, launch before containment) is a clean
  rc-2 refusal through both ``produce_plan`` and ``main()``, writing nothing;
* staleness (orchestrator's scope add): after a framework-copy edit the previously
  registered plan is refused by the registry check and the producer refuses a
  manifest record carrying the old digest;
* the registry's ``plans`` records: load-time validation and ``read_plan_bytes``
  (registered-record equality, root-owned loader, size cap, sha256 drift).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tools import host_plan_producer as HPP
from tools import persona_installer as PI
from tools._host_control_plan_fixtures import framework_overlay, register_plan
from tools.host_control.contracts import RegistryError
from tools.host_control.planner import SEAT_LOCAL_RESIDUE_LIMIT
from tools.host_control.registry import PLAN_MAX_BYTES, SeatRegistry
from tools.host_control.trusted_files import RootOwnedFileLoader
from tools.persona_installer_contracts import InstallPlan, _canonical_json
from tools.test_host_control_core import _manifest_dict

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_IDS = (
    "tools/systemd/pantheon-persona-consume@.service",
    "tools/systemd/pantheon-persona-consume@.timer",
    "tools/systemd/pantheon-persona-quota@.service",
    "tools/systemd/pantheon-persona-quota@.timer",
)
SEAT = {
    "seat_id": "recovery",
    "uid": 1002,
    "persona": "recovery",
    "role": "recovery",
    "instance": "test-instance",
    "account": "claude-fallback",
    "harness": "codex",
    "install_root": "/projection",
}


class ProducerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.root.chmod(0o700)
        self.manifest_path = self.root / "instance.yaml"
        self.manifest_path.write_text(yaml.safe_dump(_manifest_dict(), sort_keys=False), encoding="utf-8")
        self.manifest_path.chmod(0o600)
        self.manifest = {
            "revision": "manifest-r1",
            "path": str(self.manifest_path),
            "sha256": hashlib.sha256(self.manifest_path.read_bytes()).hexdigest(),
            "framework_revision": "test-rev",
            "framework_sha256": HPP.framework_artifact_digest(TEMPLATE_IDS),
            "template_ids": list(TEMPLATE_IDS),
        }

    def produce(self, seat=None, manifest=None, framework_root=None) -> bytes:
        return HPP.produce_plan(
            framework_root=framework_root or REPO_ROOT,
            seat_record=seat or dict(SEAT),
            manifest_record=manifest or dict(self.manifest),
        )

    def in_process_plan(self) -> InstallPlan:
        frozen = PI.Manifest.from_bytes(self.manifest_path, self.manifest_path.read_bytes())
        return PI.PersonaInstaller(REPO_ROOT).plan(
            frozen, "recovery", harness_override="codex", root=Path("/projection")
        )


class ProducerOutputTest(ProducerCase):
    def test_bytes_are_the_canonical_in_process_installer_plan(self) -> None:
        self.assertEqual(self.produce(), _canonical_json(self.in_process_plan().to_dict()).encode("utf-8"))

    def test_bytes_parse_back_to_a_digest_valid_plan(self) -> None:
        data = self.produce()
        plan = InstallPlan.from_dict(json.loads(data))
        self.assertTrue(plan.validate_digest())
        self.assertEqual(_canonical_json(plan.to_dict()).encode("utf-8"), data)
        self.assertEqual(plan.provenance["source_artifact_sha256"], self.manifest["framework_sha256"])

    def test_framework_digest_is_the_installer_artifact_digest(self) -> None:
        self.assertEqual(
            HPP.framework_artifact_digest(TEMPLATE_IDS),
            self.in_process_plan().provenance["source_artifact_sha256"],
        )

    def test_git_environment_is_scrubbed_during_planning_and_restored(self) -> None:
        clean = self.produce()
        with mock.patch.dict(os.environ, {"GIT_DIR": "/nonexistent-gitdir", "GIT_WORK_TREE": "/x"}):
            self.assertEqual(self.produce(), clean)
            self.assertEqual(os.environ["GIT_DIR"], "/nonexistent-gitdir")
            # control: the same variable DOES change an unscrubbed plan
            self.assertNotEqual(_canonical_json(self.in_process_plan().to_dict()).encode("utf-8"), clean)

    def _cli(self, out: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                str(REPO_ROOT / "tools" / "host_plan_producer.py"),
                "--framework-root",
                str(REPO_ROOT),
                "--seat-record",
                json.dumps(SEAT),
                "--manifest-record",
                json.dumps(self.manifest),
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )

    def test_two_fresh_processes_under_different_environments_are_byte_identical(self) -> None:
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        one = self._cli(self.root / "one.json", {**base, "HOME": str(self.root), "TZ": "UTC", "LANG": "C"})
        two = self._cli(
            self.root / "two.json",
            {
                **base,
                "HOME": "/nonexistent-home",
                "TZ": "Pacific/Kiritimati",
                "LANG": "C.UTF-8",
                "GIT_DIR": "/nonexistent-gitdir",
            },
        )
        self.assertEqual(one.returncode, 0, one.stderr)
        self.assertEqual(two.returncode, 0, two.stderr)
        first = (self.root / "one.json").read_bytes()
        second = (self.root / "two.json").read_bytes()
        self.assertEqual(first, second)
        self.assertEqual(first, self.produce())
        record = json.loads(one.stdout)
        self.assertEqual(
            record,
            {
                "seat_id": "recovery",
                "manifest_revision": "manifest-r1",
                "path": str(self.root / "one.json"),
                "sha256": hashlib.sha256(first).hexdigest(),
            },
        )
        self.assertEqual((self.root / "one.json").stat().st_mode & 0o777, 0o644)

    def test_out_file_is_0644_even_under_umask_077(self) -> None:
        out = self.root / "umask.json"
        previous = os.umask(0o077)
        try:
            HPP._write_exclusive(out, b"{}")
        finally:
            os.umask(previous)
        self.assertEqual(out.stat().st_mode & 0o777, 0o644)

    def test_cli_refuses_an_existing_or_relative_out_path(self) -> None:
        existing = self.root / "exists.json"
        existing.write_bytes(b"keep")
        for out in (existing, Path("relative.json")):
            with self.subTest(out=str(out)):
                proc = self._cli(out, {"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
                self.assertEqual(proc.returncode, 2)
                self.assertIn("refused", proc.stderr)
        self.assertEqual(existing.read_bytes(), b"keep")


def _replace_operations(plan: InstallPlan, operations) -> InstallPlan:
    values = {field.name: getattr(plan, field.name) for field in dataclasses.fields(plan) if field.name != "digest"}
    values["operations"] = tuple(operations)
    return InstallPlan.create(**values)


def _operation(plan: InstallPlan, operation_id: str):
    return next(op for op in plan.operations if op.operation_id == operation_id)


def _over_the_residue_cap(plan: InstallPlan) -> InstallPlan:
    template = _operation(plan, "host.identity")
    extras = tuple(
        dataclasses.replace(template, operation_id=f"extra.residue-{index}", kind="codex-telegram-mcp-check")
        for index in range(SEAT_LOCAL_RESIDUE_LIMIT + 1)
    )
    return _replace_operations(plan, plan.operations + extras)


def _a_routed_kind_twice(plan: InstallPlan) -> InstallPlan:
    # T-5608: the residue quarantine is admitted, so the real codex plan routes
    # nothing; a synthetic residue must-run-before-launch op (a workspace-trust
    # kind, out of the envelope) makes it route, so the duplicate is a ROUTED one
    must_run = dataclasses.replace(
        _operation(plan, "host.identity"), operation_id="synthetic.workspace-trust", kind="seat-claude-workspace-trust"
    )
    second = dataclasses.replace(_operation(plan, "connective.unit-fire"), operation_id="connective.unit-fire-2")
    return _replace_operations(plan, (must_run,) + plan.operations + (second,))


def _admitted_after_residue(plan: InstallPlan) -> InstallPlan:
    dependent = dataclasses.replace(
        _operation(plan, "connective.config-dir"),
        operation_id="connective.after-residue",
        precondition={"after": "periodic.daemon-reload"},
    )
    return _replace_operations(plan, plan.operations + (dependent,))


def _containment_after_a_launch(plan: InstallPlan) -> InstallPlan:
    quarantine = _operation(plan, "service.runtime-residue.quarantine")
    rest = [op for op in plan.operations if op is not quarantine]
    index = next(i for i, op in enumerate(rest) if op.operation_id == "connective.unit-fire")
    return _replace_operations(plan, rest[: index + 1] + [quarantine] + rest[index + 1 :])


class ProducerRefusalTest(ProducerCase):
    def assertRefused(self, message: str, **kwargs) -> None:
        with self.assertRaises(HPP.PlanProducerError) as caught:
            self.produce(**kwargs)
        self.assertIn(message, str(caught.exception))

    def test_refuses_a_framework_root_other_than_its_own(self) -> None:
        self.assertRefused("own repo root", framework_root=self.root)

    def test_refuses_manifest_bytes_that_differ_from_the_registered_sha(self) -> None:
        self.assertRefused("differ from the registered sha256", manifest={**self.manifest, "sha256": "0" * 64})

    def test_refuses_a_manifest_that_drifts_while_planning(self) -> None:
        original = PI.PersonaInstaller.plan

        def drifting(installer, *args, **kwargs):
            with self.manifest_path.open("ab") as handle:
                handle.write(b"# drift\n")
            return original(installer, *args, **kwargs)

        with mock.patch.object(PI.PersonaInstaller, "plan", drifting):
            self.assertRefused("drifted while planning")

    def test_refuses_a_template_binding_mismatch(self) -> None:
        swapped = [TEMPLATE_IDS[1], TEMPLATE_IDS[0], *TEMPLATE_IDS[2:]]
        self.assertRefused("template binding mismatch", manifest={**self.manifest, "template_ids": swapped})

    def test_refuses_a_target_mismatch(self) -> None:
        for field, value in (("account", "someone-else"), ("instance", "other"), ("role", "worker")):
            with self.subTest(field=field):
                self.assertRefused(f"target mismatch: {field}", seat={**SEAT, field: value})

    def test_refuses_a_framework_revision_mismatch(self) -> None:
        self.assertRefused("framework revision mismatch", manifest={**self.manifest, "framework_revision": "other"})

    def test_refuses_a_stale_registered_framework_digest(self) -> None:
        self.assertRefused("stale against the framework copy", manifest={**self.manifest, "framework_sha256": "f" * 64})

    def test_refuses_malformed_records(self) -> None:
        self.assertRefused("invalid seat record fields", seat={**SEAT, "extra": 1})
        self.assertRefused("invalid manifest record fields", manifest={k: v for k, v in self.manifest.items() if k != "sha256"})
        self.assertRefused("manifest path must be absolute", manifest={**self.manifest, "path": "instance.yaml"})


    # T-5617: the producer runs the broker partition (through
    # refuse_launch_before_containment); EVERY partition refusal is a
    # PolicyDenied and must come out as a clean refusal (rc 2, the "refused"
    # line on stderr, nothing at --out), never a traceback. Each plan is the
    # real codex fixture plan, transformed.
    CASES = {
        "out-of-envelope residue over the cap": (_over_the_residue_cap, "family_denied"),
        "a routed launch kind twice": (_a_routed_kind_twice, "more than one routed unit-fire op"),
        "an admitted op ordered after residue": (
            _admitted_after_residue,
            "kinds outside executor envelope: seat-periodic-daemon-reload",
        ),
        "launch before containment": (_containment_after_a_launch, "launch before containment"),
    }

    @contextlib.contextmanager
    def crafted(self, transform):
        original = PI.PersonaInstaller.plan

        def planned(installer, *args, **kwargs):
            return transform(original(installer, *args, **kwargs))

        with mock.patch.object(PI.PersonaInstaller, "plan", planned):
            yield

    def run_main(self, out: Path) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = [
            "--framework-root", str(REPO_ROOT),
            "--seat-record", json.dumps(SEAT),
            "--manifest-record", json.dumps(self.manifest),
            "--out", str(out),
        ]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = HPP.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_the_untransformed_fixture_plan_partitions_cleanly(self) -> None:
        # control: the refusals below come from the transforms, not the fixture
        with self.crafted(lambda plan: plan):
            self.produce()

    def assertPartitionRefused(self, transform, message: str) -> None:
        with self.crafted(transform):
            with self.assertRaises(HPP.PlanProducerError) as caught:
                self.produce()
            self.assertIn(message, str(caught.exception))
            self.assertLessEqual(len(str(caught.exception)), HPP.REFUSAL_DETAIL_LIMIT)
            out = self.root / "refused-plan.json"
            code, stdout, stderr = self.run_main(out)
        self.assertEqual(code, 2, stderr)
        self.assertTrue(stderr.startswith("host_plan_producer: refused: "), stderr)
        self.assertIn(message, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(stdout, "")
        self.assertFalse(out.exists())
        self.assertFalse(os.path.lexists(out))

    def test_refuses_out_of_envelope_residue_over_the_cap(self) -> None:
        self.assertPartitionRefused(*self.CASES["out-of-envelope residue over the cap"])

    def test_refuses_a_routed_launch_kind_appearing_twice(self) -> None:
        self.assertPartitionRefused(*self.CASES["a routed launch kind twice"])

    def test_refuses_an_admitted_op_ordered_after_a_residue_op(self) -> None:
        self.assertPartitionRefused(*self.CASES["an admitted op ordered after residue"])

    def test_refuses_launch_before_containment_through_main(self) -> None:
        self.assertPartitionRefused(*self.CASES["launch before containment"])


class FrameworkStalenessTest(ProducerCase):
    """orchestrator's scope add: a framework-copy edit that changes the digest makes the
    previously registered plan refusable and blocks re-planning on the old digest."""

    def setUp(self) -> None:
        super().setUp()
        self.framework = framework_overlay(self.root / "framework", REPO_ROOT, TEMPLATE_IDS)
        patcher = mock.patch.object(HPP, "REPO_ROOT", self.framework)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manifest["framework_sha256"] = HPP.framework_artifact_digest(TEMPLATE_IDS)
        self.registry_path = self.root / "registry.json"
        self.files = RootOwnedFileLoader._open_for_test((self.root,), expected_owner_uid=os.getuid())
        self.addCleanup(self.files.close)

    def registry(self) -> SeatRegistry:
        return SeatRegistry.load(self.registry_path, files=self.files, framework_root=self.framework)

    def write_registry(self, plan_bytes: bytes) -> None:
        value = {
            "schema": "pantheon-host-seat-registry/v1",
            "seats": [dict(SEAT)],
            "manifests": [dict(self.manifest)],
        }
        register_plan(value, self.root, plan_bytes, seat_id="recovery", manifest_revision="manifest-r1")
        self.registry_path.write_text(json.dumps(value), encoding="utf-8")
        self.registry_path.chmod(0o600)

    def test_registered_digest_matches_the_overlay_copy_before_any_edit(self) -> None:
        self.write_registry(self.produce(framework_root=self.framework))
        registry = self.registry()
        registry.check_framework_digest(registry.manifest("manifest-r1"))

    def test_a_framework_copy_edit_makes_the_registered_plan_stale(self) -> None:
        for artifact in ("tools/persona_installer_contracts.py", TEMPLATE_IDS[2]):
            with self.subTest(artifact=artifact):
                self.manifest["framework_sha256"] = HPP.framework_artifact_digest(TEMPLATE_IDS)
                self.write_registry(self.produce(framework_root=self.framework))
                registry = self.registry()
                record = registry.manifest("manifest-r1")
                registry.check_framework_digest(record)
                target = self.framework / artifact
                target.write_bytes(target.read_bytes() + b"\n# framework copy updated\n")
                # (1) the previously registered plan is refused at the registry check
                with self.assertRaisesRegex(RegistryError, "stale against the framework copy"):
                    registry.check_framework_digest(record)
                with self.assertRaisesRegex(RegistryError, "stale against the framework copy"):
                    self.registry().check_framework_digest(record)
                # (2) the producer refuses a manifest record carrying the old digest
                with self.assertRaisesRegex(HPP.PlanProducerError, "stale against the framework copy"):
                    self.produce(framework_root=self.framework)

    def test_registry_check_equals_the_installer_artifact_digest(self) -> None:
        self.write_registry(self.produce(framework_root=self.framework))
        expected = PI._artifact_digest(
            self.framework,
            tuple(Path(item) for item in (*PI.FRAMEWORK_CODE_ARTIFACTS, *TEMPLATE_IDS)),
        )
        self.assertEqual(expected, self.manifest["framework_sha256"])
        registry = self.registry()
        registry.check_framework_digest(registry.manifest("manifest-r1"))
        with self.assertRaises(RegistryError):
            registry.check_framework_digest(
                dataclasses.replace(registry.manifest("manifest-r1"), framework_sha256="0" * 64)
            )

    def test_framework_root_outside_the_trusted_roots_is_refused(self) -> None:
        self.write_registry(self.produce(framework_root=self.framework))
        with self.assertRaisesRegex(RegistryError, "framework root is outside trusted roots"):
            SeatRegistry.load(self.registry_path, files=self.files, framework_root=REPO_ROOT)


class RegistryPlanRecordTest(ProducerCase):
    def setUp(self) -> None:
        super().setUp()
        self.files = RootOwnedFileLoader._open_for_test((self.root,), expected_owner_uid=os.getuid())
        self.addCleanup(self.files.close)
        self.registry_path = self.root / "registry.json"
        self.plan_bytes = self.produce()

    def registry_dict(self) -> dict:
        value = {
            "schema": "pantheon-host-seat-registry/v1",
            "seats": [dict(SEAT), {**SEAT, "seat_id": "peer", "uid": 1004, "persona": "peer"}],
            "manifests": [dict(self.manifest)],
        }
        register_plan(value, self.root, self.plan_bytes, seat_id="recovery", manifest_revision="manifest-r1")
        return value

    def load(self, value: dict) -> SeatRegistry:
        self.registry_path.write_text(json.dumps(value), encoding="utf-8")
        self.registry_path.chmod(0o600)
        return SeatRegistry.load(self.registry_path, files=self.files)

    def test_registered_plan_reads_back_and_plans_key_is_optional(self) -> None:
        registry = self.load(self.registry_dict())
        record = registry.plan_record("recovery", "manifest-r1")
        self.assertEqual(registry.read_plan_bytes(record), self.plan_bytes)
        without = self.registry_dict()
        del without["plans"]
        bare = self.load(without)
        with self.assertRaisesRegex(RegistryError, "no registered plan"):
            bare.plan_record("recovery", "manifest-r1")

    def test_load_rejects_invalid_plan_records(self) -> None:
        def case(mutate):
            value = self.registry_dict()
            mutate(value)
            return value

        cases = {
            "plans not a list": case(lambda v: v.__setitem__("plans", {})),
            "extra plan key": case(lambda v: v["plans"][0].__setitem__("extra", 1)),
            "unregistered seat": case(lambda v: v["plans"][0].__setitem__("seat_id", "nobody")),
            "unregistered revision": case(lambda v: v["plans"][0].__setitem__("manifest_revision", "r9")),
            "relative path": case(lambda v: v["plans"][0].__setitem__("path", "plan.json")),
            "outside roots": case(lambda v: v["plans"][0].__setitem__("path", "/etc/hostname")),
            "bad digest": case(lambda v: v["plans"][0].__setitem__("sha256", "XYZ")),
            "hash mismatch": case(lambda v: v["plans"][0].__setitem__("sha256", "0" * 64)),
            "duplicate": case(lambda v: v["plans"].append(dict(v["plans"][0]))),
            "unknown top key": case(lambda v: v.__setitem__("other", [])),
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(RegistryError):
                    self.load(value)

    def test_read_plan_bytes_refuses_drift_unregistered_records_symlinks_and_owner(self) -> None:
        registry = self.load(self.registry_dict())
        record = registry.plan_record("recovery", "manifest-r1")
        with self.assertRaisesRegex(RegistryError, "plan record is not registered"):
            registry.read_plan_bytes(dataclasses.replace(record, sha256="0" * 64))
        with self.assertRaisesRegex(RegistryError, "no registered plan"):
            registry.read_plan_bytes(dataclasses.replace(record, seat_id="peer"))

        inode = record.path.stat().st_ino
        original = __import__("tools.host_control.trusted_files", fromlist=["_validate_stat"])._validate_stat

        def other_owner(value, *, expected_owner_uid, kind):
            if value.st_ino == inode:
                raise RegistryError(f"untrusted {kind} owner")
            return original(value, expected_owner_uid=expected_owner_uid, kind=kind)

        with mock.patch("tools.host_control.trusted_files._validate_stat", other_owner):
            with self.assertRaisesRegex(RegistryError, "owner"):
                registry.read_plan_bytes(record)

        record.path.chmod(0o620)
        with self.assertRaisesRegex(RegistryError, "mode"):
            registry.read_plan_bytes(record)
        record.path.chmod(0o600)

        record.path.write_bytes(self.plan_bytes + b" ")
        with self.assertRaisesRegex(RegistryError, "registered plan hash drift"):
            registry.read_plan_bytes(record)

        real = self.root / "real-plan.json"
        real.write_bytes(self.plan_bytes)
        real.chmod(0o600)
        record.path.unlink()
        record.path.symlink_to(real)
        with self.assertRaises(RegistryError):
            registry.read_plan_bytes(record)

    def test_read_plan_bytes_caps_the_size(self) -> None:
        registry = self.load(self.registry_dict())
        record = registry.plan_record("recovery", "manifest-r1")
        record.path.write_bytes(b" " * (PLAN_MAX_BYTES + 1))
        with self.assertRaisesRegex(RegistryError, "size limit"):
            registry.read_plan_bytes(record)


if __name__ == "__main__":
    unittest.main()
