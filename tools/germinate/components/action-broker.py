"""action-broker component planner (ORG-080 §2; T-5948).

An ADAPTER over the legacy T-2920 install (``tools/host_broker_install.py`` plans,
``tools/host_broker_install_apply.py`` applies / verifies / rolls back), which it
does not change (ORG-080 A-5: wrap, don't rewrite).

The install leg is OBSERVE-AND-ATTEST. The engine never installs the broker: the
live install is the head role's supervised root run (T-2920 / T-2925). The leg
reads the handler's observation (``request["observed"]["action-broker-install"]``):
the legacy ``pantheon-host-broker-install-receipt/v1`` receipt and the legacy
``verify()`` result over it. It refuses, naming the supervised path, unless that
evidence shows a healthy install:

* a receipt is observed, its ``receipt_sha256`` is the legacy
  ``receipt_digest`` of its body, and its schema is the legacy schema;
* its mode is the handler's declared evidence tier (the legacy
  ``require_receipt_mode``); a ``rehearsal-nspawn`` receipt NEVER serves (Recovery (c));
* its status is ``complete`` or ``noop`` (never ``partial``);
* ``verify`` returned rc 0 with every entry ok, over the SAME plan_sha256.

Then it plans ONE seat op ``action-broker.adopt-attest`` whose ``desired`` carries
the evidence digests, so the plan digest binds the evidence (ORG-080
"plan-from-current-state; already-satisfied preconditions verify-not-mutate"). A
rerun whose attestation already matches plans nothing (verify, not mutate).

Returns typed operation data only.
"""

from __future__ import annotations

import hashlib

from tools import host_broker_install_apply as A

HANDLER = "action-broker-install"
KIND = "action-broker.adopt-attest"
TARGET = "action-broker:legacy-install"
GATE = "adopt-legacy-install"
ABSENT = "absent"
SERVABLE_TIERS = frozenset({A.FIXTURE_MODE, A.LIVE_MODE})
SUPERVISED = (
    "the engine never installs the broker; the live install is the head role's supervised root run "
    "(T-2920 tools/host_broker_install_apply.py, enablement T-2925)"
)


class PlanRefused(RuntimeError):
    """The legacy install evidence does not show a healthy install."""


def _sha(value):
    return hashlib.sha256(A.canonical(value).encode("ascii")).hexdigest()


def _refuse(why):
    raise PlanRefused(f"action-broker is not attestable: {why}; {SUPERVISED}")


def _evidence(observed):
    """The evidence digests of a healthy legacy install, or PlanRefused with the reason."""

    if not isinstance(observed, dict):
        _refuse(f"request carries no {HANDLER} observation")
    receipt, verify, tier = observed.get("receipt"), observed.get("verify"), observed.get("tier")
    if not isinstance(receipt, dict):
        _refuse("no legacy install receipt (T-2920) is observed (the broker is not installed)")
    if receipt.get("schema") != A.RECEIPT_SCHEMA:
        _refuse(f"receipt schema {receipt.get('schema')!r} is not {A.RECEIPT_SCHEMA}")
    if receipt.get("receipt_sha256") != A.receipt_digest(receipt):
        _refuse("receipt_sha256 is not the legacy receipt_digest of the receipt (tampered or truncated)")
    mode = receipt.get("mode")
    if mode == A.REHEARSAL_MODE:
        _refuse("a rehearsal-nspawn receipt never serves as install evidence (Recovery (c))")
    if tier not in SERVABLE_TIERS:
        _refuse(f"the handler's evidence tier {tier!r} cannot serve")
    try:
        A.require_receipt_mode(receipt, tier)
    except A.ApplyRefused as exc:
        _refuse(str(exc))
    status = receipt.get("status")
    if status not in ("complete", "noop"):
        _refuse(f"receipt status {status!r} (only complete or noop is an install)")
    if not isinstance(verify, dict):
        _refuse("no legacy verify result is observed")
    report = verify.get("report")
    if not isinstance(report, list) or not report:
        _refuse("the legacy verify report is empty")
    failed = sorted(
        (str(r.get("id")) if isinstance(r, dict) else repr(r))
        for r in report
        if not (isinstance(r, dict) and r.get("ok") is True)
    )
    if verify.get("rc") != 0 or failed:
        _refuse(f"legacy verify rc {verify.get('rc')!r}, failed entries {failed}")
    if verify.get("plan_sha256") != receipt.get("plan_sha256"):
        _refuse(f"verify ran over plan {verify.get('plan_sha256')!r}, the receipt is for {receipt.get('plan_sha256')!r}")
    return {
        "receipt_sha256": str(receipt["receipt_sha256"]),
        "plan_sha256": str(receipt.get("plan_sha256")),
        "framework_rev": str(receipt.get("framework_rev")),
        "mode": str(mode),
        "status": str(status),
        "verify_report_sha256": _sha(report),
    }


def plan_install(request):
    observed = (request.get("observed") or {}).get(HANDLER)
    desired = _evidence(observed)
    attestation = observed.get("attestation")
    if attestation == desired:
        return []  # already attested over this exact evidence: verify, not mutate
    prior = ABSENT if attestation is None else _sha(attestation)
    return [
        {
            "operation_id": "adopt-attest-action-broker",
            "layer": 0,
            "kind": KIND,
            "target": TARGET,
            "precondition": {"attestation_sha256": prior},
            "desired": desired,
            "inverse": {"attestation": ABSENT} if attestation is None else {"attestation_sha256": prior},
            "privilege": "seat",
            "owner": "action-broker",
            "provenance": {
                "planner": "tools.germinate.components.action-broker.plan_install (observe-and-attest)",
                "evidence": "tools.host_broker_install_apply.verify + receipt_digest + require_receipt_mode",
                "live_install": "T-2920 supervised root install (enablement T-2925); never run by the engine",
            },
            "crash_contract": "write-ahead-inverse-durable",
            "side_effect_free": False,
            "gate": GATE,
        }
    ]
