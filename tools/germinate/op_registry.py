"""The germination engine's typed-op registry and its ORG-082 load digest (T-5074).

PROVISIONAL reading (T-5074 bubble 2, pending orchestrator as an A-8 build reading):
ORG-080 names ``tools/host_control/contracts.py`` as the op-enum home, but the
broker exports its kind table from ``tools/host_control/envelope.py`` "so the
ORG-082 gate 32a runtime-subset projection can digest it later". This module
BUILDS the registry from those envelope constants and the ORG-082 substrate
list without editing host_control, so there is still one source per kind.

PROVISIONAL reading (bubble 3): an op's registry key is its op KIND. The
family-style names in the ORG-080 §2 example contract (``file.atomic-write``,
``unit.user.install``) are illustrative; contract privilege lists name kinds
that exist here.

The registry blob is canonical JSON. ORG-082 D-D: the same-registry digest is
SHA-256 of the whole loaded blob, captured at each context's load time (the
simulated backend captures it in its constructor; the broker daemon's capture
is a later tranche).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, Mapping

from tools.germinate.planner_sandbox.wal.artifacts import canonical_json_bytes, sha256_hex
from tools.host_control.envelope import (
    EXECUTOR_KIND_CLASSES,
    INSPECTION_PORT_METHODS,
    INSPECTION_READ_PURPOSES,
    INSPECTION_UNIT_ROLES,
    KIND_INSPECTION_READS,
    ORG082_SUBSTRATE_FAMILIES,
    OUT_OF_ENVELOPE_KINDS,
)

SCHEMA: Final = "pantheon-op-registry/v0"
Privilege = Literal["none", "seat", "root"]
PRIVILEGES: Final = ("none", "seat", "root")

READ_ONLY_CLASSES: Final = frozenset({"diagnostics.read"})
SUBSTRATE_CLASS: Final = "substrate.root"
SEAT_LOCAL_CLASS: Final = "operator.seat_local"

#: ORG-080 D-N: these kinds exist only for the runtime controller; a plan
#: containing one is a design bug refused at plan.
LLM_PATH_KINDS: Final = frozenset({"harness-turn", "send-keys-prompt", "model-reply-wait"})

#: Engine-local kinds that neither the broker envelope nor ORG-082 names.
#: A component adopting the contract (T-5081 onward) adds its kinds HERE, as
#: data: kind -> (capability_class, privilege). Empty in T-5074.
ENGINE_KINDS: Final[Mapping[str, tuple[str, Privilege]]] = MappingProxyType({})


class OpRegistryError(RuntimeError):
    """The registry cannot be built, or two contexts loaded different registries."""


#: Capability classes only this registry may assign (ORG-080 D-AC): the broker
#: envelope's classes, the read-only classes, and the engine's own substrate
#: and seat-local labels. A component kind claiming one would borrow authority
#: the engine defines.
RESERVED_CLASSES: Final = frozenset(EXECUTOR_KIND_CLASSES.values()) | READ_ONLY_CLASSES | {SUBSTRATE_CLASS, SEAT_LOCAL_CLASS}
#: The only privilege a component-declared kind may carry.
COMPONENT_KIND_PRIVILEGE: Final = "seat"


def component_kind_errors(kind: str, capability_class: str, privilege: str) -> list[str]:
    """Why a component-declared op kind is refused (empty list = admissible).

    Components may add only ordinary seat-privilege kinds in their own class:
    never root, never none (read-only is an engine-defined label), never a
    reserved class, never an existing or LLM-path kind.
    """

    errors = []
    if privilege != COMPONENT_KIND_PRIVILEGE:
        errors.append(f"privilege {privilege!r}: component kinds are seat-privilege only (root and read-only are engine-defined)")
    if capability_class in RESERVED_CLASSES:
        errors.append(f"capability class {capability_class!r} is reserved to the engine's registry")
    if kind in LLM_PATH_KINDS:
        errors.append(f"kind {kind!r} is an LLM-path kind (ORG-080 D-N)")
    if kind in EXECUTOR_KIND_CLASSES or kind in OUT_OF_ENVELOPE_KINDS or kind in ORG082_SUBSTRATE_FAMILIES:
        errors.append(f"kind {kind!r} shadows an engine-registry kind")
    return errors


@dataclass(frozen=True)
class OpKind:
    kind: str
    capability_class: str
    privilege: Privilege
    substrate: bool
    read_only: bool
    broker_admitted: bool
    #: T-5608: the kind's declared user-manager inspection reads
    #: (envelope.KIND_INSPECTION_READS), part of the digested blob.
    inspection_reads: tuple[tuple[str, str, str], ...] = ()

    def to_json_obj(self) -> dict[str, object]:
        obj: dict[str, object] = {
            "broker_admitted": self.broker_admitted,
            "capability_class": self.capability_class,
            "privilege": self.privilege,
            "read_only": self.read_only,
            "substrate": self.substrate,
        }
        # Only kinds that declare reads carry the key, so every other entry's
        # bytes are unchanged; a declared read changes the registry digest.
        if self.inspection_reads:
            obj["inspection_reads"] = [list(read) for read in self.inspection_reads]
        return obj


@dataclass(frozen=True)
class OpRegistry:
    entries: Mapping[str, OpKind]
    blob: bytes
    digest: str

    def get(self, kind: str) -> OpKind | None:
        return self.entries.get(kind)

    @property
    def substrate_kinds(self) -> frozenset[str]:
        return frozenset(k for k, e in self.entries.items() if e.substrate)

    @property
    def broker_admitted_kinds(self) -> frozenset[str]:
        return frozenset(k for k, e in self.entries.items() if e.broker_admitted)


def _add(entries: dict[str, OpKind], entry: OpKind) -> None:
    if entry.kind in entries:
        raise OpRegistryError(f"op kind {entry.kind!r} is declared twice")
    entries[entry.kind] = entry


def build_entries(engine_kinds: Mapping[str, tuple[str, Privilege]] = ENGINE_KINDS) -> dict[str, OpKind]:
    entries: dict[str, OpKind] = {}
    unknown = sorted(set(KIND_INSPECTION_READS) - set(EXECUTOR_KIND_CLASSES))
    if unknown:
        raise OpRegistryError(f"inspection reads declared for kinds outside the executor envelope: {unknown}")
    for kind, declared in KIND_INSPECTION_READS.items():
        for read in declared:
            if not (isinstance(read, tuple) and len(read) == 3 and all(isinstance(part, str) for part in read)):
                raise OpRegistryError(f"inspection read for {kind!r} is not a (method, role, purpose) string triple: {read!r}")
            method, role, purpose = read
            if method not in INSPECTION_PORT_METHODS:
                raise OpRegistryError(f"inspection read for {kind!r}: method {method!r} not in {sorted(INSPECTION_PORT_METHODS)}")
            if role not in INSPECTION_UNIT_ROLES:
                raise OpRegistryError(f"inspection read for {kind!r}: unit role {role!r} not in {sorted(INSPECTION_UNIT_ROLES)}")
            if purpose not in INSPECTION_READ_PURPOSES:
                raise OpRegistryError(f"inspection read for {kind!r}: read purpose {purpose!r} is not a ReadPurpose value")
    for kind, klass in EXECUTOR_KIND_CLASSES.items():
        read_only = klass in READ_ONLY_CLASSES
        reads = tuple(tuple(read) for read in KIND_INSPECTION_READS.get(kind, ()))
        _add(entries, OpKind(kind, klass, "none" if read_only else "seat", False, read_only, True, reads))
    for kind in OUT_OF_ENVELOPE_KINDS:
        _add(entries, OpKind(kind, SEAT_LOCAL_CLASS, "seat", False, False, False))
    for kind in ORG082_SUBSTRATE_FAMILIES:
        _add(entries, OpKind(kind, SUBSTRATE_CLASS, "root", True, False, False))
    for kind, (klass, privilege) in engine_kinds.items():
        if privilege not in PRIVILEGES:
            raise OpRegistryError(f"engine kind {kind!r}: privilege {privilege!r} not in {PRIVILEGES}")
        read_only = klass in READ_ONLY_CLASSES
        if read_only != (privilege == "none"):
            raise OpRegistryError(f"engine kind {kind!r}: privilege none is exactly the read-only classes")
        _add(entries, OpKind(kind, klass, privilege, privilege == "root", read_only, False))
    for kind in LLM_PATH_KINDS:
        if kind in entries:
            raise OpRegistryError(f"LLM-path kind {kind!r} may not be a registered op (ORG-080 D-N)")
    return entries


def registry_blob(entries: Mapping[str, OpKind]) -> bytes:
    return canonical_json_bytes(
        {"schema": SCHEMA, "ops": {kind: entries[kind].to_json_obj() for kind in sorted(entries)}}
    )


def load_op_registry(engine_kinds: Mapping[str, tuple[str, Privilege]] = ENGINE_KINDS) -> OpRegistry:
    """Build the registry and capture its digest (ORG-082 D-D) at load time."""

    entries = build_entries(engine_kinds)
    blob = registry_blob(entries)
    return OpRegistry(MappingProxyType(entries), blob, "sha256:" + sha256_hex(blob))


def assert_same_registry(expected_digest: str, loaded_digest: str) -> None:
    """The machine form of ORG-082 gate 32a's digest-equality half."""

    if expected_digest != loaded_digest:
        raise OpRegistryError(
            f"op-registry digest mismatch: expected {expected_digest}, loaded {loaded_digest} (ORG-082 gate 32a)"
        )
