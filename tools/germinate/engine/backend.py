"""SimulatedHostBackend: the engine's only host backend (ORG-080 §4 testability tier (a), D-T).

CI-only, zero privilege, nothing live. Why it cannot reach the real host:

1. It is the only backend. Nothing in the engine names another; the root
   backend is a later tranche (T-5084). The real pantheon-config git handler
   (components/pantheon_config_git.py, T-5133) is not registered by any
   component and actuates only inside a ``guard_sim_root``-vetted sandbox.
2. Host state is a JSON document under the simulated-host root. Op targets
   are opaque keys in that document (persona_installer's simulator model) and
   are never opened as paths.
3. Every engine store (run-state, admission claims, WAL, postboot marker, boot
   id) is a fixed child of that one root (``roots`` below); no engine module
   names a production path (``PRODUCTION_PREFIXES`` is the denylist, and the
   AST lint in tests/test_safety_lint.py enforces the rest).
4. ``guard_sim_root`` refuses production prefixes, git checkouts, non-empty
   directories without the sentinel, foreign ownership and a wrong mode.
5. It refuses to run as root.
6. Registry ``contract.module`` paths are contained: absolute paths, ``..``
   and anything resolving outside the checkout refuse (contract.py
   ``contained_module_path``), so a row cannot name arbitrary host code.

What this proof does NOT cover: component code. Planner modules run IN-PROCESS
in this CI tier (``planner.InProcessJsonPlannerRunner``), which is not a
sandbox and not the ORG-080 C5 confinement (the launcher's transient service,
deferred to T-3296 / T-5106). While a planner is imported and runs, a scoped
audit-hook guard refuses write-mode opens, filesystem mutation, process
creation, signals, socket connect/bind/send, thread start and sqlite3
connections, and fails the plan. It does not stop: reads of anything this uid
can read; native code (ctypes, a direct ``_posixsubprocess.fork_exec``)
making syscalls without audit events; ``os.mkfifo``, which emits no audit
event; writes through pre-opened descriptors; CPU and memory abuse; or a
planner switching the guard off from Python through ``PlannerGuard``
internals, since the guard is ordinary state in the same interpreter. A component's ``<id>_handlers.py`` is executor code (it actuates in the
simulation) and runs unguarded, like this module. Both are reviewed repo code.

Per-kind work goes through ``EffectHandler``s. ``InstallerFileHandler``
delegates ``mkdir`` and ``atomic-write`` to persona_installer's own
``SimulatedHostBackend`` (reuse, not a fork); a component registers handlers
for its own kinds through ``components.py`` (the T-5081 seam).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from tools.germinate.op_registry import OpRegistry, load_op_registry
from tools.germinate.planner_sandbox.postboot import marker as _marker
from tools.persona_installer import (
    EXECUTION_CONTEXT_ALL_IN_ONE,
    InstallOperation,
    MutationRecord,
    OperationEvidence,
    OperationFailed,
    PreflightResult,
)
from tools.persona_installer import SimulatedHostBackend as _InstallerSimulator

SENTINEL_NAME = "SIMULATED-HOST"
SENTINEL_BODY = b'{"schema":"pantheon-simulated-host/v0"}\n'
STATE_NAME = "sim-host-state.json"
STATE_SCHEMA = "pantheon-simulated-host-state/v0"
EVIDENCE_SCOPE = "simulated"

#: Resolved roots under any of these are refused outright.
PRODUCTION_PREFIXES: tuple[str, ...] = (
    "/var/lib/pantheon",
    "/run/pantheon",
    "/opt/pantheon",
    "/etc",
    "/home/post-office",
    "/proc",
    "/sys",
)
_STATE_PANTHEON = (".local", "state", "pantheon")
_SEAT_SCOPE_RE = re.compile(r"^seat-[a-z][a-z0-9_-]{0,31}$")

KILL_POINTS = ("after-intent", "after-execute", "after-receipt", "after-manifest", "after-record")


class SimRootRefused(RuntimeError):
    """The simulated-host root could point at something real; refused."""


class SimulatedKill(BaseException):
    """Simulated process death at a kill point. BaseException so no executor
    ``except Exception`` can catch it: on-disk state is left exactly as a real
    kill would leave it (ORG-080 D-X kill-injection)."""


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def _write_sentinel(root: Path) -> None:
    fd = os.open(root / SENTINEL_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(SENTINEL_BODY)


def guard_sim_root(root: Path) -> Path:
    """Resolve and vet a simulated-host root, initializing it if new."""

    if os.geteuid() == 0:
        raise SimRootRefused("the simulated backend never runs as root")
    resolved = Path(os.path.realpath(root))
    text = str(resolved)
    for prefix in PRODUCTION_PREFIXES:
        if _under(text, prefix):
            raise SimRootRefused(f"{text} is under production prefix {prefix}")
    parts = resolved.parts
    for i in range(len(parts) - len(_STATE_PANTHEON) + 1):
        if tuple(parts[i : i + len(_STATE_PANTHEON)]) == _STATE_PANTHEON:
            raise SimRootRefused(f"{text} is inside a seat-scope pantheon state tree")
    for ancestor in (resolved, *resolved.parents):
        if (ancestor / ".git").exists():
            raise SimRootRefused(f"{text} is inside a git checkout ({ancestor})")
    if resolved.exists():
        st = resolved.lstat()
        if not stat.S_ISDIR(st.st_mode):
            raise SimRootRefused(f"{text} is not a directory")
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise SimRootRefused(f"{text} must be a mode-0700 directory owned by this uid")
        sentinel = resolved / SENTINEL_NAME
        if sentinel.exists():
            if sentinel.read_bytes() != SENTINEL_BODY:
                raise SimRootRefused(f"{sentinel} is not a simulated-host sentinel")
        elif any(resolved.iterdir()):
            raise SimRootRefused(f"{text} is non-empty and carries no {SENTINEL_NAME} sentinel")
        else:
            _write_sentinel(resolved)
    else:
        resolved.mkdir(mode=0o700)
        os.chmod(resolved, 0o700)
        _write_sentinel(resolved)
    return resolved


class EffectHandler(Protocol):
    """Per-kind host work: persona_installer's HostBackend method set, plus ``observe``."""

    kinds: frozenset[str]

    def observe(self, component: str, scope: str) -> Mapping[str, Any]: ...

    def preflight(self, op: InstallOperation) -> PreflightResult: ...

    def execute(self, op: InstallOperation) -> tuple[MutationRecord, OperationEvidence]: ...

    def verify_operation(self, op: InstallOperation) -> OperationEvidence: ...

    def rollback_intent(self, op: InstallOperation) -> MutationRecord | None: ...

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult: ...

    def rollback_mutation(self, mutation: MutationRecord) -> None: ...

    def dump_state(self) -> Mapping[str, Any]: ...

    def load_state(self, state: Mapping[str, Any]) -> None: ...


class InstallerFileHandler:
    """``mkdir`` / ``atomic-write`` via persona_installer's simulator (shared entries)."""

    kinds = frozenset({"mkdir", "atomic-write"})
    name = "installer-files"

    def __init__(self) -> None:
        self._sim = _InstallerSimulator(evidence_scope=EVIDENCE_SCOPE)

    def observe(self, component: str, scope: str) -> Mapping[str, Any]:
        return {"entries": {k: dict(v) for k, v in sorted(self._sim.entries.items())}}

    def preflight(self, op: InstallOperation) -> PreflightResult:
        return self._sim.preflight(op)

    def execute(self, op: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        return self._sim.execute(op, EXECUTION_CONTEXT_ALL_IN_ONE)

    def verify_operation(self, op: InstallOperation) -> OperationEvidence:
        return self._sim.verify_operation(op)

    def rollback_intent(self, op: InstallOperation) -> MutationRecord:
        # The installer simulator's own rollback_intent covers timer-enable
        # only; for files the write-ahead inverse is the current entry plus
        # the fingerprint the op will leave, so installer can_rollback can
        # tell not-applied / owned / drifted for an intent with no receipt.
        before = dict(self._sim.entries.get(op.target, {"state": "absent"}))
        if op.kind == "mkdir":
            after = "directory"
        else:
            after = hashlib.sha256(op.desired.get("content", "").encode("utf-8")).hexdigest()
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, before, after)

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        return self._sim.can_rollback(mutation)

    def rollback_mutation(self, mutation: MutationRecord) -> None:
        self._sim.rollback_mutation(mutation)

    def seed(self, target: str, content: str) -> None:
        self._sim.seed_file(target, content)

    def dump_state(self) -> Mapping[str, Any]:
        return {"entries": self._sim.entries, "events": self._sim.events}

    def load_state(self, state: Mapping[str, Any]) -> None:
        self._sim.entries = {k: dict(v) for k, v in state.get("entries", {}).items()}
        self._sim.events = list(state.get("events", []))


@dataclass
class Faults:
    """Simulation-only fault injection. ``fail``: op ids whose execute raises
    OperationFailed; ``rollback_fail``: op ids whose rollback raises;
    ``kill``: (op_id, point) at which the process 'dies'."""

    fail: set[str] = field(default_factory=set)
    rollback_fail: set[str] = field(default_factory=set)
    kill: tuple[str, str] | None = None


class SimulatedHostBackend:
    def __init__(
        self,
        root: Path,
        *,
        op_registry: OpRegistry | None = None,
        handlers: tuple[Any, ...] = (),
    ) -> None:
        self.root = guard_sim_root(root)
        # ORG-082 build step 2: capture the op-registry digest at this load path.
        self.op_registry = op_registry if op_registry is not None else load_op_registry()
        self.loaded_op_registry_digest = self.op_registry.digest
        self.evidence_scope = EVIDENCE_SCOPE
        self.faults = Faults()
        self._handlers: dict[str, Any] = {}
        self._by_kind: dict[str, Any] = {}
        for handler in (InstallerFileHandler(), *handlers):
            self.register_handler(handler)
        self._load()

    # --- roots (every engine store is a fixed child of the one root) -------

    @property
    def gates_root(self) -> Path:
        return self.root / "gates"

    @property
    def postboot_marker_path(self) -> Path:
        return self.root / "run" / "postboot-complete.marker"

    @property
    def boot_id_path(self) -> Path:
        return self.root / "boot-id"

    @staticmethod
    def validate_scope(scope: str) -> str:
        if scope != "system" and not _SEAT_SCOPE_RE.match(scope):
            raise SimRootRefused(f"scope {scope!r} is neither 'system' nor 'seat-<account>'")
        return scope

    def run_state_root(self, scope: str) -> Path:
        """Pure path computation (read-only verbs use it); ``ensure_run_state_root`` creates."""

        if self.validate_scope(scope) == "system":
            return self.root / "run-state"
        return self.root / "seat-state" / scope / "run-state"

    def ensure_run_state_root(self, scope: str) -> Path:
        root = self.run_state_root(scope)
        for path in reversed(root.parents):
            if self.root in path.parents and not path.exists():
                path.mkdir(mode=0o700)
                os.chmod(path, 0o700)
        return root

    # --- handlers ----------------------------------------------------------

    def register_handler(self, handler: Any) -> None:
        name = getattr(handler, "name", type(handler).__name__)
        for kind in handler.kinds:
            if kind in self._by_kind:
                raise ValueError(f"kind {kind!r} already has a handler")
            if self.op_registry.get(kind) is None:
                raise ValueError(f"handler {name!r} serves {kind!r}, which is not an op-registry kind")
            self._by_kind[kind] = handler
        self._handlers[name] = handler

    def handler(self, kind: str) -> Any:
        try:
            return self._by_kind[kind]
        except KeyError:
            raise OperationFailed(f"simulated backend has no handler for kind {kind!r}") from None

    def handles(self, kind: str) -> bool:
        return kind in self._by_kind

    # --- persistence -------------------------------------------------------

    def _state_path(self) -> Path:
        return self.root / STATE_NAME

    def _load(self) -> None:
        path = self._state_path()
        if not path.exists():
            return
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema") != STATE_SCHEMA:
            raise SimRootRefused(f"{path} is not {STATE_SCHEMA}")
        for name, state in doc.get("handlers", {}).items():
            if name in self._handlers:
                self._handlers[name].load_state(state)

    def save(self) -> None:
        doc = {"schema": STATE_SCHEMA, "handlers": {n: h.dump_state() for n, h in sorted(self._handlers.items())}}
        tmp = self.root / f".{STATE_NAME}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._state_path())

    # --- simulated host lifecycle -------------------------------------------

    def simulate_boot(self, boot_id: str = "00000000-0000-4000-8000-000000000001") -> None:
        fd = os.open(self.boot_id_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(boot_id + "\n")
        os.chmod(self.boot_id_path, 0o600)

    def simulate_postboot(self) -> None:
        """What germinate-postboot.service does on a real host: mark this boot complete."""

        if not self.boot_id_path.exists():
            self.simulate_boot()
        run_dir = self.postboot_marker_path.parent
        if not run_dir.exists():
            run_dir.mkdir(mode=0o700)
            os.chmod(run_dir, 0o700)
        boot_id = _marker.current_boot_id(self.boot_id_path)
        _marker.write_marker(boot_id, self.postboot_marker_path)

    def seed_file(self, target: str, content: str) -> None:
        self._handlers[InstallerFileHandler.name].seed(target, content)
        self.save()

    def entry(self, target: str) -> Mapping[str, Any] | None:
        entries = self._handlers[InstallerFileHandler.name].dump_state()["entries"]
        return entries.get(target)

    def checkpoint(self, point: str, op_id: str) -> None:
        if self.faults.kill == (op_id, point):
            raise SimulatedKill(f"simulated kill at {point} of {op_id}")

    # --- HostBackend surface, dispatched by kind ----------------------------

    def observe(self, component: str, scope: str, kinds: frozenset[str]) -> dict[str, Any]:
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
        return self.handler(op.kind).preflight(op)

    def execute(self, op: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        if op.operation_id in self.faults.fail:
            raise OperationFailed(f"injected failure executing {op.operation_id}")
        result = self.handler(op.kind).execute(op)
        self.save()
        return result

    def verify_operation(self, op: InstallOperation) -> OperationEvidence:
        return self.handler(op.kind).verify_operation(op)

    def rollback_intent(self, op: InstallOperation) -> MutationRecord | None:
        return self.handler(op.kind).rollback_intent(op)

    def can_rollback(self, mutation: MutationRecord) -> PreflightResult:
        return self.handler(mutation.kind).can_rollback(mutation)

    def rollback_mutation(self, mutation: MutationRecord) -> None:
        if mutation.operation_id in self.faults.rollback_fail:
            raise OperationFailed(f"injected failure rolling back {mutation.operation_id}")
        self.handler(mutation.kind).rollback_mutation(mutation)
        self.save()
