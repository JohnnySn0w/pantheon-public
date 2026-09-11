"""Unit tests for tools/harness_adapter/injection_codex.py — the Codex CLI
native encoder for the Concordance injection contract (ORG-073 §4; T-3243).

Run from repo root:
    python3 -m unittest tools.test_harness_adapter_injection_codex

**DOCUMENTED-NOT-OBSERVED discipline.** Unlike
``tools/test_harness_adapter_injection_claude.py`` (T-3242), which drives its
positive wire-shape assertions from two byte-verbatim live captures, NO Codex
capture exists anywhere readable from this arc's seat (the codex home
directory is not readable, and no codex hooks are configured anywhere
readable). Every test in this file that touches a Codex-specific key name,
key order, or envelope byte shape is driven from an IN-CODE SYNTHETIC fixture
built from the documented spec (``adr/ORG-073-...`` §4 + the codex-harness
investigation notes cited in ``injection_codex.py``'s module docstring), and
carries an explicit ``# DOCUMENTED-NOT-OBSERVED:`` marker on the assertion.
This is spec-driven testing, not wire-shape proof — the live-verify follow-on
ticket (filed to the codex seat) is what turns these into observed facts.

Coverage:
  1. Package exports — new codex names present; pre-existing names (including
     T-3242's) still exported
  2. Synthetic-payload parse — documented key set, key order, compose-style
     equality; every field-level assertion DOCUMENTED-NOT-OBSERVED-marked
  3. Parser negatives — missing/blank/non-str required fields pass through
     and fail missing_required(); cwd/hook_event projection rules; non-Mapping
     degradation; unmodeled keys verbatim in native; shallow-copy semantics;
     harness kwarg defaults to "codex"
  4. Renderer — documented envelope shape + roundtrip (NOT byte-identical to
     any real capture — no real capture exists), model silence for every
     no-op reason / failure kind / garbage, exit 0 always
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
    codex_native_response_from_result,
    codex_request_from_native,
)
from tools.harness_adapter.injection import FAILURE_KINDS, NO_OP_REASONS

# ---------------------------------------------------------------------------
# Synthetic fixture (DOCUMENTED-NOT-OBSERVED — needs T-3244 real-capture
# replacement). Key set and key order are taken from the documented spec
# cited in injection_codex.py's module docstring, NOT from any live capture.
# ---------------------------------------------------------------------------

_SESSION_ID = "codex-syn-8f21c0a4-46d2-4b2a-9c1e-2b6a7d0c9e11"
_CWD = "/home/claude/pantheon-config"
_PROMPT = "<task-notification>synthetic codex UserPromptSubmit payload</task-notification>"

# DOCUMENTED-NOT-OBSERVED: key set + key order are the ticket brief's
# documented spec (session_id, cwd, hook_event_name, permission_mode,
# prompt), not an observed wire order — no Codex capture exists to observe.
_SYNTHETIC_STDIN = {
    "session_id": _SESSION_ID,
    "cwd": _CWD,
    "hook_event_name": "UserPromptSubmit",
    "permission_mode": "default",
    "prompt": _PROMPT,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The package surface as it stood BEFORE this module was added (T-3242 state,
# i.e. AFTER T-3242 landed). Asserted as a SUBSET of __all__, same convention
# as the T-3242 test module.
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
    "native_response_from_result",
    "NoOpReason",
    "PantheonHarnessAdapter",
    "ProviderBillingEvidence",
    "request_from_native",
    "SessionHandle",
    "SourceRecord",
    "SupersessionState",
    "Usage",
)

_NEW_EXPORTS = (
    "codex_native_response_from_result",
    "codex_request_from_native",
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

    def test_codex_and_claude_exports_are_distinct_callables(self):
        # The whole reason for the codex_-prefix: package-level export names
        # must not collide/shadow between the two per-harness encoders.
        self.assertIsNot(
            harness_adapter.codex_request_from_native,
            harness_adapter.request_from_native,
        )
        self.assertIsNot(
            harness_adapter.codex_native_response_from_result,
            harness_adapter.native_response_from_result,
        )


# ---------------------------------------------------------------------------
# 2. Synthetic-payload parse (DOCUMENTED-NOT-OBSERVED)
# ---------------------------------------------------------------------------

class SyntheticParseTest(unittest.TestCase):
    def test_synthetic_payload_parses_to_expected_request(self):
        req = codex_request_from_native(_SYNTHETIC_STDIN)
        # DOCUMENTED-NOT-OBSERVED: "prompt" is the documented field name for
        # the UserPromptSubmit prompt text; not verified against a real wire.
        self.assertEqual(req.prompt_text, _PROMPT)
        # DOCUMENTED-NOT-OBSERVED: "session_id" field name per documentation.
        self.assertEqual(req.session_id, _SESSION_ID)
        # DOCUMENTED-NOT-OBSERVED: "cwd" field name + string-path shape.
        self.assertEqual(req.cwd, Path(_CWD))
        # DOCUMENTED-NOT-OBSERVED: "hook_event_name" field name.
        self.assertEqual(req.hook_event, "UserPromptSubmit")
        self.assertEqual(req.harness, "codex")
        self.assertIsNone(req.repo_identity)
        self.assertIsNone(req.role_domain)
        self.assertEqual(req.missing_required(), ())

    def test_native_preserves_all_stdin_keys_in_order(self):
        req = codex_request_from_native(_SYNTHETIC_STDIN)
        self.assertEqual(list(req.native), list(_SYNTHETIC_STDIN))
        for key in _SYNTHETIC_STDIN:
            self.assertEqual(req.native[key], _SYNTHETIC_STDIN[key], key)
        # DOCUMENTED-NOT-OBSERVED: "permission_mode" is documented as present
        # on the wire but is NOT projected to a typed field (mirrors T-3242's
        # treatment of the same key) — verify it still lands in native.
        self.assertEqual(req.native["permission_mode"], "default")
        # Mapped keys are ALSO in native (whole-payload source of record):
        # same string via a different accessor.
        self.assertEqual(req.native["prompt"], req.prompt_text)
        self.assertEqual(req.native["session_id"], req.session_id)

    def test_repeated_parse_of_identical_synthetic_payload_is_equal(self):
        req_a = codex_request_from_native(dict(_SYNTHETIC_STDIN))
        req_b = codex_request_from_native(dict(_SYNTHETIC_STDIN))
        self.assertEqual(req_a, req_b)


# ---------------------------------------------------------------------------
# 3. Parser negatives (hand-built dicts — contract behaviour, not wire shape)
# ---------------------------------------------------------------------------

class ParserNegativeTest(unittest.TestCase):
    def test_missing_prompt_yields_failing_required(self):
        req = codex_request_from_native({"session_id": "s"})
        self.assertEqual(req.prompt_text, "")
        self.assertEqual(req.missing_required(), ("prompt_text",))

    def test_missing_session_id_yields_failing_required(self):
        req = codex_request_from_native({"prompt": "p"})
        self.assertEqual(req.session_id, "")
        self.assertEqual(req.missing_required(), ("session_id",))

    def test_missing_both_flags_both(self):
        req = codex_request_from_native({})
        self.assertEqual(req.missing_required(), ("prompt_text", "session_id"))
        self.assertEqual(dict(req.native), {})

    def test_whitespace_prompt_preserved_verbatim_and_flagged(self):
        req = codex_request_from_native({"prompt": "  \n\t ", "session_id": "s"})
        self.assertEqual(req.prompt_text, "  \n\t ")  # verbatim, not coerced
        self.assertEqual(req.missing_required(), ("prompt_text",))

    def test_non_str_prompt_passes_through_and_flagged(self):
        for bad in (42, None, {"nested": "x"}):
            with self.subTest(prompt=bad):
                req = codex_request_from_native({"prompt": bad, "session_id": "s"})
                self.assertEqual(req.prompt_text, bad)  # faithful passthrough
                self.assertEqual(req.missing_required(), ("prompt_text",))
                self.assertEqual(req.native["prompt"], bad)

    def test_non_str_session_id_passes_through_and_flagged(self):
        for bad in (None, 42):
            with self.subTest(session_id=bad):
                req = codex_request_from_native({"prompt": "p", "session_id": bad})
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
                req = codex_request_from_native(payload)
                self.assertIsNone(req.cwd)
                if "cwd" in payload:
                    # Lossy projection never destroys the original value.
                    self.assertEqual(req.native["cwd"], payload["cwd"])

    def test_valid_cwd_projects_to_path(self):
        req = codex_request_from_native({"prompt": "p", "session_id": "s", "cwd": "/tmp/x"})
        self.assertEqual(req.cwd, Path("/tmp/x"))

    def test_non_str_or_absent_hook_event_is_none(self):
        for payload in (
            {"prompt": "p", "session_id": "s"},
            {"prompt": "p", "session_id": "s", "hook_event_name": 42},
            {"prompt": "p", "session_id": "s", "hook_event_name": None},
        ):
            with self.subTest(hook_event=payload.get("hook_event_name", "<absent>")):
                req = codex_request_from_native(payload)
                self.assertIsNone(req.hook_event)
        # A blank string IS a string: faithful passthrough, no coercion.
        req = codex_request_from_native({"prompt": "p", "session_id": "s", "hook_event_name": ""})
        self.assertEqual(req.hook_event, "")

    def test_non_mapping_payload_degrades_to_empty_request(self):
        for bad in ([1, 2], "a json string", None, 42):
            with self.subTest(payload=bad):
                req = codex_request_from_native(bad)  # type: ignore[arg-type]
                self.assertEqual(req.prompt_text, "")
                self.assertEqual(req.session_id, "")
                self.assertEqual(req.harness, "codex")
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
        req = codex_request_from_native(payload)
        self.assertEqual(list(req.native), list(payload))  # order preserved
        self.assertEqual(req.native["future_field"], "v2.2 adds me")
        self.assertEqual(req.native["count"], 7)
        self.assertIs(req.native["nested"], nested)  # shallow: same object

    def test_harness_kwarg_default_and_override(self):
        self.assertEqual(codex_request_from_native({"prompt": "p", "session_id": "s"}).harness, "codex")
        req = codex_request_from_native({"prompt": "p", "session_id": "s"}, harness="claude")
        self.assertEqual(req.harness, "claude")

    def test_native_is_shallow_copy_not_alias(self):
        payload = {"prompt": "p", "session_id": "s"}
        req = codex_request_from_native(payload)
        payload["added_after_parse"] = True
        self.assertNotIn("added_after_parse", req.native)


# ---------------------------------------------------------------------------
# 4. Renderer (DOCUMENTED-NOT-OBSERVED — no real capture to prove byte shape)
# ---------------------------------------------------------------------------

class RendererTest(unittest.TestCase):
    def test_injection_envelope_shape_and_roundtrip(self):
        markdown = '## Recalled decision\n\n- "quoted" — π 🧠\n'
        text, code = codex_native_response_from_result(_injection(markdown=markdown))
        self.assertEqual(code, 0)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text.count("\n"), 1)  # single wire line + terminal newline
        envelope = json.loads(text)
        # DOCUMENTED-NOT-OBSERVED: this exact envelope shape
        # ("hookSpecificOutput" > "hookEventName"/"additionalContext") is the
        # documented spec inherited from the "near-1:1 with Claude Code"
        # claim, not proven against a live Codex capture.
        self.assertEqual(list(envelope), ["hookSpecificOutput"])
        inner = envelope["hookSpecificOutput"]
        self.assertEqual(list(inner), ["hookEventName", "additionalContext"])
        self.assertEqual(inner["hookEventName"], "UserPromptSubmit")
        self.assertEqual(inner["additionalContext"], markdown)

    def test_hook_event_name_override(self):
        text, code = codex_native_response_from_result(_injection(), hook_event_name="PreToolUse")
        self.assertEqual(code, 0)
        # DOCUMENTED-NOT-OBSERVED: hookEventName is assumed passthrough-able
        # the same way Claude's encoder does it; not codex-verified.
        self.assertEqual(json.loads(text)["hookSpecificOutput"]["hookEventName"], "PreToolUse")

    def test_no_op_renders_empty_stdout_every_reason(self):
        for reason in sorted(NO_OP_REASONS):
            with self.subTest(reason=reason):
                result = InjectionNoOp(reason=reason, detail="x", evidence={"k": "v"})
                self.assertEqual(codex_native_response_from_result(result), ("", 0))

    def test_backend_failure_renders_empty_stdout_every_kind(self):
        for failure in sorted(FAILURE_KINDS):
            with self.subTest(failure=failure):
                result = InjectionBackendFailure(failure=failure, detail="boom", evidence={"k": "v"})
                self.assertEqual(codex_native_response_from_result(result), ("", 0))

    def test_non_result_garbage_renders_silent(self):
        # A defective caller cannot smuggle text: model_visible_text() is None
        # for non-results, so the wire stays empty and exits 0.
        for garbage in ("a string", None, 42, {"markdown": "x"}):
            with self.subTest(garbage=garbage):
                self.assertEqual(
                    codex_native_response_from_result(garbage),  # type: ignore[arg-type]
                    ("", 0),
                )

    def test_exit_code_zero_on_every_path(self):
        results = [_injection(), "garbage", None]
        results += [InjectionNoOp(reason=r) for r in sorted(NO_OP_REASONS)]
        results += [InjectionBackendFailure(failure=f, detail="d") for f in sorted(FAILURE_KINDS)]
        for result in results:
            with self.subTest(result=type(result).__name__):
                text, code = codex_native_response_from_result(result)  # type: ignore[arg-type]
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
            req = codex_request_from_native({})
            result = provider.invoke(req)
            return result, codex_native_response_from_result(result)

        result, wire = self._run_captured(flow)
        self.assertIsInstance(result, InjectionNoOp)
        self.assertEqual(result.reason, "missing_required_field")
        self.assertEqual(result.detail, "prompt_text,session_id")
        self.assertEqual(provider.calls, [])  # invoke short-circuited retrieve
        self.assertEqual(wire, ("", 0))

    def test_synthetic_payload_flows_to_injected_wire(self):
        inj = _injection()
        provider = _CannedProvider(outcome=inj)

        def flow():
            req = codex_request_from_native(_SYNTHETIC_STDIN)
            result = provider.invoke(req)
            return req, result, codex_native_response_from_result(result)

        req, result, (text, code) = self._run_captured(flow)
        self.assertIs(result, inj)
        self.assertEqual(provider.calls, [req])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(text)["hookSpecificOutput"]["additionalContext"], inj.markdown
        )


if __name__ == "__main__":
    unittest.main()
