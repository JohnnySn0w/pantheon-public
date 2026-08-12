"""Phase-15 composite-capability resolution (T-2556 phase 15 / T-2577).

:data:`CORE_COMPOSITES` is the runner's composites map, passed straight
through to :func:`tools.bringup_acceptance.closure.build_closure` --
the authoritative constituent lists for the six composite ids ratified so
far: ``mailbox-fully-functional`` / ``telegram-fully-functional`` from the
original T-2559 spine, ``harness-runtime-fully-functional`` /
``harness-fallback-swap-fully-functional`` / ``forced-cap-return-fully-
functional`` ratified 2026-08-05 (ledger/2026-W32/2026-W32.jsonl, entries
2026-08-05T06:19:47Z / 07:15:43Z / 07:48:43Z), plus
``urgent-mail-preempt-fully-functional`` added by T-2585 to formalize the
urgent-mail-preempt capability (T-1641) into the closure matrix -- a new,
separate composite rather than a widened ``mailbox-fully-functional``,
since it is its own subsystem/ticket the same way harness-fallback-swap and
forced-cap-return each get their own composite despite also touching
mailbox internals.

Every ``<x>-live`` id (and every telegram leg besides ``telegram-outbound``)
names a module T-2570/T-2572/T-2574/T-2582 (or, for urgent-mail-preempt, a
future live-acceptance ticket) have not landed yet -- deliberately absent
from every run's ``rows``, so ``build_closure``'s own missing-constituent
computation names the gap on its own. No placeholder row is fabricated for
a module that does not exist -- the composite's ``missing_module_ids``
field is the correct place that surfaces. (``urgent_mail_preempt.py``'s own
module docstring separately cites an already-happened, ledger-attested live
proof as prose evidence -- that citation is deliberately NOT wired into this
map, so the composite still reads RED here until a real, repeatable live
module lands.)

The >=1-of wake-trigger constituent of ``mailbox-fully-functional`` cannot
be expressed directly in ``build_closure``'s all-of composites map.
Rather than widen ``closure.py`` (a phase-1-shipped, deliberately minimal
API), this module PRE-RESOLVES ``producer_fires.reconcile_wake_trigger``'s
own >=1-of verdict -- shipped explicitly as a "phase-15 composite wire-
point, not yet called live" -- into one ordinary synthetic
:class:`~tools.bringup_acceptance.types.MatrixRow`
(:data:`WAKE_TRIGGER_MODULE_ID`), so the existing all-of gate handles it
with zero ``closure.py`` changes.
"""

from __future__ import annotations

from typing import Mapping

from tools.bringup_acceptance import (
    forced_cap_return,
    harness_fallback_swap,
    harness_runtime,
    mailbox_roundtrip,
    urgent_mail_preempt,
)
from tools.bringup_acceptance.producer_fires import reconcile_wake_trigger
from tools.bringup_acceptance.telegram_outbound import MODULE_ID as TELEGRAM_OUTBOUND_MODULE_ID
from tools.bringup_acceptance.types import MatrixRow


# Not-yet-landed LIVE module ids (T-2570 / T-2572 / T-2574 / T-2585's own live
# leg) -- named here so CORE_COMPOSITES stays readable; must never appear in
# any run's rows.
HARNESS_RUNTIME_LIVE_ID = "harness-runtime-live"
HARNESS_FALLBACK_SWAP_LIVE_ID = "harness-fallback-swap-live"
FORCED_CAP_RETURN_LIVE_ID = "forced-cap-return-live"
# T-2585's own composite is a self-pair like the three above: an informal
# live proof already happened (see urgent_mail_preempt.py's module docstring
# for the ledger citation) but that is prose evidence, not a landed,
# repeatable acceptance module -- so this id stays deliberately absent from
# every run's rows until one lands, same as its three siblings.
URGENT_MAIL_PREEMPT_LIVE_ID = "urgent-mail-preempt-live"

# Not-yet-landed telegram composite legs (original T-2559 spine; the
# dedicated non-operator test-chat infra they need is T-2582, pending).
TELEGRAM_INBOUND_ID = "telegram-inbound"
TELEGRAM_USER_VISIBLE_ID = "telegram-user-visible"
TELEGRAM_FAILURE_MODES_ID = "telegram-failure-modes"

MAILBOX_FULLY_FUNCTIONAL_ID = "mailbox-fully-functional"
TELEGRAM_FULLY_FUNCTIONAL_ID = "telegram-fully-functional"

# The pre-resolved >=1-of wake-trigger synthetic constituent -- see module
# docstring. Tier "core": this is a genuine proof obligation, not a free
# pass, so a neither-installed target still gates the composite honestly.
WAKE_TRIGGER_MODULE_ID = "mailbox-wake-trigger"


CORE_COMPOSITES: Mapping[str, tuple[str, ...]] = {
    harness_runtime.COMPOSITE_ID: (harness_runtime.MODULE_ID, HARNESS_RUNTIME_LIVE_ID),
    harness_fallback_swap.COMPOSITE_ID: (harness_fallback_swap.MODULE_ID, HARNESS_FALLBACK_SWAP_LIVE_ID),
    forced_cap_return.COMPOSITE_ID: (forced_cap_return.MODULE_ID, FORCED_CAP_RETURN_LIVE_ID),
    urgent_mail_preempt.COMPOSITE_ID: (urgent_mail_preempt.MODULE_ID, URGENT_MAIL_PREEMPT_LIVE_ID),
    MAILBOX_FULLY_FUNCTIONAL_ID: (
        mailbox_roundtrip.MODULE_ID, HARNESS_RUNTIME_LIVE_ID, WAKE_TRIGGER_MODULE_ID,
    ),
    TELEGRAM_FULLY_FUNCTIONAL_ID: (
        TELEGRAM_INBOUND_ID, TELEGRAM_OUTBOUND_MODULE_ID, TELEGRAM_USER_VISIBLE_ID, TELEGRAM_FAILURE_MODES_ID,
    ),
}


def resolve_wake_trigger_row(
    *, timer_detail: Mapping[str, object], watch_detail: Mapping[str, object]
) -> MatrixRow:
    """Pre-resolve the >=1-of wake-trigger constituent into one synthetic core row.

    ``timer_detail``/``watch_detail`` are ``TimerCronModule``'s/
    ``WatchProducerModule``'s own ``ModuleEvidence.detail`` from this same
    run -- see ``producer_fires.reconcile_wake_trigger`` for the >=1-of
    combinator this wraps. The returned row's ``status`` is "proven" iff at
    least one installed wake-trigger candidate fired, else "gap" -- feeding
    ``mailbox-fully-functional``'s ``WAKE_TRIGGER_MODULE_ID`` constituent
    exactly like any other ordinary module_id.
    """
    verdict = reconcile_wake_trigger(timer_detail=timer_detail, watch_detail=watch_detail)
    summary = (
        "at least one of mailbox-wake.timer/mailbox-wake.path is installed and fired"
        if verdict.satisfied
        else str(verdict.gap)
    )
    return MatrixRow(
        module_id=WAKE_TRIGGER_MODULE_ID,
        tier="core",
        status="proven" if verdict.satisfied else "gap",
        summary=summary,
        evidence_refs=(),
    )
