"""Durable delivery receipts derived from structural events and maildir acks."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.harness_adapter.types import DeliveryReceipt, HarnessEvent


_MECHANISM = "maildir-sidecar"


def record_submitted(
    inbox: Path,
    *,
    message_id: str,
    correlation_id: str,
    event: HarnessEvent,
) -> DeliveryReceipt:
    """Record submitted only after a structural turn_started event."""

    if event.kind != "turn_started":
        return DeliveryReceipt(
            harness=event.harness,
            correlation_id=correlation_id,
            state="durably_queued",
            mechanism=_MECHANISM,
            retryable=True,
            evidence={
                "reason": "event is not turn_started",
                "event_kind": event.kind,
                "message_id": message_id,
                "inbox": str(inbox),
            },
        )

    receipt_dir = inbox / ".delivery"
    receipt_path = receipt_dir / f"{message_id}.submitted.json"
    existing = _read_json_object(receipt_path)
    if existing is not None:
        existing_correlation = existing.get("correlation_id")
        if existing_correlation != correlation_id:
            return DeliveryReceipt(
                harness=event.harness,
                correlation_id=correlation_id,
                state="rejected",
                mechanism=_MECHANISM,
                retryable=False,
                evidence={
                    "reason": "message_id already has a different correlation_id",
                    "message_id": message_id,
                    "inbox": str(inbox),
                    "sidecar": str(receipt_path),
                },
            )
        return _submitted_receipt(event.harness, correlation_id, inbox, message_id, receipt_path)

    submitted_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "version": 1,
        "state": "submitted",
        "harness": event.harness,
        "correlation_id": correlation_id,
        "message_id": message_id,
        "event_kind": event.kind,
        "session_id": event.session_id,
        "submitted_at": submitted_at,
    }
    _write_json_atomic(receipt_path, payload)
    return _submitted_receipt(event.harness, correlation_id, inbox, message_id, receipt_path)


def inspect_delivery(
    receipt: DeliveryReceipt,
) -> DeliveryReceipt:
    """Refresh a receipt, deriving ack truth only from cur/<message_id>."""

    inbox_value = receipt.evidence.get("inbox")
    message_id = receipt.evidence.get("message_id")
    if not inbox_value or not message_id:
        return receipt

    inbox = Path(inbox_value)
    evidence = dict(receipt.evidence)
    cur_path = inbox / "cur" / message_id
    ack_record = _matching_ack_record(inbox / ".acks.jsonl", message_id)
    if ack_record is not None:
        evidence.update(_ack_evidence(ack_record))

    if cur_path.exists():
        evidence["acked_path"] = str(cur_path)
        return DeliveryReceipt(
            harness=receipt.harness,
            correlation_id=receipt.correlation_id,
            state="acked",
            mechanism=receipt.mechanism,
            retryable=False,
            evidence=evidence,
        )

    return DeliveryReceipt(
        harness=receipt.harness,
        correlation_id=receipt.correlation_id,
        state=receipt.state,
        mechanism=receipt.mechanism,
        retryable=receipt.retryable,
        evidence=evidence,
    )


def _submitted_receipt(
    harness: str,
    correlation_id: str,
    inbox: Path,
    message_id: str,
    receipt_path: Path,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        harness=harness,  # type: ignore[arg-type]
        correlation_id=correlation_id,
        state="submitted",
        mechanism=_MECHANISM,
        retryable=True,
        evidence={
            "message_id": message_id,
            "inbox": str(inbox),
            "sidecar": str(receipt_path),
            "event_kind": "turn_started",
        },
    )


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json_object(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _matching_ack_record(path: Path, message_id: str) -> Mapping[str, Any] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("id") == message_id:
            return record
    return None


def _ack_evidence(record: Mapping[str, Any]) -> dict[str, str]:
    evidence: dict[str, str] = {"ack_log_seen": "true"}
    for key in ("label", "pid", "uid", "ts_ns"):
        value = record.get(key)
        if value is not None:
            evidence[f"ack_{key}"] = str(value)
    return evidence
