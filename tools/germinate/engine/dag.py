"""Component DAG ordering and the merged plan (ORG-080 §2 "Components never call each other").

The engine alone orders components: ``depends_on`` edges, deterministic
topological order (ties broken by id), reversed for ``remove``. Each plan node
is one component's leg; the merged plan's digest binds the ops, every contract
digest and the op-registry digest, and ``apply`` executes only that digest.
"""

from __future__ import annotations

import hashlib
import heapq
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from tools.germinate.engine.operation import Operation
from tools.germinate.engine.planner import PlanError


@dataclass(frozen=True)
class PlanNode:
    component: str
    leg: str
    scope: str
    contract_digest: str
    operations: tuple[Operation, ...]

    def to_json_obj(self) -> dict[str, object]:
        return {
            "component": self.component,
            "contract_digest": self.contract_digest,
            "leg": self.leg,
            "operations": [op.to_json_obj() for op in self.operations],
            "scope": self.scope,
        }


@dataclass(frozen=True)
class MergedPlan:
    leg: str
    scope: str
    nodes: tuple[PlanNode, ...]
    op_registry_digest: str
    expected_runtime_subset_digest: str
    digest: str

    def node(self, component: str) -> PlanNode:
        for node in self.nodes:
            if node.component == component:
                return node
        raise KeyError(component)


def order_components(depends_on: Mapping[str, Sequence[str]], selected: Sequence[str], *, leg: str) -> list[str]:
    """Topological order of ``selected`` over the edges among them; cycles refuse."""

    chosen = set(selected)
    indegree = {cid: 0 for cid in chosen}
    dependents: dict[str, list[str]] = {cid: [] for cid in chosen}
    for cid in chosen:
        for dep in depends_on.get(cid, ()):
            if dep in chosen:
                indegree[cid] += 1
                dependents[dep].append(cid)
    ready = [cid for cid, deg in indegree.items() if deg == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        cid = heapq.heappop(ready)
        order.append(cid)
        for nxt in dependents[cid]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(ready, nxt)
    if len(order) != len(chosen):
        raise PlanError(f"depends_on cycle among {sorted(chosen - set(order))}")
    return list(reversed(order)) if leg == "remove" else order


def plan_digest(leg: str, scope: str, nodes: Sequence[PlanNode], op_registry_digest: str) -> str:
    payload = {
        "leg": leg,
        "nodes": [node.to_json_obj() for node in nodes],
        "op_registry_digest": op_registry_digest,
        "scope": scope,
    }
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()
