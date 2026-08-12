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
    "PantheonHarnessAdapter",
    "ProviderBillingEvidence",
    "SessionHandle",
    "Usage",
]
