"""T-2920 U1 (HO ruling Q6): the framework-copy CLOSURE runs self-contained.

Intent (T-2920): a clean box converges to the whole authority boundary. The
broker runs with ProtectHome=yes and every daemon and the plan producer load
from the root-owned framework copy, so a file missing from the copy is a dead
daemon. This test materializes ``derive_closure`` ALONE in a fresh temp dir
(outside /home), then, in ``env -i PATH=/usr/bin:/bin python3 -I -B`` processes
with cwd in that temp dir:

* installs an audit hook that REFUSES (raises) any open/listdir/scandir under
  this repository or under /home, and asserts sys.path holds no repo path;
* drives every load path from the copy: the broker's ``load_daemon_runtime``
  through the default ``load_checkout_op_registry`` (its startup record must
  equal the in-repo one: gate 32a parity), the approval service modules plus a
  real ``ApprovalJournal.open`` (the SCHEMA_DIGEST check), the executor entry
  modules plus the framework-artifact digest over the copy, the ingress modules,
  and ``persona_bringup`` (the executor's direct-fire target);
* runs the plan producer from the copy: once under the hook (runpy), and once
  as the EXACT T-5533 registration command
  (``env -i PATH=/usr/bin:/bin python3 -I <copy>/tools/host_plan_producer.py
  --framework-root <copy> ...``) for a claude and a codex fixture seat and
  every binding of the repo's instance.yaml (the real path, D5). The setpriv
  drop to nobody and the nobody-owned 0700 staging dir need root, so this test
  cannot run them; tools/test_host_broker_install.py
  (ReviewFixesTest.test_med1_*) pins the PLANNED P6 argv, the --out path and
  the staging-dir owner as consistent with the producer user (review MED-1);
* cross-checks the executor's plan-driven framework reads (every
  framework-root target and artifact/bundle list in real produced plans) lie
  inside the closure.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from tools import host_broker_install as H
from tools.germinate.registry.contract_digest import PINNED_PYYAML_VERSIONS

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_IDS = [
    "tools/systemd/pantheon-persona-consume@.service",
    "tools/systemd/pantheon-persona-consume@.timer",
    "tools/systemd/pantheon-persona-quota@.service",
    "tools/systemd/pantheon-persona-quota@.timer",
    "tools/systemd/pantheon-mcp-allowlist-audit.service",
    "tools/systemd/pantheon-mcp-allowlist-audit.timer",
]
CLEAN_ENV = {"PATH": "/usr/bin:/bin"}
SYSTEM_PYTHON = "/usr/bin/python3"
_SYSTEM_PYYAML: list[str] = []


def system_pyyaml_version() -> str:
    """PyYAML of the production interpreter (/usr/bin/python3 -I), probed once.

    The broker's load path digests component contracts, which refuses an
    unpinned PyYAML emitter (contract_digest.PINNED_PYYAML_VERSIONS). A host or
    runner whose SYSTEM PyYAML is unpinned cannot run that path at all; the
    install plan refuses such a host at verify time (python3-yaml
    accepted_versions), and the test that reaches the digest skips there."""

    if not _SYSTEM_PYYAML:
        result = subprocess.run([SYSTEM_PYTHON, "-I", "-c", "import yaml; print(yaml.__version__)"],
                                env=CLEAN_ENV, capture_output=True, timeout=60, check=False)
        _SYSTEM_PYYAML.append(result.stdout.decode().strip() if result.returncode == 0 else "<not importable>")
    return _SYSTEM_PYYAML[0]


def require_pinned_system_pyyaml(case: unittest.TestCase) -> None:
    """Per test, only where the copy-side path reaches contract_digest (the
    load-paths test; the producer and the copy-listing tests do not)."""

    found = system_pyyaml_version()
    if found not in PINNED_PYYAML_VERSIONS:
        case.skipTest(f"{SYSTEM_PYTHON} carries PyYAML {found}, not a pinned contract_digest emitter "
                      f"(pinned: {', '.join(sorted(PINNED_PYYAML_VERSIONS))}); the broker load path cannot digest "
                      "component contracts on this interpreter")

HOOK = textwrap.dedent(
    """
    import os, sys
    FORBID = (sys.argv[1].rstrip("/") + "/", "/home/")
    def _hook(event, args):
        if event in ("open", "os.listdir", "os.scandir") and args and isinstance(args[0], (str, bytes, os.PathLike)):
            path = os.path.realpath(os.fsdecode(args[0]))
            if path.startswith(FORBID) or path + "/" in FORBID:
                raise PermissionError(f"closure test: forbidden read {path}")
    sys.addaudithook(_hook)
    COPY = sys.argv[2]
    sys.argv = sys.argv[:1] + sys.argv[3:]
    assert not [p for p in sys.path if p.startswith(FORBID) or p + "/" in FORBID], sys.path
    """
)

LOAD_PATHS = HOOK + textwrap.dedent(
    """
    import hashlib, json
    from pathlib import Path
    sys.path.insert(0, COPY)
    work = Path(sys.argv[1])
    out = {}
    # broker: the T-2918 load path, registry read from the copy
    import tools.host_control.dbus_service
    import tools.host_control.__main__, tools.host_control.approval.__main__  # U2 daemon entrypoints
    from tools.host_control.runtime_table import REPO_ROOT, load_daemon_runtime
    assert str(REPO_ROOT) == COPY, REPO_ROOT
    out["broker_startup_record"] = load_daemon_runtime(host_operation_families={"persona.install", "persona.verify"}).startup_record()
    # approval: modules + a real journal open (SCHEMA_DIGEST check)
    import tools.host_control.approval.dbus_service, tools.host_control.approval.service
    from tools.host_control.approval.journal import ApprovalJournal
    ApprovalJournal.open(work / "approval.sqlite3").close()
    out["approval"] = "ok"
    # executor: entry modules + the framework artifact digest over the copy
    import tools.host_control.executor.__main__, tools.host_control.executor.dbus_service
    from tools.persona_installer_contracts import FRAMEWORK_CODE_ARTIFACTS
    digest = hashlib.sha256()
    for rel in sorted((*FRAMEWORK_CODE_ARTIFACTS, *json.loads(sys.argv[2]))):
        digest.update(rel.encode() + b"\\0" + (Path(COPY) / rel).read_bytes() + b"\\0")
    out["framework_sha256"] = digest.hexdigest()
    # ingress
    import tools.host_control.telegram_ingress.main, tools.host_control.telegram_ingress.config
    out["ingress"] = "ok"
    # executor direct-fire target
    import tools.persona_bringup
    out["persona_bringup"] = "ok"
    # agent_role_map silently falls back to hardcoded maps when instance.yaml
    # is absent, so prove it read the COPY's instance.yaml
    import tools.agent_role_map as arm
    out["instance_yaml_from_copy"] = sorted(k for k in arm._MANIFEST_CACHE if k == COPY + "/instance.yaml")
    out["loaded_outside_copy"] = sorted(
        m.__name__ for m in list(sys.modules.values())
        if (getattr(m, "__file__", None) or "").startswith(FORBID))
    print(json.dumps(out, sort_keys=True))
    """
)

PRODUCER_HOOKED = HOOK + textwrap.dedent(
    """
    import runpy
    sys.argv[0] = COPY + "/tools/host_plan_producer.py"
    runpy.run_path(sys.argv[0], run_name="__main__")
    """
)


class ClosureSelfContainedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory(prefix="t2920-closure-")
        base = Path(cls._temp.name)
        if str(base).startswith("/home/") or str(base).startswith(str(REPO_ROOT)):
            raise unittest.SkipTest("the temp dir must lie outside /home and the repo")
        cls.base = base
        cls.copy = base / "framework" / "rev"
        listing = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "-z"], capture_output=True,
                                 check=True, timeout=120).stdout
        cls.closure = H.derive_closure(REPO_ROOT, tracked=H.parse_tracked_files(listing), template_ids=TEMPLATE_IDS)
        H.materialize_closure(cls.closure, REPO_ROOT, cls.copy)
        cls.work = base / "work"
        cls.work.mkdir()
        cls.scripts = base / "scripts"
        cls.scripts.mkdir()
        (cls.scripts / "load_paths.py").write_text(LOAD_PATHS)
        (cls.scripts / "producer_hooked.py").write_text(PRODUCER_HOOKED)
        cls.manifest_path = base / "instance.yaml"
        cls.manifest_path.write_bytes((REPO_ROOT / "instance.yaml").read_bytes())
        cls.manifest_doc = yaml.safe_load(cls.manifest_path.read_text())
        cls.framework_sha256 = H.framework_artifact_digest(REPO_ROOT, TEMPLATE_IDS)
        cls.manifest_record = {
            "revision": "m1", "path": str(cls.manifest_path),
            "sha256": hashlib.sha256(cls.manifest_path.read_bytes()).hexdigest(),
            "framework_revision": cls.manifest_doc["persona_installer"]["framework"]["revision"],
            "framework_sha256": cls.framework_sha256, "template_ids": TEMPLATE_IDS,
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def run_py(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(["/usr/bin/python3", "-I", "-B", *args], cwd=self.work, env=CLEAN_ENV,
                              capture_output=True, timeout=300, check=False)

    def seats(self) -> list[dict]:
        out = [
            {"seat_id": "fixture-claude", "uid": 2001, "persona": "recovery", "role": "head-system-orchestrator",
             "instance": self.manifest_doc["instance"]["id"], "account": "claude-fallback", "harness": "claude",
             "install_root": "/projection"},
            {"seat_id": "fixture-codex", "uid": 2002, "persona": "codex", "role": "peer-daemon-codex",
             "instance": self.manifest_doc["instance"]["id"], "account": "codex", "harness": "codex",
             "install_root": "/projection"},
        ]
        for index, (role, binding) in enumerate(sorted(self.manifest_doc["roles"].items())):
            out.append({"seat_id": f"real-{binding['persona']}", "uid": 3000 + index, "persona": binding["persona"],
                        "role": role, "instance": self.manifest_doc["instance"]["id"], "account": binding["account"],
                        "harness": "codex" if binding.get("harness") == "codex" else "claude",
                        "install_root": "/projection"})
        return out

    def test_copy_is_outside_home_and_holds_only_the_closure(self) -> None:
        files = sorted(p.relative_to(self.copy).as_posix() for p in self.copy.rglob("*") if p.is_file())
        self.assertEqual(files, list(self.closure.files))
        self.assertFalse(str(self.copy).startswith("/home/"))

    def test_every_daemon_load_path_runs_from_the_copy_alone(self) -> None:
        require_pinned_system_pyyaml(self)
        result = self.run_py([str(self.scripts / "load_paths.py"), str(REPO_ROOT), str(self.copy), str(self.work),
                              json.dumps(TEMPLATE_IDS)])
        self.assertEqual(result.returncode, 0, result.stderr.decode()[-4000:])
        out = json.loads(result.stdout.decode().strip().splitlines()[-1])
        self.assertEqual(out["loaded_outside_copy"], [])
        self.assertEqual(out["framework_sha256"], self.framework_sha256)
        from tools.host_control.runtime_table import load_daemon_runtime

        in_repo = load_daemon_runtime(host_operation_families={"persona.install", "persona.verify"}).startup_record()
        self.assertEqual(out["broker_startup_record"], in_repo)  # gate 32a parity: same registry digest
        self.assertEqual((out["approval"], out["ingress"], out["persona_bringup"]), ("ok", "ok", "ok"))
        self.assertEqual(out["instance_yaml_from_copy"], [str(self.copy) + "/instance.yaml"])

    def producer_args(self, seat: dict, out: Path) -> list[str]:
        return ["--framework-root", str(self.copy), "--seat-record", json.dumps(seat),
                "--manifest-record", json.dumps(self.manifest_record), "--out", str(out)]

    def test_producer_runs_under_the_hook_from_the_copy(self) -> None:
        seat = self.seats()[0]
        out = self.base / "hooked-plan.json"
        result = self.run_py([str(self.scripts / "producer_hooked.py"), str(REPO_ROOT), str(self.copy),
                              *self.producer_args(seat, out)])
        self.assertEqual(result.returncode, 0, result.stderr.decode()[-4000:])
        self.assertTrue(out.is_file())

    def test_exact_registration_command_for_fixture_and_every_real_binding(self) -> None:
        from tools.persona_installer_contracts import InstallPlan

        reads: set[str] = set()
        for seat in self.seats():
            out = self.base / f"plan-{seat['seat_id']}.json"
            # the T-5533 step (a) command, verbatim (setpriv to nobody is U3's)
            result = subprocess.run(
                ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "/usr/bin/python3", "-I",
                 str(self.copy / "tools" / "host_plan_producer.py"), *self.producer_args(seat, out)],
                cwd=self.work, capture_output=True, timeout=300, check=False)
            with self.subTest(seat=seat["seat_id"]):
                self.assertEqual(result.returncode, 0, result.stderr.decode()[-2000:])
                record = json.loads(result.stdout)
                self.assertEqual(record["sha256"], hashlib.sha256(out.read_bytes()).hexdigest())
                plan = InstallPlan.from_dict(json.loads(out.read_bytes()))
                self.assertTrue(plan.validate_digest())
                reads |= self.framework_reads(json.loads(out.read_bytes()))
        files = set(self.closure.files)
        missing = sorted(r for r in reads if r not in files and not any(f.startswith(r + "/") for f in files))
        self.assertEqual(missing, [], "executor plan-driven framework reads outside the closure")
        self.assertTrue(reads)

    def framework_reads(self, plan: dict) -> set[str]:
        """Every framework-root path an operation targets, and every relative
        artifact/bundle entry the executor reads from the framework copy."""

        root = str(self.copy).rstrip("/") + "/"
        out: set[str] = set()
        for op in plan.get("operations", []):
            target = op.get("target")
            if isinstance(target, str) and target.startswith(root):
                out.add(target[len(root):])
            desired = op.get("desired") or {}
            for key in ("artifacts", "bundle"):
                value = desired.get(key)
                if isinstance(value, str):
                    out |= {item.rstrip("/") for item in value.split(",") if item}
            for value in desired.values():
                if isinstance(value, str) and value.startswith(root):
                    out.add(value[len(root):])
        return out


class PinnedPyyamlGuardTest(unittest.TestCase):
    """The guard skips (never widens the pin) and names both versions."""

    def test_unpinned_system_pyyaml_skips_with_both_versions_named(self) -> None:
        from unittest import mock

        with mock.patch(f"{__name__}.system_pyyaml_version", return_value="6.0.2"):
            with self.assertRaises(unittest.SkipTest) as caught:
                require_pinned_system_pyyaml(self)
        message = str(caught.exception)
        self.assertIn("PyYAML 6.0.2", message)
        for version in PINNED_PYYAML_VERSIONS:
            self.assertIn(version, message)
        self.assertNotIn("6.0.2", PINNED_PYYAML_VERSIONS)

    def test_a_pinned_system_pyyaml_does_not_skip(self) -> None:
        from unittest import mock

        with mock.patch(f"{__name__}.system_pyyaml_version", return_value=sorted(PINNED_PYYAML_VERSIONS)[0]):
            require_pinned_system_pyyaml(self)


if __name__ == "__main__":
    unittest.main()
