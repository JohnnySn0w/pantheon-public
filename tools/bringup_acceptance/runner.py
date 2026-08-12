"""End-to-end acceptance runner (T-2556 phase 15 / T-2577).

Wires every landed catalog module (hook_emissions' five events, mailbox_
roundtrip, producer_fires' timer-cron/watch-producer/unit-failure-alert,
harness_runtime, harness_fallback_swap, forced_cap_return, telegram_outbound)
plus a persona's discovered optional plug-ins (registry.py) into one run,
resolves the mailbox-wake >=1-of composite constituent (composites.py), and
emits + persists the closure matrix (closure.py + evidence.py) -- the T-2556
phase-15 capstone.

Construction of each catalog module (its own fakes/mocks for the disposable/
off-prod legs -- a fake FireHarness, an injected SendFn, a mocked
``persona_bringup.diagnose_persona``, etc.) stays the CALLER's job, exactly
as established by that module's own test suite. This runner is deliberately
just the wiring, never a second copy of any module's fixture-construction
logic (minimal footprint / stay in file scope).

Discovery. ``target: TargetInventory`` is taken as an ALREADY-BUILT input
(the phase-2 output of ``inventory.build_inventory``) -- mirrors
``inventory.py``'s own "live gathering is a later wiring concern outside
this module" precedent; this runner never shells out to systemctl/
settings.json/telegram env itself.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from tools.bringup_acceptance import registry
from tools.bringup_acceptance.closure import ClosureResult, build_closure
from tools.bringup_acceptance.composites import CORE_COMPOSITES, resolve_wake_trigger_row
from tools.bringup_acceptance.evidence import RunPaths, write_closure, write_inventory, write_run_manifest
from tools.bringup_acceptance.producer_fires import TIMER_CRON_ID, WATCH_PRODUCER_ID
from tools.bringup_acceptance.types import (
    AcceptanceModule,
    MatrixRow,
    ModuleEvidence,
    RunContext,
    TargetInventory,
)


def run_module_lifecycle(
    module: AcceptanceModule, *, target: TargetInventory, ctx: RunContext
) -> tuple[MatrixRow, ModuleEvidence | None]:
    """declare -> (skip | prove -> restore -> report), tier-aware on skip.

    Mirrors ``registry.run_optional_module``'s declare-short-circuit shape,
    but is NOT that function: a non-applying CORE module is a named GAP
    (``closure.py``'s own core-tier rule -- a core row must be "proven" or
    it is a gap), never "declared_absent" -- that status is reserved for a
    module that looked and correctly said absence is fine, which
    ``closure.py`` only accepts from optional-tier rows. Reusing
    ``run_optional_module`` here (which hardcodes "declared_absent"
    regardless of tier) would mislabel a non-applying core module as a
    legitimate absence instead of the gap it actually is.
    """
    declaration = module.declare(target)
    if not declaration.applies:
        status = "declared_absent" if declaration.tier == "optional" else "gap"
        return (
            MatrixRow(
                module_id=declaration.module_id, tier=declaration.tier, status=status,
                summary=declaration.reason, evidence_refs=(),
            ),
            None,
        )
    try:
        evidence = module.prove(ctx)
    finally:
        module.restore(ctx)
    return module.report(evidence), evidence


def run_end_to_end(
    ctx: RunContext,
    target: TargetInventory,
    *,
    catalog_modules: Sequence[AcceptanceModule],
    composites: Mapping[str, Sequence[str]] = CORE_COMPOSITES,
) -> ClosureResult:
    """Run every catalog module + persona optionals, resolve composites, persist + return the closure.

    ``catalog_modules`` is caller-constructed (see module docstring); a
    persona with no ``optionals/<persona>.py`` file naturally contributes
    zero optional rows here (``registry.run_persona_optionals``'s own
    documented bare-daemon case) -- not a skip this function special-cases.
    """
    # Written BEFORE any module runs: producer_fires' own catalog modules
    # (AcceptanceModule.prove(ctx) has no TargetInventory parameter per the
    # fixed Protocol) re-derive the target from this file when a caller
    # doesn't thread it through directly -- see producer_fires.py's own
    # "a real orchestrator run relies on discovery having already written
    # inventory.json before prove() is called" precedent.
    paths = RunPaths(root=ctx.evidence_root, run_id=ctx.run_id)
    write_run_manifest(paths, ctx)
    write_inventory(paths, target)

    rows: list[MatrixRow] = []
    evidence_by_module: dict[str, ModuleEvidence] = {}
    for module in catalog_modules:
        row, evidence = run_module_lifecycle(module, target=target, ctx=ctx)
        rows.append(row)
        if evidence is not None:
            evidence_by_module[row.module_id] = evidence

    rows.extend(registry.run_persona_optionals(target.persona, target=target, ctx=ctx))

    timer_evidence = evidence_by_module.get(TIMER_CRON_ID)
    watch_evidence = evidence_by_module.get(WATCH_PRODUCER_ID)
    if timer_evidence is not None and watch_evidence is not None:
        rows.append(
            resolve_wake_trigger_row(timer_detail=timer_evidence.detail, watch_detail=watch_evidence.detail)
        )

    closure_result = build_closure(ctx.run_id, rows, composites)
    write_closure(paths, closure_result)
    return closure_result
