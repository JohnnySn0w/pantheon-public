"""SandboxSubstratePort: the real host-substrate argv, run in the disposable host (T-2918).

Same interface as ``SimulatedSubstratePort`` (get / put / snapshot /
visudo_check), so the REAL ``RootHelper`` drives it unchanged. ``put`` maps a
subject back to its family and manifest entry and runs
``ports.apply_argv`` / ``ports.rollback_argv`` VERBATIM (the Recovery-approved
argv); ``get`` observes real state inside the sandbox. ``HostSubstratePort``
stays dark: this port never constructs it.

Two substitutions, both explicit:

* the trailing ``sudo -l -U <grantee>`` of the sudoers sequence is NEVER run
  (T-2918 ruling A1); ``cvtsudoers`` supplies "parser-derived" positive and
  negative evidence instead, recorded in ``self.sudo_evidence``;
* file bytes are staged by hash: the rendered template / pinned source for an
  install, and the PRIOR bytes captured just before any replacement, so a
  restore puts back exactly the bytes that were there (exact rollback).
"""

from __future__ import annotations

import copy
import hashlib
import json
import posixpath
from typing import Any, Mapping

from tools.germinate.host_substrate import families as F
from tools.germinate.host_substrate import helper as Hp
from tools.germinate.host_substrate import ports as P

from .launcher import Sandbox, SandboxRunner

CVTSUDOERS = "/usr/bin/cvtsudoers"
GETENT = "/usr/bin/getent"
STAT = "/usr/bin/stat"
GETFACL = "/usr/bin/getfacl"
SHA256SUM = "/usr/bin/sha256sum"
WANTS = "/etc/systemd/system/multi-user.target.wants/"
PARSER_SOURCE = "parser-derived (cvtsudoers; real sudo -l is T-2924's)"


class SandboxCommandFailed(RuntimeError):
    def __init__(self, argv, result):
        super().__init__(f"{argv[0]} exited {result.returncode}: {result.stderr.strip()[:300]}")
        self.argv = list(argv)
        self.result = result


class ArgvGap(RuntimeError):
    """The Recovery-approved argv has no command for this restore (T-5510 class)."""


class SandboxSubstratePort:
    def __init__(self, sandbox: Sandbox, manifest: F.Manifest, sources: Mapping[str, bytes] | None = None):
        self.sandbox = sandbox
        self.runner = SandboxRunner(sandbox)
        self.manifest = manifest
        self.staged: dict[str, Any] = {}
        self.sudo_evidence: list[dict[str, Any]] = []
        self.content: dict[str, bytes] = {}
        for t in manifest.section("sudoers_templates").values():
            data = F.sudoers_text(t).encode("utf-8")
            self.content[hashlib.sha256(data).hexdigest()] = data
        for sha, data in (sources or {}).items():
            if hashlib.sha256(data).hexdigest() != sha:
                raise ValueError("source bytes do not match their pin")
            self.content[sha] = data
        self._files: dict[str, tuple[str, str, Mapping[str, Any]]] = {}
        for section in ("units", "dbus_policies", "sudoers_templates"):
            for key, spec in manifest.section(section).items():
                self._files[spec["path"]] = (section, key, spec)
        self._dirs = {spec["path"]: (key, spec) for key, spec in manifest.section("mailbox_dirs").items()}
        self._enable = {posixpath.basename(spec["path"]): (key, spec) for key, spec in manifest.section("units").items()}

    # --- low level --------------------------------------------------------

    def _q(self, argv):
        return self.runner.run(argv)

    def _run_all(self, argvs, grantee: str | None = None) -> None:
        for argv in argvs:
            if argv[0] == P.SUDO:  # A1: never executed; parser-derived evidence instead
                self.sudo_evidence.append(self.parser_sudo_l(grantee or argv[-1]))
                continue
            result = self._q(argv)
            if result.returncode != 0:
                raise SandboxCommandFailed(argv, result)

    def parser_sudo_l(self, grantee: str) -> dict[str, Any]:
        """``cvtsudoers -f json -m user=<grantee> /etc/sudoers`` (follows the
        sudoers.d include): every command the policy grants that user, and
        whether the negative probe (``/bin/sh``) is granted."""

        result = self._q([CVTSUDOERS, "-f", "json", "-m", f"user={grantee}", "/etc/sudoers"])
        if result.returncode != 0:
            return {"grantee": grantee, "ok": False, "error": result.stderr.strip()[:300], "source": PARSER_SOURCE}
        doc = json.loads(result.stdout or "{}")
        allowed, runas = set(), set()
        for spec in doc.get("User_Specs") or []:
            users = {u.get("username") for u in spec.get("User_List") or []}
            if grantee not in users:
                continue
            for cmnd in spec.get("Cmnd_Specs") or []:
                for r in cmnd.get("runasusers") or []:
                    runas.add(r.get("username"))
                for c in cmnd.get("Commands") or []:
                    allowed.add(c.get("command"))
        return {
            "grantee": grantee,
            "ok": True,
            "allowed": sorted(allowed),
            "runas": sorted(runas),
            "negative_probe": Hp.NEGATIVE_PROBE,
            "negative_probe_allowed": Hp.NEGATIVE_PROBE in allowed or "ALL" in allowed,
            "source": PARSER_SOURCE,
        }

    def _stat(self, path: str) -> list[str] | None:
        result = self._q([STAT, "-c", "%U %G %a", path])
        return result.stdout.split() if result.returncode == 0 else None

    # --- the SimulatedSubstratePort interface -------------------------------

    def get(self, subject: str) -> Any:
        if subject.startswith(Hp.STAGED_PREFIX):
            return copy.deepcopy(self.staged.get(subject))
        kind, key = subject.split(":", 1)
        if kind == "passwd":
            result = self._q([GETENT, "passwd", key])
            if result.returncode != 0:
                return None
            _n, _x, uid, gid, _g, home, shell = result.stdout.strip().split(":")
            shadow = self._q([GETENT, "shadow", key]).stdout.strip().split(":")
            # "retired" == the account is EXPIRED (usermod --expiredate 1): that is
            # what disables key logins and cron/PAM jobs. The '!' prefix alone is
            # not a signal: Ubuntu useradd already writes '!' for a fresh account.
            return {"uid": int(uid), "gid": int(gid), "home": home, "shell": shell,
                    "locked": len(shadow) > 7 and shadow[7] == "1"}
        if kind == "group":
            result = self._q([GETENT, "group", key])
            return {"gid": int(result.stdout.split(":")[2])} if result.returncode == 0 else None
        if kind == "member":
            group, account = key.split(":", 1)
            result = self._q([GETENT, "group", group])
            if result.returncode != 0:
                return None
            members = result.stdout.strip().split(":")[3].split(",")
            return True if account in members else None
        if kind == "acl":
            result = self._q([GETFACL, "--omit-header", "--absolute-names", key])
            if result.returncode != 0:
                return None
            return {"entries": sorted(line.strip() for line in result.stdout.splitlines() if line.strip())}
        if kind in ("owner_mode", "dir"):
            st = self._stat(key)
            return None if st is None else {"owner": st[0], "group": st[1], "mode": st[2].zfill(4)}
        if kind == "linger":
            return True if self._stat(f"/var/lib/systemd/linger/{key}") is not None else None
        if kind == "enabled":
            return True if self._q([STAT, WANTS + key]).returncode == 0 else None
        if kind == "file":
            st = self._stat(key)
            if st is None:
                return None
            sha = self._q([SHA256SUM, key]).stdout.split()[0]
            return {"sha256": sha, "mode": st[2].zfill(4), "owner": st[0]}
        raise ValueError(f"unknown subject {subject!r}")

    def put(self, subject: str, value: Any) -> None:
        if subject.startswith(Hp.STAGED_PREFIX):  # staging never touches the host
            if value is None:
                self.staged.pop(subject, None)
            else:
                self.staged[subject] = copy.deepcopy(value)
            return
        kind, key = subject.split(":", 1)
        m = self.manifest
        R = F.Resolved
        if kind == "passwd":
            spec = m.section("accounts")[key]
            current = self.get(subject)
            if value is None:
                if current is not None:
                    self._run_all(P.rollback_argv(R("account.ensure", subject, None, spec), None))
                return
            if current is None:
                self._run_all(P.apply_argv(R("account.ensure", subject, value, spec)))
                current = self.get(subject)
            if value.get("locked") and not current["locked"]:
                self._run_all(P.apply_argv(R("account.retire", subject, value, spec)))
            elif not value.get("locked") and current["locked"]:
                try:  # restore of a retire: the unlock argv (T-5510, #1293)
                    argvs = P.rollback_argv(R("account.retire", subject, current, spec), value)
                except ValueError as exc:
                    raise ArgvGap(f"no argv restores account.retire (T-5510): {exc}") from None
                self._run_all(argvs)
            return
        if kind == "group":
            spec = m.section("groups")[key]
            if value is None:
                raise ArgvGap("no family removes a group")
            if self.get(subject) is None:
                self._run_all(P.apply_argv(R("group.ensure", subject, value, spec)))
            return
        if kind == "member":
            account = key.split(":", 1)[1]
            self._run_all(P.apply_argv(R("group.member", subject, value, m.section("accounts")[account])))
            return
        if kind in ("acl", "owner_mode"):
            spec = m.section("paths")[key]
            family = "acl.converge" if kind == "acl" else "owner_mode.set"
            self._run_all(P.apply_argv(R(family, subject, value, spec)))
            return
        if kind == "dir":
            if value is None:
                raise ArgvGap("no family removes a mailbox directory (gate 34)")
            _mb, spec = self._dirs[key]
            family = "mailbox_dir.retire" if value["mode"] == "0000" else "mailbox_dir.ensure"
            self._run_all(P.apply_argv(R(family, subject, value, spec)))
            return
        if kind == "linger":
            spec = m.section("accounts")[key]
            family = "linger.enable" if value else "linger.disable"
            self._run_all(P.apply_argv(R(family, subject, value, spec)))
            return
        if kind == "enabled":
            _u, spec = self._enable[key]
            self._run_all(P.apply_argv(R("unit.system.enable", subject, value, spec)))
            return
        if kind == "file":
            section, _key, spec = self._files[key]
            current = self.get(subject)
            if current is not None:  # stage the prior bytes for exact rollback
                prior = self.sandbox.read(key)
                self.content[hashlib.sha256(prior).hexdigest()] = prior
            if value is None:
                if current is None:
                    return
                family = {"units": "unit.system.install", "dbus_policies": "dbus_policy.retire",
                          "sudoers_templates": "sudoers_fragment.retire"}[section]
                self._run_all(P.apply_argv(R(family, subject, None, spec)))
                return
            data = self.content[value["sha256"]]
            staged = self.sandbox.stage(data)
            family = {"units": "unit.system.install", "dbus_policies": "dbus_policy.install",
                      "sudoers_templates": "sudoers_fragment.ensure"}[section]
            argvs = P.apply_argv(R(family, subject, value, spec), staged_source=staged)
            try:
                self._run_all(argvs, grantee=spec.get("grantee"))
            except SandboxCommandFailed:
                if family == "sudoers_fragment.ensure":  # never leave the temp fragment behind
                    self._q([P.UNLINK, key + ".pantheon-tmp"])
                raise
            return
        raise ValueError(f"unknown subject {subject!r}")

    def visudo_check(self, sha256: str) -> str:
        data = self.content.get(sha256)
        if data is None:
            return "error"
        path = self.sandbox.stage(data)
        result = self._q([P.VISUDO, "-cf", path])
        if result.returncode != 0:
            return "error"
        return "warning" if result.stderr.strip() else "ok"

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for line in self._q([GETENT, "passwd"]).stdout.splitlines():
            name, _x, uid, gid, _g, home, shell = line.split(":")
            out[f"passwd:{name}"] = {"uid": int(uid), "gid": int(gid), "home": home, "shell": shell}
        for line in self._q([GETENT, "group"]).stdout.splitlines():
            name, _x, gid = line.split(":")[:3]
            out[f"group:{name}"] = {"gid": int(gid)}
        return out
