"""Persona-agnostic optionals registry + lifecycle (T-2556 phase 11 / T-2561).

Module discovery. A persona's optional acceptance modules live at
``tools/bringup_acceptance/optionals/<persona>.py``; :func:`discover_optional_modules`
dotted-imports that file and calls its ``build_optional_modules()`` entry
point. No file for a given persona is NOT an error -- it is the bare-daemon,
core-only case this ticket's ask names: a new daemon with no custom surface
yet simply has zero optional modules to run. Anything else -- a plug-in
that exists but is broken, returns the wrong shape, or lies about its own
tier -- is loud (:class:`~tools.bringup_acceptance.types.HardStop`), never
silently folded into the same empty-tuple "absent" return, per T-2556's
fail-closed / no-inferred-green rigor bar.

This module contains NO per-persona branches (no ``if persona == "recovery"``
anywhere) -- every persona is discovered through the identical dotted-import
+ entry-point contract; core never needs to know which personas exist or
what their optional subsystems are.

applies|absent self-declaration. :func:`run_optional_module` is the
lifecycle glue: it calls ``declare(target)`` first, and when the module
itself says it does not apply to this target, returns a ``declared_absent``
row WITHOUT ever calling ``prove``/``restore``/``report`` -- mirroring
``hook_emissions.py``'s own documented "that row shape is the
ORCHESTRATOR's to build straight from declare()" precedent. This is what
makes an optional module cleanly SKIPPABLE: the caller (core, or whatever
later phase-15 orchestrator wires this in) never has to know WHY a
persona's subsystem is or isn't present on a given target, only that this
function always returns a complete, closure-ready row.
"""

from __future__ import annotations

import importlib
from typing import Any

from tools.bringup_acceptance.recorder import validate_token
from tools.bringup_acceptance.types import (
    AcceptanceModule,
    HardStop,
    MatrixRow,
    RunContext,
    TargetInventory,
)


OPTIONALS_PACKAGE = "tools.bringup_acceptance.optionals"
BUILD_ENTRY_POINT = "build_optional_modules"


def _assert_unique_module_ids(modules: tuple[AcceptanceModule, ...], *, source: str) -> None:
    seen: set[str] = set()
    for module in modules:
        if module.id in seen:
            raise HardStop(
                f"optionals plug-in {source} declares duplicate module_id {module.id!r}"
            )
        seen.add(module.id)


def discover_optional_modules(persona: str) -> tuple[AcceptanceModule, ...]:
    """Discover the optional modules ``optionals/<persona>.py`` declares.

    Returns ``()`` when the persona has no plug-in file at all -- the
    bare-daemon case. Any OTHER failure (the plug-in exists but is broken,
    or violates the contract below) raises :class:`HardStop` rather than
    being folded into that same empty-tuple return.

    Contract enforced on every returned object, independent of (never
    substituting for) the ``isinstance(..., AcceptanceModule)`` check --
    ``runtime_checkable`` isinstance is a hasattr-based presence check on
    data members like ``id``/``tier``, so a wrong-VALUE ``tier`` or a
    ``None`` ``id`` still passes it. The explicit checks here are what
    actually enforces the contract:
      * non-empty, non-None ``module.id``
      * ``module.tier == "optional"`` (persona plug-ins are always
        optional-tier; core-tier ids are reserved for the core catalog)
      * no duplicate ``module_id`` within one persona's returned set
    """

    module_name = f"{OPTIONALS_PACKAGE}.{validate_token(persona, label='persona')}"
    try:
        plugin = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return ()
        # A transitive import inside a REAL plug-in failed -- never let that
        # look like "this persona has no optionals".
        raise

    builder = getattr(plugin, BUILD_ENTRY_POINT, None)
    if builder is None or not callable(builder):
        raise HardStop(
            f"optionals plug-in {module_name} has no callable {BUILD_ENTRY_POINT}() entry point"
        )

    modules: tuple[Any, ...] = tuple(builder())
    for module in modules:
        if not isinstance(module, AcceptanceModule):
            raise HardStop(
                f"optionals plug-in {module_name} returned an object not conforming to "
                f"AcceptanceModule: {module!r}"
            )
        if not getattr(module, "id", None):
            raise HardStop(
                f"optionals plug-in {module_name} returned a module with no module_id: {module!r}"
            )
        if module.tier != "optional":
            raise HardStop(
                f"optionals plug-in {module_name} module {module.id!r} declared tier="
                f"{module.tier!r}, expected 'optional' -- persona plug-ins are always optional-tier"
            )
    _assert_unique_module_ids(modules, source=module_name)
    return modules


def run_optional_module(
    module: AcceptanceModule, *, target: TargetInventory, ctx: RunContext
) -> MatrixRow:
    """Run one optional module's full declare -> (skip | prove -> restore -> report) lifecycle.

    ``declare(target).applies is False`` short-circuits straight to a
    ``declared_absent`` row -- prove/restore/report are never invoked, so a
    module that correctly self-declares absence pays no live-proof cost.

    When it applies, ``prove()`` runs inside a ``try`` with ``restore()`` in
    the matching ``finally`` -- restore is attempted even if prove raises,
    mirroring ``producer_fires.nonce_prove_unit``'s own "always tear down,
    even on failure" pattern one level up. If restore() itself then raises
    (e.g. residue left by the failed prove), that HardStop propagates and
    can mask the original prove() failure -- the same trade-off
    ``nonce_prove_unit`` already makes; restore's own HardStop already names
    itself, so no further wrapping is added here.
    """

    declaration = module.declare(target)
    if not declaration.applies:
        return MatrixRow(
            module_id=declaration.module_id,
            tier=declaration.tier,
            status="declared_absent",
            summary=declaration.reason,
            evidence_refs=(),
        )

    try:
        evidence = module.prove(ctx)
    finally:
        module.restore(ctx)
    return module.report(evidence)


def run_persona_optionals(
    persona: str, *, target: TargetInventory, ctx: RunContext
) -> tuple[MatrixRow, ...]:
    """Discover + run every optional module for ``persona`` in one call.

    A bare daemon (no ``optionals/<persona>.py`` file) returns ``()`` here
    -- no error, no rows to add to the closure matrix, core runs standalone.
    """

    modules = discover_optional_modules(persona)
    return tuple(run_optional_module(module, target=target, ctx=ctx) for module in modules)
