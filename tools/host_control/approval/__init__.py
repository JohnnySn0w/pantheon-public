"""Physically separate approval authority package (ORG-052 §5 — T-2910).

Owns durable review/grant truth on its OWN journal file. The broker package
(`tools.host_control`) deliberately does not import this tree and this tree
deliberately does not import the broker's journal/operations/planner
modules; the only shared surface is the value-level contracts module
(`BindOutcome`, pure helpers). See `approval/contracts.py` for the
precisely-labeled separation claim.
"""

from tools.host_control.approval.contracts import (
    ApprovalCounts,
    ApprovalStatus,
    DecisionResult,
    GrantRecord,
    HandoffLink,
    PlanMetadata,
    ReviewRecord,
)
from tools.host_control.approval.journal import ApprovalJournal
from tools.host_control.approval.service import (
    DENY_ALL_AUTHORIZER,
    ApprovalService,
    ApproverAuthorizer,
    ConsumerBinder,
    PlanMetadataSource,
    default_handle_factory,
)

__all__ = (
    "ApprovalCounts",
    "ApprovalJournal",
    "ApprovalService",
    "ApprovalStatus",
    "ApproverAuthorizer",
    "ConsumerBinder",
    "DENY_ALL_AUTHORIZER",
    "DecisionResult",
    "GrantRecord",
    "HandoffLink",
    "PlanMetadata",
    "PlanMetadataSource",
    "ReviewRecord",
    "default_handle_factory",
)
