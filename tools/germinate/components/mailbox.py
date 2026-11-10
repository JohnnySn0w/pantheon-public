"""mailbox component planner (ORG-080 §2; T-5092, germination tranche GG).

An ADAPTER over tools/mailbox_onboard.py, which it does not change. The planner
reads only the executor-side observation (``request["observed"]["mailbox-organ"]``),
which carries mailbox_onboard ``verify()``'s own report; the planner runs no
tool, git or filesystem call (the planner guard refuses subprocess).

Observe-and-attest install (T-5947, the brownfield dependency path): an organ
that is already running (set up by mailbox_onboard, not by the engine) gets a
SATISFIED engine install record from ONE ``mailbox.adopt-attest`` op whose
``desired`` is the legacy verify's evidence (rc, report digest, facts digest),
so the approved plan digest binds the evidence and the op receipt records it.

``plan_install``, over the observed legacy evidence:

* hub substrate absent (group, hub-owner, hub root: host-substrate's ORG-082
  families) -> REFUSE, naming the supervised path. The engine never installs
  the legacy organ's root half through this leg.
* attested before: healthy and unchanged -> ``[]`` (closes SATISFIED);
  healthy but drifted -> re-plan the attest op with the new digests;
  UNHEALTHY -> REFUSE, never re-attested (converge first: the configure leg or
  the supervised tool).
* not attested, healthy: every present item engine-installed -> ``[]``;
  otherwise one attest op (adopting the legacy items).
* not attested, unhealthy: provisionable gaps only (a persona maildir, a
  sole-path wake unit: the tool's ``provision`` legs) on a GREENFIELD organ
  (no legacy item present) -> provision ops; gaps beside legacy items (a
  partial legacy organ) -> REFUSE "configure first, then install", so one
  install run never closes SATISFIED over a half-legacy organ with no
  attestation (review F2); any other failed check (group membership, a
  foreign unit) -> REFUSE, naming the supervised path.
* a present maildir whose mode is not mailbox_onboard's MAILBOX_DIR_MODE
  (02770) -> REFUSE attest and re-attest (review F1): ``verify_persona`` checks
  ``is_dir`` only, so a 0700 maildir the mailbox group cannot deliver into
  would otherwise attest healthy. The repair is the supervised tool's
  ``provision`` (or a chmod by the hub-owner); verify flags it as drift.

Attestation does not make dependency satisfaction fresh: the engine counts any
SATISFIED install run, however old (``_satisfied_install``); that residual is
T-5954. Drift is reported by ``verify`` (the attest op's postcondition re-check).
"""

from __future__ import annotations

HANDLER = "mailbox-organ"
ATTEST_TARGET = "attestation:mailbox"
SUPERVISED = "the supervised legacy path, tools/mailbox_onboard.py (hub-setup / provision --execute, then verify)"
EVIDENCE_KEYS = ("verify_rc", "report_sha256", "facts_sha256", "hub", "group", "hub_owner", "personas")


class PlanRefused(RuntimeError):
    """The observed mailbox organ cannot be planned for."""


def _observed(request):
    observed = (request.get("observed") or {}).get(HANDLER)
    if not isinstance(observed, dict) or "verify" not in observed:
        raise PlanRefused(f"request carries no {HANDLER} observation")
    return observed


def _require_substrate(obs):
    missing = sorted(k for k, ok in obs["substrate"].items() if not ok)
    if missing:
        raise PlanRefused(
            f"no healthy legacy mailbox: the hub substrate is absent ({', '.join(missing)}); it is host-substrate's "
            f"(ORG-082 families), never installed through this leg; supervised path: host-substrate install, or {SUPERVISED}"
        )


def _rows(report):
    """Walk a verify report once: (row, None) for a structurally valid row, (None, problem) for a malformed row or report."""

    if not isinstance(report, list):
        yield None, "malformed verify report: not a list"
        return
    for index, check in enumerate(report):
        # T-6092: a malformed row is an explicit hard failure (mirrors mailbox_handlers._partition/_failed), never a crash.
        if not isinstance(check, dict):
            yield None, f"malformed verify report row #{index}: not an object ({type(check).__name__})"
        elif not all(isinstance(check.get(k), str) and check.get(k) for k in ("persona", "name")):
            yield None, f"malformed verify report row #{index}: persona/name missing or not a non-empty string ({check!r})"
        else:
            yield check, None


def _is_junk(entry):
    """A truthy entry that is not an object (falsy means absent)."""

    return bool(entry) and not isinstance(entry, dict)


def _classify(obs):
    """mailbox_onboard verify's failed checks: (maildir gaps, wake-unit gaps, hard failures)."""

    personas = (obs.get("context") or {}).get("personas")
    personas = personas if isinstance(personas, dict) else {}
    units_obs = obs.get("units")
    units_obs = units_obs if isinstance(units_obs, dict) else {}
    maildirs, units, hard = set(), set(), []
    report = (obs.get("verify") or {}).get("report")
    for check, problem in _rows(report):
        if problem:
            hard.append(problem)
            continue
        name, persona = check["name"], check["persona"]
        ok = check.get("ok")
        if ok is True:
            continue
        if not isinstance(ok, bool):
            hard.append(f"{persona} {name}: ok is not a bool ({ok!r})")
        elif name.startswith("hub."):
            maildirs.add(persona)
        elif name == "wake_timer.enabled" and _is_junk(personas.get(persona)):
            hard.append(f"{persona}: persona entry is not an object ({type(personas.get(persona)).__name__})")
        elif name == "wake_timer.enabled" and (personas.get(persona) or {}).get("wake") == "sole":
            unit = units_obs.get(persona) or {}
            if not isinstance(unit, dict):
                hard.append(f"{persona}: unit entry is not an object ({type(unit).__name__})")
            elif unit.get("file") and not unit.get("managed"):
                hard.append(f"{persona}: a foreign wake unit file is in place")
            else:
                units.add(persona)
        else:
            hard.append(f"{persona} {name}: {check.get('detail', '')}")
    return sorted(maildirs), sorted(units), hard


def _op(op_id, kind, target, owner, precondition, desired, inverse, provenance, gate=None):
    op = {
        "operation_id": op_id[:64],
        "layer": 1,
        "kind": kind,
        "target": target,
        "precondition": precondition,
        "desired": desired,
        "inverse": inverse,
        "privilege": "seat",
        "owner": owner,
        "provenance": dict(provenance, planner="tools.mailbox_onboard.verify (wrapped, unchanged)"),
        "crash_contract": "write-ahead-inverse-durable",
        "side_effect_free": False,
    }
    if gate is not None:
        op["gate"] = gate
    return op


def _provenance(obs):
    return {"verify_rc": str(obs["verify"]["rc"]), "verify_report_sha256": obs["evidence"]["report_sha256"]}


def _provision_ops(obs, maildirs, units):
    ctx = obs["context"]
    ops = []
    for persona in maildirs:
        ops.append(_op(
            f"maildir-{persona}", "mailbox.maildir-provision", f"maildir:{persona}", ctx["hub_owner"],
            {"present": "false"}, {"persona": persona, "mode": ctx["maildir_mode"]}, {"present": "false"},
            dict(_provenance(obs), legacy_leg="provision hub leg (as the hub-owner)"),
        ))
    for persona in units:
        ops.append(_op(
            f"wake-unit-{persona}", "mailbox.wake-unit-install", f"wake-unit:{persona}", ctx["personas"][persona]["account"],
            {"enabled": "false"}, {"persona": persona, "enabled": "true"}, {"enabled": "false"},
            dict(_provenance(obs), legacy_leg="provision unit leg (as the persona's account)"),
        ))
    return ops


def _require_maildir_modes(obs):
    """Review F1: attest only maildirs at mailbox_onboard's own mode (02770)."""

    want = obs["context"]["maildir_mode"]
    wrong = sorted(f"{p} {md.get('mode')}" for p, md in obs["maildirs"].items() if md.get("present") and md.get("mode") != want)
    if wrong:
        raise PlanRefused(
            f"maildir-mode-degraded: {', '.join(wrong)} (mailbox_onboard requires {want}; the mailbox group cannot "
            f"deliver otherwise); never attested; repair: restore mode {want} as the hub-owner ({SUPERVISED}), then re-plan"
        )


def _legacy_present(obs):
    """Present maildirs / enabled sole-path units the engine did not install."""

    installed = set(obs["installed"])
    present = {f"maildir:{p}" for p, md in obs["maildirs"].items() if md.get("present")}
    present |= {f"wake-unit:{p}" for p, u in obs["units"].items() if u.get("enabled") and obs["context"]["personas"].get(p, {}).get("wake") == "sole"}
    return sorted(present - installed)


def _attest_op(obs):
    _require_maildir_modes(obs)
    evidence = {k: obs["evidence"][k] for k in EVIDENCE_KEYS}
    prior = obs["attestation"]
    return _op(
        "adopt-attest-mailbox", "mailbox.adopt-attest", ATTEST_TARGET, obs["context"]["hub_owner"],
        {"attested_report_sha256": (prior or {}).get("report_sha256", "none")}, evidence,
        {"attested_report_sha256": (prior or {}).get("report_sha256", "none")},
        dict(_provenance(obs), basis="observed-legacy", re_attest="true" if prior else "false"),
    )


def _engine_owned(obs):
    """Every present maildir and enabled sole-path unit was installed by the engine."""

    installed = set(obs["installed"])
    present = {f"maildir:{p}" for p, md in obs["maildirs"].items() if md.get("present")}
    present |= {f"wake-unit:{p}" for p, u in obs["units"].items() if u.get("enabled") and obs["context"]["personas"].get(p, {}).get("wake") == "sole"}
    return present <= installed


def plan_install(request):
    obs = _observed(request)
    _require_substrate(obs)
    healthy = obs["verify"]["rc"] == 0
    attested = obs["attestation"]
    if attested is not None:
        if not healthy:
            failed = [problem or f"{c['persona']} {c['name']}" for c, problem in _rows(obs["verify"]["report"]) if problem or c.get("ok") is not True]
            raise PlanRefused(
                f"the attested mailbox is UNHEALTHY now ({'; '.join(failed)}); unhealthy evidence is never re-attested: "
                f"converge it first (the configure leg, or {SUPERVISED}), then re-plan"
            )
        if all(attested.get(k) == obs["evidence"][k] for k in EVIDENCE_KEYS):
            return []
        return [_attest_op(obs)]
    if healthy:
        return [] if _engine_owned(obs) else [_attest_op(obs)]
    maildirs, units, hard = _classify(obs)
    if hard:
        raise PlanRefused(f"no healthy legacy mailbox install: {'; '.join(hard)}; supervised path: {SUPERVISED}")
    legacy = _legacy_present(obs)
    if legacy:
        gaps = [f"maildir:{p}" for p in maildirs] + [f"wake-unit:{p}" for p in units]
        raise PlanRefused(
            f"partial legacy mailbox: legacy items {', '.join(legacy)} beside gaps ({', '.join(gaps)}); "
            "configure first (it provisions the gaps), then install (it attests the whole organ)"
        )
    return _provision_ops(obs, maildirs, units)


def plan_configure(request):
    """Converge provisionable drift (a new persona's maildir, a sole-path wake unit)."""

    obs = _observed(request)
    _require_substrate(obs)
    maildirs, units, hard = _classify(obs)
    if hard:
        raise PlanRefused(f"mailbox drift the engine does not converge: {'; '.join(hard)}; supervised path: {SUPERVISED}")
    return _provision_ops(obs, maildirs, units)


def plan_remove(request):
    """Retire the sole-path wake units and the engine's attestation. Never a maildir or the
    hub (ORG-052 gate 34; mailbox_onboard teardown's purge stays an opt-in operator act)."""

    obs = _observed(request)
    ctx = obs["context"]
    ops = []
    for persona, unit in sorted(obs["units"].items()):
        if unit.get("enabled") and unit.get("managed") and ctx["personas"].get(persona, {}).get("wake") == "sole":
            ops.append(_op(
                f"wake-retire-{persona}", "mailbox.wake-unit-retire", f"wake-unit:{persona}", ctx["personas"][persona]["account"],
                {"enabled": "true"}, {"persona": persona, "enabled": "false"}, {"enabled": "true"},
                {"legacy_leg": "teardown unit leg (as the persona's account)"}, gate="mailbox-retire",
            ))
    if obs["attestation"] is not None:
        ops.append(_op(
            "attest-retire-mailbox", "mailbox.attest-retire", ATTEST_TARGET, ctx["hub_owner"],
            {"attested_report_sha256": obs["attestation"].get("report_sha256", "none")}, {"attested": "false"},
            {"attested_report_sha256": obs["attestation"].get("report_sha256", "none")},
            {"basis": "engine attestation record only"}, gate="mailbox-retire",
        ))
    return ops
