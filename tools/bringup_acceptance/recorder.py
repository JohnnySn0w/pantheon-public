"""Tamper-evident evidence recording, generalized from T-2382 for T-2558.

:class:`TraceRecorder` writes a hash-chained, append-only JSONL trace per
module per run.  It never accepts a caller-supplied verdict: callers pass
observation *payloads*, and any pass/fail determination is the calling
module's job to derive from ground truth (a live path, a systemd property,
a mailbox record) before it ever reaches ``append``.  The chain only proves
the recorded sequence of observations was not edited or reordered after
the fact -- it is not itself a truth oracle.

This module carries no T-2382-specific residue: evidence is keyed on a
generic ``module_id`` token (not T-2382's fixed five-leg registry), there
is no reviewed-branch pin, and the gate-name contract
(:func:`require_live_gates`) takes its gate names from the caller instead
of a hardcoded ``T2382_LIVE_*`` set.  ``tools/t2382_live_acceptance.py``
keeps its own independent implementation of this same algorithm; see
T-2558's DECISIONS for why this is a clean generalization rather than a
migration of that module onto this one.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.bringup_acceptance.types import HardStop


TRACE_SCHEMA = "pantheon-bringup-acceptance-trace/v1"
ZERO_HASH = "0" * 64
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_GATE_TRUE_VALUES = {"1", "true", "yes"}


@dataclasses.dataclass(frozen=True)
class TraceEvent:
    schema: str
    module_id: str
    run_id: str
    phase: str
    sequence: int
    observed_at_ns: int
    previous_hash: str
    payload: Mapping[str, Any]
    event_hash: str


def validate_token(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.match(value):
        raise HardStop(f"{label} must be a bounded identifier token: {value!r}")
    return value


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {key: _jsonable(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":")).encode("utf-8")


def _event_hash(document: Mapping[str, Any]) -> str:
    document_without_hash = dict(document)
    document_without_hash.pop("event_hash", None)
    return hashlib.sha256(_canonical_bytes(document_without_hash)).hexdigest()


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise HardStop(f"path is missing or not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_module_evidence_root(root: Path, *, run_id: str, module_id: str) -> Path:
    """Return (creating if needed) ``<root>/<run_id>/modules/<module_id>/``."""

    validate_token(run_id, label="run_id")
    validate_token(module_id, label="module_id")
    module_root = (root / run_id / "modules" / module_id).resolve()
    module_root.mkdir(parents=True, exist_ok=True)
    return module_root


def write_json_evidence(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically write ``value`` as JSON: tmpfile + fsync + rename + dir fsync."""

    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(_jsonable(value), sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


class TraceRecorder:
    """Hash-chained, tamper-evident JSONL trace for one module in one run."""

    def __init__(self, evidence_root: Path, *, module_id: str, run_id: str) -> None:
        self.module_id = validate_token(module_id, label="module_id")
        self.run_id = validate_token(run_id, label="run_id")
        self.root = make_module_evidence_root(evidence_root, run_id=run_id, module_id=module_id)
        self.path = self.root / "trace.jsonl"

    def read(self) -> tuple[TraceEvent, ...]:
        if not self.path.exists():
            return ()

        previous_hash = ZERO_HASH
        events: list[TraceEvent] = []
        for expected_sequence, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            document = json.loads(line)
            if document.get("schema") != TRACE_SCHEMA:
                raise HardStop("trace schema mismatch")
            if document.get("module_id") != self.module_id:
                raise HardStop("trace module_id mismatch")
            if document.get("run_id") != self.run_id:
                raise HardStop("trace run_id mismatch")
            if document.get("sequence") != expected_sequence:
                raise HardStop("trace sequence gap or reorder")
            if document.get("previous_hash") != previous_hash:
                raise HardStop("trace hash chain is broken")
            if document.get("event_hash") != _event_hash(document):
                raise HardStop("trace event hash mismatch")
            previous_hash = str(document["event_hash"])
            events.append(
                TraceEvent(
                    schema=str(document["schema"]),
                    module_id=str(document["module_id"]),
                    run_id=str(document["run_id"]),
                    phase=str(document["phase"]),
                    sequence=int(document["sequence"]),
                    observed_at_ns=int(document["observed_at_ns"]),
                    previous_hash=str(document["previous_hash"]),
                    payload=dict(document["payload"]),
                    event_hash=str(document["event_hash"]),
                )
            )
        return tuple(events)

    def append(self, phase: str, payload: Mapping[str, Any]) -> TraceEvent:
        events = self.read()
        previous_hash = events[-1].event_hash if events else ZERO_HASH
        document: dict[str, Any] = {
            "schema": TRACE_SCHEMA,
            "module_id": self.module_id,
            "run_id": self.run_id,
            "phase": phase,
            "sequence": len(events),
            "observed_at_ns": time.time_ns(),
            "previous_hash": previous_hash,
            "payload": _jsonable(payload),
        }
        document["event_hash"] = _event_hash(document)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return self.read()[-1]


def require_live_gates(*, module_id: str, required_gates: Sequence[str], env: Mapping[str, str]) -> None:
    """Refuse a live module unless every named env gate is explicitly set.

    Gate names are supplied by the caller -- there is no hardcoded
    ``T2382_LIVE_*``-style registry here; each module owns its own gate
    contract.
    """

    validate_token(module_id, label="module_id")
    missing = tuple(gate for gate in required_gates if env.get(gate) not in _GATE_TRUE_VALUES)
    if missing:
        raise HardStop(
            f"module {module_id} missing explicit live gate(s): " + ", ".join(missing)
        )
