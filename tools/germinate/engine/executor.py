"""The engine: plan, apply, verify, rollback, status over the simulated backend (ORG-080 §1, §4, §10).

The sole typed executor (D-AC): planners return data; only this module
actuates, through the backend, under the admission claim, with a WAL intent
before every mutation and a receipt only after the postcondition verified.

Readings in force (T-5074, provisional, each isolated here):
* bubble 1 -- ``plan`` is non-admitting and read-only; it refuses while any
  run holds the component's claim. Admission starts at ``apply``.
* bubble 7 -- ``rollback`` applies to non-terminal runs only; SATISFIED and
  ROLLED_BACK stay absorbing.
* bubble 8 -- ``--system X`` plans X only; each dependency needs a SATISFIED
  install run already, or must be in the same ``--all`` plan.
* bubble 9 -- ``verify`` re-checks receipted postconditions (drift) until A-6
  invariant lists exist.
* bubble 10 -- crash recovery is ``reconcile_run(..., executor_dead=True)``,
  library-only: the engine has no liveness primitive to tell a dead executor
  from a running one.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as _dt
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TextIO

from tools.germinate.engine.backend import BackendRefused, HostBackend, SimulatedHostBackend
from tools.germinate.engine.contract import ContractError, LoadedContract, contained_module_path, load_contract
from tools.germinate.engine.dag import MergedPlan, PlanNode, order_components, plan_digest
from tools.germinate.engine.escalation import EscalatingHalt, EscalatingRollbackStop, rollback_command
from tools.germinate.engine.grants import GrantConsumer, GrantRefused, consume as consume_grants, derive_requests, read_grant_file
from tools.germinate.engine.invariant_digest import list_path_for, load_list as load_invariant_list
from tools.germinate.engine.operation import Operation, OperationError, operation_from_json_obj, parse_operation
from tools.germinate.engine import preflight as PF
from tools.germinate.engine.planner import REQUEST_SCHEMA, InProcessJsonPlannerRunner, PlanError, PlannerRunner
from tools.germinate.engine.receipts import build_body, mutation_from_json_obj, mutation_to_json_obj
from tools.germinate.engine.run_state import MAX_PLAN_BYTES, RunHandle, RunStateError, RunStore, RunTuple, encode_plan, new_ulid, now_iso
from tools.germinate.engine import context as ctxmod
from tools.germinate.engine import privilege as privmod
from tools.germinate.engine.context import InvocationContext
from tools.germinate.engine.transcript import Transcript
from tools.germinate.planner_sandbox.admission import coordinator
from tools.germinate.planner_sandbox.admission.exceptions import AdmissionRefused, ClaimHolderMismatch
from tools.germinate.planner_sandbox.admission.exceptions import PrimitiveUnavailable as AdmissionPrimitiveUnavailable
from tools.germinate.planner_sandbox.postboot import PostbootPreconditionUnmet, require_or_refuse
from tools.germinate.planner_sandbox.postboot import PrimitiveUnavailable as PostbootPrimitiveUnavailable
from tools.germinate.planner_sandbox.state_machine.states import State
from tools.germinate.planner_sandbox.wal import (
    BytesPayload,
    CanonicalJsonError,
    ReconcileError,
    StructuralPrefix,
    WalStore,
    WalStoreError,
    canonical_json_bytes,
)
from tools.germinate.registry import schema as RS
from tools.germinate.registry.contract_digest import SENTINEL, ContractDigestError, digest_document
from tools.germinate.registry.load import RegistryLoadError, load_file
from tools.germinate.registry.validate import REGISTRY_REL, errors, validate_document
from tools.persona_installer import OperationFailed

PLAN_NODE_SCHEMA = "pantheon-germinate-plan-node/v0"
LEGS = ("install", "configure", "update", "remove", "repair")
FORWARD_LEGS = frozenset({"install", "configure", "update", "repair"})
INVARIANT_STATUSES = frozenset({"pass", "fail", "vacuous", "alarm"})
ROLLBACK_ADMISSIBLE = frozenset({State.HALTED, State.PARKING_FAILED, State.ROLLBACK_INCOMPLETE})
#: T-5875: the fence a takeover writes into the stuck run's ``rollback_incomplete`` marker BEFORE the claim swap.
SUPERSESSION_FENCE_KEYS = ("superseded_by", "supersession_state", "superseded_at", "uncovered_inverses", "supersession_notify")
#: A claim naming a taking-over run that has no record yet is left alone this long (the run may be admitting);
#: older, it is a crash and reconcile hands the claim back to the stuck run.
#: INVARIANT (T-5875 R2): this is a LIVENESS bound only, never a correctness one. The taker commits the fence
#: only inside the claim flock after re-verifying it still holds the claim (``_commit_supersession_locked``), and
#: reconcile decides everything under that same flock and never touches a ``committed`` fence. So a hand-back can
#: only beat a taker that has not yet committed, and that taker then fails its verify and halts pre-op.
SUPERSESSION_GRACE_S = 30.0


def _supersession_seam(point: str, **context: Any) -> None:
    """Test injection point for the takeover protocol (T-5875): before-swap, after-fence, after-swap,
    after-create, after-commit, rollback-locked. A no-op in production."""


class EngineRefusal(Exception):
    """A typed, fail-closed refusal: what the CLI turns into a non-zero exit."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class RunOutcome:
    component: str
    run_id: str
    state: str


#: T-5868 Phase B (D15 condition 1): the distinct page class when the on_halted hook failed.
HALT_NOT_RECORDED = "halt not recorded on seat state; seat reads as authorised pause"


def run_state_of(simulated_host_root, run_id: str) -> str | None:
    """T-5868 Phase B: the engine state of ``run_id`` in the run stores under a simulated host root
    (system, then each seat-state/<seat>/run-state, the layout ``Engine._stores`` walks), or None when
    no store holds it. Read-only; a malformed run id or record raises RunStateError."""

    root = Path(simulated_host_root)
    uid = os.getuid()
    roots = [root / "run-state", *sorted((root / "seat-state").glob("*/run-state"))]
    for store_root in roots:
        if not store_root.is_dir():
            continue
        store = RunStore(store_root, uid=uid)
        tup = store.find(run_id)
        if tup is not None:
            with store.open(tup) as handle:
                return handle.read_record()["state"]
    return None


def _page_phase(item: Mapping[str, Any]) -> str:
    """Which escalation path a marker or page came from (T-5858 review R2 F2): ``halt`` (an
    EscalatingHalt marker carries the rollback command) or ``rollback`` (an
    EscalatingRollbackStop). Pages dedupe per (run_id, op_id, phase), so one op can page once
    for its forward halt and once more if its undo later stops. A page written before the
    phase field existed derives it the same way."""

    phase = item.get("phase")
    if phase in ("halt", "rollback", "halt-unrecorded"):
        return phase
    return "halt" if "rollback" in item else "rollback"


def _payload(data: bytes) -> BytesPayload:
    return BytesPayload(
        kind="bytes",
        bytes_b64=base64.b64encode(data).decode("ascii"),
        sha256=hashlib.sha256(data).hexdigest(),
        payload=None,
    )


class Engine:
    def __init__(
        self,
        backend: HostBackend,
        *,
        repo_root: Path,
        registry_path: Path | None = None,
        instance: str = "sim",
        runner: PlannerRunner | None = None,
        echo: TextIO | None = None,
        verify_hooks: Mapping[str, Any] | None = None,
        preflight_port: "PF.PreflightPort | None" = None,
        preflight_hooks: Mapping[str, Any] | None = None,
        context: InvocationContext | None = None,
    ) -> None:
        #: T-5243 (S6): who is invoking; default operator = today's behaviour (context.py).
        self.context = context or InvocationContext()
        self.verify_hooks = dict(verify_hooks or {})
        #: T-5921: optional component PREFLIGHT hooks (engine/preflight.py "COMPONENT PREFLIGHT HOOK").
        self.preflight_hooks = dict(preflight_hooks or {})
        #: T-5860: the preflight observation port (operator, sudo listing, checkout); None = unwired.
        #: from_checkout wires the simulated port; no live port exists (deferred to live enablement).
        self.preflight_port = preflight_port
        self.clock: PF.Clock = PF.utcnow
        #: T-5135 S2: the grant source seam (grants.GrantConsumer); None = every independent-agent gate refuses.
        self.grant_consumer: GrantConsumer | None = None
        self.grant_actor = f"uid:{os.getuid()}"
        self.grant_performer = "germinate-engine"
        self.backend = backend
        self.repo_root = Path(repo_root)
        self.registry_path = Path(registry_path) if registry_path else self.repo_root / REGISTRY_REL
        self.instance = instance
        self.runner = runner or InProcessJsonPlannerRunner()
        self.echo = echo
        self.uid = os.getuid()

    @classmethod
    def from_checkout(
        cls,
        sim_root: Path | None,
        *,
        repo_root: Path,
        registry_path: Path | None = None,
        instance: str = "sim",
        runner: PlannerRunner | None = None,
        echo: TextIO | None = None,
        backend_factory: Callable[[Any, tuple[Any, ...]], HostBackend] | None = None,
        context: InvocationContext | None = None,
    ) -> "Engine":
        """Wire every registered component's handlers and op kinds (components.py), then the engine.

        This is the only constructor the CLI uses: a component adopts the
        engine by shipping files, never by editing it.

        T-5940 (S5): ``backend_factory(op_registry, handlers)`` selects the host
        backend. None (the default, and the only path the CLI takes) builds
        ``SimulatedHostBackend(sim_root, ...)`` exactly as before; ``sim_root`` may be
        None only when a factory is given. Only a simulated backend gets the
        simulated preflight port; any other backend gets None (unwired).
        """

        from tools.germinate.engine.components import ComponentLoadError, discover, load_repo_op_registry, merged_kinds
        from tools.germinate.op_registry import OpRegistryError, load_op_registry

        registry_path = Path(registry_path) if registry_path else Path(repo_root) / REGISTRY_REL
        try:
            # T-2918: the SAME loader the broker daemon uses (gate 32a digest parity).
            op_registry = load_repo_op_registry(Path(repo_root), registry_path)
            regs = discover(Path(repo_root), registry_path)
            if load_op_registry(merged_kinds(regs)).digest != op_registry.digest:
                raise OpRegistryError("component OP_KINDS changed between the literal read and the handler import")
        except (ComponentLoadError, OpRegistryError) as exc:
            raise EngineRefusal("component-registration-refused", str(exc)) from None
        handlers = tuple(h for reg in regs for h in reg.handlers)
        try:
            if backend_factory is None:
                if sim_root is None:
                    raise EngineRefusal("backend-refused", "no simulated-host root and no backend factory")
                backend = SimulatedHostBackend(sim_root, op_registry=op_registry, handlers=handlers)
            else:
                backend = backend_factory(op_registry, handlers)
        except ValueError as exc:
            raise EngineRefusal("component-registration-refused", str(exc)) from None
        hooks = {reg.component: reg.verify for reg in regs if reg.verify is not None}
        pf_hooks = {reg.component: reg.preflight for reg in regs if reg.preflight is not None}
        return cls(
            backend,
            repo_root=repo_root,
            registry_path=registry_path,
            instance=instance,
            runner=runner,
            echo=echo,
            verify_hooks=hooks,
            preflight_port=PF.SimulatedPreflightPort(backend.root) if isinstance(backend, SimulatedHostBackend) else None,
            preflight_hooks=pf_hooks,
            context=context,
        )

    # ------------------------------------------------------------------
    # Preconditions and lookups
    # ------------------------------------------------------------------

    def require_postboot(self) -> None:
        """ORG-080 C3: the first act of every verb. Unmet and broken both refuse (C4)."""

        try:
            require_or_refuse(marker_path=self.backend.postboot_marker_path, boot_id_path=self.backend.boot_id_path)
        except PostbootPreconditionUnmet as exc:
            raise EngineRefusal("postboot-unmet", str(exc)) from exc
        except PostbootPrimitiveUnavailable as exc:
            raise EngineRefusal("primitive-unavailable", str(exc)) from exc

    def _validate_scope(self, scope: str) -> None:
        try:
            self.backend.validate_scope(scope)
        except BackendRefused as exc:
            raise EngineRefusal("scope-refused", str(exc)) from None

    def _registry(self) -> tuple[dict[str, Any], str]:
        try:
            raw = self.registry_path.read_bytes()
            doc = load_file(self.registry_path)
        except (OSError, RegistryLoadError) as exc:
            raise EngineRefusal("registry-unreadable", str(exc)) from None
        bad = errors(validate_document(doc))
        if bad:
            raise EngineRefusal("registry-invalid", "; ".join(str(f) for f in bad[:5]))
        return doc, "sha256:" + hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _component_rows(doc: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        return {r["id"]: r for r in doc.get("systems") or [] if r.get("kind") == RS.KIND_COMPONENT}

    def _load_row_contract(self, row: Mapping[str, Any]) -> LoadedContract:
        contract = row["contract"]
        rid = row["id"]
        if contract.get("digest") == RS.DIGEST_UNPINNED:
            raise EngineRefusal("contract-unpinned", f"{rid}: registry digest is unpinned; nothing to plan")
        if contract.get("repo") != RS.INTERNAL_REPO:
            raise EngineRefusal("external-deferred", f"{rid}: external-repo contracts are not resolvable yet (ORG-081 §7)")
        try:
            loaded = load_contract(contained_module_path(self.repo_root, contract["module"]), self.backend.op_registry)
        except ContractError as exc:
            raise EngineRefusal("contract-refused", f"{rid}: {exc}") from None
        if loaded.digest != contract["digest"]:
            raise EngineRefusal("digest-mismatch", f"{rid}: registry pins {contract['digest']}, module is {loaded.digest} (ORG-081 D-9)")
        if loaded.id != rid:
            raise EngineRefusal("contract-refused", f"{rid}: contract module declares id {loaded.id!r}")
        if loaded.steward != row.get("steward"):
            raise EngineRefusal("steward-mismatch", f"{rid}: registry steward {row.get('steward')!r} != contract {loaded.steward!r} (D-AA)")
        return loaded

    def _stores(self) -> list[RunStore]:
        stores = [RunStore(self.backend.run_state_root("system"), uid=self.uid)]
        seat_base = self.backend.root / "seat-state"
        if seat_base.is_dir():
            for path in sorted(seat_base.iterdir()):
                stores.append(RunStore(path / "run-state", uid=self.uid))
        return stores

    def _find_run(self, run_id: str) -> tuple[RunStore, RunTuple]:
        try:
            for store in self._stores():
                tup = store.find(run_id)
                if tup is not None:
                    return store, tup
        except RunStateError as exc:
            raise EngineRefusal("run-state-refused", str(exc)) from None
        raise EngineRefusal("unknown-run", f"no run {run_id} in the run-state store")

    def _runs(self, component: str | None = None) -> list[tuple[RunTuple, dict[str, Any]]]:
        out = []
        for store in self._stores():
            for tup in store.all_tuples():
                if component is not None and tup.component != component:
                    continue
                with store.open(tup) as handle:
                    out.append((tup, handle.read_record()))
        return out

    def _claim_holder(self, component: str) -> dict[str, Any] | None:
        try:
            return coordinator.read_claim(self.backend.gates_root, self.instance, component)
        except AdmissionPrimitiveUnavailable as exc:
            raise EngineRefusal("primitive-unavailable", str(exc)) from exc

    def _satisfied_install(self, component: str, scope: str) -> bool:
        return any(
            t.leg == "install" and t.scope == scope and rec["state"] == State.SATISFIED.value
            for t, rec in self._runs(component)
        )

    @staticmethod
    def _dependency_scope(dep: LoadedContract | None, scope: str, scope_kind: str) -> str | None:
        """T-5135 S3: the scope in which a dependency's install must be SATISFIED.

        The requesting scope when the dependency declares that scope kind (or is
        not a loaded contract: the pre-S3 same-scope rule). A SEAT dependent
        whose dependency declares only ``system`` resolves to ``system`` (a seat
        configure may rest on a root-level install). Nothing else crosses: a
        system dependent never resolves to a seat install (None = refuse).
        """

        if dep is None or scope_kind in dep.scopes:
            return scope
        if scope_kind == "seat" and "system" in dep.scopes:
            return "system"
        return None

    # ------------------------------------------------------------------
    # plan (read-only, non-admitting: bubble 1)
    # ------------------------------------------------------------------

    def plan(
        self,
        leg: str,
        scope: str,
        system: str | None = None,
        *,
        desired: Mapping[str, str] | None = None,
        source_revs: tuple[str, str] | None = None,
    ) -> MergedPlan:
        merged, _contracts = self._plan(
            leg, scope, system, Transcript(new_ulid(), echo=self.echo), desired=desired, source_revs=source_revs
        )
        return merged

    def _select(self, rows: Mapping[str, Mapping[str, Any]], system: str | None, transcript: Transcript) -> list[str]:
        if system is not None:
            if system not in rows:
                raise EngineRefusal("unknown-component", f"{system!r} is not a registry component id")
            return [system]
        chosen = []
        for rid, row in sorted(rows.items()):
            contract = row["contract"]
            if contract.get("digest") == RS.DIGEST_UNPINNED or contract.get("repo") != RS.INTERNAL_REPO:
                transcript.emit("plan.skipped", component=rid, reason="unpinned or external contract")
                continue
            chosen.append(rid)
        if not chosen:
            raise EngineRefusal("nothing-to-plan", "no registry component has a pinned internal contract")
        return chosen

    def _expected_runtime_subset(self, contracts: Iterable[LoadedContract]) -> str:
        """ORG-082 §2a: the union of broker_runtime over EVERY registered component.

        Digested in memory by ``digest_document``, the sole oracle's own core
        (``compute_contract_digest`` is load-then-``digest_document``), so a
        read-only plan writes no scratch file. The ADR's auditor path (write the
        wrapper, run the oracle on it) yields the same digest.
        """

        union = sorted({kind for c in contracts for kind in c.broker_runtime})
        wrapper = {
            "component": {
                "id": "expected-runtime-subset",
                "steward": "broker-daemon",
                "version": 1,
                "contract_digest": SENTINEL,
                "broker_runtime": union,
            }
        }
        try:
            return digest_document(wrapper, source="expected-runtime-subset")
        except ContractDigestError as exc:
            raise EngineRefusal("digest-refused", str(exc)) from None

    def _context_fields(self) -> dict[str, Any]:
        """Transcript fields naming a NON-operator context; empty for the operator (byte-identical transcripts)."""

        if self.context.is_operator:
            return {}
        return {"invocation_context": {"kind": self.context.kind, "account": self.context.account}}

    def _plan(
        self,
        leg: str,
        scope: str,
        system: str | None,
        transcript: Transcript,
        *,
        desired: Mapping[str, str] | None = None,
        source_revs: tuple[str, str] | None = None,
    ) -> tuple[MergedPlan, dict[str, LoadedContract]]:
        if leg not in LEGS:
            raise EngineRefusal("unknown-leg", f"leg {leg!r} not in {LEGS}")
        self.require_postboot()
        self._validate_scope(scope)
        if not self.context.is_operator:
            transcript.emit("context.admitted", **self._context_fields()["invocation_context"])
        if source_revs is not None and leg != "update":
            raise EngineRefusal("inputs-refused", "--from-rev/--to-rev apply to the update leg only")
        if desired and system is None:
            raise EngineRefusal("inputs-refused", "desired-state values are per component: name it with --system")
        scope_kind = "system" if scope == "system" else "seat"
        doc, registry_digest = self._registry()
        rows = self._component_rows(doc)
        selected = self._select(rows, system, transcript)
        registered = {
            rid: self._load_row_contract(row)
            for rid, row in sorted(rows.items())
            if row["contract"].get("digest") != RS.DIGEST_UNPINNED and row["contract"].get("repo") == RS.INTERNAL_REPO
        }
        contracts = {cid: registered[cid] if cid in registered else self._load_row_contract(rows[cid]) for cid in selected}
        takeover_candidates: set[str] = set()
        for cid, contract in contracts.items():
            self._reconcile_supersession(cid)  # T-5875: a crashed takeover is settled before anything reads the claim
            if scope_kind not in contract.scopes:
                raise EngineRefusal("scope-refused", f"{cid} does not declare scope {scope_kind!r}")
            if not contract.declares_leg(leg):
                raise EngineRefusal("leg-not-declared", f"{cid} declares no plan_{leg}")
            for dep in contract.depends_on:
                if dep in contracts:
                    continue
                if dep not in rows:
                    raise EngineRefusal("unknown-dependency", f"{cid} depends on {dep!r}, which is not a registry component")
                if leg in FORWARD_LEGS:
                    dep_scope = self._dependency_scope(registered.get(dep), scope, scope_kind)
                    if dep_scope is None or not self._satisfied_install(dep, dep_scope):
                        where = f"scope {scope}" if dep_scope in (None, scope) else f"scope {dep_scope} (resolved from {scope})"
                        raise EngineRefusal("dependency-not-installed", f"{cid} depends on {dep}, which has no SATISFIED install in {where}")
            holder = self._claim_holder(cid)
            if holder is not None:
                # T-5875 (D-Y): a REMOVE may take over the claim of a ROLLBACK_INCOMPLETE run, and only a gated one
                # (checked once its ops are planned, below). Every other leg and every other holder state refuses.
                if leg == "remove" and self._incomplete_holder_run(holder, cid, scope) is not None:
                    takeover_candidates.add(cid)
                else:
                    raise EngineRefusal(
                        "claim-held", f"{cid} is held by run {holder.get('run_id')} ({holder.get('verb')}); plan refuses while a run holds it"
                    )
        try:
            order = order_components({c: k.depends_on for c, k in contracts.items()}, selected, leg=leg)
        except PlanError as exc:
            raise EngineRefusal("dag-refused", str(exc)) from None
        try:
            resolved_desired = {cid: contracts[cid].resolve_desired(desired or {}) for cid in order}
        except ContractError as exc:
            raise EngineRefusal("inputs-refused", str(exc)) from None
        nodes = []
        for cid in order:
            contract = contracts[cid]
            kinds = frozenset().union(*contract.privilege.values()) if contract.privilege else frozenset()
            request = {
                "schema": REQUEST_SCHEMA,
                "component": cid,
                "leg": leg,
                "scope": scope,
                "desired": resolved_desired[cid],
                "observed": self.backend.observe(cid, scope, kinds),
                "registry_digest": registry_digest,
                "contract_digest": contract.digest,
                "source_rev_from": source_revs[0] if source_revs else None,
                "source_rev_to": source_revs[1] if source_revs else None,
                "verify_result": self._verify_component(cid, scope) if leg == "repair" else None,
            }
            try:
                raw_ops = self.runner.run(contract.planner_path, leg, request)
                ops = tuple(parse_operation(raw, contract=contract, op_registry=self.backend.op_registry) for raw in raw_ops)
            except (PlanError, OperationError) as exc:
                raise EngineRefusal("plan-refused", f"{cid}: {exc}") from None
            ids = [op.operation_id for op in ops]
            if len(set(ids)) != len(ids):
                raise EngineRefusal("plan-refused", f"{cid}: duplicate operation ids")
            for op in ops:
                if not self.backend.handles(op.kind):
                    raise EngineRefusal("plan-refused", f"{cid}: the simulated backend has no handler for {op.kind!r}")
                why = ctxmod.refusal(self.context, op)
                if why is not None:
                    raise EngineRefusal("context-refused", f"{cid}: {why}")
                why = privmod.refusal(self.backend, self.context, scope, op, cid)  # T-5941 (S7)
                if why is not None:
                    raise EngineRefusal("privilege-refused", f"{cid}: {why}")
            if cid in takeover_candidates and not self._has_decommission_gate(contract, ops):
                holder = self._claim_holder(cid) or {}
                raise EngineRefusal(
                    "claim-held",
                    f"{cid} is held by ROLLBACK_INCOMPLETE run {holder.get('run_id')}; only a remove carrying an "
                    "independent-agent gated op (the D-E decommission gate) may take the claim over (D-Y)",
                )
            nodes.append(PlanNode(cid, leg, scope, contract.digest, ops))
        subset = self._expected_runtime_subset(registered.values())
        digest = plan_digest(leg, scope, nodes, self.backend.loaded_op_registry_digest)
        merged = MergedPlan(leg, scope, tuple(nodes), self.backend.loaded_op_registry_digest, subset, digest)
        transcript.emit(
            "plan.computed",
            leg=leg,
            scope=scope,
            plan_digest=digest,
            op_registry_digest=merged.op_registry_digest,
            expected_runtime_subset={"digest": subset, "manifest_revision": None},
            nodes=[{"component": n.component, "operations": [op.operation_id for op in n.operations]} for n in nodes],
        )
        return merged, contracts

    # ------------------------------------------------------------------
    # preflight (read-only GO/NO-GO receipt: T-5860, T-3191 i/ii/viii)
    # ------------------------------------------------------------------

    def preflight(
        self,
        leg: str,
        scope: str,
        system: str | None = None,
        *,
        desired: Mapping[str, str] | None = None,
        source_revs: tuple[str, str] | None = None,
        plan_digest: str | None = None,
        accept_gates: frozenset[str] | None = None,
        grant_file: Path | None = None,
        ttl_seconds: int = PF.DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Run every check and return the sealed receipt. Never mutates, claims, writes
        run state or consumes a grant (see engine/preflight.py). A plan refusal is a
        NO-GO receipt (the later checks ``skip``), not an exception; only a bad TTL
        refuses outright."""

        if not (isinstance(ttl_seconds, int) and not isinstance(ttl_seconds, bool)
                and PF.MIN_TTL_SECONDS <= ttl_seconds <= PF.MAX_TTL_SECONDS):
            raise EngineRefusal(
                "inputs-refused", f"receipt TTL must be {PF.MIN_TTL_SECONDS}..{PF.MAX_TTL_SECONDS} seconds, got {ttl_seconds!r}"
            )
        observed_at = self.clock().replace(microsecond=0)
        transcript = Transcript(new_ulid(), echo=self.echo)
        plan_info: dict[str, Any] = {
            "leg": leg,
            "scope": scope,
            "system": system,
            "instance": self.instance,
            "plan_digest": None,
            "op_registry_digest": None,
            "nodes": [],
        }
        operator = None
        try:
            merged, contracts = self._plan(leg, scope, system, transcript, desired=desired, source_revs=source_revs)
        except EngineRefusal as exc:
            checks = [PF.check("plan", PF.FAIL, exc.code, exc.message)]
            checks += [PF.check(cid, PF.SKIP, None, "not evaluated: the plan was refused") for cid in PF.CHECK_IDS[1:]]
        else:
            plan_info.update(
                plan_digest=merged.digest,
                op_registry_digest=merged.op_registry_digest,
                nodes=[{"component": n.component, "contract_digest": n.contract_digest} for n in merged.nodes],
            )
            capability, operator = self._pf_capability(merged)
            checks = [
                PF.check("plan", PF.PASS, None, f"plan computed ({sum(len(n.operations) for n in merged.nodes)} ops)"),
                self._pf_plan_digest(merged, plan_digest),
                self._pf_encodable(merged),
                self._pf_gates(merged, contracts, accept_gates),
                self._pf_grants(merged, contracts, grant_file),
                capability,
                self._pf_source_hold(merged, contracts, desired),
                self._pf_component(merged, contracts, desired),
            ]
        port = self.preflight_port
        receipt = PF.seal(
            {
                "schema": PF.RECEIPT_SCHEMA,
                "verdict": PF.verdict_of(checks),
                "plan": plan_info,
                "observation_tier": getattr(port, "tier", None),
                "evidence_scope": self.backend.evidence_scope,
                "observed_at": PF.format_time(observed_at),
                "expires_at": PF.format_time(observed_at + _dt.timedelta(seconds=ttl_seconds)),
                "ttl_seconds": ttl_seconds,
                "operator": None if operator is None else {"user": operator.user, "uid": operator.uid},
                "checks": checks,
            }
        )
        transcript.emit(
            "preflight.computed",
            verdict=receipt["verdict"],
            plan_digest=plan_info["plan_digest"],
            receipt_digest=receipt["receipt_digest"],
            failed=[c["id"] for c in checks if c["status"] == PF.FAIL],
        )
        return receipt

    def _pf_plan_digest(self, merged: MergedPlan, approved: str | None) -> dict[str, Any]:
        if approved is None:
            return PF.check(
                "plan-digest", PF.PASS, None, "no approved digest given; the receipt binds the computed one", {"computed": merged.digest}
            )
        if approved != merged.digest:
            return PF.check(
                "plan-digest", PF.FAIL, "plan-digest-mismatch", f"approved {approved}, current plan is {merged.digest}",
                {"approved": approved, "computed": merged.digest},
            )
        return PF.check("plan-digest", PF.PASS, None, "the approved digest is the current plan", {"computed": merged.digest})

    def _pf_encodable(self, merged: MergedPlan) -> dict[str, Any]:
        for node in merged.nodes:
            try:
                self._encode_plan_node(node, merged)  # pure: the bytes apply would admit, nothing written
            except EngineRefusal as exc:
                return PF.check("plan-encodable", PF.FAIL, exc.code, exc.message)
        return PF.check("plan-encodable", PF.PASS, None, "every plan node encodes under MAX_PLAN_BYTES")

    def _pf_gates(self, merged: MergedPlan, contracts: Mapping[str, LoadedContract], accept: frozenset[str] | None) -> dict[str, Any]:
        """The operator-gate half of ``_check_gates`` as findings (that method is unchanged)."""

        failures: list[tuple[str, str]] = []
        operator_gates: set[str] = set()
        for node in merged.nodes:
            contract = contracts[node.component]
            for op in node.operations:
                if op.gate is None:
                    continue
                if op.gate in contract.reserved_authority:
                    failures.append(("reserved-authority", f"{op.operation_id}: {op.gate} is reserved authority; never pre-acceptable (D-Q)"))
                    continue
                gate = contract.gates[op.gate]
                if gate.approval_class == "independent-agent":
                    continue
                operator_gates.add(gate.id)
                if accept is None or (accept and gate.id not in accept):
                    failures.append(("gate-unanswered", f"{op.operation_id}: operator gate {gate.id} needs --accept-gates"))
        evidence = {"operator_gates": sorted(operator_gates), "accepted": PF.accepted_gates_token(accept)}
        if failures:
            reason = "reserved-authority" if any(code == "reserved-authority" for code, _ in failures) else "gate-unanswered"
            return PF.check("gates", PF.FAIL, reason, "; ".join(msg for _, msg in failures[:5]), evidence)
        return PF.check("gates", PF.PASS, None, "every operator gate is answered", evidence)

    def _pf_grants(self, merged: MergedPlan, contracts: Mapping[str, LoadedContract], grant_file: Path | None) -> dict[str, Any]:
        """Presence and shape only: a grant is single-use, so binding it here would consume it."""

        requests = derive_requests(merged, contracts, actor=self.grant_actor, performer=self.grant_performer)
        evidence: dict[str, Any] = {"requests": [r.op_id for r in requests], "present": [], "binding": "not-checked (single-use)"}
        if not requests:
            return PF.check("grants", PF.PASS, None, "no independent-agent gate in the plan", evidence)
        handles: Mapping[str, str] = {}
        if grant_file is not None:
            try:
                handles = read_grant_file(Path(grant_file), os.getuid())
            except GrantRefused as exc:
                return PF.check("grants", PF.FAIL, exc.code, exc.message, evidence)
        evidence["present"] = [r.op_id for r in requests if r.op_id in handles]  # op ids only, never handles
        if self.grant_consumer is None:
            first = requests[0]
            return PF.check(
                "grants", PF.FAIL, "grant-required",
                f"{first.op_id}: gate {first.gate_id} needs a pre-issued independent-agent grant and no grant consumer is wired",
                evidence,
            )
        missing = [r for r in requests if r.op_id not in handles]
        if missing:
            return PF.check(
                "grants", PF.FAIL, "grant-required",
                "no grant supplied for " + ", ".join(f"{r.op_id} (gate {r.gate_id})" for r in missing[:5]), evidence,
            )
        return PF.check("grants", PF.PASS, None, f"a grant handle is present for each of {len(requests)} gated ops", evidence)

    def _pf_capability(self, merged: MergedPlan) -> tuple[dict[str, Any], "PF.OperatorIdentity | None"]:
        try:
            required = PF.required_capabilities(
                (f"{n.component}/{op.operation_id}", op.privilege, op.owner) for n in merged.nodes for op in n.operations
            )
        except PF.PreflightError as exc:  # T-5860 r2 nit 1: a typed NO-GO, never a traceback
            return PF.check("capability", PF.FAIL, exc.code, exc.message), None
        port = self.preflight_port
        if port is None:
            if not required:
                return PF.evaluate_capabilities(required, None, None), None
            return PF.check(
                "capability", PF.FAIL, "observation-port-unwired", "no preflight observation port is wired", {"required": required}
            ), None
        operator = listing = None
        operator_error = listing_error = None
        try:
            operator = port.operator()
        except Exception as exc:  # noqa: BLE001 -- any port failure is unobservable, never a pass
            operator_error = f"{type(exc).__name__}: {exc}"
        try:
            listing = port.sudo_listing()
        except Exception as exc:  # noqa: BLE001 -- as above
            listing_error = f"{type(exc).__name__}: {exc}"
        result = PF.evaluate_capabilities(
            required, operator, listing, operator_error=operator_error, listing_error=listing_error,
            roles=PF.role_accounts(self.repo_root),
        )
        return result, operator

    def _pf_source_hold(
        self, merged: MergedPlan, contracts: Mapping[str, LoadedContract], desired: Mapping[str, str] | None
    ) -> dict[str, Any]:
        """T-3191 (ii), detect-only: no hold mechanism exists, so the receipt pins the
        checkout head and ``apply --preflight-receipt`` refuses on drift."""

        evidence: dict[str, Any] = {
            "head": None, "clean": None, "hold_mechanism": "none", "mode": "detect-at-apply", "plan_revisions": {},
        }
        for node in merged.nodes:
            revision = contracts[node.component].resolve_desired(desired or {}).get("source_revision")
            if revision:
                evidence["plan_revisions"][node.component] = revision
        port = self.preflight_port
        if port is None:
            return PF.check("source-hold", PF.FAIL, "observation-port-unwired", "no preflight observation port is wired", evidence)
        try:
            seen = port.checkout(self.repo_root)
        except Exception as exc:  # noqa: BLE001 -- unobservable is a NO-GO, never a pass
            return PF.check("source-hold", PF.FAIL, "checkout-unobservable", f"{type(exc).__name__}: {exc}", evidence)
        evidence.update(head=seen.head, clean=seen.clean)
        if not seen.clean:
            return PF.check("source-hold", PF.FAIL, "checkout-dirty", "the checkout has uncommitted changes; it cannot be pinned", evidence)
        off = {cid: rev for cid, rev in evidence["plan_revisions"].items() if rev != seen.head}
        if off:
            detail = "; ".join(f"{cid} plans source_revision {rev}, checkout is at {seen.head}" for cid, rev in sorted(off.items()))
            return PF.check("source-hold", PF.FAIL, "checkout-not-at-plan-revision", detail, evidence)
        return PF.check("source-hold", PF.PASS, None, f"checkout pinned at {seen.head}; apply re-checks it", evidence)

    def _pf_component(
        self, merged: MergedPlan, contracts: Mapping[str, LoadedContract], desired: Mapping[str, str] | None
    ) -> dict[str, Any]:
        """T-5921: each node's optional component PREFLIGHT hook, folded into one check.
        Read-only: the hook sees plan data and ``PF.Probe`` over the port, nothing else."""

        port = self.preflight_port
        per: dict[str, dict[str, Any]] = {}
        for node in merged.nodes:
            hook = self.preflight_hooks.get(node.component)
            if hook is None:
                per[node.component] = {"hook": "none", "checks": []}
                continue
            entry: dict[str, Any] = {"hook": "registered", "checks": []}
            per[node.component] = entry
            if port is None:
                entry["error"] = "observation-port-unwired: no preflight observation port is wired"
                continue
            try:
                ctx = PF.PreflightContext(
                    component=node.component,
                    leg=merged.leg,
                    scope=merged.scope,
                    ops=tuple(op.to_json_obj() for op in node.operations),
                    desired=dict(contracts[node.component].resolve_desired(desired or {})),
                    repo_root=str(self.repo_root),
                    tier=str(getattr(port, "tier", "")),
                    probe=PF.Probe(port),
                )
                entry["checks"] = PF.component_checks(hook(ctx))
            except PF.PreflightError as exc:
                entry["error"] = f"{exc.code}: {exc.message}"
            except Exception as exc:  # noqa: BLE001 -- a broken hook is a NO-GO, never a pass
                entry["error"] = f"component-hook-raised: {type(exc).__name__}: {exc}"
        return PF.fold_component_checks(per)

    def _check_preflight_receipt(
        self, receipt: Mapping[str, Any], merged: MergedPlan, system: str | None, accept_gates: frozenset[str] | None
    ) -> None:
        """``apply --preflight-receipt``: bind the receipt to THIS plan, then re-observe source-hold."""

        try:
            PF.validate_receipt(receipt)
        except PF.PreflightError as exc:
            raise EngineRefusal(exc.code, exc.message) from None
        port = self.preflight_port
        if port is None:
            raise EngineRefusal("preflight-port-unwired", "a preflight receipt needs the observation port to re-check source-hold")
        if receipt["observation_tier"] != port.tier:
            raise EngineRefusal(
                "preflight-tier-mismatch", f"receipt observed on tier {receipt['observation_tier']!r}, this engine is {port.tier!r}"
            )
        if receipt["verdict"] != "GO":
            failed = [f"{c['id']} ({c['reason']})" for c in receipt["checks"] if c["status"] == PF.FAIL]
            raise EngineRefusal("preflight-no-go", "the receipt is NO-GO: " + ", ".join(failed))
        plan = receipt["plan"]
        expected = {
            "leg": merged.leg,
            "scope": merged.scope,
            "system": system,
            "instance": self.instance,
            "plan_digest": merged.digest,
            "op_registry_digest": merged.op_registry_digest,
        }
        diffs = [k for k, v in expected.items() if plan.get(k) != v]
        if PF.receipt_accepted_gates(receipt) != PF.accepted_gates_token(accept_gates):
            diffs.append("accept_gates")  # the gates check proved THAT answer set, not this one
        if receipt["evidence_scope"] != self.backend.evidence_scope:
            diffs.append("evidence_scope")
        if diffs:
            raise EngineRefusal(
                "preflight-receipt-mismatch", f"the receipt is for another plan (differs in {', '.join(diffs)}); re-run the preflight"
            )
        now = self.clock()
        observed, expires = PF.parse_time(receipt["observed_at"]), PF.parse_time(receipt["expires_at"])
        if observed > now + _dt.timedelta(seconds=PF.FUTURE_SKEW_SECONDS):
            raise EngineRefusal("preflight-receipt-future", f"receipt observed_at {receipt['observed_at']} is in the future")
        if now > expires:
            raise EngineRefusal("preflight-receipt-expired", f"receipt expired at {receipt['expires_at']}; re-run the preflight")
        head = PF.receipt_head(receipt)
        try:
            seen = port.checkout(self.repo_root)
        except Exception as exc:  # noqa: BLE001 -- unobservable at apply refuses, never passes
            raise EngineRefusal("checkout-unobservable", f"source-hold re-check: {type(exc).__name__}: {exc}") from None
        if seen.head != head or not seen.clean:
            state = "dirty" if seen.head == head else f"at {seen.head}"
            raise EngineRefusal(
                "source-drift", f"the checkout moved since the preflight (pinned {head}, now {state}); re-plan and re-run the preflight"
            )

    # ------------------------------------------------------------------
    # apply
    # ------------------------------------------------------------------

    def _check_gates(
        self,
        merged: MergedPlan,
        contracts: Mapping[str, LoadedContract],
        accept: frozenset[str] | None,
        grants: Mapping[str, str] | None = None,
        transcript: Transcript | None = None,
    ) -> dict[tuple[str, str], dict[str, str]]:
        """D-F / D-Q: every gate is answered before any mutation, or the apply refuses.

        ``accept`` None = no --accept-gates; the empty set = accept every
        operator-class gate; otherwise the named gate ids. An independent-agent
        gate is never answered by ``accept``: it consumes a pre-issued grant
        (grants.py, T-5135 S2), and that consumption runs LAST, so a refusal on
        any other gate never burns a grant.

        Returns the bound grants keyed ``(component, op_id)`` (op ids may repeat
        across components), so each op's receipt can carry its authority (T-5233).
        """

        for node in merged.nodes:
            contract = contracts[node.component]
            for op in node.operations:
                if op.gate is None:
                    continue
                if op.gate in contract.reserved_authority:
                    raise EngineRefusal("reserved-authority", f"{op.operation_id}: {op.gate} is reserved authority; never pre-acceptable (D-Q)")
                gate = contract.gates[op.gate]
                if gate.approval_class == "independent-agent":
                    continue
                if accept is None or (accept and gate.id not in accept):
                    raise EngineRefusal("gate-unanswered", f"{op.operation_id}: operator gate {gate.id} needs --accept-gates")
        requests = derive_requests(merged, contracts, actor=self.grant_actor, performer=self.grant_performer)
        try:
            bound = consume_grants(requests, grants, self.grant_consumer)
        except GrantRefused as exc:
            raise EngineRefusal(exc.code, exc.message) from None
        if transcript is not None:
            for entry in bound:
                transcript.emit("grant.bound", **entry)
        return {(entry["component"], entry["op_id"]): entry for entry in bound}

    def grant_requests(
        self,
        leg: str,
        scope: str,
        system: str | None = None,
        *,
        desired: Mapping[str, str] | None = None,
        source_revs: tuple[str, str] | None = None,
    ) -> list:
        """The grants the CURRENT plan would consume (read-only; what an issuer reviews)."""

        merged, contracts = self._plan(
            leg, scope, system, Transcript(new_ulid(), echo=self.echo), desired=desired, source_revs=source_revs
        )
        return derive_requests(merged, contracts, actor=self.grant_actor, performer=self.grant_performer)

    def apply(
        self,
        leg: str,
        scope: str,
        system: str | None,
        *,
        plan_digest: str,
        accept_gates: frozenset[str] | None = None,
        grants: Mapping[str, str] | None = None,
        desired: Mapping[str, str] | None = None,
        source_revs: tuple[str, str] | None = None,
        preflight_receipt: Mapping[str, Any] | None = None,
    ) -> list[RunOutcome]:
        transcript = Transcript(new_ulid(), echo=self.echo)
        merged, contracts = self._plan(leg, scope, system, transcript, desired=desired, source_revs=source_revs)
        if merged.digest != plan_digest:
            raise EngineRefusal("plan-digest-mismatch", f"approved {plan_digest}, current plan is {merged.digest}; re-plan")
        if preflight_receipt is not None:
            # T-5860, opt-in only: before any gate, grant, claim or run-state write
            self._check_preflight_receipt(preflight_receipt, merged, system, accept_gates)
            transcript.emit("preflight.receipt-accepted", receipt_digest=preflight_receipt["receipt_digest"])
        authority = self._check_gates(merged, contracts, accept_gates, grants, transcript)
        # Every node's plan.json is encoded under MAX_PLAN_BYTES BEFORE any claim or
        # run admission (T-5809): an oversized node refuses with nothing claimed or
        # admitted, for it or for any earlier node of the same plan.
        plan_bytes = [self._encode_plan_node(node, merged) for node in merged.nodes]
        outcomes = []
        for node, data in zip(merged.nodes, plan_bytes):
            supersede = self._supersession_for(node, contracts[node.component])
            outcomes.append(self._apply_node(node, merged, transcript, data, authority, supersede=supersede))
        return outcomes

    def _encode_plan_node(self, node: PlanNode, merged: MergedPlan) -> bytes:
        try:
            return encode_plan(
                {
                    "schema": PLAN_NODE_SCHEMA,
                    "plan_digest": merged.digest,
                    "op_registry_digest": merged.op_registry_digest,
                    "node": node.to_json_obj(),
                }
            )
        except CanonicalJsonError as exc:
            if str(exc) == "json-too-large":
                raise EngineRefusal(
                    "plan-too-large", f"{node.component}: plan node does not encode under MAX_PLAN_BYTES ({MAX_PLAN_BYTES})"
                ) from exc
            # e.g. float-not-allowed: plan_digest's plain json.dumps accepted it, canonical JSON does not
            raise EngineRefusal("plan-unencodable", f"{node.component}: plan node is not canonical JSON: {exc}") from exc

    def _claim(self, component: str, run_id: str, scope: str, verb: str) -> None:
        try:
            coordinator.bootstrap_component(self.backend.gates_root, self.instance, component)
            coordinator.claim(
                self.backend.gates_root, self.instance, component, run_id=run_id, scope=scope, uid=self.uid, verb=verb
            )
        except AdmissionRefused as exc:
            raise EngineRefusal("admission-refused", str(exc)) from exc
        except AdmissionPrimitiveUnavailable as exc:
            raise EngineRefusal("primitive-unavailable", str(exc)) from exc

    def _release(self, component: str, run_id: str) -> None:
        """Release ``run_id``'s claim; never another run's (T-5819 compare-and-delete)."""

        try:
            coordinator.release(self.backend.gates_root, self.instance, component, expected_run_id=run_id)
        except ClaimHolderMismatch as exc:
            raise EngineRefusal("claim-holder-mismatch", str(exc)) from exc
        except AdmissionPrimitiveUnavailable as exc:
            raise EngineRefusal("primitive-unavailable", str(exc)) from exc

    # ------------------------------------------------------------------
    # takeover of a ROLLBACK_INCOMPLETE claim by a gated remove (T-5875, ORG-080 D-Y)
    # ------------------------------------------------------------------

    def _incomplete_holder_run(self, holder: Mapping[str, Any], component: str, scope: str):
        """``(store, tup)`` of the run named by ``holder`` when it is a live, not-yet-superseded
        ROLLBACK_INCOMPLETE run of this component and scope; else None (every other state refuses)."""

        run_id = holder.get("run_id")
        if not isinstance(run_id, str):
            return None
        try:
            store, tup = self._find_run(run_id)
            with store.open(tup) as handle:
                record = handle.read_record()
        except (EngineRefusal, RunStateError):
            return None
        if tup.component != component or tup.scope != scope or record["state"] != State.ROLLBACK_INCOMPLETE.value:
            return None
        if "superseded_by" in record["rollback_incomplete"]:
            return None
        return store, tup

    @staticmethod
    def _has_decommission_gate(contract: LoadedContract, ops: Sequence[Operation]) -> bool:
        """The D-E decommission gate: an op bound to an independent-agent-class gate (answered by a grant)."""

        return any(
            op.gate is not None and op.gate not in contract.reserved_authority
            and contract.gates[op.gate].approval_class == "independent-agent"
            for op in ops
        )

    def _supersession_for(self, node: PlanNode, contract: LoadedContract) -> dict[str, Any] | None:
        """The takeover descriptor for ``node``, or None when its claim is a plain claim.

        Only a gated remove over a ROLLBACK_INCOMPLETE holder qualifies. Unconditional ownership of the
        outstanding inverses (operator ruling, T-5875): the takeover is never refused for an inverse the remove does
        not clean, but each such ``uncovered`` inverse (its target is no target of the remove's ops) is
        recorded in the audit and escalated, never silent.
        """

        if node.leg != "remove":
            return None
        holder = self._claim_holder(node.component)
        if holder is None or not self._has_decommission_gate(contract, node.operations):
            return None
        found = self._incomplete_holder_run(holder, node.component, node.scope)
        if found is None:
            return None  # _claim then refuses admission, exactly as before
        store, tup = found
        try:
            with store.open(tup) as handle:
                incomplete = handle.read_record()["rollback_incomplete"]
                targets = {o["operation_id"]: o["target"] for o in handle.read_plan()["node"]["operations"]}
        except Exception as exc:  # noqa: BLE001 -- an unreadable stuck run is never taken over blind
            raise EngineRefusal("run-state-refused", f"run {tup.run_id}: record/plan unreadable: {type(exc).__name__}: {exc}") from None
        outstanding = list(incomplete.get("outstanding_inverses") or ())
        covered = {op.target for op in node.operations}
        uncovered = [{"op_id": i, "target": targets.get(i)} for i in outstanding if targets.get(i) not in covered]
        return {
            "holder": dict(holder), "store": store, "tup": tup, "outstanding": outstanding,
            "uncovered": uncovered, "escalations": list(incomplete.get("escalations") or ()), "steward": contract.steward,
        }

    @contextlib.contextmanager
    def _component_lock(self, component: str):
        """The component's claim flock (coordinator.claim_lock), fail closed (T-5875)."""

        try:
            coordinator.bootstrap_component(self.backend.gates_root, self.instance, component)
            with coordinator.claim_lock(self.backend.gates_root, self.instance, component):
                yield
        except AdmissionPrimitiveUnavailable as exc:
            raise EngineRefusal("primitive-unavailable", str(exc)) from exc

    def _takeover_claim(self, node: PlanNode, sup: Mapping[str, Any], run_id: str) -> None:
        """Steps (a) and (b): fence the stuck run, then swap the claim, both under the claim flock.

        The precondition (still ROLLBACK_INCOMPLETE, not fenced) is re-read INSIDE the lock, where
        ``rollback`` makes its own check-and-transition, so the two are mutually exclusive.
        """

        holder, old = sup["holder"], sup["tup"].run_id
        _supersession_seam("before-swap", sup=sup, run_id=run_id)
        fenced: list[bool] = []

        def fence() -> None:
            with sup["store"].open(sup["tup"]) as old_handle:
                record = old_handle.read_record()
                marker = record["rollback_incomplete"]
                if record["state"] != State.ROLLBACK_INCOMPLETE.value or marker is None or "superseded_by" in marker:
                    raise EngineRefusal(
                        "claim-held",
                        f"{node.component}: run {old} is no longer an unsuperseded ROLLBACK_INCOMPLETE run "
                        f"({record['state']}); takeover refused, nothing swapped (D-Y)",
                    )
                marker = dict(marker)
                marker.update(
                    superseded_by=run_id, supersession_state="pending", superseded_at=now_iso(),
                    uncovered_inverses=sup["uncovered"], supersession_notify=sup["steward"],
                )
                old_handle.update(rollback_incomplete=marker)
            fenced.append(True)
            _supersession_seam("after-fence", sup=sup, run_id=run_id)

        try:
            coordinator.takeover(
                self.backend.gates_root, self.instance, node.component,
                expected_run_id=holder["run_id"], run_id=run_id, scope=node.scope, uid=self.uid, verb="apply",
                before_swap=fence,
            )
        except ClaimHolderMismatch as exc:
            self._clear_fence(sup, run_id) if fenced else None
            raise EngineRefusal("claim-held", f"{node.component}: the stuck run's claim moved before the takeover: {exc}") from exc
        except AdmissionPrimitiveUnavailable as exc:
            self._clear_fence(sup, run_id) if fenced else None
            raise EngineRefusal("primitive-unavailable", str(exc)) from exc
        except Exception:
            self._clear_fence(sup, run_id) if fenced else None
            raise
        _supersession_seam("after-swap", sup=sup, run_id=run_id)

    @staticmethod
    def _clear_fence(sup: Mapping[str, Any], run_id: str) -> None:
        """Drop the PENDING fence ``run_id`` wrote on the stuck run. Best effort: reconcile finishes a miss."""

        try:
            with sup["store"].open(sup["tup"]) as old_handle:
                marker = dict(old_handle.read_record()["rollback_incomplete"] or {})
                if marker.get("superseded_by") == run_id and marker.get("supersession_state") == "pending":
                    for key in SUPERSESSION_FENCE_KEYS:
                        marker.pop(key, None)
                    old_handle.update(rollback_incomplete=marker)
        except Exception:  # noqa: BLE001 -- the original failure is what the caller must see
            pass

    def _restore_claim(self, node: PlanNode, sup: Mapping[str, Any], run_id: str) -> None:
        """No record was admitted for the taking-over run: hand the claim back and clear the fence (best effort)."""

        holder = sup["holder"]
        try:
            coordinator.takeover(
                self.backend.gates_root, self.instance, node.component,
                expected_run_id=run_id, run_id=holder["run_id"], scope=holder.get("scope", node.scope),
                uid=holder.get("uid", self.uid), verb=holder.get("verb", "apply"),
            )
        except Exception:  # noqa: BLE001 -- reconcile hands it back on the next pass
            pass
        self._clear_fence(sup, run_id)

    @staticmethod
    def _has_event(handle: RunHandle, name: str) -> bool:
        for line in handle.read_transcript():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == name:
                return True
        return False

    def _commit_supersession(self, handle: RunHandle, run_t: Transcript, run_id: str, old_store: RunStore, old_tup: RunTuple) -> None:
        """Step (d): flip the fence to ``committed`` and write the audit events (P4). Idempotent: reconcile
        rolls a crashed takeover forward through here. Neither run is deleted or re-stated."""

        with old_store.open(old_tup) as old_handle:
            marker = dict(old_handle.read_record()["rollback_incomplete"] or {})
            if marker.get("superseded_by") != run_id:
                raise EngineRefusal("run-state-refused", f"run {old_tup.run_id} is not fenced for {run_id}; supersession not committed")
            if not self._has_event(handle, "run.supersedes"):
                run_t.emit(
                    "run.supersedes", superseded_run_id=old_tup.run_id, outstanding_inverses=list(marker.get("outstanding_inverses") or ()),
                    uncovered_inverses=list(marker.get("uncovered_inverses") or ()), escalations=list(marker.get("escalations") or ()),
                )
            if marker.get("supersession_state") != "committed":
                marker["supersession_state"] = "committed"
                old_handle.update(rollback_incomplete=marker)  # the state stays ROLLBACK_INCOMPLETE; only the marker grows
            if not self._has_event(old_handle, "rollback.superseded"):
                Transcript(new_ulid(), echo=self.echo).bind(old_handle).emit(
                    "rollback.superseded", superseded_by=run_id, outstanding_inverses=list(marker.get("outstanding_inverses") or ())
                )
        self._page_escalations_once(handle, run_t, run_id, [
            {
                "op_id": u["op_id"], "subject": u["target"] or u["op_id"], "recipient": marker["supersession_notify"],
                "reason": f"outstanding inverse of superseded run {old_tup.run_id} is not covered by this decommission",
            }
            for u in marker.get("uncovered_inverses") or ()
        ])

    def _commit_supersession_locked(self, handle: RunHandle, run_t: Transcript, node: PlanNode, run_id: str, sup: Mapping[str, Any]) -> None:
        """Step (d) as the taker runs it: ONE critical section under the claim flock that re-verifies the claim still
        names ``run_id`` and the old run's fence still names it (pending, or committed by a reconcile roll-forward),
        then commits. A lost race (reconcile handed the claim back) raises ``supersession-lost``."""

        with self._component_lock(node.component):
            holder = self._claim_holder(node.component)
            with sup["store"].open(sup["tup"]) as old_handle:
                marker = (old_handle.read_record()["rollback_incomplete"] or {})
            if (
                holder is None or holder.get("run_id") != run_id
                or marker.get("superseded_by") != run_id or marker.get("supersession_state") not in ("pending", "committed")
            ):
                raise EngineRefusal(
                    "supersession-lost",
                    f"run {run_id}: the claim on {node.component} no longer names it or the fence on {sup['tup'].run_id} "
                    "was cleared (reconcile handed the claim back); halting before any actuation",
                )
            self._commit_supersession(handle, run_t, run_id, sup["store"], sup["tup"])

    def _halt_supersession_lost(self, handle: RunHandle, node: PlanNode, run_t: Transcript | None, exc: EngineRefusal) -> None:
        """The taker lost its claim before actuating: PENDING/ACTIVE -> HALTED (no op ran), audit event, claim untouched
        (it is not ours). Always raises ``exc``."""

        run_id = handle.tuple.run_id
        try:
            if State(handle.read_record()["state"]) is State.PENDING:
                handle.update(state=State.ACTIVE)
            first = node.operations[0].operation_id if node.operations else "supersession"
            handle.update(state=State.HALTED, halted={"op_id": first, "reason": f"supersession-lost: {exc.message}", "at": now_iso()})
            if run_t is not None:
                run_t.emit("run.supersession-lost", reason=exc.message)
        except Exception:  # noqa: BLE001 -- the refusal below is what the caller must see; reconcile_run recovers the record
            pass
        raise exc

    def _pending_fences(self, component: str) -> list[RunTuple]:
        try:
            return [
                tup for tup, record in self._runs(component)
                if record["state"] == State.ROLLBACK_INCOMPLETE.value
                and (record["rollback_incomplete"] or {}).get("supersession_state") == "pending"
            ]
        except (RunStateError, OSError):
            return []  # unreadable records are not this pass's business; a fence still refuses rollback

    def _reconcile_supersession(self, component: str) -> None:
        """Settle a takeover that crashed between its fence and its commit (T-5875).

        A stuck run carrying a PENDING fence: if the taking-over run has a record, roll the commit forward;
        if it has none, hand the claim back to the stuck run (when it names the taker, past the grace, or is
        absent) and clear the fence. Runs at the start of plan, apply (via plan), rollback and status. Under
        the claim flock, so it never interleaves with a live takeover; a fence seen under the lock whose
        claim still names the stuck run is a crash between the fence and the swap (the swap follows the
        fence inside one locked section).
        """

        if not self._pending_fences(component):
            return  # the common case: no lock, no bootstrap, nothing written
        with self._component_lock(component):
            for tup in self._pending_fences(component):
                self._reconcile_one(component, tup)

    def _reconcile_one(self, component: str, tup: RunTuple) -> None:
        store, tup = self._find_run(tup.run_id)
        with store.open(tup) as old_handle:
            marker = dict(old_handle.read_record()["rollback_incomplete"] or {})
        if marker.get("supersession_state") != "pending":
            return  # a committed fence is final: the taker owns the component; never hand back or clear it
        taker = marker["superseded_by"]
        try:
            new_store, new_tup = self._find_run(taker)
            has_record = new_store.has_record(new_tup)
        except EngineRefusal:
            has_record = False
        if has_record:
            with new_store.open(new_tup) as new_handle:
                self._commit_supersession(new_handle, Transcript(new_ulid(), echo=self.echo).bind(new_handle), taker, store, tup)
            return
        holder = self._claim_holder(component)
        action = "fence-cleared"
        if holder is not None and holder.get("run_id") == taker:
            if time.time() - float(holder.get("claimed_at") or 0) < SUPERSESSION_GRACE_S:
                return  # the taker may still be admitting its run: not yet a crash
            coordinator.takeover(
                self.backend.gates_root, self.instance, component, expected_run_id=taker, run_id=tup.run_id,
                scope=tup.scope, uid=self.uid, verb="rollback", already_locked=True,  # reconcile holds the lock
            )
            action = "claim-restored"
        elif holder is None:
            coordinator.claim(self.backend.gates_root, self.instance, component, run_id=tup.run_id, scope=tup.scope, uid=self.uid, verb="rollback")
            action = "claim-restored"
        with store.open(tup) as old_handle:
            marker = dict(old_handle.read_record()["rollback_incomplete"] or {})
            for key in SUPERSESSION_FENCE_KEYS:
                marker.pop(key, None)
            old_handle.update(rollback_incomplete=marker)
            Transcript(new_ulid(), echo=self.echo).bind(old_handle).emit("supersession.reconciled", abandoned_taker=taker, action=action)

    def _apply_node(
        self,
        node: PlanNode,
        merged: MergedPlan,
        transcript: Transcript,
        plan_bytes: bytes,
        authority: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
        *,
        supersede: Mapping[str, Any] | None = None,
    ) -> RunOutcome:
        run_id = new_ulid()
        if supersede is None:
            self._claim(node.component, run_id, node.scope, "apply")
        else:
            self._takeover_claim(node, supersede, run_id)  # T-5875: fence + CAS hand-over from the stuck run
        tup = RunTuple(self.instance, node.component, node.scope, node.leg, run_id)
        store = RunStore(self.backend.ensure_run_state_root(node.scope), uid=self.uid)
        try:
            handle = store.create(
                tup,
                operations=[
                    {
                        "op_id": op.operation_id,
                        "crash_contract": op.crash_contract,
                        "precondition_check": dict(op.precondition),
                        "postcondition_check": dict(op.desired),
                    }
                    for op in node.operations
                ],
                plan=plan_bytes,  # written BEFORE record.json: an admitted run always has its plan + manifest (T-5810)
            )
        except Exception:
            if supersede is None:
                self._release(node.component, run_id)  # no record was admitted, so the claim must not outlive it
            else:
                self._restore_claim(node, supersede, run_id)  # ... and a takeover hands it back, fence cleared
            raise
        if supersede is not None:
            _supersession_seam("after-create", sup=supersede, run_id=run_id)
        try:
            run_t = None
            try:
                wal = handle.wal()
                run_t = transcript.bind(handle)
                run_t.emit(
                    "run.admitted",
                    **self._context_fields(),
                    component=node.component,
                    leg=node.leg,
                    scope=node.scope,
                    plan_digest=merged.digest,
                    contract_digest=node.contract_digest,
                    op_registry_digest=merged.op_registry_digest,
                    expected_runtime_subset={"digest": merged.expected_runtime_subset_digest, "manifest_revision": None},
                    evidence_scope=self.backend.evidence_scope,
                )
                if supersede is not None:
                    self._commit_supersession_locked(handle, run_t, node, run_id, supersede)
                    _supersession_seam("after-commit", sup=supersede, run_id=run_id)
                handle.update(state=State.ACTIVE)
            except EngineRefusal as exc:
                if exc.code != "supersession-lost":
                    self._settle_pre_op_failure(handle, node, run_t, exc)
                self._halt_supersession_lost(handle, node, run_t, exc)
            except Exception as exc:  # noqa: BLE001 -- a pre-op failure leaves a recoverable run, never a wedge (T-5810)
                self._settle_pre_op_failure(handle, node, run_t, exc)
            if supersede is not None:
                holder = self._claim_holder(node.component)  # before the first actuation: the claim must still be ours
                if holder is None or holder.get("run_id") != run_id:
                    self._halt_supersession_lost(handle, node, run_t, EngineRefusal(
                        "supersession-lost", f"run {run_id}: the claim on {node.component} moved before the first actuation"))
            for op in node.operations:
                try:
                    self._execute_op(handle, wal, node, merged, op, run_t, (authority or {}).get((node.component, op.operation_id)))
                except Exception as exc:  # noqa: BLE001 -- any op failure halts the run (ORG-080 §10), claim retained
                    reason = f"{type(exc).__name__}: {exc}"
                    halted = {"op_id": op.operation_id, "reason": reason, "at": now_iso()}
                    escalations = []
                    if isinstance(exc, EscalatingHalt):
                        # T-5858: the halt itself left the component's subject unsupervised; the
                        # durable marker is written WITH the halt, before the page
                        escalations = [{  # T-5868 Phase B: one entry per recipient (the component names them)
                            "op_id": op.operation_id, "subject": exc.subject, "reason": exc.reason,
                            "recipient": recipient, "rollback": rollback_command(run_id),
                        } for recipient in exc.recipients]
                        halted["escalations"] = escalations
                    handle.update(state=State.HALTED, halted=halted)
                    run_t.emit("run.halted", op_id=op.operation_id, reason=reason)
                    self._page_escalations_once(handle, run_t, run_id, escalations)
                    if isinstance(exc, EscalatingHalt):
                        self._record_halt_on_host(handle, run_t, run_id, op, exc)
                    message = f"run {run_id} HALTED at {op.operation_id}: {reason}"
                    if op.rollback_on_halt:  # T-5866: opt-in only; every other op halts exactly as before
                        message += self._rollback_on_halt(run_id, run_t, op.operation_id)
                    raise EngineRefusal("halted", message) from exc
            handle.update(state=State.SATISFIED)
            self._release(node.component, run_id)
            run_t.emit("run.satisfied")
            return RunOutcome(node.component, run_id, State.SATISFIED.value)
        finally:
            handle.close()

    def _record_halt_on_host(self, handle: RunHandle, t: Transcript, run_id: str, op, exc: EscalatingHalt) -> None:
        """T-5868 Phase B: after the HALTED record and the page are durable, let the component's
        handler record the halt on the host (``backend.on_halted(run_id, op, exc)``, optional; U5
        replaces its paused-intake seat-state record with the ORG-106 halted one). Review condition:
        if the hook raises, the halt still stands and the same recipients get a page of a distinct
        class, because the seat would otherwise read as an authorised pause. Never raises."""

        hook = getattr(self.backend, "on_halted", None)
        if hook is None:
            return
        try:
            hook(run_id, op, exc)
            return
        except Exception as hook_exc:  # noqa: BLE001 -- the halt stands; the failure is paged, never fatal
            failure = f"{type(hook_exc).__name__}: {hook_exc}"
        try:
            reason = f"{HALT_NOT_RECORDED}: {failure}"
            self._page_escalations_once(handle, t, run_id, [{
                "op_id": op.operation_id, "subject": exc.subject, "reason": reason, "recipient": recipient,
                "rollback": rollback_command(run_id), "phase": "halt-unrecorded",
            } for recipient in exc.recipients])
        except Exception:  # noqa: BLE001 -- best effort: the halt itself is already durable
            pass

    def _rollback_on_halt(self, run_id: str, run_t: Transcript, op_id: str) -> str:
        """T-5866 (D15 ruling on T-5858, condition r2): the halted op opted in, so roll the run back NOW.

        The HALTED record, its escalation marker and its one page are already durable. This
        is the manual ``rollback(run_id)`` itself, not a second loop, so carry-forward,
        escalation and claim handling are the same by construction. The ``rollback.auto``
        event is written first: a crash after it leaves the run HALTED with its marker and
        page, and the operator or recovery ``rollback(run_id)`` finishes it. A crash
        mid-rollback is ``reconcile_run`` then ``rollback`` (the WAL classifies what is
        already reversed, so no inverse is applied twice). Returns the outcome text the
        caller appends to its ``halted`` refusal; a BaseException (a process death)
        propagates untouched.
        """

        run_t.emit("rollback.auto", op_id=op_id)
        try:
            outcome = self.rollback(run_id)
        except EngineRefusal as exc:
            return f"; rollback_on_halt: {exc.code}: {exc.message}"
        except Exception as exc:  # noqa: BLE001 -- never mask the halt; reconcile_run then rollback recovers
            return (f"; rollback_on_halt failed ({type(exc).__name__}: {exc}); "
                    f"the claim is held and the halt marker is kept: retry with {rollback_command(run_id)} "
                    "(if it reports run-active ROLLING_BACK, the settle itself failed: "
                    "reconcile_run(executor_dead=True), then retry)")
        return f"; rollback_on_halt: {outcome.state}"

    def _settle_pre_op_failure(self, handle: RunHandle, node: PlanNode, run_t: Transcript | None, exc: Exception) -> None:
        """A failure after admission (record.json) but before the first op. Always raises.

        No op has written an intent, so nothing is on the host. With ops, the run
        halts at its first op through the existing PENDING->ACTIVE->HALTED edges and
        KEEPS the claim, exactly like an op failure (ORG-080 §10): ``rollback`` then
        finds every op BEFORE_INTENT and closes ROLLED_BACK, releasing the claim. A
        zero-op node has no op to halt at, so it closes SATISFIED (vacuously
        complete, as ``reconcile_run`` closes it) and releases. If these writes fail
        too, the record stays PENDING/ACTIVE with its plan and manifest on disk
        (``RunStore.create``), so ``reconcile_run`` then ``rollback`` recover it.
        """

        run_id = handle.tuple.run_id
        reason = f"pre-op failure: {type(exc).__name__}: {exc}"
        try:
            if State(handle.read_record()["state"]) is State.PENDING:
                handle.update(state=State.ACTIVE)
            if not node.operations:
                handle.update(state=State.SATISFIED)
                self._release(node.component, run_id)
            else:
                handle.update(
                    state=State.HALTED,
                    halted={"op_id": node.operations[0].operation_id, "reason": reason, "at": now_iso()},
                )
        except Exception as settle_exc:  # noqa: BLE001 -- unsettled: the claim stays and reconcile_run recovers the run
            if isinstance(settle_exc, EngineRefusal) and settle_exc.code == "claim-holder-mismatch":
                # T-5819 N5: our claim is gone and another run holds the component; reconcile_run
                # cannot fix that, so surface the typed refusal instead of a misleading remedy
                raise EngineRefusal(
                    "claim-holder-mismatch",
                    f"run {run_id}: {reason}; claim held by another run: investigate as tampering "
                    f"({settle_exc.message})",
                ) from exc
            # a zero-op run needs only reconcile_run (it closes SATISFIED or releases a SATISFIED run's
            # claim; rollback refuses SATISFIED); a run with ops reconciles to HALTED, then rolls back
            remedy = "reconcile_run" if not node.operations else "reconcile_run then rollback"
            raise EngineRefusal(
                "pre-op-failure",
                f"run {run_id}: {reason}; settling it also failed ({type(settle_exc).__name__}: {settle_exc}); "
                f"the claim is held: {remedy}",
            ) from exc
        if not node.operations:
            raise EngineRefusal(
                "pre-op-failure", f"run {run_id}: {reason}; the zero-op run closed SATISFIED and its claim is released"
            ) from exc
        op_id = node.operations[0].operation_id
        if run_t is not None:
            try:
                run_t.emit("run.halted", op_id=op_id, reason=reason)
            except Exception:  # noqa: BLE001 -- the record already carries the halt; a transcript fault must not mask it
                pass
        raise EngineRefusal("halted", f"run {run_id} HALTED before {op_id} ran: {reason}") from exc

    def _execute_op(
        self,
        handle: RunHandle,
        wal: WalStore,
        node: PlanNode,
        merged: MergedPlan,
        op: Operation,
        t: Transcript,
        grant: Mapping[str, str] | None = None,
    ) -> None:
        backend = self.backend
        # T-5941 (S7) defence in depth: a path that skipped _plan still checks the declared privilege
        # before any preflight, WAL intent or actuation.
        why = privmod.refusal(backend, self.context, node.scope, op, node.component)
        if why is not None:
            raise OperationFailed(f"privilege refused: {why}")
        inst = op.to_install_operation()
        pre = backend.preflight(inst)
        if not pre.ok:
            raise OperationFailed(f"preflight refused ({pre.classification}): {pre.detail}")
        inverse = None if op.side_effect_free else backend.rollback_intent(inst)
        inverse_obj = {"kind": "none"} if inverse is None else mutation_to_json_obj(inverse)
        if not op.side_effect_free and inverse is None:
            raise OperationFailed(f"{op.operation_id}: no write-ahead inverse available (ORG-080 D-X)")
        intent_ref, intent_digest = wal.write_intent(
            op_id=op.operation_id,
            effect_domain="local",
            preimage=_payload(canonical_json_bytes(op.to_json_obj())),
            inverse=_payload(canonical_json_bytes(inverse_obj)),
            postcondition_check={"kind": op.kind, "desired": dict(op.desired)},
            created_at=now_iso(),
        )
        t.emit("op.intent", op_id=op.operation_id, crash_contract=op.crash_contract)
        backend.checkpoint("after-intent", op.operation_id)
        mutation, _evidence = backend.execute(inst)
        t.emit("op.executed", op_id=op.operation_id, changed=mutation.changed)
        backend.checkpoint("after-execute", op.operation_id)
        verified = backend.verify_operation(inst)
        if not verified.ok:
            raise OperationFailed(f"postcondition unmet: {verified.detail}")
        body = build_body(
            run_id=handle.tuple.run_id,
            op_id=op.operation_id,
            component=node.component,
            leg=node.leg,
            crash_contract=op.crash_contract,
            plan_digest=merged.digest,
            contract_digest=node.contract_digest,
            op_registry_digest=merged.op_registry_digest,
            evidence_scope=backend.evidence_scope,
            mutation=mutation,
            evidence=verified,
            authority=None
            if grant is None
            else {"gate_id": grant["gate"], "scope": grant["scope"], "authority_record_id": grant["authority_record_id"]},
        )
        receipt_ref, _ = wal.write_receipt(
            op_id=op.operation_id,
            intent_ref=intent_ref,
            intent_digest=intent_digest,
            postcondition_result={"satisfied": True, "detail": verified.detail},
            receipt_payload=body,
            committed_at=now_iso(),
        )
        backend.checkpoint("after-receipt", op.operation_id)
        stamp = now_iso()
        wal.commit_receipt_reference(op_id=op.operation_id, receipt_ref=receipt_ref, committed_at=stamp, updated_at=stamp)
        backend.checkpoint("after-manifest", op.operation_id)
        handle.update(receipts=[e.to_json_obj() for e in wal.read_manifest().receipts])
        t.emit("op.receipted", op_id=op.operation_id, receipt_ref=receipt_ref)
        backend.checkpoint("after-record", op.operation_id)

    # ------------------------------------------------------------------
    # rollback (HALTED / PARKING_FAILED / ROLLBACK_INCOMPLETE only: bubble 7)
    # ------------------------------------------------------------------

    def rollback(self, run_id: str) -> RunOutcome:
        self.require_postboot()
        store, tup = self._find_run(run_id)
        self._reconcile_supersession(tup.component)  # T-5875: settle a crashed takeover before the fence check below
        transcript = Transcript(new_ulid(), echo=self.echo)
        with store.open(tup) as handle:
            try:
                state = State(handle.read_record()["state"])
            except RunStateError as exc:
                raise EngineRefusal("run-state-refused", str(exc)) from None
            if state in (State.ACTIVE, State.PENDING, State.ROLLING_BACK):
                raise EngineRefusal(
                    "run-active",
                    f"run {run_id} is {state.value}; if its executor is dead, reconcile it first "
                    "(Engine.reconcile_run, library-only in T-5074)",
                )
            if state not in ROLLBACK_ADMISSIBLE:
                raise EngineRefusal(
                    "rollback-refused",
                    f"run {run_id} is {state.value}; rollback admits only "
                    f"{', '.join(sorted(s.value for s in ROLLBACK_ADMISSIBLE))} runs (SATISFIED and ROLLED_BACK are absorbing)",
                )
            try:
                plan = handle.read_plan()
                ops = [operation_from_json_obj(o) for o in plan["node"]["operations"]]
                wal = handle.wal()
            except Exception as exc:  # noqa: BLE001 -- an unreadable plan refuses before any state change
                raise EngineRefusal("run-state-refused", f"run {run_id}: plan/WAL unreadable: {type(exc).__name__}: {exc}") from None
            for op in ops:  # T-5243: a manual rollback re-actuates recorded inverses without re-planning
                why = ctxmod.refusal(self.context, op)
                if why is not None:
                    raise EngineRefusal("context-refused", f"rollback of run {run_id}: {why}")
                # review fix-up: a manual rollback also re-checks privilege against the CURRENT backend/context
                # (the run may have been applied under another backend), before any claim or state write
                why = privmod.refusal(self.backend, self.context, tup.scope, op, tup.component)
                if why is not None:
                    raise EngineRefusal("privilege-refused", f"rollback of run {run_id}: {why}")
            # T-5875: the fence check, the claim check and the ROLLBACK_INCOMPLETE -> ROLLING_BACK write are ONE
            # critical section under the claim flock, the one a takeover swaps the claim under: mutually exclusive.
            with self._component_lock(tup.component):
                _supersession_seam("rollback-locked", run_id=run_id)
                fresh = handle.read_record()
                if fresh["state"] != state.value:
                    raise EngineRefusal("run-state-refused", f"run {run_id} moved from {state.value} to {fresh['state']} concurrently")
                fence = (fresh.get("rollback_incomplete") or {}).get("superseded_by")
                if fence is not None:
                    # P4: a gated decommission fenced this run (pending or committed); the remove run owns the
                    # rollback now, and a retry would re-actuate inverses on a decommissioned component
                    raise EngineRefusal(
                        "rollback-superseded",
                        f"run {run_id} was superseded by remove run {fence}; a retry would re-actuate inverses "
                        "on a decommissioned component",
                    )
                holder = self._claim_holder(tup.component)
                if holder is None:
                    self._claim(tup.component, run_id, tup.scope, "rollback")
                elif holder.get("run_id") != run_id:
                    raise EngineRefusal("claim-held", f"{tup.component} is held by run {holder.get('run_id')}")
                t = transcript.bind(handle)
                t.emit("rollback.started", from_state=state.value)
                # review R3-1: escalations from an earlier attempt survive this rebuild while
                # their op is still outstanding (a retry refused for another reason, e.g. drift,
                # must not erase the marker of a subject that is still unsupervised)
                record = handle.read_record()
                prior_escalations = list((record.get("rollback_incomplete") or {}).get("escalations") or ())
                halted_op = (record.get("halted") or {}).get("op_id")
                if any(op.operation_id == halted_op and op.rollback_on_halt for op in ops):
                    # T-5866: an opt-in op's forward-halt marker survives until the run is fully reversed
                    prior_escalations += list(record["halted"].get("escalations") or ())
                # T-5960: the marker is durable from this write on, so a crash or an I/O fault before the final
                # ROLLBACK_INCOMPLETE write cannot lose it (the non-opt-in path writes None, as before)
                handle.update(
                    state=State.ROLLING_BACK, halted=None,
                    rollback_incomplete={"escalations": list(prior_escalations)} if prior_escalations else None,
                )
            escalations: list[dict[str, str]] = []
            try:
                return self._rollback_settled(handle, t, tup, run_id, ops, wal, prior_escalations, escalations)
            except EngineRefusal:
                raise
            except Exception:
                # an ordinary fault after ROLLING_BACK: settle in process so the operator's plain retry works
                # (a BaseException, i.e. a process death, is deliberately NOT caught: it is a crash)
                self._settle_interrupted_rollback(handle, run_id, prior_escalations, escalations)
                raise

    def _rollback_settled(self, handle, t, tup, run_id, ops, wal, prior_escalations, escalations) -> RunOutcome:
        outstanding = []
        for op in reversed(ops):
            if op.side_effect_free:
                continue
            try:
                mutation = self._mutation_for_rollback(wal, op)
                if mutation is None:
                    continue
                check = self.backend.can_rollback(mutation)
                if check.classification == "not-applied":
                    t.emit("op.rollback.skipped", op_id=op.operation_id, detail=check.detail)
                    continue
                if not check.ok:
                    raise OperationFailed(f"cannot roll back ({check.classification}): {check.detail}")
                self.backend.rollback_mutation(mutation)
                t.emit("op.rolled_back", op_id=op.operation_id)
            except EscalatingRollbackStop as exc:
                # the undo changed the host, then stopped, leaving its subject unsupervised
                outstanding.append(op.operation_id)
                escalations.append(
                    {"op_id": op.operation_id, "subject": exc.subject, "reason": exc.reason, "recipient": exc.recipient}
                )
                t.emit("op.rollback.failed", op_id=op.operation_id, reason=f"{type(exc).__name__}: {exc}")
            except Exception as exc:  # noqa: BLE001 -- an unreversed op is carried forward, never dropped (ORG-080 §10)
                outstanding.append(op.operation_id)
                t.emit("op.rollback.failed", op_id=op.operation_id, reason=f"{type(exc).__name__}: {exc}")
        if outstanding:
            incomplete = {"failed_run_id": run_id, "outstanding_inverses": outstanding}
            # carried forward until that op rolls back (``escalations`` stays the fresh ones only)
            merged = self._carried_escalations(prior_escalations, outstanding, escalations) + escalations
            if merged:
                incomplete["escalations"] = merged  # the durable marker, written before the page
            handle.update(state=State.ROLLBACK_INCOMPLETE, rollback_incomplete=incomplete)
            self._page_escalations_once(handle, t, run_id, merged)
            t.emit("rollback.incomplete", outstanding=outstanding)
            raise EngineRefusal("rollback-incomplete", f"run {run_id}: unreversed {outstanding}; the claim stays held (D-Y)")
        handle.update(state=State.ROLLED_BACK, rollback_incomplete=None)  # drops the T-5960 marker
        self._release(tup.component, run_id)
        t.emit("rollback.complete")
        return RunOutcome(tup.component, run_id, State.ROLLED_BACK.value)

    @staticmethod
    def _carried_escalations(prior: list[Any], outstanding: list[str], fresh: list[dict[str, str]]) -> list[dict[str, str]]:
        """Earlier markers that survive a rebuild (review R3-1): a halt-phase marker (only an opt-in
        op's, T-5866) stays while ANY inverse is outstanding, since its own op may have no effect to
        undo (e.g. a drain); a rollback-phase marker stays while its op is outstanding and was not
        re-raised fresh. One filter for the rollback, the in-process settle and the reconcile."""

        fresh_ids = {item["op_id"] for item in fresh}
        return [
            item for item in prior
            if isinstance(item, dict) and (
                _page_phase(item) == "halt"
                or (item.get("op_id") in outstanding and item.get("op_id") not in fresh_ids)
            )
        ] if outstanding else []

    def _settle_interrupted_rollback(
        self, handle: RunHandle, run_id: str, prior: list[Any], fresh: list[dict[str, str]]
    ) -> None:
        """T-5960: best effort, never raises. An ordinary exception left the record in ROLLING_BACK:
        move it to ROLLBACK_INCOMPLETE with the carried marker so a plain ``rollback`` retry is
        admitted. If this write fails too, the ROLLING_BACK write already holds the marker and
        ``reconcile_run`` finishes the move. A ROLLED_BACK (or any other) record is left alone."""

        try:
            if State(handle.read_record()["state"]) is not State.ROLLING_BACK:
                return
            outstanding = self._outstanding_inverses(handle)
            incomplete: dict[str, Any] = {"failed_run_id": run_id, "outstanding_inverses": outstanding}
            merged = self._carried_escalations(prior, outstanding, fresh) + (fresh if outstanding else [])
            if merged:
                incomplete["escalations"] = merged
            handle.update(state=State.ROLLBACK_INCOMPLETE, rollback_incomplete=incomplete)
        except Exception:  # noqa: BLE001 -- best effort; the marker is already durable
            pass

    def _rebuild_escalations(self, handle: RunHandle, run_id: str, outstanding: list[str]) -> list[dict[str, str]]:
        """T-5960 fallback: rebuild the marker from this run's ``escalation.page`` events when the
        record lost it (a ROLLING_BACK record written before the durable marker). Keeps the same set
        ``rollback`` carries: halt pages of an opt-in op in the plan, rollback pages whose op is still
        outstanding. Never emits a page (``_page_escalations_once`` is the only sender)."""

        if not outstanding:
            return []
        opt_in = {
            raw.get("operation_id") for raw in handle.read_plan()["node"]["operations"]
            if isinstance(raw, dict) and raw.get("rollback_on_halt")
        }
        rebuilt: dict[tuple[str, str, str], dict[str, str]] = {}
        for line in handle.read_transcript():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not (isinstance(event, dict) and event.get("event") == "escalation.page"):
                continue
            phase, op_id = _page_phase(event), event.get("op_id")
            if phase not in ("halt", "rollback"):
                continue  # T-5868: a halt-unrecorded page is not a rollback marker
            if phase == "halt" and op_id not in opt_in:
                continue
            if phase == "rollback" and op_id not in outstanding:
                continue
            marker = {k: event.get(k) for k in ("op_id", "subject", "reason", "recipient")}
            if phase == "halt":
                marker["rollback"] = event.get("rollback") or rollback_command(run_id)
            if not all(isinstance(v, str) and v for v in marker.values()):
                continue
            rebuilt.setdefault((op_id, phase, marker["recipient"]), marker)
        return list(rebuilt.values())

    @staticmethod
    def _page_escalations_once(handle: RunHandle, t: Transcript, run_id: str, escalations: list[dict[str, str]]) -> None:
        """One ``escalation.page`` intent per (run_id, op_id, phase), addressed to the
        component-named ``recipient``, beside the durable ``halted`` / ``rollback_incomplete.escalations``
        marker (see tools/germinate/engine/escalation.py).

        Recorded, never delivered (no mailbox, no live I/O; a future live handler
        delivers it). Idempotent: a retried rollback of the same run finds the earlier
        event in this run's transcript and emits nothing, so a stop is never silent and
        never a retry loop of pages. Non-object or unparseable transcript lines are
        skipped, never fatal.
        """

        if not escalations:
            return
        paged = set()
        for line in handle.read_transcript():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == "escalation.page":
                paged.add((event.get("op_id"), _page_phase(event), event.get("recipient")))
        for item in escalations:
            key = (item["op_id"], _page_phase(item), item["recipient"])  # T-5868: per (run, op, phase, recipient)
            if key in paged:
                continue
            extra = {"rollback": item["rollback"]} if "rollback" in item else {}  # a forward halt's page (T-5858)
            t.emit(
                "escalation.page", op_id=item["op_id"], recipient=item["recipient"], subject=item["subject"],
                reason=item["reason"], page_run_id=run_id, delivery="deferred-dark", phase=key[1], **extra,
            )
            paged.add(key)

    @staticmethod
    def _mutation_for_rollback(wal: WalStore, op: Operation):
        cls = wal.classify_prefix(op_id=op.operation_id)
        if cls.structural_prefix is StructuralPrefix.BEFORE_INTENT:
            return None
        if cls.receipt_ref is not None:
            receipt, _ = wal.read_receipt_by_ref(cls.receipt_ref)
            return mutation_from_json_obj(receipt.receipt_payload["mutation"])
        intent, _ = wal.read_intent(op.operation_id)
        inverse = json.loads(base64.b64decode(intent.inverse.bytes_b64))
        if inverse.get("kind") == "none":
            return None
        return mutation_from_json_obj(inverse)

    # ------------------------------------------------------------------
    # crash reconciliation (library-only: bubble 10)
    # ------------------------------------------------------------------

    def _outstanding_inverses(self, handle: RunHandle) -> list[str]:
        """Ops of a run whose effect is still on the host (not yet reversed), newest first."""

        plan = handle.read_plan()
        wal = handle.wal()
        outstanding = []
        for raw in reversed(plan["node"]["operations"]):
            op = operation_from_json_obj(raw)
            if op.side_effect_free:
                continue
            try:
                mutation = self._mutation_for_rollback(wal, op)
                if mutation is not None and self.backend.can_rollback(mutation).classification != "not-applied":
                    outstanding.append(op.operation_id)
            except Exception:  # noqa: BLE001 -- unclassifiable counts as outstanding (fail-closed)
                outstanding.append(op.operation_id)
        return outstanding

    def reconcile_run(self, run_id: str, *, executor_dead: bool) -> RunOutcome:
        """Classify a run whose executor died mid-apply or mid-rollback. The caller asserts the death.

        ACTIVE (or PENDING) -> HALTED at the first unreceipted op, or SATISFIED
        if every op is receipted. ROLLING_BACK -> ROLLBACK_INCOMPLETE with the
        not-yet-reversed ops outstanding; ``rollback`` then retries (D-Y).
        SATISFIED / ROLLED_BACK still holding the claim -> release it (the close
        failed after the record write; ORG-080 admission matrix, T-5810).
        """

        self.require_postboot()  # C3 covers library-call surfaces too
        if executor_dead is not True:
            raise EngineRefusal("liveness-unknown", "reconcile needs an explicit executor_dead=True; there is no liveness primitive")
        if not self.context.is_operator:
            # T-5243: reconcile actuates recorded inverses without re-planning; the recorded ops are
            # not re-classified here, so a non-operator context fails closed (P1).
            raise EngineRefusal(
                "context-refused",
                f"reconcile of run {run_id} is operator-only; refused in the {self.context.kind} invocation context (ORG-080 A-3)",
            )
        store, tup = self._find_run(run_id)
        holder = self._claim_holder(tup.component)
        if holder is None or holder.get("run_id") != run_id:
            raise EngineRefusal("tampering-signal", f"run {run_id} is in flight but does not hold {tup.component}'s claim")
        if not store.has_record(tup):
            # ORG-080 admission matrix: "active_run_id == run_X + no record.json" is a tampering signal,
            # fail-closed (T-5810). It is also what a crash inside RunStore.create leaves; either way
            # the claim is NOT released here: an operator inspects the directory first.
            raise EngineRefusal(
                "tampering-signal",
                f"run {run_id} holds {tup.component}'s claim but its run directory has no record.json "
                "(a crash inside run admission, or tampering); the claim stays held for operator investigation",
            )
        with store.open(tup) as handle:
            record = handle.read_record()
            state = State(record["state"])
            if state is State.PENDING:
                record = handle.update(state=State.ACTIVE)  # admission-matrix row 3
                state = State.ACTIVE
            if state is State.ROLLING_BACK:
                outstanding = self._outstanding_inverses(handle)
                incomplete: dict[str, Any] = {"failed_run_id": run_id, "outstanding_inverses": outstanding}
                kept = list((record.get("rollback_incomplete") or {}).get("escalations") or ())
                # T-5960: the durable marker from the ROLLING_BACK write, else rebuilt from the page events
                escalations = (self._carried_escalations(kept, outstanding, []) if kept
                               else self._rebuild_escalations(handle, run_id, outstanding))
                if escalations:
                    incomplete["escalations"] = escalations
                handle.update(state=State.ROLLBACK_INCOMPLETE, rollback_incomplete=incomplete)
                # a crash before the page leaves a stop that was never announced: send it now (idempotent)
                self._page_escalations_once(handle, Transcript(new_ulid(), echo=self.echo).bind(handle), run_id, escalations)
                return RunOutcome(tup.component, run_id, State.ROLLBACK_INCOMPLETE.value)
            if state in (State.SATISFIED, State.ROLLED_BACK):
                # ORG-080 admission matrix: terminal record + gate still naming this run means the
                # close crashed between the record write and the gate clear; finish it (T-5810).
                self._release(tup.component, run_id)
                return RunOutcome(tup.component, run_id, state.value)
            if state is not State.ACTIVE:
                raise EngineRefusal("nothing-to-reconcile", f"run {run_id} is {state.value}")
            wal = handle.wal()
            try:
                committed = list(wal.read_manifest().receipts)
            except WalStoreError as exc:
                if "open-regular:2" not in str(exc):
                    raise EngineRefusal("run-state-refused", str(exc)) from None
                committed = []
            done = {e.op_id for e in committed}
            handle.update(receipts=[e.to_json_obj() for e in committed])
            for op in record["operations"]:
                if op["op_id"] not in done:
                    try:
                        prefix = wal.classify_prefix(op_id=op["op_id"]).structural_prefix.value
                    except ReconcileError as exc:
                        prefix = f"refuse:{exc}"
                    handle.update(
                        state=State.HALTED,
                        halted={"op_id": op["op_id"], "reason": f"executor-crash (wal prefix {prefix})", "at": now_iso()},
                    )
                    return RunOutcome(tup.component, run_id, State.HALTED.value)
            handle.update(state=State.SATISFIED)
            self._release(tup.component, run_id)
            return RunOutcome(tup.component, run_id, State.SATISFIED.value)

    # ------------------------------------------------------------------
    # verify (drift check: bubble 9) and status
    # ------------------------------------------------------------------

    def _verify_component(self, component: str, scope: str) -> dict[str, Any]:
        candidates = [
            (t, rec)
            for t, rec in self._runs(component)
            if t.scope == scope and t.leg in FORWARD_LEGS and rec["state"] == State.SATISFIED.value
        ]
        if not candidates:
            return {"component": component, "run_id": None, "ok": True, "checks": [], "detail": "no satisfied run to verify"}
        tup, record = max(candidates, key=lambda item: item[0].run_id)
        store = RunStore(self.backend.run_state_root(scope), uid=self.uid)
        checks = []
        with store.open(tup) as handle:
            plan = handle.read_plan()
            receipted = {r["op_id"] for r in record["receipts"]}
            for raw in plan["node"]["operations"]:
                op = operation_from_json_obj(raw)
                if op.side_effect_free or op.operation_id not in receipted:
                    continue
                evidence = self.backend.verify_operation(op.to_install_operation())
                checks.append({"op_id": op.operation_id, "ok": evidence.ok, "detail": evidence.detail})
        result = {"component": component, "run_id": tup.run_id, "ok": all(c["ok"] for c in checks), "checks": checks}
        invariants = self._invariant_check(component, scope)
        if invariants is not None:
            result["invariants"] = invariants
            result["ok"] = result["ok"] and invariants["ok"]
        return result

    def _invariant_check(self, component: str, scope: str) -> dict[str, Any] | None:
        """T-5135 S1: the ONE invariant list, evaluated (ORG-080 §2 / A-6). Alarm is never a pass."""

        doc, _ = self._registry()
        row = self._component_rows(doc).get(component)
        if row is None or row["contract"].get("digest") == RS.DIGEST_UNPINNED or row["contract"].get("repo") != RS.INTERNAL_REPO:
            return None
        contract = self._load_row_contract(row)  # recomputes the list digest (S4)
        verify_leg = contract.legs.get("verify")
        if not (isinstance(verify_leg, Mapping) and verify_leg.get("invariant_list_ref")):
            if contract.body.get("invariant_list_digest") is None:
                return None
            # a declared list the verify leg does not reference: could not ask, never a silent skip
            return {
                "list_ref": None,
                "ok": False,
                "state": None,
                "results": [{"id": "*", "status": "alarm", "detail": "an invariant list is declared but legs.verify has no invariant_list_ref: could not ask"}],
            }
        ref = verify_leg["invariant_list_ref"]

        def alarm(detail: str) -> dict[str, Any]:
            return {"list_ref": ref, "ok": False, "state": None, "results": [{"id": "*", "status": "alarm", "detail": detail}]}

        hook = self.verify_hooks.get(component)
        if hook is None:
            return alarm("no VERIFY hook registered for a component that declares a verify list: could not ask")
        try:
            list_doc = load_invariant_list(list_path_for(contract.path))
            kinds = frozenset().union(*contract.privilege.values()) if contract.privilege else frozenset()
            out = hook(list_doc, self.backend.observe(component, scope, kinds), scope)
            results = list(out["results"])
            state = out.get("state")
        except Exception as exc:  # noqa: BLE001 -- a broken evaluator is an alarm, never a pass
            return alarm(f"evaluator failed: {type(exc).__name__}: {exc}")
        clean = [
            r if isinstance(r, Mapping) and isinstance(r.get("status"), str) and r["status"] in INVARIANT_STATUSES else {"id": "?", "status": "alarm", "detail": "malformed result"}
            for r in results
        ]
        if not clean:
            return alarm("evaluator returned no results")
        return {"list_ref": ref, "ok": all(r["status"] in ("pass", "vacuous") for r in clean), "state": state, "results": clean}

    def verify(self, scope: str, system: str | None = None) -> list[dict[str, Any]]:
        self.require_postboot()
        self._validate_scope(scope)
        doc, _ = self._registry()
        rows = self._component_rows(doc)
        if system is not None and system not in rows:
            raise EngineRefusal("unknown-component", f"{system!r} is not a registry component id")
        targets = [system] if system is not None else sorted(rows)
        return [self._verify_component(cid, scope) for cid in targets]

    def status(self) -> list[dict[str, Any]]:
        """ORG-080 §1: coverage from the registry joined, display-only, with run state."""

        self.require_postboot()
        doc, _ = self._registry()
        for rid in sorted(self._component_rows(doc)):
            try:
                self._reconcile_supersession(rid)  # T-5875; a busy lock leaves the fence, status stays a read
            except EngineRefusal:
                pass
        runs = self._runs()
        orphans = [t for store in self._stores() for t in store.orphan_tuples()]
        out = []
        for rid, row in sorted(self._component_rows(doc).items()):
            latest: dict[str, dict[str, Any]] = {}
            for tup, record in runs:
                if tup.component == rid:
                    latest[f"{tup.scope}/{tup.leg}"] = {"run_id": tup.run_id, "state": record["state"]}
                    escalations = (record.get("rollback_incomplete") or {}).get("escalations") or (
                        record.get("halted") or {}).get("escalations")
                    superseded_by = (record.get("rollback_incomplete") or {}).get("superseded_by")
                    if superseded_by:
                        latest[f"{tup.scope}/{tup.leg}"]["superseded_by"] = superseded_by
                    if escalations:  # an unsupervised subject left by a stopped undo or an escalating halt
                        latest[f"{tup.scope}/{tup.leg}"]["escalations"] = escalations
            holder = self._claim_holder(rid)
            out.append(
                {
                    "component": rid,
                    "contract_adoption": row.get("contract_adoption"),
                    "digest": row["contract"].get("digest"),
                    "claim": None if holder is None else {"run_id": holder.get("run_id"), "verb": holder.get("verb")},
                    "runs": latest,
                    # record-less run directories (T-5810): reported, never read as runs
                    "orphan_runs": sorted(t.run_id for t in orphans if t.component == rid),
                }
            )
        return out
