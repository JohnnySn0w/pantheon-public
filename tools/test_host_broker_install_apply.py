#!/usr/bin/env python3
"""Tests for tools/host_broker_install_apply.py (T-2920 U3a: apply/verify/rollback, DARK).

Every test runs under a tmp FIXTURE root with FixtureCommandRunner (a
FakeCommandRunner that records the live argv and emulates a fixed binary table
inside the fixture). Nothing here can reach the real /usr, /etc, /var or /run.

The plan is U1's real plan (the example manifest plus a two-seat registry). The
real repo's machine MCP authority carries the pantheon-host and pantheon-approval
rows (T-2920 U5; both with an EMPTY persona set until the enable-time membership
commit), so the real plan has an empty blocking_preconditions (pinned below). The
"rows absent" refusal is exercised by patching U1's authority read to DROP the two
rows. _literal_allowlist_landed is kept (now a no-op on the real repo) because the
U4a harness tests import it.

The real-path proof of this code is U4's nspawn rehearsal (D5 real-path bar).
"""

from __future__ import annotations

import ast
import copy
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import host_broker_install as H  # noqa: E402
from tools import host_broker_install_apply as A  # noqa: E402
from tools import test_host_broker_install as T  # noqa: E402
from tools._organ_lifecycle import CommandResult  # noqa: E402
from tools.germinate.host_substrate import families as F  # noqa: E402
from tools.germinate.host_substrate import ports as P  # noqa: E402
from tools.germinate.host_substrate.helper import ACTIVATE  # noqa: E402
from tools.persona_installer_contracts import InstallPlan  # noqa: E402

RECEIPT = "/var/lib/pantheon-install-receipts/r1.json"
TOKEN = b"0000000000:FIXTURE-TOKEN-BYTES-NEVER-IN-A-RECEIPT"
MANIFEST_M1 = b"# fixture registered manifest m1 (bytes only; never parsed here)\n"
HOST_POLICY = {"schema": "pantheon-host-policy/v1", "revision": "host-policy-r1", "grants": []}
PYYAML_OK = sorted(H.PINNED_PYYAML_VERSIONS)[0]
FW_ACTIVE = "/usr/local/lib/pantheon/framework/active"
BASE_DIRS = ("etc", "etc/systemd/system", "etc/dbus-1/system.d", "usr/share/dbus-1/system-services",
             "usr/local/lib", "usr/local/libexec", "var/lib", "run", "home/post-office/mailbox",
             "var/lib/pantheon-install-receipts", "srv/inputs", "srv/inputs/wheelhouse")
WHEEL = b"fixture wheel bytes (the real wheelhouse is lock-pinned; see _Base)\n"
WHEEL_SHA = hashlib.sha256(WHEEL).hexdigest()
_REAL_PINNED = A.lock_pinned_hashes


def _pinned_plus_fixture_wheel(text):
    return _REAL_PINNED(text) | {WHEEL_SHA}

_REAL_LITERAL = H._literal
_REAL_MCP_ALLOWLIST = H._mcp_allowlist


def _literal_allowlist_landed(repo_root, rel, name):
    value = _REAL_LITERAL(repo_root, rel, name)
    if name == "_CLAUDE_MCP_ALLOWLIST":
        value = {**value, "pantheon-host": None, "pantheon-approval": None}
    return value


def _literal_allowlist_absent(repo_root, rel, name):
    value = _REAL_LITERAL(repo_root, rel, name)
    if name == "_CLAUDE_MCP_ALLOWLIST":
        value = {k: v for k, v in value.items() if k not in ("pantheon-host", "pantheon-approval")}
    return value


def _machine_allowlist_absent(repo_root):
    return {
        key: value
        for key, value in _REAL_MCP_ALLOWLIST(repo_root).items()
        if key not in ("pantheon-host", "pantheon-approval")
    }


def registry_doc() -> dict:
    doc = T.registry_doc()
    doc["manifests"][0]["sha256"] = hashlib.sha256(MANIFEST_M1).hexdigest()
    return doc


def manifest_doc() -> dict:
    return T.with_protocol_test_seats(T.with_approver(T.manifest_doc()))


def build_plan(manifest: dict | None = None, registry: dict | None = None, closure: H.Closure | None = None):
    m = H.parse_manifest(T.raw(manifest or manifest_doc()))
    r = H.parse_registry(T.raw(registry or registry_doc()))
    return H.build_plan(m, r, H.REPO_ROOT, closure=closure or T.closure())


def approver_policy() -> dict:
    from tools.test_host_control_approval_policy import base_policy

    return base_policy()


def inputs_doc() -> dict:
    return {
        "schema": A.INPUTS_SCHEMA,
        "operator_batch": {
            "1": {"token_source_path": "/srv/inputs/bot-token", "operator_uid": 1002},
            "2": {"telegram_user_id": 424242, "human_subject": "operator"},
            "3": {"approver_policy_source": "/srv/inputs/approver-policy.json",
                  "broker_policy_source": "/srv/inputs/host-policy.json"},
        },
        "broker": {"host_operation_families": ["persona.install", "persona.verify"], "plan_ttl_seconds": 3600},
        "registered_manifests": {"m1": "/srv/inputs/m1.yaml"},
        "wheelhouse": "/srv/inputs/wheelhouse",
    }


def make_root(tmp: Path) -> Path:
    root = tmp / "root"
    root.mkdir()
    for rel in BASE_DIRS:
        (root / rel).mkdir(parents=True, exist_ok=True)
    for d in [root, *(p for p in root.rglob("*") if p.is_dir())]:
        os.chmod(d, 0o755)  # base OS dirs are 0755 whatever the test umask is
    (root / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/bash\n"
                                     "claude-fallback:x:1002:1002::/home/claude-fallback:/bin/bash\n"
                                     "codex-fallback:x:1011:1011::/home/codex-fallback:/bin/bash\n"
                                     "fixture-recovery:x:2001:2001::/home/fixture-recovery:/bin/bash\n"
                                     "fixture-watchman:x:2002:2002::/home/fixture-watchman:/bin/bash\n")
    (root / "etc/group").write_text("root:x:0:\nclaude-fallback:x:1002:\ncodex-fallback:x:1011:\nfixture-recovery:x:2001:\nfixture-watchman:x:2002:\n")
    (root / "etc/shadow").write_text("root:*:20000:0:99999:7:::\n")
    (root / "srv/inputs/bot-token").write_bytes(TOKEN)
    os.chmod(root / "srv/inputs/bot-token", 0o600)
    (root / "srv/inputs/wheelhouse/fixture-0.0-py3-none-any.whl").write_bytes(WHEEL)
    (root / "srv/inputs/approver-policy.json").write_text(json.dumps(approver_policy()))
    (root / "srv/inputs/host-policy.json").write_text(json.dumps(HOST_POLICY))
    (root / "srv/inputs/m1.yaml").write_bytes(MANIFEST_M1)
    meta = root / A.FIXTURE_META
    meta.mkdir()
    (meta / "facts.json").write_text(json.dumps({
        "dpkg": {"python3": "3.12.3-0", "python3-dbus": "1.3.2-5", "python3-gi": "3.48.2-1", "python3-yaml": PYYAML_OK},
        "pyyaml": PYYAML_OK}))
    return root


# --- test emulation of the two argv the fixed table does not cover -----------


def _is_producer(argv) -> bool:
    return argv[0] == H.SETPRIV


def _producer(argv, host: A.FixtureHost) -> CommandResult:
    seat = json.loads(argv[argv.index("--seat-record") + 1])
    manifest = json.loads(argv[argv.index("--manifest-record") + 1])
    out = argv[argv.index("--out") + 1]
    plan = InstallPlan.create(schema="fixture-plan", manifest_version=1, instance=seat["instance"], role=seat["role"],
                              persona=seat["persona"], account=seat["account"], configured_harness=seat["harness"],
                              selected_harness=seat["harness"], root=seat["install_root"], operations=(),
                              operator_actions=(), provenance={"declared_revision": manifest["framework_revision"]})
    data = (H.canonical(plan.to_dict())).encode("utf-8")
    host.write_file(out, data, "nobody", "nogroup", "0644")
    record = {"seat_id": seat["seat_id"], "manifest_revision": manifest["revision"], "path": out,
              "sha256": hashlib.sha256(data).hexdigest()}
    return CommandResult(0, H.canonical(record) + "\n", "")


def _is_build(argv) -> bool:
    return any(a.endswith(H.BUILD_RUNTIME_REL) for a in argv)


def _build(argv, host: A.FixtureHost) -> CommandResult:
    key = argv[argv.index("--adapter") + 1]
    spec = T.manifest_doc()["runtimes"][key]
    prefix, launcher = spec["prefix"], spec["launcher"]
    if host.exists(prefix) or host.exists(launcher):
        return CommandResult(1, "", "refusing to build over an existing artifact")
    for d in (prefix, prefix + "/venv", prefix + "/venv/bin", prefix + "/app", prefix + "/app/tools"):
        host.mkdir(d, "root", "root", "0755")
    host.write_file(prefix + "/venv/bin/python3", b"#!fixture interpreter\n", "root", "root", "0755")
    host.write_file(prefix + "/app/tools/__init__.py", b"", "root", "root", "0644")
    host.write_file(launcher, f"#!/{prefix}/venv/bin/python3 -I\n# fixture {key} launcher\n".encode(), "root", "root",
                    "0755")
    lock = H.REPO_ROOT / "tools/host_mcp_adapter/dist/requirements.lock"
    doc = {"schema": "fixture", "requirements_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
           "files": A.runtime_entries(host, prefix, launcher)}
    host.write_file(prefix + "/BUILD-MANIFEST.json", json.dumps(doc).encode(), "root", "root", "0644")
    return CommandResult(0, "", "")


def runner_factory(scripted=None, holder=None):
    def make(host: A.FixtureHost) -> A.FixtureCommandRunner:
        runner = A.FixtureCommandRunner(host, scripted=scripted, handlers=[(_is_producer, _producer), (_is_build, _build)])
        if holder is not None:
            holder["runner"] = runner
            holder["host"] = host
        return runner
    return make


def snapshot(root: Path) -> dict:
    """path -> (type, sha, mode, owner, group, link) under the fixture root, sidecar applied."""

    owners = {}
    f = root / A.FIXTURE_META / "owners.json"
    if f.is_file():
        owners = json.loads(f.read_text())
    enabled = (root / A.FIXTURE_META / "enabled.json")
    out = {"<enabled>": json.loads(enabled.read_text()) if enabled.is_file() else []}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir.parts[:1] == (A.FIXTURE_META,):
            continue
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            host = "/" + p.relative_to(root).as_posix()
            if host.startswith("/" + A.FIXTURE_META) or host.startswith(RECEIPT):
                continue
            st = os.lstat(p)
            owner = tuple(owners.get(host, ["root", "root"]))
            if p.is_symlink():
                out[host] = ("link", os.readlink(p), owner)
            elif p.is_dir():
                out[host] = ("dir", oct(st.st_mode & 0o7777), owner)
            else:
                out[host] = ("file", hashlib.sha256(p.read_bytes()).hexdigest(), oct(st.st_mode & 0o7777), owner)
    return out


def without(snap: dict, prefixes) -> dict:
    prefixes = tuple(prefixes)
    return {k: v for k, v in snap.items() if not any(k == p or k.startswith(p.rstrip("/") + "/") for p in prefixes)}


class _Base(unittest.TestCase):
    """The plan as if the allowlist rows landed (see the module docstring)."""

    patcher = None
    PLAN = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.patcher = mock.patch.object(H, "_literal", _literal_allowlist_landed)
        cls.patcher.start()
        # the fixture wheelhouse holds one stand-in wheel; real wheels are pinned by the lock
        cls.wheel_patcher = mock.patch.object(A, "lock_pinned_hashes", _pinned_plus_fixture_wheel)
        cls.wheel_patcher.start()
        if _Base.PLAN is None:
            _Base.PLAN = build_plan()
        cls.plan, cls.staging = _Base.PLAN

    @classmethod
    def tearDownClass(cls) -> None:
        cls.patcher.stop()
        cls.wheel_patcher.stop()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = make_root(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def plan_bytes(self, plan=None) -> bytes:
        return H.plan_bytes(plan or self.plan)

    def apply(self, *, execute=True, receipt=RECEIPT, plan=None, manifest=None, registry=None, inputs=None,
              scripted=None, closure=None, hook=None, holder=None):
        holder = {} if holder is None else holder

        def host_hook(host):
            holder["host"] = host
            if hook:
                hook(host)

        outcome = A.apply(plan_raw=self.plan_bytes(plan), manifest_raw=T.raw(manifest or manifest_doc()),
                          registry_raw=T.raw(registry or registry_doc()), tracked_raw=None,
                          inputs_raw=json.dumps(inputs or inputs_doc()).encode(), root=self.root, receipt=receipt,
                          execute=execute, runner_factory=runner_factory(scripted, holder),
                          closure=closure or T.closure(), now=lambda: "2026-10-27T12:00:00Z", host_hook=host_hook)
        return outcome, holder

    def verify(self, plan=None, **kw):
        return A.verify(plan_raw=self.plan_bytes(plan), root=self.root, runner_factory=runner_factory(), **kw)

    def rollback(self, receipt=RECEIPT, holder=None, stamp="2026-10-27T13:00:00Z"):
        return A.rollback(receipt=receipt, root=self.root, runner_factory=runner_factory(None, holder),
                          now=lambda: stamp)

    def by_id(self, receipt: dict) -> dict:
        return {r["id"]: r for r in receipt["entries"]}


# ---------------------------------------------------------------------------
# 1. dark pins
# ---------------------------------------------------------------------------


class DarkPinTest(unittest.TestCase):
    SOURCE = Path(A.__file__).read_text(encoding="utf-8")

    def test_no_subprocess_no_shutil_no_network_and_no_real_runner(self) -> None:
        tree = ast.parse(self.SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & {"subprocess", "socket", "urllib", "http", "shutil", "pty", "multiprocessing"})
        self.assertNotIn("SubprocessCommandRunner", self.SOURCE)
        self.assertNotIn("os.system", self.SOURCE)
        self.assertNotIn("os.exec", self.SOURCE)
        self.assertNotIn("HostSubstratePort", self.SOURCE)

    def test_the_one_dark_switch_is_u1s_literal_and_this_module_has_none(self) -> None:
        tree = ast.parse(self.SOURCE)
        writes = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                  and any(isinstance(t, ast.Name) and t.id == "LIVE_ACTUATION_ENABLED"
                          for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
        self.assertEqual(writes, [])
        self.assertFalse([n for n in ast.walk(tree) if isinstance(n, ast.Global)])
        self.assertIs(H.LIVE_ACTUATION_ENABLED, False)

    def test_fixture_root_refusals(self) -> None:
        for bad in (None, "", "/", "/tmp/..", "/tmp/../"):
            with self.subTest(root=bad), self.assertRaises(A.ApplyRefused):
                A.FixtureHost(bad)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            os.symlink("/etc", root / "etc")  # a symlink component may never leave the fixture
            host = A.FixtureHost(root)
            with self.assertRaisesRegex(A.ApplyRefused, "symlink component"):
                host.path("/etc/pantheon/x")
            with self.assertRaises(A.ApplyRefused):
                host.path("/etc/../x")
            link = Path(tmp) / "link"
            os.symlink(root, link)
            with self.assertRaises(A.ApplyRefused):
                A.FixtureHost(link)

    def test_unknown_binary_fails_instead_of_defaulting_to_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = A.FixtureCommandRunner(A.FixtureHost(tmp))
            for argv in (["/usr/bin/curl", "https://example.invalid"], ["/bin/sh", "-c", "true"],
                         [P.SYSTEMCTL, "start", "x.service"], [H.SETPRIV, "--reuid=nobody"]):
                self.assertEqual(runner.run(argv).returncode, 127, argv)
            self.assertEqual(len(runner.calls), 4)
            self.assertNotEqual(runner.default.returncode, 0)

    def test_execute_without_root_is_refused_even_if_the_switch_were_flipped(self) -> None:
        for flipped in (False, True):
            with self.subTest(live=flipped), mock.patch.object(H, "LIVE_ACTUATION_ENABLED", flipped):
                with self.assertRaisesRegex(A.ApplyRefused, "no live mode"):
                    A.apply(plan_raw=b"{}", manifest_raw=b"", registry_raw=b"", tracked_raw=b"", inputs_raw=b"",
                            root=None, receipt=RECEIPT, execute=True)
                err = io.StringIO()
                with mock.patch("sys.stderr", err):
                    rc = A.main(["apply", "--plan", "p", "--manifest", "m", "--registry", "r", "--tracked-files", "t",
                                 "--inputs", "i", "--receipt", RECEIPT, "--execute"])
                self.assertEqual(rc, 2)
                self.assertIn("no live mode", err.getvalue())

    def test_inputs_schema_is_closed_and_errors_never_echo_values(self) -> None:
        A.parse_inputs(json.dumps(inputs_doc()).encode())
        doc = inputs_doc()
        doc["operator_batch"]["2"]["telegram_user_id"] = "987654321"
        with self.assertRaises(A.ApplyRefused) as cm:
            A.parse_inputs(json.dumps(doc).encode())
        self.assertNotIn("987654321", str(cm.exception))
        doc = inputs_doc()
        doc["operator_batch"]["1"]["token"] = "secret"
        with self.assertRaises(A.ApplyRefused) as cm:
            A.parse_inputs(json.dumps(doc).encode())
        self.assertNotIn("secret", str(cm.exception))
        doc = inputs_doc()
        doc["extra"] = 1
        with self.assertRaises(A.ApplyRefused):
            A.parse_inputs(json.dumps(doc).encode())


# ---------------------------------------------------------------------------
# 2. refusals before the first action
# ---------------------------------------------------------------------------


class RealPlanRefusalTest(unittest.TestCase):
    """The REAL plan (no patch) has no blocking precondition now the allowlist rows
    landed (T-2920 U5); with the two rows dropped, apply refuses before the first action."""

    def test_real_plan_has_no_blocking_precondition(self) -> None:
        plan, _ = build_plan()
        self.assertEqual(plan["blocking_preconditions"], [])
        entry = next(e for e in plan["entries"] if e["id"] == "precondition:mcp-allowlist")
        self.assertEqual(entry["inputs"]["observed"], "met")

    def test_blocking_precondition_refuses_before_the_first_action(self) -> None:
        with mock.patch.object(H, "_mcp_allowlist", _machine_allowlist_absent), tempfile.TemporaryDirectory() as tmp:
            plan, _ = build_plan()
            self.assertEqual(plan["blocking_preconditions"], ["precondition:mcp-allowlist"])
            root = make_root(Path(tmp))
            before = snapshot(root)
            holder = {}
            with self.assertRaisesRegex(A.ApplyRefused, "blocking_preconditions"):
                A.apply(plan_raw=H.plan_bytes(plan), manifest_raw=T.raw(manifest_doc()),
                        registry_raw=T.raw(registry_doc()), tracked_raw=None,
                        inputs_raw=json.dumps(inputs_doc()).encode(), root=root, receipt=RECEIPT, execute=True,
                        runner_factory=runner_factory(None, holder), closure=T.closure())
            self.assertEqual(snapshot(root), before)
            self.assertEqual(holder["host"].mutations, [])
            self.assertEqual(holder["runner"].calls, [])


class RefusalTest(_Base):
    def assert_refused(self, message: str, **kw) -> None:
        before = snapshot(self.root)
        holder = {}
        with self.assertRaisesRegex(A.ApplyRefused, message):
            self.apply(holder=holder, **kw)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(self.root.joinpath(RECEIPT.lstrip("/")).exists())
        if "host" in holder:
            self.assertEqual(holder["host"].mutations, [])
        for call in holder.get("runner").calls if "runner" in holder else []:
            self.assertIn(call[0], (A.DPKG_QUERY, H.PYTHON3))  # read-only probes only

    def test_plan_bytes_tampered(self) -> None:
        plan = copy.deepcopy(self.plan)
        plan["entries"][0]["gate"] = ["E1"]
        self.assert_refused("plan_sha256 mismatch", plan=plan)

    def test_inputs_no_longer_produce_the_plan(self) -> None:
        registry = registry_doc()
        registry["seats"][0]["uid"] = 2003  # a still-valid registry that no longer renders the plan's units
        self.assert_refused("plan_sha256 mismatch", registry=registry)

    def test_stale_registry_framework_digest(self) -> None:
        registry = registry_doc()
        registry["manifests"][0]["framework_sha256"] = "0" * 64
        self.assert_refused("framework_sha256", registry=registry)

    def test_framework_digest_against_the_closure_bytes(self) -> None:
        holder = {}
        host = A.FixtureHost(self.root)
        contents: dict = {}
        H.build_plan(H.parse_manifest(T.raw(manifest_doc())), H.parse_registry(T.raw(registry_doc())), H.REPO_ROOT,
                     closure=T.closure(), contents=contents)
        rel = "tools/persona_installer.py"
        fid = f"framework-file:{rel}"
        contents[fid] = contents[fid] + b"# drift\n"
        ctx = A.Ctx(host, A.FixtureCommandRunner(host), self.plan, F.load_manifest(A.fragment_from_plan(self.plan)),
                    contents=contents)
        with self.assertRaisesRegex(A.ApplyRefused, "framework_sha256 differs from the closure"):
            A.preflight_refusals(ctx, None, H.parse_registry(T.raw(registry_doc())))
        del holder

    def test_monitor_uids_other_than_hostmonitor(self) -> None:
        plan = copy.deepcopy(self.plan)
        for e in plan["entries"]:
            kv = (e.get("inputs") or {}).get("known_values") or {}
            if "monitor_uids" in kv:
                kv["monitor_uids"] = [1904, 1002]
        host = A.FixtureHost(self.root)
        contents: dict = {}
        H.build_plan(H.parse_manifest(T.raw(manifest_doc())), H.parse_registry(T.raw(registry_doc())), H.REPO_ROOT,
                     closure=T.closure(), contents=contents)
        ctx = A.Ctx(host, A.FixtureCommandRunner(host), plan, F.load_manifest(A.fragment_from_plan(self.plan)),
                    contents=contents)
        with self.assertRaisesRegex(A.ApplyRefused, "monitor_uids must be exactly the hostmonitor uid"):
            A.preflight_refusals(ctx, None, H.parse_registry(T.raw(registry_doc())))
        self.assertEqual(host.mutations, [])

    def test_uid_collision_in_the_fixture_passwd(self) -> None:
        with (self.root / "etc/passwd").open("a") as f:
            f.write("intruder:x:1901:1901::/home/intruder:/bin/sh\n")
        self.assert_refused("collides with")

    def test_account_name_with_another_uid(self) -> None:
        with (self.root / "etc/passwd").open("a") as f:
            f.write("pantheon-hostbroker:x:2901:2901::/x:/usr/sbin/nologin\n")
        self.assert_refused("exists with uid 2901")

    def test_unaccepted_system_pyyaml(self) -> None:
        facts = self.root / A.FIXTURE_META / "facts.json"
        doc = json.loads(facts.read_text())
        doc["pyyaml"] = "5.4.1"
        facts.write_text(json.dumps(doc))
        self.assert_refused("not in accepted_versions")

    def test_missing_dpkg_package(self) -> None:
        facts = self.root / A.FIXTURE_META / "facts.json"
        doc = json.loads(facts.read_text())
        del doc["dpkg"]["python3-gi"]
        facts.write_text(json.dumps(doc))
        self.assert_refused("python3-gi: not installed")

    def test_receipt_inside_the_framework_tree_or_a_state_dir(self) -> None:
        self.assert_refused("framework tree or a StateDirectory", receipt="/usr/local/lib/pantheon/framework/r.json")
        self.assert_refused("StateDirectory", receipt="/var/lib/pantheon-host-broker/r.json")
        self.assert_refused("StateDirectory", receipt="/var/lib/pantheon-host-approval-telegram/x/r.json")


# ---------------------------------------------------------------------------
# 3-8. full apply, family argv, verify, idempotent rerun, partial failure, upgrade
# ---------------------------------------------------------------------------


class ApplyTest(_Base):
    def test_full_apply_converges_every_entry_and_verify_is_green(self) -> None:
        pre = snapshot(self.root)
        outcome, holder = self.apply()
        rc = outcome.receipt
        self.assertEqual(outcome.rc, 0, rc["failed_entry"])
        self.assertEqual(rc["status"], "complete")
        self.assertTrue(all(r["action"] in ("created", "replaced", "converged", "observed") for r in rc["entries"]))
        # the framework is written whole, then ONE os.replace switches 'active' (Q6)
        rev = self.plan["framework"]["rev"]
        self.assertEqual(os.readlink(self.root / FW_ACTIVE.lstrip("/")), rev)
        mutations = holder["host"].mutations
        self.assertEqual([m for m in mutations if m[0] == "replace"], [("replace", FW_ACTIVE)])
        self.assertIn(("rename", f"/usr/local/lib/pantheon/framework/.{rev}.partial"), mutations)
        # receipt: canonical, signed, O_EXCL 0600 in a 0700 RC.d
        raw = (self.root / RECEIPT.lstrip("/")).read_bytes()
        self.assertEqual(json.loads(raw), rc)
        self.assertEqual(rc["receipt_sha256"], A.receipt_digest(rc))
        self.assertEqual(oct((self.root / RECEIPT.lstrip("/")).stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((self.root / (RECEIPT.lstrip("/") + ".d")).stat().st_mode & 0o777), "0o700")
        self.assertEqual(rc["packages"]["python3-yaml"], PYYAML_OK)
        self.assertEqual(rc["pyyaml_version"], PYYAML_OK)
        accounts = [r for r in rc["residuals"] if r["id"].startswith("account-created:")]
        self.assertEqual(len(accounts), 4)
        rc2, report = self.verify()
        self.assertEqual(rc2, 0, [r for r in report if not r["ok"]])
        rc3, report = self.verify(receipt_raw=raw)
        self.assertEqual(rc3, 0, [r for r in report if not r["ok"]])
        self.assertNotEqual(snapshot(self.root), pre)

    def test_rendered_configs_load_through_u2s_own_loaders(self) -> None:
        outcome, _ = self.apply()
        self.assertEqual(outcome.rc, 0, outcome.receipt["failed_entry"])
        from tools.host_control.approval.config import load_approval_config
        from tools.host_control.broker_config import load_broker_config
        from tools.host_control.policy import HostPolicy
        from tools.host_control.telegram_ingress.config import load_ingress_config
        from tools.host_control.telegram_ingress.main import credential_ref
        from tools.host_control.trusted_files import RootOwnedFileLoader

        r = self.root
        with RootOwnedFileLoader._open_for_test((r / "etc/pantheon/host-broker",), expected_owner_uid=os.getuid()) as files:
            broker = load_broker_config(r / "etc/pantheon/host-broker/config.json", files=files)
            HostPolicy.load(r / "etc/pantheon/host-broker/host-policy.json", files=files)
        self.assertIs(broker.compensate, False)  # Q4
        self.assertEqual(broker.monitor_uids, frozenset({1904}))  # LOW-3
        self.assertEqual(str(broker.framework_root), self.plan["framework"]["root"])
        self.assertEqual(str(broker.policy_path), "/etc/pantheon/host-broker/host-policy.json")  # Q-B
        self.assertEqual(broker.broker_revision, self.plan["framework"]["rev"])
        self.assertEqual(broker.host_operation_families, frozenset({"persona.install", "persona.verify"}))
        with RootOwnedFileLoader._open_for_test((r / "etc/pantheon/host-approval",), expected_owner_uid=os.getuid()) as files:
            approval = load_approval_config(r / "etc/pantheon/host-approval/config.json", files=files)
        self.assertEqual(approval.monitor_uids, frozenset({1904}))
        ingress = load_ingress_config(r / "etc/pantheon/host-approval-telegram/ingress.json",
                                      expected_owner_uid=os.getuid())
        self.assertEqual((ingress.allow_user_id, ingress.allow_chat_id), (424242, 424242))
        ref = credential_ref(ingress)
        self.assertEqual(str(ref.location), "/etc/pantheon/host-approval-telegram/credentials/bot-token")
        exec_cfg = json.loads((r / "etc/pantheon/host-executor/config.json").read_bytes())
        self.assertEqual(exec_cfg["framework_root"], self.plan["framework"]["root"])

    def test_launchers_name_the_concrete_rev_and_run_the_u2_modules(self) -> None:
        outcome, _ = self.apply()
        self.assertEqual(outcome.rc, 0)
        fw = self.plan["framework"]["root"]
        text = (self.root / "usr/local/libexec/pantheon-host-broker").read_text()
        self.assertTrue(text.startswith(f"#!{H.PYTHON3} -I\n"))
        self.assertIn(f"FRAMEWORK_ROOT = \"{fw}\"", text)
        self.assertIn('runpy.run_module("tools.host_control", run_name="__main__"', text)
        self.assertNotIn("/active", text)
        self.assertEqual(oct((self.root / "usr/local/libexec/pantheon-host-broker").stat().st_mode & 0o777), "0o755")
        ingress = (self.root / "usr/local/libexec/pantheon-host-approval-telegram-ingress").read_text()
        self.assertIn('"--config", "/etc/pantheon/host-approval-telegram/ingress.json"', ingress)

    def test_plan_registration_runs_the_plans_argv_and_the_root_checks(self) -> None:
        outcome, holder = self.apply()
        self.assertEqual(outcome.rc, 0)
        argvs = [list(e["source"]["producer_argv"]) for e in self.plan["entries"] if e["kind"] == "plan_registration"]
        calls = [list(c) for c in holder["runner"].calls if c[0] == H.SETPRIV]
        self.assertEqual(calls, argvs)  # exactly the reviewed argv, in plan order
        registry = json.loads((self.root / "etc/pantheon/host-broker/seat-registry.json").read_bytes())
        self.assertEqual([p["seat_id"] for p in registry["plans"]], ["watchman"])
        for p in registry["plans"]:
            data = (self.root / p["path"].lstrip("/")).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), p["sha256"])
            self.assertTrue(InstallPlan.from_dict(json.loads(data)).validate_digest())
        self.assertFalse((self.root / "run/pantheon-host-install/staging").exists())  # removed after registration
        regs = [r for r in outcome.receipt["entries"] if r["kind"] == "plan_registration"]
        self.assertTrue(all(r["observations"]["producer_root_checks"] == len(H.PRODUCER_ROOT_CHECKS) for r in regs))

    def test_producer_output_owned_by_the_wrong_user_is_refused(self) -> None:
        def bad(argv, host):
            res = _producer(argv, host)
            host.chown(argv[argv.index("--out") + 1], "root", "root")
            return res

        holder = {}
        orig = runner_factory(None, holder)

        def factory(host):
            runner = orig(host)
            runner.handlers.insert(0, (_is_producer, bad))
            return runner

        outcome = A.apply(plan_raw=self.plan_bytes(), manifest_raw=T.raw(manifest_doc()),
                          registry_raw=T.raw(registry_doc()), tracked_raw=None,
                          inputs_raw=json.dumps(inputs_doc()).encode(), root=self.root, receipt=RECEIPT, execute=True,
                          runner_factory=factory, closure=T.closure(), now=lambda: "2026-10-27T12:00:00Z")
        self.assertEqual(outcome.rc, 1)
        self.assertIn("not owned by the producer user", outcome.receipt["failed_entry"]["error"])

    def test_token_bytes_never_reach_the_receipt_rcd_or_stdout(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            outcome, holder = self.apply()
        self.assertEqual(outcome.rc, 0)
        token_file = self.root / "etc/pantheon/host-approval-telegram/credentials/bot-token"
        self.assertEqual(token_file.read_bytes(), TOKEN)
        self.assertEqual(oct(token_file.stat().st_mode & 0o777), "0o600")
        self.assertEqual(holder["host"].owner_of("/etc/pantheon/host-approval-telegram/credentials"),
                         ("pantheon-hostapproval-ingress", "pantheon-hostapproval-ingress"))
        token_sha = hashlib.sha256(TOKEN).hexdigest().encode()
        rcd = self.root / (RECEIPT.lstrip("/") + ".d")
        for p in [self.root / RECEIPT.lstrip("/"), *[q for q in rcd.rglob("*") if q.is_file()]]:
            data = p.read_bytes()
            self.assertNotIn(TOKEN, data, p)
            self.assertNotIn(token_sha, data, p)
        self.assertNotIn(TOKEN.decode(), out.getvalue())
        cred = self.by_id(outcome.receipt)["credential:/etc/pantheon/host-approval-telegram/credentials/bot-token"]
        self.assertIsNone(cred["after_sha256"])
        self.assertEqual(cred["argv"], [])  # Recovery D15: created in-process, no install argv
        self.assertEqual(cred["action"], "created")
        self.assertFalse([c for c in holder["runner"].calls if "/srv/inputs/bot-token" in c])

    def test_family_entries_run_exactly_ports_apply_argv_via_roothelper(self) -> None:
        outcome, holder = self.apply()
        self.assertEqual(outcome.rc, 0)
        rcd = RECEIPT + ".d"
        expected: list[tuple] = []
        for r in outcome.receipt["entries"]:
            fam = r["family"]
            if not fam or r["action"] not in ("created", "replaced"):
                continue
            staged = None
            if fam["family"] in ("unit.system.install", "dbus_policy.install"):
                entry = next(e for e in self.plan["entries"] if e["id"] == r["id"])
                staged = f"{rcd}/substrate-staging/{entry['source']['staging']}"
            argvs = P.apply_argv(F.Resolved(fam["family"], fam["subject"], fam["resolved_after"], fam["entry"]),
                                 staged_source=staged)
            self.assertEqual(r["argv"], argvs)
            expected += [tuple(a) for a in argvs]
        family_bins = {P.GROUPADD, P.USERADD, P.CHOWN, P.CHMOD, P.UNLINK, P.SYSTEMCTL}
        calls = [c for c in holder["runner"].calls
                 if c[0] in family_bins or (c[0] == P.INSTALL and "substrate-staging" in "".join(c))]
        self.assertEqual(calls, expected)
        kinds = {r["family"]["family"] for r in outcome.receipt["entries"] if r["family"] and r["action"] == "created"}
        self.assertEqual(kinds, {"account.ensure", "unit.system.install", "dbus_policy.install", "unit.system.enable"})
        self.assertIn((P.USERADD, "--uid", "1904", "--gid", "1904", "--home-dir", "/var/lib/pantheon/home/pantheon-hostmonitor",
                       "--create-home", "--shell", "/usr/sbin/nologin", "pantheon-hostmonitor"), calls)

    def test_owner_mode_family_converges_a_pre_existing_path(self) -> None:
        holder_host = {}
        (self.root / "etc/pantheon").mkdir(mode=0o700)
        os.chmod(self.root / "etc/pantheon", 0o700)
        outcome, holder = self.apply(holder=holder_host)
        self.assertEqual(outcome.rc, 0, outcome.receipt["failed_entry"])
        r = self.by_id(outcome.receipt)["owner-mode:/etc/pantheon"]
        self.assertEqual(r["action"], "replaced")
        self.assertEqual(r["argv"], [[P.CHOWN, "-h", "root:root", "/etc/pantheon"], [P.CHMOD, "00755", "/etc/pantheon"]])
        self.assertEqual(oct((self.root / "etc/pantheon").stat().st_mode & 0o777), "0o755")

    def test_verify_reports_drift_per_entry(self) -> None:
        outcome, _ = self.apply()
        self.assertEqual(outcome.rc, 0)
        r = self.root
        rev = self.plan["framework"]["rev"]
        fw = f"usr/local/lib/pantheon/framework/{rev}"
        os.chmod(r / fw / "tools/persona_installer.py", 0o666)
        owners = json.loads((r / A.FIXTURE_META / "owners.json").read_text())
        owners["/etc/pantheon/host-broker"] = ["pantheon-hostbroker", "root"]
        (r / A.FIXTURE_META / "owners.json").write_text(json.dumps(owners))
        os.unlink(r / FW_ACTIVE.lstrip("/"))
        os.symlink("deadbeefdeadbeef", r / FW_ACTIVE.lstrip("/"))
        (r / "etc/systemd/system/pantheon-host-broker.service").write_text("[Service]\nExecStart=/bin/sh\n")
        shadow = (r / "etc/shadow").read_text().replace("pantheon-hostmonitor:!:", "pantheon-hostmonitor:$6$salt$hash:")
        (r / "etc/shadow").write_text(shadow)
        rc, report = self.verify()
        self.assertEqual(rc, 1)
        bad = {row["id"] for row in report if not row["ok"]}
        self.assertEqual(bad, {
            "framework-file:tools/persona_installer.py", f"framework-active:{FW_ACTIVE}",
            "owner-mode:/etc/pantheon/host-broker", "dir:/etc/pantheon/host-broker",  # both re-observe the owner
            "unit:pantheon-host-broker.service", "account:pantheon-hostmonitor"})
        row = next(x for x in report if x["id"] == "account:pantheon-hostmonitor")
        self.assertIn("shadow password field is not locked", row["drift"])

    def test_journal_roots_are_pending_start_unless_expect_started(self) -> None:
        outcome, _ = self.apply()
        self.assertEqual(outcome.rc, 0)
        rc, report = self.verify()
        self.assertEqual(rc, 0)
        self.assertEqual({row["state"] for row in report if row["kind"] == "journal_root"}, {"pending-start"})
        rc, report = self.verify(expect_started=True)
        self.assertEqual(rc, 1)
        self.assertEqual({row["id"] for row in report if not row["ok"]},
                         {e["id"] for e in self.plan["entries"] if e["kind"] == "journal_root"})

    def test_idempotent_rerun_is_a_noop_with_zero_mutations(self) -> None:
        first, _ = self.apply()
        self.assertEqual(first.rc, 0)
        before = snapshot(self.root)
        second, holder = self.apply(receipt="/var/lib/pantheon-install-receipts/r2.json")
        self.assertEqual(second.rc, 0, second.receipt["failed_entry"])
        self.assertEqual(second.receipt["status"], "noop")
        self.assertTrue(second.receipt["noop"])
        self.assertEqual(holder["host"].mutations, [])
        self.assertTrue(all(c[0] in (A.DPKG_QUERY, H.PYTHON3) for c in holder["runner"].calls))
        self.assertEqual({r["action"] for r in second.receipt["entries"]}, {"converged", "observed"})
        after = snapshot(self.root)
        self.assertEqual(without(after, ["/var/lib/pantheon-install-receipts"]),
                         without(before, ["/var/lib/pantheon-install-receipts"]))

    def test_rerun_backs_up_state_dirs_and_rollback_restores_without_deleting(self) -> None:
        first, _ = self.apply()
        self.assertEqual(first.rc, 0)
        state = self.root / "var/lib/pantheon-host-broker"
        state.mkdir(mode=0o700)
        (state / "broker.sqlite3").write_bytes(b"journal v1")
        (self.root / "usr/local/libexec/pantheon-host-broker").write_text("drifted launcher\n")  # a real rerun
        second, holder = self.apply(receipt="/var/lib/pantheon-install-receipts/r2.json")
        self.assertEqual(second.receipt["status"], "complete")
        backups = second.receipt["backups"]
        archive = self.root / backups[0]["archive"].lstrip("/")
        self.assertEqual(oct(archive.stat().st_mode & 0o777), "0o600")  # L5: created 0600
        self.assertEqual([b["state_dir"] for b in backups], ["/var/lib/pantheon-host-broker"])
        (state / "broker.sqlite3").write_bytes(b"journal v2 (newer)")
        rc, out = self.rollback(receipt="/var/lib/pantheon-install-receipts/r2.json")
        self.assertEqual(rc, 0, out["errors"])
        self.assertEqual((state / "broker.sqlite3").read_bytes(), b"journal v1")
        kept = [p for p in self.root.joinpath("var/lib").iterdir() if p.name.startswith("pantheon-host-broker.pre-rollback-")]
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0] / "broker.sqlite3").read_bytes(), b"journal v2 (newer)")  # never deleted
        self.assertEqual(len(out["pre_rollback_backups"]), 1)
        self.assertEqual((self.root / "usr/local/libexec/pantheon-host-broker").read_text(), "drifted launcher\n")

    def test_rolling_back_a_noop_receipt_never_swaps_the_state_dirs(self) -> None:
        first, _ = self.apply()
        self.assertEqual(first.rc, 0)
        state = self.root / "var/lib/pantheon-host-broker"
        state.mkdir(mode=0o700)
        (state / "broker.sqlite3").write_bytes(b"journal v1")
        second, _ = self.apply(receipt="/var/lib/pantheon-install-receipts/r2.json")
        self.assertEqual(second.receipt["status"], "noop")
        (state / "broker.sqlite3").write_bytes(b"journal v2 (newer)")
        before = snapshot(self.root)
        rc, out = self.rollback(receipt="/var/lib/pantheon-install-receipts/r2.json")
        self.assertEqual(rc, 0, out["errors"])
        self.assertIn("noop receipt: state restore skipped", out["notes"])
        self.assertEqual((state / "broker.sqlite3").read_bytes(), b"journal v2 (newer)")
        self.assertEqual(out["pre_rollback_backups"], [])
        self.assertEqual(without(snapshot(self.root), ["/var/lib/pantheon-install-receipts"]),
                         without(before, ["/var/lib/pantheon-install-receipts"]))


class PartialFailureTest(_Base):
    def run_cut(self, **kw) -> tuple[dict, dict]:
        pre = snapshot(self.root)
        outcome, _ = self.apply(**kw)
        self.assertEqual(outcome.rc, 1)
        rc = outcome.receipt
        self.assertEqual(rc["status"], "partial")
        self.assertTrue((self.root / RECEIPT.lstrip("/")).is_file())
        failed_seq = rc["failed_entry"]["seq"]
        self.assertTrue(all(r["action"] == "not-reached" for r in rc["entries"] if r["seq"] > failed_seq))
        holder = {}
        code, out = self.rollback(holder=holder)
        self.assertEqual(code, 0, out["errors"])
        self.assertFalse([c for c in holder["runner"].calls if c[0] in (P.USERDEL, P.GROUPDEL)])  # accounts sticky
        residual_paths = [p for r in rc["residuals"] + out["residuals"] for p in r.get("paths", [])]
        self.assertEqual(without(snapshot(self.root), residual_paths), without(pre, residual_paths))
        return rc, out

    def test_early_cut_at_the_accounts(self) -> None:
        argv = (P.USERADD, "--uid", "1903", "--gid", "1903", "--home-dir",
                "/var/lib/pantheon/home/pantheon-hostapproval-ingress", "--create-home", "--shell", "/usr/sbin/nologin",
                "pantheon-hostapproval-ingress")
        rc, out = self.run_cut(scripted={argv: CommandResult(1, "", "useradd: fixture failure")})
        self.assertEqual(rc["failed_entry"]["id"], "account:pantheon-hostapproval-ingress")
        self.assertIn("account-created:pantheon-hostapproval-ingress", {r["id"] for r in rc["residuals"]})
        self.assertIn("/var/lib/pantheon/home", {p for r in out["residuals"] for p in r.get("paths", [])})

    def test_mid_cut_at_the_framework_active_switch(self) -> None:
        rc, out = self.run_cut(hook=lambda host: host.faults.add(("replace", FW_ACTIVE)))
        self.assertEqual(rc["failed_entry"]["id"], f"framework-active:{FW_ACTIVE}")
        rev = self.plan["framework"]["rev"]
        self.assertFalse((self.root / f"usr/local/lib/pantheon/framework/{rev}").exists())
        self.assertFalse((self.root / "usr/local/lib/pantheon").exists())  # runtime parents removed too

    def test_late_cut_at_plan_registration(self) -> None:
        argv = next(tuple(e["source"]["producer_argv"]) for e in self.plan["entries"] if e["kind"] == "plan_registration")
        rc, out = self.run_cut(scripted={argv: CommandResult(2, "", "host_plan_producer: refused: fixture")})
        self.assertEqual(rc["failed_entry"]["id"], "plan-registration:watchman:m1")
        self.assertFalse((self.root / "etc/pantheon").exists())


    def test_a_lost_receipt_falls_back_to_the_progress_journal(self) -> None:
        pre = snapshot(self.root)
        outcome, _ = self.apply(hook=lambda host: host.faults.add(("replace", FW_ACTIVE)))
        self.assertEqual(outcome.rc, 1)
        os.unlink(self.root / RECEIPT.lstrip("/"))  # as if the process died before the final write
        code, out = self.rollback()
        self.assertEqual(code, 0, out["errors"])
        self.assertTrue(out["crash_recovered"])
        residual_paths = [p for r in outcome.receipt["residuals"] + out["residuals"] for p in r.get("paths", [])]
        self.assertEqual(without(snapshot(self.root), residual_paths), without(pre, residual_paths))


def subset_closure(closure: H.Closure, drop: str) -> H.Closure:
    files = tuple(f for f in closure.files if f != drop)
    digest = hashlib.sha256()
    for rel in files:
        digest.update(rel.encode() + b"\0" + (H.REPO_ROOT / rel).read_bytes() + b"\0")
    return H.Closure(files, closure.external_imports, digest.hexdigest())


class UpgradeTest(_Base):
    def test_upgrade_then_rollback_points_active_back_and_keeps_the_previous_rev(self) -> None:
        first, _ = self.apply()
        self.assertEqual(first.rc, 0)
        rev_a = self.plan["framework"]["rev"]
        a_snapshot = snapshot(self.root)
        drop = next(f for f in T.closure().files if f.startswith("pinned-rules/"))
        closure_b = subset_closure(T.closure(), drop)
        plan_b, _ = build_plan(closure=closure_b)
        rev_b = plan_b["framework"]["rev"]
        self.assertNotEqual(rev_a, rev_b)
        receipt_b = "/var/lib/pantheon-install-receipts/r-b.json"
        second, holder = self.apply(plan=plan_b, closure=closure_b, receipt=receipt_b)
        self.assertEqual(second.rc, 0, second.receipt["failed_entry"])
        link = self.root / FW_ACTIVE.lstrip("/")
        self.assertEqual(os.readlink(link), rev_b)
        active = self.by_id(second.receipt)[f"framework-active:{FW_ACTIVE}"]
        self.assertEqual((active["action"], active["before"]["link_target"]), ("replaced", rev_a))
        regs = [r for r in second.receipt["entries"] if r["kind"] == "plan_registration"]
        self.assertTrue(all(r["action"] in ("created", "replaced") for r in regs))  # re-registered on a new rev
        self.assertEqual(self.verify(plan=plan_b)[0], 0)
        code, out = self.rollback(receipt=receipt_b)
        self.assertEqual(code, 0, out["errors"])
        self.assertEqual(os.readlink(link), rev_a)
        self.assertTrue((self.root / f"usr/local/lib/pantheon/framework/{rev_a}").is_dir())  # kept
        self.assertFalse((self.root / f"usr/local/lib/pantheon/framework/{rev_b}").exists())
        self.assertEqual(without(snapshot(self.root), ["/var/lib/pantheon-install-receipts"]),
                         without(a_snapshot, ["/var/lib/pantheon-install-receipts"]))
        rc, report = self.verify()
        self.assertEqual(rc, 0, [r for r in report if not r["ok"]])

    def test_rollback_refuses_a_rev_tree_outside_receipt_proof(self) -> None:
        host = A.FixtureHost(self.root)
        base = "/usr/local/lib/pantheon/framework"
        r = {"id": "framework:abc", "kind": "framework_tree", "action": "created", "before": {"exists": True},
             "observations": {"base": base, "partial": f"{base}/.abc.partial"}, "family": None}
        with self.assertRaisesRegex(A.EntryFailed, "never removed"):
            A._rollback_entry(host, A.FixtureCommandRunner(host), r, {"framework:abc": r})
        with self.assertRaisesRegex(A.EntryFailed, "direct child"):
            host.remove_tree_contained("/usr/local/lib", base)


# ---------------------------------------------------------------------------
# 10-11. the seam, the fragment, the CLI
# ---------------------------------------------------------------------------


class SeamTest(_Base):
    def test_handlers_default_is_unchanged_and_the_fragment_loads_through_the_seam(self) -> None:
        handlers = A._handlers_module()
        default = handlers.HostSubstrateSim()
        self.assertEqual(default._helper().manifest.sha256, handlers._manifest().sha256)
        m = H.parse_manifest(T.raw(manifest_doc()))
        fragment = H.substrate_manifest_fragment(m, self.plan["entries"])
        self.assertEqual(fragment, A.fragment_from_plan(self.plan))
        staging = self.tmp / "staging"
        H.write_staging(self.staging, staging)
        sim = handlers.HostSubstrateSim(manifest_doc=fragment, repo_root=staging)
        unit = next(e for e in self.plan["entries"] if e["id"] == "unit:pantheon-host-broker.service")
        step = sim._helper().admit("unit.system.install", dict(unit["source"]["params"]), ACTIVATE)
        self.assertEqual(step.subject, "file:/etc/systemd/system/pantheon-host-broker.service")
        self.assertTrue(sim._helper().preflight(step).ok)

    def test_fragment_paths_carry_no_acl_key(self) -> None:
        # U3b adds a mask:: rule for named ACL entries; the fragment emits none, so it stays valid
        fragment = A.fragment_from_plan(self.plan)
        self.assertTrue(fragment["paths"])
        self.assertFalse([p for p, spec in fragment["paths"].items() if "acl" in spec])


class CliTest(_Base):
    def files(self) -> list[str]:
        paths = {}
        for name, data in (("plan.json", self.plan_bytes()), ("manifest.json", T.raw(manifest_doc())),
                           ("registry.json", T.raw(registry_doc())), ("tracked", T.tracked_bytes()),
                           ("inputs.json", json.dumps(inputs_doc()).encode())):
            (self.tmp / name).write_bytes(data)
            paths[name] = str(self.tmp / name)
        return ["--plan", paths["plan.json"], "--manifest", paths["manifest.json"], "--registry", paths["registry.json"],
                "--tracked-files", paths["tracked"], "--inputs", paths["inputs.json"]]

    def test_dry_run_prints_the_ordered_actions_and_mutates_nothing(self) -> None:
        before = snapshot(self.root)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = A.main(["apply", *self.files(), "--root", str(self.root), "--receipt", RECEIPT],
                        runner_factory=runner_factory())
        self.assertEqual(rc, 0)
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r["seq"] for r in rows], list(range(len(self.plan["entries"]))))
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / RECEIPT.lstrip("/")).exists())
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id["account:pantheon-hostbroker"]["action"], "would-apply")
        self.assertEqual(by_id["account:pantheon-hostbroker"]["argv"][0], [P.GROUPADD, "--gid", "1901", "pantheon-hostbroker"])

    def test_execute_verify_and_refusals_through_the_cli(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = A.main(["apply", *self.files(), "--root", str(self.root), "--receipt", RECEIPT, "--execute"],
                        runner_factory=runner_factory())
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["status"], "complete")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(A.main(["verify", "--plan", str(self.tmp / "plan.json"), "--root", str(self.root),
                                     "--receipt", RECEIPT], runner_factory=runner_factory()), 0)
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            # O_EXCL: the same receipt path again is refused
            self.assertEqual(A.main(["apply", *self.files(), "--root", str(self.root), "--receipt", RECEIPT,
                                     "--execute"], runner_factory=runner_factory()), 2)
            self.assertEqual(A.main(["apply", *self.files(), "--root", "/", "--receipt", RECEIPT, "--execute"]), 2)
        self.assertIn("already exists", err.getvalue())
        self.assertIn("must not be '/'", err.getvalue())

    def test_a_cli_execute_without_test_handlers_stops_at_the_first_unemulated_argv(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            rc = A.main(["apply", *self.files(), "--root", str(self.root), "--receipt", RECEIPT, "--execute"])
        self.assertEqual(rc, 1)
        failed = json.loads(out.getvalue())["failed_entry"]
        self.assertEqual(failed["id"], "runtime:approval")  # build_runtime has no fixture emulation: it FAILS
        self.assertTrue(re.search(r"exited 127", failed["error"]))


# ---------------------------------------------------------------------------
# Recovery D15 conditions (T-2920 U3a argv review)
# ---------------------------------------------------------------------------


class RecoveryDpkgTest(_Base):
    def test_a_plan_naming_any_other_package_is_refused(self) -> None:
        plan = copy.deepcopy(self.plan)
        entry = next(e for e in plan["entries"] if e["id"] == "package:python3-gi")
        entry["inputs"]["package"] = "curl"
        host = A.FixtureHost(self.root)
        runner = A.FixtureCommandRunner(host)
        contents: dict = {}
        H.build_plan(H.parse_manifest(T.raw(manifest_doc())), H.parse_registry(T.raw(registry_doc())), H.REPO_ROOT,
                     closure=T.closure(), contents=contents)
        ctx = A.Ctx(host, runner, plan, F.load_manifest(A.fragment_from_plan(self.plan)), contents=contents)
        with self.assertRaisesRegex(A.ApplyRefused, "must name exactly"):
            A.preflight_refusals(ctx, None, H.parse_registry(T.raw(registry_doc())))
        with self.assertRaisesRegex(A.EntryFailed, "not one of the verify-only packages"):
            A.ex_package(ctx, entry)
        self.assertEqual(runner.calls, [])
        self.assertEqual(A.VERIFY_PACKAGES, ("python3", "python3-dbus", "python3-gi", "python3-yaml"))
        self.assertEqual(set(A.VERIFY_PACKAGES), H.REQUIRED_VERIFY_PACKAGES)


class RecoveryBuildRuntimeTest(_Base):
    def test_covered_set_env_and_argv_are_exact_and_recorded(self) -> None:
        outcome, holder = self.apply()
        self.assertEqual(outcome.rc, 0, outcome.receipt["failed_entry"])
        fw = self.plan["framework"]["root"]
        covered = H.runtime_build_files()
        walked, _ = H.import_closure(H.REPO_ROOT, (H.BUILD_RUNTIME_MODULE,))
        self.assertTrue(set(walked) <= set(covered))
        for rel in (H.BUILD_RUNTIME_REL, "tools/host_mcp_adapter/dist/requirements.lock",
                    "tools/host_mcp_adapter/dist/requirements.in", "tools/host_mcp_adapter/server.py",
                    "tools/approval_mcp_adapter/server.py"):
            self.assertIn(rel, covered)
        builds = [list(c) for c in holder["runner"].calls if _is_build(c)]
        self.assertEqual(builds, [
            ["/usr/bin/env", "-i", "LC_ALL=C", "PATH=/usr/bin:/bin", H.PYTHON3, "-I",
             f"{fw}/tools/host_mcp_adapter/dist/build_runtime.py", "--root", "/", "--wheelhouse",
             "/srv/inputs/wheelhouse", "--python", H.PYTHON3, "--adapter", key] for key in ("approval", "host")])
        self.assertFalse([a for c in builds for a in c if a.startswith("PYTHON")])
        # Recovery D15 (f): the build always runs from the explicit root-owned concrete rev
        self.assertEqual([cwd for argv, cwd in holder["runner"].call_cwds if _is_build(argv)], [fw, fw])
        for key in ("approval", "host"):
            r = self.by_id(outcome.receipt)[f"runtime:{key}"]
            self.assertEqual(r["observations"]["covered"]["files"], covered)
            self.assertEqual(r["observations"]["covered"]["wheelhouse"],
                             [{"name": "fixture-0.0-py3-none-any.whl", "sha256": WHEEL_SHA}])
            self.assertEqual(r["observations"]["env"], ["LC_ALL=C", "PATH=/usr/bin:/bin"])
            self.assertEqual(r["observations"]["cwd"], fw)
            self.assertEqual(r["argv"], [builds[0 if key == "approval" else 1]])

    def _apply_with_tamper(self, tamper):
        real = A.wheelhouse_manifest

        def wrapped(ctx, lock):
            out = real(ctx, lock)
            tamper(ctx.host)  # the last moment before the exec-time stats
            return out

        with mock.patch.object(A, "wheelhouse_manifest", wrapped):
            outcome, holder = self.apply()
        self.assertEqual(outcome.rc, 1)
        self.assertEqual(outcome.receipt["failed_entry"]["id"], "runtime:approval")
        self.assertFalse([c for c in holder["runner"].calls if _is_build(c)])  # never exec'd
        return outcome.receipt["failed_entry"]["error"]

    def test_a_covered_file_not_root_owned_at_exec_time_is_refused(self) -> None:
        fw = self.plan["framework"]["root"]
        err = self._apply_with_tamper(lambda h: h.chown(f"{fw}/{H.BUILD_RUNTIME_REL}", "pantheon-hostbroker", "root"))
        self.assertIn("not root-owned at exec time", err)

    def test_a_group_writable_framework_path_component_is_refused(self) -> None:
        err = self._apply_with_tamper(lambda h: os.chmod(h.path("/usr/local/lib"), 0o775))
        self.assertIn("/usr/local/lib: group- or other-writable at exec time", err)

    def test_a_symlinked_covered_file_is_refused(self) -> None:
        fw = self.plan["framework"]["root"]

        def swap(h):
            target = h.path(f"{fw}/tools/host_mcp_adapter/server.py")
            os.unlink(target)
            os.symlink("/etc/passwd", target)

        self.assertIn("a symlink at exec time", self._apply_with_tamper(swap))

    def test_an_unpinned_wheel_is_refused(self) -> None:
        (self.root / "srv/inputs/wheelhouse/evil-1.0-py3-none-any.whl").write_bytes(b"not pinned")
        outcome, holder = self.apply()
        self.assertEqual(outcome.rc, 1)
        self.assertIn("is not pinned by requirements.lock", outcome.receipt["failed_entry"]["error"])
        self.assertFalse([c for c in holder["runner"].calls if _is_build(c)])

    def test_adapter_and_output_dirs_are_the_fixed_literals(self) -> None:
        host = A.FixtureHost(self.root)
        ctx = A.Ctx(host, A.FixtureCommandRunner(host), self.plan, F.load_manifest(A.fragment_from_plan(self.plan)),
                    inputs=A.parse_inputs(json.dumps(inputs_doc()).encode()))
        entry = next(e for e in self.plan["entries"] if e["id"] == "runtime:host")
        for change in ({"adapter": "evil"}, {"launcher": "/usr/local/libexec/other"}):
            bad = copy.deepcopy(entry)
            bad["source"].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(A.EntryFailed, "fixed output literals"):
                A.ex_runtime(ctx, bad)
        bad = copy.deepcopy(entry)
        bad["path"] = "/usr/local/lib/pantheon/elsewhere"
        with self.assertRaisesRegex(A.EntryFailed, "fixed output literals"):
            A.ex_runtime(ctx, bad)
        doc = manifest_doc()
        doc["runtimes"]["approval"]["prefix"] = "/opt/approval"
        with self.assertRaisesRegex(H.InstallPlanError, "fixed literals"):
            H.parse_manifest(T.raw(doc))
        self.assertEqual(sorted(H.RUNTIME_ADAPTERS), ["approval", "host"])


TOKEN_PATH = "/etc/pantheon/host-approval-telegram/credentials/bot-token"
INGRESS = "pantheon-hostapproval-ingress"


class RecoveryTokenTest(_Base):
    """In-process O_EXCL creation (no install argv) and the source checks."""

    def ctx(self, **inputs_over):
        host = A.FixtureHost(self.root)
        with (self.root / "etc/passwd").open("a") as f:
            f.write(f"{INGRESS}:x:1903:1903::/x:/usr/sbin/nologin\n")
        for d, owner, mode in (("/etc/pantheon", "root", "0755"), ("/etc/pantheon/host-approval-telegram", "root", "0750"),
                               ("/etc/pantheon/host-approval-telegram/credentials", INGRESS, "0700")):
            if not host.exists(d):
                host.mkdir(d, owner, owner if owner != "root" else "root", mode)
        doc = inputs_doc()
        doc["operator_batch"]["1"].update(inputs_over)
        ctx = A.Ctx(host, A.FixtureCommandRunner(host), self.plan, F.load_manifest(A.fragment_from_plan(self.plan)),
                    inputs=A.parse_inputs(json.dumps(doc).encode()), rcd=RECEIPT + ".d", execute=True)
        entry = next(e for e in self.plan["entries"] if e["kind"] == "credential")
        return ctx, entry

    def refused(self, message, **inputs_over):
        ctx, entry = self.ctx(**inputs_over)
        with self.assertRaises(A.EntryFailed) as cm:
            A.ex_credential(ctx, entry)
        self.assertIn(message, str(cm.exception))
        self.assertNotIn(TOKEN.decode(), str(cm.exception))
        self.assertNotIn(TOKEN.decode(), repr(cm.exception))
        self.assertFalse(os.path.lexists(self.root / TOKEN_PATH.lstrip("/")))
        return ctx

    def test_creates_0600_ingress_owned_without_any_argv(self) -> None:
        ctx, entry = self.ctx()
        r = A.ex_credential(ctx, entry)
        self.assertEqual(r["action"], "created")
        self.assertEqual(ctx.runner.calls, [])
        p = self.root / TOKEN_PATH.lstrip("/")
        self.assertEqual(p.read_bytes(), TOKEN)
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
        self.assertEqual(ctx.host.owner_of(TOKEN_PATH), (INGRESS, INGRESS))
        self.assertNotIn(TOKEN.decode(), json.dumps(r))

    def test_planted_symlink_at_dest_is_refused_and_touches_nothing(self) -> None:
        decoy = self.root / "srv/inputs/decoy"
        decoy.write_bytes(b"decoy content")
        ctx, entry = self.ctx()
        ctx.host.race_hook = lambda: os.symlink("/srv/inputs/decoy", self.root / TOKEN_PATH.lstrip("/"))
        with self.assertRaisesRegex(A.EntryFailed, "planted symlink"):
            A.ex_credential(ctx, entry)
        self.assertEqual(decoy.read_bytes(), b"decoy content")
        self.assertEqual(os.readlink(self.root / TOKEN_PATH.lstrip("/")), "/srv/inputs/decoy")

    def test_a_pre_existing_dest_is_observe_only(self) -> None:
        ctx, entry = self.ctx()
        ctx.host.write_file(TOKEN_PATH, b"previously placed", INGRESS, INGRESS, "0600")
        r = A.ex_credential(ctx, entry)
        self.assertEqual(r["action"], "observed")
        self.assertEqual((self.root / TOKEN_PATH.lstrip("/")).read_bytes(), b"previously placed")
        # the same when it appears inside the lstat->open window (O_EXCL catches it)
        os.unlink(self.root / TOKEN_PATH.lstrip("/"))
        ctx.host.race_hook = lambda: ctx.host.write_file(TOKEN_PATH, b"raced in", INGRESS, INGRESS, "0600")
        self.assertEqual(A.ex_credential(ctx, entry)["action"], "observed")
        self.assertEqual((self.root / TOKEN_PATH.lstrip("/")).read_bytes(), b"raced in")

    def test_a_symlinked_parent_is_refused(self) -> None:
        host = A.FixtureHost(self.root)
        host.mkdir("/etc/pantheon", "root", "root", "0755")
        host.mkdir("/etc/pantheon/host-approval-telegram", "root", "root", "0750")
        os.symlink("/srv/inputs", self.root / "etc/pantheon/host-approval-telegram/credentials")
        ctx, entry = self.ctx()
        with self.assertRaisesRegex(A.EntryFailed, "must be a real directory"):
            A.ex_credential(ctx, entry)
        self.assertFalse((self.root / "srv/inputs/bot-token").read_bytes() != TOKEN)

    def test_a_wrong_parent_owner_or_mode_is_refused(self) -> None:
        ctx, entry = self.ctx()
        os.chmod(self.root / "etc/pantheon/host-approval-telegram/credentials", 0o750)
        with self.assertRaisesRegex(A.EntryFailed, "0700"):
            A.ex_credential(ctx, entry)

    def test_source_swapped_to_a_symlink_between_lstat_and_open_is_refused(self) -> None:
        ctx, entry = self.ctx()
        src = self.root / "srv/inputs/bot-token"

        def swap():
            os.unlink(src)
            os.symlink("/etc/shadow", src)

        ctx.host.race_hook = swap
        with self.assertRaisesRegex(A.EntryFailed, "refused at open") as cm:
            A.ex_credential(ctx, entry)
        self.assertNotIn(TOKEN.decode(), str(cm.exception))
        self.assertFalse(os.path.lexists(self.root / TOKEN_PATH.lstrip("/")))

    def test_source_swapped_to_another_regular_file_is_refused(self) -> None:
        ctx, entry = self.ctx()
        src = self.root / "srv/inputs/bot-token"

        def swap():
            os.rename(src, self.root / "srv/inputs/old")
            src.write_bytes(b"other")
            os.chmod(src, 0o600)

        ctx.host.race_hook = swap
        with self.assertRaisesRegex(A.EntryFailed, "swapped between lstat and open"):
            A.ex_credential(ctx, entry)

    def test_source_refusals(self) -> None:
        src = self.root / "srv/inputs/bot-token"
        os.chmod(src, 0o640)
        self.refused("must not be group- or other-readable")
        os.chmod(src, 0o600)
        ctx, _ = self.ctx()
        ctx.host.chown("/srv/inputs/bot-token", INGRESS, INGRESS)  # uid 1903: neither root nor the operator
        self.refused("owned by root or the operator uid")
        ctx.host.chown("/srv/inputs/bot-token", "claude-fallback", "claude-fallback")  # the operator uid 1002
        ctx, entry = self.ctx()
        self.assertEqual(A.ex_credential(ctx, entry)["action"], "created")
        os.unlink(self.root / TOKEN_PATH.lstrip("/"))
        src.write_bytes(b"x" * 4097)
        self.refused("larger than 4096")
        os.unlink(src)
        os.symlink("/etc/shadow", src)
        self.refused("not a regular file")

    def test_an_ingress_owner_outside_the_account_regex_is_refused(self) -> None:
        ctx, entry = self.ctx()
        bad = copy.deepcopy(entry)
        bad["owner"] = "../root"
        with self.assertRaisesRegex(A.EntryFailed, "not an account name"):
            A.ex_credential(ctx, bad)

    def test_the_runner_table_has_no_token_install_argv(self) -> None:
        host = A.FixtureHost(self.root)
        runner = A.FixtureCommandRunner(host)
        res = runner.run([P.INSTALL, "-o", INGRESS, "-g", INGRESS, "-m", "0600", "/srv/inputs/bot-token", TOKEN_PATH])
        self.assertEqual(res.returncode, 127)


class TarRestoreTest(unittest.TestCase):
    def test_malicious_backups_are_refused_before_extraction(self) -> None:
        import tarfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / "var/lib").mkdir(parents=True)
            host = A.FixtureHost(root)
            sd = "/var/lib/pantheon-host-broker"
            cases = {
                "dotdot": lambda t: _add(t, "pantheon-host-broker/../../evil", tarfile.REGTYPE),
                "absolute": lambda t: _add(t, "/etc/evil", tarfile.REGTYPE),
                "outside-name": lambda t: _add(t, "other-dir/x", tarfile.REGTYPE),
                "symlink-out": lambda t: _add(t, "pantheon-host-broker/l", tarfile.SYMTYPE, "/etc/passwd"),
                "symlink-up": lambda t: _add(t, "pantheon-host-broker/l", tarfile.SYMTYPE, "../../etc"),
                "device": lambda t: _add(t, "pantheon-host-broker/dev", tarfile.CHRTYPE),
                "fifo": lambda t: _add(t, "pantheon-host-broker/fifo", tarfile.FIFOTYPE),
            }
            for label, build in cases.items():
                archive = Path(tmp) / f"{label}.tar"
                with tarfile.open(archive, "w") as t:
                    build(t)
                rel = "/" + archive.relative_to(root).as_posix() if archive.is_relative_to(root) else None
                (root / "rc").mkdir(exist_ok=True)
                dest = root / "rc" / archive.name
                dest.write_bytes(archive.read_bytes())
                with self.subTest(case=label), self.assertRaises(A.EntryFailed):
                    A._restore_backup(host, f"/rc/{archive.name}", sd)
                del rel
            self.assertEqual(sorted(p.name for p in (root / "var/lib").iterdir()), [])
            self.assertFalse((Path(tmp) / "evil").exists())


def _add(tar, name, kind, linkname=""):
    info = __import__("tarfile").TarInfo(name)
    info.type = kind
    info.linkname = linkname
    data = b"x" if kind == __import__("tarfile").REGTYPE else b""
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data) if data else None)


class BuildRuntimeChildIsolationTest(unittest.TestCase):
    """Recovery D15 condition (f): build_runtime.py's child interpreters run with -I, so
    a pip/ or venv/ package planted in the operator's cwd is never imported AS ROOT.
    Runs the REAL script (empty wheelhouse: the build fails after the children start,
    which is fine; the planted marker must not appear)."""

    SCRIPT = H.REPO_ROOT / H.BUILD_RUNTIME_REL

    def run_from_planted_cwd(self, planted: str) -> tuple[Path, str]:
        import subprocess

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        cwd, root, wheels = tmp / "cwd", tmp / "root", tmp / "wheels"
        for d in (cwd / planted, root, wheels):
            d.mkdir(parents=True)
        marker = tmp / f"PLANTED-{planted}-IMPORTED"
        body = f"open({str(marker)!r}, 'w').write('planted {planted} ran')\nraise SystemExit(3)\n"
        (cwd / planted / "__init__.py").write_text(body)
        (cwd / planted / "__main__.py").write_text(body)
        result = subprocess.run(
            ["/usr/bin/env", "-i", "LC_ALL=C", "PATH=/usr/bin:/bin", sys.executable, "-I", str(self.SCRIPT),
             "--root", str(root), "--wheelhouse", str(wheels), "--python", sys.executable, "--adapter", "host"],
            cwd=cwd, capture_output=True, text=True, timeout=300, check=False)
        return marker, result.stdout + result.stderr

    def test_a_planted_venv_package_in_cwd_is_never_imported(self) -> None:
        marker, output = self.run_from_planted_cwd("venv")
        self.assertFalse(marker.exists(), f"planted venv/ ran as the build's child:\n{output[-2000:]}")

    def test_a_planted_pip_package_in_cwd_is_never_imported(self) -> None:
        marker, output = self.run_from_planted_cwd("pip")
        self.assertFalse(marker.exists(), f"planted pip/ ran as the build's child:\n{output[-2000:]}")

    def test_every_child_interpreter_call_has_dash_I_right_after_the_interpreter(self) -> None:
        tree = ast.parse(self.SCRIPT.read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "run" and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
        self.assertEqual(len(calls), 3)  # venv, pip, compileall
        for call in calls:
            argv = call.args[0]
            self.assertIsInstance(argv, ast.List)
            self.assertIsInstance(argv.elts[1], ast.Constant, ast.unparse(argv))
            self.assertEqual(argv.elts[1].value, "-I", ast.unparse(argv))
            self.assertEqual(argv.elts[2].value, "-m", ast.unparse(argv))


# ---------------------------------------------------------------------------
# independent review of 0f788b8c6 (REBOUND): MED-1..3 and L1..L7
# ---------------------------------------------------------------------------


class ReviewMedTest(PartialFailureTest):
    """Each MED reproduces the reviewer's probe (scratchpad/probe/probe_u3a.py)."""

    test_early_cut_at_the_accounts = None
    test_mid_cut_at_the_framework_active_switch = None
    test_late_cut_at_plan_registration = None
    test_a_lost_receipt_falls_back_to_the_progress_journal = None

    def test_med1_an_unexpected_exception_after_a_write_is_partial_and_rolled_back(self) -> None:
        real = A.TOOL_EXECUTORS["launcher"]

        def boom(ctx, e):
            real(ctx, e)
            raise KeyError("unexpected")

        with mock.patch.dict(A.TOOL_EXECUTORS, {"launcher": boom}):
            rc, out = self.run_cut()
        self.assertEqual(rc["failed_entry"]["id"], "launcher:/usr/local/libexec/pantheon-host-approval")
        self.assertTrue(rc["failed_entry"]["unexpected"])
        self.assertTrue(rc["failed_entry"]["mutated"])
        r = self.by_id(rc)[rc["failed_entry"]["id"]]
        self.assertEqual(r["action"], "failed-unknown")
        self.assertEqual(r["before"]["exists"], False)
        self.assertFalse((self.root / "usr/local/libexec/pantheon-host-approval").exists())

    def test_med2_p4_runtime_move_aside_then_fault_restores_the_old_build(self) -> None:
        prefix, launcher = "/usr/local/lib/pantheon/host-mcp-adapter", "/usr/local/libexec/pantheon-host-mcp-adapter"
        (self.root / "usr/local/lib/pantheon").mkdir(parents=True)
        os.chmod(self.root / "usr/local/lib/pantheon", 0o755)
        (self.root / prefix.lstrip("/")).mkdir()
        os.chmod(self.root / prefix.lstrip("/"), 0o755)
        (self.root / prefix.lstrip("/") / "old").write_text("old build")
        (self.root / launcher.lstrip("/")).write_text("old launcher")
        rc, out = self.run_cut(hook=lambda h: h.faults.add(("rename", launcher)))
        self.assertEqual(rc["failed_entry"]["id"], "runtime:host")
        self.assertEqual(self.by_id(rc)["runtime:host"]["action"], "failed")
        self.assertEqual((self.root / prefix.lstrip("/") / "old").read_text(), "old build")
        self.assertEqual((self.root / launcher.lstrip("/")).read_text(), "old launcher")

    def test_med2_dir_meta_fault_between_chown_and_chmod(self) -> None:
        (self.root / "var/lib/pantheon").mkdir()
        os.chmod(self.root / "var/lib/pantheon", 0o700)
        rc, _ = self.run_cut(hook=lambda h: h.faults.add(("chmod", "/var/lib/pantheon")))
        self.assertEqual(rc["failed_entry"]["id"], "dir:/var/lib/pantheon")
        self.assertEqual(self.by_id(rc)["dir:/var/lib/pantheon"]["action"], "failed")

    def test_med2_family_fault_after_the_install_argv(self) -> None:
        def hook(h):
            h.faults.add(("exec:" + P.SYSTEMCTL, "daemon-reload"))

        rc, _ = self.run_cut(hook=hook)
        first_unit = next(e["id"] for e in self.plan["entries"] if e["kind"] == "unit")
        self.assertEqual(rc["failed_entry"]["id"], first_unit)
        self.assertEqual(self.by_id(rc)[first_unit]["action"], "failed")

    def test_med2_framework_tree_fault_mid_write(self) -> None:
        rev = self.plan["framework"]["rev"]
        bad = f"/usr/local/lib/pantheon/framework/.{rev}.partial/tools/persona_installer.py"
        rc, _ = self.run_cut(hook=lambda h: h.faults.add(("write", bad)))
        self.assertEqual(rc["failed_entry"]["id"], f"framework:{rev}")
        self.assertFalse((self.root / f"usr/local/lib/pantheon/framework/.{rev}.partial").exists())

    def test_med2_plan_registration_fault_after_the_producer(self) -> None:
        dest = next(e["path"] for e in self.plan["entries"] if e["kind"] == "plan_registration")
        rc, _ = self.run_cut(hook=lambda h: h.faults.add(("write", dest)))
        self.assertTrue(rc["failed_entry"]["id"].startswith("plan-registration:"))
        self.assertFalse((self.root / "run/pantheon-host-install/staging").exists())

    def test_med2_registry_post_staging_removal_fault(self) -> None:
        rc, _ = self.run_cut(hook=lambda h: h.faults.add(("rmdir", "/run/pantheon-host-install/staging")))
        self.assertTrue(rc["failed_entry"]["id"].startswith("registry-post:"))

    def test_med2_credential_fault_after_the_create(self) -> None:
        rc, _ = self.run_cut(hook=lambda h: h.faults.add(("fchown", TOKEN_PATH)))
        self.assertEqual(rc["failed_entry"]["id"], f"credential:{TOKEN_PATH}")
        self.assertFalse(os.path.lexists(self.root / TOKEN_PATH.lstrip("/")))
        self.assertNotIn(TOKEN.decode(), json.dumps(rc))

    def test_med3_p2_a_symlink_swapped_in_before_rollback_never_reaches_outside(self) -> None:
        (self.root / "etc/pantheon").mkdir()
        os.chmod(self.root / "etc/pantheon", 0o700)
        out, _ = self.apply()
        self.assertEqual(out.rc, 0, out.receipt["failed_entry"])
        outside = self.tmp / "outside"
        outside.mkdir()
        os.chmod(outside, 0o755)
        os.rename(self.root / "etc/pantheon", self.tmp / "moved")
        os.symlink(str(outside), self.root / "etc/pantheon")
        code, res = self.rollback()
        self.assertEqual(code, 1)  # refused, recorded, never followed
        self.assertTrue(res["errors"])
        self.assertEqual(oct(outside.stat().st_mode & 0o777), "0o755")
        self.assertEqual(list(outside.iterdir()), [])

    def test_med3_p1_a_leaf_symlink_at_apply_is_refused(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        os.chmod(outside, 0o700)
        os.symlink(str(outside), self.root / "etc/pantheon")
        out, _ = self.apply()
        self.assertEqual(out.rc, 1)
        self.assertEqual(oct(outside.stat().st_mode & 0o777), "0o700")
        self.assertEqual(list(outside.iterdir()), [])

    def test_med3_every_write_and_meta_op_refuses_a_leaf_symlink(self) -> None:
        host = A.FixtureHost(self.root)
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "f").write_text("x")
        os.chmod(outside, 0o755)
        os.symlink(str(outside), self.root / "srv/link")
        os.symlink(str(outside / "f"), self.root / "srv/flink")
        ops = {
            "chmod": lambda: host.chmod("/srv/link", "0700"),
            "chown": lambda: host.chown("/srv/link", "x", "x"),
            "write": lambda: host.write_file("/srv/flink", b"y", "root", "root", "0644"),
            "mkdir": lambda: host.mkdir("/srv/link", "root", "root", "0755"),
            "rmdir": lambda: host.rmdir("/srv/link"),
            "unlink": lambda: host.unlink("/srv/flink"),
            "rename": lambda: host.rename("/srv/link", "/srv/elsewhere"),
            "empty-check": lambda: A._is_empty_dir(host, "/srv/link"),
        }
        for name, op in ops.items():
            with self.subTest(op=name), self.assertRaises(A.EntryFailed):
                op()
        self.assertEqual(oct(outside.stat().st_mode & 0o777), "0o755")
        self.assertEqual((outside / "f").read_text(), "x")
        self.assertEqual(host.mutations, [])


class ReviewLowTest(_Base):
    def test_l1_every_intermediate_dir_is_stat_checked(self) -> None:
        fw = self.plan["framework"]["root"]
        paths = A._exec_stat_paths(fw, H.runtime_build_files())
        for d in (f"{fw}/tools", f"{fw}/tools/host_mcp_adapter", f"{fw}/tools/host_mcp_adapter/dist",
                  f"{fw}/tools/approval_mcp_adapter"):
            self.assertIn(d, paths)
        real = A.wheelhouse_manifest

        def wrapped(ctx, lock):
            out = real(ctx, lock)
            os.chmod(ctx.host.path(f"{fw}/tools/host_mcp_adapter/dist"), 0o775)
            return out

        with mock.patch.object(A, "wheelhouse_manifest", wrapped):
            outcome, _ = self.apply()
        self.assertEqual(outcome.rc, 1)
        self.assertIn("/tools/host_mcp_adapter/dist: group- or other-writable", outcome.receipt["failed_entry"]["error"])

    def test_l2_a_real_runner_is_refused_everywhere(self) -> None:
        from tools._organ_lifecycle import SubprocessCommandRunner

        real = lambda host: SubprocessCommandRunner()  # noqa: E731
        with self.assertRaisesRegex(A.ApplyRefused, "only a FixtureCommandRunner"):
            A.apply(plan_raw=self.plan_bytes(), manifest_raw=T.raw(manifest_doc()), registry_raw=T.raw(registry_doc()),
                    tracked_raw=None, inputs_raw=json.dumps(inputs_doc()).encode(), root=self.root, receipt=RECEIPT,
                    execute=True, runner_factory=real, closure=T.closure())
        with self.assertRaisesRegex(A.ApplyRefused, "only a FixtureCommandRunner"):
            A.verify(plan_raw=self.plan_bytes(), root=self.root, runner_factory=real)
        with self.assertRaisesRegex(A.ApplyRefused, "only a FixtureCommandRunner"):
            A.rollback(receipt=RECEIPT, root=self.root, runner_factory=real)

    def test_l3_the_pyyaml_probe_is_a_module_literal(self) -> None:
        plan = copy.deepcopy(self.plan)
        entry = next(e for e in plan["entries"] if e["id"] == "package:python3-yaml")
        self.assertEqual(tuple(entry["inputs"]["probe"]), A.PYYAML_PROBE)
        entry["inputs"]["probe"] = ["/bin/sh", "-c", "echo 6.0.1"]
        host = A.FixtureHost(self.root)
        runner = A.FixtureCommandRunner(host)
        ctx = A.Ctx(host, runner, plan, F.load_manifest(A.fragment_from_plan(self.plan)))
        with self.assertRaisesRegex(A.EntryFailed, "differs from the module literal PYYAML_PROBE"):
            A.ex_package(ctx, entry)
        self.assertFalse([c for c in runner.calls if c[0] == "/bin/sh"])

    def test_l3_the_producer_runs_from_the_plans_cwd_and_it_is_recorded(self) -> None:
        outcome, holder = self.apply()
        self.assertEqual(outcome.rc, 0)
        fw = self.plan["framework"]["root"]
        cwds = [cwd for argv, cwd in holder["runner"].call_cwds if argv[0] == H.SETPRIV]
        self.assertEqual(cwds, [fw])  # one protocol-test seat, one producer run
        for r in outcome.receipt["entries"]:
            if r["kind"] == "plan_registration":
                self.assertEqual(r["observations"]["cwd"], fw)

    def test_l4_operator_uid_is_never_a_service_uid_or_nobody(self) -> None:
        for uid, message in ((1901, "planned service-account uid"), (65534, "nobody")):
            doc = inputs_doc()
            doc["operator_batch"]["1"]["operator_uid"] = uid
            with self.subTest(uid=uid), self.assertRaisesRegex(A.ApplyRefused, message):
                self.apply(inputs=doc)

    def test_l5_a_crash_recovered_rollback_still_restores_the_state(self) -> None:
        first, _ = self.apply()
        self.assertEqual(first.rc, 0)
        state = self.root / "var/lib/pantheon-host-broker"
        state.mkdir(mode=0o700)
        (state / "broker.sqlite3").write_bytes(b"journal v1")
        (self.root / "usr/local/libexec/pantheon-host-broker").write_text("drifted launcher\n")
        receipt = "/var/lib/pantheon-install-receipts/r2.json"
        second, _ = self.apply(receipt=receipt)
        self.assertEqual(second.receipt["status"], "complete")
        os.unlink(self.root / receipt.lstrip("/"))  # as if the process died before the final write
        (state / "broker.sqlite3").write_bytes(b"journal v2 (newer)")
        rc, out = self.rollback(receipt=receipt)
        self.assertEqual(rc, 0, out["errors"])
        self.assertTrue(out["crash_recovered"])
        self.assertEqual((state / "broker.sqlite3").read_bytes(), b"journal v1")



class AncestorWalkTest(_Base):
    """U3d (T-5657): the fixture family route feeds the REAL fixture stat of every
    ancestor into RootHelper's ancestor walk (not the simulated trusted-dir default)."""

    def test_a_group_writable_ancestor_refuses_the_owner_mode_family(self) -> None:
        (self.root / "etc/pantheon").mkdir()
        os.chmod(self.root / "etc/pantheon", 0o700)  # so owner_mode.set must act
        os.chmod(self.root / "etc", 0o775)
        outcome, holder = self.apply()
        self.assertEqual(outcome.rc, 1)
        self.assertEqual(outcome.receipt["failed_entry"]["id"], "owner-mode:/etc/pantheon")
        self.assertIn("ANCESTOR_REFUSED", outcome.receipt["failed_entry"]["error"])
        self.assertIn("/etc", outcome.receipt["failed_entry"]["error"])
        self.assertFalse([c for c in holder["runner"].calls if c[0] in (P.CHOWN, P.CHMOD)])

    def test_the_seeded_port_carries_the_fixture_stats(self) -> None:
        host = A.FixtureHost(self.root)
        host.mkdir("/etc/pantheon", "root", "root", "0755")
        port = A.seeded_port(host, ["owner_mode:/etc/pantheon"])
        self.assertEqual(port.path_stat("/"), ("directory", "root", "root", "0755"))
        self.assertEqual(port.path_stat("/etc"), ("directory", "root", "root", "0755"))
        self.assertEqual(port.path_stat("/etc/pantheon"), ("directory", "root", "root", "0755"))



class ReviewMed1ResidualTest(PartialFailureTest):
    """Delta review of 25d167b07: the before-capture itself must never escape the loop."""

    test_early_cut_at_the_accounts = None
    test_mid_cut_at_the_framework_active_switch = None
    test_late_cut_at_plan_registration = None
    test_a_lost_receipt_falls_back_to_the_progress_journal = None

    def test_p6_an_ancestor_symlink_at_a_family_file_subject_is_partial_not_refused(self) -> None:
        real = self.root / "etc/dbus-1"
        outside = self.tmp / "outside-dbus"
        os.rename(real, outside)
        os.symlink(str(outside), real)
        rc, _ = self.run_cut()
        failed = rc["failed_entry"]
        self.assertTrue(failed["id"].startswith("dbus_policy:"))
        self.assertIn(failed["id"], {r["id"] for r in rc["entries"] if r["action"] != "not-reached"})
        self.assertEqual({r["id"] for r in rc["entries"]}, {e["id"] for e in self.plan["entries"]})
        self.assertTrue(sum(r["action"] in ("created", "replaced") for r in rc["entries"]) > 0)
        self.assertEqual(sorted(os.listdir(outside)), ["system.d"])
        self.assertEqual(os.listdir(outside / "system.d"), [])

    def test_p5_an_oserror_inside_the_before_capture_is_partial(self) -> None:
        real = A._capture_before

        def flaky(ctx, e):
            if e["kind"] == "config":
                raise OSError(28, "No space left on device")
            return real(ctx, e)

        with mock.patch.object(A, "_capture_before", flaky):
            rc, _ = self.run_cut()
        self.assertTrue(rc["failed_entry"]["id"].startswith("config:"))
        r = self.by_id(rc)[rc["failed_entry"]["id"]]
        self.assertEqual(r["action"], "failed-before-mutation")

    def test_an_applyrefused_after_the_first_mutation_is_rc_1_never_rc_2(self) -> None:
        real = A.TOOL_EXECUTORS["launcher"]

        def refuse(ctx, e):
            raise A.ApplyRefused("refused late")

        with mock.patch.dict(A.TOOL_EXECUTORS, {"launcher": refuse}):
            rc, _ = self.run_cut()
        self.assertIn("ApplyRefused: refused late", rc["failed_entry"]["error"])
        del real

    def test_a_failure_while_recording_still_writes_a_partial_receipt(self) -> None:
        real = A._append_progress
        calls = {"n": 0}

        def flaky(host, rcd, record):
            calls["n"] += 1
            if calls["n"] == 30:
                raise OSError(28, "No space left on device")
            return real(host, rcd, record)

        with mock.patch.object(A, "_append_progress", flaky):
            outcome, _ = self.apply()
        self.assertEqual(outcome.rc, 1)
        self.assertEqual(outcome.receipt["status"], "partial")
        self.assertTrue(outcome.receipt["failed_entry"]["backstop"])


class TokenNeverHashedTest(_Base):
    def test_the_token_bytes_are_never_read_for_hashing(self) -> None:
        real_read, real_sha = A.FixtureHost.read_bytes, A.sha256_bytes

        def read(host, host_path):
            if host_path in (TOKEN_PATH, "/srv/inputs/bot-token"):
                raise AssertionError(f"the token at {host_path} was read")
            return real_read(host, host_path)

        def sha(data):
            if TOKEN in data:
                raise AssertionError("the token bytes were hashed")
            return real_sha(data)

        with mock.patch.object(A.FixtureHost, "read_bytes", read), mock.patch.object(A, "sha256_bytes", sha):
            outcome, _ = self.apply()
            self.assertEqual(outcome.rc, 0, outcome.receipt["failed_entry"])
            rc, report = self.verify()
            self.assertEqual(rc, 0, [r for r in report if not r["ok"]])
        self.assertEqual((self.root / TOKEN_PATH.lstrip("/")).read_bytes(), TOKEN)


# ---------------------------------------------------------------------------
# T-2920 U4b: the amended setpriv form, receipt separation (Recovery (c)), the seam
# ---------------------------------------------------------------------------


OLD_SETPRIV_PREFIX = [H.SETPRIV, "--reuid=nobody", "--regid=nogroup", "--clear-groups", "--"]


class U4bSetprivFormTest(_Base):
    def test_the_plan_carries_the_amended_ruled_form(self) -> None:
        regs = [e for e in self.plan["entries"] if e["kind"] == "plan_registration"]
        self.assertTrue(regs)
        for e in regs:
            argv = e["source"]["producer_argv"]
            self.assertEqual(argv[:8], [H.SETPRIV, "--reuid=nobody", "--regid=nogroup", "--clear-groups",
                                        "--no-new-privs", "--inh-caps=-all", "--bounding-set=-all", "--"])
            self.assertTrue(A.producer_argv_shape_ok(argv))

    def test_the_old_form_now_fails_even_with_a_matching_handler(self) -> None:
        argv = list(next(e for e in self.plan["entries"] if e["kind"] == "plan_registration")["source"]["producer_argv"])
        old = OLD_SETPRIV_PREFIX + argv[8:]
        self.assertFalse(A.producer_argv_shape_ok(old))
        runner = runner_factory()(A.FixtureHost(self.root))
        res = runner.run(old, cwd=self.plan["framework"]["root"])
        self.assertEqual(res.returncode, 127)
        for i in range(1, 8):  # any one hardening/shape token changed or dropped fails too
            bad = argv[:i] + argv[i + 1:]
            self.assertEqual(runner.run(bad).returncode, 127, bad[:9])

    def test_a_swapped_producer_output_is_refused_by_the_printed_sha(self) -> None:
        """Recovery (load-bearing: nobody is a shared uid): the bytes the root side reads must
        hash to the producer's PRINTED record, or the registration refuses."""

        def swapped(argv, host):
            res = _producer(argv, host)
            out = argv[argv.index("--out") + 1]
            data = json.loads(host.read_bytes(out))
            data["persona"] = "someone-else"  # same owner, mode and shape; different bytes
            host.write_file(out, H.canonical(data).encode("utf-8"), "nobody", "nogroup", "0644")
            return res

        holder: dict = {}
        orig = runner_factory(None, holder)

        def factory(host):
            runner = orig(host)
            runner.handlers.insert(0, (_is_producer, swapped))
            return runner

        outcome = A.apply(plan_raw=self.plan_bytes(), manifest_raw=T.raw(manifest_doc()),
                          registry_raw=T.raw(registry_doc()), tracked_raw=None,
                          inputs_raw=json.dumps(inputs_doc()).encode(), root=self.root, receipt=RECEIPT, execute=True,
                          runner_factory=factory, closure=T.closure(), now=lambda: "2026-10-27T12:00:00Z")
        self.assertEqual(outcome.rc, 1)
        self.assertIn("sha256 differs from its printed record", outcome.receipt["failed_entry"]["error"])
        self.assertTrue(outcome.receipt["failed_entry"]["id"].startswith("plan-registration:"))
        failed = self.by_id(outcome.receipt)[outcome.receipt["failed_entry"]["id"]]
        self.assertFalse((self.root / failed["path"].lstrip("/")).exists())  # nothing was installed


class U4bReceiptSeparationTest(_Base):
    def _rewrite(self, **changes) -> None:
        path = self.root / RECEIPT.lstrip("/")
        doc = json.loads(path.read_bytes())
        doc.update(changes)
        doc["receipt_sha256"] = A.receipt_digest(doc)
        os.chmod(path, 0o600)
        path.write_bytes((H.canonical(doc) + "\n").encode("ascii"))

    def test_fixture_receipts_say_fixture_and_the_head_carries_the_mode(self) -> None:
        outcome, _ = self.apply()
        self.assertEqual(outcome.rc, 0, outcome.receipt["failed_entry"])
        self.assertEqual(outcome.receipt["mode"], "fixture")
        self.assertNotIn("rehearsal", outcome.receipt)
        head = json.loads((self.root / (RECEIPT.lstrip("/") + ".d/progress.jsonl")).read_text().splitlines()[0])
        self.assertEqual(head["mode"], "fixture")

    def test_the_fixture_path_refuses_rehearsal_and_live_receipts(self) -> None:
        outcome, _ = self.apply()
        self.assertEqual(outcome.rc, 0, outcome.receipt["failed_entry"])
        for mode in ("rehearsal-nspawn", "live"):
            with self.subTest(mode=mode):
                self._rewrite(mode=mode, rehearsal={"machine": "m", "run_id": "r", "nonce_sha256": "0" * 64})
                raw = (self.root / RECEIPT.lstrip("/")).read_bytes()
                with self.assertRaisesRegex(A.ApplyRefused, "cannot serve the fixture path"):
                    self.verify(receipt_raw=raw)
                with self.assertRaisesRegex(A.ApplyRefused, "cannot serve the fixture path"):
                    self.rollback()

    def test_receipt_mode_rules(self) -> None:
        att = mock.Mock(machine="m1", run_id="run-1", nonce_sha256="a" * 64)
        rehearsal = mock.Mock(attestation=att)
        ident = {"machine": "m1", "run_id": "run-1", "nonce_sha256": "a" * 64}
        ok = [({"mode": "fixture"}, "fixture"), ({"crash_recovered": True}, "fixture"),
              ({"mode": "rehearsal-nspawn", "rehearsal": ident}, "rehearsal-nspawn"), ({"mode": "live"}, "live")]
        for doc, want in ok:
            with self.subTest(doc=doc, want=want):
                A.require_receipt_mode(doc, want, rehearsal=rehearsal)
        bad = [({"mode": "rehearsal-nspawn", "rehearsal": ident}, "live"), ({"mode": "fixture"}, "live"),
               ({}, "live"), ({"crash_recovered": True}, "live"),
               ({"mode": "live"}, "rehearsal-nspawn"), ({"mode": "fixture"}, "rehearsal-nspawn"),
               ({"crash_recovered": True}, "rehearsal-nspawn"), ({}, "rehearsal-nspawn"),
               ({"mode": "rehearsal-nspawn", "rehearsal": {**ident, "run_id": "other"}}, "rehearsal-nspawn"),
               ({"mode": "rehearsal-nspawn", "rehearsal": {**ident, "machine": "m2"}}, "rehearsal-nspawn"),
               ({"mode": "rehearsal-nspawn"}, "rehearsal-nspawn"),
               ({"mode": "rehearsal-nspawn", "rehearsal": ident}, "fixture"), ({"mode": "live"}, "fixture"),
               ({}, "fixture"), ({"mode": "fixture"}, "nonsense")]
        for doc, want in bad:
            with self.subTest(doc=doc, want=want), self.assertRaises(A.ApplyRefused):
                A.require_receipt_mode(doc, want, rehearsal=rehearsal)
        with self.assertRaises(A.ApplyRefused):  # the rehearsal path with no attestation at all
            A.require_receipt_mode({"mode": "rehearsal-nspawn", "rehearsal": ident}, "rehearsal-nspawn")


class U4bSeamTest(unittest.TestCase):
    def test_a_non_context_rehearsal_is_refused_on_every_entry_point(self) -> None:
        with mock.patch.object(A, "_plan_doc", lambda raw: {"framework": {"root": "/usr/local/lib/pantheon/framework/x"},
                                                           "entries": [], "plan_sha256": "0" * 64}):
            for fake in (object(), mock.Mock(), {"host": None}):
                with self.subTest(fake=type(fake).__name__):
                    with self.assertRaisesRegex(A.ApplyRefused, "attested RehearsalContext"):
                        A.apply(plan_raw=b"", manifest_raw=b"", registry_raw=b"", tracked_raw=b"", inputs_raw=b"",
                                root="/", receipt=RECEIPT, execute=False, rehearsal=fake)
                    with self.assertRaisesRegex(A.ApplyRefused, "attested RehearsalContext"):
                        A.verify(plan_raw=b"", root="/", rehearsal=fake)
                    with self.assertRaisesRegex(A.ApplyRefused, "attested RehearsalContext"):
                        A.rollback(receipt=RECEIPT, root="/", rehearsal=fake)

    def test_the_cli_stays_fixture_only(self) -> None:
        tree = ast.parse(Path(A.__file__).read_text(encoding="utf-8"))
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        self.assertNotIn("rehearsal", ast.dump(main))


class ProductionPlanApplyTest(_Base):
    """ORG-052 section 8 clarification (#1665): a production plan (no protocol-test
    seat) applies, verifies and reruns with no plan registration at all."""

    def production(self):
        return build_plan(manifest=T.with_approver(T.manifest_doc()))

    def test_a_production_plan_applies_and_verifies_with_no_registered_plan(self) -> None:
        plan, _ = self.production()
        self.assertFalse([e for e in plan["entries"] if e["kind"] in ("plan_registration", "registry_post_registration")])
        outcome, holder = self.apply(plan=plan, manifest=T.with_approver(T.manifest_doc()))
        self.assertEqual(outcome.rc, 0, outcome.receipt["failed_entry"])
        self.assertEqual([c for c in holder["runner"].calls if c[0] == H.SETPRIV], [])  # the producer never runs
        registry = json.loads((self.root / "etc/pantheon/host-broker/seat-registry.json").read_bytes())
        self.assertNotIn("plans", registry)
        self.assertFalse((self.root / "run/pantheon-host-install").exists())
        self.assertEqual(os.listdir(self.root / "etc/pantheon/host-broker/plans"), [])
        rc, report = self.verify(plan=plan)
        self.assertEqual(rc, 0, [r for r in report if not r["ok"]])
        self.assertFalse([r for r in report if "plans not registered" in json.dumps(r)])

    def test_a_production_rerun_is_idempotent_and_still_registers_nothing(self) -> None:
        plan, _ = self.production()
        manifest = T.with_approver(T.manifest_doc())
        first, _ = self.apply(plan=plan, manifest=manifest)
        self.assertEqual(first.rc, 0, first.receipt["failed_entry"])
        before = snapshot(self.root)
        second, holder = self.apply(plan=plan, manifest=manifest, receipt="/var/lib/pantheon-install-receipts/r2.json")
        self.assertEqual(second.rc, 0, second.receipt["failed_entry"])
        self.assertEqual([c for c in holder["runner"].calls if c[0] == H.SETPRIV], [])
        self.assertEqual(without(snapshot(self.root), ["/var/lib/pantheon-install-receipts"]),
                         without(before, ["/var/lib/pantheon-install-receipts"]))


if __name__ == "__main__":
    unittest.main()
