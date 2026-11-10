"""host-substrate component planner (ORG-080 §2 / §Build plan (c); T-5084, tranche GE).

Data-only. Converges the observed substrate (``request["observed"]["host-substrate"]``,
which the executor-side handler produced) toward the manifest
(``host-substrate.manifest.yaml``, read, never written) through
``tools.germinate.host_substrate.families.resolve``, the same resolution the
root helper re-runs at apply. A subject already at its desired value yields no
op (verify-not-mutate), so an identical rerun plans nothing (ORG-052 gate 35).

Every op is privilege root, ``write-ahead-inverse-durable`` (ORG-080 §2: the
host-substrate ops take the write-ahead path), and carries only manifest ids.
The update leg plans ``execution_scope: staged-cutover`` (D-H / A-3: a
privileged component auto-stages; cutover is a supervised install/configure run).

SEAT FRAGMENTS (T-5827, T-5091 U2; T-5829 ruling (1) Option A, orchestrator + Recovery D15).
The manifest is the in-repo base plus the per-seat fragments that harness-manager's
pure resolver (``harness_manager_root``) derives from this checkout's committed
instance.yaml for every ``root_half: managed`` seat, merged add-only (a collision
refuses), seat-scoped and order-independent. With no managed seat the manifest is
the base, byte for byte, and no seat observation is asked for. When fragments
exist, every op's provenance records ``base_manifest_sha256`` and one
``fragment_sha256:<role>`` per fragment next to the merged ``manifest_sha256``
(provenance is a flat string mapping); the handler refuses an
unrecorded, absent or changed fragment at apply (ruling (1)(f)).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from tools.germinate.components import harness_manager_root as HR
from tools.germinate.host_substrate import families as F

HANDLER = "host-substrate"
MANIFEST = Path(__file__).with_name("host-substrate.manifest.yaml")
REPO_ROOT = Path(__file__).resolve().parents[3]
STAGED = "staged:"

SUDOERS_GATE = "sudoers-self-lockout"
RETIRE_GATE = "substrate-retire"
RETIRE_FAMILIES = frozenset({"account.retire", "mailbox_dir.retire", "linger.disable"})


class PlanRefused(RuntimeError):
    """The manifest or the observation is refused."""


def _manifest(request=None):
    """The merged manifest and the provenance additions (empty: no seat fragment)."""

    try:
        base_doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
        base = F.load_manifest(base_doc, repo_root=REPO_ROOT)
    except (OSError, yaml.YAMLError, F.ManifestError) as exc:
        raise PlanRefused(f"host-substrate manifest refused: {exc}") from None
    try:
        seats = HR.seat_merge(base_doc, HR.load_instance(REPO_ROOT / "instance.yaml"),
                              lambda: _observed(request or {}), plan_time=True)
    except HR.IdentityRefused as exc:
        raise PlanRefused(f"seat identity refused: {exc}") from None
    if not seats.fragment_sha256:
        return base, {}
    try:
        merged = F.load_manifest(seats.doc, repo_root=REPO_ROOT)
    except F.ManifestError as exc:
        raise PlanRefused(f"merged host-substrate manifest refused: {exc}") from None
    return merged, {"base_manifest_sha256": base.sha256, **HR.provenance_fields(seats.fragment_sha256)}


def _observed(request):
    observed = (request.get("observed") or {}).get(HANDLER)
    if not isinstance(observed, dict) or not isinstance(observed.get("subjects"), dict):
        raise PlanRefused(f"request carries no {HANDLER} observation")
    return observed["subjects"]


def converge_steps(m):
    """The forward converge, in dependency order."""

    steps = [("group.ensure", {"group": g}) for g in sorted(m.section("groups"))]
    accounts = m.section("accounts")
    steps += [("account.ensure", {"account": a}) for a in sorted(accounts)]
    for a in sorted(accounts):
        steps += [("group.member", {"group": g, "account": a, "state": "present"}) for g in sorted(accounts[a].get("groups") or [])]
        if accounts[a].get("linger"):
            steps.append(("linger.enable", {"account": a}))
    steps += [("mailbox_dir.ensure", {"mailbox": mb}) for mb in sorted(m.section("mailbox_dirs"))]
    for p in sorted(m.section("paths")):
        steps.append(("owner_mode.set", {"path": p}))
        if m.section("paths")[p].get("acl"):
            steps.append(("acl.converge", {"path": p}))
    units = m.section("units")
    steps += [("unit.system.install", {"unit": u, "state": "present", "sha256": units[u]["sha256"]}) for u in sorted(units)]
    steps += [("unit.system.enable", {"unit": u, "state": "enabled"}) for u in sorted(units) if units[u].get("enable")]
    pols = m.section("dbus_policies")
    steps += [("dbus_policy.install", {"policy": p, "sha256": pols[p]["sha256"]}) for p in sorted(pols)]
    tmpls = m.section("sudoers_templates")
    steps += [("sudoers_fragment.ensure", {"template": t, "sha256": F.sha256_text(F.sudoers_text(tmpls[t]))}) for t in sorted(tmpls)]
    return steps


def retire_steps(m):
    """Decommission: retire, never delete (ORG-052 gate 34; ORG-080 D-E)."""

    steps = [("sudoers_fragment.retire", {"template": t}) for t in sorted(m.section("sudoers_templates"))]
    steps += [("dbus_policy.retire", {"policy": p}) for p in sorted(m.section("dbus_policies"))]
    units = m.section("units")
    steps += [("unit.system.enable", {"unit": u, "state": "disabled"}) for u in sorted(units) if units[u].get("enable")]
    steps += [("unit.system.install", {"unit": u, "state": "absent", "sha256": units[u]["sha256"]}) for u in sorted(units)]
    accounts = m.section("accounts")
    for a in sorted(accounts):
        if accounts[a].get("linger"):
            steps.append(("linger.disable", {"account": a}))
        steps += [("group.member", {"group": g, "account": a, "state": "absent"}) for g in sorted(accounts[a].get("groups") or [])]
    steps += [("mailbox_dir.retire", {"mailbox": mb}) for mb in sorted(m.section("mailbox_dirs"))]
    steps += [("account.retire", {"account": a}) for a in sorted(accounts)]
    return steps


def _op(family, params, r, current, m, *, staged, seat_provenance=None):
    subject = (STAGED if staged else "") + r.subject
    tag = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:10]
    op = {
        "operation_id": f"{family.replace('.', '-')}-{tag}",
        "layer": 0,
        "kind": family,
        "target": subject,
        "precondition": {"observed_sha256": F.sha256_text(F.canonical(current))},
        "desired": dict(params),
        "inverse": {"restore": "preimage"},
        "privilege": "root",
        "owner": "root",
        "provenance": {"planner": "host-substrate", "manifest_sha256": m.sha256, **(seat_provenance or {})},
        "crash_contract": "write-ahead-inverse-durable",
        "side_effect_free": False,
    }
    if family.startswith("sudoers_fragment."):
        op["gate"] = SUDOERS_GATE
    elif family in RETIRE_FAMILIES:
        op["gate"] = RETIRE_GATE
    if staged:
        op["execution_scope"] = "staged-cutover"
    return op


def _plan(request, steps_for, *, staged=False):
    m, seat_provenance = _manifest(request)
    subjects = _observed(request)
    ops = []
    for family, params in steps_for(m):
        try:
            r = F.resolve(family, params, m)
        except F.ParamError as exc:
            raise PlanRefused(str(exc)) from None
        current = subjects.get((STAGED if staged else "") + r.subject)
        if current == r.after:
            continue  # already converged: verify, not mutate
        if family in RETIRE_FAMILIES and current is None:
            continue  # nothing present to retire
        ops.append(_op(family, params, r, current, m, staged=staged, seat_provenance=seat_provenance))
    return ops


def plan_install(request):
    return _plan(request, converge_steps)


def plan_configure(request):
    return _plan(request, converge_steps)


def plan_update(request):
    return _plan(request, converge_steps, staged=True)


def plan_repair(request):
    return _plan(request, converge_steps)


def plan_remove(request):
    return _plan(request, retire_steps)
