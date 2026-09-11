from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Mapping, Protocol

from .artifacts import (
    INTENT_SCHEMA,
    MANIFEST_SCHEMA,
    RECEIPT_SCHEMA,
    ArtifactError,
    BytesPayload,
    IntentRecord,
    ManifestRecord,
    ReceiptRecord,
    RunReceiptEntry,
    intent_name,
    receipt_name,
    sha256_hex,
    validate_op_id,
    validate_receipt_ref,
    validate_run_key,
)
from .authority import AuthorityError, HeldDir


class WalStoreError(Exception):
    """Raised for refused PoC WAL store operations."""


class ReconcileError(Exception):
    """Raised when prefix classification must fail closed."""


class StructuralPrefix(Enum):
    BEFORE_INTENT = "before_intent"
    INTENT_ONLY = "intent_only"
    APPLIED_NO_RECEIPT = "applied_no_receipt"
    ORPHAN_RECEIPT = "orphan_receipt"
    MANIFEST_REFERENCED = "manifest_referenced"
    REFUSE = "refuse"


class EffectState(Enum):
    NOT_OBSERVED = "not_observed"
    UNKNOWN_REFUSE = "unknown_refuse"
    NOT_APPLIED = "not_applied"
    APPLIED = "applied"
    POSTCONDITION_SATISFIED = "postcondition_satisfied"
    POSTCONDITION_FAILED = "postcondition_failed"


@dataclass(frozen=True)
class EffectObservation:
    applied: bool | None
    postcondition_satisfied: bool | None
    evidence: Mapping[str, object]


class EffectProbe(Protocol):
    def observe(self, intent: IntentRecord) -> EffectObservation: ...


@dataclass(frozen=True)
class PrefixClassification:
    run_key: str
    op_id: str
    structural_prefix: StructuralPrefix
    effect_state: EffectState
    intent_ref: str | None
    intent_digest: str | None
    receipt_ref: str | None
    manifest_entry: RunReceiptEntry | None
    reason: str
    success_authorized: bool


class WalStore:
    def __init__(self, held_dir: HeldDir, *, run_key: str, expected_uid: int) -> None:
        self._dir = held_dir
        self._run_key = validate_run_key(run_key)
        self._expected_uid = expected_uid

    @classmethod
    def from_fd(
        cls,
        fd: int,
        *,
        run_key: str,
        expected_uid: int,
        expected_mode: int = 0o700,
    ) -> "WalStore":
        try:
            held_dir = HeldDir.from_fd(fd, expected_uid=expected_uid, expected_mode=expected_mode)
        except AuthorityError as exc:
            raise WalStoreError(str(exc)) from exc
        return cls(held_dir, run_key=run_key, expected_uid=expected_uid)

    def close(self) -> None:
        self._dir.close()

    def __enter__(self) -> "WalStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def initialize_manifest(self, *, created_at: str, updated_at: str) -> ManifestRecord:
        record = ManifestRecord(
            schema=MANIFEST_SCHEMA,
            run_key=self._run_key,
            receipts=(),
            created_at=created_at,
            updated_at=updated_at,
        )
        data = record.to_json_bytes()
        try:
            self._dir.write_immutable(
                "manifest.json",
                data,
                expected_uid=self._expected_uid,
                mode=0o600,
            )
            return record
        except FileExistsError:
            existing = self.read_manifest()
            if existing.to_json_bytes() == data:
                return existing
            raise WalStoreError("manifest-exists")
        except (AuthorityError, ArtifactError) as exc:
            raise WalStoreError(str(exc)) from exc

    def read_manifest(self) -> ManifestRecord:
        try:
            data = self._dir.read_regular(
                "manifest.json",
                expected_uid=self._expected_uid,
                expected_mode=0o600,
                max_bytes=65536,
            )
            record = ManifestRecord.from_json_bytes(data)
        except (AuthorityError, ArtifactError, Exception) as exc:
            if isinstance(exc, WalStoreError):
                raise
            raise WalStoreError(str(exc)) from exc
        if record.run_key != self._run_key:
            raise WalStoreError("manifest-cross-run")
        return record

    def write_intent(
        self,
        *,
        op_id: str,
        effect_domain: Literal["local", "external"],
        preimage: BytesPayload,
        inverse: BytesPayload,
        postcondition_check: Mapping[str, object],
        created_at: str,
    ) -> tuple[str, str]:
        op_id = validate_op_id(op_id)
        name = intent_name(op_id)
        record = IntentRecord(
            schema=INTENT_SCHEMA,
            run_key=self._run_key,
            op_id=op_id,
            effect_domain=effect_domain,
            preimage=preimage,
            inverse=inverse,
            postcondition_check=postcondition_check,
            created_at=created_at,
        )
        data = record.to_json_bytes()
        digest = sha256_hex(data)
        try:
            self._dir.write_immutable(name, data, expected_uid=self._expected_uid, mode=0o600)
        except FileExistsError:
            try:
                existing = self._dir.read_regular(
                    name,
                    expected_uid=self._expected_uid,
                    expected_mode=0o600,
                    max_bytes=65536,
                )
            except AuthorityError as exc:
                raise WalStoreError(str(exc)) from exc
            if existing == data:
                return name, digest
            raise WalStoreError("intent-exists")
        except (AuthorityError, ArtifactError) as exc:
            raise WalStoreError(str(exc)) from exc
        return name, digest

    def read_intent(self, op_id: str) -> tuple[IntentRecord, str]:
        op_id = validate_op_id(op_id)
        name = intent_name(op_id)
        try:
            data = self._dir.read_regular(
                name,
                expected_uid=self._expected_uid,
                expected_mode=0o600,
                max_bytes=65536,
            )
            record = IntentRecord.from_json_bytes(data)
        except (AuthorityError, ArtifactError, Exception) as exc:
            raise WalStoreError(str(exc)) from exc
        if record.run_key != self._run_key or record.op_id != op_id:
            raise WalStoreError("intent-identity-mismatch")
        return record, sha256_hex(data)

    def write_receipt(
        self,
        *,
        op_id: str,
        intent_ref: str,
        intent_digest: str,
        postcondition_result: Mapping[str, object],
        receipt_payload: Mapping[str, object],
        committed_at: str,
    ) -> tuple[str, str]:
        op_id = validate_op_id(op_id)
        expected_intent_ref = intent_name(op_id)
        if intent_ref != expected_intent_ref:
            raise WalStoreError("intent-ref-mismatch")
        _intent, actual_digest = self.read_intent(op_id)
        if intent_digest != actual_digest:
            raise WalStoreError("intent-digest-mismatch")
        if postcondition_result.get("satisfied") is not True:
            raise WalStoreError("postcondition-not-satisfied")
        name = receipt_name(op_id)
        record = ReceiptRecord(
            schema=RECEIPT_SCHEMA,
            run_key=self._run_key,
            op_id=op_id,
            intent_ref=intent_ref,
            intent_digest=intent_digest,
            postcondition_result=postcondition_result,
            receipt_payload=receipt_payload,
            committed_at=committed_at,
        )
        data = record.to_json_bytes()
        digest = sha256_hex(data)
        try:
            self._dir.write_immutable(name, data, expected_uid=self._expected_uid, mode=0o600)
        except FileExistsError:
            try:
                existing = self._dir.read_regular(
                    name,
                    expected_uid=self._expected_uid,
                    expected_mode=0o600,
                    max_bytes=65536,
                )
            except AuthorityError as exc:
                raise WalStoreError(str(exc)) from exc
            if existing == data:
                return name, digest
            raise WalStoreError("receipt-exists")
        except (AuthorityError, ArtifactError) as exc:
            raise WalStoreError(str(exc)) from exc
        return name, digest

    def read_receipt_by_ref(self, receipt_ref: str) -> tuple[ReceiptRecord, str]:
        record, digest = self._read_receipt_artifact_by_ref(receipt_ref)
        _intent, actual_digest = self.read_intent(record.op_id)
        if record.intent_digest != actual_digest or record.intent_ref != intent_name(record.op_id):
            raise WalStoreError("receipt-intent-mismatch")
        return record, digest

    def _read_receipt_artifact_by_ref(self, receipt_ref: str) -> tuple[ReceiptRecord, str]:
        name = validate_receipt_ref(receipt_ref)
        try:
            data = self._dir.read_regular(
                name,
                expected_uid=self._expected_uid,
                expected_mode=0o600,
                max_bytes=65536,
            )
            record = ReceiptRecord.from_json_bytes(data)
        except (AuthorityError, ArtifactError, Exception) as exc:
            raise WalStoreError(str(exc)) from exc
        if record.run_key != self._run_key or receipt_name(record.op_id) != receipt_ref:
            raise WalStoreError("receipt-identity-mismatch")
        return record, sha256_hex(data)

    def commit_receipt_reference(
        self,
        *,
        op_id: str,
        receipt_ref: str,
        committed_at: str,
        updated_at: str,
    ) -> RunReceiptEntry:
        op_id = validate_op_id(op_id)
        receipt_ref = validate_receipt_ref(receipt_ref)
        if receipt_ref != receipt_name(op_id):
            raise WalStoreError("receipt-ref-mismatch")
        self.read_receipt_by_ref(receipt_ref)
        try:
            lock = self._dir.open_record_lock("manifest.lock", expected_uid=self._expected_uid, mode=0o600)
        except AuthorityError as exc:
            raise WalStoreError(str(exc)) from exc
        with lock:
            manifest = self.read_manifest()
            entry = RunReceiptEntry(op_id=op_id, receipt_ref=receipt_ref, committed_at=committed_at)
            for existing in manifest.receipts:
                if existing.op_id == op_id:
                    if existing == entry:
                        return existing
                    raise WalStoreError("duplicate-receipt-entry")
            updated = ManifestRecord(
                schema=MANIFEST_SCHEMA,
                run_key=manifest.run_key,
                receipts=manifest.receipts + (entry,),
                created_at=manifest.created_at,
                updated_at=updated_at,
            )
            try:
                self._dir.replace_regular(
                    "manifest.json",
                    updated.to_json_bytes(),
                    expected_uid=self._expected_uid,
                    mode=0o600,
                )
            except (AuthorityError, ArtifactError) as exc:
                raise WalStoreError(str(exc)) from exc
            return entry

    def classify_prefix(self, *, op_id: str, probe: EffectProbe | None = None) -> PrefixClassification:
        op_id = validate_op_id(op_id)
        manifest = self._read_manifest_for_reconcile()
        manifest_entry = None
        for entry in manifest.receipts:
            if entry.op_id == op_id:
                manifest_entry = entry
                break
        intent_record, intent_digest = self._try_read_intent_for_reconcile(op_id)
        receipt_record = self._try_read_receipt_for_reconcile(op_id)

        if intent_record is None:
            if manifest_entry is not None or receipt_record is not None:
                raise ReconcileError("receipt-state-missing-intent")
            return PrefixClassification(
                run_key=self._run_key,
                op_id=op_id,
                structural_prefix=StructuralPrefix.BEFORE_INTENT,
                effect_state=EffectState.NOT_OBSERVED,
                intent_ref=None,
                intent_digest=None,
                receipt_ref=None,
                manifest_entry=manifest_entry,
                reason="no-intent",
                success_authorized=False,
            )

        if receipt_record is not None:
            if receipt_record.intent_ref != intent_name(op_id) or receipt_record.intent_digest != intent_digest:
                raise ReconcileError("receipt-intent-mismatch")

        effect_state, observed_applied = self._observe_effect(intent_record, probe)
        receipt_ref = receipt_name(op_id) if receipt_record is not None else None
        if manifest_entry is not None:
            if receipt_record is None or manifest_entry.receipt_ref != receipt_name(op_id):
                raise ReconcileError("manifest-receipt-mismatch")
            structural = StructuralPrefix.MANIFEST_REFERENCED
            reason = "manifest-referenced"
        elif receipt_record is not None:
            structural = StructuralPrefix.ORPHAN_RECEIPT
            reason = "orphan-receipt"
        elif observed_applied:
            structural = StructuralPrefix.APPLIED_NO_RECEIPT
            reason = "applied-no-receipt"
        else:
            structural = StructuralPrefix.INTENT_ONLY
            reason = "intent-only"
        return PrefixClassification(
            run_key=self._run_key,
            op_id=op_id,
            structural_prefix=structural,
            effect_state=effect_state,
            intent_ref=intent_name(op_id),
            intent_digest=intent_digest,
            receipt_ref=receipt_ref,
            manifest_entry=manifest_entry,
            reason=reason,
            success_authorized=(
                structural is StructuralPrefix.MANIFEST_REFERENCED
                and effect_state is EffectState.POSTCONDITION_SATISFIED
            ),
        )

    def _read_manifest_for_reconcile(self) -> ManifestRecord:
        try:
            return self.read_manifest()
        except WalStoreError as exc:
            raise ReconcileError(str(exc)) from exc

    def _try_read_intent_for_reconcile(self, op_id: str) -> tuple[IntentRecord, str] | tuple[None, None]:
        try:
            return self.read_intent(op_id)
        except WalStoreError as exc:
            if "open-regular:2" in str(exc):
                return None, None
            raise ReconcileError(str(exc)) from exc

    def _try_read_receipt_for_reconcile(self, op_id: str) -> ReceiptRecord | None:
        try:
            receipt, _digest = self._read_receipt_artifact_by_ref(receipt_name(op_id))
            return receipt
        except WalStoreError as exc:
            if "open-regular:2" in str(exc):
                return None
            raise ReconcileError(str(exc)) from exc

    def _observe_effect(self, intent: IntentRecord, probe: EffectProbe | None) -> tuple[EffectState, bool]:
        if probe is None:
            return EffectState.UNKNOWN_REFUSE, False
        observation = probe.observe(intent)
        if observation.postcondition_satisfied is True:
            return EffectState.POSTCONDITION_SATISFIED, observation.applied is True
        if observation.postcondition_satisfied is False:
            return EffectState.POSTCONDITION_FAILED, observation.applied is True
        if observation.applied is True:
            return EffectState.APPLIED, True
        if observation.applied is False:
            return EffectState.NOT_APPLIED, False
        return EffectState.UNKNOWN_REFUSE, False
