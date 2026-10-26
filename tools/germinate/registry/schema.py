"""Closed vocabulary of the ORG-081 system registry (schema pantheon-system-registry/v0).

Constants only: every set below is CLOSED. A value outside it is a validation
error (ORG-081 §2 "enum bytes are exact"), never a warning.
"""

from __future__ import annotations

import re

SCHEMA_ID = "pantheon-system-registry/v0"
ALIASES_SCHEMA_ID = "pantheon-registry-aliases/v0"

TOP_LEVEL_KEYS = frozenset({"schema", "systems"})

KIND_COMPONENT = "component"
KIND_EXTERNAL_PLANE = "external-plane"
KINDS = (KIND_COMPONENT, KIND_EXTERNAL_PLANE)

# ORG-081 §6 / D-12: exact field sets per discriminated shape.
COMPONENT_REQUIRED = frozenset({"kind", "id", "title", "steward", "contract", "contract_adoption"})
COMPONENT_OPTIONAL = frozenset(
    {"adr", "adoption_evidence", "adoption_gaps", "legs", "legacy_coverage", "note"}
)
EXTERNAL_REQUIRED = frozenset({"kind", "id", "title", "steward", "adr", "tracking", "relationship"})
EXTERNAL_OPTIONAL = frozenset({"note"})

CONTRACT_REQUIRED = frozenset({"repo", "module", "digest"})
CONTRACT_OPTIONAL = frozenset({"repo_revision", "unpinned_reason"})
INTERNAL_REPO = "pantheon-config"

# PROVISIONAL reading (T-5073, pending orchestrator ratification in the MR): no
# component contract module exists yet, so a not_adopted row whose module is
# absent carries this sentinel plus a required `unpinned_reason`. Changing this
# reading touches only this constant and its two readers, validate._check_contract
# (shape) and validate.check_refs (module-absent check).
DIGEST_UNPINNED = "unpinned"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")

ADOPTION_NOT_ADOPTED = "not_adopted"
ADOPTION_PARTIAL = "partial"
ADOPTION_ADOPTED = "adopted"
ADOPTION_VALUES = (ADOPTION_NOT_ADOPTED, ADOPTION_PARTIAL, ADOPTION_ADOPTED)

# ORG-068's five legs, in matrix column order.
LEGS = ("install", "configure", "health", "update", "remove")

# ORG-081 §2: closed status enum and the fields each status requires.
STATUS_COVERED = "covered"
STATUS_PARTIAL = "partial"
STATUS_MISSING = "missing"
STATUS_PARTIAL_BY_DESIGN = "partial_by_design"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_FIELDS = {
    STATUS_COVERED: frozenset({"evidence"}),
    STATUS_PARTIAL: frozenset({"gap", "ticket"}),
    STATUS_MISSING: frozenset({"gap", "ticket"}),
    STATUS_PARTIAL_BY_DESIGN: frozenset({"waiver"}),
    STATUS_NOT_APPLICABLE: frozenset({"waiver"}),
}
STATUSES = tuple(STATUS_FIELDS)
CELL_OPTIONAL = frozenset({"note"})
WAIVED_STATUSES = frozenset({STATUS_PARTIAL_BY_DESIGN, STATUS_NOT_APPLICABLE})

# ORG-081 §4 / D-11: a covered health cell proves all three wiring predicates.
HEALTH_EVIDENCE_KEYS = frozenset({"emitter", "invariant_binding", "collector_alert"})
# D-11: an explicit partial_by_design health waiver acknowledges E9.C3 non-cover.
HEALTH_WAIVER_MARKER = "E9.C3"

# ORG-081 §1: the seven adoption_evidence fields.
EVIDENCE_FIELDS = ("plan", "apply", "receipt", "rollback", "crash_contract", "gates", "emitter")

# ORG-081 §1 / D-8: the closed 16-key adoption_gaps predicate set (5 + 1 + 7 + 3).
PLAN_METHODS = ("plan_install", "plan_configure", "plan_update", "plan_repair", "plan_remove")
PREDICATE_KEYS = frozenset(
    PLAN_METHODS
    + ("verify_contract",)
    + tuple(f"adoption_evidence.{f}" for f in EVIDENCE_FIELDS)
    + ("crash_contract", "sandbox", "receipt_schema")
)
assert len(PREDICATE_KEYS) == 16
# Mandatory for `partial`, so never legal as a gap entry (ORG-081 §3).
PARTIAL_MANDATORY = frozenset({"plan_install", "plan_remove"})
GAP_ENTRY_KEYS = frozenset({"gap", "ticket"})

ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
TICKET_RE = re.compile(r"^T-\d{4,}$")
ADR_ID_RE = re.compile(r"^ORG-\d{3,}$")
