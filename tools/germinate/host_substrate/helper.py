"""RootHelper: the narrow root helper behind the host-substrate component (T-5084, GE).

Generalizes ORG-052 Unit 6b (T-2918): a CLOSED op enum, every value resolved
from the manifest, context admission before anything else runs.

Context admission (checked first, before any parameter is read):

* ``bootstrap-in-process-root`` -- the only context that may ACTIVATE
  (ORG-082 §1 / §3: substrate converges through the supervised operator-run leg).
* ``broker-daemon`` -- refused for every family, with the ORG-082 §2 payload
  (``SUBSTRATE_FAMILY_NOT_EXPOSED``, retry in ``bootstrap-in-process-root``).
* ``seat`` -- refused: an unprivileged context never reaches root families.
* ``scheduled`` -- may STAGE only, never activate (ORG-080 A-3: a scheduled run
  never runs as root; a privileged component auto-stages, cutover stays supervised).

Engine seam gap S6 (T-5135): the engine passes no context and ignores
``execution_scope``; the component handler pins the context and maps
``staged-cutover`` to the stage phase, so this is enforced component-side only.

CURRENT (co-author recovery; D15 approved at 71fd484cc): the admission rules are Recovery-approved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Mapping

from tools.ci_fleet_account_reconcile import CONFLICT, DRIFT, AccountSpec, PasswdEntry, classify

from . import families as F
from .ports import SimulatedSubstratePort

BOOTSTRAP: Final = "bootstrap-in-process-root"
DAEMON: Final = "broker-daemon"
SEAT: Final = "seat"
SCHEDULED: Final = "scheduled"
CONTEXTS: Final = frozenset({BOOTSTRAP, DAEMON, SEAT, SCHEDULED})

STAGE: Final = "stage"
ACTIVATE: Final = "activate"
PHASES: Final = frozenset({STAGE, ACTIVATE})
STAGED_PREFIX: Final = "staged:"

#: A command the sudo -l negative check must never see allowed.
NEGATIVE_PROBE: Final = "/bin/sh"


class HelperRefusal(RuntimeError):
    code = "REFUSED"


class UnknownOperation(HelperRefusal):
    code = "UNKNOWN_OPERATION"


class SubstrateFamilyNotExposed(HelperRefusal):
    """ORG-082 §2: the broker daemon never exposes a substrate family."""

    code = "SUBSTRATE_FAMILY_NOT_EXPOSED"
    dbus_name = "org.pantheon.HostBroker1.SubstrateFamilyNotExposed"

    def __init__(self, family: str):
        super().__init__(f"{family} is a substrate family; the broker daemon does not expose it (ORG-082 §1)")
        self.family = family

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "family": self.family, "amendment": "ORG-082", "retry_context": BOOTSTRAP}


class UnprivilegedContext(HelperRefusal):
    code = "UNPRIVILEGED_CONTEXT"


class ScheduledActivationRefused(HelperRefusal):
    code = "SCHEDULED_ACTIVATION_REFUSED"


class PreflightRefused(HelperRefusal):
    code = "PREFLIGHT_REFUSED"


@dataclass(frozen=True)
class Step:
    """One admitted op: the subject it converges and the value it converges to."""

    resolved: F.Resolved
    phase: str

    @property
    def subject(self) -> str:
        s = self.resolved.subject
        return STAGED_PREFIX + s if self.phase == STAGE else s


@dataclass
class Evidence:
    ok: bool
    detail: str
    data: dict[str, str] = field(default_factory=dict)


def _passwd_entries(snapshot: Mapping[str, Any]) -> list[PasswdEntry]:
    out = []
    for subject, value in snapshot.items():
        if subject.startswith("passwd:") and isinstance(value, dict):
            out.append(PasswdEntry(subject[7:], value["uid"], value["gid"], value["home"], value["shell"]))
    return out


def _group_collision(snapshot: Mapping[str, Any], name: str, gid: int, *, private: bool) -> str | None:
    """Return a stable refusal reason for a name/gid collision, else None."""

    label = "private group" if private else "group"
    current = snapshot.get(f"group:{name}")
    if current is not None:
        current_gid = current.get("gid") if isinstance(current, dict) else None
        if current_gid != gid:
            return f"{label} CONFLICT: {name} exists with gid {current_gid!r}, expected {gid}"
        if private:
            return f"private group CONFLICT: {name} already exists with gid {gid}; account.ensure requires creating it"
    for subject, value in snapshot.items():
        if not subject.startswith("group:") or subject == f"group:{name}" or not isinstance(value, dict):
            continue
        if value.get("gid") == gid:
            return f"{label} CONFLICT: gid {gid} is held by {subject[6:]}"
    return None


@dataclass(frozen=True)
class _ManifestAccount(AccountSpec):
    """ci_fleet_account_reconcile.AccountSpec with the manifest's home and shell."""

    home_path: str = ""
    login_shell: str = ""

    @property
    def home(self) -> str:  # type: ignore[override]
        return self.home_path

    @property
    def shell(self) -> str:  # type: ignore[override]
        return self.login_shell


class RootHelper:
    def __init__(self, port: SimulatedSubstratePort, manifest: F.Manifest, context: str):
        if context not in CONTEXTS:
            raise HelperRefusal(f"unknown invocation context {context!r}")
        self.port = port
        self.manifest = manifest
        self.context = context

    # --- admission ---------------------------------------------------------

    def admit(self, family: str, params: Mapping[str, str], phase: str) -> Step:
        if family not in F.FAMILIES:
            raise UnknownOperation(f"{family!r} is not in the closed root op enum; refused")
        if phase not in PHASES:
            raise HelperRefusal(f"unknown phase {phase!r}")
        if self.context == DAEMON:
            raise SubstrateFamilyNotExposed(family)
        if self.context == SEAT:
            raise UnprivilegedContext(f"{family}: an unprivileged seat context never runs root families")
        if self.context == SCHEDULED and phase == ACTIVATE:
            raise ScheduledActivationRefused(f"{family}: a scheduled run may stage but never activate (ORG-080 A-3)")
        try:
            resolved = F.resolve(family, params, self.manifest)
        except F.ParamError as exc:
            raise PreflightRefused(str(exc)) from None
        return Step(resolved, phase)

    # --- preflight ---------------------------------------------------------

    def preflight(self, step: Step) -> Evidence:
        r = step.resolved
        snapshot = self.port.snapshot()
        if r.family == "group.ensure":
            name = r.subject.split(":", 1)[1]
            conflict = _group_collision(snapshot, name, r.after["gid"], private=False)
            if conflict:
                return Evidence(False, conflict)
        if r.family == "account.ensure":
            name = r.subject.split(":", 1)[1]
            conflict = _group_collision(snapshot, name, r.after["gid"], private=True)
            if conflict:
                return Evidence(False, conflict)
            entries = _passwd_entries(snapshot)
            spec = _ManifestAccount(name, r.after["uid"], home_path=r.after["home"], login_shell=r.after["shell"])
            verdict = classify(spec, {e.name: e for e in entries}, {e.uid: e for e in entries})
            if verdict["verdict"] in (CONFLICT, DRIFT):  # blocking, never clobbered (the reconciler's own rule)
                return Evidence(False, f"account {verdict['verdict']}: {verdict['detail']}")
        if r.family == "account.retire" and self.port.get(r.subject) is None:
            return Evidence(False, "account absent: nothing to retire")
        if r.family == "sudoers_fragment.ensure":
            current = self.port.get(r.subject)
            tracked = {r.after["sha256"], *(r.entry.get("prior_sha256") or [])}
            if current is not None and current.get("sha256") not in tracked:
                return Evidence(False, "untracked existing fragment at the template path; refused")
            verdict = self.port.visudo_check(r.after["sha256"])
            if verdict != "ok":
                return Evidence(False, f"visudo -cf reports {verdict}; refused")
        return Evidence(True, f"{r.family} admitted in {self.context} ({step.phase})")

    # --- apply / verify / restore -----------------------------------------

    def before(self, step: Step) -> Any:
        return self.port.get(step.subject)

    def apply(self, step: Step) -> Any:
        """Converge the subject; return its preimage. Sudoers: post-check, restore on failure."""

        evidence = self.preflight(step)
        if not evidence.ok:
            raise PreflightRefused(evidence.detail)
        before = self.port.get(step.subject)
        self.port.put(step.subject, step.resolved.after)
        if step.phase == ACTIVATE and step.resolved.family == "sudoers_fragment.ensure":
            check = self.sudo_l(step.resolved.entry)
            if not check.ok:
                self.port.put(step.subject, before)
                raise PreflightRefused(f"sudo -l evidence failed after install; restored: {check.detail}")
        return before

    def verify(self, step: Step) -> Evidence:
        current = self.port.get(step.subject)
        ok = current == step.resolved.after
        evidence = Evidence(ok, f"{step.subject} {'converged' if ok else 'drifted'}")
        if ok and step.phase == ACTIVATE and step.resolved.family == "sudoers_fragment.ensure":
            return self.sudo_l(step.resolved.entry)
        return evidence

    def restore(self, step: Step, before: Any) -> None:
        self.port.put(step.subject, before)

    def sudo_l(self, template: Mapping[str, Any]) -> Evidence:
        """Modelled ``sudo -l -U <grantee>``: positive for every template command,
        negative for ``NEGATIVE_PROBE``; computed from the installed fragments."""

        grantee = template["grantee"]
        allowed: set[str] = set()
        for t in self.manifest.section("sudoers_templates").values():
            current = self.port.get(f"file:{t['path']}")
            if t["grantee"] == grantee and current is not None and current.get("sha256") == F.sha256_text(F.sudoers_text(t)):
                allowed.update(t["commands"])
        missing = sorted(set(template["commands"]) - allowed)
        negative_ok = NEGATIVE_PROBE not in allowed
        ok = not missing and negative_ok
        detail = "sudo -l positive+negative evidence ok" if ok else f"missing {missing}, negative probe allowed={not negative_ok}"
        return Evidence(ok, detail, {"sudo_l_allowed": ",".join(sorted(allowed)), "sudo_l_negative": NEGATIVE_PROBE})
