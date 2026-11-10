"""The actuation-time privilege check (T-5941, engine seam gap S7; ORG-080 A-3, ORG-082 §1).

CLASSIFICATION, not a deny-list. ``refusal`` returns None only for a KNOWN,
PERMITTED combination of (op privilege, backend ceiling, node scope, backend
binding, op owner, invocation account). Anything this module has never heard of
(an unknown privilege, an unparseable scope, a ceiling holding a stray value) is
refused too: fail closed (P1).

Classes, each independent (a refusal in any one refuses the op):

* C1  op.privilege is one of PRIVILEGES (none, seat, root).
* C2  op.privilege is in backend.privilege_ceiling. A ceiling that holds an
      unknown value never admits that value (C1 runs first).
* C3  privilege vs node scope. ``system``: none|seat|root. ``seat-<acct>``:
      none|seat only; a root op in a seat scope is refused (ORG-080 A-3,
      ORG-082 §1).
* C4  node scope vs backend.bound_account. Unbound (None) admits any scope. A
      bound backend admits its own ``seat-<bound_account>`` scope; a ``system``
      scope on a bound backend admits privilege ``none`` only.
* C5  seat op owner vs bound_account. Unbound, or a non-seat op, or owner ==
      bound_account, or owner == the planning component's id (the component-label
      owners ``action-broker`` and ``watch``; exactly that, nothing broader).
* C6  ctx.account. None imposes no constraint. Otherwise it must equal the
      bound_account (when set) and the scope account (for a seat scope), and, in
      EVERY scope (system included), it must equal the owner of a seat op whose
      owner is an account (owner != the planning component's id). The component-
      label owners (``action-broker``, ``watch``) are exempt; a context that sets
      an account never actuates a seat op owned by another account.

Scope SHAPE is not re-validated here: ``refusal`` relies on ``backend.validate_scope``
having run first (the executor does so at plan entry). On an unbound backend a
malformed scope such as ``"seat-a "`` is admitted by ``refusal`` alone; the sim
backend's ``_SEAT_SCOPE_RE`` refuses it before this check is reached.

Accounts are compared as strings; there is no persona lookup (the seat backend
binds persona -> account at construction, T-5940). The check is pure and emits
nothing on success, so a permitted plan's digest, transcript and receipts are
byte-identical to before.
"""

from __future__ import annotations

from typing import Any, Final

from tools.germinate.engine.context import InvocationContext
from tools.germinate.op_registry import PRIVILEGES

SYSTEM_SCOPE: Final = "system"
SEAT_SCOPE_PREFIX: Final = "seat-"

#: Privileges a node scope admits (C3).
_SCOPE_PRIVILEGES: Final = {
    "system": frozenset(PRIVILEGES),
    "seat": frozenset({"none", "seat"}),
}


def _scope_account(node_scope: str) -> tuple[bool, str | None]:
    """(known, account): ``system`` -> (True, None); ``seat-<acct>`` -> (True, acct); else (False, None)."""

    if node_scope == SYSTEM_SCOPE:
        return True, None
    if isinstance(node_scope, str) and node_scope.startswith(SEAT_SCOPE_PREFIX) and len(node_scope) > len(SEAT_SCOPE_PREFIX):
        return True, node_scope[len(SEAT_SCOPE_PREFIX):]
    return False, None


def refusal(backend: Any, ctx: InvocationContext, node_scope: str, op: Any, component: str) -> str | None:
    """None when the op is a known permitted combination for this backend/scope/context, else the reason."""

    op_id = getattr(op, "operation_id", "?")
    privilege = getattr(op, "privilege", None)
    owner = getattr(op, "owner", None)
    head = f"op {op_id} (privilege {privilege}, owner {owner}, scope {node_scope})"

    if privilege not in PRIVILEGES:  # C1
        return f"{head}: privilege is not one of {', '.join(PRIVILEGES)} (ORG-080 A-3)"
    if privilege not in backend.privilege_ceiling:  # C2
        return f"{head}: exceeds the backend privilege ceiling {sorted(backend.privilege_ceiling)} (ORG-080 A-3)"

    known, scope_acct = _scope_account(node_scope)
    if not known:  # C3, unknown scope shape
        return f"{head}: scope is neither 'system' nor 'seat-<account>'"
    scope_class = "system" if scope_acct is None else "seat"
    if privilege not in _SCOPE_PRIVILEGES[scope_class]:  # C3
        return f"{head}: privilege {privilege} is not permitted in a {scope_class} scope (ORG-080 A-3, ORG-082 §1)"

    bound = backend.bound_account
    if bound is not None:
        if scope_acct is None:  # C4
            if privilege != "none":
                return f"{head}: a backend bound to {bound} admits only privilege none in the system scope"
        elif scope_acct != bound:
            return f"{head}: a backend bound to {bound} cannot actuate scope {node_scope}"
        if privilege == "seat" and owner != bound and owner != component:  # C5
            return f"{head}: a backend bound to {bound} cannot actuate a seat op owned by {owner}"

    acct = ctx.account
    if acct is not None:  # C6
        if bound is not None and acct != bound:
            return f"{head}: invocation account {acct} is not the backend's bound account {bound}"
        if scope_acct is not None and acct != scope_acct:
            return f"{head}: invocation account {acct} is not the scope account {scope_acct}"
        if privilege == "seat" and owner != component and owner != acct:
            return f"{head}: invocation account {acct} does not own this seat op (owner {owner})"
    return None
