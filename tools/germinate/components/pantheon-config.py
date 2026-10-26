"""pantheon-config component planner (ORG-080 §2; T-5081, germination tranche GC).

An ADAPTER over tools/repo_reconciler.py, which it does not change. Every leg
runs the reconciler's own public planning call, ``RepoReconciler.reconcile_plan()``
(classification, action choice, refusal mapping, brokered-intent emission), with
the reconciler's seams injected:

* ``probe``: ``_ObservedProbe`` rebuilds each ``CheckoutObservation`` from
  ``request["observed"]``, which the executor-side handler's ``observe()``
  produced. The planner never runs git (the planner guard refuses subprocess).
* ``remote_resolver`` / ``health_probe``: the remote url and health result the
  handler observed.

The spec is the declarative ``checkouts/`` surface, read (never written) through
``checkout_spec.load_checkout_spec`` and narrowed to logical repo pantheon-config.

Returns typed operation data only. Reconciler refusals are plan refusals.
"""

from __future__ import annotations

import hashlib
from dataclasses import fields
from pathlib import Path

from tools.checkout_spec import CheckoutSpec, load_checkout_spec
from tools.repo_reconciler import ACTION_CLONE, ACTION_FF_ADVANCE, CheckoutObservation, RepoReconciler

LOGICAL_REPO = "pantheon-config"
HANDLER = "pantheon-config-repo"
CHECKOUTS_DIR = Path(__file__).resolve().parents[3] / "checkouts"
_OBS_FIELDS = tuple(f.name for f in fields(CheckoutObservation))


class PlanRefused(RuntimeError):
    """The reconciler (or the adapter's input checks) refuses this plan."""


class _ObservedProbe:
    """CheckoutProbe over the handler's observation data."""

    def __init__(self, checkouts):
        self._checkouts = checkouts

    def observe(self, entry, remote_url):
        raw = self._checkouts.get(entry.checkout_id)
        if raw is None:
            raise PlanRefused(f"no observation for checkout {entry.checkout_id!r}")
        return CheckoutObservation(**{name: raw[name] for name in _OBS_FIELDS})


def _reconcile(request):
    observed = (request.get("observed") or {}).get(HANDLER)
    if not isinstance(observed, dict):
        raise PlanRefused(f"request carries no {HANDLER} observation")
    spec = CheckoutSpec({e.checkout_id: e for e in load_checkout_spec(CHECKOUTS_DIR).by_logical_repo(LOGICAL_REPO)})
    checkouts = observed.get("checkouts") or {}
    reconciler = RepoReconciler(
        spec,
        probe=_ObservedProbe(checkouts),
        remote_resolver=lambda _logical_repo: observed.get("remote_url") or "",
        health_probe=lambda entry: bool(checkouts.get(entry.checkout_id, {}).get("health_ok", False)),
    )
    return spec, checkouts, reconciler.reconcile_plan()


def _require_ok(plan):
    if not plan.ok:
        raise PlanRefused("repo_reconciler refuses: " + "; ".join(sorted(r.as_text() for r in plan.refusals())))


def _op(prefix, kind, entry, item, plan, precondition, desired, inverse):
    provenance = {
        "planner": "tools.repo_reconciler.RepoReconciler.reconcile_plan",
        "reconciler_state": item.state if item else "n/a",
        "reconciler_action": item.action if item else "n/a",
        "reconcile_report_sha256": hashlib.sha256(plan.to_report_bytes()).hexdigest(),
    }
    if item is not None and item.brokered_intent is not None:
        intent = item.brokered_intent
        provenance["brokered_intent"] = f"{intent.operation_family}:{intent.target_unit} (inert, never dispatched)"
    return {
        "operation_id": f"{prefix}-{entry.checkout_id}"[:64],
        "layer": 1,
        "kind": kind,
        "target": entry.checkout_id,
        "precondition": precondition,
        "desired": dict(desired, target_path=entry.target_path, update_policy=entry.update_policy),
        "inverse": inverse,
        "privilege": "seat",
        "owner": entry.target_account,
        "provenance": provenance,
        "crash_contract": "write-ahead-inverse-durable",
        "side_effect_free": False,
    }


def _advance_ops(request, from_rev, to_rev, *, leg):
    spec, checkouts, plan = _reconcile(request)
    _require_ok(plan)
    ops = []
    for item in sorted(plan.items, key=lambda i: i.checkout_id):
        entry = spec.checkouts[item.checkout_id]
        if item.action == ACTION_CLONE:
            raise PlanRefused(f"{item.checkout_id} is absent; the {leg} leg never clones (run install)")
        if item.action != ACTION_FF_ADVANCE:
            continue
        raw = checkouts[item.checkout_id]
        frm = raw["head_sha"] if from_rev is None else from_rev
        to = raw["desired_target"] if to_rev is None else to_rev
        if raw["head_sha"] != frm:
            raise PlanRefused(f"{item.checkout_id}: --from-rev {frm} is not the observed HEAD {raw['head_sha']}")
        if raw["desired_target"] != to:
            raise PlanRefused(f"{item.checkout_id}: --to-rev {to} is not the observed desired target {raw['desired_target']}")
        ops.append(
            _op("ff-advance", "repo.ff-advance", entry, item, plan, {"head": frm, "reconciler_state": item.state}, {"head": to}, {"head": frm})
        )
    return ops


def plan_install(request):
    spec, checkouts, plan = _reconcile(request)
    _require_ok(plan)
    ops = []
    for item in sorted(plan.items, key=lambda i: i.checkout_id):
        if item.action != ACTION_CLONE:
            continue  # present checkouts are the update leg's; noop is nothing
        entry = spec.checkouts[item.checkout_id]
        to = checkouts[item.checkout_id]["desired_target"]
        ops.append(_op("clone", "repo.clone", entry, item, plan, {"exists": "false"}, {"head": to}, {"exists": "false"}))
    return ops


def plan_update(request):
    from_rev, to_rev = request.get("source_rev_from"), request.get("source_rev_to")
    if not from_rev or not to_rev:
        raise PlanRefused("the update leg needs --from-rev and --to-rev")
    return _advance_ops(request, from_rev, to_rev, leg="update")


def plan_repair(request):
    """Converge a drifted checkout: the same ff-advance, to the observed desired target."""

    return _advance_ops(request, None, None, leg="repair")


def plan_remove(request):
    """Reclaim ONLY engine-installed, pristine checkouts (repo_reconciler ``_reclaim``'s
    checks, re-stated over observation data: that method is fused with git I/O).
    Anything not engine-installed is unknown data and is never planned for removal."""

    spec, checkouts, plan = _reconcile(request)
    ops = []
    for cid in sorted(spec.checkouts):
        raw = checkouts.get(cid) or {}
        installed = raw.get("installed_sha")
        if not installed or not raw.get("exists"):
            continue
        if not raw.get("is_managed"):
            raise PlanRefused(f"{cid}: no longer the managed repo; refusing to reap")
        if raw.get("dirty"):
            raise PlanRefused(f"{cid}: working tree is dirty; refusing to reap")
        if raw.get("head_sha") != installed:
            raise PlanRefused(f"{cid}: HEAD {raw.get('head_sha')} != installed {installed}; refusing to reap")
        ops.append(_op("reclaim", "repo.reclaim", spec.checkouts[cid], None, plan, {"head": installed}, {"exists": "false"}, {"head": installed}))
    return ops
