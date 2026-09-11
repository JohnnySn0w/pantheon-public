from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping


MAX_ARTIFACT_BYTES = 65536
MANIFEST_SCHEMA = "pantheon-wal-poc/v1"
INTENT_SCHEMA = "pantheon-wal-intent-poc/v1"
RECEIPT_SCHEMA = "pantheon-wal-receipt-poc/v1"


class CanonicalJsonError(Exception):
    """Raised for noncanonical or invalid PoC WAL JSON."""


class ArtifactError(Exception):
    """Raised for invalid PoC WAL artifact shape."""


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class BytesPayload:
    kind: str
    bytes_b64: str
    sha256: str
    payload: Mapping[str, object] | None = None

    def to_json_obj(self) -> dict[str, object]:
        _validate_bytes_payload(self)
        return {
            "bytes_b64": self.bytes_b64,
            "kind": self.kind,
            "payload": self.payload,
            "sha256": self.sha256,
        }

    @classmethod
    def from_json_obj(cls, value: object) -> "BytesPayload":
        if not isinstance(value, dict):
            raise ArtifactError("payload-not-object")
        _require_keys(value, {"kind", "bytes_b64", "sha256", "payload"})
        payload = cls(
            kind=_expect_str(value["kind"], "kind"),
            bytes_b64=_expect_str(value["bytes_b64"], "bytes_b64"),
            sha256=_expect_str(value["sha256"], "sha256"),
            payload=_expect_optional_mapping(value["payload"], "payload"),
        )
        _validate_bytes_payload(payload)
        return payload


@dataclass(frozen=True)
class IntentRecord:
    schema: Literal["pantheon-wal-intent-poc/v1"]
    run_key: str
    op_id: str
    effect_domain: Literal["local", "external"]
    preimage: BytesPayload
    inverse: BytesPayload
    postcondition_check: Mapping[str, object]
    created_at: str

    def to_json_bytes(self) -> bytes:
        _validate_intent(self)
        return canonical_json_bytes(
            {
                "created_at": self.created_at,
                "effect_domain": self.effect_domain,
                "inverse": self.inverse.to_json_obj(),
                "op_id": self.op_id,
                "postcondition_check": dict(self.postcondition_check),
                "preimage": self.preimage.to_json_obj(),
                "run_key": self.run_key,
                "schema": self.schema,
            }
        )

    @classmethod
    def from_json_bytes(cls, data: bytes) -> "IntentRecord":
        obj = load_canonical_json(data)
        _require_keys(
            obj,
            {
                "schema",
                "run_key",
                "op_id",
                "effect_domain",
                "preimage",
                "inverse",
                "postcondition_check",
                "created_at",
            },
        )
        record = cls(
            schema=_expect_literal(obj["schema"], INTENT_SCHEMA, "schema"),
            run_key=validate_run_key(obj["run_key"]),
            op_id=validate_op_id(obj["op_id"]),
            effect_domain=_expect_domain(obj["effect_domain"]),
            preimage=BytesPayload.from_json_obj(obj["preimage"]),
            inverse=BytesPayload.from_json_obj(obj["inverse"]),
            postcondition_check=_expect_mapping(obj["postcondition_check"], "postcondition_check"),
            created_at=_expect_str(obj["created_at"], "created_at"),
        )
        _validate_intent(record)
        return record


@dataclass(frozen=True)
class ReceiptRecord:
    schema: Literal["pantheon-wal-receipt-poc/v1"]
    run_key: str
    op_id: str
    intent_ref: str
    intent_digest: str
    postcondition_result: Mapping[str, object]
    receipt_payload: Mapping[str, object]
    committed_at: str

    def to_json_bytes(self) -> bytes:
        _validate_receipt(self)
        return canonical_json_bytes(
            {
                "committed_at": self.committed_at,
                "intent_digest": self.intent_digest,
                "intent_ref": self.intent_ref,
                "op_id": self.op_id,
                "postcondition_result": dict(self.postcondition_result),
                "receipt_payload": dict(self.receipt_payload),
                "run_key": self.run_key,
                "schema": self.schema,
            }
        )

    @classmethod
    def from_json_bytes(cls, data: bytes) -> "ReceiptRecord":
        obj = load_canonical_json(data)
        _require_keys(
            obj,
            {
                "schema",
                "run_key",
                "op_id",
                "intent_ref",
                "intent_digest",
                "postcondition_result",
                "receipt_payload",
                "committed_at",
            },
        )
        record = cls(
            schema=_expect_literal(obj["schema"], RECEIPT_SCHEMA, "schema"),
            run_key=validate_run_key(obj["run_key"]),
            op_id=validate_op_id(obj["op_id"]),
            intent_ref=validate_intent_ref(obj["intent_ref"]),
            intent_digest=_expect_sha256(obj["intent_digest"], "intent_digest"),
            postcondition_result=_expect_mapping(obj["postcondition_result"], "postcondition_result"),
            receipt_payload=_expect_mapping(obj["receipt_payload"], "receipt_payload"),
            committed_at=_expect_str(obj["committed_at"], "committed_at"),
        )
        _validate_receipt(record)
        return record


@dataclass(frozen=True)
class RunReceiptEntry:
    op_id: str
    receipt_ref: str
    committed_at: str

    def to_json_obj(self) -> dict[str, object]:
        return {
            "committed_at": self.committed_at,
            "op_id": validate_op_id(self.op_id),
            "receipt_ref": validate_receipt_ref(self.receipt_ref),
        }

    @classmethod
    def from_json_obj(cls, value: object) -> "RunReceiptEntry":
        if not isinstance(value, dict):
            raise ArtifactError("receipt-entry-not-object")
        _require_keys(value, {"op_id", "receipt_ref", "committed_at"})
        return cls(
            op_id=validate_op_id(value["op_id"]),
            receipt_ref=validate_receipt_ref(value["receipt_ref"]),
            committed_at=_expect_str(value["committed_at"], "committed_at"),
        )


@dataclass(frozen=True)
class ManifestRecord:
    schema: Literal["pantheon-wal-poc/v1"]
    run_key: str
    receipts: tuple[RunReceiptEntry, ...]
    created_at: str
    updated_at: str

    def to_json_bytes(self) -> bytes:
        _validate_manifest(self)
        return canonical_json_bytes(
            {
                "created_at": self.created_at,
                "receipts": [entry.to_json_obj() for entry in self.receipts],
                "run_key": self.run_key,
                "schema": self.schema,
                "updated_at": self.updated_at,
            }
        )

    @classmethod
    def from_json_bytes(cls, data: bytes) -> "ManifestRecord":
        obj = load_canonical_json(data)
        _require_keys(obj, {"schema", "run_key", "receipts", "created_at", "updated_at"})
        receipts_raw = obj["receipts"]
        if not isinstance(receipts_raw, list):
            raise ArtifactError("receipts-not-list")
        record = cls(
            schema=_expect_literal(obj["schema"], MANIFEST_SCHEMA, "schema"),
            run_key=validate_run_key(obj["run_key"]),
            receipts=tuple(RunReceiptEntry.from_json_obj(entry) for entry in receipts_raw),
            created_at=_expect_str(obj["created_at"], "created_at"),
            updated_at=_expect_str(obj["updated_at"], "updated_at"),
        )
        _validate_manifest(record)
        return record


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    _reject_float(value)
    try:
        data = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise CanonicalJsonError("json-encode") from exc
    if len(data) > MAX_ARTIFACT_BYTES:
        raise CanonicalJsonError("json-too-large")
    return data


def load_canonical_json(data: bytes, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> dict[str, object]:
    if not isinstance(data, bytes):
        raise CanonicalJsonError("not-bytes")
    if len(data) > max_bytes:
        raise CanonicalJsonError("json-too-large")
    if not data.endswith(b"\n"):
        raise CanonicalJsonError("json-not-newline-terminated")

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CanonicalJsonError("duplicate-key")
            result[key] = value
        return result

    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=pairs_hook)
    except CanonicalJsonError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalJsonError("json-decode") from exc
    if not isinstance(obj, dict):
        raise CanonicalJsonError("json-not-object")
    if canonical_json_bytes(obj) != data:
        raise CanonicalJsonError("json-noncanonical")
    return obj


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_run_key(value: object) -> str:
    return _validate_id(value, "run_key")


def validate_op_id(value: object) -> str:
    return _validate_id(value, "op_id")


def intent_name(op_id: str) -> str:
    return f"intent-{validate_op_id(op_id)}.json"


def receipt_name(op_id: str) -> str:
    return f"receipt-{validate_op_id(op_id)}.json"


def validate_intent_ref(ref: object) -> str:
    if not isinstance(ref, str):
        raise ArtifactError("intent-ref-not-str")
    if ref.startswith("intent-") and ref.endswith(".json"):
        validate_op_id(ref[len("intent-") : -len(".json")])
        return ref
    raise ArtifactError("bad-intent-ref")


def validate_receipt_ref(ref: object) -> str:
    if not isinstance(ref, str):
        raise ArtifactError("receipt-ref-not-str")
    if ref.startswith("receipt-") and ref.endswith(".json"):
        validate_op_id(ref[len("receipt-") : -len(".json")])
        return ref
    raise ArtifactError("bad-receipt-ref")


def _validate_id(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ArtifactError(f"{field}-not-str")
    if not _ID_RE.fullmatch(value):
        raise ArtifactError(f"{field}-invalid")
    return value


def _expect_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ArtifactError(f"{field}-not-str")
    return value


def _expect_sha256(value: object, field: str) -> str:
    text = _expect_str(value, field)
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ArtifactError(f"{field}-invalid")
    return text


def _expect_literal(value: object, expected: str, field: str) -> Any:
    if value != expected:
        raise ArtifactError(f"{field}-invalid")
    return value


def _expect_domain(value: object) -> Literal["local", "external"]:
    if value not in {"local", "external"}:
        raise ArtifactError("effect-domain-invalid")
    return value  # type: ignore[return-value]


def _expect_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ArtifactError(f"{field}-not-object")
    return value


def _expect_optional_mapping(value: object, field: str) -> Mapping[str, object] | None:
    if value is None:
        return None
    return _expect_mapping(value, field)


def _require_keys(value: Mapping[str, object], keys: set[str]) -> None:
    actual = set(value)
    if actual != keys:
        raise ArtifactError("bad-keys")


def _validate_bytes_payload(payload: BytesPayload) -> None:
    if payload.kind != "bytes":
        raise ArtifactError("payload-kind-invalid")
    _expect_sha256(payload.sha256, "sha256")
    try:
        decoded = base64.b64decode(payload.bytes_b64.encode("ascii"), validate=True)
    except Exception as exc:
        raise ArtifactError("payload-base64-invalid") from exc
    if hashlib.sha256(decoded).hexdigest() != payload.sha256:
        raise ArtifactError("payload-digest-mismatch")
    if payload.payload is not None:
        _reject_float(payload.payload)


def _validate_intent(record: IntentRecord) -> None:
    if record.schema != INTENT_SCHEMA:
        raise ArtifactError("intent-schema-invalid")
    validate_run_key(record.run_key)
    validate_op_id(record.op_id)
    _expect_domain(record.effect_domain)
    _validate_bytes_payload(record.preimage)
    _validate_bytes_payload(record.inverse)
    _reject_float(record.postcondition_check)
    _expect_str(record.created_at, "created_at")


def _validate_receipt(record: ReceiptRecord) -> None:
    if record.schema != RECEIPT_SCHEMA:
        raise ArtifactError("receipt-schema-invalid")
    validate_run_key(record.run_key)
    validate_op_id(record.op_id)
    validate_intent_ref(record.intent_ref)
    _expect_sha256(record.intent_digest, "intent_digest")
    _reject_float(record.postcondition_result)
    _reject_float(record.receipt_payload)
    if record.postcondition_result.get("satisfied") is not True:
        raise ArtifactError("receipt-postcondition-not-satisfied")
    _expect_str(record.committed_at, "committed_at")


def _validate_manifest(record: ManifestRecord) -> None:
    if record.schema != MANIFEST_SCHEMA:
        raise ArtifactError("manifest-schema-invalid")
    validate_run_key(record.run_key)
    seen: set[str] = set()
    for entry in record.receipts:
        if entry.op_id in seen:
            raise ArtifactError("duplicate-receipt-entry")
        seen.add(entry.op_id)
    _expect_str(record.created_at, "created_at")
    _expect_str(record.updated_at, "updated_at")


def _reject_float(value: object) -> None:
    if isinstance(value, float):
        raise CanonicalJsonError("float-not-allowed")
    if isinstance(value, dict):
        for item in value.values():
            _reject_float(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_float(item)
