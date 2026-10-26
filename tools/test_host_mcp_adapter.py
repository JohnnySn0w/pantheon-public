"""Tests for the privilege-free stdio host MCP adapter (ORG-052 Unit 4a, T-2915).

Four tiers, each with an EXPLICIT skip reason when its prerequisites are
absent (never a silent skip):

1. Pure contract / parity / structural / lock tests — run everywhere,
   including CI (system python3; parity tests additionally need the
   landed ``tools.host_control`` importable, and the signature-parity test
   needs dbus-python).
2. Runtime tests (SDK stdio, private bus) — need the pinned runtime
   (mcp 2.x + jeepney). CI pins mcp 1.x for the seat MCP servers, so they
   skip there; run them locally with the fixture venv built by
   ``tools/host_mcp_adapter/dist/build_runtime.py``; in CI the hash-locked
   ``.github/workflows/mcp-adapter-runtime.yml`` job (T-4858) runs them and
   fails on any skip. ``SkipGuardTests`` (tier 1) pins that each guard
   fires ONLY when its prerequisite is absent.
3. Private-bus tests also need ``dbus-daemon`` plus a system python3 with
   dbus-python for the landed broker driver (the same private-bus pattern
   as T-2912/T-2913, via ``tools/_host_control_testbus.py``).
4. Packaging tests need a hash-verified wheelhouse named by
   ``T2915_WHEELHOUSE`` (``pip download --require-hashes`` of the lock).

Two test-only knobs (T-4858), defaults unchanged: ``PANTHEON_BROKER_PYTHON``
is the interpreter that runs the landed broker driver for the private-bus
tier (default ``/usr/bin/python3``; it needs dbus-python, PyGObject and
PyYAML), and ``T2915_BUILD_PYTHON`` is the interpreter the packaging tier
builds the runtime venv from (default ``/usr/bin/python3``; it must be the
lock's CPython 3.12).

Local run (fixture venv python, from the repo root)::

    timeout 900 taskset -c 3 <fixture>/usr/local/lib/pantheon/host-mcp-adapter/venv/bin/python3 \\
        -m unittest tools.test_host_mcp_adapter -v
"""

from __future__ import annotations

import ast
import asyncio
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.host_mcp_adapter import contract  # noqa: E402

PACKAGE_DIR = REPO_ROOT / "tools" / "host_mcp_adapter"
DIST_DIR = PACKAGE_DIR / "dist"
sys.path.insert(0, str(DIST_DIR))
import build_runtime  # noqa: E402

# T-4858: the broker driver's interpreter and the packaging build interpreter; CI points them
# at a dbus-capable venv and at setup-python 3.12 (the lock's target) respectively.
SYSTEM_PYTHON = os.environ.get("PANTHEON_BROKER_PYTHON") or "/usr/bin/python3"
BUILD_PYTHON = os.environ.get("T2915_BUILD_PYTHON") or "/usr/bin/python3"
DBUS_DAEMON = shutil.which("dbus-daemon")
NO_BUS = "unix:path=/nonexistent-t2915/bus.sock"

EXPECTED_TOOLS = (
    ("host.capabilities", "Capabilities", "", "ss"),
    ("host.plan", "Plan", "sss", "sss"),
    ("host.apply", "Apply", "ssss", "s"),
    ("host.verify", "Verify", "sss", "s"),
    ("host.rollback", "Rollback", "sss", "s"),
    ("host.operation.get", "OperationGet", "s", "ss"),
    ("host.approval.get", "ApprovalGet", "s", "ss"),
)


# --------------------------------------------------------------------
# skip guards (explicit reasons)
# --------------------------------------------------------------------


def runtime_skip_reason(version_of: Callable[[str], str] = importlib.metadata.version) -> str | None:
    """None iff the pinned adapter runtime (mcp 2.x + jeepney) is importable."""

    def _version(dist: str) -> str | None:
        try:
            return version_of(dist)
        except importlib.metadata.PackageNotFoundError:
            return None

    mcp_version = _version("mcp")
    if mcp_version is None or not mcp_version.startswith("2."):
        return (
            "T-2915 runtime tests need the pinned mcp 2.x runtime "
            f"(this interpreter has mcp {mcp_version or 'absent'}); run them with the "
            "fixture venv from tools/host_mcp_adapter/dist/build_runtime.py - "
            "in CI the hash-locked mcp-adapter-runtime workflow (T-4858) runs them"
        )
    if _version("jeepney") is None:
        return "T-2915 runtime tests need jeepney (part of the pinned adapter runtime)"
    return None


RUNTIME_SKIP = runtime_skip_reason()
requires_runtime = unittest.skipIf(RUNTIME_SKIP is not None, RUNTIME_SKIP or "")


def _system_python_has_dbus() -> bool:
    if not os.path.exists(SYSTEM_PYTHON):
        return False
    probe = subprocess.run(
        [SYSTEM_PYTHON, "-c", "import dbus, gi, yaml"],
        capture_output=True,
        timeout=30,
    )
    return probe.returncode == 0


def private_bus_skip_reason(
    runtime_skip: str | None,
    dbus_daemon: str | None,
    broker_python_has_dbus: Callable[[], bool],
) -> str | None:
    """None iff the private-bus tier can run (T-4858 carry-in 1: a pure guard, pinned by
    SkipGuardTests, so the tier cannot start skipping without a test noticing)."""
    if runtime_skip is not None:
        return runtime_skip
    if not dbus_daemon:
        return "private-bus tests need dbus-daemon on PATH"
    if not broker_python_has_dbus():
        return (
            f"private-bus tests need {SYSTEM_PYTHON} (PANTHEON_BROKER_PYTHON) with dbus-python, "
            "PyGObject and PyYAML for the landed broker driver"
        )
    return None


PRIVATE_BUS_SKIP = private_bus_skip_reason(RUNTIME_SKIP, DBUS_DAEMON, _system_python_has_dbus)
requires_private_bus = unittest.skipIf(
    RUNTIME_SKIP is not None or PRIVATE_BUS_SKIP is not None,
    RUNTIME_SKIP or PRIVATE_BUS_SKIP or "",
)

WHEELHOUSE = os.environ.get("T2915_WHEELHOUSE", "")
requires_wheelhouse = unittest.skipUnless(
    WHEELHOUSE and Path(WHEELHOUSE).is_dir(),
    "packaging tests need T2915_WHEELHOUSE pointing at a hash-verified wheelhouse "
    "(pip download --require-hashes -r tools/host_mcp_adapter/dist/requirements.lock)",
)


def _import_host_control() -> None:
    import tools.host_control.wire  # noqa: F401
    import tools.host_control.operations  # noqa: F401
    import tools.host_control.planner  # noqa: F401


def host_control_skip_reason(importer: Callable[[], None] = _import_host_control) -> str | None:
    """None iff the landed tools.host_control imports (the parity tier's prerequisite)."""
    try:
        importer()
    except Exception as exc:  # noqa: BLE001
        return f"parity tests need the landed tools.host_control importable here ({type(exc).__name__})"
    return None


def _host_control_importable() -> str | None:
    return host_control_skip_reason()


def _private_bus_work_dir_args() -> dict[str, str]:
    """Where the private-bus tier's work dir goes (T-4858 carry-in 2): the system temp dir, NOT
    $TMPDIR, which a CI or pytest wrapper may point deep into the checkout; a short prefix, since
    unix-socket paths are capped near 108 bytes. Kwargs for tempfile.TemporaryDirectory."""
    return {"prefix": "t2915-w-", "dir": "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()}


HOST_CONTROL_SKIP = _host_control_importable()
requires_host_control = unittest.skipIf(HOST_CONTROL_SKIP is not None, HOST_CONTROL_SKIP or "")


# ====================================================================
# Tier 1 — pure
# ====================================================================


class SkipGuardTests(unittest.TestCase):
    """The runtime skip fires ONLY when the pinned runtime is absent."""

    @staticmethod
    def _versions(mapping):
        def version_of(dist):
            if dist not in mapping:
                raise importlib.metadata.PackageNotFoundError(dist)
            return mapping[dist]

        return version_of

    def test_guard_does_not_fire_with_mcp2_and_jeepney(self) -> None:
        self.assertIsNone(runtime_skip_reason(self._versions({"mcp": "2.3.0", "jeepney": "0.9.0"})))
        self.assertIsNone(runtime_skip_reason(self._versions({"mcp": "2.0.0", "jeepney": "0.9.0"})))

    def test_guard_fires_with_explicit_reason_when_mcp2_absent(self) -> None:
        for mapping in ({"mcp": "1.27.1", "jeepney": "0.9.0"}, {"jeepney": "0.9.0"}, {}):
            reason = runtime_skip_reason(self._versions(mapping))
            self.assertIsNotNone(reason)
            self.assertIn("mcp 2.x", reason)

    def test_guard_fires_when_jeepney_absent(self) -> None:
        reason = runtime_skip_reason(self._versions({"mcp": "2.3.0"}))
        self.assertIn("jeepney", reason)

    def test_guard_agrees_with_this_interpreter(self) -> None:
        try:
            mcp_version = importlib.metadata.version("mcp")
        except importlib.metadata.PackageNotFoundError:
            mcp_version = None
        if mcp_version and mcp_version.startswith("2."):
            try:
                importlib.metadata.version("jeepney")
                self.assertIsNone(RUNTIME_SKIP)
            except importlib.metadata.PackageNotFoundError:
                self.assertIsNotNone(RUNTIME_SKIP)
        else:
            self.assertIsNotNone(RUNTIME_SKIP)

    # T-4858 carry-in 1: the parity-tier and private-bus guards, pinned like the runtime one.

    def test_host_control_guard_fires_only_when_the_import_fails(self) -> None:
        self.assertIsNone(host_control_skip_reason(lambda: None))

        def broken() -> None:
            raise ImportError("gone")

        reason = host_control_skip_reason(broken)
        self.assertIn("tools.host_control importable", reason)
        self.assertIn("ImportError", reason)

    def test_host_control_is_importable_in_this_checkout(self) -> None:
        # Not a skip: the landed package is in this repo, so failing to import it is a regression
        # that would otherwise turn the whole parity tier into quiet skips.
        self.assertIsNone(HOST_CONTROL_SKIP)

    def test_private_bus_guard_truth_table(self) -> None:
        def yes() -> bool:
            return True

        def no() -> bool:
            return False

        self.assertIsNone(private_bus_skip_reason(None, "/usr/bin/dbus-daemon", yes))
        self.assertEqual(private_bus_skip_reason("no runtime", "/usr/bin/dbus-daemon", yes), "no runtime")
        self.assertIn("dbus-daemon on PATH", private_bus_skip_reason(None, None, yes))
        reason = private_bus_skip_reason(None, "/usr/bin/dbus-daemon", no)
        self.assertIn("PANTHEON_BROKER_PYTHON", reason)
        self.assertIn("dbus-python", reason)

    def test_private_bus_guard_agrees_with_this_host(self) -> None:
        expected = private_bus_skip_reason(RUNTIME_SKIP, DBUS_DAEMON, _system_python_has_dbus)
        self.assertEqual(PRIVATE_BUS_SKIP, expected)
        if RUNTIME_SKIP is None and DBUS_DAEMON and _system_python_has_dbus():
            self.assertIsNone(PRIVATE_BUS_SKIP)

    def test_private_bus_work_dir_is_short_and_outside_the_checkout(self) -> None:
        # T-4858 carry-in 2: the broker's domain lives under this dir; keep it out of the repo
        # tree (lints walk it) and short (unix-socket paths are capped near 108 bytes).
        with tempfile.TemporaryDirectory(**_private_bus_work_dir_args()) as name:
            path = Path(name).resolve()
            self.assertFalse(path.is_relative_to(REPO_ROOT.resolve()), path)
            self.assertTrue(path.name.startswith("t2915-w-"), path)
            self.assertLess(len(str(path)) + len("/broker-99/domain/bus.sock"), 108, path)


class ToolTableTests(unittest.TestCase):
    def test_exactly_seven_tools_in_ratified_order(self) -> None:
        got = tuple(
            (tool.name, tool.method.member, tool.method.in_signature, tool.method.out_signature)
            for tool in contract.TOOLS
        )
        self.assertEqual(got, EXPECTED_TOOLS)
        self.assertEqual(len({tool.method for tool in contract.TOOLS}), 7)
        self.assertEqual(len(contract.Method), 7)

    def test_params_and_outputs_match_signature_arity(self) -> None:
        for tool in contract.TOOLS:
            self.assertEqual(len(tool.params), len(tool.method.in_signature), tool.name)
            self.assertEqual(len(tool.outputs), len(tool.method.out_signature), tool.name)

    def test_no_identity_or_passthrough_parameters(self) -> None:
        forbidden = re.compile(
            r"uid|persona|role|instance|caller|subject|command|argv|shell|path|file|"
            r"content|env|sudo|destination|interface|member|signature|address|credential|token"
        )
        for tool in contract.TOOLS:
            for name, _kind in tool.params:
                self.assertIsNone(forbidden.search(name), f"{tool.name}.{name}")

    def test_annotations_match_adr_section_4(self) -> None:
        by_name = {tool.name: contract.annotations(tool) for tool in contract.TOOLS}
        for name in ("host.capabilities", "host.operation.get", "host.approval.get"):
            self.assertTrue(by_name[name]["readOnlyHint"], name)
        for name in ("host.plan", "host.verify"):
            self.assertFalse(by_name[name]["readOnlyHint"], name)
            self.assertFalse(by_name[name]["destructiveHint"], name)
        for name in ("host.apply", "host.rollback"):
            self.assertFalse(by_name[name]["readOnlyHint"], name)
            self.assertTrue(by_name[name]["destructiveHint"], name)
            self.assertTrue(by_name[name]["idempotentHint"], name)
        for hints in by_name.values():
            self.assertFalse(hints["openWorldHint"])

    def test_timeouts_are_bounded(self) -> None:
        for tool in contract.TOOLS:
            self.assertLessEqual(tool.timeout, 30.0)
        self.assertLessEqual(contract.CONNECT_TIMEOUT, 5.0)


@requires_host_control
class LandedParityTests(unittest.TestCase):
    """The mirrored constants cannot drift from the landed service."""

    def test_wire_constants_match(self) -> None:
        from tools.host_control import wire

        self.assertEqual(contract.BUS_NAME, wire.BUS_NAME)
        self.assertEqual(contract.OBJECT_PATH, wire.OBJECT_PATH)
        self.assertEqual(contract.MANAGER_INTERFACE, wire.MANAGER_INTERFACE)
        self.assertEqual(contract.ERROR_NAMESPACE, wire.ERROR_NAMESPACE)
        self.assertEqual(set(contract.WIRE_ERROR_CODES), set(wire.CLOSED_ERROR_NAMES))
        self.assertEqual(contract.JSON_LIMIT, wire._JSON_LIMIT)

    def test_input_bounds_match_landed_validators(self) -> None:
        from tools.host_control import operations
        from tools.host_control.contracts import HostControlError

        corpus = [
            "", "a", "A" * 128, "A" * 129, "has space", "tab\t", "x\x7f", "é", "!~",
            "0" * 64, "0" * 63, "0" * 65, "F" * 64, "g" * 64, "a-b_c.d:e/f",
            # T-4858 carry-in 4: a trailing newline is the classic `$`-anchor escape.
            "a\n", "0" * 64 + "\n", "\n", "a\r\n", "a" * 127 + "\n",
        ]

        def accepts(fn, value):
            try:
                fn(value, "field")
                return True
            except HostControlError:
                return False

        from tools.host_control.planner import HostPlanService

        for value in corpus:
            self.assertEqual(contract._matches("scalar", value), accepts(operations._bounded_scalar, value), repr(value))
            self.assertEqual(contract._matches("hex64", value), accepts(operations._bounded_hex, value), repr(value))
            # T-4858 carry-in 4: the planner keeps its OWN scalar validator (host.plan's fields).
            self.assertEqual(
                contract._matches("scalar", value), accepts(HostPlanService._bounded_scalar, value), repr(value)
            )
        for value in ("a\n", "0" * 64 + "\n", "\n"):
            self.assertFalse(contract._matches("scalar", value), repr(value))
            self.assertFalse(contract._matches("hex64", value), repr(value))

    def test_every_param_kind_is_the_kind_its_landed_method_enforces(self) -> None:
        # T-4858 carry-in 4: the field -> kind mapping, observed on the landed call path. Each
        # landed validator raises HostControlError("invalid <field label>"), the label being the
        # param name with spaces. A value valid for only ONE kind is fed to one param at a time
        # (the others valid); the field's own rejection must fire exactly when the adapter's
        # kind would refuse it. Domain objects are stubs that stop the call past validation.
        from tools.host_control import operations
        from tools.host_control.contracts import HostControlError
        from tools.host_control.planner import HostPlanService

        class _Reached(Exception):
            pass

        class _Domain:
            def subject_for_uid(self, uid):
                return "subject"

            def __getattr__(self, name):
                def stop(*args, **kwargs):
                    raise _Reached(name)

                return stop

        ops = object.__new__(operations.OperationService)
        ops._registry = _Domain()
        ops._journal = _Domain()
        planner = object.__new__(HostPlanService)
        planner._registry = _Domain()
        landed = {
            "host.plan": planner.plan,
            "host.apply": ops.prepare_apply,
            "host.verify": ops.prepare_verify,
            "host.rollback": ops.prepare_rollback,
            "host.operation.get": ops.get_operation,
        }
        # host.capabilities takes no params; host.approval.get's single handle is pinned by
        # test_approval_handle_is_hex64_on_both_sides (HostApproval1 owns its validation).
        with_params = {tool.name for tool in contract.TOOLS if tool.params}
        self.assertEqual(with_params - set(landed), {"host.approval.get"})
        valid = {"scalar": "k", "hex64": "a" * 64}
        only_scalar = ("g" * 64, "Z")  # valid scalars, never hex64

        def rejected(fn, args, field) -> bool:
            try:
                fn(1003, *args)
            except HostControlError as exc:
                return str(exc) == f"invalid {field.replace('_', ' ')}"
            except Exception:  # noqa: BLE001 - past validation (stub, missing domain state)
                return False
            return False

        for tool in contract.TOOLS:
            if tool.name not in landed:
                continue
            for index, (name, kind) in enumerate(tool.params):
                base = [valid[k] for _, k in tool.params]
                with self.subTest(tool=tool.name, param=name):
                    probe = list(base)
                    probe[index] = ""  # invalid for both kinds: proves the probe sees this field
                    self.assertTrue(rejected(landed[tool.name], probe, name))
                    for value in only_scalar:
                        probe[index] = value
                        self.assertEqual(rejected(landed[tool.name], probe, name), kind == "hex64", value)

    def test_approval_handle_is_hex64_on_both_sides(self) -> None:
        # T-2917 ruling f2: the canonical approval handle is 64 lowercase
        # hex everywhere (HostApproval1 mints token_hex(32)). The adapter
        # field kind and the broker's prepare-time validator move together.
        from tools.host_control import operations
        from tools.host_control.contracts import HostControlError

        approval_params = [
            (tool.name, kind)
            for tool in contract.TOOLS
            for name, kind in tool.params
            if name == "approval_handle"
        ]
        self.assertEqual(
            sorted(name for name, _ in approval_params),
            ["host.apply", "host.approval.get", "host.rollback"],
        )
        self.assertEqual({kind for _, kind in approval_params}, {"hex64"})

        class _Unreachable:
            def __getattr__(self, name):
                raise AssertionError("domain reached before handle validation")

        service = object.__new__(operations.OperationService)
        service._registry = _Unreachable()
        service._journal = _Unreachable()
        for bad in ("approval-x", "A" * 64, "a" * 63, "a" * 65):
            with self.subTest(handle=bad):
                with self.assertRaises(HostControlError):
                    service.prepare_apply(1003, "a" * 64, "b" * 64, "k", bad)
                with self.assertRaises(HostControlError):
                    service.prepare_rollback(1003, "a" * 64, "k", bad)

    def test_method_signatures_and_arg_names_match_landed_dbus_service(self) -> None:
        try:
            from tools.host_control.dbus_service import DBUS_AVAILABLE
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"dbus_service not importable here ({type(exc).__name__})")
        if not DBUS_AVAILABLE:
            self.skipTest("signature parity needs dbus-python in this interpreter")
        from tools.host_control.dbus_service import HostBroker1Object

        manager_methods = {
            name
            for name, value in vars(HostBroker1Object).items()
            if getattr(value, "_dbus_is_method", False)
            and value._dbus_interface == contract.MANAGER_INTERFACE
        }
        self.assertEqual(manager_methods, {method.member for method in contract.Method})
        for tool in contract.TOOLS:
            landed = getattr(HostBroker1Object, tool.method.member)
            self.assertEqual(landed._dbus_in_signature, tool.method.in_signature, tool.name)
            self.assertEqual(landed._dbus_out_signature, tool.method.out_signature, tool.name)
            self.assertEqual(list(landed._dbus_args), [name for name, _ in tool.params], tool.name)


class SchemaTests(unittest.TestCase):
    def test_input_schemas_are_closed_and_fully_required(self) -> None:
        for tool in contract.TOOLS:
            schema = contract.input_schema(tool)
            self.assertEqual(schema["$schema"], contract.JSON_SCHEMA_DIALECT)
            self.assertEqual(schema["type"], "object")
            self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(schema["required"], [name for name, _ in tool.params])
            for prop in schema["properties"].values():
                self.assertEqual(prop["type"], "string")
                self.assertIn("pattern", prop)

    def test_output_schemas_are_closed_objects(self) -> None:
        for tool in contract.TOOLS:
            schema = contract.output_schema(tool)
            self.assertEqual(schema["$schema"], contract.JSON_SCHEMA_DIALECT)
            self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(schema["required"], [name for name, _ in tool.outputs])

    def test_schemas_meta_validate_and_agree_with_adapter_validator(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("schema meta-validation needs jsonschema (in the pinned runtime)")
        hex_value, scalar = "a" * 64, "key-1"
        for tool in contract.TOOLS:
            in_schema = contract.input_schema(tool)
            out_schema = contract.output_schema(tool)
            Draft202012Validator.check_schema(in_schema)
            Draft202012Validator.check_schema(out_schema)
            validator = Draft202012Validator(in_schema)
            good = {name: (hex_value if kind == "hex64" else scalar) for name, kind in tool.params}
            cases = [good, {**good, "extra": "x"}, {}]
            for name, _kind in tool.params:
                cases += [
                    {k: v for k, v in good.items() if k != name},
                    {**good, name: ""},
                    {**good, name: "bad value"},
                    {**good, name: 7},
                    {**good, name: "Z" * 129},
                ]
            for case in cases:
                schema_ok = validator.is_valid(case)
                try:
                    contract.validate_arguments(tool, case)
                    adapter_ok = True
                except contract.AdapterError:
                    adapter_ok = False
                self.assertEqual(schema_ok, adapter_ok, f"{tool.name}: {case}")

    def test_success_payloads_conform_to_output_schema(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("output conformance needs jsonschema (in the pinned runtime)")
        bodies = {
            "host.capabilities": ("1", '{"schema":"pantheon-host-capabilities/v1"}'),
            "host.plan": ("a" * 64, "b" * 64, '{"schema":"pantheon-host-plan-summary/v1"}'),
            "host.apply": ("c" * 64,),
            "host.verify": ("c" * 64,),
            "host.rollback": ("c" * 64,),
            "host.operation.get": ("PREPARED", '{"operation_handle":"x"}'),
            "host.approval.get": ("ISSUED", '{"schema":"s"}'),
        }
        for tool in contract.TOOLS:
            shaped = contract.shape_result(tool, bodies[tool.name])
            Draft202012Validator(contract.output_schema(tool)).validate(shaped)


class ErrorMappingTests(unittest.TestCase):
    def test_every_closed_wire_name_maps_to_a_distinct_code(self) -> None:
        codes = list(contract.WIRE_ERROR_CODES.values())
        # 12 T-2912 OQ4 names + RateLimited (T-2917 5a, ruling i1)
        # + the 3 ORG-082 §2 ratified names (T-2918)
        self.assertEqual(len(codes), 16)
        self.assertEqual(len(set(codes)), 16)
        self.assertEqual(
            contract.WIRE_ERROR_CODES[f"{contract.ERROR_NAMESPACE}.RateLimited"],
            "broker_rate_limited",
        )
        self.assertTrue(contract.ERROR_CODES["broker_rate_limited"].retryable)
        for name, code in contract.WIRE_ERROR_CODES.items():
            if name in contract.ORG082_WIRE_ERROR_CODES:
                # ORG-082 §2: the body must be a valid broker-error payload
                tail = code.upper()
                body = {"code": tail, "amendment": "ORG-082", "retry_context": "not-retryable"}
                if tail == "SUBSTRATE_FAMILY_NOT_EXPOSED":
                    body.update(family="account.ensure", retry_context="bootstrap-in-process-root")
                self.assertEqual(contract.error_for_dbus_name(name, (json.dumps(body),)).code, code)
                self.assertEqual(contract.error_for_dbus_name(name, ("why",)).code, "broker_contract_mismatch")
                continue
            self.assertEqual(contract.error_for_dbus_name(name, ("why",)).code, code)

    def test_closed_names_carry_sanitized_bounded_detail(self) -> None:
        error = contract.error_for_dbus_name(
            f"{contract.ERROR_NAMESPACE}.PolicyDenied", ("subject_denied\n\x00" + "x" * 500,)
        )
        self.assertEqual(error.code, "policy_denied")
        self.assertTrue(error.detail.startswith("subject_denied"))
        self.assertLessEqual(len(error.detail), contract.DETAIL_LIMIT)
        self.assertTrue(all(" " <= ch <= "~" for ch in error.detail))

    def test_bus_errors_are_classified(self) -> None:
        cases = {
            "org.freedesktop.DBus.Error.ServiceUnknown": "broker_unavailable",
            "org.freedesktop.DBus.Error.NameHasNoOwner": "broker_unavailable",
            "org.freedesktop.DBus.Error.Spawn.ChildExited": "broker_unavailable",
            "org.freedesktop.DBus.Error.NoReply": "broker_no_reply",
            "org.freedesktop.DBus.Error.Timeout": "broker_no_reply",
            "org.freedesktop.DBus.Error.Disconnected": "broker_no_reply",
            "org.freedesktop.DBus.Error.AccessDenied": "bus_access_denied",
            "org.freedesktop.DBus.Error.UnknownMethod": "broker_contract_mismatch",
            f"{contract.ERROR_NAMESPACE}.Bogus": "broker_contract_mismatch",
            "": "broker_contract_mismatch",
            None: "broker_contract_mismatch",
        }
        for name, code in cases.items():
            self.assertEqual(contract.error_for_dbus_name(name, ("x",)).code, code, name)

    def test_unclosed_names_never_echo_the_remote_message(self) -> None:
        secret = "/etc/pantheon/registry.json mode 0644 owner 1003 token=abc"
        for name in (
            "org.freedesktop.DBus.Error.AccessDenied",
            "org.freedesktop.DBus.Error.ServiceUnknown",
            f"{contract.ERROR_NAMESPACE}.Bogus",
            "com.example.Other",
        ):
            payload = contract.error_for_dbus_name(name, (secret,)).payload()
            self.assertNotIn("detail", payload["error"])
            self.assertNotIn("registry", contract.canonical_text(payload))

    def test_every_error_code_is_bounded_and_known(self) -> None:
        for code, spec in contract.ERROR_CODES.items():
            self.assertRegex(code, r"^[a-z_]+$")
            self.assertLessEqual(len(spec.message), contract.DETAIL_LIMIT)
            text = contract.canonical_text(contract.AdapterError(code).payload())
            self.assertLessEqual(len(text), 400)
        with self.assertRaises(AssertionError):
            contract.AdapterError("not_a_code")
        self.assertTrue(set(contract.WIRE_ERROR_CODES.values()) <= set(contract.ERROR_CODES))

    def test_shape_result_is_strict(self) -> None:
        plan = contract.TOOLS_BY_NAME["host.plan"]
        cap = contract.TOOLS_BY_NAME["host.capabilities"]
        bad_bodies = [
            (plan, ("not-hex", "b" * 64, "{}")),
            (plan, ("a" * 64, "b" * 64)),
            (plan, ("a" * 64, "b" * 64, "[]")),
            (cap, ("1", "not json")),
            (cap, ("1", '{"a":1,"a":2}')),
            (cap, ("1", '{"a":NaN}')),
            (cap, ("1", '{"a":"' + "x" * contract.JSON_LIMIT + '"}')),
            (cap, ("", "{}")),
            (cap, "ss"),
        ]
        for tool, body in bad_bodies:
            with self.assertRaises(contract.AdapterError) as ctx:
                contract.shape_result(tool, body)
            self.assertEqual(ctx.exception.code, "broker_contract_mismatch", body)

    def test_validate_arguments_returns_signature_order(self) -> None:
        apply = contract.TOOLS_BY_NAME["host.apply"]
        args = {"approval_handle": "c" * 64, "idempotency_key": "k", "plan_digest": "b" * 64, "plan_handle": "a" * 64}
        self.assertEqual(contract.validate_arguments(apply, args), ("a" * 64, "b" * 64, "k", "c" * 64))
        self.assertEqual(contract.validate_arguments(contract.TOOLS_BY_NAME["host.capabilities"], None), ())
        with self.assertRaises(contract.AdapterError):
            contract.validate_arguments(apply, ["a"])


class RateLimiterTests(unittest.TestCase):
    def test_burst_then_refill(self) -> None:
        now = [0.0]
        limiter = contract.RateLimiter(burst=3, per_second=1.0, clock=lambda: now[0])
        self.assertEqual([limiter.allow() for _ in range(4)], [True, True, True, False])
        now[0] = 1.0
        self.assertTrue(limiter.allow())
        self.assertFalse(limiter.allow())
        now[0] = 0.5  # clock going backwards never mints tokens
        self.assertFalse(limiter.allow())


class SelfCheckReportTests(unittest.TestCase):
    def test_report_shape_and_overall_derivation(self) -> None:
        ready = ("ready", "", "r")
        report = contract.self_check_report({"runtime": ready, "system_bus": ready, "broker": ready})
        self.assertEqual(report["Status"], "ready")
        self.assertEqual(report["ContractRevision"], "pantheon-health/v1")
        self.assertEqual(set(report), {"ContractRevision", "Service", "Status", "Subcomponents", "Reasons", "Revisions"})
        degraded = contract.self_check_report(
            {"runtime": ready, "system_bus": ready, "broker": ("degraded", "absent", "-")}
        )
        self.assertEqual(degraded["Status"], "degraded")
        unhealthy = contract.self_check_report(
            {"runtime": ("unhealthy", "x", "-"), "system_bus": ready, "broker": ready}
        )
        self.assertEqual(unhealthy["Status"], "unhealthy")
        with self.assertRaises(AssertionError):
            contract.self_check_report({"runtime": ready})


class StructuralTests(unittest.TestCase):
    """Gate 18 (adapter side): no listener/credential/journal/executor/root/
    mailbox/arbitrary-D-Bus surface, enforced over the packaged sources."""

    ALLOWED_IMPORT_ROOTS = {
        "__future__", "argparse", "asyncio", "enum", "importlib", "json", "re",
        "sys", "time", "dataclasses", "typing", "anyio", "mcp", "mcp_types",
        "jeepney", "tools",
    }

    def _trees(self):
        for module in build_runtime.PACKAGED_MODULES:
            path = PACKAGE_DIR / module
            yield module, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_packaged_modules_are_exactly_the_production_set(self) -> None:
        self.assertEqual(
            set(build_runtime.PACKAGED_MODULES),
            {"__init__.py", "contract.py", "bus_client.py", "server.py"},
        )
        on_disk = {p.name for p in PACKAGE_DIR.glob("*.py")}
        self.assertEqual(on_disk, set(build_runtime.PACKAGED_MODULES))
        self.assertTrue((REPO_ROOT / "tools" / "test_host_mcp_adapter_driver.py").exists())

    def test_import_closure(self) -> None:
        for module, tree in self._trees():
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    root = name.split(".")[0]
                    self.assertIn(root, self.ALLOWED_IMPORT_ROOTS, f"{module}: {name}")
                    if root == "tools":
                        self.assertTrue(name.startswith("tools.host_mcp_adapter"), f"{module}: {name}")

    def test_jeepney_is_confined_to_bus_client(self) -> None:
        for module, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    if any(n.split(".")[0] == "jeepney" for n in names):
                        self.assertEqual(module, "bus_client.py")

    def test_no_file_env_or_process_surface(self) -> None:
        for module, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"open", "exec", "eval", "compile", "__import__"}, module)
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(
                        node.attr,
                        {"environ", "getenv", "system", "popen", "Popen", "bind", "listen", "accept", "write_text", "write_bytes"},
                        module,
                    )

    def test_bus_client_names_only_the_fixed_broker(self) -> None:
        source = (PACKAGE_DIR / "bus_client.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        strings = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(("org.", "/org"))
        }
        # broker name/path/interface come only from contract; the only other
        # destination literal is the bus daemon (the --self-check probe)
        self.assertEqual(strings, {"/org/freedesktop/DBus", "org.freedesktop.DBus"})

    def test_system_bus_address_is_fixed(self) -> None:
        self.assertEqual(contract.SYSTEM_BUS_ADDRESS, "unix:path=/run/dbus/system_bus_socket")
        source = (PACKAGE_DIR / "bus_client.py").read_text(encoding="utf-8")
        self.assertNotIn("SYSTEM'", source)
        self.assertNotIn('"SYSTEM"', source)
        self.assertNotIn("DBUS_SYSTEM_BUS_ADDRESS", source)


class LockTests(unittest.TestCase):
    LOCK = DIST_DIR / "requirements.lock"

    def _pins(self):
        lines = self.LOCK.read_text(encoding="utf-8").splitlines()
        pins = {}
        for index, line in enumerate(lines):
            match = re.match(r"^([A-Za-z0-9._-]+)==(\S+) \\$", line)
            if match:
                self.assertRegex(lines[index + 1], r"^    --hash=sha256:[0-9a-f]{64}$", line)
                pins[match.group(1).lower().replace("_", "-")] = match.group(2)
        return lines, pins

    def test_lock_is_complete_hashed_and_pins_the_approved_versions(self) -> None:
        lines, pins = self._pins()
        self.assertEqual(pins["mcp"], "2.3.0")
        self.assertEqual(pins["jeepney"], "0.9.0")
        self.assertEqual(pins["mcp-types"], "2.3.0")
        self.assertEqual(len(pins), 29)
        hashes = [line for line in lines if "--hash=sha256:" in line]
        self.assertEqual(len(hashes), len(pins))
        for line in lines:
            if line.startswith("# ") and line.endswith((".whl", ".tar.gz", ".zip")):
                self.assertTrue(line.endswith(".whl"), line)

    def test_lock_header_matches_requirements_in(self) -> None:
        self.assertTrue(build_runtime.lock_is_current())

    def test_requirements_in_pins_only_the_two_direct_dependencies(self) -> None:
        reqs = [
            line.strip()
            for line in (DIST_DIR / "requirements.in").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertEqual(reqs, ["mcp==2.3.0", "jeepney==0.9.0"])

    def test_runtime_pins_in_contract_match_lock(self) -> None:
        _lines, pins = self._pins()
        for dist, version in contract.EXPECTED_RUNTIME.items():
            self.assertEqual(pins[dist], version)


# ====================================================================
# Tier 2 — runtime (pinned mcp 2.x + jeepney)
# ====================================================================


def _driver_params(address: str):
    from mcp.client.stdio import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "tools.test_host_mcp_adapter_driver", address],
        cwd=str(REPO_ROOT),
    )


def _tool_dump(tool) -> dict:
    return tool.model_dump(by_alias=True, exclude_none=True, mode="json")


class VerifyArtifactMetadataTests(unittest.TestCase):
    """T-4858 carry-in 3: a dist-info without METADATA is a NAMED failed check, not a traceback.
    Pure tier (a fake venv tree), so it runs everywhere, CI's tools suite included."""

    def test_missing_metadata_is_a_named_check_not_a_crash(self) -> None:
        from unittest import mock

        import verify_artifact

        profile = build_runtime.HOST_PROFILE
        with tempfile.TemporaryDirectory(prefix="t4858-meta-") as tmp:
            root = Path(tmp)
            site = root / profile.prefix / "venv" / "lib" / "python3.12" / "site-packages"
            (site / "nometa-1.0.dist-info").mkdir(parents=True)
            good = site / "jeepney-0.9.0.dist-info"
            good.mkdir()
            (good / "METADATA").write_text("Name: jeepney\nVersion: 0.9.0\n", encoding="utf-8")
            with mock.patch.object(verify_artifact, "check_isolated_import", return_value=(True, "stub")), \
                    mock.patch.object(verify_artifact, "check_manifest", return_value=[]), \
                    mock.patch.object(verify_artifact, "check_modes", return_value=[]):
                report = verify_artifact.verify(root, profile=profile)
            self.assertFalse(report["ok"])
            meta = report["checks"]["dist_metadata"]
            self.assertFalse(meta["ok"])
            self.assertEqual(len(meta["missing_metadata"]), 1)
            self.assertTrue(meta["missing_metadata"][0].endswith("nometa-1.0.dist-info"))
            self.assertEqual(report["checks"]["dist_set"]["count"], 1)
            self.assertFalse(report["checks"]["dist_set"]["ok"])

    def test_all_metadata_present_passes_the_check(self) -> None:
        import verify_artifact

        profile = build_runtime.HOST_PROFILE
        with tempfile.TemporaryDirectory(prefix="t4858-meta-") as tmp:
            root = Path(tmp)
            good = root / profile.prefix / "venv" / "lib" / "python3.12" / "site-packages" / "a-1.dist-info"
            good.mkdir(parents=True)
            (good / "METADATA").write_text("Name: a\nVersion: 1\n", encoding="utf-8")
            self.assertEqual(verify_artifact.unreadable_metadata(root, profile), [])


@requires_runtime
class SdkStdioTests(unittest.TestCase):
    """Gate 1 (SDK-client half): both protocol eras over a real stdio
    subprocess, exact tools/list, schemas, error mapping, lifecycle."""

    async def _session(self, mode: str, body):
        from mcp import Client

        async with Client(_driver_params(NO_BUS), mode=mode) as client:
            return await body(client)

    def test_modern_era_2026_07_28_lists_exact_tools(self) -> None:
        async def body(client):
            tools = await client.list_tools()
            return client.protocol_version, [_tool_dump(t) for t in tools.tools]

        version, tools = asyncio.run(self._session("2026-07-28", body))
        self.assertEqual(version, "2026-07-28")
        self.assertEqual([t["name"] for t in tools], [t[0] for t in EXPECTED_TOOLS])
        for dumped, tool in zip(tools, contract.TOOLS):
            self.assertEqual(dumped["inputSchema"], contract.input_schema(tool))
            self.assertEqual(dumped["outputSchema"], contract.output_schema(tool))
            self.assertEqual(dumped["annotations"], contract.annotations(tool))

    def test_legacy_initialize_2025_11_25(self) -> None:
        async def body(client):
            tools = await client.list_tools()
            return client.protocol_version, [t.name for t in tools.tools]

        version, names = asyncio.run(self._session("legacy", body))
        self.assertEqual(version, "2025-11-25")
        self.assertEqual(names, [t[0] for t in EXPECTED_TOOLS])

    def test_tools_list_identical_across_eras_after_canonical_json(self) -> None:
        async def body(client):
            return [_tool_dump(t) for t in (await client.list_tools()).tools]

        modern = asyncio.run(self._session("2026-07-28", body))
        legacy = asyncio.run(self._session("legacy", body))
        self.assertEqual(contract.canonical_text(modern), contract.canonical_text(legacy))

    def test_unknown_tool_is_protocol_error_and_bad_arguments_are_tool_errors(self) -> None:
        from mcp.shared.exceptions import MCPError

        async def body(client):
            with self.assertRaises(MCPError):
                await client.call_tool("host.shell", {"command": "id"})
            results = []
            for name, args in (
                ("host.plan", {"seat_id": "recovery"}),
                ("host.plan", {"seat_id": "recovery", "manifest_revision": "r", "idempotency_key": "k", "uid": "0"}),
                ("host.apply", {"plan_handle": "../etc", "plan_digest": "b" * 64, "idempotency_key": "k", "approval_handle": "c" * 64}),
            ):
                results.append(await client.call_tool(name, args))
            return results

        for mode in ("2026-07-28", "legacy"):
            results = asyncio.run(self._session(mode, body))
            for result in results:
                self.assertTrue(result.is_error)
                self.assertIsNone(result.structured_content)
                payload = json.loads(result.content[0].text)
                self.assertEqual(payload["error"]["code"], "invalid_arguments")

    def test_initializes_and_returns_bounded_broker_unavailable_without_any_bus(self) -> None:
        async def body(client):
            started = time.monotonic()
            result = await client.call_tool("host.capabilities", {})
            return result, time.monotonic() - started

        for mode in ("2026-07-28", "legacy"):
            result, elapsed = asyncio.run(self._session(mode, body))
            self.assertTrue(result.is_error)
            payload = json.loads(result.content[0].text)
            self.assertEqual(payload["error"]["code"], "broker_unavailable")
            self.assertTrue(payload["error"]["retryable"])
            self.assertIn("no harness restart", payload["error"]["message"])
            self.assertLess(elapsed, contract.CONNECT_TIMEOUT + 3.0)

    def test_stdio_lifecycle_eof_exits_cleanly_with_only_jsonrpc_on_stdout(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-m", "tools.test_host_mcp_adapter_driver", NO_BUS],
            cwd=str(REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "t2915", "version": "0"},
            },
        }
        try:
            out, err = process.communicate(
                (json.dumps(initialize) + "\n").encode(), timeout=60
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        self.assertEqual(process.returncode, 0, err.decode(errors="replace")[-2000:])
        lines = [line for line in out.decode().splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 1)
        for line in lines:
            self.assertEqual(json.loads(line)["jsonrpc"], "2.0")
        reply = json.loads(lines[0])
        self.assertEqual(reply["result"]["serverInfo"]["name"], contract.ADAPTER_NAME)

    def test_main_accepts_no_bus_override(self) -> None:
        from tools.host_mcp_adapter import server

        for argv in (["--bus-address", "unix:path=/tmp/x"], ["--no-bus"], ["unix:path=/tmp/x"]):
            with self.assertRaises(SystemExit) as ctx:
                server.main(argv)
            self.assertEqual(ctx.exception.code, 2, argv)

    def test_self_check_reports_unreachable_bus_and_no_bus_mode(self) -> None:
        from tools.host_mcp_adapter import server

        components = asyncio.run(server.self_check_components(NO_BUS, probe_bus=True))
        report = contract.self_check_report(components)
        self.assertEqual(report["Subcomponents"]["runtime"], "ready")
        self.assertEqual(report["Subcomponents"]["system_bus"], "unhealthy")
        self.assertEqual(report["Status"], "unhealthy")
        skipped = contract.self_check_report(asyncio.run(server.self_check_components(NO_BUS, probe_bus=False)))
        self.assertEqual(skipped["Status"], "degraded")
        self.assertEqual(skipped["Subcomponents"]["broker"], "not_configured")

    def test_bus_client_refuses_non_enum_methods(self) -> None:
        from tools.host_mcp_adapter.bus_client import BrokerClient

        client = BrokerClient(NO_BUS)

        async def body():
            with self.assertRaises(TypeError):
                await client.call("Capabilities", (), timeout=1)
            with self.assertRaises(TypeError):
                await client.call(contract.Method.PLAN, ("a",), timeout=1)

        asyncio.run(body())

    def test_rate_limited_is_a_tool_error_and_sends_nothing(self) -> None:
        from tools.host_mcp_adapter.server import HostAdapter

        class Recorder:
            calls = 0

            async def call(self, *args, **kwargs):
                Recorder.calls += 1
                return ("1", "{}")

        adapter = HostAdapter(Recorder(), contract.RateLimiter(burst=1, per_second=0.0001))

        async def body():
            return [await adapter.call_tool("host.capabilities", {}) for _ in range(2)]

        first, second = asyncio.run(body())
        self.assertFalse(first.is_error)
        self.assertTrue(second.is_error)
        self.assertEqual(json.loads(second.content[0].text)["error"]["code"], "rate_limited")
        self.assertEqual(Recorder.calls, 1)


# ====================================================================
# Tier 3 — private bus (real landed broker driver + fault fakes)
# ====================================================================

_PERMISSIVE_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


class _Broker:
    """The landed ``tools.test_host_control_dbus_driver`` (real domain)
    run under the system python3 that carries dbus-python."""

    _starts = 0

    def __init__(self, bus_address: str, work: Path, *, register_subject: bool = True) -> None:
        self.bus_address = bus_address
        self.work = work
        self.register_subject = register_subject
        self.process: subprocess.Popen | None = None
        self.state: dict = {}

    def start(self, testbus, handle) -> None:
        # Under the test's own cleaned work dir (tempfile-hygiene lint): one
        # fresh subdir per broker start, so a restart never reuses state.
        _Broker._starts += 1
        root = self.work / f"broker-{_Broker._starts}"
        root.mkdir()
        config_path = root / "driver.json"
        config_path.write_text(
            json.dumps(
                {
                    "bus_address": self.bus_address,
                    "root": str(root / "domain"),
                    "subject_uid": os.getuid(),
                    "register_subject": self.register_subject,
                    "approval_uid": os.getuid() + 54321,
                }
            ),
            encoding="utf-8",
        )
        self.log = open(root / "driver.log", "w", encoding="utf-8")  # noqa: SIM115
        self.process = subprocess.Popen(
            [SYSTEM_PYTHON, "-m", "tools.test_host_control_dbus_driver", str(config_path)],
            cwd=str(REPO_ROOT),
            stdout=self.log,
            stderr=self.log,
            preexec_fn=testbus.pdeathsig_preexec_fn(),
        )
        handle.register_driver(self.process.pid)
        state_path = root / "domain" / "state.json"
        deadline = time.monotonic() + 60
        while not state_path.exists():
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise AssertionError("broker driver failed: " + (root / "driver.log").read_text()[-2000:])
            time.sleep(0.05)
        self.state = json.loads(state_path.read_text(encoding="utf-8"))
        # state.json is written just before the main loop: wait for the name
        deadline = time.monotonic() + 30
        while not self._name_owned():
            if time.monotonic() > deadline:
                raise AssertionError("broker never owned its name")
            time.sleep(0.05)

    def _name_owned(self) -> bool:
        async def probe():
            from tools.host_mcp_adapter.bus_client import BrokerClient

            client = BrokerClient(self.bus_address)
            try:
                owned, _ = await client.broker_name_state(timeout=5)
                return owned
            finally:
                await client.aclose()

        return asyncio.run(probe())

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.process = None
        if getattr(self, "log", None) is not None:
            self.log.close()
            self.log = None


class _FakeBroker:
    """In-process jeepney fake owning the broker name, for faults the real
    broker cannot be made to produce deterministically."""

    def __init__(self, address: str, behavior: str) -> None:
        self.address = address
        self.behavior = behavior
        self.seen = 0
        self._task = None
        self._ready = None

    async def __aenter__(self):
        self._ready = asyncio.Event()
        self._task = asyncio.create_task(self._serve())
        await asyncio.wait_for(self._ready.wait(), 10)
        return self

    async def __aexit__(self, *exc):
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def _serve(self):
        from jeepney import DBusAddress, MatchRule, new_error, new_method_call, new_method_return
        from jeepney.io.asyncio import DBusRouter, open_dbus_connection

        conn = await open_dbus_connection(self.address)
        router = DBusRouter(conn)
        bus = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus", interface="org.freedesktop.DBus")
        try:
            with router.filter(MatchRule(type="method_call"), bufsize=100) as queue:
                await router.send_and_get_reply(new_method_call(bus, "RequestName", "su", (contract.BUS_NAME, 4)))
                self._ready.set()
                while True:
                    msg = await queue.get()
                    self.seen += 1
                    behavior = self.behavior
                    if behavior == "stall_first":
                        behavior = "stall" if self.seen == 1 else "ok"
                    if behavior == "stall":
                        continue
                    if behavior == "ok":
                        reply = new_method_return(msg, "ss", ("1", '{"schema":"fake"}'))
                    elif behavior == "bad_signature":
                        reply = new_method_return(msg, "i", (5,))
                    elif behavior == "bad_json":
                        reply = new_method_return(msg, "ss", ("1", "not json"))
                    elif behavior == "unknown_error":
                        reply = new_error(msg, f"{contract.ERROR_NAMESPACE}.Bogus", "s", ("/etc/secret token=abc",))
                    elif behavior == "unknown_method":
                        reply = new_error(msg, "org.freedesktop.DBus.Error.UnknownMethod", "s", ("no such method",))
                    else:
                        raise AssertionError(behavior)
                    await router.send(reply)
        finally:
            await router.__aexit__(None, None, None)
            await conn.close()


@requires_private_bus
class PrivateBusTests(unittest.TestCase):
    """Gates 6 and 29/31 (adapter side) on a private dbus-daemon with the
    REAL landed HostBroker1 service (permissive bus policy, as T-2912)."""

    @classmethod
    def setUpClass(cls) -> None:
        import tools._host_control_testbus as testbus

        cls.testbus = testbus
        cls.handle = testbus.spawn_daemon("t2915-", _PERMISSIVE_BUS_CONFIG)
        cls.address = cls.handle.bus_address
        cls.work_dir = tempfile.TemporaryDirectory(**_private_bus_work_dir_args())
        cls.work = Path(cls.work_dir.name)
        cls.broker = _Broker(cls.address, cls.work)
        cls.broker.start(testbus, cls.handle)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.broker.stop()
        cls.testbus.stop_daemon(cls.handle)
        cls.work_dir.cleanup()

    def _ensure_broker(self) -> None:
        if self.broker.process is None or self.broker.process.poll() is not None:
            self.broker.start(self.testbus, self.handle)

    def _adapter(self):
        from tools.host_mcp_adapter.bus_client import BrokerClient
        from tools.host_mcp_adapter.server import HostAdapter

        return HostAdapter(BrokerClient(self.address), contract.RateLimiter(burst=1000, per_second=1000))

    @staticmethod
    def _payload(result) -> dict:
        return json.loads(result.content[0].text)

    @staticmethod
    async def _once(adapter, name, args):
        try:
            return await adapter.call_tool(name, args)
        finally:
            await adapter._client.aclose()

    def test_all_seven_tools_route_one_to_one_to_the_real_broker(self) -> None:
        self._ensure_broker()
        adapter = self._adapter()
        state = self.broker.state

        async def body():
            out = {}
            out["cap"] = await adapter.call_tool("host.capabilities", {})
            out["plan"] = await adapter.call_tool(
                "host.plan", {"seat_id": "recovery", "manifest_revision": "manifest-r1", "idempotency_key": "t2915-route"}
            )
            out["plan_again"] = await adapter.call_tool(
                "host.plan", {"seat_id": "recovery", "manifest_revision": "manifest-r1", "idempotency_key": "t2915-route"}
            )
            out["conflict"] = await adapter.call_tool(
                "host.plan", {"seat_id": "recovery", "manifest_revision": "manifest-r2", "idempotency_key": "t2915-route"}
            )
            plan = out["plan"].structured_content
            out["apply"] = await adapter.call_tool(
                "host.apply",
                {
                    "plan_handle": plan["plan_handle"],
                    "plan_digest": plan["plan_digest"],
                    "idempotency_key": "t2915-apply",
                    "approval_handle": "af" * 32,
                },
            )
            if not out["apply"].is_error:
                out["op"] = await adapter.call_tool(
                    "host.operation.get", {"operation_handle": out["apply"].structured_content["operation_handle"]}
                )
            out["verify"] = await adapter.call_tool(
                "host.verify", {"seat_id": "recovery", "plan_handle": plan["plan_handle"], "idempotency_key": "t2915-v"}
            )
            out["rollback"] = await adapter.call_tool(
                "host.rollback",
                {
                    "receipt_handle": state["subject_complete_receipt_handle"],
                    "idempotency_key": "t2915-rb",
                    "approval_handle": "af" * 32,
                },
            )
            out["approval"] = await adapter.call_tool("host.approval.get", {"approval_handle": "af" * 32})
            out["stolen"] = await adapter.call_tool(
                "host.operation.get", {"operation_handle": state["seat_b_operation_handle"]}
            )
            await adapter._client.aclose()
            return out

        out = asyncio.run(body())
        cap = out["cap"]
        self.assertFalse(cap.is_error, cap.content[0].text)
        self.assertEqual(cap.structured_content["capabilities"]["schema"], "pantheon-host-capabilities/v1")
        self.assertEqual(json.loads(cap.content[0].text), cap.structured_content)

        plan = out["plan"]
        self.assertFalse(plan.is_error, plan.content[0].text)
        self.assertEqual(plan.structured_content["summary"]["schema"], "pantheon-host-plan-summary/v1")
        self.assertEqual(out["plan_again"].structured_content, plan.structured_content)
        self.assertEqual(self._payload(out["conflict"])["error"]["code"], "idempotency_conflict")

        apply = out["apply"]
        self.assertFalse(apply.is_error, apply.content[0].text)
        self.assertRegex(apply.structured_content["operation_handle"], contract.HEX64_PATTERN)
        self.assertFalse(out["op"].is_error, out["op"].content[0].text)
        self.assertIsInstance(out["op"].structured_content["result"], dict)
        self.assertEqual(out["op"].structured_content["state"], "prepared")

        # Verify is exposed-but-refusing until T-2917 5b wires it
        self.assertEqual(self._payload(out["verify"])["error"]["code"], "unsupported")
        rollback = out["rollback"]
        self.assertFalse(rollback.is_error, rollback.content[0].text)
        # this test driver wires the fail-closed UnavailableApprovalStatus;
        # the real Consumer client landed in T-2917 5a (approval_client.py)
        self.assertEqual(self._payload(out["approval"])["error"]["code"], "approval_unavailable")
        stolen = self._payload(out["stolen"])["error"]
        self.assertIn(stolen["code"], {"unknown_handle", "subject_mismatch"})
        self.assertNotIn("bravo", out["stolen"].content[0].text)

    def test_broker_absent_then_started_then_restarted_same_client_recovers(self) -> None:
        """Gate 6 (host-adapter half): unavailable while absent, success
        after the broker appears, and again after a restart with a NEW
        unique name — one client, no restart."""

        adapter = self._adapter()
        self.broker.stop()
        broker, testbus, handle = self.broker, self.testbus, self.handle

        async def body():
            # one event loop, one client: exactly the production shape
            started = time.monotonic()
            absent = await adapter.call_tool("host.capabilities", {})
            elapsed = time.monotonic() - started
            await asyncio.to_thread(broker.start, testbus, handle)
            present = await adapter.call_tool("host.capabilities", {})
            await asyncio.to_thread(broker.stop)
            down_again = await adapter.call_tool("host.capabilities", {})
            await asyncio.to_thread(broker.start, testbus, handle)
            again = await adapter.call_tool("host.capabilities", {})
            await adapter._client.aclose()
            return absent, elapsed, present, down_again, again

        absent, elapsed, present, down_again, again = asyncio.run(body())
        self.assertLess(elapsed, contract.CONNECT_TIMEOUT + 3.0)
        self.assertEqual(self._payload(absent)["error"]["code"], "broker_unavailable")
        self.assertFalse(present.is_error, present.content[0].text)
        # broker gone mid-session: the bus answers ServiceUnknown (not delivered)
        self.assertEqual(self._payload(down_again)["error"]["code"], "broker_unavailable")
        self.assertFalse(again.is_error, again.content[0].text)

    def test_one_live_stdio_adapter_survives_broker_outage_and_repair(self) -> None:
        """Gate 6 end-to-end through the official SDK client: the SAME
        adapter process lists tools with the broker absent, returns the
        bounded unavailable error, then succeeds after repair."""

        from mcp import Client

        self.broker.stop()
        broker, testbus, handle = self.broker, self.testbus, self.handle

        async def body():
            async with Client(_driver_params(self.address), mode="2026-07-28") as client:
                names = [t.name for t in (await client.list_tools()).tools]
                down = await client.call_tool("host.capabilities", {})
                await asyncio.to_thread(broker.start, testbus, handle)
                up = await client.call_tool("host.capabilities", {})
                return names, down, up

        names, down, up = asyncio.run(body())
        self.assertEqual(names, [t[0] for t in EXPECTED_TOOLS])
        self.assertEqual(self._payload(down)["error"]["code"], "broker_unavailable")
        self.assertFalse(up.is_error, up.content[0].text)
        self.assertIn("contract_version", up.structured_content)

    def test_disconnect_after_submit_replays_the_original_plan(self) -> None:
        """Gate 29 (adapter side): a submit whose reply is never read,
        followed by a reconnect and a retry with the SAME key, returns the
        same server-owned plan — the adapter never mints keys or retries."""

        from jeepney import new_method_call

        from tools.host_mcp_adapter import bus_client
        from tools.host_mcp_adapter.bus_client import BrokerClient

        self._ensure_broker()
        client = BrokerClient(self.address)
        args = ("recovery", "manifest-r1", "t2915-disconnect")

        async def body():
            await client.call(contract.Method.CAPABILITIES, (), timeout=10)
            await client._router.send(new_method_call(bus_client._BROKER, "Plan", "sss", args))
            await client._drop()
            first = await client.call(contract.Method.PLAN, args, timeout=10)
            second = await client.call(contract.Method.PLAN, args, timeout=10)
            await client.aclose()
            return first, second

        first, second = asyncio.run(body())
        self.assertEqual(first, second)

    def test_unregistered_subject_is_policy_denied(self) -> None:
        self.broker.stop()
        unregistered = _Broker(self.address, self.work, register_subject=False)
        unregistered.start(self.testbus, self.handle)
        try:
            result = asyncio.run(self._once(self._adapter(), "host.capabilities", {}))
        finally:
            unregistered.stop()
        error = self._payload(result)["error"]
        self.assertEqual(error["code"], "policy_denied")
        self.assertEqual(error["detail"], "subject_denied")

    def test_self_check_against_private_bus(self) -> None:
        from tools.host_mcp_adapter import server

        self.broker.stop()
        absent = contract.self_check_report(asyncio.run(server.self_check_components(self.address, probe_bus=True)))
        self.assertEqual(absent["Subcomponents"]["system_bus"], "ready")
        self.assertEqual(absent["Subcomponents"]["broker"], "degraded")
        self.assertIn("not activatable", absent["Reasons"]["broker"])
        self.broker.start(self.testbus, self.handle)
        present = contract.self_check_report(asyncio.run(server.self_check_components(self.address, probe_bus=True)))
        self.assertEqual(present["Status"], "ready")

    # ---------------- fault fakes (broker stopped for these) ----------------

    def _with_fake(self, behavior: str, body):
        self.broker.stop()

        async def run():
            async with _FakeBroker(self.address, behavior) as fake:
                return await body(fake)

        return asyncio.run(run())

    def test_no_reply_is_outcome_unknown_and_drops_the_link(self) -> None:
        from tools.host_mcp_adapter.bus_client import BrokerClient

        client = BrokerClient(self.address)

        async def body(fake):
            with self.assertRaises(contract.AdapterError) as ctx:
                await client.call(contract.Method.CAPABILITIES, (), timeout=0.5)
            connected_after = client.connected
            await client.aclose()
            return ctx.exception.code, connected_after

        code, connected_after = self._with_fake("stall", body)
        self.assertEqual(code, "broker_no_reply")
        self.assertFalse(connected_after)
        self.assertIn("SAME idempotency_key", contract.ERROR_CODES[code].message)

    def test_client_cancellation_abandons_the_wait_only(self) -> None:
        """Gate 31 (adapter side): cancelling the wait does not tear down
        the link or send anything; the next call succeeds."""

        from tools.host_mcp_adapter.bus_client import BrokerClient

        client = BrokerClient(self.address)

        async def body(fake):
            task = asyncio.create_task(client.call(contract.Method.CAPABILITIES, (), timeout=10))
            while fake.seen < 1:
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            still_connected = client.connected
            result = await client.call(contract.Method.CAPABILITIES, (), timeout=10)
            await client.aclose()
            return still_connected, result, fake.seen

        still_connected, result, seen = self._with_fake("stall_first", body)
        self.assertTrue(still_connected)
        self.assertEqual(result, ("1", '{"schema":"fake"}'))
        self.assertEqual(seen, 2)

    def test_malformed_replies_are_contract_mismatch_without_echo(self) -> None:
        for behavior in ("bad_signature", "bad_json", "unknown_error", "unknown_method"):
            adapter = self._adapter()

            async def body(fake, adapter=adapter):
                return await self._once(adapter, "host.capabilities", {})

            result = self._with_fake(behavior, body)
            payload = self._payload(result)
            self.assertEqual(payload["error"]["code"], "broker_contract_mismatch", behavior)
            self.assertNotIn("secret", result.content[0].text)
            self.assertNotIn("detail", payload["error"])


# ====================================================================
# Tier 4 — packaging against a fixture root (gate 7, fixture form)
# ====================================================================


@requires_wheelhouse
class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import verify_artifact

        cls.verify_artifact = verify_artifact
        cls.tmp = tempfile.TemporaryDirectory(prefix="t2915-pkg-")
        cls.root = Path(cls.tmp.name) / "root"
        cls.manifest = build_runtime.build(cls.root, Path(WHEELHOUSE), python=BUILD_PYTHON)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_fixture_root_verifies_every_check(self) -> None:
        report = self.verify_artifact.verify(self.root)
        self.assertTrue(report["ok"], json.dumps(report, indent=1))
        self.assertEqual(report["checks"]["dist_set"]["count"], 29)
        self.assertEqual(report["checks"]["dist_metadata"], {"ok": True, "missing_metadata": []})
        self.assertEqual(report["checks"]["isolated_import"]["detail"], "mcp==2.3.0,jeepney==0.9.0")
        # a re-verify after the launcher ran proves nothing was written at run time
        self.assertTrue(self.verify_artifact.verify(self.root)["ok"])

    def test_owner_check_is_live(self) -> None:
        if os.getuid() == 0:
            self.skipTest("owner check falsification needs a non-root runner")
        report = self.verify_artifact.verify(self.root, expect_owner=0)
        self.assertFalse(report["checks"]["owner"]["ok"])

    def test_packaged_app_is_exactly_the_production_modules(self) -> None:
        app = self.root / build_runtime.PREFIX / "app" / "tools" / "host_mcp_adapter"
        self.assertEqual({p.name for p in app.glob("*.py")}, set(build_runtime.PACKAGED_MODULES))
        self.assertFalse(list((self.root / build_runtime.PREFIX).rglob("test_host_mcp_adapter_driver.py")))
        venv_bin = {p.name for p in (self.root / build_runtime.PREFIX / "venv" / "bin").iterdir()}
        self.assertTrue(all(name.startswith("python") for name in venv_bin), venv_bin)
        launcher = (self.root / build_runtime.LAUNCHER).read_text(encoding="utf-8")
        self.assertTrue(launcher.startswith("#!/usr/local/lib/pantheon/host-mcp-adapter/venv/bin/python3 -I\n"))

    def test_tampered_artifact_fails_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t2915-tamper-") as tmp:
            root = Path(tmp) / "root"
            build_runtime.build(root, Path(WHEELHOUSE), python=BUILD_PYTHON)
            target = root / build_runtime.PREFIX / "app" / "tools" / "host_mcp_adapter" / "contract.py"
            os.chmod(target, 0o644)
            target.write_text(target.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
            report = self.verify_artifact.verify(root)
            self.assertFalse(report["checks"]["manifest"]["ok"])
            self.assertIn(
                (build_runtime.PREFIX / "app/tools/host_mcp_adapter/contract.py").as_posix(),
                report["checks"]["manifest"]["mismatched"],
            )

    def test_build_refuses_a_wheel_that_does_not_match_the_lock_hash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t2915-badwheel-") as tmp:
            wheelhouse = Path(tmp) / "wheels"
            shutil.copytree(WHEELHOUSE, wheelhouse)
            victim = next(wheelhouse.glob("jeepney-*.whl"))
            with victim.open("ab") as handle:
                handle.write(b"\0")
            with self.assertRaises(build_runtime.BuildError) as ctx:
                build_runtime.build(Path(tmp) / "root", wheelhouse, python=BUILD_PYTHON)
            self.assertIn("hash", str(ctx.exception).lower())

    def test_build_refuses_to_overwrite_an_existing_artifact(self) -> None:
        with self.assertRaises(build_runtime.BuildError):
            build_runtime.build(self.root, Path(WHEELHOUSE), python=BUILD_PYTHON)


if __name__ == "__main__":
    unittest.main()
