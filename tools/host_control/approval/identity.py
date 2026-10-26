"""Bus-uid to approver-subject resolution for the ``HostApproval1``
Reviewer identity gate (ORG-052 §2/§5, T-2913).

Value-level and dbus-free: the D-Bus tier resolves the live message
sender to a Unix uid and hands it here; this module owns the mapping of
that uid onto the root-owned approver policy. Identity is NEVER a
caller argument — the only input is the bus-attested uid.
"""

from __future__ import annotations

from tools.host_control.approval.policy import ApproverPolicy


class ApproverResolver:
    """Resolve a live bus-derived uid to exactly one approver subject.

    Candidates are the intersection of the approver ROSTER with the
    uid's subject labels (the T-2913 plan refinement): a uid that also
    carries non-approver subject labels is NOT ambiguous — those labels
    grant no reviewer standing and take none away, and the uid-level
    trust-domain separation at the policy tier (gate 11/13) still
    evaluates the shared uid.

    Zero candidates (unregistered uid — the requester-only case, gate
    5) and more than one (ambiguous policy authoring) both return
    ``None``; the transport refuses both uniformly as an authorization
    verdict, byte-indistinguishable on the wire (the T-2912 C1
    non-disclosure posture: an unauthorized caller learns nothing about
    policy topology, not even that it is ambiguous).
    """

    def __init__(self, policy: ApproverPolicy) -> None:
        self._policy = policy

    def subject_for_uid(self, uid: object) -> str | None:
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
            return None
        # T-2919 Q4: a HUMAN-decision-source approver has no Reviewer
        # standing at all. Its only path is the provider-authenticated
        # HumanIngress; its policy uid is the ingress uid, which must never
        # open reviews or request cards. The harness registration gate
        # (host_mcp_registration.is_policy_approver) delegates here, so
        # generation and server share this one predicate.
        candidates = [
            subject
            for subject, entry in self._policy.approvers.items()
            if entry.decision_source == "agent"
            and self._policy.subject_uids.get(subject) == uid
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None
