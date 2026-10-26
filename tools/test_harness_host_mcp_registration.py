"""Tests for role-gated host/approver MCP registration generation
(ORG-052 §7, Unit 4b, T-2916): ``tools/harness_adapter/host_mcp_registration.py``.

Tiers (explicit skip reasons, never silent):

1. Pure generation - golden bytes, closed structure, the policy gate over
   REAL ``SeatRegistry`` / ``ApproverPolicy`` loaded from fixture files
   through ``RootOwnedFileLoader``, the forbidden-field scan, launch-tuple
   parity, the SUPPORT_MATRIX integrity check. Needs ``tools.host_control``
   importable for the policy-gate tier (system python3).
2. Server-observed parity - launches every server a generated fragment
   declares through the official MCP SDK client (both protocol eras) and
   compares canonical tools/list across harnesses. Needs the pinned
   adapter runtime (mcp 2.x + jeepney), i.e. the fixture venv.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import hashlib
import importlib
import importlib.metadata
import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.approval_mcp_adapter import contract as approval_contract  # noqa: E402
from tools.harness_adapter import host_mcp_registration as reg  # noqa: E402
from tools.host_mcp_adapter import contract as host_contract  # noqa: E402

GOLDEN_DIR = REPO_ROOT / "tools" / "harness_adapter" / "golden" / "host_mcp"
HOST_DIST_DIR = REPO_ROOT / "tools" / "host_mcp_adapter" / "dist"
NO_BUS = "unix:path=/nonexistent-t2916/bus.sock"

HOST_TOOLS = tuple(t.name for t in host_contract.TOOLS)
APPROVAL_TOOLS = tuple(t.name for t in approval_contract.TOOLS)


def _host_control_skip() -> str | None:
    try:
        import tools.host_control.approval.identity  # noqa: F401
        import tools.host_control.registry  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return f"policy-gate tests need tools.host_control importable here ({type(exc).__name__})"
    return None


HOST_CONTROL_SKIP = _host_control_skip()
requires_host_control = unittest.skipIf(HOST_CONTROL_SKIP is not None, HOST_CONTROL_SKIP or "")


def _runtime_skip() -> str | None:
    try:
        version = importlib.metadata.version("mcp")
        importlib.metadata.version("jeepney")
    except importlib.metadata.PackageNotFoundError:
        return (
            "server-observed parity needs the pinned adapter runtime (mcp 2.x + jeepney): run under the "
            "fixture venv - in CI the hash-locked mcp-adapter-runtime workflow (T-4858) runs it"
        )
    if not version.startswith("2."):
        return (
            f"server-observed parity needs mcp 2.x (this interpreter has {version}): run under the "
            "fixture venv - in CI the hash-locked mcp-adapter-runtime workflow (T-4858) runs it"
        )
    return None


RUNTIME_SKIP = _runtime_skip()
requires_runtime = unittest.skipIf(RUNTIME_SKIP is not None, RUNTIME_SKIP or "")


# --------------------------------------------------------------------
# fixtures: real registry + policy files through the real trusted loader
# --------------------------------------------------------------------

BASE_UID = 41000
SEATS = {
    # seat_id: (uid, harness)
    "specialist": (BASE_UID + 1, "codex"),     # agent-source approver
    "mason": (BASE_UID + 2, "claude"),    # known subject, NO approver standing
    "alpha": (BASE_UID + 3, "claude"),    # plan owner, requester-only
    "hum": (BASE_UID + 4, "codex"),       # human-source approver subject
    "ghost": (BASE_UID + 5, "codex"),     # registered seat, unknown to approver policy
    "consultant": (BASE_UID + 6, "claude"),    # agent-source approver on a claude seat
}


def _approver(decision_source: str = "agent") -> dict:
    return {
        "decision_source": decision_source,
        "grants": [{"family": "persona.install", "seat_classes": ["worker-seat"], "max_ttl_seconds": 3600}],
    }


def policy_raw(*, approvers: dict | None = None, subjects: dict | None = None, revision: str = "approver-policy-r1") -> dict:
    subjects = subjects if subjects is not None else {
        "specialist": SEATS["specialist"][0],
        "mason": SEATS["mason"][0],
        "alpha": SEATS["alpha"][0],
        "human-operator": SEATS["hum"][0],
        "consultant": SEATS["consultant"][0],
    }
    approvers = approvers if approvers is not None else {
        "specialist": _approver(),
        "consultant": _approver(),
        "human-operator": _approver("human"),
    }
    return {
        "schema": "pantheon-approval-policy/v1",
        "revision": revision,
        "seat_classes": {"recovery": "worker-seat"},
        "subjects": subjects,
        "family_categories": {"persona.install": "seat_apply_reversible"},
        "approvers": approvers,
        "revokers": [],
    }


def registry_raw(manifest_path: Path) -> dict:
    return {
        "schema": "pantheon-host-seat-registry/v1",
        "seats": [
            {
                "seat_id": seat_id,
                "uid": uid,
                "persona": seat_id,
                "role": "worker",
                "instance": "t2916",
                "account": seat_id,
                "harness": harness,
                "install_root": f"/home/{seat_id}",
            }
            for seat_id, (uid, harness) in SEATS.items()
        ],
        "manifests": [
            {
                "revision": "manifest-r1",
                "path": str(manifest_path),
                "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "framework_revision": "t2916",
                "framework_sha256": "0" * 64,
                "template_ids": ["tools/systemd/pantheon-persona-consume@.service"],
            }
        ],
    }


class RootFixture:
    """A fixture 'root-owned' tree (owned by the test uid, the
    ``_open_for_test`` trust mode) holding a seat registry and approver
    policy; inputs are loaded through ``reg.load_root_inputs`` exactly as
    the enable leg loads the real ones, but with the test loader."""

    def __init__(self, base: Path, policy: dict) -> None:
        self.root = base / "etc"
        self.root.mkdir(mode=0o700, parents=True)
        self.root.chmod(0o700)
        manifest = self.root / "instance.yaml"
        manifest.write_text("schema: t2916-fixture\n", encoding="utf-8")
        self.registry_path = self.root / "seat-registry.json"
        self.policy_path = self.root / "approver-policy.json"
        self.registry_path.write_text(json.dumps(registry_raw(manifest)), encoding="utf-8")
        self.write_policy(policy)
        for path in (manifest, self.registry_path, self.policy_path):
            path.chmod(0o600)

    def write_policy(self, policy: dict) -> None:
        self.policy_path.write_text(json.dumps(policy), encoding="utf-8")
        self.policy_path.chmod(0o600)

    def load(self):
        """(loader, registry, policy); the caller closes the loader."""

        from tools.host_control.trusted_files import RootOwnedFileLoader

        loader = RootOwnedFileLoader._open_for_test((self.root,), expected_owner_uid=os.getuid())
        registry, policy = reg.load_root_inputs(
            policy_path=self.policy_path, registry_path=self.registry_path, loader=loader
        )
        return loader, registry, policy


class _FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="t2916-reg-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def fixture(self, policy: dict | None = None):
        fixture = RootFixture(self.base / f"f{len(list(self.base.iterdir()))}", policy or policy_raw())
        loader, registry, policy_obj = fixture.load()
        self.addCleanup(loader.close)
        return registry, policy_obj


# ====================================================================
# Tier 1 - pure generation
# ====================================================================


class StructureTests(unittest.TestCase):
    def test_registration_type_is_closed(self) -> None:
        self.assertEqual(
            [f.name for f in dataclasses.fields(reg.StdioMcpRegistration)],
            ["server_name", "command", "tool_names", "startup_timeout_sec", "tool_timeout_sec", "required"],
        )

    def test_tool_lists_come_from_the_adapter_contracts(self) -> None:
        self.assertEqual(reg.HOST_REGISTRATION.tool_names, HOST_TOOLS)
        self.assertEqual(reg.APPROVAL_REGISTRATION.tool_names, APPROVAL_TOOLS)
        self.assertEqual(len(HOST_TOOLS), 7)
        self.assertEqual(len(APPROVAL_TOOLS), 4)

    def test_commands_are_the_packaged_launchers(self) -> None:
        sys.path.insert(0, str(HOST_DIST_DIR))
        try:
            import build_runtime
        finally:
            sys.path.remove(str(HOST_DIST_DIR))
        self.assertEqual(reg.HOST_ADAPTER_COMMAND, "/" + build_runtime.HOST_PROFILE.launcher.as_posix())
        self.assertEqual(reg.APPROVAL_ADAPTER_COMMAND, "/" + build_runtime.APPROVAL_PROFILE.launcher.as_posix())
        self.assertEqual(reg.PACKAGED_COMMANDS, {reg.HOST_ADAPTER_COMMAND, reg.APPROVAL_ADAPTER_COMMAND})

    def test_startup_posture_and_timeouts(self) -> None:
        for registration in (reg.HOST_REGISTRATION, reg.APPROVAL_REGISTRATION):
            self.assertFalse(registration.required)
            self.assertGreater(
                registration.tool_timeout_sec,
                approval_contract.CONNECT_TIMEOUT + approval_contract.WRITE_CALL_TIMEOUT,
            )
            self.assertGreater(
                registration.tool_timeout_sec,
                host_contract.CONNECT_TIMEOUT + host_contract.WRITE_CALL_TIMEOUT,
            )

    def test_seat_registrations_host_always_approver_only_behind_the_gate(self) -> None:
        self.assertEqual(reg.seat_registrations(approver=False), (reg.HOST_REGISTRATION,))
        self.assertEqual(reg.seat_registrations(approver=True), (reg.HOST_REGISTRATION, reg.APPROVAL_REGISTRATION))

    def test_root_input_paths_are_named_constants(self) -> None:
        self.assertEqual(reg.ROOT_APPROVER_POLICY_PATH, Path("/etc/pantheon/host-approval/approver-policy.json"))
        self.assertEqual(reg.ROOT_SEAT_REGISTRY_PATH, Path("/etc/pantheon/host-broker/seat-registry.json"))

    def test_module_writes_nothing_and_has_no_process_surface(self) -> None:
        source = Path(reg.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "exec", "eval"})
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"write_text", "write_bytes", "Popen", "run", "system", "environ", "mkdir", "unlink"})


class GoldenTests(unittest.TestCase):
    CASES = (
        ("codex", "approver", True, "toml"),
        ("codex", "requester", False, "toml"),
        ("claude", "approver", True, "json"),
        ("claude", "requester", False, "json"),
    )

    def test_rendered_fragments_match_golden_bytes(self) -> None:
        self.assertEqual({p.name for p in GOLDEN_DIR.iterdir()}, {f"{h}-{r}.{e}" for h, r, _a, e in self.CASES})
        for harness, role, approver, ext in self.CASES:
            rendered = reg.RENDERERS[harness](reg.seat_registrations(approver=approver))
            golden = (GOLDEN_DIR / f"{harness}-{role}.{ext}").read_text(encoding="utf-8")
            self.assertEqual(rendered, golden, f"{harness}-{role}")
            self.assertEqual(rendered, reg.RENDERERS[harness](reg.seat_registrations(approver=approver)))

    def test_codex_fragment_round_trips_through_tomllib(self) -> None:
        document = tomllib.loads((GOLDEN_DIR / "codex-approver.toml").read_text(encoding="utf-8"))
        servers = document["mcp_servers"]
        self.assertEqual(list(servers), ["pantheon-host", "pantheon-approval"])
        approval = servers["pantheon-approval"]
        self.assertEqual(approval["command"], reg.APPROVAL_ADAPTER_COMMAND)
        self.assertEqual(approval["enabled_tools"], list(APPROVAL_TOOLS))
        self.assertEqual(approval["startup_timeout_sec"], 10)
        self.assertEqual(approval["tool_timeout_sec"], 40)
        self.assertIs(approval["required"], False)
        # dotted tool names survive as quoted keys
        self.assertEqual(set(approval["tools"]), set(APPROVAL_TOOLS))
        self.assertEqual({v["approval_mode"] for v in approval["tools"].values()}, {"approve"})
        requester = tomllib.loads((GOLDEN_DIR / "codex-requester.toml").read_text(encoding="utf-8"))
        self.assertEqual(list(requester["mcp_servers"]), ["pantheon-host"])

    def test_claude_fragment_shape(self) -> None:
        document = json.loads((GOLDEN_DIR / "claude-approver.json").read_text(encoding="utf-8"))
        self.assertEqual(
            document["claude_json"]["mcpServers"]["pantheon-approval"],
            {"type": "stdio", "command": reg.APPROVAL_ADAPTER_COMMAND, "args": []},
        )
        self.assertEqual(document["settings_json"]["permissions"]["allow"], ["mcp__pantheon-host", "mcp__pantheon-approval"])
        requester = json.loads((GOLDEN_DIR / "claude-requester.json").read_text(encoding="utf-8"))
        self.assertEqual(list(requester["claude_json"]["mcpServers"]), ["pantheon-host"])
        self.assertNotIn("pantheon-approval", (GOLDEN_DIR / "claude-requester.json").read_text(encoding="utf-8"))


@requires_host_control
class PolicyGateTests(_FixtureCase):
    def test_approver_entry_iff_policy_approver(self) -> None:
        registry, policy = self.fixture()
        expected = {"specialist": True, "mason": False, "alpha": False, "hum": False, "ghost": False, "consultant": True}  # hum: T-2919 Q4
        for seat_id, is_approver in expected.items():
            rendered = reg.generate_for_seat(seat_id, registry=registry, policy=policy)
            self.assertEqual(rendered.approver, is_approver, seat_id)
            self.assertEqual(
                rendered.servers,
                ("pantheon-host", "pantheon-approval") if is_approver else ("pantheon-host",),
                seat_id,
            )
            self.assertEqual(rendered.harness, SEATS[seat_id][1])
            self.assertEqual(("pantheon-approval" in rendered.text), is_approver, seat_id)

    def test_requester_only_seat_gets_no_approver_entry(self) -> None:
        registry, policy = self.fixture()
        for seat_id in ("alpha", "mason", "ghost"):
            for harness in reg.HARNESSES:
                rendered = reg.generate_for_seat(seat_id, registry=registry, policy=policy, harness=harness)
                document = reg.parse_rendered(harness, rendered.text)
                specs = reg.launch_specs(harness, rendered.text)
                self.assertEqual(list(specs), ["pantheon-host"], (seat_id, harness))
                for tool in APPROVAL_TOOLS:
                    self.assertNotIn(tool, rendered.text, (seat_id, harness))
                self.assertNotIn(reg.APPROVAL_ADAPTER_COMMAND, json.dumps(document))

    def test_ambiguous_uid_gets_no_entry_like_the_server(self) -> None:
        raw = policy_raw()
        raw["subjects"]["specialist-twin"] = SEATS["specialist"][0]
        raw["approvers"]["specialist-twin"] = _approver()
        registry, policy = self.fixture(raw)
        self.assertFalse(reg.generate_for_seat("specialist", registry=registry, policy=policy).approver)
        from tools.host_control.approval.identity import ApproverResolver

        self.assertIsNone(ApproverResolver(policy).subject_for_uid(SEATS["specialist"][0]))

    def test_human_source_approver_gets_no_registration(self) -> None:
        """SUPPORT_MATRIX 'proven' row (T-2919 Q4): a human-decision-source
        subject has NO Reviewer standing - the shared resolver skips it -
        so generation emits no approver registration, and the server gate
        (the same predicate) refuses it. Its only path is HumanIngress."""

        registry, policy = self.fixture()
        self.assertEqual(policy.approvers["human-operator"].decision_source, "human")
        rendered = reg.generate_for_seat("hum", registry=registry, policy=policy)
        self.assertFalse(rendered.approver)
        self.assertFalse(reg.is_policy_approver(policy, SEATS["hum"][0]))
        from tools.host_control.approval.identity import ApproverResolver

        self.assertIsNone(ApproverResolver(policy).subject_for_uid(SEATS["hum"][0]))
        # an agent-source approver on another uid still resolves (the
        # exclusion is by decision source, not a blanket refusal)
        self.assertIsNotNone(ApproverResolver(policy).subject_for_uid(SEATS["specialist"][0]))
        self.assertEqual(reg.SUPPORT_MATRIX["human_source_approver.gets_no_registration"][0], "proven")
        self.assertNotIn("human_source_approver.gets_registration", reg.SUPPORT_MATRIX)

    def test_policy_removal_drops_the_entry(self) -> None:
        registry, policy = self.fixture()
        self.assertTrue(reg.generate_for_seat("specialist", registry=registry, policy=policy).approver)
        revoked = policy_raw(approvers={"consultant": _approver(), "human-operator": _approver("human")}, revision="approver-policy-r2")
        registry2, policy2 = self.fixture(revoked)
        self.assertFalse(reg.generate_for_seat("specialist", registry=registry2, policy=policy2).approver)
        self.assertTrue(reg.generate_for_seat("consultant", registry=registry2, policy=policy2).approver)

    def test_generation_tracks_the_bus_scenarios(self) -> None:
        """The exact policy documents the private-bus revocation test runs
        (tools/test_host_control_approval_dbus_driver.py), keyed to the
        specialist seat's uid: approver under r1, no entry under r2 or for the
        requester-only shape - generation and the live gate agree."""

        from tools.test_host_control_approval_dbus_driver import SCENARIOS

        uid = SEATS["specialist"][0]
        for scenario, expected in (
            ("cross_service", True),
            ("cross_service_revoked", False),
            ("cross_service_requester", False),
        ):
            registry, policy = self.fixture(SCENARIOS[scenario](uid)["policy"])
            self.assertEqual(reg.generate_for_seat("specialist", registry=registry, policy=policy).approver, expected, scenario)

    def test_unknown_seat_refuses(self) -> None:
        registry, policy = self.fixture()
        with self.assertRaises(reg.RegistrationError):
            reg.generate_for_seat("nobody", registry=registry, policy=policy)

    def test_generated_text_never_carries_a_registered_uid(self) -> None:
        registry, policy = self.fixture()
        uids = reg.known_uids_of(registry, policy)
        self.assertIn(SEATS["specialist"][0], uids)
        for seat_id in SEATS:
            for harness in reg.HARNESSES:
                text = reg.generate_for_seat(seat_id, registry=registry, policy=policy, harness=harness).text
                for uid in uids:
                    self.assertNotIn(str(uid), text)

    def test_cli_requires_root_owned_inputs(self) -> None:
        fixture = RootFixture(self.base / "cli", policy_raw())
        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            code = reg.main([
                "--seat", "specialist",
                "--approver-policy", str(fixture.policy_path),
                "--seat-registry", str(fixture.registry_path),
            ])
        self.assertEqual(code, 1)
        # the production loader's root-trust walk refuses a tree the test
        # uid owns under a world-writable /tmp
        self.assertIn("refused: RegistryError", err.getvalue())
        self.assertEqual(out.getvalue(), "")


class ForbiddenFieldScanTests(unittest.TestCase):
    def _clean(self, harness: str) -> dict:
        return reg.parse_rendered(harness, reg.RENDERERS[harness](reg.seat_registrations(approver=True)))

    def test_clean_documents_pass(self) -> None:
        for harness in reg.HARNESSES:
            reg.assert_registration_clean(harness, self._clean(harness), known_uids={1000, 41001})

    def test_every_forbidden_class_is_refused(self) -> None:
        host = "pantheon-host"

        def codex(mutate):
            document = self._clean("codex")
            mutate(document["mcp_servers"][host])
            return document

        def claude(mutate):
            document = self._clean("claude")
            mutate(document["claude_json"]["mcpServers"][host])
            return document

        cases = {
            # broker token
            "codex env token": ("codex", codex(lambda t: t.update(env={"PANTHEON_BROKER_TOKEN": "x"}))),
            "codex bearer": ("codex", codex(lambda t: t.update(bearer_token_env_var="TOKEN"))),
            "claude env token": ("claude", claude(lambda e: e.update(env={"TOKEN": "abc"}))),
            "claude header": ("claude", claude(lambda e: e.update(headers={"Authorization": "Bearer abc"}))),
            # D-Bus address override
            "codex env bus address": ("codex", codex(lambda t: t.update(env={"DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/tmp/b"}))),
            "codex args bus address": ("codex", codex(lambda t: t.update(args=["--bus-address", "unix:path=/tmp/b"]))),
            "claude args bus address": ("claude", claude(lambda e: e.update(args=["unix:path=/run/dbus/system_bus_socket"]))),
            "claude nonempty args": ("claude", claude(lambda e: e.update(args=["--self-check"]))),
            "codex url": ("codex", codex(lambda t: t.update(url="http://127.0.0.1:9"))),
            # Unix uid
            "codex uid key": ("codex", codex(lambda t: t.update(uid=41001))),
            "claude uid key": ("claude", claude(lambda e: e.update(uid=41001))),
            "codex uid as timeout": ("codex", codex(lambda t: t.update(tool_timeout_sec=41001))),
            "codex boolean elsewhere": ("codex", codex(lambda t: t.update(startup_timeout_sec=True))),
            # bus policy
            "codex busconfig value": ("codex", codex(lambda t: t["tools"]["host.plan"].update(approval_mode="<busconfig><policy user='x'/>"))),
            "codex send_destination": ("codex", codex(lambda t: t.update(enabled_tools=list(HOST_TOOLS) + ["send_destination"]))),
            # executor path
            "codex executor command": ("codex", codex(lambda t: t.update(command="/usr/local/libexec/pantheon-host-executor"))),
            "claude executor command": ("claude", claude(lambda e: e.update(command="/usr/local/libexec/pantheon-host-executor"))),
            # command / server allowlists
            "codex foreign command": ("codex", codex(lambda t: t.update(command="/usr/bin/python3"))),
            "codex swapped command": ("codex", codex(lambda t: t.update(command=reg.APPROVAL_ADAPTER_COMMAND))),
            "codex extra tool": ("codex", codex(lambda t: t.update(enabled_tools=list(HOST_TOOLS) + ["host.shell"]))),
            "codex per-tool extra key": ("codex", codex(lambda t: t["tools"]["host.plan"].update(env="x"))),
            "claude http transport": ("claude", claude(lambda e: e.update(type="http"))),
            # keys only the structural allowlists catch (no denylisted text,
            # no number): an empty env block or a working-directory override
            "claude empty env": ("claude", claude(lambda e: e.update(env={}))),
            "claude cwd": ("claude", claude(lambda e: e.update(cwd="/srv/seat"))),
            "codex empty env": ("codex", codex(lambda t: t.update(env={}))),
            "codex cwd": ("codex", codex(lambda t: t.update(cwd="/srv/seat"))),
        }
        # N1 (T-2916 review): the per-tool approval_mode is pinned, and no
        # string anywhere may carry a D-Bus address (any transport), a
        # uid-as-string or a token-shaped value. These probes all got past
        # the 425490b7a scan.
        smuggled_values = {
            "tcp": "tcp:host=127.0.0.1,port=5555",
            "nonce-tcp": "nonce-tcp:host=localhost,noncefile=/tmp/n",
            "unix dir": "unix:dir=/tmp",
            "unix tmpdir": "unix:tmpdir=/tmp",
            "unix runtime": "unix:runtime=yes",
            "unixexec": "unixexec:path=/bin/sh",
            "autolaunch bare": "autolaunch:",
            "systemd bare": "systemd:",
            "launchd": "launchd:env=DBUS_LAUNCHD_SESSION_BUS_SOCKET",
            "vsock": "vsock:cid=2,port=1",
            "future transport": "quic:host=example",
            "address list": "approve;tcp:host=h",
            "known uid string": "41001",
            "unknown uid string": "1001",
            "ghp token": "ghp_" + "a" * 36,
            "gho token": "gho_" + "b" * 36,
            "github pat": "github_pat_" + "c" * 30,
            "sk token": "sk-" + "d" * 40,
            "slack token": "xoxb-1234-abcd",
            "not approve": "prompt",
        }
        for label, value in smuggled_values.items():
            cases[f"codex approval_mode {label}"] = (
                "codex", codex(lambda t, v=value: t["tools"]["host.plan"].update(approval_mode=v))
            )
        self.smuggled_values = smuggled_values
        unknown_server = self._clean("codex")
        unknown_server["mcp_servers"]["pantheon-shell"] = dict(unknown_server["mcp_servers"][host])
        cases["codex unknown server"] = ("codex", unknown_server)
        extra_rule = self._clean("claude")
        extra_rule["settings_json"]["permissions"]["allow"].append("Bash")
        cases["claude extra permission rule"] = ("claude", extra_rule)
        extra_top = self._clean("codex")
        extra_top["projects"] = {}
        cases["codex extra top-level"] = ("codex", extra_top)
        for label, (harness, document) in cases.items():
            with self.assertRaises(reg.RegistrationError, msg=label):
                reg.assert_registration_clean(harness, document, known_uids={41001})

    def test_string_scan_refuses_each_class_on_its_own(self) -> None:
        """The deep string scan, isolated from the structural pins (which
        would also catch most of these): each arm must refuse alone."""

        known = frozenset({41001})
        refusals = {
            "D-Bus address": [
                "tcp:host=127.0.0.1,port=5555", "nonce-tcp:host=h,noncefile=/n", "unix:dir=/tmp",
                "unix:tmpdir=/tmp", "unix:runtime=yes", "unixexec:path=/bin/sh", "autolaunch:",
                "systemd:", "launchd:env=X", "vsock:cid=2,port=1", "approve;tcp:host=h",
                "quic:host=example",
            ],
            "token-shaped value": [
                "ghp_" + "a" * 36, "gho_" + "b" * 36, "ghs_" + "c" * 36, "github_pat_" + "d" * 30,
                "sk-" + "e" * 40, "xoxb-1234-abcd", "Authorization: sk-abc",
            ],
            "a registered uid as a string": ["41001", " 41001 "],
            "numeric string (uid-shaped)": ["1001", "0"],
        }
        for reason, values in refusals.items():
            for value in values:
                self.assertEqual(reg._forbidden_string(value, known), reason, value)
                with self.assertRaises(reg.RegistrationError, msg=value):
                    reg._deep_scan({"tools": {"x": {"note": value}}}, known)
        for harness in reg.HARNESSES:
            for _path, value in reg._walk(self._clean(harness), ()):
                if isinstance(value, str):
                    self.assertIsNone(reg._forbidden_string(value, known), value)

    def test_registered_uid_in_a_timeout_is_refused(self) -> None:
        with self.assertRaises(reg.RegistrationError):
            reg.render_codex(reg.seat_registrations(approver=False), known_uids={40})

    def test_renderers_fail_closed(self) -> None:
        bad = dataclasses.replace(reg.HOST_REGISTRATION, command="/usr/local/libexec/pantheon-host-executor")
        for harness in reg.HARNESSES:
            with self.assertRaises(reg.RegistrationError, msg=harness):
                reg.RENDERERS[harness]((bad,))
        smuggled = dataclasses.replace(reg.HOST_REGISTRATION, tool_names=HOST_TOOLS + ("host.shell",))
        with self.assertRaises(reg.RegistrationError):
            reg.render_codex((smuggled,))


class LaunchTupleParityTests(unittest.TestCase):
    def test_launch_tuples_are_identical_across_harnesses(self) -> None:
        for approver in (True, False):
            regs = reg.seat_registrations(approver=approver)
            codex = reg.launch_specs("codex", reg.render_codex(regs))
            claude = reg.launch_specs("claude", reg.render_claude(regs))
            self.assertEqual(codex, claude)
            for command, args, env in codex.values():
                self.assertIn(command, reg.PACKAGED_COMMANDS)
                self.assertEqual((args, env), ((), ()))


class SupportMatrixTests(unittest.TestCase):
    def _resolve(self, dotted: str) -> None:
        module_name, class_name, method_name = dotted.rsplit(".", 2)
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
        self.assertTrue(callable(getattr(cls, method_name)), dotted)

    def test_every_row_is_proven_by_a_named_test_recorded_or_unsupported(self) -> None:
        for key, (status, evidence) in reg.SUPPORT_MATRIX.items():
            self.assertIn(status, {"proven", "recorded", "unsupported"}, key)
            if status == "proven":
                self._resolve(evidence)
            elif status == "recorded" and evidence.startswith("tools."):
                self._resolve(evidence.split(":", 1)[0])
            else:
                self.assertTrue(evidence.strip(), key)

    def test_unsupported_rows_the_ho_ruled_on_are_present(self) -> None:
        for key in (
            "codex.real_client_accepts_generated_table",
            "claude.real_client_accepts_generated_config",
            "revocation.production_latency",
            "distinct_uid_separation_and_bus_policy_fragments",
            "clients.dotted_tool_names",
        ):
            self.assertEqual(reg.SUPPORT_MATRIX[key][0], "unsupported", key)
        self.assertIn("T-2924", reg.SUPPORT_MATRIX["codex.real_client_accepts_generated_table"][1])
        self.assertIn("restart", reg.SUPPORT_MATRIX["revocation.production_latency"][1])

    def test_no_real_client_behaviour_is_marked_proven(self) -> None:
        for key, (status, _evidence) in reg.SUPPORT_MATRIX.items():
            if "real_client" in key or "(E3)" in key or "(E4)" in key:
                self.assertEqual(status, "unsupported", key)


# ====================================================================
# Tier 2 - server-observed parity through the official SDK client
# ====================================================================

# Production launcher -> never-packaged test driver at a dead bus address.
# tools/list never touches the bus, so this observes exactly what the
# packaged binary serves; args/env from the fragment are appended as-is
# (both are empty by the tests above).
DRIVER_FOR_COMMAND = {
    reg.HOST_ADAPTER_COMMAND: ["-m", "tools.test_host_mcp_adapter_driver", NO_BUS],
    reg.APPROVAL_ADAPTER_COMMAND: ["-m", "tools.test_approval_mcp_adapter_driver", NO_BUS],
}


async def _observe(harness: str, text: str, mode: str) -> dict[str, list[dict]]:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    observed = {}
    for name, (command, args, env) in reg.launch_specs(harness, text).items():
        params = StdioServerParameters(
            command=sys.executable,
            args=DRIVER_FOR_COMMAND[command] + list(args),
            env=dict(env) or None,
            cwd=str(REPO_ROOT),
        )
        async with Client(params, mode=mode) as client:
            tools = (await client.list_tools()).tools
            observed[name] = [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools]
    return observed


@requires_runtime
class ServerObservedParityTests(unittest.TestCase):
    def test_server_observed_tools_list_is_canonically_identical(self) -> None:
        for approver in (True, False):
            regs = reg.seat_registrations(approver=approver)
            texts = {harness: reg.RENDERERS[harness](regs) for harness in reg.HARNESSES}
            canon = {}
            for harness, text in texts.items():
                for mode in ("2026-07-28", "legacy"):
                    observed = asyncio.run(_observe(harness, text, mode))
                    canon[(harness, mode)] = host_contract.canonical_text(observed)
            self.assertEqual(len(set(canon.values())), 1, f"approver={approver}")

    def test_requester_seat_union_has_no_approval_tools(self) -> None:
        union = {}
        for approver in (True, False):
            text = reg.render_claude(reg.seat_registrations(approver=approver))
            observed = asyncio.run(_observe("claude", text, "2026-07-28"))
            union[approver] = [tool["name"] for tools in observed.values() for tool in tools]
        self.assertEqual(union[True], list(HOST_TOOLS) + list(APPROVAL_TOOLS))
        self.assertEqual(union[False], list(HOST_TOOLS))
        self.assertFalse([name for name in union[False] if name.startswith("approval.")])


if __name__ == "__main__":
    unittest.main()
