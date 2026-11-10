"""The engine invocation context (T-5243, engine seam gap S6; ORG-080 A-3, ORG-082 §1/§2).

CLASSIFICATION, not a deny-list. Every (context kind, execution_scope, privilege)
triple is either a KNOWN, PERMITTED combination listed in ``ADMITTED`` or it is
refused. A kind, scope or privilege this table has never heard of is therefore
refused too (fail closed, P1).

* ``operator``  -- a human-run invocation. Every known combination is permitted
  (today's behaviour; this is the default, so nothing changes unless a caller opts in).
  The bootstrap in-process root run is operator + root, not its own kind (ORG-082 §1).
* ``scheduled`` -- a timer-driven run. It may execute ONLY ``none|seat`` ops, in
  either execution_scope (ORG-080 A-3, read strictly by the HO 2026-11-03): a root op
  is refused even when it belongs to a staged cutover.
* ``daemon``    -- the broker daemon. Narrowest: ``execution_scope: all`` with
  ``none|seat`` only (ORG-082 §2).

``account`` is carried into the transcript for audit and is NOT checked here (S7).
The check runs at plan time (Engine._plan), before any digest, claim or run-state
write, and refuses the WHOLE plan; ops are never filtered. The helper-side
equivalent (host_substrate/helper.py) stays as defence in depth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

OPERATOR: Final = "operator"
SCHEDULED: Final = "scheduled"
DAEMON: Final = "daemon"
CONTEXT_KINDS: Final = (OPERATOR, SCHEDULED, DAEMON)

_SCOPES: Final = ("all", "staged-cutover")
_PRIVILEGES: Final = ("none", "seat", "root")

ADMITTED: Final = frozenset(
    {(OPERATOR, s, p) for s in _SCOPES for p in _PRIVILEGES}
    | {
        (SCHEDULED, "all", "none"),
        (SCHEDULED, "all", "seat"),
        (SCHEDULED, "staged-cutover", "none"),
        (SCHEDULED, "staged-cutover", "seat"),
        # No (SCHEDULED, *, "root"): HO ruling 2026-11-03 (orchestrator, strict A-3). An unattended
        # run never actuates root, even for a staged cutover; "auto-stage" means staging
        # through non-root ops.
        (DAEMON, "all", "none"),
        (DAEMON, "all", "seat"),
    }
)


@dataclass(frozen=True)
class InvocationContext:
    kind: str = OPERATOR
    account: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in CONTEXT_KINDS:
            raise ValueError(f"unknown invocation context kind {self.kind!r}; known: {', '.join(CONTEXT_KINDS)}")

    @property
    def is_operator(self) -> bool:
        return self.kind == OPERATOR


def refusal(ctx: InvocationContext, op: Any) -> str | None:
    """None when (kind, op.execution_scope, op.privilege) is a known permitted combination, else the reason."""

    scope = getattr(op, "execution_scope", None)
    privilege = getattr(op, "privilege", None)
    if (ctx.kind, scope, privilege) in ADMITTED:
        return None
    return (
        f"op {getattr(op, 'operation_id', '?')} ({scope}, privilege {privilege}) "
        f"is not admitted in the {ctx.kind} invocation context (ORG-080 A-3)"
    )
