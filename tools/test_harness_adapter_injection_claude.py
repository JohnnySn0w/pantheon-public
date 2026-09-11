"""Unit tests for tools/harness_adapter/injection_claude.py — the Claude Code
native encoder for the Concordance injection contract (ORG-073 §4; T-3242).

Run from repo root:
    python3 -m unittest tools.test_harness_adapter_injection_claude

Fixture-backed (D11): the positive wire-shape cases load the two live
UserPromptSubmit capture files (T-3241, session 193a6f06, 2026-08-25T19:20Z)
byte-verbatim from disk. The parser is driven from ``stdin.raw`` — the actual
wire bytes — and the renderer is asserted byte-for-byte against
``emit.stdout`` — the exact envelope the harness demonstrably accepted and
injected (COMPOSE verdict, T-3241 §4 annotation). Negative cases use
hand-built dicts deliberately: they exercise contract behaviour on gaps, not
wire-shape claims.

Coverage:
  1. Package exports — new names present; pre-existing names still exported
  2. Fixture-driven parse — both fires, byte-verbatim, key order, compose
     invariant (identical stdin -> equal requests)
  3. Parser negatives — missing/blank/non-str required fields pass through
     and fail missing_required(); cwd/hook_event projection rules; non-Mapping
     degradation; unmodeled keys verbatim in native; shallow-copy semantics
  4. Renderer — fixture byte-identical envelope, JSON round-trip, model
     silence for every no-op reason / failure kind / garbage, exit 0 always
  5. End-to-end seam — parse -> invoke -> render with stdout/stderr capture
     proving the encoder itself writes nothing anywhere
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools import harness_adapter
from tools.harness_adapter import (
    HookInjectionProvider,
    Injection,
    InjectionBackendFailure,
    InjectionNoOp,
    InjectionRequest,
    SourceRecord,
    native_response_from_result,
    request_from_native,
)
from tools.harness_adapter.injection import FAILURE_KINDS, NO_OP_REASONS

# ---------------------------------------------------------------------------
# Fixtures (T-3241 live captures — never paraphrased, loaded from disk)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_DIR = (
    _REPO_ROOT
    / "knowledge"
    / "episodic"
    / "2026-08-21-org-073-v2-poc-prep"
    / "ups-capture-fixtures"
)
_FIXTURE_A = _FIXTURE_DIR / "ups_20260825T192004.389081Z_00002430909510836194_193a6f06_A_2684936.json"
_FIXTURE_B = _FIXTURE_DIR / "ups_20260825T192004.379255Z_00002430909500988217_193a6f06_B_2684934.json"

# Observed stdin key order (T-3241 §4: the 7 native fields, wire order).
_OBSERVED_STDIN_KEYS = [
    "session_id",
    "transcript_path",
    "cwd",
    "prompt_id",
    "permission_mode",
    "hook_event_name",
    "prompt",
]

_SESSION_ID = "193a6f06-5a95-4996-a8f3-eebb5c1d8a57"


def _load_capture(path: Path) -> dict:
    """Load a capture file byte-verbatim from disk."""
    return json.loads(path.read_bytes())


def _wire_stdin(capture: dict) -> dict:
    """Parse the capture's raw stdin bytes — the actual wire — into a dict."""
    return json.loads(capture["stdin"]["raw"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The package surface as it stood BEFORE this module was added (T-3240 state).
# Asserted as a SUBSET of __all__, same convention as the T-3240 test module.
_PRE_EXISTING_EXPORTS = (
    "ClaudeHarnessAdapter",
    "CodexHarnessAdapter",
    "CommandSpec",
    "CorpusClass",
    "DeliveryReceipt",
    "DiagnosticObservation",
    "DiagnosticReport",
    "ExecutionError",
    "ExecutionRequest",
    "ExecutionResult",
    "FailureKind",
    "HarnessCapabilities",
    "HarnessEvent",
    "HarnessRunner",
    "HookCapability",
    "HookInjectionProvider",
    "InboundRequest",
    "Injection",
    "InjectionBackendFailure",
    "InjectionKind",
    "InjectionNoOp",
    "InjectionRequest",
    "InjectionResult",
    "integration",
    "model_visible_text",
    "NoOpReason",
    "PantheonHarnessAdapter",
    "ProviderBillingEvidence",
    "SessionHandle",
    "SourceRecord",
    "SupersessionState",
    "Usage",
)

_NEW_EXPORTS = (
    "native_response_from_result",
    "request_from_native",
)


def _source(**overrides) -> SourceRecord:
    fields = {
        "source_id": "ORG-073",
        "corpus_class": "adr",
        "path": "adr/ORG-073-information-flow-retrieval-injection-layer.md",
        "rank": 1,
        "supersession": "current",
    }
    fields.update(overrides)
    return SourceRecord(**fields)


def _injection(markdown: str = "Recalled decision (verify it still applies): ORG-073") -> Injection:
    return Injection(markdown=markdown, sources=(_source(),))


class _CannedProvider(HookInjectionProvider):
    """Returns a canned result; records whether retrieve() was reached."""

    def __init__(self, outcome):
        self.calls: list = []
        self._outcome = outcome

    def retrieve(self, request: InjectionRequest):
        self.calls.append(request)
        return self._outcome


# ---------------------------------------------------------------------------
# 1. Package exports
# ---------------------------------------------------------------------------

class ExportsTest(unittest.TestCase):
    def test_new_names_exported_from_package(self):
        for name in _NEW_EXPORTS:
            with self.subTest(name=name):
                self.assertIn(name, harness_adapter.__all__)
                self.assertTrue(hasattr(harness_adapter, name), name)

    def test_existing_exports_undisturbed(self):
        self.assertTrue(set(_PRE_EXISTING_EXPORTS) <= set(harness_adapter.__all__))
        for name in _PRE_EXISTING_EXPORTS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(harness_adapter, name), name)


# ---------------------------------------------------------------------------
# 2. Fixture-driven parse
# ---------------------------------------------------------------------------

class FixtureParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.capture_a = _load_capture(_FIXTURE_A)
        cls.capture_b = _load_capture(_FIXTURE_B)

    def test_fixture_raw_parses_to_recorded_json(self):
        # Preamble anchor: the raw wire bytes parse to exactly the capture
        # tool's own parsed record — driving tests from raw is legitimate.
        for label, capture in (("A", self.capture_a), ("B", self.capture_b)):
            with self.subTest(label=label):
                self.assertIsNone(capture["stdin"]["json_error"])
                self.assertEqual(_wire_stdin(capture), capture["stdin"]["json"])

    def _assert_expected_request(self, capture: dict) -> InjectionRequest:
        stdin = _wire_stdin(capture)
        req = request_from_native(stdin)
        self.assertEqual(req.prompt_text, stdin["prompt"])
        self.assertTrue(req.prompt_text.startswith("<task-notification>"))
        self.assertEqual(req.session_id, _SESSION_ID)
        self.assertEqual(req.cwd, Path("/home/claude/pantheon-config"))
        self.assertEqual(req.hook_event, "UserPromptSubmit")
        self.assertEqual(req.harness, "claude")
        self.assertIsNone(req.repo_identity)
        self.assertIsNone(req.role_domain)
        self.assertEqual(req.missing_required(), ())
        return req

    def test_fixture_a_parses_to_expected_request(self):
        self._assert_expected_request(self.capture_a)

    def test_fixture_b_parses_to_expected_request(self):
        self._assert_expected_request(self.capture_b)

    def test_native_preserves_all_stdin_keys_in_order(self):
        for label, capture in (("A", self.capture_a), ("B", self.capture_b)):
            with self.subTest(label=label):
                stdin = _wire_stdin(capture)
                req = request_from_native(stdin)
                self.assertEqual(list(req.native), list(stdin))
                self.assertEqual(list(req.native), _OBSERVED_STDIN_KEYS)
                for key in stdin:
                    self.assertEqual(req.native[key], stdin[key], key)
                # Unmodeled fields explicitly spot-checked verbatim.
                self.assertEqual(
                    req.native["transcript_path"],
                    f"/home/claude/.claude/projects/-home-claude/{_SESSION_ID}.jsonl",
                )
                self.assertEqual(req.native["prompt_id"], "82ab6a63-856a-4e89-99ef-b2bd5ac62a97")
                self.assertEqual(req.native["permission_mode"], "default")
                # Mapped keys are ALSO in native (whole-payload source of
                # record): same string via a different accessor.
                self.assertEqual(req.native["prompt"], req.prompt_text)
                self.assertEqual(req.native["session_id"], req.session_id)

    def test_compose_fires_deliver_identical_stdin(self):
        # Measured compose fact (T-3241 §4 COMPOSE verdict): the SAME event is
        # delivered verbatim to both registered hooks — raw bytes identical.
        self.assertEqual(self.capture_a["stdin"]["raw"], self.capture_b["stdin"]["raw"])

    def test_both_fires_produce_equal_requests(self):
        # "Structurally identical modulo label + pid" is vacuously exact here:
        # label/pid live in capture metadata, not in the stdin payload, so the
        # two requests are simply EQUAL.
        req_a = request_from_native(_wire_stdin(self.capture_a))
        req_b = request_from_native(_wire_stdin(self.capture_b))
        self.assertEqual(req_a, req_b)


# ---------------------------------------------------------------------------
# 3. Parser negatives (hand-built dicts — contract behaviour, not wire shape)
# ---------------------------------------------------------------------------

class ParserNegativeTest(unittest.TestCase):
    def test_missing_prompt_yields_failing_required(self):
        req = request_from_native({"session_id": "s"})
        self.assertEqual(req.prompt_text, "")
        self.assertEqual(req.missing_required(), ("prompt_text",))

    def test_missing_session_id_yields_failing_required(self):
        req = request_from_native({"prompt": "p"})
        self.assertEqual(req.session_id, "")
        self.assertEqual(req.missing_required(), ("session_id",))

    def test_missing_both_flags_both(self):
        req = request_from_native({})
        self.assertEqual(req.missing_required(), ("prompt_text", "session_id"))
        self.assertEqual(dict(req.native), {})

    def test_whitespace_prompt_preserved_verbatim_and_flagged(self):
        req = request_from_native({"prompt": "  \n\t ", "session_id": "s"})
        self.assertEqual(req.prompt_text, "  \n\t ")  # verbatim, not coerced
        self.assertEqual(req.missing_required(), ("prompt_text",))

    def test_non_str_prompt_passes_through_and_flagged(self):
        for bad in (42, None, {"nested": "x"}):
            with self.subTest(prompt=bad):
                req = request_from_native({"prompt": bad, "session_id": "s"})
                self.assertEqual(req.prompt_text, bad)  # faithful passthrough
                self.assertEqual(req.missing_required(), ("prompt_text",))
                self.assertEqual(req.native["prompt"], bad)

    def test_non_str_session_id_passes_through_and_flagged(self):
        for bad in (None, 42):
            with self.subTest(session_id=bad):
                req = request_from_native({"prompt": "p", "session_id": bad})
                self.assertEqual(req.session_id, bad)
                self.assertEqual(req.missing_required(), ("session_id",))

    def test_absent_blank_or_non_str_cwd_is_none(self):
        for payload in (
            {"prompt": "p", "session_id": "s"},
            {"prompt": "p", "session_id": "s", "cwd": ""},
            {"prompt": "p", "session_id": "s", "cwd": "   "},
            {"prompt": "p", "session_id": "s", "cwd": 42},
            {"prompt": "p", "session_id": "s", "cwd": None},
        ):
            with self.subTest(cwd=payload.get("cwd", "<absent>")):
                req = request_from_native(payload)
                self.assertIsNone(req.cwd)
                if "cwd" in payload:
                    # Lossy projection never destroys the original value.
                    self.assertEqual(req.native["cwd"], payload["cwd"])

    def test_valid_cwd_projects_to_path(self):
        req = request_from_native({"prompt": "p", "session_id": "s", "cwd": "/tmp/x"})
        self.assertEqual(req.cwd, Path("/tmp/x"))

    def test_non_str_or_absent_hook_event_is_none(self):
        for payload in (
            {"prompt": "p", "session_id": "s"},
            {"prompt": "p", "session_id": "s", "hook_event_name": 42},
            {"prompt": "p", "session_id": "s", "hook_event_name": None},
        ):
            with self.subTest(hook_event=payload.get("hook_event_name", "<absent>")):
                req = request_from_native(payload)
                self.assertIsNone(req.hook_event)
        # A blank string IS a string: faithful passthrough, no coercion.
        req = request_from_native({"prompt": "p", "session_id": "s", "hook_event_name": ""})
        self.assertEqual(req.hook_event, "")

    def test_non_mapping_payload_degrades_to_empty_request(self):
        for bad in ([1, 2], "a json string", None, 42):
            with self.subTest(payload=bad):
                req = request_from_native(bad)  # type: ignore[arg-type]
                self.assertEqual(req.prompt_text, "")
                self.assertEqual(req.session_id, "")
                self.assertEqual(req.harness, "claude")
                self.assertEqual(dict(req.native), {})
                self.assertEqual(req.missing_required(), ("prompt_text", "session_id"))

    def test_extra_unmodeled_keys_land_verbatim_in_native(self):
        nested = {"deep": {"a": 1}}
        payload = {
            "prompt": "p",
            "session_id": "s",
            "future_field": "v2.2 adds me",
            "nested": nested,
            "count": 7,
        }
        req = request_from_native(payload)
        self.assertEqual(list(req.native), list(payload))  # order preserved
        self.assertEqual(req.native["future_field"], "v2.2 adds me")
        self.assertEqual(req.native["count"], 7)
        self.assertIs(req.native["nested"], nested)  # shallow: same object

    def test_harness_kwarg_default_and_override(self):
        self.assertEqual(request_from_native({"prompt": "p", "session_id": "s"}).harness, "claude")
        req = request_from_native({"prompt": "p", "session_id": "s"}, harness="codex")
        self.assertEqual(req.harness, "codex")

    def test_native_is_shallow_copy_not_alias(self):
        payload = {"prompt": "p", "session_id": "s"}
        req = request_from_native(payload)
        payload["added_after_parse"] = True
        self.assertNotIn("added_after_parse", req.native)


# ---------------------------------------------------------------------------
# 4. Renderer
# ---------------------------------------------------------------------------

class RendererTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.capture_a = _load_capture(_FIXTURE_A)
        cls.capture_b = _load_capture(_FIXTURE_B)

    def test_injection_renders_fixture_byte_identical_envelope(self):
        # The strongest wire anchor: rebuild each fixture's own accepted
        # emit.stdout byte-for-byte from an Injection carrying its marker.
        for label, capture in (("A", self.capture_a), ("B", self.capture_b)):
            with self.subTest(label=label):
                observed = capture["emit"]["stdout"]
                marker = json.loads(observed)["hookSpecificOutput"]["additionalContext"]
                text, code = native_response_from_result(_injection(markdown=marker))
                self.assertEqual(text, observed)  # byte-for-byte, incl. trailing \n
                self.assertEqual(code, 0)

    def test_injection_envelope_shape_and_roundtrip(self):
        markdown = '## Recalled decision\n\n- "quoted" — π 🧠\n'
        text, code = native_response_from_result(_injection(markdown=markdown))
        self.assertEqual(code, 0)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text.count("\n"), 1)  # single wire line + terminal newline
        envelope = json.loads(text)
        self.assertEqual(list(envelope), ["hookSpecificOutput"])
        inner = envelope["hookSpecificOutput"]
        self.assertEqual(list(inner), ["hookEventName", "additionalContext"])
        self.assertEqual(inner["hookEventName"], "UserPromptSubmit")
        self.assertEqual(inner["additionalContext"], markdown)

    def test_hook_event_name_override(self):
        text, code = native_response_from_result(_injection(), hook_event_name="PreToolUse")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["hookSpecificOutput"]["hookEventName"], "PreToolUse")

    def test_no_op_renders_empty_stdout_every_reason(self):
        for reason in sorted(NO_OP_REASONS):
            with self.subTest(reason=reason):
                result = InjectionNoOp(reason=reason, detail="x", evidence={"k": "v"})
                self.assertEqual(native_response_from_result(result), ("", 0))

    def test_backend_failure_renders_empty_stdout_every_kind(self):
        for failure in sorted(FAILURE_KINDS):
            with self.subTest(failure=failure):
                result = InjectionBackendFailure(failure=failure, detail="boom", evidence={"k": "v"})
                self.assertEqual(native_response_from_result(result), ("", 0))

    def test_non_result_garbage_renders_silent(self):
        # A defective caller cannot smuggle text: model_visible_text() is None
        # for non-results, so the wire stays empty and exits 0.
        for garbage in ("a string", None, 42, {"markdown": "x"}):
            with self.subTest(garbage=garbage):
                self.assertEqual(
                    native_response_from_result(garbage),  # type: ignore[arg-type]
                    ("", 0),
                )

    def test_exit_code_zero_on_every_path(self):
        results = [_injection(), "garbage", None]
        results += [InjectionNoOp(reason=r) for r in sorted(NO_OP_REASONS)]
        results += [InjectionBackendFailure(failure=f, detail="d") for f in sorted(FAILURE_KINDS)]
        for result in results:
            with self.subTest(result=type(result).__name__):
                text, code = native_response_from_result(result)  # type: ignore[arg-type]
                self.assertEqual(code, 0)
                self.assertIsInstance(text, str)


# ---------------------------------------------------------------------------
# 5. End-to-end seam (parse -> invoke -> render), stdout/stderr capture
# ---------------------------------------------------------------------------

class EndToEndSilenceTest(unittest.TestCase):
    def _run_captured(self, fn):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = fn()
        self.assertEqual(out.getvalue(), "")  # constraint (b): encoder writes
        self.assertEqual(err.getvalue(), "")  # NOTHING to stdout or stderr
        return result

    def test_gap_payload_flows_to_silent_wire(self):
        provider = _CannedProvider(outcome=_injection())

        def flow():
            req = request_from_native({})
            result = provider.invoke(req)
            return result, native_response_from_result(result)

        result, wire = self._run_captured(flow)
        self.assertIsInstance(result, InjectionNoOp)
        self.assertEqual(result.reason, "missing_required_field")
        self.assertEqual(result.detail, "prompt_text,session_id")
        self.assertEqual(provider.calls, [])  # invoke short-circuited retrieve
        self.assertEqual(wire, ("", 0))

    def test_fixture_payload_flows_to_injected_wire(self):
        stdin = _wire_stdin(_load_capture(_FIXTURE_A))
        inj = _injection()
        provider = _CannedProvider(outcome=inj)

        def flow():
            req = request_from_native(stdin)
            result = provider.invoke(req)
            return req, result, native_response_from_result(result)

        req, result, (text, code) = self._run_captured(flow)
        self.assertIs(result, inj)
        self.assertEqual(provider.calls, [req])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(text)["hookSpecificOutput"]["additionalContext"], inj.markdown
        )


if __name__ == "__main__":
    unittest.main()
