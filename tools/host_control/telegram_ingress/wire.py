"""Ingress-side wire constants (T-2919).

DUPLICATED from ``tools.host_control.approval.wire`` / ``approval.human``
on purpose: importing those would pull ``tools.host_control.contracts``
-> ``tools.persona_installer`` -> ``tools.codex_telegram_bridge`` into the
ingress process (credential/module isolation, gate 14). Equality with the
approval-side constants is pinned by test, so the copies cannot drift.
"""

from __future__ import annotations

# exported BY the ingress, callable only by the approval uid
INGRESS_BUS_NAME = "org.pantheon.HostApprovalTelegram1"
INGRESS_OBJECT_PATH = "/org/pantheon/HostApprovalTelegram1"
INGRESS_INTERFACE = "org.pantheon.HostApprovalTelegram1.Ingress"
INGRESS_ERROR_NAMESPACE = "org.pantheon.HostApprovalTelegram1.Error"
INGRESS_POLICY_DENIED_ERROR = f"{INGRESS_ERROR_NAMESPACE}.PolicyDenied"
INGRESS_INVALID_ERROR = f"{INGRESS_ERROR_NAMESPACE}.Invalid"
INGRESS_CONFLICT_ERROR = f"{INGRESS_ERROR_NAMESPACE}.Conflict"
INGRESS_UNAVAILABLE_ERROR = f"{INGRESS_ERROR_NAMESPACE}.Unavailable"

# the approval service's HumanIngress (called BY the ingress)
APPROVAL_BUS_NAME = "org.pantheon.HostApproval1"
APPROVAL_OBJECT_PATH = "/org/pantheon/HostApproval1"
HUMAN_INGRESS_INTERFACE = "org.pantheon.HostApproval1.HumanIngress"
APPROVAL_ERROR_NAMESPACE = "org.pantheon.HostApproval1.Error"

# the bounded card payload (Present slot 3)
CARD_SCHEMA = "pantheon-approval-human-card/v1"
CARD_KEYS = frozenset(
    (
        "schema",
        "review_handle",
        "plan_digest",
        "target_seat_id",
        "operation_families",
        "redacted_effects",
        "expires_at",
    )
)
MAX_CARD_SUMMARY_LENGTH = 4096
# aligned with the approval wire JSON limit: the summary is canonical
# ASCII-escaped JSON, so one astral character costs 12 bytes (review finding 5)
MAX_SUMMARY_JSON_BYTES = 65536
