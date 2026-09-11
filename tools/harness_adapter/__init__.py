"""Pantheon harness-adapter package (ORG-031 §i typed bindings).

Re-exports the public interface and both concrete bindings so callers can do:

    from tools.harness_adapter import PantheonHarnessAdapter
    from tools.harness_adapter import ClaudeHarnessAdapter, CodexHarnessAdapter

See tools/harness_adapter/base.py for the ORG-031 §i partition and the rationale
for each surface's classification (generalized / bridge / not-abstracted).
"""

from __future__ import annotations

from tools.harness_adapter.base import PantheonHarnessAdapter
from tools.harness_adapter.claude import ClaudeHarnessAdapter
from tools.harness_adapter.codex import CodexHarnessAdapter
from tools.harness_adapter.runner import HarnessRunner
from tools.harness_adapter import integration
from tools.harness_adapter.injection import (
    CorpusClass,
    FailureKind,
    HookInjectionProvider,
    Injection,
    InjectionBackendFailure,
    InjectionKind,
    InjectionNoOp,
    InjectionRequest,
    InjectionResult,
    NoOpReason,
    SourceRecord,
    SupersessionState,
    model_visible_text,
)
from tools.harness_adapter.injection_claude import (
    native_response_from_result,
    request_from_native,
)
from tools.harness_adapter.injection_codex import (
    native_response_from_result as codex_native_response_from_result,
    request_from_native as codex_request_from_native,
)
from tools.harness_adapter.types import (
    CommandSpec,
    DeliveryReceipt,
    DiagnosticObservation,
    DiagnosticReport,
    ExecutionError,
    ExecutionRequest,
    ExecutionResult,
    HarnessCapabilities,
    HarnessEvent,
    HookCapability,
    InboundRequest,
    ProviderBillingEvidence,
    SessionHandle,
    Usage,
)

__all__ = [
    "ClaudeHarnessAdapter",
    "CodexHarnessAdapter",
    "codex_native_response_from_result",
    "codex_request_from_native",
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
]
