"""The broker daemon's load-time runtime table (ORG-082 build steps 3 and 5; T-2918).

Step 3 (ORG-082 D-D): at daemon load the broker loads the ONE shared typed-op
registry through the SAME loader the germination engine's ``Engine.from_checkout``
uses for step 2 (``tools.germinate.engine.components.load_repo_op_registry``:
base kinds plus every registered component's ``OP_KINDS`` literal, read without
importing component code; T-2918 review B1) and captures the SHA-256
digest of the loaded registry blob. ``DaemonRuntime.loaded_op_registry_digest``
is that digest; ``startup_record()`` is its canonical, loggable form, which the
disposable-host gate 32a run (step 4, T-2924) compares out of process with
``gate32a_check``.

Step 5 (ORG-082 D-B): the daemon's operation-family DISPATCH TABLE (the kinds
its ``Plan`` composer admits, NOT the public seven-method D-Bus contract) is
built once, here, and the daemon REFUSES TO START if any substrate family
(ORG-082 §1) would be in it, in the advertised host families, or in any
execution class this revision implements, or if the table is not exactly the
registry's runtime projection. This is an assertion at start, not a filter at
call time: there is no per-call path that consults a substrate list to decide.
The ``Plan`` composer refuses a substrate kind with ``SubstrateFamilyNotExposed``
because the kind is absent from this table (``refuse_outside_table``).

ORG-052 gate 19 (T-2918 D7): the broker is an unprivileged service, so start
also refuses when the effective uid is 0.
"""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Final, Iterable, Mapping

from tools.germinate.host_substrate import gate32a
from tools.germinate.op_registry import SUBSTRATE_CLASS, OpRegistry, OpRegistryError, load_op_registry
from tools.host_control.contracts import (
    RegistryDigestMismatch,
    RuntimeSubsetMismatch,
    SubstrateFamilyNotExposed,
    canonical_json,
)
from tools.host_control.envelope import (
    EXECUTOR_KIND_CLASSES,
    IMPLEMENTED_EXECUTION_CLASSES,
    ORG082_SUBSTRATE_FAMILIES,
)

STARTUP_RECORD_SCHEMA: Final = "pantheon-broker-runtime-load/v1"

#: The checkout this broker runs from (its registry and component contracts).
REPO_ROOT: Final = Path(__file__).resolve().parents[2]


def load_checkout_op_registry(repo_root: Path = REPO_ROOT) -> OpRegistry:
    """The SAME registry ``Engine.from_checkout`` loads (T-2918 review B1):
    the base kinds plus every registered component's ``OP_KINDS`` literal, read
    WITHOUT importing component code (``components.load_repo_op_registry``).

    Component kinds are seat-privilege and ``broker_admitted=False`` by
    construction (``op_registry.build_entries``), so they enter the digest but
    never the daemon's dispatch projection: they are engine-handler kinds for
    the operator/bootstrap legs, not broker runtime (ORG-082 §2a: the runtime
    subset is the union of declared ``broker_runtime`` lists, not every kind).
    """

    from tools.germinate.engine.components import ComponentLoadError, load_repo_op_registry

    try:
        return load_repo_op_registry(repo_root)
    except ComponentLoadError as exc:
        raise OpRegistryError(f"component op kinds refused: {exc}") from None


class DaemonStartRefused(RuntimeError):
    """The broker daemon must not start (ORG-082 step 5 / ORG-052 gate 19).

    ``cause`` is the ORG-082 refusal whose payload names why, when there is one.
    """

    def __init__(self, message: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


@dataclass(frozen=True)
class DaemonRuntime:
    op_registry: OpRegistry
    loaded_op_registry_digest: str
    dispatch_table: frozenset[str]
    host_operation_families: frozenset[str]

    @property
    def runtime_subset_digest(self) -> str:
        return gate32a.runtime_subset_digest(self.dispatch_table)

    def startup_record(self) -> str:
        """One canonical JSON line for the daemon's start log (T-2924 reads it)."""

        return canonical_json(
            {
                "schema": STARTUP_RECORD_SCHEMA,
                "op_registry_digest": self.loaded_op_registry_digest,
                "dispatch_table": sorted(self.dispatch_table),
                "runtime_subset_digest": self.runtime_subset_digest,
            }
        )

    def refuse_outside_table(self, kinds: Iterable[str], *, manifest_revision: str | None = None) -> None:
        """Plan-composer guard (ORG-082 §2): a substrate kind in a resolved plan
        is refused with ``SubstrateFamilyNotExposed`` before partition, so it can
        never become seat-local residue. Other non-table kinds keep the existing
        partition / envelope treatment."""

        for kind in sorted(set(kinds)):
            if kind not in self.dispatch_table and kind in self.op_registry.substrate_kinds:
                raise SubstrateFamilyNotExposed(kind, manifest_revision=manifest_revision)


def load_daemon_runtime(
    *,
    host_operation_families: Iterable[str],
    dispatch_kinds: Mapping[str, str] | Iterable[str] = EXECUTOR_KIND_CLASSES,
    implemented_classes: Mapping[str, frozenset[str]] = IMPLEMENTED_EXECUTION_CLASSES,
    registry_loader: Callable[[], OpRegistry] = load_checkout_op_registry,
    substrate_families: frozenset[str] = ORG082_SUBSTRATE_FAMILIES,
    geteuid: Callable[[], int] = os.geteuid,
) -> DaemonRuntime:
    """Load the registry, capture its digest, build and assert the dispatch table."""

    if geteuid() == 0:
        raise DaemonStartRefused("the broker daemon runs unprivileged; refusing to start as uid 0 (ORG-052 gate 19)")
    try:
        registry = registry_loader()
    except OpRegistryError as exc:
        raise DaemonStartRefused(f"op registry failed to load: {exc}", exc) from None
    # Step 3: the digest captured at THIS load (ORG-082 D-D).
    digest = registry.digest
    table = frozenset(dispatch_kinds)
    families = frozenset(host_operation_families)
    substrate = frozenset(substrate_families) | registry.substrate_kinds
    implemented_kinds = frozenset(
        kind for kind, klass in EXECUTOR_KIND_CLASSES.items()
        if any(klass in classes for classes in implemented_classes.values())
    )

    # Step 5: structural exclusion, asserted at start.
    for label, names in (
        ("dispatch table", table),
        ("host operation families", families),
        ("implemented execution kinds", implemented_kinds),
    ):
        leaked = sorted(names & substrate)
        if leaked:
            cause = SubstrateFamilyNotExposed(leaked[0])
            raise DaemonStartRefused(f"substrate families in the daemon {label}: {leaked} (ORG-082 §1)", cause)
    implemented_class_names = frozenset(c for classes in implemented_classes.values() for c in classes)
    if SUBSTRATE_CLASS in implemented_class_names:
        raise DaemonStartRefused(
            f"the substrate capability class {SUBSTRATE_CLASS!r} is in an implemented execution class (ORG-082 §1)",
            SubstrateFamilyNotExposed(SUBSTRATE_CLASS),
        )
    projection = gate32a.daemon_projection(registry)
    if table != projection:
        cause = RuntimeSubsetMismatch("dispatch table != op-registry runtime projection")
        raise DaemonStartRefused(
            f"dispatch table is not the registry's runtime projection: extra {sorted(table - projection)}, "
            f"missing {sorted(projection - table)} (ORG-082 §2a)",
            cause,
        )
    return DaemonRuntime(registry, digest, table, families)


def gate32a_check(
    *,
    root_digest: str,
    startup_record: str,
    expected_union: frozenset[str],
    expected_digest: str,
    substrate_kinds: frozenset[str] = ORG082_SUBSTRATE_FAMILIES,
) -> list[str]:
    """Gate 32a over a daemon's ``startup_record()`` (out of process; T-2924).

    The same four checks ``gate32a.check`` makes over in-process registries:
    digest equality, projection == union, projection digest == expected, no
    substrate family in either. Empty list when the gate holds.
    """

    import json

    record = json.loads(startup_record)
    if not isinstance(record, dict) or record.get("schema") != STARTUP_RECORD_SCHEMA:
        return [f"RUNTIME_SUBSET_MISMATCH: startup record schema is not {STARTUP_RECORD_SCHEMA}"]
    failures = []
    if record.get("op_registry_digest") != root_digest:
        failures.append(
            f"{RegistryDigestMismatch.code}: expected {root_digest}, daemon loaded {record.get('op_registry_digest')}"
        )
    projection = frozenset(record.get("dispatch_table") or ())
    if projection != expected_union:
        failures.append(
            f"{RuntimeSubsetMismatch.code}: extra {sorted(projection - expected_union)}, "
            f"missing {sorted(expected_union - projection)}"
        )
    if gate32a.runtime_subset_digest(projection) != expected_digest or record.get("runtime_subset_digest") != expected_digest:
        failures.append(f"{RuntimeSubsetMismatch.code}: projection digest != expected-runtime-subset digest")
    leaked = sorted((projection | expected_union) & substrate_kinds)
    if leaked:
        failures.append(f"{SubstrateFamilyNotExposed.code} violated: {leaked}")
    return failures
