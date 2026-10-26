"""Tests for the privilege-free stdio approver MCP adapter (ORG-052 Unit 4b, T-2916).

Mirrors ``tools/test_host_mcp_adapter.py`` (T-2915). Tiers, each with an
EXPLICIT skip reason when its prerequisites are absent (never silent):

1. Pure contract / landed-parity / structural / shared-generic-parity /
   build-profile tests - run everywhere (system python3; the landed-parity
   tests additionally need ``tools.host_control`` importable and the
   signature-parity test needs dbus-python).
2. Runtime tests (SDK stdio, both protocol eras) - need the pinned runtime
   (mcp 2.x + jeepney), i.e. the fixture venv built by
   ``tools/host_mcp_adapter/dist/build_runtime.py``. CI pins mcp 1.x, so
   they skip there.
3. Private-bus tests - additionally need ``dbus-daemon`` and a system
   python3 with dbus-python, PyGObject and PyYAML: the REAL landed
   HostBroker1 driver (real planner summaries, real metadata) and the REAL
   landed HostApproval1 driver run on one private bus with a permissive
   bus config, so every refusal observed is the service's own server-side
   authorization (gate 10 posture). The fault matrix runs the host and
   approver bus clients through identical jeepney fault fakes.
4. Packaging - needs a hash-verified wheelhouse named by
   ``T2916_WHEELHOUSE`` (or ``T2915_WHEELHOUSE``).

Local run (fixture venv python, from the repo root)::

    T2916_WHEELHOUSE=<wheels> timeout 900 taskset -c 3 \\
        <fixture>/usr/local/lib/pantheon/host-mcp-adapter/venv/bin/python3 \\
        -m unittest tools.test_approval_mcp_adapter -v
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.metadata
import importlib.util
import inspect
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

from tools.approval_mcp_adapter import contract  # noqa: E402
from tools.host_mcp_adapter import contract as host_contract  # noqa: E402

PACKAGE_DIR = REPO_ROOT / "tools" / "approval_mcp_adapter"
HOST_DIST_DIR = REPO_ROOT / "tools" / "host_mcp_adapter" / "dist"
if str(HOST_DIST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIST_DIR))
import build_runtime  # noqa: E402

# T-4858 test-only knobs, defaults unchanged: the interpreter that runs the landed broker and
# approval drivers (needs dbus-python, PyGObject, PyYAML) and the one the packaging tier builds
# the runtime venv from (must be the lock's CPython 3.12). CI's mcp-adapter-runtime job sets both.
SYSTEM_PYTHON = os.environ.get("PANTHEON_BROKER_PYTHON") or "/usr/bin/python3"
BUILD_PYTHON = os.environ.get("T2915_BUILD_PYTHON") or "/usr/bin/python3"
DBUS_DAEMON = shutil.which("dbus-daemon")
NO_BUS = "unix:path=/nonexistent-t2916/bus.sock"
HEX64 = re.compile(r"^[0-9a-f]{64}$")

EXPECTED_TOOLS = (
    ("approval.review", "Review", "sss", "ssss"),
    ("approval.review.get", "ReviewGet", "s", "ss"),
    ("approval.decide", "Decide", "ssssss", "ss"),
    ("approval.request_human", "RequestHuman", "sss", "ss"),
)

# The T-2915 build_runtime.py blob, i.e. the dist script BEFORE the
# T-2916 AdapterProfile refactor. The equivalence test rebuilds the host
# artifact with it and with the refactored host profile and requires
# byte-identical trees (HO condition (c) on T-2916).
PRE_REFACTOR_BUILD_RUNTIME_BLOB = "5c27072669e171f711d9d9e9a11bac5d9dbd23f0"
# sha256 of the T-2915 host launcher template, frozen.
HOST_LAUNCHER_TEXT_SHA256 = "a29337464148abd481e29b62eba4cc6a795480a0f969bd6e49c07eb60b9bc069"


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
            "T-2916 runtime tests need the pinned mcp 2.x runtime "
            f"(this interpreter has mcp {mcp_version or 'absent'}); run them with the "
            "fixture venv from tools/host_mcp_adapter/dist/build_runtime.py - "
            "in CI the hash-locked mcp-adapter-runtime workflow (T-4858) runs them"
        )
    if _version("jeepney") is None:
        return "T-2916 runtime tests need jeepney (part of the pinned adapter runtime)"
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


FAULT_BUS_SKIP = RUNTIME_SKIP or (None if DBUS_DAEMON else "fault-matrix tests need dbus-daemon on PATH")
requires_fault_bus = unittest.skipIf(FAULT_BUS_SKIP is not None, FAULT_BUS_SKIP or "")

PRIVATE_BUS_SKIP = FAULT_BUS_SKIP
if PRIVATE_BUS_SKIP is None and not _system_python_has_dbus():
    PRIVATE_BUS_SKIP = (
        f"private-bus tests need {SYSTEM_PYTHON} (PANTHEON_BROKER_PYTHON) with dbus-python, "
        "PyGObject and PyYAML for the landed broker and approval drivers"
    )
requires_private_bus = unittest.skipIf(PRIVATE_BUS_SKIP is not None, PRIVATE_BUS_SKIP or "")

WHEELHOUSE = os.environ.get("T2916_WHEELHOUSE") or os.environ.get("T2915_WHEELHOUSE", "")
requires_wheelhouse = unittest.skipUnless(
    WHEELHOUSE and Path(WHEELHOUSE).is_dir(),
    "packaging tests need T2916_WHEELHOUSE (or T2915_WHEELHOUSE) pointing at a hash-verified "
    "wheelhouse (pip download --require-hashes -r tools/host_mcp_adapter/dist/requirements.lock)",
)


def _approval_control_importable() -> str | None:
    try:
        import tools.host_control.approval.wire  # noqa: F401
        import tools.host_control.approval.service  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return f"parity tests need the landed tools.host_control.approval importable here ({type(exc).__name__})"
    return None


APPROVAL_CONTROL_SKIP = _approval_control_importable()
requires_approval_control = unittest.skipIf(APPROVAL_CONTROL_SKIP is not None, APPROVAL_CONTROL_SKIP or "")


# ====================================================================
# Tier 1 - pure
# ====================================================================


class SkipGuardTests(unittest.TestCase):
    """The runtime skip fires ONLY when the pinned runtime is absent."""

    @staticmethod
    def _versions(mapping: dict[str, str]):
        def version_of(dist: str) -> str:
            if dist in mapping:
                return mapping[dist]
            raise importlib.metadata.PackageNotFoundError(dist)

        return version_of

    def test_guard_does_not_fire_with_mcp2_and_jeepney(self) -> None:
        self.assertIsNone(runtime_skip_reason(self._versions({"mcp": "2.3.0", "jeepney": "0.9.0"})))

    def test_guard_fires_with_explicit_reason(self) -> None:
        for mapping in ({"mcp": "1.30.0", "jeepney": "0.9.0"}, {"jeepney": "0.9.0"}, {"mcp": "2.3.0"}):
            reason = runtime_skip_reason(self._versions(mapping))
            self.assertIsNotNone(reason)
            self.assertIn("T-2916", reason)

    def test_guard_agrees_with_this_interpreter(self) -> None:
        try:
            importlib.import_module("jeepney")
            importable = importlib.metadata.version("mcp").startswith("2.")
        except Exception:  # noqa: BLE001
            importable = False
        self.assertEqual(RUNTIME_SKIP is None, importable)


class ToolTableTests(unittest.TestCase):
    def test_exactly_four_tools_in_ratified_order(self) -> None:
        self.assertEqual(
            [(t.name, t.method.member, t.method.in_signature, t.method.out_signature) for t in contract.TOOLS],
            list(EXPECTED_TOOLS),
        )
        self.assertEqual(len(contract.TOOLS_BY_NAME), 4)

    def test_method_enum_is_exactly_the_reviewer_four(self) -> None:
        self.assertEqual(
            [(m.member, m.in_signature, m.out_signature) for m in contract.Method],
            [t[1:] for t in EXPECTED_TOOLS],
        )
        self.assertEqual({t.method for t in contract.TOOLS}, set(contract.Method))

    def test_params_and_outputs_match_signature_arity(self) -> None:
        for tool in contract.TOOLS:
            self.assertEqual(len(tool.params), len(tool.method.in_signature), tool.name)
            self.assertEqual(len(tool.outputs), len(tool.method.out_signature), tool.name)

    def test_param_names_follow_adr_section_5(self) -> None:
        self.assertEqual(
            {t.name: [p for p, _ in t.params] for t in contract.TOOLS},
            {
                "approval.review": ["plan_handle", "expected_digest", "request_plan_handle"],
                "approval.review.get": ["review_handle"],
                "approval.decide": [
                    "review_handle", "expected_digest", "decision",
                    "reason_code", "audit_anchor", "idempotency_key",
                ],
                "approval.request_human": ["review_handle", "expected_digest", "idempotency_key"],
            },
        )

    def test_no_identity_authority_or_passthrough_parameters(self) -> None:
        forbidden = re.compile(
            r"uid|user|persona|role|approver|requester|performer|subject|seat|policy|"
            r"authority|grant|token|bus|address|path|command|argv|shell|env|executor",
            re.I,
        )
        for tool in contract.TOOLS:
            for name, _ in tool.params:
                if name in ("plan_handle", "request_plan_handle"):
                    continue
                self.assertIsNone(forbidden.search(name), f"{tool.name}.{name}")

    def test_annotations_are_hints_only(self) -> None:
        self.assertEqual(contract.annotations(contract.TOOLS_BY_NAME["approval.review.get"]), {"readOnlyHint": True, "openWorldHint": False})
        decide = contract.annotations(contract.TOOLS_BY_NAME["approval.decide"])
        self.assertTrue(decide["destructiveHint"])
        self.assertTrue(decide["idempotentHint"])
        for tool in contract.TOOLS:
            self.assertFalse(contract.annotations(tool)["openWorldHint"])

    def test_timeouts_are_bounded(self) -> None:
        for tool in contract.TOOLS:
            self.assertLessEqual(tool.timeout, contract.WRITE_CALL_TIMEOUT)
        self.assertEqual(contract.CONNECT_TIMEOUT, 2.0)


@requires_approval_control
class LandedParityTests(unittest.TestCase):
    """The mirrored constants and bounds equal the landed approval service."""

    def test_wire_constants_match(self) -> None:
        from tools.host_control.approval import wire

        self.assertEqual(contract.BUS_NAME, wire.BUS_NAME)
        self.assertEqual(contract.OBJECT_PATH, wire.OBJECT_PATH)
        self.assertEqual(contract.REVIEWER_INTERFACE, wire.REVIEWER_INTERFACE)
        self.assertEqual(contract.ERROR_NAMESPACE, wire.ERROR_NAMESPACE)

    def test_error_mapping_covers_exactly_the_closed_namespace(self) -> None:
        from tools.host_control.approval import wire

        self.assertEqual(set(contract.WIRE_ERROR_CODES), set(wire.CLOSED_ERROR_NAMES))

    def test_decision_and_reason_sets_match_the_service(self) -> None:
        from tools.host_control.approval.contracts import DECIDE_REASON_CODES
        from tools.host_control.approval.service import ApprovalService

        self.assertEqual(set(contract.REASON_CODES), set(DECIDE_REASON_CODES))
        source = inspect.getsource(ApprovalService.decide)
        self.assertIn('if decision not in ("granted", "denied"):', source)
        self.assertEqual(contract.DECISIONS, ("granted", "denied"))

    def test_adapter_bounds_are_no_looser_than_the_service(self) -> None:
        from tools.host_control.contracts import _closed_hex, closed_record_string

        samples_ok = ["a" * 64, "0123456789abcdef" * 4]
        samples_bad = ["A" * 64, "a" * 63, "g" * 64, "", "a" * 65]
        for value in samples_ok:
            self.assertTrue(contract._matches("hex64", value))
            _closed_hex(value, "x")
        for value in samples_bad:
            self.assertFalse(contract._matches("hex64", value))
        # every value the adapter accepts for a record string, the service accepts
        for kind, maximum, values in (
            ("scalar", 128, ["k", "x" * 128, "T-2916:decide#1"]),
            ("anchor", 256, ["ticket T-2916", "a" * 256, " leading space"]),
        ):
            for value in values:
                self.assertTrue(contract._matches(kind, value), (kind, value))
                closed_record_string(value, "x", maximum=maximum)
            self.assertFalse(contract._matches(kind, "x" * (maximum + 1)))
            self.assertFalse(contract._matches(kind, ""))
        # the empty-or-hex slot: "" is the wire's direct-review marker
        self.assertTrue(contract._matches("hex64_or_empty", ""))
        self.assertTrue(contract._matches("hex64_or_empty", "b" * 64))
        self.assertFalse(contract._matches("hex64_or_empty", "b" * 63))

    def test_method_signatures_and_arg_names_match_landed_dbus_service(self) -> None:
        try:
            from tools.host_control.approval import dbus_service
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"dbus_service not importable here ({type(exc).__name__})")
        if not dbus_service.DBUS_AVAILABLE:
            self.skipTest("signature parity needs dbus-python in this interpreter")
        cls = dbus_service.HostApproval1Object
        for tool in contract.TOOLS:
            func = getattr(cls, tool.method.member)
            self.assertEqual(func._dbus_interface, contract.REVIEWER_INTERFACE, tool.name)
            self.assertEqual(func._dbus_in_signature, tool.method.in_signature, tool.name)
            self.assertEqual(func._dbus_out_signature, tool.method.out_signature, tool.name)
            # T-5518: dbus-python's deferred-reply callbacks (RequestHuman)
            # are transport plumbing, not wire arguments
            plumbing = {"self", "sender", *(getattr(func, "_dbus_async_callbacks", None) or ())}
            args = [a for a in inspect.signature(func).parameters if a not in plumbing]
            self.assertEqual(args, [p for p, _ in tool.params], tool.name)
            self.assertEqual(list(func._dbus_args), [p for p, _ in tool.params], tool.name)


class SchemaTests(unittest.TestCase):
    def test_input_schemas_are_closed_and_fully_required(self) -> None:
        for tool in contract.TOOLS:
            schema = contract.input_schema(tool)
            self.assertEqual(schema["$schema"], contract.JSON_SCHEMA_DIALECT)
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(schema["required"], [p for p, _ in tool.params])
            self.assertEqual(list(schema["properties"]), [p for p, _ in tool.params])

    def test_output_schemas_are_closed_objects(self) -> None:
        for tool in contract.TOOLS:
            schema = contract.output_schema(tool)
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(schema["required"], [p for p, _ in tool.outputs])

    def test_enums_are_closed(self) -> None:
        props = contract.input_schema(contract.TOOLS_BY_NAME["approval.decide"])["properties"]
        self.assertEqual(props["decision"], {"type": "string", "enum": ["granted", "denied"]})
        self.assertEqual(props["reason_code"], {"type": "string", "enum": ["approved", "rejected"]})

    def test_schemas_meta_validate_and_agree_with_adapter_validator(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("schema meta-validation needs jsonschema (in the pinned runtime)")
        good = {
            "approval.review": {"plan_handle": "a" * 64, "expected_digest": "b" * 64, "request_plan_handle": ""},
            "approval.review.get": {"review_handle": "c" * 64},
            "approval.decide": {
                "review_handle": "c" * 64, "expected_digest": "b" * 64, "decision": "granted",
                "reason_code": "approved", "audit_anchor": "ticket T-2916", "idempotency_key": "k1",
            },
            "approval.request_human": {"review_handle": "c" * 64, "expected_digest": "b" * 64, "idempotency_key": "k"},
        }
        bad_variants = [
            ("approval.review", {"request_plan_handle": "x"}),
            ("approval.review", {"uid": "0"}),
            ("approval.decide", {"decision": "approve"}),
            ("approval.decide", {"reason_code": "admin"}),
            ("approval.decide", {"audit_anchor": ""}),
            ("approval.decide", {"idempotency_key": "has space"}),
            ("approval.review.get", {"review_handle": "../etc"}),
        ]
        for tool in contract.TOOLS:
            for schema in (contract.input_schema(tool), contract.output_schema(tool)):
                jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.validate(good[tool.name], contract.input_schema(tool))
            self.assertEqual(len(contract.validate_arguments(tool, good[tool.name])), len(tool.params))
        for name, patch in bad_variants:
            tool = contract.TOOLS_BY_NAME[name]
            args = dict(good[name], **patch)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(args, contract.input_schema(tool))
            with self.assertRaises(contract.AdapterError):
                contract.validate_arguments(tool, args)


class ErrorMappingTests(unittest.TestCase):
    def test_every_closed_wire_name_maps_to_a_distinct_code(self) -> None:
        codes = list(contract.WIRE_ERROR_CODES.values())
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(len(codes), 12)
        for code in codes:
            self.assertIn(code, contract.ERROR_CODES)

    def test_closed_names_carry_sanitized_bounded_detail(self) -> None:
        error = contract.error_for_dbus_name(f"{contract.ERROR_NAMESPACE}.PolicyDenied", ("subject_denied\x00\n" + "x" * 500,))
        self.assertEqual(error.code, "policy_denied")
        self.assertTrue(error.detail.startswith("subject_denied"))
        self.assertLessEqual(len(error.detail), contract.DETAIL_LIMIT)

    def test_bus_errors_are_classified(self) -> None:
        cases = {
            "org.freedesktop.DBus.Error.ServiceUnknown": "authority_unavailable",
            "org.freedesktop.DBus.Error.NameHasNoOwner": "authority_unavailable",
            "org.freedesktop.DBus.Error.Spawn.ExecFailed": "authority_unavailable",
            "org.freedesktop.DBus.Error.NoReply": "authority_no_reply",
            "org.freedesktop.DBus.Error.AccessDenied": "bus_access_denied",
            "org.pantheon.HostBroker1.Error.PolicyDenied": "authority_contract_mismatch",
            "org.freedesktop.DBus.Error.UnknownMethod": "authority_contract_mismatch",
            None: "authority_contract_mismatch",
        }
        for name, code in cases.items():
            self.assertEqual(contract.error_for_dbus_name(name, ("secret",)).code, code, name)

    def test_unclosed_names_never_echo_the_remote_message(self) -> None:
        for name in ("org.freedesktop.DBus.Error.AccessDenied", f"{contract.ERROR_NAMESPACE}.Bogus", "x.y"):
            self.assertEqual(contract.error_for_dbus_name(name, ("/etc/secret token",)).detail, "")

    def test_shape_result_is_strict(self) -> None:
        review = contract.TOOLS_BY_NAME["approval.review"]
        ok = ("a" * 64, "open", "b" * 64, '{"schema":"pantheon-host-plan-summary/v1"}')
        self.assertEqual(contract.shape_result(review, ok)["summary"]["schema"], "pantheon-host-plan-summary/v1")
        for body in (
            ok[:3],
            ("A" * 64, *ok[1:]),
            (*ok[:3], "redacted-effects:22222222"),
            (*ok[:3], "[]"),
            (*ok[:3], '{"a":1,"a":2}'),
            (*ok[:3], '{"a":NaN}'),
        ):
            with self.assertRaises(contract.AdapterError) as ctx:
                contract.shape_result(review, body)
            self.assertEqual(ctx.exception.code, "authority_contract_mismatch")

    def test_decide_returns_empty_approval_handle_only_as_the_wire_denial(self) -> None:
        decide = contract.TOOLS_BY_NAME["approval.decide"]
        self.assertEqual(contract.shape_result(decide, ("denied", "")), {"state": "denied", "approval_handle": ""})
        self.assertEqual(contract.shape_result(decide, ("granted", "c" * 64))["approval_handle"], "c" * 64)
        with self.assertRaises(contract.AdapterError):
            contract.shape_result(decide, ("granted", "short"))


class SharedGenericParityTests(unittest.TestCase):
    """The generic behaviour mirrored from the host adapter is identical,
    so two copies cannot drift (D-1: separate closed module sets per
    binary, kept in step by tests instead of a shared import)."""

    CORPUS = [
        "", "plain", "x" * 500, "tab\tnl\nnul\x00", "unicode é☃", 7, None,
        '{"a":1}', '{"a":1,"a":2}', '{"a":NaN}', "[1]", "nope", '{"n":' + "[" * 2000 + "]" * 2000 + "}",
        '{"big":"' + "x" * 70000 + '"}',
    ]

    def test_sanitize_and_canonical_text_identical(self) -> None:
        for value in self.CORPUS:
            self.assertEqual(contract.sanitize_detail(value), host_contract.sanitize_detail(value))
            self.assertEqual(contract.canonical_text({"v": str(value)}), host_contract.canonical_text({"v": str(value)}))

    def test_bounded_object_parse_accepts_and_rejects_identically(self) -> None:
        for value in self.CORPUS:
            outcomes = []
            for module in (contract, host_contract):
                try:
                    outcomes.append(("ok", module.parse_bounded_object(value)))
                except module.AdapterError:
                    outcomes.append(("rejected", None))
            self.assertEqual(outcomes[0], outcomes[1], repr(value)[:80])

    def test_rate_limiter_identical(self) -> None:
        for burst, rate, steps in ((2, 1.0, [0, 0, 0, 0.5, 1.0, 1.0, 3.0]), (20, 5.0, [0] * 25)):
            traces = []
            for module in (contract, host_contract):
                now = [0.0]
                limiter = module.RateLimiter(burst=burst, per_second=rate, clock=lambda: now[0])
                trace = []
                for step in steps:
                    now[0] += step
                    trace.append(limiter.allow())
                traces.append(trace)
            self.assertEqual(traces[0], traces[1])
        self.assertEqual((contract.RATE_BURST, contract.RATE_PER_SECOND), (host_contract.RATE_BURST, host_contract.RATE_PER_SECOND))

    def test_bus_fault_classification_sets_and_timeouts_identical(self) -> None:
        for name in ("UNAVAILABLE_BUS_ERRORS", "UNAVAILABLE_BUS_ERROR_PREFIX", "NO_REPLY_BUS_ERRORS",
                     "ACCESS_DENIED_BUS_ERRORS", "SYSTEM_BUS_ADDRESS", "CONNECT_TIMEOUT",
                     "READ_CALL_TIMEOUT", "WRITE_CALL_TIMEOUT", "JSON_LIMIT", "DETAIL_LIMIT",
                     "SCALAR_PATTERN", "HEX64_PATTERN", "JSON_SCHEMA_DIALECT", "EXPECTED_RUNTIME",
                     "EXPECTED_PYTHON", "HEALTH_CONTRACT_REVISION"):
            self.assertEqual(getattr(contract, name), getattr(host_contract, name), name)

    def test_self_check_derivation_identical(self) -> None:
        statuses = ("ready", "degraded", "unhealthy", "not_configured")
        for a in statuses:
            for b in statuses:
                for c in statuses:
                    mine = contract.self_check_report(dict(zip(contract.SELF_CHECK_ROSTER, [(a, "", ""), (b, "", ""), (c, "", "")])))
                    theirs = host_contract.self_check_report(dict(zip(host_contract.SELF_CHECK_ROSTER, [(a, "", ""), (b, "", ""), (c, "", "")])))
                    self.assertEqual(mine["Status"], theirs["Status"])
        self.assertEqual(contract.SELF_CHECK_ROSTER, ("runtime", "system_bus", "approval_authority"))


class StructuralTests(unittest.TestCase):
    """Gate 18 (approver-adapter side): no listener/credential/journal/
    executor/root/mailbox/arbitrary-D-Bus surface, enforced over the
    packaged sources; and exactly one reachable interface."""

    ALLOWED_IMPORT_ROOTS = {
        "__future__", "argparse", "asyncio", "enum", "importlib", "json", "re",
        "sys", "time", "dataclasses", "typing", "anyio", "mcp", "mcp_types",
        "jeepney", "tools",
    }

    def _trees(self):
        for module in build_runtime.APPROVAL_PACKAGED_MODULES:
            path = PACKAGE_DIR / module
            yield module, path.read_text(encoding="utf-8"), ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_packaged_modules_are_exactly_the_production_set(self) -> None:
        self.assertEqual(
            set(build_runtime.APPROVAL_PACKAGED_MODULES),
            {"__init__.py", "contract.py", "bus_client.py", "server.py"},
        )
        on_disk = {p.name for p in PACKAGE_DIR.glob("*.py")}
        self.assertEqual(on_disk, set(build_runtime.APPROVAL_PACKAGED_MODULES))
        self.assertTrue((REPO_ROOT / "tools" / "test_approval_mcp_adapter_driver.py").exists())

    def test_import_closure_is_this_package_only(self) -> None:
        for module, _source, tree in self._trees():
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
                        self.assertTrue(name.startswith("tools.approval_mcp_adapter"), f"{module}: {name}")

    def test_jeepney_is_confined_to_bus_client(self) -> None:
        for module, _source, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    if any(n.split(".")[0] == "jeepney" for n in names):
                        self.assertEqual(module, "bus_client.py")

    def test_no_file_env_or_process_surface(self) -> None:
        for module, _source, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"open", "exec", "eval", "compile", "__import__"}, module)
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(
                        node.attr,
                        {"environ", "getenv", "system", "popen", "Popen", "bind", "listen", "accept", "write_text", "write_bytes"},
                        module,
                    )

    def test_bus_client_names_only_the_fixed_reviewer(self) -> None:
        source = (PACKAGE_DIR / "bus_client.py").read_text(encoding="utf-8")
        strings = {
            node.value for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(("org.", "/org"))
        }
        self.assertEqual(strings, {"/org/freedesktop/DBus", "org.freedesktop.DBus"})
        self.assertIn("_REVIEWER = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=REVIEWER_INTERFACE)", source)

    @staticmethod
    def _code_tokens(tree: ast.AST) -> set[str]:
        """Identifiers and non-docstring string literals (docstrings and
        comments may NAME other interfaces to say they are unreachable)."""

        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    docstrings.add(id(body[0].value))
        tokens = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                tokens.add(node.value)
            elif isinstance(node, ast.Name):
                tokens.add(node.id)
            elif isinstance(node, ast.Attribute):
                tokens.add(node.attr)
        return tokens

    def test_no_other_approval_interface_or_broker_is_reachable(self) -> None:
        for module, _source, tree in self._trees():
            joined = "\n".join(sorted(self._code_tokens(tree)))
            for needle in ("Consumer", "HumanIngress", "ValidateAndBind", "HumanDecision",
                           "HostBroker1", "HostExecutor", "Telegram"):
                self.assertFalse(needle in joined, f"{module}: code names {needle!r}")
            # credential words: string literals only (the rate limiter's
            # ``_tokens`` bucket attribute is not a credential)
            identifiers = {
                node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
            } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            literals = [token.lower() for token in self._code_tokens(tree) - identifiers]
            for literal in literals:
                for needle in ("bearer", "password", r"(?<!non-)secret", "credential", "api_key"):
                    self.assertIsNone(re.search(needle, literal), (module, needle, literal[:80]))

    def test_system_bus_address_is_fixed(self) -> None:
        self.assertEqual(contract.SYSTEM_BUS_ADDRESS, "unix:path=/run/dbus/system_bus_socket")
        for module, source, _tree in self._trees():
            self.assertNotIn("DBUS_SYSTEM_BUS_ADDRESS", source, module)
            self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", source, module)


class DistProfileTests(unittest.TestCase):
    """HO condition (c): the host profile's inputs are frozen to T-2915."""

    def test_host_profile_inputs_are_frozen_to_t2915(self) -> None:
        host = build_runtime.HOST_PROFILE
        self.assertEqual(host.prefix, Path("usr/local/lib/pantheon/host-mcp-adapter"))
        self.assertEqual(host.launcher, Path("usr/local/libexec/pantheon-host-mcp-adapter"))
        self.assertEqual(host.manifest_schema, "pantheon-host-mcp-adapter-build/v1")
        self.assertEqual(host.packaged_modules, ("__init__.py", "contract.py", "bus_client.py", "server.py"))
        self.assertEqual(host.package, "host_mcp_adapter")
        self.assertEqual(host.package_dir, REPO_ROOT / "tools" / "host_mcp_adapter")
        self.assertEqual(hashlib.sha256(host.launcher_text.encode()).hexdigest(), HOST_LAUNCHER_TEXT_SHA256)
        # the module-level names the 4a tests use still mean the host profile
        self.assertEqual(
            (build_runtime.PREFIX, build_runtime.LAUNCHER, build_runtime.PACKAGED_MODULES, build_runtime.MANIFEST_SCHEMA),
            (host.prefix, host.launcher, host.packaged_modules, host.manifest_schema),
        )

    def test_build_and_cli_default_to_the_host_profile(self) -> None:
        self.assertIs(inspect.signature(build_runtime.build).parameters["profile"].default, build_runtime.HOST_PROFILE)
        self.assertIs(inspect.signature(build_runtime.manifest_entries).parameters["profile"].default, build_runtime.HOST_PROFILE)
        source = inspect.getsource(build_runtime.main)
        self.assertIn("default=HOST_PROFILE.name", source)

    def test_approval_profile_shape(self) -> None:
        approval = build_runtime.APPROVAL_PROFILE
        self.assertEqual(approval.launcher, Path("usr/local/libexec/pantheon-approval-mcp-adapter"))
        self.assertEqual(approval.package_dir, PACKAGE_DIR)
        self.assertNotEqual(approval.prefix, build_runtime.HOST_PROFILE.prefix)
        self.assertIn("from tools.approval_mcp_adapter.server import main", approval.launcher_text)
        self.assertNotIn("host_mcp_adapter", approval.launcher_text)
        self.assertEqual(set(build_runtime.PROFILES), {"host", "approval"})

    def test_one_lock_serves_both_adapters(self) -> None:
        self.assertEqual(contract.EXPECTED_RUNTIME, host_contract.EXPECTED_RUNTIME)
        lock = build_runtime.REQUIREMENTS_LOCK.read_text(encoding="utf-8")
        for dist, version in contract.EXPECTED_RUNTIME.items():
            self.assertIn(f"{dist}=={version} \\", lock)
        self.assertFalse((PACKAGE_DIR / "dist").exists(), "the approval adapter must not carry a second lock")


# ====================================================================
# Tier 2 - runtime (pinned mcp 2.x + jeepney)
# ====================================================================


def _driver_params(address: str):
    from mcp.client.stdio import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "tools.test_approval_mcp_adapter_driver", address],
        cwd=str(REPO_ROOT),
    )


def _tool_dump(tool) -> dict:
    return tool.model_dump(by_alias=True, exclude_none=True, mode="json")


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


@requires_runtime
class SdkStdioTests(unittest.TestCase):
    """Gate 1 (SDK-client half) for the approver adapter: both protocol
    eras, exact tools/list, schemas, error mapping, lifecycle."""

    async def _session(self, mode: str, body, address: str = NO_BUS):
        from mcp import Client

        async with Client(_driver_params(address), mode=mode) as client:
            return await body(client)

    def test_modern_era_lists_exactly_the_four_tools(self) -> None:
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

    def test_legacy_initialize_and_identical_canonical_tools_list(self) -> None:
        async def body(client):
            return client.protocol_version, [_tool_dump(t) for t in (await client.list_tools()).tools]

        modern_version, modern = asyncio.run(self._session("2026-07-28", body))
        legacy_version, legacy = asyncio.run(self._session("legacy", body))
        self.assertEqual(legacy_version, "2025-11-25")
        self.assertEqual(contract.canonical_text(modern), contract.canonical_text(legacy))

    def test_unknown_tool_is_protocol_error_and_bad_arguments_are_tool_errors(self) -> None:
        from mcp.shared.exceptions import MCPError

        async def body(client):
            for name in ("approval.grant", "host.capabilities", "approval.validate_and_bind"):
                with self.assertRaises(MCPError):
                    await client.call_tool(name, {})
            results = []
            for name, args in (
                ("approval.review", {"plan_handle": "a" * 64}),
                ("approval.review", {"plan_handle": "a" * 64, "expected_digest": "b" * 64, "request_plan_handle": "", "approver": "specialist"}),
                ("approval.decide", {"review_handle": "c" * 64, "expected_digest": "b" * 64, "decision": "approve",
                                     "reason_code": "approved", "audit_anchor": "a", "idempotency_key": "k"}),
            ):
                results.append(await client.call_tool(name, args))
            return results

        for mode in ("2026-07-28", "legacy"):
            for result in asyncio.run(self._session(mode, body)):
                self.assertTrue(result.is_error)
                self.assertIsNone(result.structured_content)
                self.assertEqual(_payload(result)["error"]["code"], "invalid_arguments")

    def test_initializes_and_returns_bounded_authority_unavailable_without_any_bus(self) -> None:
        async def body(client):
            started = time.monotonic()
            result = await client.call_tool("approval.review.get", {"review_handle": "c" * 64})
            return result, time.monotonic() - started

        for mode in ("2026-07-28", "legacy"):
            result, elapsed = asyncio.run(self._session(mode, body))
            error = _payload(result)["error"]
            self.assertEqual(error["code"], "authority_unavailable")
            self.assertTrue(error["retryable"])
            self.assertIn("no harness restart", error["message"])
            self.assertLess(elapsed, contract.CONNECT_TIMEOUT + 3.0)

    def test_stdio_lifecycle_eof_exits_cleanly_with_only_jsonrpc_on_stdout(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-m", "tools.test_approval_mcp_adapter_driver", NO_BUS],
            cwd=str(REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        initialize = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t2916", "version": "0"}},
        }
        try:
            out, err = process.communicate((json.dumps(initialize) + "\n").encode(), timeout=60)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        self.assertEqual(process.returncode, 0, err.decode(errors="replace")[-2000:])
        lines = [line for line in out.decode().splitlines() if line.strip()]
        for line in lines:
            self.assertEqual(json.loads(line)["jsonrpc"], "2.0")
        self.assertEqual(json.loads(lines[0])["result"]["serverInfo"]["name"], "pantheon-approval")

    def test_main_accepts_no_bus_override(self) -> None:
        from tools.approval_mcp_adapter import server

        for argv in (["--bus-address", "unix:path=/tmp/x"], ["--no-bus"], ["unix:path=/tmp/x"], ["--config", "x"]):
            with self.assertRaises(SystemExit) as ctx:
                server.main(argv)
            self.assertEqual(ctx.exception.code, 2, argv)

    def test_self_check_reports_unreachable_bus_and_no_bus_mode(self) -> None:
        from tools.approval_mcp_adapter import server

        report = contract.self_check_report(asyncio.run(server.self_check_components(NO_BUS, probe_bus=True)))
        self.assertEqual(report["Service"], "pantheon-approval")
        self.assertEqual(report["Subcomponents"]["runtime"], "ready")
        self.assertEqual(report["Subcomponents"]["system_bus"], "unhealthy")
        skipped = contract.self_check_report(asyncio.run(server.self_check_components(NO_BUS, probe_bus=False)))
        self.assertEqual(skipped["Status"], "degraded")
        self.assertEqual(skipped["Subcomponents"]["approval_authority"], "not_configured")

    def test_bus_client_refuses_non_enum_methods_and_host_methods(self) -> None:
        from tools.approval_mcp_adapter.bus_client import ReviewerClient

        client = ReviewerClient(NO_BUS)

        async def body():
            for method, args in (("Review", ("a", "b", "")), (host_contract.Method.CAPABILITIES, ()),
                                 (contract.Method.REVIEW_GET, ("a", "b"))):
                with self.assertRaises(TypeError):
                    await client.call(method, args, timeout=1)

        asyncio.run(body())

    def test_rate_limited_is_a_tool_error_and_sends_nothing(self) -> None:
        from tools.approval_mcp_adapter.server import ApprovalAdapter

        class Recorder:
            calls = 0

            async def call(self, *args, **kwargs):
                Recorder.calls += 1
                return ("open", "{}")

        adapter = ApprovalAdapter(Recorder(), contract.RateLimiter(burst=1, per_second=0.0001))

        async def body():
            return [await adapter.call_tool("approval.review.get", {"review_handle": "c" * 64}) for _ in range(2)]

        first, second = asyncio.run(body())
        self.assertFalse(first.is_error)
        self.assertEqual(_payload(second)["error"]["code"], "rate_limited")
        self.assertEqual(Recorder.calls, 1)


# ====================================================================
# Tier 3a - fault matrix: host AND approver bus clients, identical fakes
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


class _FakeService:
    """In-process jeepney fake owning ``bus_name``, for faults a real
    service cannot be made to produce deterministically. Replies ``ss``
    on success (the out signature of both probed methods)."""

    def __init__(self, address: str, bus_name: str, error_namespace: str, behavior: str) -> None:
        self.address = address
        self.bus_name = bus_name
        self.error_namespace = error_namespace
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
                await router.send_and_get_reply(new_method_call(bus, "RequestName", "su", (self.bus_name, 4)))
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
                        reply = new_method_return(msg, "ss", ("open", '{"schema":"fake"}'))
                    elif behavior == "bad_signature":
                        reply = new_method_return(msg, "i", (5,))
                    elif behavior == "unknown_error":
                        reply = new_error(msg, f"{self.error_namespace}.Bogus", "s", ("/etc/secret token=abc",))
                    elif behavior == "unknown_method":
                        reply = new_error(msg, "org.freedesktop.DBus.Error.UnknownMethod", "s", ("no such method",))
                    else:
                        raise AssertionError(behavior)
                    await router.send(reply)
        finally:
            await router.__aexit__(None, None, None)
            await conn.close()


def _client_specs():
    """(label, client factory, contract module, method, args, code map)."""

    from tools.approval_mcp_adapter.bus_client import ReviewerClient
    from tools.host_mcp_adapter.bus_client import BrokerClient

    return (
        ("host", BrokerClient, host_contract, host_contract.Method.OPERATION_GET, ("d" * 64,),
         {"no_reply": "broker_no_reply", "mismatch": "broker_contract_mismatch", "unavailable": "broker_unavailable"}),
        ("approval", ReviewerClient, contract, contract.Method.REVIEW_GET, ("d" * 64,),
         {"no_reply": "authority_no_reply", "mismatch": "authority_contract_mismatch", "unavailable": "authority_unavailable"}),
    )


@requires_fault_bus
class FaultMatrixTests(unittest.TestCase):
    """One fault matrix, both bus clients: the duplicated transport code
    (D-1) must classify every fault identically (gates 6/29/31)."""

    @classmethod
    def setUpClass(cls) -> None:
        import tools._host_control_testbus as testbus

        cls.testbus = testbus
        cls.handle = testbus.spawn_daemon("t2916f-", _PERMISSIVE_BUS_CONFIG)
        cls.address = cls.handle.bus_address

    @classmethod
    def tearDownClass(cls) -> None:
        cls.testbus.stop_daemon(cls.handle)

    def _with_fake(self, module, behavior: str, body):
        async def run():
            async with _FakeService(self.address, module.BUS_NAME, module.ERROR_NAMESPACE, behavior) as fake:
                return await body(fake)

        return asyncio.run(run())

    def test_absent_service_is_unavailable(self) -> None:
        for label, factory, module, method, args, codes in _client_specs():
            client = factory(self.address)

            async def body(client=client, method=method, args=args, module=module):
                with self.assertRaises(module.AdapterError) as ctx:
                    await client.call(method, args, timeout=2)
                await client.aclose()
                return ctx.exception.code

            self.assertEqual(asyncio.run(body()), codes["unavailable"], label)

    def test_no_reply_is_outcome_unknown_and_drops_the_link(self) -> None:
        for label, factory, module, method, args, codes in _client_specs():
            client = factory(self.address)

            async def body(fake, client=client, method=method, args=args, module=module):
                with self.assertRaises(module.AdapterError) as ctx:
                    await client.call(method, args, timeout=0.5)
                connected = client.connected
                await client.aclose()
                return ctx.exception.code, connected

            code, connected = self._with_fake(module, "stall", body)
            self.assertEqual(code, codes["no_reply"], label)
            self.assertFalse(connected, label)
            self.assertIn("SAME idempotency_key", module.ERROR_CODES[code].message)

    def test_client_cancellation_abandons_the_wait_only(self) -> None:
        for label, factory, module, method, args, _codes in _client_specs():
            client = factory(self.address)

            async def body(fake, client=client, method=method, args=args):
                task = asyncio.create_task(client.call(method, args, timeout=10))
                while fake.seen < 1:
                    await asyncio.sleep(0.01)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                still = client.connected
                result = await client.call(method, args, timeout=10)
                await client.aclose()
                return still, result, fake.seen

            still, result, seen = self._with_fake(module, "stall_first", body)
            self.assertTrue(still, label)
            self.assertEqual(result, ("open", '{"schema":"fake"}'), label)
            self.assertEqual(seen, 2, label)

    def test_malformed_replies_are_contract_mismatch_without_echo(self) -> None:
        for label, factory, module, method, args, codes in _client_specs():
            for behavior in ("bad_signature", "unknown_error", "unknown_method"):
                client = factory(self.address)

                async def body(fake, client=client, method=method, args=args, module=module):
                    with self.assertRaises(module.AdapterError) as ctx:
                        await client.call(method, args, timeout=5)
                    await client.aclose()
                    return ctx.exception

                error = self._with_fake(module, behavior, body)
                self.assertEqual(error.code, codes["mismatch"], (label, behavior))
                self.assertEqual(error.detail, "", (label, behavior))


# ====================================================================
# Tier 3b - private bus with the REAL broker + approval services
# ====================================================================


class _Service:
    """One landed driver subprocess under the system python3 (dbus-python)."""

    def __init__(self, module: str, config: dict, root: Path, testbus, handle) -> None:
        self.module = module
        self.config = config
        self.root = root
        self.testbus = testbus
        self.handle = handle
        self.process: subprocess.Popen | None = None
        self.log = None
        self.state: dict = {}

    def start(self, bus_name: str, address: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        state_path = self.root / "domain" / "state.json"
        if state_path.exists():
            state_path.unlink()  # a restart on the same journal root rewrites it
        config_path = self.root / f"{self.config.get('scenario', 'broker')}.json"
        config_path.write_text(json.dumps(dict(self.config, root=str(self.root / "domain"))), encoding="utf-8")
        self.log = open(self.root / f"{config_path.stem}.log", "a", encoding="utf-8")  # noqa: SIM115
        self.process = subprocess.Popen(
            [SYSTEM_PYTHON, "-m", self.module, str(config_path)],
            cwd=str(REPO_ROOT),
            stdout=self.log,
            stderr=self.log,
            preexec_fn=self.testbus.pdeathsig_preexec_fn(),
        )
        self.handle.register_driver(self.process.pid)
        deadline = time.monotonic() + 60
        while not state_path.exists():
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise AssertionError(f"{self.module} failed: " + Path(self.log.name).read_text()[-3000:])
            time.sleep(0.05)
        self.state = json.loads(state_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 30
        while not _name_owned(address, bus_name):
            if time.monotonic() > deadline:
                raise AssertionError(f"{bus_name} never owned")
            time.sleep(0.05)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None


def _name_owned(address: str, bus_name: str) -> bool:
    async def probe():
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.asyncio import open_dbus_router

        bus = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus", interface="org.freedesktop.DBus")
        async with open_dbus_router(address) as router:
            reply = await router.send_and_get_reply(new_method_call(bus, "NameHasOwner", "s", (bus_name,)))
            return bool(reply.body[0])

    return asyncio.run(probe())


def direct_reviewer_call(address: str, member: str, signature: str, args: tuple) -> tuple[str, tuple]:
    """ANOTHER same-uid process's view: a raw D-Bus call to the Reviewer
    interface that bypasses MCP and any harness registration entirely.
    Returns (error name or "", body)."""

    async def call():
        from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
        from jeepney.io.asyncio import open_dbus_router

        target = DBusAddress(contract.OBJECT_PATH, bus_name=contract.BUS_NAME, interface=contract.REVIEWER_INTERFACE)
        async with open_dbus_router(address) as router:
            reply = await router.send_and_get_reply(new_method_call(target, member, signature, args))
            if reply.header.message_type == MessageType.error:
                return reply.header.fields.get(HeaderFields.error_name), tuple(reply.body)
            return "", tuple(reply.body)

    return asyncio.run(call())


POLICY_DENIED = f"{contract.ERROR_NAMESPACE}.PolicyDenied"


@requires_private_bus
class ApproverPrivateBusTests(unittest.TestCase):
    """Gate 5 and the ORG-052 §7 revocation rule, behaviourally, against
    the REAL landed HostBroker1 (plan source) and HostApproval1 (Reviewer)
    services on one private bus. The caller's uid class is scenario data
    (``tools/test_host_control_approval_dbus_driver.py``): one test process
    has one uid, so the policy shifts relative to it. A private bus cannot
    prove distinct-uid separation or bus-policy fragments - those stay
    enable-leg evidence (SUPPORT_MATRIX)."""

    @classmethod
    def setUpClass(cls) -> None:
        import tools._host_control_testbus as testbus

        cls.testbus = testbus
        cls.handle = testbus.spawn_daemon("t2916-", _PERMISSIVE_BUS_CONFIG)
        cls.address = cls.handle.bus_address
        cls.work_dir = tempfile.TemporaryDirectory(prefix="t2916-bus-")
        cls.work = Path(cls.work_dir.name)
        cls.broker = _Service(
            "tools.test_host_control_dbus_driver",
            {
                "bus_address": cls.address,
                "subject_uid": os.getuid(),
                "register_subject": True,
                "approval_uid": os.getuid(),
            },
            cls.work / "broker",
            testbus,
            cls.handle,
        )
        cls.broker.start("org.pantheon.HostBroker1", cls.address)
        cls.bravo_plan = (cls.broker.state["seat_b_plan_handle"], cls.broker.state["seat_b_plan_digest"])
        cls.own_plan = (cls.broker.state["subject_plan_handle"], cls.broker.state["subject_plan_digest"])
        cls._approval_runs = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls.broker.stop()
        cls.testbus.stop_daemon(cls.handle)
        cls.work_dir.cleanup()

    def setUp(self) -> None:
        self.approval: _Service | None = None

    def tearDown(self) -> None:
        if self.approval is not None:
            self.approval.stop()

    def _approval(self, scenario: str, root: Path | None = None) -> _Service:
        if root is None:
            type(self)._approval_runs += 1
            root = self.work / f"approval-{self._approval_runs}"
        service = _Service(
            "tools.test_host_control_approval_dbus_driver",
            {"bus_address": self.address, "scenario": scenario},
            root,
            self.testbus,
            self.handle,
        )
        service.start(contract.BUS_NAME, self.address)
        self.approval = service
        return service

    def _review_args(self) -> dict:
        return {"plan_handle": self.bravo_plan[0], "expected_digest": self.bravo_plan[1], "request_plan_handle": ""}

    def test_approver_seat_lists_exactly_four_tools_and_routes_one_to_one(self) -> None:
        from mcp import Client

        self._approval("cross_service")
        bravo_handle, bravo_digest = self.bravo_plan

        async def body():
            async with Client(_driver_params(self.address), mode="2026-07-28") as client:
                out = {"names": [t.name for t in (await client.list_tools()).tools]}
                out["review"] = await client.call_tool("approval.review", self._review_args())
                review_handle = out["review"].structured_content["review_handle"]
                out["get"] = await client.call_tool("approval.review.get", {"review_handle": review_handle})
                decide_args = {
                    "review_handle": review_handle, "expected_digest": bravo_digest, "decision": "granted",
                    "reason_code": "approved", "audit_anchor": "ticket T-2916 bus test", "idempotency_key": "t2916-d1",
                }
                out["decide"] = await client.call_tool("approval.decide", decide_args)
                out["decide_again"] = await client.call_tool("approval.decide", decide_args)
                out["human"] = await client.call_tool(
                    "approval.request_human",
                    {"review_handle": review_handle, "expected_digest": bravo_digest, "idempotency_key": "t2916-h"},
                )
                out["own_plan"] = await client.call_tool(
                    "approval.review",
                    {"plan_handle": self.own_plan[0], "expected_digest": self.own_plan[1], "request_plan_handle": ""},
                )
                return out

        out = asyncio.run(body())
        self.assertEqual(out["names"], [t[0] for t in EXPECTED_TOOLS])
        review = out["review"]
        self.assertFalse(review.is_error, review.content[0].text)
        self.assertRegex(review.structured_content["review_handle"], HEX64)
        self.assertEqual(review.structured_content["state"], "open")
        self.assertEqual(review.structured_content["plan_digest"], bravo_digest)
        # the REAL planner's redacted summary, parsed as an object
        self.assertEqual(review.structured_content["summary"]["schema"], "pantheon-host-plan-summary/v1")
        self.assertEqual(review.structured_content["summary"]["plan_handle"], bravo_handle)
        self.assertEqual(json.loads(review.content[0].text), review.structured_content)
        get = out["get"]
        self.assertFalse(get.is_error, get.content[0].text)
        self.assertEqual(get.structured_content["summary"]["schema"], "pantheon-approval-review-summary/v1")
        decide = out["decide"]
        self.assertFalse(decide.is_error, decide.content[0].text)
        self.assertEqual(decide.structured_content["state"], "granted")
        self.assertRegex(decide.structured_content["approval_handle"], HEX64)
        self.assertEqual(out["decide_again"].structured_content, decide.structured_content)
        # T-2919: RequestHuman is live; this driver registers no human
        # ingress, so the decided review gets the closed domain refusal
        human_error = _payload(out["human"])["error"]
        self.assertEqual((human_error["code"], human_error["detail"]), ("failed", "human_ingress_unconfigured"))
        own = _payload(out["own_plan"])["error"]
        self.assertEqual(own["code"], "approver_denied")
        self.assertEqual(own["detail"], "approver_shares_trust_domain")

    def test_requester_only_seat_manual_launch_lists_but_every_call_is_denied(self) -> None:
        """Gate 5: a requester-only seat has no approver registration (see
        tools/test_harness_host_mcp_registration.py). If its uid launches
        the binary manually, tools/list still shows the four tools -
        listing never touches the bus - but every call, and every direct
        Reviewer D-Bus call, is refused server-side."""

        from mcp import Client

        self._approval("cross_service_requester")
        bravo_handle, bravo_digest = self.bravo_plan
        calls = (
            ("approval.review", self._review_args()),
            ("approval.review.get", {"review_handle": "c" * 64}),
            ("approval.decide", {"review_handle": "c" * 64, "expected_digest": bravo_digest, "decision": "granted",
                                 "reason_code": "approved", "audit_anchor": "x", "idempotency_key": "k"}),
            ("approval.request_human", {"review_handle": "c" * 64, "expected_digest": bravo_digest, "idempotency_key": "k"}),
        )

        async def body():
            async with Client(_driver_params(self.address), mode="2026-07-28") as client:
                names = [t.name for t in (await client.list_tools()).tools]
                return names, [await client.call_tool(name, args) for name, args in calls]

        names, results = asyncio.run(body())
        self.assertEqual(names, [t[0] for t in EXPECTED_TOOLS])
        for (name, _), result in zip(calls, results):
            error = _payload(result)["error"]
            self.assertEqual((error["code"], error["detail"]), ("policy_denied", "subject_denied"), name)
            self.assertNotIn("pantheon-host-plan-summary", result.content[0].text)
            self.assertNotIn(bravo_handle, result.content[0].text)
        for member, signature, args in (
            ("Review", "sss", (bravo_handle, bravo_digest, "")),
            ("ReviewGet", "s", ("c" * 64,)),
            ("Decide", "ssssss", ("c" * 64, bravo_digest, "granted", "approved", "x", "k")),
            ("RequestHuman", "sss", ("c" * 64, bravo_digest, "k")),
        ):
            name, body_ = direct_reviewer_call(self.address, member, signature, args)
            self.assertEqual((name, body_), (POLICY_DENIED, ("subject_denied",)), member)

    def test_policy_removal_revokes_despite_stale_config_and_registration_deletion_is_not_revocation(self) -> None:
        """ORG-052 §7: removing the registration is not revocation; policy
        removal is, even for a live adapter launched from stale config and
        for another same-uid process calling Reviewer directly."""

        from mcp import Client

        root = self.work / "approval-revocation"
        self._approval("cross_service", root)
        bravo_handle, bravo_digest = self.bravo_plan
        approval_r1 = self.approval

        async def body():
            # the adapter process stays up across the policy change: this is
            # the seat whose harness config still registers the approver
            async with Client(_driver_params(self.address), mode="2026-07-28") as client:
                before = await client.call_tool("approval.review", self._review_args())
                review_handle = before.structured_content["review_handle"]
                # (1) registration deletion is NOT revocation: with policy r1
                # still installed, a process that has no MCP registration at
                # all reaches the Reviewer directly and is served
                direct_before = await asyncio.to_thread(
                    direct_reviewer_call, self.address, "ReviewGet", "s", (review_handle,)
                )
                # (2) policy removal: install r2 (specialist dropped) and restart
                # the approval service on the SAME journal root
                await asyncio.to_thread(approval_r1.stop)
                await asyncio.to_thread(self._approval, "cross_service_revoked", root)
                after = {
                    "get": await client.call_tool("approval.review.get", {"review_handle": review_handle}),
                    "review": await client.call_tool("approval.review", self._review_args()),
                    "decide": await client.call_tool("approval.decide", {
                        "review_handle": review_handle, "expected_digest": bravo_digest, "decision": "granted",
                        "reason_code": "approved", "audit_anchor": "stale config", "idempotency_key": "t2916-rev",
                    }),
                }
                direct_after = await asyncio.to_thread(
                    direct_reviewer_call, self.address, "ReviewGet", "s", (review_handle,)
                )
                return before, direct_before, after, direct_after

        before, direct_before, after, direct_after = asyncio.run(body())
        self.assertFalse(before.is_error, before.content[0].text)
        self.assertEqual(direct_before[0], "", direct_before)
        self.assertEqual(direct_before[1][0], "open")
        for name, result in after.items():
            error = _payload(result)["error"]
            self.assertEqual((error["code"], error["detail"]), ("policy_denied", "subject_denied"), name)
        self.assertEqual(direct_after, (POLICY_DENIED, ("subject_denied",)))

    def test_service_absent_then_started_same_adapter_recovers(self) -> None:
        """Gate 6 (approver half): the SAME live adapter lists its tools with
        the approval service absent, returns the bounded unavailable error,
        then succeeds once the service appears - no harness restart."""

        from mcp import Client

        async def body():
            async with Client(_driver_params(self.address), mode="2026-07-28") as client:
                names = [t.name for t in (await client.list_tools()).tools]
                down = await client.call_tool("approval.review", self._review_args())
                await asyncio.to_thread(self._approval, "cross_service")
                up = await client.call_tool("approval.review", self._review_args())
                return names, down, up

        names, down, up = asyncio.run(body())
        self.assertEqual(names, [t[0] for t in EXPECTED_TOOLS])
        self.assertEqual(_payload(down)["error"]["code"], "authority_unavailable")
        self.assertFalse(up.is_error, up.content[0].text)

    def test_self_check_against_private_bus(self) -> None:
        from tools.approval_mcp_adapter import server

        absent = contract.self_check_report(asyncio.run(server.self_check_components(self.address, probe_bus=True)))
        self.assertEqual(absent["Subcomponents"]["system_bus"], "ready")
        self.assertEqual(absent["Subcomponents"]["approval_authority"], "degraded")
        self._approval("cross_service")
        present = contract.self_check_report(asyncio.run(server.self_check_components(self.address, probe_bus=True)))
        self.assertEqual(present["Status"], "ready")


# ====================================================================
# Tier 4 - packaging against fixture roots (gate 7, fixture form)
# ====================================================================


def _load_pre_refactor_build_runtime(tmp: Path):
    """The T-2915 dist script, from git, with its paths pointed at this
    checkout. Returns None when the blob is not in this clone."""

    result = subprocess.run(
        ["git", "cat-file", "-p", PRE_REFACTOR_BUILD_RUNTIME_BLOB],
        cwd=str(REPO_ROOT), capture_output=True, timeout=30,
    )
    if result.returncode != 0:
        return None
    path = tmp / "build_runtime_t2915.py"
    path.write_bytes(result.stdout)
    spec = importlib.util.spec_from_file_location("build_runtime_t2915", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DIST_DIR = HOST_DIST_DIR
    module.PACKAGE_DIR = HOST_DIST_DIR.parent
    module.REQUIREMENTS_IN = HOST_DIST_DIR / "requirements.in"
    module.REQUIREMENTS_LOCK = HOST_DIST_DIR / "requirements.lock"
    return module


@requires_wheelhouse
class ApprovalPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import verify_artifact

        cls.verify_artifact = verify_artifact
        cls.tmp = tempfile.TemporaryDirectory(prefix="t2916-pkg-")
        cls.root = Path(cls.tmp.name) / "root"
        cls.profile = build_runtime.APPROVAL_PROFILE
        cls.manifest = build_runtime.build(cls.root, Path(WHEELHOUSE), python=BUILD_PYTHON, profile=cls.profile)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_fixture_root_verifies_every_check(self) -> None:
        report = self.verify_artifact.verify(self.root, profile=self.profile)
        self.assertTrue(report["ok"], json.dumps(report, indent=1))
        self.assertEqual(report["checks"]["dist_set"]["count"], 29)
        self.assertEqual(report["checks"]["isolated_import"]["detail"], "mcp==2.3.0,jeepney==0.9.0")
        self.assertTrue(self.verify_artifact.verify(self.root, profile=self.profile)["ok"])
        self.assertEqual(self.manifest["schema"], "pantheon-approval-mcp-adapter-build/v1")

    def test_owner_check_is_live(self) -> None:
        if os.getuid() == 0:
            self.skipTest("owner check falsification needs a non-root runner")
        self.assertFalse(self.verify_artifact.verify(self.root, 0, self.profile)["checks"]["owner"]["ok"])

    def test_packaged_app_is_exactly_the_approver_modules(self) -> None:
        tools_dir = self.root / self.profile.prefix / "app" / "tools"
        self.assertEqual({p.name for p in tools_dir.iterdir() if p.is_dir() and p.name != "__pycache__"}, {"approval_mcp_adapter"})
        self.assertEqual({p.name for p in (tools_dir / "approval_mcp_adapter").glob("*.py")}, set(self.profile.packaged_modules))
        self.assertFalse(list((self.root / self.profile.prefix).rglob("test_approval_mcp_adapter_driver.py")))
        self.assertFalse(list((self.root / self.profile.prefix).rglob("host_mcp_adapter")))
        launcher = (self.root / self.profile.launcher).read_text(encoding="utf-8")
        self.assertTrue(launcher.startswith("#!/usr/local/lib/pantheon/approval-mcp-adapter/venv/bin/python3 -I\n"))

    def test_launcher_self_check_identifies_the_approver(self) -> None:
        result = subprocess.run(
            [str(self.root / self.profile.prefix / "venv" / "bin" / "python3"), "-I",
             str(self.root / self.profile.launcher), "--self-check", "--no-bus"],
            capture_output=True, text=True, timeout=60, env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["Service"], "pantheon-approval")

    def test_tampered_artifact_fails_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t2916-tamper-") as tmp:
            root = Path(tmp) / "root"
            build_runtime.build(root, Path(WHEELHOUSE), python=BUILD_PYTHON, profile=self.profile)
            target = root / self.profile.prefix / "app" / "tools" / "approval_mcp_adapter" / "contract.py"
            os.chmod(target, 0o644)
            target.write_text(target.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
            report = self.verify_artifact.verify(root, profile=self.profile)
            self.assertFalse(report["checks"]["manifest"]["ok"])


@requires_wheelhouse
class HostProfileEquivalenceTests(unittest.TestCase):
    """HO condition (c) on T-2916: for the same inputs, the refactored
    default (host) profile builds a byte-identical tree and BUILD-MANIFEST
    to the pre-refactor T-2915 script. "Same inputs" includes the build
    root path (bytecode and pyvenv.cfg embed it) and SOURCE_DATE_EPOCH
    (hash-based .pyc invalidation): without both, even two runs of the
    UNCHANGED T-2915 script differ, so the comparison pins them."""

    def test_default_profile_is_byte_identical_to_the_t2915_build(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t2916-equiv-") as tmp:
            tmp_path = Path(tmp)
            legacy = _load_pre_refactor_build_runtime(tmp_path)
            if legacy is None:
                self.skipTest(f"pre-refactor blob {PRE_REFACTOR_BUILD_RUNTIME_BLOB} not in this clone")
            saved = os.environ.get("SOURCE_DATE_EPOCH")
            os.environ["SOURCE_DATE_EPOCH"] = "1760000000"
            try:
                root = tmp_path / "root"
                old_manifest = legacy.build(root, Path(WHEELHOUSE), python=BUILD_PYTHON)
                old_launcher = (root / build_runtime.LAUNCHER).read_bytes()
                os.rename(root, tmp_path / "t2915")
                new_manifest = build_runtime.build(root, Path(WHEELHOUSE), python=BUILD_PYTHON)
            finally:
                if saved is None:
                    os.environ.pop("SOURCE_DATE_EPOCH", None)
                else:
                    os.environ["SOURCE_DATE_EPOCH"] = saved
            self.assertEqual(len(new_manifest["files"]), len(old_manifest["files"]))
            differing = sorted(
                key for key in set(old_manifest["files"]) | set(new_manifest["files"])
                if old_manifest["files"].get(key) != new_manifest["files"].get(key)
            )
            self.assertEqual(differing, [])
            self.assertEqual(new_manifest, old_manifest)
            self.assertEqual(
                (root / build_runtime.PREFIX / build_runtime.MANIFEST_NAME).read_bytes(),
                (tmp_path / "t2915" / build_runtime.PREFIX / build_runtime.MANIFEST_NAME).read_bytes(),
            )
            self.assertEqual((root / build_runtime.LAUNCHER).read_bytes(), old_launcher)


if __name__ == "__main__":
    unittest.main()
