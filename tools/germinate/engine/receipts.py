"""The pantheon-receipt/v1 per-op receipt body (ORG-080 §2 ``receipts: schema``).

No third receipt vocabulary: the body is persona_installer's ``MutationRecord``
and ``OperationEvidence`` (the same shapes ``host_control.OperationReceipt``
wraps on the broker path), plus the engine bindings ORG-080 needs. It travels
as the ``receipt_payload`` of a T-3336 WAL receipt, so the WAL's intent-digest
binding and immutability apply to it unchanged. Broker-only fields (plan
handle, subject, approval) are absent: on the broker path the broker journal
stays the receipt owner (T-2917 5a).
"""

from __future__ import annotations

from typing import Any, Mapping

from tools.persona_installer import MutationRecord, OperationEvidence

SCHEMA = "pantheon-receipt/v1"
BODY_KEYS = frozenset(
    {
        "schema",
        "run_id",
        "op_id",
        "component",
        "leg",
        "crash_contract",
        "plan_digest",
        "contract_digest",
        "op_registry_digest",
        "evidence_scope",
        "mutation",
        "evidence",
    }
)


class ReceiptError(ValueError):
    pass


def mutation_to_json_obj(m: MutationRecord) -> dict[str, Any]:
    return {
        "after_fingerprint": m.after_fingerprint,
        "before": dict(m.before),
        "changed": m.changed,
        "kind": m.kind,
        "operation_id": m.operation_id,
        "owner": m.owner,
        "target": m.target,
    }


def mutation_from_json_obj(raw: Mapping[str, Any]) -> MutationRecord:
    try:
        return MutationRecord(
            operation_id=raw["operation_id"],
            kind=raw["kind"],
            target=raw["target"],
            owner=raw["owner"],
            changed=raw["changed"],
            before=dict(raw["before"]),
            after_fingerprint=raw["after_fingerprint"],
        )
    except (KeyError, TypeError) as exc:
        raise ReceiptError(f"malformed mutation record: {exc}") from None


def evidence_to_json_obj(e: OperationEvidence) -> dict[str, Any]:
    return {
        "detail": e.detail,
        "evidence": dict(e.evidence),
        "kind": e.kind,
        "layer": e.layer,
        "ok": e.ok,
        "operation_id": e.operation_id,
    }


def build_body(
    *,
    run_id: str,
    op_id: str,
    component: str,
    leg: str,
    crash_contract: str | None,
    plan_digest: str,
    contract_digest: str,
    op_registry_digest: str,
    evidence_scope: str,
    mutation: MutationRecord,
    evidence: OperationEvidence,
) -> dict[str, Any]:
    if mutation.operation_id != op_id or evidence.operation_id != op_id:
        raise ReceiptError("mutation/evidence op id disagrees with the receipt op id")
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "op_id": op_id,
        "component": component,
        "leg": leg,
        "crash_contract": crash_contract,
        "plan_digest": plan_digest,
        "contract_digest": contract_digest,
        "op_registry_digest": op_registry_digest,
        "evidence_scope": evidence_scope,
        "mutation": mutation_to_json_obj(mutation),
        "evidence": evidence_to_json_obj(evidence),
    }


def validate_body(body: Mapping[str, Any]) -> None:
    if set(body) != BODY_KEYS or body.get("schema") != SCHEMA:
        raise ReceiptError(f"receipt body is not {SCHEMA}")
    mutation_from_json_obj(body["mutation"])
