"""Live-seat handlers for the 37 persona-installer op kinds (T-6167 PR-2, ORG-080 D-I). DARK.

The germination engine becomes the seat installer by WRAPPING the installer's own
``persona_installer.LocalHostBackend`` (the code that today runs under a hand-run
``persona_installer apply``): every verb delegates to it, so the actuation semantics,
the viability and maintenance guards, the tmux and launch-identity handling and the
rollback logic are the installer's, unforked. Nothing here reaches a host by itself:
the handlers register only into ``LiveSeatBackend``, whose constructor refuses while
the seat's ``LIVE_ACTUATION_ENABLED`` pin is False (``ports.py`` is untouched).

Two handler classes, one shared bridge:

* ``ReadOnlyInstallHandler``: the 12 read-only check kinds (privilege ``none``). It
  has NO ``rollback_intent``, answers ``can_rollback`` "not-applied" and refuses
  ``rollback_mutation``; ``execute`` raises if the wrapped backend reports a change
  or a before-state other than ``not-applicable``. The backend core enforces the same
  rules without trusting the handler (``READ_ONLY_NONE_KINDS``).
* ``MutatingInstallHandler``: the 25 seat kinds.

The bridge (``_LocalBackendBridge``) owns ONE ``LocalHostBackend`` per run (the
installer keys cross-op state, ``events``, by installer dotted ids), rebuilds each
engine op into the installer op it was planned from (installer operation id and
privilege come from the op's provenance), pins the allowed target roots from the
trusted manifest (the engine ops do not carry them), and refuses root, another
account, a non-``/`` install root and an unknown installer privilege label. The
installer's ``privileged-helper`` / ``operator-required`` labels describe the split-model
routing of an op; ``LocalHostBackend`` never reads them and runs every op as the seat
uid (the hand-run all-in-one apply does the same), so the label is restored verbatim.
The core's own ``unprivileged`` check on the engine-derived op stays in force. It is
constructed lazily: building handlers does no I/O.

Write-ahead inverse (ORG-080 D-X). ``LocalHostBackend`` has a native pre-execute
rollback intent only for ``timer-enable``. For the file kinds and ``mkdir`` this
module snapshots the pre-image; every other seat kind records an intent-only
marker whose ``can_rollback`` answers "drifted" (operator escalation): rollback is
never guessed.
"""

from __future__ import annotations

import dataclasses
import os
import pwd
from pathlib import Path
from typing import Any, Callable, Mapping

from tools import persona_installer as _pi
from tools.persona_installer import (
    ExecutionContext,
    InstallOperation,
    LocalHostBackend,
    MutationRecord,
    OperationEvidence,
    OperationFailed,
    PreflightResult,
)

from .backend import HANDLER_TIER, READ_ONLY_NONE_KINDS

#: The 25 seat kinds the installer plans (the engine's ``_INSTALL_KIND_MAP`` minus the 12 reads).
SEAT_INSTALL_KINDS: frozenset[str] = frozenset({
    "atomic-write",
    "claude-mcp-server-registration",
    "codex-standing-instructions-install",
    "codex-telegram-mcp-check",
    "codex-telegram-source-check",
    "daemon-reload",
    "direct-fire",
    "mailbox-probe",
    "mailbox-writable-root-check",
    "mkdir",
    "seat-claude-settings-env",
    "seat-claude-workspace-trust",
    "seat-codex-hook-config",
    "seat-periodic-daemon-reload",
    "seat-periodic-timer-enable",
    "seat-periodic-timer-observe",
    "seat-periodic-unit-install",
    "seat-runtime-residue-quarantine",
    "seat-service-decommission",
    "seat-service-enable-observe",
    "stale-unit-failure-reset",
    "timer-enable",
    "timer-observe",
    "unit-fire",
    "user-unit-install",
})

#: Kinds whose pre-image this module snapshots for the write-ahead inverse.
_FILE_KINDS: frozenset[str] = frozenset({
    "atomic-write", "user-unit-install", "seat-periodic-unit-install", "codex-standing-instructions-install",
})
#: Kinds whose inverse LocalHostBackend itself provides before execute.
_NATIVE_INTENT_KINDS: frozenset[str] = frozenset({"timer-enable"})

INTENT_ONLY_STATE = "intent-only"
#: ``after_fingerprint`` of a pre-image intent: equal to no real fingerprint, so
#: ``can_rollback`` answers "not-applied" (still pre-image) or "drifted" (anything else).
INTENT_SENTINEL = "write-ahead-intent"
_NOT_APPLICABLE = {"state": "not-applicable"}
#: The provenance keys ``harness-manager._install_ops`` adds to an installer op (it refuses a clash),
#: dropped on rebuild so the installer backend sees exactly the op it planned.
_ENGINE_ADDED_PROVENANCE = frozenset({
    "installer_operation_id", "installer_privilege", "plan_source_revision",
    "adapter_of", "installer_plan_digest", "installer_operator_actions",
})
_INSTALLER_PRIVILEGE_LABELS = frozenset({"unprivileged", "privileged-helper", "operator-required"})


class _Refused(Exception):
    """An op or mutation the bridge will not hand to the installer backend."""


class _LocalBackendBridge:
    """Owns the one ``LocalHostBackend`` of a run and the engine-op to installer-op mapping."""

    def __init__(
        self,
        account: str,
        manifest: Any,
        *,
        local_factory: Callable[[], Any] = LocalHostBackend,
        root: Path = Path("/"),
        euid: Callable[[], int] = os.geteuid,
        pwnam: Callable[[str], Any] = pwd.getpwnam,
    ) -> None:
        self.account = account
        self._manifest = manifest
        self._local_factory = local_factory
        self._root = Path(root)
        self._euid = euid
        self._pwnam = pwnam
        self._local: Any = None
        self._roots: tuple[str, ...] | None = None

    # --- guards (evaluated on every call; nothing is cached) --------------------

    def _guard(self) -> None:
        uid = self._euid()
        if uid == 0:
            raise _Refused("the live install handler never runs as root")
        try:
            pw = self._pwnam(self.account)
        except KeyError:
            raise _Refused(f"account {self.account!r} does not exist") from None
        if pw.pw_uid != uid:
            raise _Refused(f"seat account {self.account!r} (uid {pw.pw_uid}); this process runs as uid {uid}")

    @property
    def local(self) -> Any:
        if self._local is None:
            self._local = self._local_factory()
        return self._local

    @property
    def allowed_roots(self) -> tuple[str, ...]:
        """Re-derived from the TRUSTED manifest for the bound account; never from op data."""
        if self._roots is None:
            derived = _pi._derive_allowed_roots(self._manifest, self.account, self._root)
            self._roots = tuple(item for item in derived.split(",") if item)
        return self._roots

    @property
    def context(self) -> ExecutionContext:
        return ExecutionContext("all-in-one", self.allowed_roots)

    # --- the mapping -------------------------------------------------------------

    def rebuild(self, op: InstallOperation) -> InstallOperation:
        """The installer op an engine op was planned from, or ``_Refused``."""
        self._guard()
        prov = op.provenance
        installer_id = prov.get("installer_operation_id")
        installer_privilege = prov.get("installer_privilege")
        if not installer_id or not installer_privilege:
            raise _Refused(f"{op.operation_id}: provenance lacks installer_operation_id / installer_privilege")
        if installer_privilege not in _INSTALLER_PRIVILEGE_LABELS:
            raise _Refused(f"{op.operation_id}: unknown installer privilege label {installer_privilege!r}")
        if prov.get("install_root") != "/":
            raise _Refused(f"{op.operation_id}: install_root {prov.get('install_root')!r}; the live backend activates only a root=/ plan")
        if op.owner != self.account:
            raise _Refused(f"{op.operation_id}: owner {op.owner!r} is not the bound seat account {self.account!r}")
        original = {k: v for k, v in prov.items() if k not in _ENGINE_ADDED_PROVENANCE}
        return dataclasses.replace(op, operation_id=installer_id, privilege=installer_privilege, provenance=original)

    def check_mutation(self, mutation: MutationRecord) -> None:
        self._guard()
        if mutation.owner != self.account:
            raise _Refused(f"{mutation.operation_id}: owner {mutation.owner!r} is not the bound seat account {self.account!r}")

    @staticmethod
    def own_mutation(op: InstallOperation, mutation: MutationRecord) -> MutationRecord:
        return dataclasses.replace(mutation, operation_id=op.operation_id)

    @staticmethod
    def own_evidence(op: InstallOperation, evidence: OperationEvidence) -> OperationEvidence:
        return dataclasses.replace(evidence, operation_id=op.operation_id)

    # --- the six verbs -------------------------------------------------------------

    def preflight(self, op: InstallOperation) -> PreflightResult:
        try:
            installer_op = self.rebuild(op)
            context = self.context
        except _Refused as exc:
            return PreflightResult(False, "refused", str(exc))
        return self.local.preflight(installer_op, context)

    def execute(self, op: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        installer_op = self._rebuild_or_fail(op)
        mutation, evidence = self.local.execute(installer_op, self.context)
        return self.own_mutation(op, mutation), self.own_evidence(op, evidence)

    def verify_operation(self, op: InstallOperation) -> OperationEvidence:
        installer_op = self._rebuild_or_fail(op)
        return self.own_evidence(op, self.local.verify_operation(installer_op))

    def native_rollback_intent(self, op: InstallOperation) -> MutationRecord | None:
        installer_op = self._rebuild_or_fail(op)
        intent = self.local.rollback_intent(installer_op)
        return None if intent is None else self.own_mutation(op, intent)

    def pre_image_intent(self, op: InstallOperation) -> MutationRecord:
        """Snapshot the target before execute (file and mkdir kinds), inside the allowed roots."""
        installer_op = self._rebuild_or_fail(op)
        roots = self.allowed_roots
        containment = _pi._realpath_containment_refusal(installer_op, roots) or _pi._desired_path_refusal(
            installer_op, roots
        )
        if containment:
            raise OperationFailed(f"target path check: {containment}")
        path = Path(installer_op.target)
        if _pi._mkdir_owned(installer_op):
            # lstat only, never follow a planted link or read its bytes (the receipt shape of _simple_mutation)
            before = {**_pi._lstat_snapshot(path), "nofollow_root": installer_op.provenance.get("install_root", "/")}
        else:
            before = _pi._snapshot(path)
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, before, INTENT_SENTINEL)

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        try:
            self.check_mutation(mutation)
        except _Refused as exc:
            return PreflightResult(False, "refused", str(exc))
        return self.local.can_rollback(mutation)

    def rollback_mutation(self, mutation: MutationRecord) -> None:
        try:
            self.check_mutation(mutation)
            roots = self.allowed_roots
        except _Refused as exc:
            raise OperationFailed(f"refused: {exc}") from None
        self.local.rollback_mutation(mutation, allowed_roots=roots)

    def _rebuild_or_fail(self, op: InstallOperation) -> InstallOperation:
        try:
            return self.rebuild(op)
        except _Refused as exc:
            raise OperationFailed(f"refused: {exc}") from None


def _is_intent_only(mutation: MutationRecord) -> bool:
    return mutation.before.get("state") == INTENT_ONLY_STATE


class ReadOnlyInstallHandler:
    """The 12 read-only installer checks. Structurally unable to roll back or mutate."""

    name = "install-read-only"
    tier = HANDLER_TIER
    kinds = READ_ONLY_NONE_KINDS

    def __init__(self, bridge: _LocalBackendBridge) -> None:
        self._bridge = bridge

    def observe(self, component: str, scope: str) -> Mapping[str, Any]:
        return {"entries": {}}  # reads happen in preflight / verify; observe actuates and snapshots nothing

    def preflight(self, op: InstallOperation) -> PreflightResult:
        return self._bridge.preflight(op)

    def execute(self, op: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        mutation, evidence = self._bridge.execute(op)
        if mutation.changed or dict(mutation.before) != _NOT_APPLICABLE:
            raise OperationFailed(f"{op.operation_id}: read-only kind {op.kind!r} reported a change; refused")
        return mutation, evidence

    def verify_operation(self, op: InstallOperation) -> OperationEvidence:
        return self._bridge.verify_operation(op)

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        return PreflightResult(True, "not-applied", "read-only kind made no change")

    def rollback_mutation(self, mutation: MutationRecord) -> None:
        raise OperationFailed(f"{mutation.operation_id}: read-only kind {mutation.kind!r} has nothing to roll back")


class MutatingInstallHandler:
    """The 25 seat installer kinds, delegated whole to ``LocalHostBackend``."""

    name = "install-seat"
    tier = HANDLER_TIER
    kinds = SEAT_INSTALL_KINDS

    def __init__(self, bridge: _LocalBackendBridge) -> None:
        self._bridge = bridge

    def observe(self, component: str, scope: str) -> Mapping[str, Any]:
        return {"entries": {}}

    def preflight(self, op: InstallOperation) -> PreflightResult:
        return self._bridge.preflight(op)

    def execute(self, op: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        return self._bridge.execute(op)

    def verify_operation(self, op: InstallOperation) -> OperationEvidence:
        return self._bridge.verify_operation(op)

    def rollback_intent(self, op: InstallOperation) -> MutationRecord | None:
        if op.kind in _NATIVE_INTENT_KINDS:
            return self._bridge.native_rollback_intent(op)
        if op.kind in _FILE_KINDS or op.kind == "mkdir":
            return self._bridge.pre_image_intent(op)
        # No native inverse and no pre-image to take: record the intent so the engine can write ahead,
        # and answer "drifted" at rollback (operator escalation). Never guess an undo.
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, {"state": INTENT_ONLY_STATE}, INTENT_SENTINEL)

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        if _is_intent_only(mutation):
            return PreflightResult(
                False, "drifted", f"{mutation.kind!r} has no automatic inverse; the operator must reconcile this seat"
            )
        return self._bridge.can_rollback(mutation)

    def rollback_mutation(self, mutation: MutationRecord) -> None:
        if _is_intent_only(mutation):
            raise OperationFailed(f"{mutation.operation_id}: intent-only mutation has no automatic inverse; refused")
        self._bridge.rollback_mutation(mutation)


def live_install_handlers(
    manifest: Any,
    persona: str,
    *,
    local_factory: Callable[[], Any] = LocalHostBackend,
    euid: Callable[[], int] = os.geteuid,
    pwnam: Callable[[str], Any] = pwd.getpwnam,
) -> tuple[ReadOnlyInstallHandler, MutatingInstallHandler]:
    """The two install handlers for ``persona``, sharing one bridge (so one ``LocalHostBackend``).

    ``manifest`` is the trusted ``persona_installer.Manifest`` (the allowed roots come from it).
    Pass the result as ``live_seat_factory(persona, runner, live_handlers)``. No I/O here."""

    account = manifest.binding(persona).account
    bridge = _LocalBackendBridge(account, manifest, local_factory=local_factory, euid=euid, pwnam=pwnam)
    return ReadOnlyInstallHandler(bridge), MutatingInstallHandler(bridge)
