"""RootHelper: the narrow root helper behind the host-substrate component (T-5084, GE).

Generalizes ORG-052 Unit 6b (T-2918): a CLOSED op enum, every value resolved
from the manifest, context admission before anything else runs.

Context admission (checked first, before any parameter is read):

* ``bootstrap-in-process-root`` -- the only context that may ACTIVATE
  (ORG-082 §1 / §3: substrate converges through the supervised operator-run leg).
* ``broker-daemon`` -- refused for every family, with the ORG-082 §2 payload
  (``SUBSTRATE_FAMILY_NOT_EXPOSED``, retry in ``bootstrap-in-process-root``).
* ``seat`` -- refused: an unprivileged context never reaches root families.
* ``scheduled``: refused for every family at every phase (strict ORG-080 A-3, HO ruling 2026-11-03;
  engine has no scheduled/root admission since #1564).

Engine seam gap S6 (T-5135): the engine passes no context and ignores
``execution_scope``; the component handler pins the context and maps
``staged-cutover`` to the stage phase. The engine now refuses scheduled+root at plan
time (engine/context.py ADMITTED); this refusal is defence in depth behind it.

CURRENT (co-author recovery; D15 approved 2026-11-03 for T-6048, strict-A-3 scheduled refusal; previously 71fd484cc): the admission rules are Recovery-approved.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any, Final, Mapping

from tools.ci_fleet_account_reconcile import CONFLICT, DRIFT, AccountSpec, PasswdEntry, classify

from . import families as F
from .ports import STAT_FILE_TYPES, SubstratePort

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


class ScheduledRootRefused(HelperRefusal):
    """ORG-080 A-3 (strict): a scheduled run never executes a root-privileged op, at any phase."""

    code = "SCHEDULED_ROOT_REFUSED"

    def __init__(self, family: str):
        super().__init__(f"{family}: a scheduled run never executes root-privileged ops, stage included (ORG-080 A-3)")
        self.family = family

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "family": self.family, "amendment": "ORG-080", "retry_context": BOOTSTRAP}


class PreflightRefused(HelperRefusal):
    code = "PREFLIGHT_REFUSED"


class SymlinkSubjectRefused(PreflightRefused):
    """T-5653 (Recovery, D15): root never re-ACLs, chowns or chmods through a symlink subject."""

    code = "SYMLINK_SUBJECT_REFUSED"


class AncestorRefused(PreflightRefused):
    """T-5657 (Recovery, D15): an ancestor of a path subject is not a trusted directory."""

    code = "ANCESTOR_REFUSED"


#: Evidence ``code`` -> the exception it raises (apply / restore / guarded_put).
_REFUSAL_CLASSES: Final = {c.code: c for c in (SymlinkSubjectRefused, AncestorRefused)}


def _ancestors(path: str) -> list[str]:
    """``/a/b/c`` -> ``["/", "/a", "/a/b"]``: top-down, ``/`` to the subject's parent."""

    parts = path.strip("/").split("/")[:-1]
    return ["/"] + ["/" + "/".join(parts[:i]) for i in range(1, len(parts) + 1)]


#: T-5653: the live subject kinds whose actuation takes a path, one-to-one with the
#: guarded families (acl.converge, owner_mode.set, mailbox_dir.ensure / .retire).
SYMLINK_GUARDED_KINDS: Final = {"acl": ("acl.converge",), "owner_mode": ("owner_mode.set",),
                                "dir": ("mailbox_dir.ensure", "mailbox_dir.retire")}
SYMLINK_TYPE: Final = "symbolic link"


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
    def __init__(self, port: SubstratePort, manifest: F.Manifest, context: str):
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
        if self.context == SCHEDULED:
            raise ScheduledRootRefused(family)
        try:
            resolved = F.resolve(family, params, self.manifest)
        except F.ParamError as exc:
            raise PreflightRefused(str(exc)) from None
        return Step(resolved, phase)

    # --- ancestor walk (T-5657) + symlink guard (T-5653) ----------------------

    def designated_owner(self, subject: str, entry: Mapping[str, Any] | None = None) -> str | None:
        """T-5657 (Recovery, D15): the ONE non-root owner the leaf's parent may have. It is the
        ``owner`` of the subject's OWN manifest entry: ``entry`` (``step.resolved.entry``,
        which ``families.resolve`` took from the manifest) where a step is available; else,
        for restore / guarded_put, the entry looked up by the subject path (``paths.<p>``
        for ``acl:`` and ``owner_mode:``; the ``mailbox_dirs`` entry with that path for
        ``dir:``, unique since load_manifest refuses duplicate paths). Never a step value or
        caller data. It counts only when it is a NON-ROOT, DECLARED manifest account;
        otherwise None (only root may own every ancestor)."""

        kind, path = subject.split(":", 1)
        if entry is None and kind in ("acl", "owner_mode"):
            entry = self.manifest.section("paths").get(path)
        elif entry is None:
            entry = next((s for s in self.manifest.section("mailbox_dirs").values() if s.get("path") == path), None)
        owner = entry.get("owner") if isinstance(entry, Mapping) else None
        if not isinstance(owner, str) or owner == "root" or owner not in self.manifest.section("accounts"):
            return None
        return owner

    def ancestor_walk(self, subject: str, entry: Mapping[str, Any] | None = None) -> Evidence | None:
        """T-5657: walk EVERY ancestor of a live path subject, ``/`` down to its parent, with
        the row-4 stat (no -L; ``port.path_stat``). Refuse unless each is present, a
        directory (never a symlink or another type), owned by root (the leaf's parent alone
        may instead be owned by ``designated_owner``) and not group/other-writable (that
        holds for the D-owned parent too). No cache: an earlier op in the same run can
        change an ancestor (U1's /etc/pantheon is both a subject and an ancestor)."""

        path = subject.split(":", 1)[1]
        parent = posixpath.dirname(path)
        d_owner = self.designated_owner(subject, entry)

        def refuse(anc: str, why: str) -> Evidence:
            return Evidence(False, f"{AncestorRefused.code}: {subject}: ancestor {anc} {why}; root never acts "
                                   f"below an untrusted directory (T-5657 D15)",
                            {"code": AncestorRefused.code, "ancestor": anc})

        for anc in _ancestors(path):
            try:
                st = self.port.path_stat(anc)
            except Exception as exc:  # noqa: BLE001 - any observation failure refuses
                return refuse(anc, f"could not be observed ({type(exc).__name__}: {str(exc)[:200]})")
            if st is None:
                return refuse(anc, "is absent")
            ftype, owner, _group, mode = st
            if ftype == SYMLINK_TYPE:
                return refuse(anc, "is a symbolic link")
            if ftype != "directory":
                return refuse(anc, f"is a {str(ftype)[:80]!r}, not a directory")
            # "/" itself is never D-owned, even when it is a depth-1 subject's parent.
            allowed = ("root", d_owner) if (anc == parent and anc != "/" and d_owner) else ("root",)
            if owner not in allowed:
                return refuse(anc, f"is owned by {owner!r}, not {' or '.join(map(repr, allowed))}")
            try:
                bits = int(mode, 8)
            except (TypeError, ValueError):
                return refuse(anc, f"has an unparseable mode {str(mode)[:20]!r}")
            # mode & 0o022 must be 0. Sticky is no exemption: a 1777 /tmp is refused.
            if bits & 0o020:
                return refuse(anc, f"is group-writable (mode {mode})")
            if bits & 0o002:
                return refuse(anc, f"is other-writable (mode {mode})")
        return None

    def symlink_guard(self, subject: str, entry: Mapping[str, Any] | None = None) -> Evidence | None:
        """The path guard BEFORE any path-taking actuation. First the T-5657 ancestor walk
        (stat without -L follows an intermediate symlink, so the subject's own type means
        nothing until its ancestors are proven), then the T-5653 subject typing with the
        row-4 stat: a symlink is REFUSED with a named reason, never skipped. An
        unobservable type refuses too (fail closed). Staged subjects never touch the host,
        and other subject kinds take no path argv; both return None (nothing to guard)."""

        if subject.startswith(STAGED_PREFIX) or ":" not in subject:
            return None
        kind, path = subject.split(":", 1)
        if kind not in SYMLINK_GUARDED_KINDS:
            return None
        refusal = self.ancestor_walk(subject, entry)
        if refusal is not None:
            return refusal
        try:
            ftype = self.port.path_type(path)
        except Exception as exc:  # noqa: BLE001 - any observation failure refuses
            return Evidence(False, f"{subject}: could not type the subject path ({type(exc).__name__}: {exc}); refused",
                            {"code": PreflightRefused.code})
        if ftype is not None and ftype not in STAT_FILE_TYPES:  # review MED-1: allowlist, never "not a symlink"
            return Evidence(False, f"{subject}: unrecognised file type {str(ftype)[:80]!r} (not a C-locale "
                                   f"allowlisted %F); refused", {"code": PreflightRefused.code})
        if ftype == SYMLINK_TYPE:
            return Evidence(False, f"{SymlinkSubjectRefused.code}: {path} is a symbolic link; root never "
                                   f"re-ACLs/chowns/chmods a symlink target (T-5653 D15)",
                            {"code": SymlinkSubjectRefused.code})
        return None

    def _require_not_symlink(self, subject: str) -> None:
        refusal = self.symlink_guard(subject)
        if refusal is not None:
            raise _REFUSAL_CLASSES.get(refusal.data.get("code"), PreflightRefused)(refusal.detail)

    # --- preflight ---------------------------------------------------------

    def preflight(self, step: Step) -> Evidence:
        r = step.resolved
        if any(r.family in families for families in SYMLINK_GUARDED_KINDS.values()):
            refusal = self.symlink_guard(r.subject, r.entry)  # the LIVE subject, in both phases
            if refusal is not None:
                return refusal
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
            raise _REFUSAL_CLASSES.get(evidence.data.get("code"), PreflightRefused)(evidence.detail)
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
        # T-5653 "guard ALL path-taking actuation": a restore re-applies argv on the path too.
        self._require_not_symlink(step.subject)
        self.port.put(step.subject, before)

    def guarded_put(self, subject: str, value: Any) -> None:
        """A rollback write (the component handler's rollback_mutation) under the same guard."""

        self._require_not_symlink(subject)
        self.port.put(subject, value)

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
