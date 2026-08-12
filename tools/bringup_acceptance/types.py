"""Core scaffolding types for the bringup-acceptance suite (T-2558 phase 1).

These are the shapes every later acceptance module plugs into: a
:class:`RunContext` naming the target and its disposable state, a
:class:`TargetInventory` describing what the runner discovered on that
target (discovery itself is a later phase), the :class:`AcceptanceModule`
protocol a subsystem module implements, and the thin evidence/report
dataclasses that flow between a module and the closure emitter.

Nothing here is persona- or harness-specific: `persona`/`account`/`harness`/
`profile` are plain strings supplied by the caller (normally resolved from
`instance.yaml` via `tools.agent_role_map`), never hardcoded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, runtime_checkable


Tier = Literal["core", "optional"]
InventoryStatus = Literal["applies", "absent"]
ModuleEvidenceStatus = Literal["proven", "gap", "hard_stop"]
MatrixRowStatus = Literal["proven", "declared_absent", "gap", "hard_stop"]


class HardStop(RuntimeError):
    """Raised by a module's ``prove()``/``restore()`` on any anomaly.

    Any modal state, dropped delivery, stale metadata, incomplete handoff,
    or restore residue is a HardStop, not a soft failure the caller can
    paper over -- per T-2556's hard-stop-on-anomaly requirement.
    """


@dataclass(frozen=True)
class RunContext:
    """One acceptance run's identity, target, and disposable working state."""

    run_id: str
    persona: str
    account: str
    harness: str
    profile: str
    hub_path: Path
    state_path: Path
    evidence_root: Path
    baseline_ref: str


@dataclass(frozen=True)
class InventoryPathEntry:
    """One discovered producer/delivery path on the target, tagged for closure."""

    path: Path
    tier: Tier
    status: InventoryStatus
    module_id: str


@dataclass(frozen=True)
class TargetInventory:
    """The discovered path set for one persona target.

    Discovery (walking timers/units/hooks/mailbox config to populate
    ``entries``) is phase 2; this phase only fixes the shape.
    """

    persona: str
    entries: tuple[InventoryPathEntry, ...]


@dataclass(frozen=True)
class ModuleDeclaration:
    """A module's answer to "does this target even have my subsystem?".

    ``contributes_to`` names the composite capabilities (see
    :mod:`tools.bringup_acceptance.closure`) this module's proof feeds --
    a module never self-reports a composite as proven; only the closure
    emitter does, and only once every module the composite requires is
    itself proven.
    """

    module_id: str
    tier: Tier
    applies: bool
    reason: str
    contributes_to: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModuleEvidence:
    """A module's proof (or gap) for one run, referencing its own trace/evidence files."""

    module_id: str
    status: ModuleEvidenceStatus
    trace_path: Path
    evidence_paths: tuple[Path, ...]
    detail: Mapping[str, Any]


@dataclass(frozen=True)
class RestoreEvidence:
    """A module's proof that it left the target exactly as it found it."""

    module_id: str
    restored: bool
    detail: Mapping[str, Any]


@dataclass(frozen=True)
class MatrixRow:
    """One row of the human/machine closure matrix."""

    module_id: str
    tier: Tier
    status: MatrixRowStatus
    summary: str
    evidence_refs: tuple[Path, ...]


@runtime_checkable
class AcceptanceModule(Protocol):
    """The contract every per-subsystem acceptance module implements.

    ``declare`` decides whether the module applies to a given target's
    inventory; ``prove`` re-observes ground truth and either returns proof
    or raises :class:`HardStop`; ``restore`` undoes anything ``prove`` set
    up (and itself raises :class:`HardStop` on any residue); ``report``
    turns one run's evidence into a closure-matrix row.
    """

    id: str
    tier: Tier

    def declare(self, target: TargetInventory) -> ModuleDeclaration: ...

    def prove(self, ctx: RunContext) -> ModuleEvidence: ...

    def restore(self, ctx: RunContext) -> RestoreEvidence: ...

    def report(self, evidence: ModuleEvidence) -> MatrixRow: ...
