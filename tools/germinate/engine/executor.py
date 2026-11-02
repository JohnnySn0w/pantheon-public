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
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, TextIO

from tools.germinate.engine.backend import SimRootRefused, SimulatedHostBackend
from tools.germinate.engine.contract import ContractError, LoadedContract, contained_module_path, load_contract
from tools.germinate.engine.dag import MergedPlan, PlanNode, order_components, plan_digest
from tools.germinate.engine.grants import GrantConsumer, GrantRefused, consume as consume_grants, derive_requests
from tools.germinate.engine.invariant_digest import list_path_for, load_list as load_invariant_list
from tools.germinate.engine.operation import Operation, OperationError, operation_from_json_obj, parse_operation
from tools.germinate.engine.planner import REQUEST_SCHEMA, InProcessJsonPlannerRunner, PlanError, PlannerRunner
from tools.germinate.engine.receipts import build_body, mutation_from_json_obj, mutation_to_json_obj
from tools.germinate.engine.run_state import MAX_PLAN_BYTES, RunHandle, RunStateError, RunStore, RunTuple, encode_plan, new_ulid, now_iso
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
        backend: SimulatedHostBackend,
        *,
        repo_root: Path,
        registry_path: Path | None = None,
        instance: str = "sim",
        runner: PlannerRunner | None = None,
        echo: TextIO | None = None,
        verify_hooks: Mapping[str, Any] | None = None,
    ) -> None:
        self.verify_hooks = dict(verify_hooks or {})
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
        sim_root: Path,
        *,
        repo_root: Path,
        registry_path: Path | None = None,
        instance: str = "sim",
        runner: PlannerRunner | None = None,
        echo: TextIO | None = None,
    ) -> "Engine":
        """Wire every registered component's handlers and op kinds (components.py), then the engine.

        This is the only constructor the CLI uses: a component adopts the
        engine by shipping files, never by editing it.
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
            backend = SimulatedHostBackend(sim_root, op_registry=op_registry, handlers=handlers)
        except ValueError as exc:
            raise EngineRefusal("component-registration-refused", str(exc)) from None
        hooks = {reg.component: reg.verify for reg in regs if reg.verify is not None}
        return cls(
            backend, repo_root=repo_root, registry_path=registry_path, instance=instance, runner=runner, echo=echo, verify_hooks=hooks
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
        except SimRootRefused as exc:
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
        for cid, contract in contracts.items():
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
    # apply
    # ------------------------------------------------------------------

    def _check_gates(
        self,
        merged: MergedPlan,
        contracts: Mapping[str, LoadedContract],
        accept: frozenset[str] | None,
        grants: Mapping[str, str] | None = None,
        transcript: Transcript | None = None,
    ) -> None:
        """D-F / D-Q: every gate is answered before any mutation, or the apply refuses.

        ``accept`` None = no --accept-gates; the empty set = accept every
        operator-class gate; otherwise the named gate ids. An independent-agent
        gate is never answered by ``accept``: it consumes a pre-issued grant
        (grants.py, T-5135 S2), and that consumption runs LAST, so a refusal on
        any other gate never burns a grant.
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
    ) -> list[RunOutcome]:
        transcript = Transcript(new_ulid(), echo=self.echo)
        merged, contracts = self._plan(leg, scope, system, transcript, desired=desired, source_revs=source_revs)
        if merged.digest != plan_digest:
            raise EngineRefusal("plan-digest-mismatch", f"approved {plan_digest}, current plan is {merged.digest}; re-plan")
        self._check_gates(merged, contracts, accept_gates, grants, transcript)
        # Every node's plan.json is encoded under MAX_PLAN_BYTES BEFORE any claim or
        # run admission (T-5809): an oversized node refuses with nothing claimed or
        # admitted, for it or for any earlier node of the same plan.
        plan_bytes = [self._encode_plan_node(node, merged) for node in merged.nodes]
        outcomes = []
        for node, data in zip(merged.nodes, plan_bytes):
            outcomes.append(self._apply_node(node, merged, transcript, data))
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

    def _apply_node(self, node: PlanNode, merged: MergedPlan, transcript: Transcript, plan_bytes: bytes) -> RunOutcome:
        run_id = new_ulid()
        self._claim(node.component, run_id, node.scope, "apply")
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
            self._release(node.component, run_id)  # no record was admitted, so the claim must not outlive it
            raise
        try:
            run_t = None
            try:
                wal = handle.wal()
                run_t = transcript.bind(handle)
                run_t.emit(
                    "run.admitted",
                    component=node.component,
                    leg=node.leg,
                    scope=node.scope,
                    plan_digest=merged.digest,
                    contract_digest=node.contract_digest,
                    op_registry_digest=merged.op_registry_digest,
                    expected_runtime_subset={"digest": merged.expected_runtime_subset_digest, "manifest_revision": None},
                    evidence_scope=self.backend.evidence_scope,
                )
                handle.update(state=State.ACTIVE)
            except Exception as exc:  # noqa: BLE001 -- a pre-op failure leaves a recoverable run, never a wedge (T-5810)
                self._settle_pre_op_failure(handle, node, run_t, exc)
            for op in node.operations:
                try:
                    self._execute_op(handle, wal, node, merged, op, run_t)
                except Exception as exc:  # noqa: BLE001 -- any op failure halts the run (ORG-080 §10), claim retained
                    reason = f"{type(exc).__name__}: {exc}"
                    handle.update(state=State.HALTED, halted={"op_id": op.operation_id, "reason": reason, "at": now_iso()})
                    run_t.emit("run.halted", op_id=op.operation_id, reason=reason)
                    raise EngineRefusal("halted", f"run {run_id} HALTED at {op.operation_id}: {reason}") from exc
            handle.update(state=State.SATISFIED)
            self._release(node.component, run_id)
            run_t.emit("run.satisfied")
            return RunOutcome(node.component, run_id, State.SATISFIED.value)
        finally:
            handle.close()

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
        self, handle: RunHandle, wal: WalStore, node: PlanNode, merged: MergedPlan, op: Operation, t: Transcript
    ) -> None:
        backend = self.backend
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
            holder = self._claim_holder(tup.component)
            if holder is None:
                self._claim(tup.component, run_id, tup.scope, "rollback")
            elif holder.get("run_id") != run_id:
                raise EngineRefusal("claim-held", f"{tup.component} is held by run {holder.get('run_id')}")
            t = transcript.bind(handle)
            t.emit("rollback.started", from_state=state.value)
            handle.update(state=State.ROLLING_BACK, halted=None, rollback_incomplete=None)
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
                except Exception as exc:  # noqa: BLE001 -- an unreversed op is carried forward, never dropped (ORG-080 §10)
                    outstanding.append(op.operation_id)
                    t.emit("op.rollback.failed", op_id=op.operation_id, reason=f"{type(exc).__name__}: {exc}")
            if outstanding:
                handle.update(
                    state=State.ROLLBACK_INCOMPLETE,
                    rollback_incomplete={"failed_run_id": run_id, "outstanding_inverses": outstanding},
                )
                t.emit("rollback.incomplete", outstanding=outstanding)
                raise EngineRefusal("rollback-incomplete", f"run {run_id}: unreversed {outstanding}; the claim stays held (D-Y)")
            handle.update(state=State.ROLLED_BACK)
            self._release(tup.component, run_id)
            t.emit("rollback.complete")
            return RunOutcome(tup.component, run_id, State.ROLLED_BACK.value)

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
                handle.update(
                    state=State.ROLLBACK_INCOMPLETE,
                    rollback_incomplete={"failed_run_id": run_id, "outstanding_inverses": outstanding},
                )
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
        runs = self._runs()
        orphans = [t for store in self._stores() for t in store.orphan_tuples()]
        out = []
        for rid, row in sorted(self._component_rows(doc).items()):
            latest: dict[str, dict[str, Any]] = {}
            for tup, record in runs:
                if tup.component == rid:
                    latest[f"{tup.scope}/{tup.leg}"] = {"run_id": tup.run_id, "state": record["state"]}
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
