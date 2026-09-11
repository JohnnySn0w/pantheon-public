"""tools/organism_topology_audit.py — ORG-056 Unit F (T-3190).

Pure, read-only upkeep audit of the ORG-056 organism topology. Emits typed
candidates (stale / unowned / drifted / unsupported / uneconomic), measures the
topology's maintenance surface, and recommends a review cadence from observed
staleness and cost.

It NEVER installs standing work (no timer/cron), rewrites topology truth, or
performs live recovery or decommissioning — it measures and recommends. Review
obligations are bound through the ORG-061 substrate
(tickets/grand-project-reevaluation-obligations.yaml), not re-implemented here.

Design and falsifiers: tickets/plans/T-2416-implementation-plan.md (Unit F).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path

from tools import organism_topology as ot


class FindingKind(str, Enum):
    STALE = "stale"          # a review obligation is unarmed-in-practice or overdue
    UNOWNED = "unowned"      # a contract/binding lacks an owner
    DRIFTED = "drifted"      # generated index/schema stale, or a structural check issue
    UNSUPPORTED = "unsupported"  # a binding carries no current evidence for its claim
    UNECONOMIC = "uneconomic"    # maintenance surface unjustified by evidenced value


@dataclasses.dataclass(frozen=True)
class AuditFinding:
    kind: FindingKind
    subject_id: str
    detail: str


@dataclasses.dataclass(frozen=True)
class UpkeepCost:
    """The topology's maintenance surface — the count of records an audit must walk."""

    contracts: int
    bindings: int
    dependencies: int
    surfaces: int
    schemas: int
    obligations: int

    @property
    def maintenance_surface(self) -> int:
        return (
            self.contracts
            + self.bindings
            + self.dependencies
            + self.surfaces
            + self.schemas
            + self.obligations
        )


@dataclasses.dataclass(frozen=True)
class AuditObligation:
    """A read-only view of one ORG-061 review obligation the topology depends on."""

    obligation_id: str
    state: str
    last_evaluated: str | None
    next_check: str | None


@dataclasses.dataclass(frozen=True)
class AuditReport:
    findings: tuple[AuditFinding, ...]
    cost: UpkeepCost
    cadence: str

    def counts(self) -> Mapping[FindingKind, int]:
        out: dict[FindingKind, int] = {kind: 0 for kind in FindingKind}
        for finding in self.findings:
            out[finding.kind] += 1
        return out


def _instant(value: str) -> dt.datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return dt.datetime.fromisoformat(text).astimezone(dt.timezone.utc)


def audit_topology(
    topology: ot.OrganismTopology,
    *,
    structural_issues: Sequence[ot.TopologyIssue],
    obligations: Sequence[AuditObligation],
    now: dt.datetime,
) -> AuditReport:
    """Produce the typed audit over a loaded topology — pure and read-only.

    ``structural_issues`` is the output of :func:`organism_topology.check_organism_topology`
    (drift / dangling / false-live are already typed there); this audit lifts them
    to DRIFTED and adds the soft signals a structural check does not gate on:
    unowned contracts, evidence-unsupported bindings, unarmed/overdue review
    obligations, and an uneconomic maintenance surface.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    findings: list[AuditFinding] = []

    # DRIFTED — every structural issue is a drift the generated truth already rejects.
    for issue in structural_issues:
        findings.append(AuditFinding(FindingKind.DRIFTED, issue.record_id, f"{issue.code}: {issue.detail}"))

    # UNOWNED — a contract with no owner has no one to keep it true.
    for organ_id in sorted(topology.organs):
        if not topology.organs[organ_id].owners:
            findings.append(AuditFinding(FindingKind.UNOWNED, organ_id, "organ contract has no owner"))

    # UNSUPPORTED — a binding with no evidence backs its claim on nothing. (A live
    # binding without evidence is already rejected structurally; a partial one is
    # honestly evidence-pending, surfaced so the gap is visible, not hidden.)
    for binding in topology.bindings:
        if not binding.evidence_refs:
            findings.append(
                AuditFinding(
                    FindingKind.UNSUPPORTED,
                    binding.binding_id,
                    f"{binding.lifecycle.value} binding carries no evidence_refs",
                )
            )

    # STALE — a review obligation the topology depends on that is armed but never
    # evaluated, or whose next_check has passed, is a review cadence that is not
    # actually running.
    for obligation in obligations:
        if obligation.state == "armed" and obligation.last_evaluated is None:
            findings.append(
                AuditFinding(FindingKind.STALE, obligation.obligation_id, "armed but never evaluated")
            )
        elif obligation.next_check is not None and _instant(obligation.next_check) < now.astimezone(dt.timezone.utc):
            findings.append(
                AuditFinding(FindingKind.STALE, obligation.obligation_id, f"next_check {obligation.next_check} passed")
            )

    cost = UpkeepCost(
        contracts=len(topology.organs),
        bindings=len(topology.bindings),
        dependencies=len(topology.dependencies),
        surfaces=len(topology.surfaces),
        schemas=len(ot.generate_topology_schemas()),
        obligations=len(obligations),
    )

    # UNECONOMIC — if the topology carries more contract surface than it has any
    # evidence for, the upkeep is currently unjustified by evidenced value.
    supported_bindings = sum(1 for b in topology.bindings if b.evidence_refs)
    if cost.contracts > 0 and supported_bindings == 0:
        findings.append(
            AuditFinding(
                FindingKind.UNECONOMIC,
                topology.organism.organism_id,
                f"{cost.contracts} contracts / {cost.bindings} bindings carry zero current evidence",
            )
        )

    cadence = _recommend_cadence(findings, cost, obligations)
    return AuditReport(findings=tuple(findings), cost=cost, cadence=cadence)


def _recommend_cadence(
    findings: Sequence[AuditFinding],
    cost: UpkeepCost,
    obligations: Sequence[AuditObligation],
) -> str:
    """Recommend a review cadence from observed staleness and cost — no install.

    Cadence is recommended, never actuated: the ORG-061 obligations are the
    event-armed review triggers; a standing timer is only warranted once the
    topology carries live dependency edges and evidence to keep fresh.
    """
    drifted = any(f.kind is FindingKind.DRIFTED for f in findings)
    if drifted:
        return "on-change: structural drift present — regenerate and re-audit before any other cadence."
    if cost.dependencies == 0 and cost.surfaces == 0:
        return (
            "event-driven only. The topology carries no live dependency edges or surfaces to keep "
            "fresh, so a standing timer is not yet economic. Re-audit on topology change (a new "
            "binding/edge/surface, or a binding gaining/losing evidence) and let the ORG-061 "
            f"obligations ({len(obligations)} armed) drive review. Revisit for a periodic cadence "
            "once evidence-bearing bindings and dependency edges exist."
        )
    return "periodic: quarterly, or on-change — whichever comes first — now that evidence-bearing edges exist."


def load_audit_obligations(repo_root: Path, *, prefix: str = "ORG-056") -> tuple[AuditObligation, ...]:
    """Read the ORG-061 obligations the topology depends on (read-only)."""
    import yaml

    path = repo_root / "tickets" / "grand-project-reevaluation-obligations.yaml"
    if not path.is_file():
        return ()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    result: list[AuditObligation] = []
    for item in data.get("obligations", []):
        oid = str(item.get("obligation_id", ""))
        if not oid.startswith(prefix):
            continue
        result.append(
            AuditObligation(
                obligation_id=oid,
                state=str(item.get("state", "")),
                last_evaluated=item.get("last_evaluated"),
                next_check=item.get("next_check"),
            )
        )
    return tuple(result)


def audit_repo(repo_root: Path, *, now: dt.datetime) -> AuditReport:
    """Load the live topology + obligations and audit them — read-only end to end."""
    topology = ot.load_organism_topology(repo_root)
    issues = ot.check_organism_topology(repo_root, now=now)
    obligations = load_audit_obligations(repo_root)
    return audit_topology(topology, structural_issues=issues, obligations=obligations, now=now)
