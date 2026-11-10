"""credential-broker component planner (ORG-051, ORG-080 §2; T-5092, germination tranche GG).

An ADAPTER over tools/credential_broker.py and tools/credential_broker_cli.py,
which it does not change. The planner reads only the executor-side observation
(``request["observed"]["credential-broker-refs"]``): the broker's own ``verify``
report per declared credential reference, metadata only (availability, mode,
expiry, error text; never a value).

ADOPT-ONLY. The broker's secret-touching installer verbs (install / rotate /
revoke) are gated stubs pending the T-2869 custody decision (operator-reserved),
so no leg plans an op that touches credential material:

* ``plan_install`` is observe-and-attest (T-5947). Healthy, never attested ->
  ONE ``credential-broker.adopt-attest`` op whose ``desired`` is the verify
  evidence; attested and unchanged -> ``[]`` (closes SATISFIED); healthy drift ->
  re-attest with the new digests; UNHEALTHY (any reference unavailable) ->
  REFUSE with the CLI's own gate message, never attested or re-attested.
* ``plan_remove`` retires the engine's attestation record only; revocation and
  legacy-material retirement stay gated (T-2870).
* configure / update / repair are not declared (leg-not-declared).

The attestation op's ``owner`` is the ``owner`` every declared reference names
in instance.yaml (one account, ``claude`` today); mixed owners refuse rather
than pick one (review F5).

On a real host this refuses today (no healthy legacy setup to attest: the
install verb is a gated stub and instance.yaml's references carry no location
metadata). Attestation does not make dependency satisfaction fresh: the engine
counts any SATISFIED install run, however old; residual T-5954.
"""

from __future__ import annotations

HANDLER = "credential-broker-refs"
ATTEST_TARGET = "attestation:credential-broker"
EVIDENCE_KEYS = ("verify_rc", "report_sha256", "refs")
SUPERVISED = "the supervised path: the operator places each credential per ORG-051, then tools/credential_broker_cli.py verify"


class PlanRefused(RuntimeError):
    """The observed credential references cannot be planned for."""


def _observed(request):
    observed = (request.get("observed") or {}).get(HANDLER)
    if not isinstance(observed, dict) or "verify" not in observed:
        raise PlanRefused(f"request carries no {HANDLER} observation")
    return observed


def _op(op_id, kind, owner, precondition, desired, inverse, provenance):
    return {
        "operation_id": op_id,
        "layer": 1,
        "kind": kind,
        "target": ATTEST_TARGET,
        "precondition": precondition,
        "desired": desired,
        "inverse": inverse,
        "privilege": "seat",
        "owner": owner,
        "provenance": dict(provenance, planner="tools.credential_broker.HostFileCredentialBroker.verify (wrapped, unchanged)"),
        "crash_contract": "write-ahead-inverse-durable",
        "side_effect_free": False,
    }


def _prior(obs):
    return {"attested_report_sha256": (obs["attestation"] or {}).get("report_sha256", "none")}


def plan_install(request):
    obs = _observed(request)
    report = obs["verify"]["report"]
    if not report:
        raise PlanRefused(f"no declared credential_refs: nothing to attest; {obs['gate_message']} (T-2869)")
    if obs["verify"]["rc"] != 0:
        failed = [f"{r['credential_id']}: {'; '.join(r['errors']) or 'unavailable'}" for r in report if not r["available"]]
        raise PlanRefused(
            f"no healthy legacy credential setup ({' | '.join(failed)}); unhealthy evidence is never attested; "
            f"install is a gated stub: {obs['gate_message']} (T-2869); {SUPERVISED}"
        )
    attested = obs["attestation"]
    evidence = {k: obs["evidence"][k] for k in EVIDENCE_KEYS}
    if attested is not None and all(attested.get(k) == evidence[k] for k in EVIDENCE_KEYS):
        return []
    if obs.get("owner") is None:
        raise PlanRefused("credential_refs declare more than one owner: no single account owns the attestation (review F5)")
    return [_op(
        "adopt-attest-credential-broker", "credential-broker.adopt-attest", obs["owner"], _prior(obs), evidence, _prior(obs),
        {"basis": "observed-legacy", "re_attest": "true" if attested else "false", "verify_report_sha256": evidence["report_sha256"]},
    )]


def plan_remove(request):
    obs = _observed(request)
    if obs["attestation"] is None:
        return []
    return [_op(
        "attest-retire-credential-broker", "credential-broker.attest-retire", obs["owner"] or "unowned", _prior(obs),
        {"attested": "false"}, _prior(obs), {"basis": "engine attestation record only; revocation stays gated (T-2870)"},
    )]
