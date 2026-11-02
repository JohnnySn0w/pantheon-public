"""Read-only lineage-graph queries over the ticket corpus (T-3332 part (a)).

Answers two questions for a seat or for the operator:

  up    "what does T-X serve?"   walk from a ticket up to its terminus
  down  "what serves Y?"         walk from a ticket / goal / root back down
  roots "where do walks end?"    per-class counts (UNROOTED is the honesty metric)

Edge model. The DEFAULT up-walk follows the purpose spine plus filed-from
lineage: `nearest_intent_ref: ticket:T-X` (ORG-054 typed purpose),
`parent_ticket` (ORG-021 filed-from), and the ORG-065 obligation edges
`implements` / `verifies` / `ratifies` (to a ticket or an ADR obligation).
`depends_on`, `supersedes` and `informs` are informational and opt-in via
`--edges`: a prerequisite, a replacement and an advisory note are not
statements of purpose. `serves` is the node's free-text terminal label and is
NEVER parsed into edges (no fabricated lineage, the T-3331 rule).

Terminus classes (`classify_terminus`, the one swappable function; orchestrator
ratified the four-class model for T-3332 (b) on 2026-10-27):

  goal              a `goal:` ref, or a serves string naming a goals.md key
  operational-root  the ticket carries the `operational-root` label (T-5677)
  maintenance       tools.ticket._is_hygiene: a `maintenance:` ref, the
                    goal-neutral-maintenance serves marker, or a hygiene label
  UNROOTED          everything else (free text, ADR terminus, orphan, ...)

A ticket's class is the best class over its own fields and every node on
every up-path, in the order goal > operational-root > maintenance > UNROOTED.

Strictly read-only: it never writes a file, runs git, or commits.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import AbstractSet, Iterable, Mapping

from tools.ticket_purpose import (
    GOAL_NEUTRAL_SERVES,
    HYGIENE_LABELS,
    OPERATIONAL_ROOT_LABEL,
    PurposeError,
    PurposeRefKind,
    TICKET_ID_RE,
    parse_intent_ref,
)

DEFAULT_UP_FIELDS: tuple[str, ...] = (
    "nearest_intent_ref", "parent_ticket", "implements", "verifies", "ratifies",
)
OPTIONAL_UP_FIELDS: tuple[str, ...] = ("depends_on", "supersedes", "informs")
ALL_UP_FIELDS: tuple[str, ...] = DEFAULT_UP_FIELDS + OPTIONAL_UP_FIELDS
_LIST_FIELDS = ("implements", "verifies", "ratifies", "depends_on", "supersedes", "informs")

LEGACY_AD_HOC_SERVES = "operator-ad-hoc"
OPEN_STATES = ("inbox", "active")


class Terminus(str, Enum):
    GOAL = "goal"
    OPERATIONAL_ROOT = "operational-root"
    MAINTENANCE = "maintenance"
    UNROOTED = "UNROOTED"


PRECEDENCE: tuple[Terminus, ...] = (
    Terminus.GOAL, Terminus.OPERATIONAL_ROOT, Terminus.MAINTENANCE, Terminus.UNROOTED,
)
_RANK = {cls: i for i, cls in enumerate(PRECEDENCE)}


@dataclass(frozen=True)
class Classification:
    """`cls` is the class; `code` + `text` are diagnostic only (never change cls)."""

    cls: Terminus
    code: str
    text: str


# Goal keys: the canonical source is tools/goal_registry.py (T-5679), which
# parses the `<!-- goal-key: <key> -->` markers in goals.md. Callers pass the
# resulting set into build_graph; this module never reads goals.md itself.


# ---------------------------------------------------------------------------
# The terminus classifier (the ONE swappable function).
# ---------------------------------------------------------------------------


def _labels(fields: Mapping[str, object]) -> set[str]:
    raw = fields.get("labels") or []
    if not isinstance(raw, list):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}


def _str(fields: Mapping[str, object], key: str) -> str:
    value = fields.get(key)
    return value.strip() if isinstance(value, str) else ""


def classify_terminus(
    fields: Mapping[str, object], goal_keys: AbstractSet[str]
) -> Classification:
    """Classify ONE ticket's own frontmatter (no walk). Checks run in
    precedence order, so the first hit is the ticket's own best class."""
    # Lazy import: tools.ticket imports this module lazily from cmd_lineage.
    from tools.ticket import _is_hygiene

    labels = _labels(fields)
    nir = _str(fields, "nearest_intent_ref")
    serves = _str(fields, "serves")
    ref = None
    ref_error = None
    if nir:
        try:
            ref = parse_intent_ref(nir)
        except PurposeError as exc:
            ref_error = str(exc)

    if ref is not None and ref.kind is PurposeRefKind.GOAL:
        if ref.value in goal_keys:
            return Classification(Terminus.GOAL, "goal-ref", nir)
        return Classification(Terminus.GOAL, "goal-ref-unregistered", f"{nir} (not a goals.md key)")
    serves_key = serves[len("goal:"):] if serves.startswith("goal:") else serves
    if serves_key and serves_key in goal_keys:
        return Classification(Terminus.GOAL, "goal-serves", f"serves {serves}")
    if OPERATIONAL_ROOT_LABEL in labels:
        return Classification(Terminus.OPERATIONAL_ROOT, "operational-root-label", OPERATIONAL_ROOT_LABEL)
    if _is_hygiene(dict(fields)):
        if ref is not None and ref.kind is PurposeRefKind.MAINTENANCE:
            return Classification(Terminus.MAINTENANCE, "maintenance-ref", nir)
        if serves == GOAL_NEUTRAL_SERVES:
            return Classification(Terminus.MAINTENANCE, "goal-neutral-serves", f"serves {serves}")
        hit = sorted(labels & HYGIENE_LABELS)
        return Classification(Terminus.MAINTENANCE, "hygiene-label", "label " + ",".join(hit))

    if ref_error is not None:
        return Classification(Terminus.UNROOTED, "malformed-ref", f"{nir!r}: {ref_error}")
    if ref is not None and ref.kind is PurposeRefKind.PROJECT:
        return Classification(Terminus.UNROOTED, "project-ref", f"{nir} (project-to-goal not walked)")
    if serves == LEGACY_AD_HOC_SERVES:
        return Classification(Terminus.UNROOTED, "legacy-operator-ad-hoc", "serves operator-ad-hoc (legacy; use ticket:T-5677)")
    if serves:
        return Classification(Terminus.UNROOTED, "free-text-serves", f"serves {serves}")
    if _str(fields, "orphan_reason"):
        return Classification(Terminus.UNROOTED, "declared-orphan", "orphan_reason set")
    return Classification(Terminus.UNROOTED, "no-purpose", "no serves, intent ref or root label")


_MISSING = Classification(Terminus.UNROOTED, "missing-ticket", "ticket not in corpus")


def _adr_terminus(node: str) -> Classification:
    return Classification(Terminus.UNROOTED, "adr-terminus", f"ADR obligation {node} (no ADR-to-goal edge)")


# ---------------------------------------------------------------------------
# Graph.
# ---------------------------------------------------------------------------


@dataclass
class Node:
    tid: str
    state: str
    fields: dict
    title: str = ""


@dataclass
class LineageGraph:
    nodes: dict[str, Node]
    up: dict[str, list[tuple[str, str]]]      # tid -> [(field, target)]
    down: dict[str, list[tuple[str, str]]]    # target -> [(field, tid)]
    goal_keys: frozenset[str]
    edge_fields: tuple[str, ...]
    unparseable: list[str] = field(default_factory=list)
    malformed: list[dict] = field(default_factory=list)
    _own: dict[str, Classification] = field(default_factory=dict)

    def is_ticket(self, node: str) -> bool:
        return bool(TICKET_ID_RE.fullmatch(node))

    def own(self, node: str) -> Classification:
        """The node's own terminus classification (memoized)."""
        if node in self._own:
            return self._own[node]
        if node in self.nodes:
            cls = classify_terminus(self.nodes[node].fields, self.goal_keys)
        elif self.is_ticket(node):
            cls = _MISSING
        else:
            cls = _adr_terminus(node)
        self._own[node] = cls
        return cls

    def superseded_by(self, tid: str) -> list[str]:
        """Tickets whose `supersedes` names `tid` (always indexed by build_graph)."""
        return sorted(src for f, src in self.down.get(tid, []) if f == "supersedes")


def _title_from_body(body_head: str) -> str:
    for line in body_head.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _edges_for(fields: Mapping[str, object], edge_fields: Iterable[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Outbound (field, target) edges + malformed-value notes for one ticket."""
    from tools.ticket_lint import edge_node_id

    out: list[tuple[str, str]] = []
    bad: list[str] = []
    for name in edge_fields:
        if name == "nearest_intent_ref":
            nir = _str(fields, name)
            if not nir:
                continue
            try:
                ref = parse_intent_ref(nir)
            except PurposeError as exc:
                bad.append(f"nearest_intent_ref {nir!r}: {exc}")
                continue
            if ref.kind is PurposeRefKind.TICKET:
                out.append((name, ref.value))
        elif name == "parent_ticket":
            parent = _str(fields, name)
            if not parent:
                continue
            if TICKET_ID_RE.fullmatch(parent):
                out.append((name, parent))
            else:
                bad.append(f"parent_ticket {parent!r}: not a T-NNNN id")
        else:
            raw = fields.get(name) or []
            if isinstance(raw, str):
                raw = [raw]
            for item in raw:
                _kind, ident = edge_node_id(str(item).strip())
                out.append((name, ident))
    return out, bad


def build_graph(
    tickets_root: Path,
    goal_keys: AbstractSet[str],
    edge_fields: tuple[str, ...] = DEFAULT_UP_FIELDS,
) -> LineageGraph:
    """One corpus pass (ticket_lint.iter_ticket_frontmatter), then in-memory indices."""
    from tools.ticket_lint import iter_ticket_frontmatter

    skipped: list[Path] = []
    nodes: dict[str, Node] = {}
    up: dict[str, list[tuple[str, str]]] = {}
    down: dict[str, list[tuple[str, str]]] = {}
    malformed: list[dict] = []
    # Reverse supersedes is always indexed so done/superseded annotation works
    # even when supersedes is not a walked edge.
    index_fields = tuple(dict.fromkeys(edge_fields + ("supersedes",)))
    for state, tid, fields in iter_ticket_frontmatter(tickets_root, skipped):
        nodes[tid] = Node(tid=tid, state=state, fields=fields)
        edges, bad = _edges_for(fields, index_fields)
        for note in bad:
            malformed.append({"ticket": tid, "detail": note})
        walked = [(f, t) for f, t in edges if f in edge_fields]
        up[tid] = walked
        for f, t in edges:
            down.setdefault(t, []).append((f, tid))
    return LineageGraph(
        nodes=nodes, up=up, down=down, goal_keys=frozenset(goal_keys),
        edge_fields=tuple(edge_fields), unparseable=[str(p) for p in skipped],
        malformed=malformed,
    )


def load_titles(graph: LineageGraph, tickets_root: Path, tids: Iterable[str]) -> None:
    """Fill titles for the nodes a query will print (cheap: only those files)."""
    for tid in tids:
        node = graph.nodes.get(tid)
        if node is None or node.title:
            continue
        path = tickets_root / node.state / f"{tid}.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        end = text.find("\n---\n", 4)
        node.title = _title_from_body(text[end + 5:] if end >= 0 else "")


# ---------------------------------------------------------------------------
# Walks.
# ---------------------------------------------------------------------------


def _down_edges(graph: LineageGraph, node: str) -> list[tuple[str, str]]:
    return sorted(
        ((f, src) for f, src in graph.down.get(node, []) if f in graph.edge_fields),
        key=lambda e: (e[1], e[0]),
    )


def walk(graph: LineageGraph, seeds: list[str], direction: str, depth: int | None = None) -> dict:
    """Traverse from `seeds` (direction 'up' or 'down'). Returns a graph-shaped
    result: visited nodes, traversed edges (always child->parent orientation:
    src serves dst), and findings (cycles on the current path, missing nodes)."""
    visited: list[str] = []
    seen: set[str] = set()
    edges: list[dict] = []
    edge_seen: set[tuple[str, str, str]] = set()
    cycles: list[list[str]] = []
    missing: set[str] = set()

    def neighbours(node: str) -> list[tuple[str, str]]:
        if direction == "up":
            return graph.up.get(node, []) if node in graph.nodes else []
        return _down_edges(graph, node)

    # Iterative pre-order DFS (r1 LOW-3: no recursion, so chain depth is
    # bounded only by memory). An ("exit", node) marker pops the node off the
    # current path once its whole subtree is done, so the on-path test for a
    # cycle sees exactly the recursive walk's ancestors.
    expanded: set[str] = set()
    for seed in seeds:
        path: list[str] = []
        on_path: set[str] = set()
        stack: list[tuple] = [("enter", seed, 0)]
        while stack:
            item = stack.pop()
            if item[0] == "exit":
                on_path.discard(path.pop())
                continue
            _op, node, level = item
            if node in expanded:
                continue
            expanded.add(node)
            if node not in seen:
                seen.add(node)
                visited.append(node)
            if graph.is_ticket(node) and node not in graph.nodes:
                missing.add(node)
                continue
            if depth is not None and level >= depth:
                continue
            path.append(node)
            on_path.add(node)
            stack.append(("exit",))
            children: list[str] = []
            for f, nxt in neighbours(node):
                src, dst = (node, nxt) if direction == "up" else (nxt, node)
                key = (src, dst, f)
                if key not in edge_seen:
                    edge_seen.add(key)
                    edges.append({"src": src, "dst": dst, "field": f})
                if nxt in on_path:
                    cycles.append(path[path.index(nxt):] + [nxt])
                    continue
                if nxt in seen:
                    continue
                children.append(nxt)
            for nxt in reversed(children):
                stack.append(("enter", nxt, level + 1))
    return {"visited": visited, "edges": edges, "cycles": cycles, "missing": sorted(missing)}


def ticket_terminus(graph: LineageGraph, tid: str) -> tuple[Classification, str]:
    """Best class over `tid`'s own fields and every node on every up-path.
    Returns (classification, node that supplied it). For UNROOTED, the reason
    is the nearest leaf's (BFS order), so the breakdown names where it ends."""
    best: Classification | None = None
    best_node = tid
    leaf: tuple[Classification, str] | None = None
    seen = {tid}
    queue: deque[str] = deque([tid])
    while queue:
        node = queue.popleft()
        own = graph.own(node)
        if own.cls is not Terminus.UNROOTED and (best is None or _RANK[own.cls] < _RANK[best.cls]):
            best, best_node = own, node
            if own.cls is Terminus.GOAL:
                return best, best_node
        outs = graph.up.get(node, []) if node in graph.nodes else []
        if not outs and leaf is None:
            leaf = (own, node)
        for _f, nxt in outs:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    if best is not None:
        return best, best_node
    if leaf is None:  # every path loops: no leaf at all
        return Classification(Terminus.UNROOTED, "cycle", "every up-path loops"), tid
    return leaf


def roots_summary(graph: LineageGraph, states: tuple[str, ...]) -> dict:
    counts = {cls.value: 0 for cls in PRECEDENCE}
    detail: dict[str, dict[str, int]] = {cls.value: {} for cls in PRECEDENCE}
    members: dict[str, list[dict]] = {cls.value: [] for cls in PRECEDENCE}
    total = 0
    for tid in sorted(graph.nodes):
        node = graph.nodes[tid]
        if node.state not in states:
            continue
        total += 1
        cls, via = ticket_terminus(graph, tid)
        counts[cls.cls.value] += 1
        bucket = detail[cls.cls.value]
        bucket[cls.code] = bucket.get(cls.code, 0) + 1
        members[cls.cls.value].append({"ticket": tid, "state": node.state, "code": cls.code, "via": via})
    return {"states": list(states), "total": total, "counts": counts, "detail": detail, "members": members}


# ---------------------------------------------------------------------------
# Seeds for `down`.
# ---------------------------------------------------------------------------


class SeedError(ValueError):
    """The query names no node this corpus knows."""


def resolve_down_seeds(graph: LineageGraph, spec: str) -> list[str]:
    spec = spec.strip()
    if TICKET_ID_RE.fullmatch(spec):
        if spec not in graph.nodes and spec not in graph.down:
            raise SeedError(f"unknown ticket {spec}")
        return [spec]
    if spec.startswith("goal:"):
        key = spec[len("goal:"):]
        seeds = [t for t in sorted(graph.nodes) if graph.own(t).cls is Terminus.GOAL
                 and (_str(graph.nodes[t].fields, "nearest_intent_ref") == spec
                      or _str(graph.nodes[t].fields, "serves") in (key, spec))]
        if not seeds and key not in graph.goal_keys:
            raise SeedError(f"unknown goal {spec} (goals.md keys: {', '.join(sorted(graph.goal_keys)) or 'none'})")
        return seeds
    if spec == "maintenance" or spec.startswith("maintenance:"):
        seeds = [t for t in sorted(graph.nodes) if graph.own(t).cls is Terminus.MAINTENANCE
                 and (spec == "maintenance" or _str(graph.nodes[t].fields, "nearest_intent_ref") == spec)]
        if not seeds:
            raise SeedError(f"no ticket terminates at {spec}")
        return seeds
    if spec == OPERATIONAL_ROOT_LABEL:
        seeds = [t for t in sorted(graph.nodes) if OPERATIONAL_ROOT_LABEL in _labels(graph.nodes[t].fields)]
        if not seeds:
            raise SeedError(f"no ticket carries the {OPERATIONAL_ROOT_LABEL} label")
        return seeds
    if re.fullmatch(r"(?:ADR|ORG)-\d+(?:#E\d+(?:\.C\d+)?)?", spec):
        seeds = sorted(t for t in graph.down if t == spec or t.startswith(spec + "#"))
        if not seeds:
            raise SeedError(f"no ticket points at {spec}")
        return seeds
    raise SeedError(
        f"cannot read {spec!r}: use T-NNNN, goal:KEY, maintenance[:ASSET], "
        f"{OPERATIONAL_ROOT_LABEL}, or ORG-NNN[#Ek]"
    )


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def node_record(graph: LineageGraph, node: str) -> dict:
    own = graph.own(node)
    rec: dict = {"id": node, "class": own.cls.value, "code": own.code, "detail": own.text}
    n = graph.nodes.get(node)
    if n is None:
        rec["kind"] = "ticket" if graph.is_ticket(node) else "obligation"
        rec["missing"] = graph.is_ticket(node)
        return rec
    rec.update({
        "kind": "ticket", "state": n.state, "title": n.title,
        "serves": _str(n.fields, "serves") or None,
        "nearest_intent_ref": _str(n.fields, "nearest_intent_ref") or None,
        "labels": sorted(_labels(n.fields)),
        "substate": _str(n.fields, "substate") or None,
    })
    sup = graph.superseded_by(node)
    if sup:
        rec["superseded_by"] = sup
    return rec


def _node_line(rec: dict) -> str:
    if rec["kind"] != "ticket":
        return f"{rec['id']} [ADR obligation]"
    if rec.get("missing"):
        return f"{rec['id']} [MISSING: not in corpus]"
    tags = [rec["state"]]
    if rec.get("substate"):
        tags.append(rec["substate"])
    if rec.get("superseded_by"):
        tags.append("superseded by " + ",".join(rec["superseded_by"]))
    return f"{rec['id']} [{'; '.join(tags)}] {rec.get('title') or ''}".rstrip()


def render_tree(graph: LineageGraph, seeds: list[str], direction: str, depth: int | None) -> list[str]:
    lines: list[str] = []
    printed: set[str] = set()

    def neighbours(node: str) -> list[tuple[str, str]]:
        if direction == "up":
            return graph.up.get(node, []) if node in graph.nodes else []
        return _down_edges(graph, node)

    arrow = "->" if direction == "up" else "<-"

    # Iterative (r1 LOW-3): a ("node", ...) item prints a node's body and
    # schedules its out-edges; an ("edge", ...) item prints one edge line and,
    # unless it closes a cycle or was printed already, schedules the target's
    # body; ("exit",) pops the node off the current path after its subtree.
    for seed in seeds:
        lines.append(_node_line(node_record(graph, seed)))
        path: list[str] = []
        on_path: set[str] = set()
        stack: list[tuple] = [("node", seed, "", 0)]
        while stack:
            item = stack.pop()
            kind = item[0]
            if kind == "exit":
                on_path.discard(path.pop())
                continue
            if kind == "edge":
                _k, f, nxt, prefix, level = item
                head = f"{prefix}  {arrow} {f} {arrow} "
                if nxt in on_path:
                    lines.append(f"{head}{nxt}  CYCLE (back to an ancestor on this path)")
                elif nxt in printed:
                    lines.append(f"{head}{nxt}  (seen above)")
                else:
                    lines.append(head + _node_line(node_record(graph, nxt)))
                    stack.append(("node", nxt, prefix + "  ", level))
                continue
            _k, node, prefix, level = item
            rec = node_record(graph, node)
            pad = prefix + "    "
            if rec.get("serves"):
                lines.append(f"{pad}serves: {rec['serves']}")
            outs = neighbours(node)
            # Up: an UNROOTED node with parents is not a terminus, so its class
            # line is noise. Down: every node's own class is the answer (r1 LOW-4).
            if rec["class"] != Terminus.UNROOTED.value or not outs or direction == "down":
                lines.append(f"{pad}=> {rec['class']}: {rec['detail']}")
            printed.add(node)
            if depth is not None and level >= depth:
                if outs:
                    lines.append(f"{pad}... ({len(outs)} more, --depth reached)")
                continue
            path.append(node)
            on_path.add(node)
            stack.append(("exit",))
            for f, nxt in reversed(outs):
                stack.append(("edge", f, nxt, prefix, level + 1))
    return lines
