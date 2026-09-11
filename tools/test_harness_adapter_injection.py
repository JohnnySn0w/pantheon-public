"""Unit tests for tools/harness_adapter/injection.py — the Concordance injection
contract (ORG-073 §4; T-3240).

Run from repo root:
    python3 -m unittest tools.test_harness_adapter_injection

Coverage:
  1. Package exports — new names present; pre-existing names still exported
  2. Provider ABC — abstract retrieve(); invoke() is final and override-refused
  3. Request — frozen, required-vs-optional split, extensible native mapping,
     missing_required() reporting
  4. Negative cases — empty / whitespace / None / non-str prompt, missing
     session, non-request object, raising provider, provider returning a raw
     string or None, KeyboardInterrupt propagation
  5. Model-silence — no_op and backend_failure render to nothing model-visible
     and carry no text-bearing attribute
  6. Sum-type invariants — sealed, root not instantiable, closed Literal sets,
     Injection rejects blank markdown / empty sources / superseded sources
  7. Round-trips — dataclasses.replace / copy / deepcopy / pickle of each of the
     three sanctioned result classes survive the seal
"""

from __future__ import annotations

import copy
import dataclasses
import pickle
import unittest
from pathlib import Path

from tools import harness_adapter
from tools.harness_adapter import (
    HookInjectionProvider,
    Injection,
    InjectionBackendFailure,
    InjectionNoOp,
    InjectionRequest,
    InjectionResult,
    SourceRecord,
    model_visible_text,
)
from tools.harness_adapter.injection import (
    CORPUS_CLASSES,
    FAILURE_KINDS,
    NO_OP_REASONS,
    SUPERSESSION_STATES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The package surface as it stood BEFORE this module was added. Asserted as a
# SUBSET of __all__ (not equality) so a future legitimate export in an unrelated
# ticket does not trip this test (po-T-3006 directive A).
_PRE_EXISTING_EXPORTS = (
    "ClaudeHarnessAdapter",
    "CodexHarnessAdapter",
    "CommandSpec",
    "DeliveryReceipt",
    "DiagnosticObservation",
    "DiagnosticReport",
    "ExecutionError",
    "ExecutionRequest",
    "ExecutionResult",
    "HarnessCapabilities",
    "HarnessEvent",
    "HarnessRunner",
    "HookCapability",
    "InboundRequest",
    "integration",
    "PantheonHarnessAdapter",
    "ProviderBillingEvidence",
    "SessionHandle",
    "Usage",
)

_NEW_EXPORTS = (
    "CorpusClass",
    "FailureKind",
    "HookInjectionProvider",
    "Injection",
    "InjectionBackendFailure",
    "InjectionKind",
    "InjectionNoOp",
    "InjectionRequest",
    "InjectionResult",
    "NoOpReason",
    "SourceRecord",
    "SupersessionState",
    "model_visible_text",
)


def _request(**overrides) -> InjectionRequest:
    fields = {"prompt_text": "how do we handle superseded ADRs?", "session_id": "sess-1"}
    fields.update(overrides)
    return InjectionRequest(**fields)


def _source(**overrides) -> SourceRecord:
    fields = {
        "source_id": "ORG-073",
        "corpus_class": "adr",
        "path": "adr/ORG-073-information-flow-retrieval-injection-layer.md",
        "rank": 1,
        "supersession": "current",
        "git_sha": "ec411c0af",
        "locator": "L88-L110",
        "content_sha256": "0" * 64,
        "excerpt": "Retrieval returns a model-silent no-op when ...",
        "title": "Concordance — retrieval-policy and injection layer",
        "observed_at": "2026-08-21T00:00:00Z",
        "score": 0.0164,
    }
    fields.update(overrides)
    return SourceRecord(**fields)


def _injection(**overrides) -> Injection:
    fields = {"markdown": "Recalled decision (verify it still applies): ORG-073", "sources": (_source(),)}
    fields.update(overrides)
    return Injection(**fields)


class _SpyProvider(HookInjectionProvider):
    """Records every retrieve() call; returns a canned outcome or raises."""

    def __init__(self, outcome=None, raise_exc: BaseException | None = None):
        self.calls: list = []
        self._outcome = outcome
        self._raise = raise_exc

    def retrieve(self, request: InjectionRequest) -> InjectionResult:
        self.calls.append(request)
        if self._raise is not None:
            raise self._raise
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
        # Subset, not equality — see _PRE_EXISTING_EXPORTS comment.
        self.assertTrue(set(_PRE_EXISTING_EXPORTS) <= set(harness_adapter.__all__))
        for name in _PRE_EXISTING_EXPORTS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(harness_adapter, name), name)


# ---------------------------------------------------------------------------
# 2. Provider ABC
# ---------------------------------------------------------------------------

class ProviderAbcTest(unittest.TestCase):
    def test_provider_is_abstract(self):
        with self.assertRaises(TypeError):
            HookInjectionProvider()  # type: ignore[abstract]

    def test_subclass_must_implement_retrieve(self):
        class Incomplete(HookInjectionProvider):
            pass

        with self.assertRaises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_subclass_cannot_override_invoke(self):
        with self.assertRaises(TypeError):
            class Rogue(HookInjectionProvider):  # noqa: F841
                def retrieve(self, request):
                    return InjectionNoOp(reason="no_candidates")

                def invoke(self, request):
                    return "smuggled text"

    def test_invoke_is_marked_final(self):
        self.assertTrue(getattr(HookInjectionProvider.invoke, "__final__", False))

    def test_invoke_calls_retrieve_once_with_same_request(self):
        req = _request()
        provider = _SpyProvider(outcome=InjectionNoOp(reason="empty_corpus"))
        provider.invoke(req)
        self.assertEqual(len(provider.calls), 1)
        self.assertIs(provider.calls[0], req)


# ---------------------------------------------------------------------------
# 3. Request
# ---------------------------------------------------------------------------

class RequestTest(unittest.TestCase):
    def test_request_constructs_with_required_only(self):
        req = InjectionRequest(prompt_text="p", session_id="s")
        self.assertEqual(req.prompt_text, "p")
        self.assertEqual(req.session_id, "s")
        self.assertIsNone(req.harness)
        self.assertIsNone(req.hook_event)
        self.assertIsNone(req.cwd)
        self.assertIsNone(req.repo_identity)
        self.assertIsNone(req.role_domain)
        self.assertEqual(dict(req.native), {})
        self.assertEqual(req.missing_required(), ())

    def test_request_carries_optional_context(self):
        req = _request(
            harness="claude",
            hook_event="UserPromptSubmit",
            cwd=Path("/tmp/x"),
            repo_identity="pantheon-config",
            role_domain="orchestrator",
        )
        self.assertEqual(req.harness, "claude")
        self.assertEqual(req.hook_event, "UserPromptSubmit")
        self.assertEqual(req.cwd, Path("/tmp/x"))
        self.assertEqual(req.repo_identity, "pantheon-config")
        self.assertEqual(req.role_domain, "orchestrator")

    def test_request_is_frozen(self):
        req = _request()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            req.prompt_text = "changed"  # type: ignore[misc]

    def test_native_mapping_carries_unmodeled_keys(self):
        # The native stdin schema has not been observed yet; whatever the
        # encoder captures must ride along without a contract change.
        native = {"transcript_path": "/x/y.jsonl", "permission_mode": "default", "nested": {"a": 1}}
        req = _request(native=native)
        self.assertEqual(req.native["transcript_path"], "/x/y.jsonl")
        self.assertEqual(req.native["nested"], {"a": 1})

    def test_missing_required_reports_field_names(self):
        self.assertEqual(_request(prompt_text="").missing_required(), ("prompt_text",))
        self.assertEqual(_request(session_id="").missing_required(), ("session_id",))
        self.assertEqual(
            _request(prompt_text="", session_id="").missing_required(),
            ("prompt_text", "session_id"),
        )


# ---------------------------------------------------------------------------
# 4. Negative cases through invoke()
# ---------------------------------------------------------------------------

class InvokeNegativeTest(unittest.TestCase):
    def _assert_missing_no_op(self, result, *fields):
        self.assertIsInstance(result, InjectionNoOp)
        self.assertEqual(result.kind, "no_op")
        self.assertEqual(result.reason, "missing_required_field")
        self.assertEqual(result.detail, ",".join(fields))
        self.assertIsNone(model_visible_text(result))

    def test_empty_prompt_yields_typed_no_op_and_skips_retrieve(self):
        provider = _SpyProvider(outcome=_injection())
        result = provider.invoke(_request(prompt_text=""))
        self._assert_missing_no_op(result, "prompt_text")
        self.assertEqual(provider.calls, [])

    def test_whitespace_prompt_is_missing(self):
        provider = _SpyProvider(outcome=_injection())
        result = provider.invoke(_request(prompt_text="  \n\t "))
        self._assert_missing_no_op(result, "prompt_text")
        self.assertEqual(provider.calls, [])

    def test_none_prompt_yields_typed_no_op(self):
        # An encoder that did data.get("prompt") on a payload lacking the key.
        provider = _SpyProvider(outcome=_injection())
        result = provider.invoke(_request(prompt_text=None))  # type: ignore[arg-type]
        self._assert_missing_no_op(result, "prompt_text")

    def test_non_string_prompt_is_missing(self):
        provider = _SpyProvider(outcome=_injection())
        result = provider.invoke(_request(prompt_text=42))  # type: ignore[arg-type]
        self._assert_missing_no_op(result, "prompt_text")

    def test_missing_session_yields_typed_no_op(self):
        provider = _SpyProvider(outcome=_injection())
        for bad in ("", "   ", None):
            with self.subTest(session_id=bad):
                result = provider.invoke(_request(session_id=bad))  # type: ignore[arg-type]
                self._assert_missing_no_op(result, "session_id")
        self.assertEqual(provider.calls, [])

    def test_both_missing_lists_both_fields(self):
        provider = _SpyProvider(outcome=_injection())
        result = provider.invoke(_request(prompt_text="", session_id=None))  # type: ignore[arg-type]
        self._assert_missing_no_op(result, "prompt_text", "session_id")

    def test_non_request_yields_backend_failure_invalid_request(self):
        provider = _SpyProvider(outcome=_injection())
        for bad in (None, "a prompt string", {"prompt": "x", "session_id": "s"}):
            with self.subTest(request=bad):
                result = provider.invoke(bad)  # type: ignore[arg-type]
                self.assertIsInstance(result, InjectionBackendFailure)
                self.assertEqual(result.kind, "backend_failure")
                self.assertEqual(result.failure, "invalid_request")
                self.assertIn(type(bad).__name__, result.detail)
                self.assertIsNone(model_visible_text(result))
        self.assertEqual(provider.calls, [])

    def test_provider_raise_yields_backend_failure_no_exception_escapes(self):
        provider = _SpyProvider(raise_exc=RuntimeError("faiss index unreachable"))
        result = provider.invoke(_request())
        self.assertIsInstance(result, InjectionBackendFailure)
        self.assertEqual(result.failure, "provider_exception")
        self.assertIn("RuntimeError", result.detail)
        self.assertIn("faiss index unreachable", result.detail)
        self.assertEqual(result.evidence["exception_type"], "RuntimeError")
        self.assertIn("faiss index unreachable", result.evidence["traceback"])
        self.assertEqual(result.evidence["provider"], "_SpyProvider")
        self.assertIsNone(model_visible_text(result))

    def test_provider_raise_with_unprintable_exception_still_typed(self):
        class Unprintable(Exception):
            def __str__(self):
                raise ValueError("nope")

        provider = _SpyProvider(raise_exc=Unprintable())
        result = provider.invoke(_request())
        self.assertIsInstance(result, InjectionBackendFailure)
        self.assertEqual(result.failure, "provider_exception")
        self.assertEqual(result.evidence["exception_type"], "Unprintable")

    def test_provider_returning_raw_string_yields_contract_violation(self):
        # The "smuggle text" path: a provider returning markdown directly can
        # never reach the model — it becomes a silent, typed failure.
        provider = _SpyProvider(outcome="Recalled decision: ORG-073 ...")
        result = provider.invoke(_request())
        self.assertIsInstance(result, InjectionBackendFailure)
        self.assertEqual(result.failure, "contract_violation")
        self.assertIn("str", result.detail)
        self.assertIsNone(model_visible_text(result))

    def test_provider_returning_none_yields_contract_violation(self):
        provider = _SpyProvider(outcome=None)
        result = provider.invoke(_request())
        self.assertIsInstance(result, InjectionBackendFailure)
        self.assertEqual(result.failure, "contract_violation")
        self.assertIn("NoneType", result.detail)

    def test_keyboard_interrupt_propagates(self):
        # Process-control signals belong to the hook harness (timeout kill);
        # the guard must not swallow them into a backend_failure.
        provider = _SpyProvider(raise_exc=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            provider.invoke(_request())

    def test_system_exit_propagates(self):
        provider = _SpyProvider(raise_exc=SystemExit(3))
        with self.assertRaises(SystemExit):
            provider.invoke(_request())

    def test_inject_passthrough_preserves_sources_as_data(self):
        inj = _injection()
        provider = _SpyProvider(outcome=inj)
        result = provider.invoke(_request())
        self.assertIs(result, inj)
        self.assertIsInstance(result, Injection)
        self.assertEqual(result.kind, "inject")
        self.assertEqual(model_visible_text(result), inj.markdown)
        src = result.sources[0]
        self.assertIsInstance(src, SourceRecord)
        self.assertEqual(src.source_id, "ORG-073")
        self.assertEqual(src.git_sha, "ec411c0af")
        self.assertEqual(src.content_sha256, "0" * 64)
        self.assertEqual(src.locator, "L88-L110")


# ---------------------------------------------------------------------------
# 5. Model-silence
# ---------------------------------------------------------------------------

_TEXT_BEARING_ATTRS = ("markdown", "sources", "content", "text", "additional_context")


class ModelSilenceTest(unittest.TestCase):
    def test_no_op_renders_nothing_model_visible(self):
        for reason in sorted(NO_OP_REASONS):
            with self.subTest(reason=reason):
                result = InjectionNoOp(reason=reason, detail="x", evidence={"k": "v"})
                self.assertIsNone(model_visible_text(result))
                for attr in _TEXT_BEARING_ATTRS:
                    self.assertFalse(hasattr(result, attr), attr)

    def test_backend_failure_renders_nothing_model_visible(self):
        for failure in sorted(FAILURE_KINDS):
            with self.subTest(failure=failure):
                result = InjectionBackendFailure(failure=failure, detail="boom", evidence={"k": "v"})
                self.assertIsNone(model_visible_text(result))
                for attr in _TEXT_BEARING_ATTRS:
                    self.assertFalse(hasattr(result, attr), attr)

    def test_injection_renders_its_markdown(self):
        inj = _injection(markdown="## Recalled decision\n\n- ORG-073 ...")
        self.assertEqual(model_visible_text(inj), "## Recalled decision\n\n- ORG-073 ...")

    def test_model_visible_text_of_non_result_is_none(self):
        self.assertIsNone(model_visible_text("a string"))  # type: ignore[arg-type]
        self.assertIsNone(model_visible_text(None))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 6. Sum-type invariants
# ---------------------------------------------------------------------------

class SumTypeTest(unittest.TestCase):
    def test_kind_discriminators_and_isinstance(self):
        inj = _injection()
        noop = InjectionNoOp(reason="empty_corpus")
        fail = InjectionBackendFailure(failure="backend_unavailable", detail="timeout")
        self.assertEqual(Injection.kind, "inject")
        self.assertEqual(InjectionNoOp.kind, "no_op")
        self.assertEqual(InjectionBackendFailure.kind, "backend_failure")
        for r in (inj, noop, fail):
            self.assertIsInstance(r, InjectionResult)
        self.assertEqual({inj.kind, noop.kind, fail.kind}, {"inject", "no_op", "backend_failure"})

    def test_result_root_not_instantiable(self):
        with self.assertRaises(TypeError):
            InjectionResult()

    def test_result_sum_type_is_sealed(self):
        with self.assertRaises(TypeError):
            class Fourth(InjectionResult):  # noqa: F841
                pass
        with self.assertRaises(TypeError):
            class ChattyNoOp(InjectionNoOp):  # noqa: F841
                markdown: str = "I found nothing relevant"

    def test_results_are_frozen(self):
        noop = InjectionNoOp(reason="empty_corpus")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            noop.reason = "no_candidates"  # type: ignore[misc]
        inj = _injection()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            inj.markdown = "x"  # type: ignore[misc]

    def test_no_op_reason_closed_set(self):
        for reason in sorted(NO_OP_REASONS):
            InjectionNoOp(reason=reason)
        for bad in ("nothing_relevant", "", None, 3):
            with self.subTest(reason=bad):
                with self.assertRaises(ValueError):
                    InjectionNoOp(reason=bad)  # type: ignore[arg-type]

    def test_no_op_reasons_match_org_073_section_3(self):
        self.assertEqual(
            NO_OP_REASONS,
            frozenset({
                "missing_required_field",
                "empty_corpus",
                "no_candidates",
                "all_superseded",
                "all_seen_this_session",
                "provenance_unestablished",
            }),
        )

    def test_failure_kind_closed_set(self):
        for failure in sorted(FAILURE_KINDS):
            InjectionBackendFailure(failure=failure, detail="d")
        for bad in ("oops", "", None):
            with self.subTest(failure=bad):
                with self.assertRaises(ValueError):
                    InjectionBackendFailure(failure=bad, detail="d")  # type: ignore[arg-type]

    def test_injection_rejects_blank_markdown(self):
        for bad in ("", "   \n", None, 7):
            with self.subTest(markdown=bad):
                with self.assertRaises(ValueError):
                    _injection(markdown=bad)  # type: ignore[arg-type]

    def test_injection_rejects_empty_sources(self):
        with self.assertRaises(ValueError):
            _injection(sources=())
        with self.assertRaises(ValueError):
            _injection(sources=[])  # type: ignore[arg-type]

    def test_injection_rejects_non_source_record_entries(self):
        with self.assertRaises(ValueError):
            _injection(sources=({"source_id": "ORG-073"},))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            _injection(sources="ORG-073")  # type: ignore[arg-type]

    def test_injection_rejects_superseded_source(self):
        # ORG-073 §3: a superseded decision never injects as current.
        with self.assertRaises(ValueError):
            _injection(sources=(_source(), _source(source_id="ORG-073-v1", rank=2, supersession="superseded")))

    def test_injection_accepts_unknown_supersession(self):
        # Legacy sources without machine-readable lineage are allowed at the
        # contract tier; the provider's policy may tighten this.
        inj = _injection(sources=(_source(supersession="unknown"),))
        self.assertEqual(inj.sources[0].supersession, "unknown")

    def test_injection_freezes_source_list_to_tuple(self):
        inj = _injection(sources=[_source()])  # type: ignore[arg-type]
        self.assertIsInstance(inj.sources, tuple)

    def test_source_record_validation(self):
        with self.assertRaises(ValueError):
            _source(source_id="")
        with self.assertRaises(ValueError):
            _source(source_id=None)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            _source(path="")
        with self.assertRaises(ValueError):
            _source(corpus_class="ticket")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            _source(supersession="retired")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            _source(rank=0)
        with self.assertRaises(ValueError):
            _source(rank="1")  # type: ignore[arg-type]

    def test_source_record_optional_provenance_defaults(self):
        src = SourceRecord(source_id="ledger:2026-W34-0012", corpus_class="ledger", path="ledger/2026-W34/2026-W34.jsonl", rank=3)
        self.assertEqual(src.supersession, "unknown")
        for attr in ("git_sha", "locator", "content_sha256", "excerpt", "title", "observed_at", "score"):
            self.assertIsNone(getattr(src, attr), attr)

    def test_closed_sets_derive_from_literals(self):
        self.assertEqual(CORPUS_CLASSES, frozenset({"adr", "ledger", "note"}))
        self.assertEqual(SUPERSESSION_STATES, frozenset({"current", "superseded", "unknown"}))
        self.assertEqual(
            FAILURE_KINDS,
            frozenset({"provider_exception", "contract_violation", "invalid_request", "backend_unavailable", "backend_error"}),
        )


# ---------------------------------------------------------------------------
# 7. Round-trips survive the seal (po-T-3006 directive B)
# ---------------------------------------------------------------------------

class RoundTripTest(unittest.TestCase):
    def _samples(self):
        return (
            _injection(),
            InjectionNoOp(reason="all_seen_this_session", detail="ORG-073", evidence={"seen": "3"}),
            InjectionBackendFailure(failure="backend_unavailable", detail="timeout", evidence={"ms": "30000"}),
        )

    def test_dataclasses_replace(self):
        for sample in self._samples():
            with self.subTest(kind=sample.kind):
                self.assertEqual(dataclasses.replace(sample), sample)
        inj = dataclasses.replace(_injection(), markdown="changed")
        self.assertEqual(inj.markdown, "changed")
        noop = dataclasses.replace(InjectionNoOp(reason="empty_corpus"), reason="no_candidates")
        self.assertEqual(noop.reason, "no_candidates")

    def test_copy_and_deepcopy(self):
        for sample in self._samples():
            with self.subTest(kind=sample.kind):
                self.assertEqual(copy.copy(sample), sample)
                clone = copy.deepcopy(sample)
                self.assertEqual(clone, sample)
                self.assertIs(type(clone), type(sample))

    def test_pickle_round_trip(self):
        for sample in self._samples():
            with self.subTest(kind=sample.kind):
                clone = pickle.loads(pickle.dumps(sample))
                self.assertEqual(clone, sample)
                self.assertIs(type(clone), type(sample))

    def test_request_round_trips(self):
        req = _request(cwd=Path("/tmp/x"), native={"k": "v"})
        self.assertEqual(dataclasses.replace(req), req)
        self.assertEqual(copy.deepcopy(req), req)
        self.assertEqual(pickle.loads(pickle.dumps(req)), req)

    def test_dataclasses_asdict_works_on_all_types(self):
        for sample in self._samples():
            with self.subTest(kind=sample.kind):
                self.assertIsInstance(dataclasses.asdict(sample), dict)
        self.assertIsInstance(dataclasses.asdict(_request(native={"k": "v"})), dict)


if __name__ == "__main__":
    unittest.main()
