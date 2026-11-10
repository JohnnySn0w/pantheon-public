"""mailbox component: executor-side registration (ORG-080 §2; T-5092, germination tranche GG).

``OP_KINDS`` is literal data (read with ast.literal_eval): seat privilege, in the
component's own non-reserved ``mailbox.organ`` class.

``HANDLERS`` holds ONE handler, and it is SIMULATED (CI tier, nothing live):
``MailboxOrganSim`` keeps the mailbox organ as JSON state inside the simulated
host. It WRAPS tools/mailbox_onboard.py (unchanged): every observation runs the
tool's own ``verify()`` over a throwaway materialization of the simulated organ
(the persona maildirs as real directories, the tool's own ``FakeHostIdentity``
and ``FakeCommandRunner`` seams for group membership and ``systemctl --user
is-enabled``), with the context from the tool's own ``load_mailbox_context``
over this checkout's instance.yaml. No real hub path, account or unit is read.

Observe-and-attest install (T-5947): ``observe()`` reports the wrapped verify's
evidence, ``verify_rc`` plus a ``report_sha256`` over its checks and a
``facts_sha256`` over the substrate, maildir-mode, maildir-ACL and unit-marker facts. The
planner's ``mailbox.adopt-attest`` op carries that evidence in ``desired``, so
the approved plan digest binds it and the receipt records it. The op's only
mutation is the attestation entry in this handler's own state; its inverse
restores the prior entry. Its ``verify_operation`` (and so the engine's
``verify``) compares the CURRENT evidence with the attested evidence and names
the drifted keys. Attestation does not make dependency satisfaction fresh: the
engine counts any SATISFIED install run (``_satisfied_install``), however old;
that residual is T-5954.

The hub substrate (the pantheon-mailbox group, the hub-owner account, linger and
the hub root) is host-substrate's (ORG-082 families); this handler only reports
it. Simulated state::

    substrate:   {group, hub_owner, hub_root: bool}
    members:     {account: bool}                      # in the mailbox group
    maildirs:    {persona: {present: bool, mode: "2770", acl: "<getfacl -p text>"}}   # acl: T-6009, the
                                                  # maildir + new/ + tmp/ over the REAL hub paths; absent = unobserved
    units:       {persona: {file, managed, enabled: bool}}
    installed:   {target: true}                       # engine-installed items
    attestation: null | {evidence key: str}
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from tools import mailbox_onboard as MO
from tools._organ_lifecycle import CommandResult, FakeCommandRunner
from tools.persona_installer_contracts import MutationRecord, OperationEvidence, PreflightResult

OP_KINDS = {
    "mailbox.maildir-provision": ("mailbox.organ", "seat"),
    "mailbox.wake-unit-install": ("mailbox.organ", "seat"),
    "mailbox.wake-unit-retire": ("mailbox.organ", "seat"),
    "mailbox.adopt-attest": ("mailbox.organ", "seat"),
    "mailbox.attest-retire": ("mailbox.organ", "seat"),
}

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPO_ROOT / "instance.yaml"
ATTEST_TARGET = "attestation:mailbox"
#: mailbox_onboard's own maildir mode (MAILBOX_DIR_MODE, 02770): the mode the planner requires
#: before it attests (review F1: verify_persona checks is_dir only, so a 0700 maildir the
#: group cannot deliver into would otherwise read healthy).
MAILDIR_MODE = format(MO.MAILBOX_DIR_MODE, "o")
EVIDENCE_KEYS = ("verify_rc", "report_sha256", "facts_sha256", "hub", "group", "hub_owner", "personas")


def _default_state():
    return {
        "substrate": {"group": False, "hub_owner": False, "hub_root": False},
        "members": {},
        "maildirs": {},
        "units": {},
        "installed": {},
        "attestation": None,
    }


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _canon(value):
    return json.dumps(value, sort_keys=True)


def _persona(target):
    return target.split(":", 1)[1]


class MailboxOrganSim:
    name = "mailbox-organ"
    kinds = frozenset(OP_KINDS)

    def __init__(self):
        self.state = _default_state()
        self._ctx = None

    # --- the wrapped tool ----------------------------------------------------

    def context(self):
        """mailbox_onboard's own context and persona bindings for this checkout's manifest."""

        if self._ctx is None:
            ctx = MO.load_mailbox_context(MANIFEST, REPO_ROOT)
            self._ctx = (ctx, MO._all_persona_bindings(ctx.manifest))
        return self._ctx

    def legacy_verify(self):
        """``mailbox_onboard.verify()`` over a throwaway materialization of the simulated organ."""

        ctx, bindings = self.context()
        st = self.state
        with tempfile.TemporaryDirectory(prefix="germinate-mailbox-") as tmp:
            hub = Path(tmp) / "hub"
            if st["substrate"]["hub_root"]:
                hub.mkdir()
                for persona, md in st["maildirs"].items():
                    if md.get("present"):
                        mode = int(md.get("mode") or MAILDIR_MODE, 8)
                        (hub / persona).mkdir()
                        for path in (hub / persona, *(hub / persona / sub for sub in MO.MAILBOX_SUBDIRS)):
                            path.mkdir(exist_ok=True)
                            os.chmod(path, mode)  # the simulated mode, applied (review F1)
            members = sorted(
                ({a for a, v in st["members"].items() if v} | ({ctx.hub_owner} if st["substrate"]["hub_owner"] else set()))
                if st["substrate"]["group"] else ()
            )
            identity = MO.FakeHostIdentity(
                users=[ctx.hub_owner] if st["substrate"]["hub_owner"] else [],
                groups=[ctx.mailbox_group] if st["substrate"]["group"] else [],
                members={ctx.mailbox_group: members},
                memberships={
                    (b.account, ctx.mailbox_group): (
                        (True, f"{b.account} in {ctx.mailbox_group}")
                        if st["members"].get(b.account) and st["substrate"]["group"]
                        else (False, f"{b.account} not in {ctx.mailbox_group}")
                    )
                    for b in bindings
                },
            )
            scripted = {}
            for persona, md in st["maildirs"].items():  # T-6009: the simulated `getfacl -p`, paths mapped onto the throwaway hub
                if md.get("present") and md.get("acl") is not None:
                    paths = (hub / persona, hub / persona / "new", hub / persona / "tmp")
                    scripted[("getfacl", "-p", *map(str, paths))] = CommandResult(0, md["acl"].replace(str(ctx.mailbox_hub), str(hub)), "")
            for b in bindings:
                enabled = bool((st["units"].get(b.persona) or {}).get("enabled"))
                argv = ("systemctl", "--user", "is-enabled", f"{MO.WAKE_UNIT_STEM}@{b.persona}.timer")
                scripted[argv] = CommandResult(0, "enabled\n", "") if enabled else CommandResult(1, "disabled\n", "")
            checks = MO.verify(dataclasses.replace(ctx, mailbox_hub=hub), bindings, FakeCommandRunner(scripted), identity)
            report = [
                {"persona": c.persona, "name": c.name, "ok": c.ok, "detail": c.detail.replace(str(hub), str(ctx.mailbox_hub))}
                for c in checks
            ]
        return report

    def evidence(self, report=None):
        ctx, bindings = self.context()
        report = self.legacy_verify() if report is None else report
        st = self.state
        facts = {
            "substrate": st["substrate"],
            "maildir_modes": {p: md.get("mode") for p, md in sorted(st["maildirs"].items()) if md.get("present")},
            "maildir_acls": {p: _sha(md.get("acl")) for p, md in sorted(st["maildirs"].items()) if md.get("present")},
            "unit_markers": {p: bool(u.get("managed")) for p, u in sorted(st["units"].items()) if u.get("file")},
        }
        return {
            "verify_rc": _rc(report),
            "report_sha256": _sha(report),
            "facts_sha256": _sha(facts),
            "hub": str(ctx.mailbox_hub),
            "group": ctx.mailbox_group,
            "hub_owner": ctx.hub_owner,
            "personas": ",".join(sorted(b.persona for b in bindings)),
        }

    # --- EffectHandler -------------------------------------------------------

    def observe(self, component, scope):
        ctx, bindings = self.context()
        report = self.legacy_verify()
        st = self.state

        def wake(persona):
            if persona in ctx.wake_timer_personas:
                return "sole"
            return "secondary" if persona in ctx.wake_timer_secondary_personas else "none"

        return {
            "context": {
                "hub": str(ctx.mailbox_hub),
                "group": ctx.mailbox_group,
                "hub_owner": ctx.hub_owner,
                "maildir_mode": MAILDIR_MODE,
                "personas": {b.persona: {"account": b.account, "wake": wake(b.persona)} for b in bindings},
            },
            "substrate": dict(st["substrate"]),
            "verify": {"rc": int(_rc(report)), "report": report},
            "evidence": self.evidence(report),
            "attestation": copy.deepcopy(st["attestation"]),
            "maildirs": copy.deepcopy(st["maildirs"]),
            "units": copy.deepcopy(st["units"]),
            "installed": sorted(st["installed"]),
        }

    def _slot(self, op):
        if op.kind == "mailbox.maildir-provision":
            return self.state["maildirs"].get(_persona(op.target))
        if op.kind in ("mailbox.wake-unit-install", "mailbox.wake-unit-retire"):
            return self.state["units"].get(_persona(op.target))
        return self.state["attestation"]

    def _snapshot(self, op):
        return {"slot": _canon(self._slot(op)), "installed": "true" if op.target in self.state["installed"] else "false"}

    @staticmethod
    def _result(op):
        if op.kind == "mailbox.maildir-provision":
            return {"present": True, "mode": op.desired["mode"]}
        if op.kind == "mailbox.wake-unit-install":
            return {"file": True, "managed": True, "enabled": True}
        if op.kind == "mailbox.wake-unit-retire":
            return {"file": False, "managed": False, "enabled": False}
        if op.kind == "mailbox.adopt-attest":
            return {k: op.desired[k] for k in EVIDENCE_KEYS}
        return None

    def _drift(self, attested):
        current = self.evidence()
        return current, sorted(k for k in EVIDENCE_KEYS if current.get(k) != (attested or {}).get(k))

    def preflight(self, op):
        slot = self._slot(op)
        if op.kind == "mailbox.maildir-provision":
            ok = self.state["substrate"]["hub_root"] and not (slot or {}).get("present")
            return PreflightResult(ok, self.name, "hub root present, maildir absent" if ok else "hub root missing or maildir present")
        if op.kind == "mailbox.wake-unit-install":
            ok = not (slot or {}).get("file") or (slot or {}).get("managed")
            ok = ok and not (slot or {}).get("enabled")
            return PreflightResult(ok, self.name, "unit absent or ours and disabled" if ok else "a foreign or enabled unit is in place")
        if op.kind == "mailbox.wake-unit-retire":
            ok = bool((slot or {}).get("enabled") and (slot or {}).get("managed"))
            return PreflightResult(ok, self.name, "managed unit enabled" if ok else "nothing managed to retire")
        if op.kind == "mailbox.adopt-attest":
            current, drifted = self._drift(op.desired)
            ok = not drifted and current["verify_rc"] == "0"
            detail = "legacy verify healthy and matches the planned evidence" if ok else f"evidence moved since plan: {drifted or ['verify_rc']}"
            return PreflightResult(ok, self.name, detail)
        ok = slot is not None
        return PreflightResult(ok, self.name, "attestation present" if ok else "no attestation to retire")

    def rollback_intent(self, op):
        return MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, self._snapshot(op), _canon(self._result(op)))

    def execute(self, op):
        mutation = self.rollback_intent(op)
        result = self._result(op)
        if op.kind == "mailbox.maildir-provision":
            self.state["maildirs"][_persona(op.target)] = result
            self.state["installed"][op.target] = True
        elif op.kind in ("mailbox.wake-unit-install", "mailbox.wake-unit-retire"):
            self.state["units"][_persona(op.target)] = result
            if op.kind == "mailbox.wake-unit-install":
                self.state["installed"][op.target] = True
            else:
                self.state["installed"].pop(f"wake-unit:{_persona(op.target)}", None)
        else:
            self.state["attestation"] = result
        return mutation, OperationEvidence(op.operation_id, op.layer, op.kind, True, f"simulated {op.kind}")

    def verify_operation(self, op):
        if op.kind in ("mailbox.maildir-provision", "mailbox.wake-unit-install"):
            persona = _persona(op.target)
            prefix = "hub." if op.kind == "mailbox.maildir-provision" else "wake_timer.enabled"
            checks = [c for c in self.legacy_verify() if c["persona"] == persona and c["name"].startswith(prefix)]
            ok = bool(checks) and all(c["ok"] for c in checks)
            return OperationEvidence(op.operation_id, op.layer, op.kind, ok, "mailbox_onboard verify: " + "; ".join(c["detail"] for c in checks))
        if op.kind == "mailbox.wake-unit-retire":
            unit = self._slot(op) or {}
            ok = not unit.get("enabled") and not unit.get("file")
            return OperationEvidence(op.operation_id, op.layer, op.kind, ok, "unit retired" if ok else "unit still present or enabled")
        if op.kind == "mailbox.attest-retire":
            ok = self.state["attestation"] is None
            return OperationEvidence(op.operation_id, op.layer, op.kind, ok, "attestation retired" if ok else "attestation still present")
        stored = self.state["attestation"]
        if stored != self._result(op):
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, "the recorded attestation is not this op's evidence")
        current, drifted = self._drift(stored)
        if drifted:
            return OperationEvidence(op.operation_id, op.layer, op.kind, False, f"drift from the attested evidence: {', '.join(drifted)} (verify_rc now {current['verify_rc']})")
        return OperationEvidence(op.operation_id, op.layer, op.kind, True, f"attested mailbox_onboard verify rc 0, report {stored['report_sha256'][:12]}")

    def can_rollback(self, mutation):
        current = _canon(self._slot_for(mutation))
        if current == mutation.before["slot"]:
            return PreflightResult(True, "not-applied", "slot still at its preimage")
        owned = current == mutation.after_fingerprint
        return PreflightResult(owned, "owned" if owned else "drifted", "this op's result" if owned else "slot moved since the op")

    def _slot_for(self, mutation):
        return self._slot(SimpleNamespace(kind=mutation.kind, target=mutation.target))

    def rollback_mutation(self, mutation):
        before = json.loads(mutation.before["slot"])
        if mutation.kind == "mailbox.maildir-provision":
            table = self.state["maildirs"]
        elif mutation.kind in ("mailbox.wake-unit-install", "mailbox.wake-unit-retire"):
            table = self.state["units"]
        else:
            self.state["attestation"] = before
            return
        persona = _persona(mutation.target)
        if before is None:
            table.pop(persona, None)
        else:
            table[persona] = before
        key = f"{'maildir' if mutation.kind == 'mailbox.maildir-provision' else 'wake-unit'}:{persona}"
        if mutation.before["installed"] == "true":
            self.state["installed"][key] = True
        else:
            self.state["installed"].pop(key, None)

    def dump_state(self):
        return copy.deepcopy(self.state)

    def load_state(self, state):
        self.state = dict(_default_state(), **copy.deepcopy(dict(state)))


#: T-5991: the org.pantheon.mailbox/v1 invariant list (M1-M7), evaluated by VERIFY.
_PASS_DETAIL = {
    "M1-hub-substrate": "group, hub owner and hub root present",
    "M2-maildir-present": "every bound persona has its maildir",
    "M3-maildir-acl": "every mailbox-group member holds effective rwx (access and default) on each maildir, its new/ and tmp/",
    "M4-group-membership": "every bound account is in the mailbox group",
    "M5-wake-unit-sole": "every sole-wake persona has a managed, enabled wake unit",
    "M6-wake-unit-non-sole": "no non-wake persona has its timer enabled",
    "M7-attestation-current": "the attestation matches the current evidence (or none is recorded)",
}
_FAILED, _DEGRADED = "failed", "degraded"


def _report(observed):
    """The wrapped verify report, or None when it is missing, empty or not a list (could not ask)."""

    report = (observed.get("verify") or {}).get("report")
    return report if isinstance(report, list) and report else None


def _rc(report):
    """verify_rc over a raw report: any row that is not a dict with ``ok is True`` (a real bool) reads red."""

    return "0" if all(isinstance(c, dict) and c.get("ok") is True for c in report) else "1"


def _partition(observed):
    """(valid rows, problems): a row is valid when it is a dict with a non-empty str persona and name.

    ``ok`` is NOT validated here (a non-bool ``ok`` is a failed check, see ``_failed``); malformed rows are
    excluded from every other evaluator and reported once, under M2."""

    valid, problems = [], []
    for index, c in enumerate(_report(observed) or []):
        if not isinstance(c, dict):
            problems.append(f"malformed verify report row #{index}: not an object ({type(c).__name__})")
        elif not all(isinstance(c.get(k), str) and c.get(k) for k in ("persona", "name")):
            problems.append(f"malformed verify report row #{index}: persona/name missing or not a non-empty string ({c!r})")
        else:
            valid.append(c)
    return valid, problems


def _failed(rows, *, prefix=None, name=None, named=False):
    """(row, message) for each failed valid row matching. A non-bool ``ok`` is a failed check, never a pass
    ("<persona> <name>: ok is not a bool (<repr>)"); else "<persona>: <detail>" ("<persona> <name>: <detail>" when ``named``)."""

    out = []
    for c in rows:
        if not ((prefix is not None and c["name"].startswith(prefix)) or (name is not None and c["name"] == name)):
            continue
        ok = c.get("ok")
        if ok is True:
            continue
        who = f"{c['persona']} {c['name']}" if named or not isinstance(ok, bool) else c["persona"]
        out.append((c, f"{who}: {c.get('detail', '')}" if isinstance(ok, bool) else f"{who}: ok is not a bool ({ok!r})"))
    return out


def _findings(observed):
    """(invariant id, severity, message) for each breach, over the observation only."""

    out = []
    substrate = observed.get("substrate") or {}
    missing = sorted(k for k in ("group", "hub_owner", "hub_root") if not substrate.get(k))
    if missing:
        out.append(("M1-hub-substrate", _FAILED, f"hub substrate absent: {', '.join(missing)}"))
    context = observed.get("context") or {}
    personas = context.get("personas") or {}
    maildirs = observed.get("maildirs") or {}
    units = observed.get("units") or {}
    rows, problems = _partition(observed)
    have_report = _report(observed) is not None
    if not personas:
        out.append(("M2-maildir-present", _FAILED, "no bound personas observed"))
    if not have_report:  # an empty observation is never a pass
        out.append(("M2-maildir-present", _FAILED, "could not evaluate the hub.* checks: no verify report observed"))
        out.append(("M4-group-membership", _FAILED, "no verify report observed"))
        out.append(("M6-wake-unit-non-sole", _FAILED, "could not evaluate: no verify report observed"))
    out.extend(("M2-maildir-present", _FAILED, problem) for problem in problems)
    for persona in sorted(personas):
        if not (maildirs.get(persona) or {}).get("present"):
            out.append(("M2-maildir-present", _FAILED, f"{persona}: maildir absent"))
    for c, text in _failed(rows, prefix="hub.", named=True):
        if (maildirs.get(c["persona"]) or {}).get("present"):
            out.append(("M2-maildir-present", _FAILED, text))
    for _c, text in _failed(rows, name="maildir_acl"):  # T-6009: the mode evaluation is gone; the ACLs are what grant delivery
        out.append(("M3-maildir-acl", _DEGRADED, text))
    for _c, text in _failed(rows, name="group_membership"):
        out.append(("M4-group-membership", _FAILED, text))
    for persona, info in sorted(personas.items()):
        if info.get("wake") != "sole":
            continue
        unit = units.get(persona) or {}
        if unit.get("file") and not unit.get("managed"):
            out.append(("M5-wake-unit-sole", _FAILED, f"{persona}: a foreign wake unit file is in place"))
        elif not (unit.get("file") and unit.get("managed") and unit.get("enabled")):
            out.append(("M5-wake-unit-sole", _FAILED, f"{persona}: wake unit not present, managed and enabled"))
    for c, text in _failed(rows, name="wake_timer.enabled"):  # both the unit facts and the report check must hold
        if (personas.get(c["persona"]) or {}).get("wake") == "sole":
            out.append(("M5-wake-unit-sole", _FAILED, text))
    for _c, text in _failed(rows, name="wake_timer.not_enabled"):
        out.append(("M6-wake-unit-non-sole", _FAILED, text))
    if have_report:
        out.extend(_coverage(rows, personas, maildirs))
    attested = observed.get("attestation")
    if attested is not None:
        drifted = sorted(k for k in EVIDENCE_KEYS if (observed.get("evidence") or {}).get(k) != attested.get(k))
        if drifted:
            out.append(("M7-attestation-current", _DEGRADED, f"drift from the attested evidence: {', '.join(drifted)}"))
    return out


def _coverage(rows, personas, maildirs):
    """T-6009: a bound persona's required row must be PRESENT (a missing row is could-not-evaluate, never a pass).

    Secondary wakers carry no required wake row (their timer is allowed either way)."""

    have = {(c["persona"], c["name"]) for c in rows}
    out = []
    for persona, info in sorted(personas.items()):
        if (persona, "group_membership") not in have:
            out.append(("M4-group-membership", _FAILED, f"{persona}: no group_membership row"))
        if info.get("wake") == "sole" and (persona, "wake_timer.enabled") not in have:
            out.append(("M5-wake-unit-sole", _FAILED, f"{persona}: no wake_timer.enabled row"))
        if info.get("wake") == "none" and (persona, "wake_timer.not_enabled") not in have:
            out.append(("M6-wake-unit-non-sole", _FAILED, f"{persona}: could not evaluate: no wake_timer.not_enabled row"))
        if (maildirs.get(persona) or {}).get("present") and (persona, "maildir_acl") not in have:
            out.append(("M3-maildir-acl", _DEGRADED, f"{persona}: could not evaluate: no maildir_acl row"))
    return out


def VERIFY(list_doc, observed, scope):  # noqa: N802 -- the engine's registration name (components.py)
    """T-5991: evaluate org.pantheon.mailbox/v1 (M1-M7) over the observed mailbox state.

    Any FAILED finding makes the state unhealthy; DEGRADED-only findings make it degraded."""

    observation = (observed or {}).get(MailboxOrganSim.name)
    if not isinstance(observation, dict) or "verify" not in observation:
        raise ValueError("no mailbox observation: could not ask")
    findings = _findings(observation)
    results, severities = [], set()
    for entry in list_doc.get("invariants") or []:
        iid = entry.get("id") if isinstance(entry, dict) else None
        if iid not in _PASS_DETAIL:
            results.append({"id": str(iid), "status": "alarm", "detail": "no evaluator for this invariant"})
            severities.add(_FAILED)
            continue
        mine = [(sev, msg) for fid, sev, msg in findings if fid == iid]
        severities.update(sev for sev, _m in mine)
        if mine:
            results.append({"id": iid, "status": "fail", "detail": "; ".join(m for _s, m in mine)})
        else:
            results.append({"id": iid, "status": "pass", "detail": _PASS_DETAIL[iid]})
    state = "unhealthy" if _FAILED in severities else "degraded" if severities else "ready"
    return {"results": results, "state": state}


HANDLERS = [MailboxOrganSim()]
