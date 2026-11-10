"""Independent-agent grant consumption (ORG-080 §2 gates / "--accept-gates is consume-not-mint"; T-5135 S2).

An independent-agent gate (authority-transfer, sudoers-self-lockout, ...) is
answered ONLY by consuming a matching pre-issued grant from the named
``grant_source``. The grant store is the ORG-052 approval service
(``tools.host_control.approval``): single-use, scoped to one operation,
receipt-bearing (``authority_record_id``), fail-closed. This module never issues
or edits a grant and never imports the approval package: the engine sees only
the ``GrantConsumer`` Protocol, whose shape is ``ApprovalBinder.validate_and_bind``.
No bus, socket or live approval service is reachable from here (the simulated
tier injects an in-process consumer; production wiring is a separate, reviewed
go).

Binding. One grant binds exactly one operation. For every independent-agent
gated op the engine derives, from the plan it is about to apply:

* ``plan_handle`` / ``operation_handle`` -- sha256 of (plan digest, component,
  op id, gate id, scope), under distinct domain prefixes;
* ``binding()`` -- the explicit record of what the grant authorizes: component,
  op id, gate id, scope, op kind, the op's canonical spec digest and the
  contract digest (T-5233);
* the metadata tuple the approval side compares against the grant:
  owner = the invoking actor, performer, target = the scope, families = (op
  kind,), plan digest, ``effect_digest`` = sha256 over the canonical
  ``binding()`` record (``germinate-effect/v2``) and ``manifest_revision`` =
  the contract digest.

Scope and gate are bound, not lossy: the approval service binds only on
``plan_handle`` equality, and the plan handle commits to both, so a grant
issued for scope X or gate G1 is refused (``plan_mismatch``) for scope Y or gate
G2. They are explicit in ``effect_digest`` (which also commits to the component
and op content, so two components can never share one), in the reviewer-facing
``summary()``, in the ``grant.bound`` transcript event and in each op's
pantheon-receipt ``authority`` field. ORG-052's ``PlanMetadata`` wire is
unchanged (``"s" -> "ssssssss"``), so the approval POLICY still sees scope only
as ``target`` and the gate not at all; a gate-keyed policy needs an ORG-052 §5
amendment (optional; not built here). A bound grant replays (``bound_replay``)
for the identical tuple only until its ``expires_at``.

Fail-closed edges (each tested): no consumer wired; a gated op with no handle;
a malformed handle; any denial (closed reason code); any consumer exception
(outage, metadata unavailable); a malformed outcome. Handles are validated for
ALL gated ops before any bind, and binds run last in the gate check, so a
refusal elsewhere never burns a grant. The operation handle is deterministic, so
a resumed apply within the grant's expiry replays the original binding
(``bound_replay``) without a second grant; after expiry the replay is denied
(``expired``) and a new grant is needed.

Secrets. Handles are read from a mode-0600 file the invoking uid owns (opened
``O_NOFOLLOW``, size-capped); they are never in argv, the transcript, receipts
or error text. Only ``authority_record_id`` is recorded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MAX_GRANT_FILE_BYTES = 65536
_PLAN_PREFIX = b"germinate-plan/v1\0"
_OP_PREFIX = b"germinate-op/v1\0"
_EFFECT_PREFIX = b"germinate-effect/v2\0"
BINDING_SCHEMA = "germinate-grant-binding/v1"


class GrantRefused(Exception):
    """A typed grant refusal; ``code`` is ``grant-required`` or ``grant-refused``."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class GrantConsumer(Protocol):
    """``ApprovalBinder.validate_and_bind``: returns ``.state`` / ``.authority_record_id`` / ``.reason_code``."""

    def validate_and_bind(self, approval_handle: str, plan_handle: str, expected_digest: str, operation_handle: str) -> Any: ...


def _h(prefix: bytes, *parts: str) -> str:
    return hashlib.sha256(prefix + "\0".join(parts).encode("utf-8")).hexdigest()


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def op_spec_digest(op: Any) -> str:
    """sha256 over the operation's canonical JSON (every execution-affecting field of the op)."""

    return hashlib.sha256(_canonical(op.to_json_obj())).hexdigest()


@dataclass(frozen=True)
class GrantRequest:
    """What one independent-agent gated operation needs a grant for."""

    component: str
    op_id: str
    gate_id: str
    kind: str
    scope: str
    plan_digest: str  # 64 hex, no prefix
    contract_digest: str
    actor: str
    performer: str
    op_spec_digest: str  # 64 hex: op_spec_digest(op)

    @property
    def _parts(self) -> tuple[str, ...]:
        return (self.plan_digest, self.component, self.op_id, self.gate_id, self.scope)

    @property
    def plan_handle(self) -> str:
        return _h(_PLAN_PREFIX, *self._parts)

    @property
    def operation_handle(self) -> str:
        return _h(_OP_PREFIX, *self._parts)

    def binding(self) -> dict[str, str]:
        """The explicit record this grant authorizes (actor-free: ORG-052 §4 effect digest excludes owner/performer)."""

        return {
            "schema": BINDING_SCHEMA,
            "component": self.component,
            "op_id": self.op_id,
            "gate_id": self.gate_id,
            "scope": self.scope,
            "kind": self.kind,
            "op_spec_digest": self.op_spec_digest,
            "contract_digest": self.contract_digest,
        }

    @property
    def effect_digest(self) -> str:
        return hashlib.sha256(_EFFECT_PREFIX + _canonical(self.binding())).hexdigest()

    def summary(self) -> str:
        """Bounded canonical JSON for the approval ``PlanSummary`` seam: names the gate and scope to the reviewer."""

        return _canonical(dict(self.binding(), plan_digest=self.plan_digest, effect_digest=self.effect_digest)).decode("ascii")

    def metadata_fields(self, policy_revision: str) -> dict[str, Any]:
        """The ORG-052 ``PlanMetadata`` fields for this operation (the approval-side seam builds the value)."""

        return {
            "owner": self.actor,
            "performer": self.performer,
            "target_seat_id": self.scope,
            "operation_families": (self.kind,),
            "plan_digest": self.plan_digest,
            "effect_digest": self.effect_digest,
            "manifest_revision": self.contract_digest,
            "policy_revision": policy_revision,
        }


def derive_requests(merged: Any, contracts: Mapping[str, Any], *, actor: str, performer: str) -> list[GrantRequest]:
    """One request per independent-agent gated op in the plan (reserved-authority gates never reach here)."""

    digest = merged.digest.removeprefix("sha256:")
    out = []
    for node in merged.nodes:
        contract = contracts[node.component]
        for op in node.operations:
            if op.gate is None or op.gate in contract.reserved_authority:
                continue
            if contract.gates[op.gate].approval_class != "independent-agent":
                continue
            out.append(
                GrantRequest(
                    node.component, op.operation_id, op.gate, op.kind, node.scope, digest, contract.digest, actor, performer,
                    op_spec_digest(op),
                )
            )
    return out


def read_grant_file(path: Path, uid: int) -> dict[str, str]:
    """``{operation_id: approval_handle}`` from a 0600 file owned by ``uid`` (lines: ``<op-id> <64-hex>``)."""

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        raise GrantRefused("grant-refused", f"grant file {path.name!r} cannot be opened (no symlinks): {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise GrantRefused("grant-refused", "grant file is not a regular file")
        if st.st_uid != uid:
            raise GrantRefused("grant-refused", "grant file is not owned by the invoking uid")
        if stat.S_IMODE(st.st_mode) & 0o177:
            raise GrantRefused("grant-refused", "grant file must be mode 0600 (or stricter)")
        if st.st_size > MAX_GRANT_FILE_BYTES:
            raise GrantRefused("grant-refused", "grant file is oversized")
        data = os.read(fd, MAX_GRANT_FILE_BYTES + 1)
    finally:
        os.close(fd)
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise GrantRefused("grant-refused", "grant file is not ASCII") from None
    grants: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 2 or not _HEX64.match(fields[1]):
            raise GrantRefused("grant-refused", f"grant file line {number} is not '<operation-id> <64 lowercase hex>'")
        if fields[0] in grants:
            raise GrantRefused("grant-refused", f"grant file names operation {fields[0]!r} twice")
        grants[fields[0]] = fields[1]
    return grants


def consume(
    requests: Sequence[GrantRequest],
    handles: Mapping[str, str] | None,
    consumer: GrantConsumer | None,
) -> list[dict[str, str]]:
    """Bind one grant per request, fail closed. Returns ``[{component, op_id, gate, scope, authority_record_id}]`` (no handles)."""

    if not requests:
        return []
    if consumer is None:
        raise GrantRefused("grant-required", f"{requests[0].op_id}: gate {requests[0].gate_id} needs a pre-issued independent-agent grant and no grant consumer is wired")
    handles = handles or {}
    for req in requests:
        handle = handles.get(req.op_id)
        if handle is None:
            raise GrantRefused("grant-required", f"{req.op_id}: gate {req.gate_id} needs a pre-issued independent-agent grant; none was supplied")
        if not (isinstance(handle, str) and _HEX64.match(handle)):
            raise GrantRefused("grant-refused", f"{req.op_id}: the supplied grant handle is malformed")
    bound = []
    for req in requests:
        try:
            outcome = consumer.validate_and_bind(handles[req.op_id], req.plan_handle, req.plan_digest, req.operation_handle)
        except Exception as exc:  # noqa: BLE001 -- outage / unavailable metadata / anything else is a refusal, never a pass
            raise GrantRefused("grant-refused", f"{req.op_id}: the grant source could not confirm the grant ({type(exc).__name__})") from None
        state = getattr(outcome, "state", None)
        record_id = getattr(outcome, "authority_record_id", None)
        if state != "bound":
            reason = getattr(outcome, "reason_code", "unknown")
            raise GrantRefused("grant-refused", f"{req.op_id}: grant denied ({reason if isinstance(reason, str) else 'unknown'})")
        if not (isinstance(record_id, str) and _HEX64.match(record_id)):
            raise GrantRefused("grant-refused", f"{req.op_id}: the grant source returned no authority record")
        bound.append(
            {"component": req.component, "op_id": req.op_id, "gate": req.gate_id, "scope": req.scope, "authority_record_id": record_id}
        )
    return bound
