"""The invariant-list digest oracle (ORG-080 §2 / §6 touch-point 2, ORG-081 D-11; T-5135 S4).

``invariant_list_digest`` was only format-checked; nothing recomputed it. This
module is the one place the engine and the registry validator recompute it, from
the ``<id>.invariants.yaml`` file next to the contract module.

Two list shapes exist and each keeps the digest it was pinned with (no re-pin):

* ``invariant_list:`` wrapper (harness-manager, ``harness_manager_verify.list_digest``):
  sha256 of ``canonical_json_bytes`` of the whole parsed document;
* ``schema: pantheon-invariant-list/v0`` (host-substrate, ``host_substrate.invariants.list_digest``):
  sha256 of the compact sorted-key ASCII JSON of ``{id, invariants}`` (status fields excluded).

Tests assert both reproduce the components' own functions. Any other shape is a
refusal (fail-closed), never a guess.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from tools.germinate.planner_sandbox.wal.artifacts import canonical_json_bytes

FLAT_SCHEMA = "pantheon-invariant-list/v0"
WRAPPED_KEY = "invariant_list"
LIST_SUFFIX = ".invariants.yaml"
MAX_BYTES = 1 << 20


class InvariantDigestError(ValueError):
    """The invariant list cannot be read or digested."""


def list_path_for(module_path: Path) -> Path:
    return module_path.with_name(module_path.name.removesuffix(".yaml") + LIST_SUFFIX)


def load_list(path: Path) -> Mapping[str, Any]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise InvariantDigestError(f"{path.name}: invariant list missing, not a regular file, or oversized")
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InvariantDigestError(f"{path.name}: cannot read invariant list: {exc}") from None
    if not isinstance(doc, dict):
        raise InvariantDigestError(f"{path.name}: top level must be a mapping")
    return doc


def digest_of(doc: Mapping[str, Any]) -> str:
    if set(doc) == {WRAPPED_KEY}:
        return "sha256:" + hashlib.sha256(canonical_json_bytes(doc)).hexdigest()
    if doc.get("schema") == FLAT_SCHEMA and "id" in doc and "invariants" in doc:
        body = {"id": doc["id"], "invariants": doc["invariants"]}
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "sha256:" + hashlib.sha256(text.encode("ascii")).hexdigest()
    raise InvariantDigestError("unrecognised invariant list shape (neither `invariant_list:` nor " + FLAT_SCHEMA + ")")


def list_id_of(doc: Mapping[str, Any]) -> Any:
    if set(doc) == {WRAPPED_KEY} and isinstance(doc[WRAPPED_KEY], dict):
        return doc[WRAPPED_KEY].get("id")
    return doc.get("id")


def compute(module_path: Path) -> tuple[str, Any]:
    """``(digest, list id)`` recomputed from the list file beside ``module_path``."""

    doc = load_list(list_path_for(module_path))
    return digest_of(doc), list_id_of(doc)
