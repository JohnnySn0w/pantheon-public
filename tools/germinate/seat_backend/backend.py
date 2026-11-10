"""``LiveSeatBackend``: the engine's live SEAT-scope host backend (T-5940, S5). DARK.

It satisfies ``tools.germinate.engine.backend.HostBackend`` and is injected with
``Engine.from_checkout(None, ..., backend_factory=live_seat_factory(persona, runner))``.
No CLI path selects it (the CLI stays simulated-only).

Scoping: ONE persona, bound to ONE account. Construction, in order:

1. the persona's own pin (``ports.LIVE_ACTUATION_ENABLED``) must be ``True``, else
   ``LiveActuationDisabled`` before any I/O;
2. the process must not be root;
3. the persona's instance.yaml account must be the account this process runs as
   (so an artisan flip run from any other account refuses);
4. the state root is the seat's own, resolved like the ORG-106 readers
   (``harness_state.state_root_from_env``) and contained in the seat's home.

After that, every scope other than ``seat-<bound account>`` is refused (``system``
included), and every op is refused before it reaches a handler unless its op-registry
privilege is ``seat`` (or ``none`` for one of the pinned read-only kinds, below), its
installer privilege is ``unprivileged`` and its owner is the bound account.
``privilege_ceiling`` and ``bound_account`` are the interface the engine's S7
actuation-time check (T-5941) reads; that check is coarse (privilege only), so the
per-kind rule lives here.

``none`` (T-6167 PR-2): the ceiling admits privilege ``none`` ONLY because the
installer's 12 read-only check kinds must run (the engine executes ``none`` ops; a
``none`` op skips only the write-ahead inverse). ``READ_ONLY_NONE_KINDS`` is the
exact, literal set of those kinds. A ``none`` kind outside it is refused at
registration and at every op; a handler may not mix ``none`` and ``seat`` kinds; and
for a ``none`` kind the core never routes a rollback verb to a handler (no
write-ahead inverse, nothing to undo) and refuses an execute that reports a change.

Handlers: only explicitly passed handlers declaring ``tier = "live-seat"`` register,
and only for seat-privilege registry kinds (plus the pinned ``none`` kinds). The
simulated handlers ``from_checkout`` discovers are DISCARDED by ``live_seat_factory``.
The installer-kind handlers live in ``install_handlers.py`` (T-6167 PR-2); the
harness-manager run-state live handlers are T-5867 scope.

Postboot: the engine's first act on every verb is the ORG-080 C3 postboot check, and a
live backend points it at the production marker (``marker.MARKER_PATH``) and boot id.
Confirming germinate-postboot has run on the seat (artisan first) is a T-5867 Phase-1
prerequisite; without it every live verb refuses ``postboot-unmet``.

Preflight: a live backend gets no preflight port from ``from_checkout`` (None,
unwired); the live preflight reader is T-5921 (tools/germinate/preflight_live.py).
"""

from __future__ import annotations

import os
import pwd
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from tools._organ_lifecycle import CommandRunner
from tools.germinate.engine.backend import BackendRefused
from tools.germinate.op_registry import OpRegistry
from tools.germinate.planner_sandbox.postboot import marker as _marker
from tools.persona_installer import (
    InstallOperation,
    MutationRecord,
    OperationEvidence,
    OperationFailed,
    PreflightResult,
)

from .ports import LiveActuationDisabled, LiveSeatPort, is_live

EVIDENCE_SCOPE = "live-seat"
HANDLER_TIER = "live-seat"
PRIVILEGE_CEILING: frozenset[str] = frozenset({"none", "seat"})
#: The ONLY ``none`` kinds this backend will serve: the installer's read-only checks.
#: A literal on purpose (no derivation from the registry, so a registry edit cannot
#: widen what a ``none`` op may do); a test pins it to the registry's ``none`` set.
READ_ONLY_NONE_KINDS: frozenset[str] = frozenset({
    "brief-check",
    "claude-mcp-server-check",
    "codex-standing-instructions-override-check",
    "codex-standing-instructions-source-check",
    "codex-standing-instructions-target-check",
    "codex-telegram-disabled-first",
    "harness-check",
    "identity-check",
    "manager-check",
    "policy-check",
    "source-check",
    "timer-enablement-absence-check",
})
_INSTALLER_SEAT_PRIVILEGE = "unprivileged"
_SEAT_PREFIX = "seat-"


class SeatBackendRefused(BackendRefused):
    """The live seat backend refused a binding, scope, handler or op."""


@dataclass(frozen=True)
class SeatIdentity:
    persona: str
    account: str
    uid: int
    state_root: Path


def _default_binding(persona: str) -> Mapping[str, Any]:
    from tools import agent_role_map

    return agent_role_map.instance_binding(persona)


def _default_state_root(default: Path) -> Path:
    from tools import harness_state

    return harness_state.state_root_from_env(default=default)


def resolve_identity(
    persona: str,
    *,
    binding: Callable[[str], Mapping[str, Any]] = _default_binding,
    pwnam: Callable[[str], Any] = pwd.getpwnam,
    euid: Callable[[], int] = os.geteuid,
    state_root: Callable[[Path], Path] = _default_state_root,
) -> SeatIdentity:
    """Bind ``persona`` to its own account, as the account this process runs as."""

    uid = euid()
    if uid == 0:
        raise SeatBackendRefused("the live seat backend never runs as root")
    account = (binding(persona) or {}).get("account")
    if not isinstance(account, str) or not account:
        raise SeatBackendRefused(f"{persona!r} has no instance.yaml account binding")
    try:
        pw = pwnam(account)
    except KeyError:
        raise SeatBackendRefused(f"account {account!r} of {persona!r} does not exist") from None
    if pw.pw_uid != uid:
        raise SeatBackendRefused(
            f"{persona!r} is seat account {account!r} (uid {pw.pw_uid}); this process runs as uid {uid}"
        )
    home = Path(os.path.realpath(pw.pw_dir))
    root = Path(os.path.realpath(state_root(home / ".local" / "state" / "pantheon")))
    if home not in root.parents:
        raise SeatBackendRefused(f"state root {root} is outside the seat's own home {home}")
    return SeatIdentity(persona, account, uid, root)


class _SeatBackendCore:
    """The backend for one bound seat. No pin check here: ``LiveSeatBackend`` is the
    only production subclass and its constructor is the dark gate (structural test)."""

    def __init__(
        self,
        identity: SeatIdentity,
        port: Any,  # the seat port: ports.LiveSeatPort in production (the ungated ops base is never named here)
        *,
        op_registry: OpRegistry,
        handlers: Sequence[Any] = (),
    ) -> None:
        self.identity = identity
        self.port = port
        self.root = Path(identity.state_root) / "germinate"
        self.op_registry = op_registry
        self.loaded_op_registry_digest = op_registry.digest
        self.evidence_scope = EVIDENCE_SCOPE
        self.privilege_ceiling = PRIVILEGE_CEILING
        self.bound_account: str | None = identity.account
        self._handlers: dict[str, Any] = {}
        self._by_kind: dict[str, Any] = {}
        for handler in handlers:
            self.register_handler(handler)

    # --- roots (the seat's own state tree) ------------------------------------

    @property
    def scope(self) -> str:
        return _SEAT_PREFIX + self.identity.account

    @property
    def gates_root(self) -> Path:
        return self.root / "gates"

    @property
    def postboot_marker_path(self) -> Path:
        return _marker.MARKER_PATH

    @property
    def boot_id_path(self) -> Path:
        return _marker.BOOT_ID_PATH

    def validate_scope(self, scope: str) -> str:
        if scope != self.scope:
            raise SeatBackendRefused(f"scope {scope!r}: this backend serves only {self.scope!r}")
        return scope

    def run_state_root(self, scope: str) -> Path:
        """Pure path computation. ``system`` is answered (the engine enumerates its store
        read-only on every run lookup) but never created: ``ensure_run_state_root`` refuses it."""

        if scope == "system":
            return self.root / "run-state"
        return self.root / "seat-state" / self.validate_scope(scope) / "run-state"

    def ensure_run_state_root(self, scope: str) -> Path:
        root = self.run_state_root(self.validate_scope(scope))
        path = self.root
        chain = [path]
        for part in root.relative_to(self.root).parts:
            path = path / part
            chain.append(path)
        for path in chain:
            if path.is_symlink():
                raise SeatBackendRefused(f"{path} is a symlink")
            if not path.exists():
                path.mkdir(mode=0o700)
                os.chmod(path, 0o700)
        return root

    def checkpoint(self, point: str, op_id: str) -> None:
        """Kill injection is simulation-only."""

    # --- handlers --------------------------------------------------------------

    def register_handler(self, handler: Any) -> None:
        name = getattr(handler, "name", type(handler).__name__)
        if getattr(handler, "tier", None) != HANDLER_TIER:
            raise SeatBackendRefused(f"handler {name!r} is not a {HANDLER_TIER!r} handler")
        privileges: set[str] = set()
        for kind in handler.kinds:
            entry = self.op_registry.get(kind)
            if entry is None:
                raise SeatBackendRefused(f"handler {name!r} serves {kind!r}, which is not an op-registry kind")
            if entry.privilege not in PRIVILEGE_CEILING:
                raise SeatBackendRefused(f"handler {name!r} serves {kind!r} of privilege {entry.privilege!r}, not seat")
            if entry.privilege == "none" and kind not in READ_ONLY_NONE_KINDS:
                raise SeatBackendRefused(f"handler {name!r} serves {kind!r}, a none kind outside the pinned read-only set")
            if kind in self._by_kind:
                raise SeatBackendRefused(f"kind {kind!r} already has a handler")
            privileges.add(entry.privilege)
        if len(privileges) > 1:
            raise SeatBackendRefused(f"handler {name!r} mixes none and seat kinds")
        for kind in handler.kinds:
            self._by_kind[kind] = handler
        self._handlers[name] = handler

    def handles(self, kind: str) -> bool:
        return kind in self._by_kind

    def _refusal(self, kind: str, owner: str, installer_privilege: str | None) -> str | None:
        """Why an op may not reach a handler here (None = admissible)."""

        entry = self.op_registry.get(kind)
        if entry is None:
            return f"{kind!r} is not an op-registry kind"
        if entry.privilege not in PRIVILEGE_CEILING:
            return f"{kind!r} declares privilege {entry.privilege!r}; the live seat backend actuates seat only"
        if entry.privilege == "none" and kind not in READ_ONLY_NONE_KINDS:
            return f"{kind!r} declares privilege 'none' but is not a pinned read-only kind"
        if installer_privilege is not None and installer_privilege != _INSTALLER_SEAT_PRIVILEGE:
            return f"{kind!r} carries installer privilege {installer_privilege!r}, not {_INSTALLER_SEAT_PRIVILEGE!r}"
        if owner != self.identity.account:
            return f"op owner {owner!r} is not the bound seat account {self.identity.account!r}"
        if kind not in self._by_kind:
            return f"the live seat backend has no handler for {kind!r}"
        return None

    def _admit(self, op: InstallOperation) -> Any:
        reason = self._refusal(op.kind, op.owner, op.privilege)
        if reason is not None:
            raise OperationFailed(f"refused: {reason}")
        return self._by_kind[op.kind]

    # --- HostBackend surface ---------------------------------------------------

    def observe(self, component: str, scope: str, kinds: frozenset[str]) -> dict[str, Any]:
        self.validate_scope(scope)
        out: dict[str, Any] = {}
        seen: set[int] = set()
        for kind in sorted(kinds):
            handler = self._by_kind.get(kind)
            if handler is None or id(handler) in seen:
                continue
            seen.add(id(handler))
            out[getattr(handler, "name", type(handler).__name__)] = handler.observe(component, scope)
        return out

    def preflight(self, op: InstallOperation) -> PreflightResult:
        reason = self._refusal(op.kind, op.owner, op.privilege)
        if reason is not None:
            return PreflightResult(False, "refused", reason)
        return self._by_kind[op.kind].preflight(op)

    def _is_none(self, kind: str) -> bool:
        entry = self.op_registry.get(kind)
        return entry is not None and entry.privilege == "none"

    def execute(self, op: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        handler = self._admit(op)
        mutation, evidence = handler.execute(op)
        if self._is_none(op.kind) and mutation.changed:
            raise OperationFailed(f"refused: {op.kind!r} is a read-only (none) kind and reported a change")
        return mutation, evidence

    def verify_operation(self, op: InstallOperation) -> OperationEvidence:
        return self._admit(op).verify_operation(op)

    def rollback_intent(self, op: InstallOperation) -> MutationRecord | None:
        handler = self._admit(op)
        if self._is_none(op.kind):
            return None  # a read-only kind has no write-ahead inverse
        return handler.rollback_intent(op)

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        reason = self._refusal(mutation.kind, mutation.owner, None)
        if reason is not None:
            return PreflightResult(False, "refused", reason)
        if self._is_none(mutation.kind):
            return PreflightResult(True, "not-applied", "read-only (none) kind made no change")
        return self._by_kind[mutation.kind].can_rollback(mutation)

    def rollback_mutation(self, mutation: MutationRecord) -> None:
        reason = self._refusal(mutation.kind, mutation.owner, None)
        if reason is not None:
            raise OperationFailed(f"refused: {reason}")
        if self._is_none(mutation.kind):
            raise OperationFailed(f"refused: {mutation.kind!r} is a read-only (none) kind; there is nothing to roll back")
        self._by_kind[mutation.kind].rollback_mutation(mutation)


class LiveSeatBackend(_SeatBackendCore):
    """The live backend for ONE seat. DARK: refuses to construct while that seat's pin is off."""

    def __init__(
        self,
        persona: str,
        runner: CommandRunner,
        *,
        op_registry: OpRegistry,
        handlers: Sequence[Any] = (),
    ) -> None:
        if not is_live(persona):
            raise LiveActuationDisabled(
                f"live seat actuation is disabled for {persona!r} (T-5940 lands dark; a per-seat reviewed "
                "flip of LIVE_ACTUATION_ENABLED at a supervised first fire, T-5867)"
            )
        identity = resolve_identity(persona)  # pragma: no cover - unreachable while dark
        port = LiveSeatPort(persona, runner, uid=identity.uid, state_root=identity.state_root)  # pragma: no cover
        super().__init__(identity, port, op_registry=op_registry, handlers=handlers)  # pragma: no cover


def live_seat_factory(
    persona: str, runner: CommandRunner, live_handlers: Sequence[Any] = ()
) -> Callable[[OpRegistry, tuple[Any, ...]], LiveSeatBackend]:
    """A ``backend_factory`` for ``Engine.from_checkout``. The discovered (simulated)
    handlers it is handed are discarded; only ``live_handlers`` register."""

    def factory(op_registry: OpRegistry, _discovered_sim_handlers: tuple[Any, ...]) -> LiveSeatBackend:
        return LiveSeatBackend(persona, runner, op_registry=op_registry, handlers=live_handlers)

    return factory
