"""Hand-sized registry documents and tmp_path repo stand-ins for the registry tests."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from tools.germinate.registry import schema as S

TICKET = "T-1234"
DIGEST = "sha256:" + "a" * 64
REVISION = "b" * 40


def leg(status: str = "covered", **extra) -> dict:
    cell = {"status": status}
    if status == "covered":
        cell["evidence"] = "tools/x.py"
    elif status in ("partial", "missing"):
        cell.update(gap="something is missing", ticket=TICKET)
    else:
        cell["waiver"] = "named rationale"
    cell.update(extra)
    return cell


def health_covered() -> dict:
    return {
        "status": "covered",
        "evidence": {"emitter": "unit x", "invariant_binding": "list@sha256:1", "collector_alert": "receipt r1"},
    }


def all_legs(status: str = "covered") -> dict:
    legs = {name: leg(status) for name in S.LEGS}
    if status == "covered":
        legs["health"] = health_covered()
    return legs


def component(**overrides) -> dict:
    row = {
        "kind": "component",
        "id": "comp-a",
        "title": "Component A",
        "steward": "orchestrator",
        "contract": {
            "repo": "pantheon-config",
            "module": "tools/germinate/components/comp-a.yaml",
            "digest": "unpinned",
            "unpinned_reason": "no module yet",
        },
        "adr": ["ORG-081"],
        "contract_adoption": "not_adopted",
        "legacy_coverage": {"install": leg("covered"), "remove": leg("missing")},
    }
    row.update(overrides)
    return row


def pinned_contract(**overrides) -> dict:
    contract = {"repo": "pantheon-config", "module": "tools/germinate/components/comp-a.yaml", "digest": DIGEST}
    contract.update(overrides)
    return contract


def adopted(**overrides) -> dict:
    row = component(
        contract=pinned_contract(),
        contract_adoption="adopted",
        adoption_evidence={f: f"ref-{f}" for f in S.EVIDENCE_FIELDS},
        legs=all_legs(),
    )
    del row["legacy_coverage"]
    row.update(overrides)
    return row


def partial(**overrides) -> dict:
    evidence = {f: f"ref-{f}" for f in S.EVIDENCE_FIELDS if f != "gates"}
    row = component(
        contract=pinned_contract(),
        contract_adoption="partial",
        adoption_evidence=evidence,
        adoption_gaps={
            "adoption_evidence.gates": {"gap": "approval registry not wired", "ticket": TICKET},
            "sandbox": {"gap": "profile not enforced in CI", "ticket": TICKET},
        },
        legs=all_legs(),
    )
    del row["legacy_coverage"]
    row.update(overrides)
    return row


def external(**overrides) -> dict:
    row = {
        "kind": "external-plane",
        "id": "plane-a",
        "title": "Plane A",
        "steward": "recovery",
        "adr": ["ORG-076"],
        "tracking": {"build": TICKET},
        "relationship": "engine installs emitters only",
    }
    row.update(overrides)
    return row


def doc(*rows) -> dict:
    return {"schema": S.SCHEMA_ID, "systems": [copy.deepcopy(r) for r in (rows or (component(), external()))]}


def write_yaml(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def make_repo(root: Path, registry: dict | None = None, *, tickets=(("inbox", TICKET),), adrs=("ORG-081", "ORG-076")) -> Path:
    """A minimal tmp checkout: registry, repos.yaml, tickets and ADR stubs."""
    write_yaml(root / "registry" / "systems.yaml", registry if registry is not None else doc())
    write_yaml(
        root / "repos.yaml",
        {"always_available": [{"repo": "pantheon-config"}], "repos": [{"name": "pantheon-config"}, {"name": "echo-prismatic"}]},
    )
    for state, ticket in tickets:
        (root / "tickets" / state).mkdir(parents=True, exist_ok=True)
        (root / "tickets" / state / f"{ticket}.md").write_text("---\nid: x\n---\n", encoding="utf-8")
    (root / "architecture" / "adr").mkdir(parents=True, exist_ok=True)
    for adr in adrs:
        (root / "architecture" / "adr" / f"{adr}-stub.md").write_text("---\nid: x\n---\n", encoding="utf-8")
    return root
