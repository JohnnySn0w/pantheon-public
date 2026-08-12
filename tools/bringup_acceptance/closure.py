"""Closure-matrix emitter: per-module evidence -> GREEN/RED verdict + gap list.

Per T-2556: a green result must name every tested subsystem, and any
untested or unverified path must be an explicit named gap -- never an
inferred green borrowed from a sibling module.  The rule this module
enforces at the per-module level:

* every **core** module's row must be ``"proven"``, or it is a gap.
* every **optional** module's row must be ``"proven"`` or
  ``"declared_absent"`` (the module looked, ground-truthed the target
  doesn't have that subsystem, and said so) -- anything else is a gap.

The same rule applies one level up, at **composite capabilities**
(e.g. "mailbox is fully functional" = substrate + live-delivery +
wake-trigger, per T-2556 phase 15): a composite is GREEN only when
every module_id it requires is itself proven.  A single constituent
module's green must never be read as the composite being proven --
per-module rows and composite rows stay distinct rows in the matrix.
Phase 1 ships the field, this logic, and an empty composites map;
concrete capability definitions land in phase 15.

Status is GREEN only when the gap list is empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from tools.bringup_acceptance.types import MatrixRow


@dataclass(frozen=True)
class CompositeRow:
    """One composite-capability row: GREEN iff every constituent is proven."""

    capability_id: str
    status: str  # "GREEN" | "RED"
    required_module_ids: tuple[str, ...]
    missing_module_ids: tuple[str, ...]


@dataclass(frozen=True)
class ClosureResult:
    run_id: str
    status: str  # "GREEN" | "RED"
    rows: tuple[MatrixRow, ...]
    composite_rows: tuple[CompositeRow, ...]
    gaps: tuple[str, ...]


def build_closure(
    run_id: str,
    rows: Sequence[MatrixRow],
    composites: Mapping[str, Sequence[str]] | None = None,
) -> ClosureResult:
    """Build the closure verdict from per-module rows plus an optional composite map.

    ``composites`` maps ``capability_id -> required module_ids``.  Phase 1
    callers pass nothing (or ``{}``) -- no composite rows, no change in
    behavior from before this capability existed.
    """

    gaps: list[str] = []
    for row in rows:
        if row.tier == "core" and row.status != "proven":
            gaps.append(f"{row.module_id}: core module not proven (status={row.status})")
        elif row.tier == "optional" and row.status not in ("proven", "declared_absent"):
            gaps.append(
                f"{row.module_id}: optional module not proven or declared absent "
                f"(status={row.status})"
            )

    proven_module_ids = {row.module_id for row in rows if row.status == "proven"}
    composite_rows: list[CompositeRow] = []
    for capability_id, required in (composites or {}).items():
        required_ids = tuple(required)
        missing = tuple(module_id for module_id in required_ids if module_id not in proven_module_ids)
        composite_status = "GREEN" if not missing else "RED"
        if missing:
            gaps.append(
                f"{capability_id}: composite capability missing constituent(s): "
                + ", ".join(missing)
            )
        composite_rows.append(
            CompositeRow(
                capability_id=capability_id,
                status=composite_status,
                required_module_ids=required_ids,
                missing_module_ids=missing,
            )
        )

    status = "GREEN" if not gaps else "RED"
    return ClosureResult(
        run_id=run_id,
        status=status,
        rows=tuple(rows),
        composite_rows=tuple(composite_rows),
        gaps=tuple(gaps),
    )


def render_closure_markdown(closure: ClosureResult) -> str:
    lines = [
        f"# Bringup Acceptance Closure — {closure.run_id}",
        "",
        f"**Status:** {closure.status}",
        "",
        "## Modules",
        "",
        "| module_id | tier | status | summary |",
        "|---|---|---|---|",
    ]
    for row in closure.rows:
        lines.append(f"| {row.module_id} | {row.tier} | {row.status} | {row.summary} |")
    if closure.composite_rows:
        lines.extend(
            ["", "## Composite capabilities", "", "| capability_id | status | required | missing |", "|---|---|---|---|"]
        )
        for composite in closure.composite_rows:
            lines.append(
                f"| {composite.capability_id} | {composite.status} | "
                f"{', '.join(composite.required_module_ids)} | "
                f"{', '.join(composite.missing_module_ids) or 'none'} |"
            )
    lines.extend(["", "## GAPS", ""])
    if closure.gaps:
        lines.extend(f"- {gap}" for gap in closure.gaps)
    else:
        lines.append("none")
    lines.append("")
    return "\n".join(lines)
