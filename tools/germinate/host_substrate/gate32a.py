"""ORG-082 gate 32a at the simulated tier (T-5084, GE).

Gate 32a: both invocation contexts load the same op-registry revision (digest
equality), the daemon's loaded family dispatch table equals the authoritative
expected runtime subset (ORG-082 §2a), and no substrate family is in either.

Sim tier only. The real daemon-side digest capture and the structural
method-table exclusion (ORG-082 build steps 3 and 5) are action-broker work
(re-scoped T-2918); the disposable-host run is out of this tranche.
"""

from __future__ import annotations

from typing import Iterable

from tools.germinate.op_registry import OpRegistry, OpRegistryError, assert_same_registry
from tools.germinate.registry.contract_digest import SENTINEL, digest_document


def daemon_projection(op_registry: OpRegistry) -> frozenset[str]:
    """The family dispatch table a broker daemon loading ``op_registry`` admits:
    its broker-admitted, non-substrate kinds."""

    return frozenset(e.kind for e in op_registry.entries.values() if e.broker_admitted and not e.substrate)


def runtime_subset_digest(kinds: Iterable[str]) -> str:
    """ORG-082 §2a's wrapper digest (the same sole oracle the engine uses)."""

    wrapper = {
        "component": {
            "id": "expected-runtime-subset",
            "steward": "broker-daemon",
            "version": 1,
            "contract_digest": SENTINEL,
            "broker_runtime": sorted(set(kinds)),
        }
    }
    return digest_document(wrapper, source="expected-runtime-subset")


def check(*, root_registry: OpRegistry, daemon_registry: OpRegistry, projection: frozenset[str], expected_union: frozenset[str], expected_digest: str) -> list[str]:
    """Every gate 32a failure, empty when the gate holds."""

    failures = []
    try:
        assert_same_registry(root_registry.digest, daemon_registry.digest)
    except OpRegistryError as exc:
        failures.append(f"REGISTRY_DIGEST_MISMATCH: {exc}")
    if projection != expected_union:
        failures.append(f"RUNTIME_SUBSET_MISMATCH: extra {sorted(projection - expected_union)}, missing {sorted(expected_union - projection)}")
    if runtime_subset_digest(projection) != expected_digest:
        failures.append("RUNTIME_SUBSET_MISMATCH: projection digest != expected-runtime-subset digest")
    substrate = root_registry.substrate_kinds
    leaked = sorted((projection | expected_union) & substrate)
    if leaked:
        failures.append(f"SUBSTRATE_FAMILY_NOT_EXPOSED violated: {leaked}")
    return failures
