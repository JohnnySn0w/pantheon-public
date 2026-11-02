"""T-5084 (germination GE): the root helper, its closed op enum and the dark host port.

Pure unit tests over in-memory manifests and the simulated port; nothing reads
or writes a real host path (tmp_path is used only for the lint's file reads).
"""

from __future__ import annotations

import ast
import re
import copy
from pathlib import Path

import pytest

from tools._organ_lifecycle import FakeCommandRunner
from tools.germinate.host_substrate import families as F
from tools.germinate.host_substrate import helper as Hp
from tools.germinate.host_substrate import invariants as I
from tools.germinate.host_substrate import ports as P
from tools.host_control.envelope import ORG082_SUBSTRATE_FAMILIES, OutOfEnvelopeKinds, require_executor_envelope

PKG = Path(__file__).resolve().parents[1]
HANDLERS = PKG.parent / "components" / "host-substrate_handlers.py"

BASE = {
    "schema": F.MANIFEST_SCHEMA,
    "groups": {"pmail": {"gid": 3001}},
    "accounts": {"alice": {"uid": 3101, "home": "/home/alice", "shell": "/bin/bash", "linger": True, "groups": ["pmail"]},
                 "bob": {"uid": 3102, "home": "/home/bob", "shell": "/bin/bash"}},
    "installer_roots": ["/srv/pantheon"],
    "paths": {"/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": "0750", "acl": ["user::rwx", "group::r-x", "other::---", "group:pmail:r-x", "mask::r-x"]}},
    "mailbox_dirs": {"alice": {"path": "/srv/pantheon/mbox-alice", "owner": "alice", "group": "pmail", "mode": "2770"}},
    "units": {"x.service": {"path": "/etc/systemd/system/x.service", "source": "x.service", "sha256": "a" * 64, "enable": True}},
    "dbus_policies": {},
    "sudoers_templates": {
        "alice-probe": {"id": "alice-probe", "path": "/etc/sudoers.d/pantheon-alice-probe", "grantee": "alice", "runas": "root",
                        "commands": ["/usr/local/sbin/pantheon-probe status"], "ticket": "T-5084", "expiry": "2026-12-31"}
    },
}


def manifest(**edits):
    doc = copy.deepcopy(BASE)
    for key, value in edits.items():
        doc[key] = value
    return F.load_manifest(doc)


class SpyPort(P.SimulatedSubstratePort):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def get(self, subject):
        self.calls += 1
        return super().get(subject)

    def put(self, subject, value):
        self.calls += 1
        super().put(subject, value)


def helper(context=Hp.BOOTSTRAP, m=None, port=None):
    return Hp.RootHelper(port if port is not None else SpyPort(), m or manifest(), context)


def sudo_params(m=None):
    t = (m or manifest()).section("sudoers_templates")["alice-probe"]
    return {"template": "alice-probe", "sha256": F.sha256_text(F.sudoers_text(t))}


# --- the closed enum ---------------------------------------------------------


def test_families_are_exactly_the_org082_substrate_families():
    assert F.FAMILIES == ORG082_SUBSTRATE_FAMILIES and set(F.PARAMS) == F.FAMILIES


@pytest.mark.parametrize("kind", ["shell.exec", "sudo", "account.delete", "file.atomic-write", "unit.user.install", ""])
def test_unknown_ops_are_refused_before_the_port_is_touched(kind):
    h = helper()
    with pytest.raises(Hp.UnknownOperation):
        h.admit(kind, {}, Hp.ACTIVATE)
    assert h.port.calls == 0


@pytest.mark.parametrize(
    "family, params",
    [
        ("account.ensure", {"account": "alice", "uid": "0"}),             # a caller-chosen uid
        ("account.ensure", {"account": "alice", "argv": "/bin/sh"}),      # argv
        ("unit.system.install", {"unit": "x.service", "state": "present", "sha256": "a" * 64, "content": "[Unit]"}),
        ("group.member", {"group": "pmail", "account": "alice", "state": "maybe"}),
        ("account.ensure", {"account": "mallory"}),                        # not a manifest account
        ("group.member", {"group": "pmail", "account": "bob", "state": "present"}),  # not allowlisted into pmail
        ("owner_mode.set", {"path": "/etc/shadow"}),                       # unregistered path
        ("unit.system.install", {"unit": "x.service", "state": "present", "sha256": "b" * 64}),  # pin mismatch
        ("sudoers_fragment.ensure", {"template": "alice-probe", "sha256": "0" * 64}),
        ("sudoers_fragment.ensure", {"template": "root-all", "sha256": "0" * 64}),
    ],
)
def test_values_outside_the_typed_schema_or_manifest_are_refused(family, params):
    h = helper()
    with pytest.raises(Hp.PreflightRefused):
        h.admit(family, params, Hp.ACTIVATE)
    assert h.port.calls == 0


@pytest.mark.parametrize(
    "edit, match",
    [
        ({"paths": {"/etc/passwd": {"owner": "root", "group": "root", "mode": "0644"}}}, "installer-owned root"),
        ({"accounts": {"r": {"uid": 0, "home": "/root", "shell": "/bin/bash"}}}, "uid"),
        ({"units": {"u": {"path": "/usr/lib/systemd/system/u.service", "source": "u", "sha256": "a" * 64}}}, "unit file"),
        ({"units": {"u": {"path": "/etc/systemd/system/u.service", "source": "/etc/u", "sha256": "a" * 64}}}, "repo-relative"),
        ({"accounts": {"alice": {"uid": 3101, "home": "/home/alice", "shell": "/usr/bin/zsh"}}}, "shell"),
        ({"paths": {"/srv/pantheon/x": {"owner": "alice", "group": "pmail", "mode": "rwx"}}}, "octal"),
    ],
)
def test_the_manifest_refuses_values_outside_the_allowlists(edit, match):
    with pytest.raises(F.ManifestError, match=match):
        manifest(**edit)


@pytest.mark.parametrize(
    "section, key, field",
    [
        ("groups", "pmail", "gidd"),
        ("accounts", "alice", "lingre"),
        ("paths", "/srv/pantheon/shared", "modd"),
        ("mailbox_dirs", "alice", "acl"),
        ("units", "x.service", "enablee"),
        ("dbus_policies", "org.fixture", "enable"),
        ("sudoers_templates", "alice-probe", "command"),
        ("legacy_requirements", 0, "optional"),
    ],
)
def test_every_manifest_item_schema_rejects_unknown_nested_fields(section, key, field):
    doc = copy.deepcopy(BASE)
    doc["dbus_policies"] = {
        "org.fixture": {
            "path": "/etc/dbus-1/system.d/org.fixture.conf",
            "source": "org.fixture.conf",
            "sha256": "b" * 64,
        }
    }
    doc["legacy_requirements"] = [{"path": "/usr/local/sbin/legacy", "note": "legacy fixture"}]
    entry = doc[section][key] if isinstance(key, str) else doc[section][key]
    entry[field] = True
    with pytest.raises(F.ManifestError, match=rf"{section}.*unknown fields.*{field}"):
        F.load_manifest(doc)


@pytest.mark.parametrize(
    "section, bad",
    [
        ("groups", []),
        ("accounts", []),
        ("installer_roots", {}),
        ("paths", []),
        ("mailbox_dirs", []),
        ("units", []),
        ("dbus_policies", []),
        ("sudoers_templates", []),
        ("legacy_requirements", {}),
    ],
)
def test_every_manifest_section_has_a_closed_container_type(section, bad):
    with pytest.raises(F.ManifestError, match=rf"{section}: must be a"):
        manifest(**{section: bad})


@pytest.mark.parametrize(
    "edit, match",
    [
        ({"groups": {"pmail": "3001"}}, r"groups\.pmail: must be a mapping"),
        ({"accounts": {"alice": "3101"}}, r"accounts\.alice: must be a mapping"),
        ({"paths": {"/srv/pantheon/shared": "0750"}}, r"paths\./srv/pantheon/shared: must be a mapping"),
        ({"mailbox_dirs": {"alice": "/srv/pantheon/mbox-alice"}}, r"mailbox_dirs\.alice: must be a mapping"),
        ({"units": {"x.service": "x.service"}}, r"units\.x.service: must be a mapping"),
        ({"dbus_policies": {"org.fixture": "org.fixture.conf"}}, r"dbus_policies\.org.fixture: must be a mapping"),
        ({"sudoers_templates": {"alice-probe": "/etc/sudoers.d/probe"}}, r"sudoers_templates\.alice-probe: must be a mapping"),
        ({"legacy_requirements": ["/usr/local/sbin/legacy"]}, r"legacy_requirements\[0\]: must be a mapping"),
        ({"accounts": {"alice": {"uid": 3101, "home": "/home/alice", "shell": "/bin/bash", "groups": "pmail"}}},
         r"accounts\.alice\.groups: must be a list"),
        ({"paths": {"/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": "0750", "acl": "group:pmail:r-x"}}},
         r"paths\./srv/pantheon/shared\.acl: must be a list"),
        ({"legacy_requirements": [{"path": "/usr/local/sbin/legacy", "note": 1}]},
         r"legacy_requirements\[0\]\.note: must be a non-empty string"),
    ],
)
def test_manifest_items_refuse_wrong_types_without_incidental_exceptions(edit, match):
    with pytest.raises(F.ManifestError, match=match):
        manifest(**edit)


@pytest.mark.parametrize(
    "edit, match",
    [
        ({"groups": {"pmail": {"gid": "3001"}}}, r"groups\.pmail: gid must be an int"),
        ({"accounts": {"alice": {"uid": "3101", "home": "/home/alice", "shell": "/bin/bash"}}},
         r"accounts\.alice: uid must be a unique int"),
        ({"accounts": {"alice": {"uid": 3101, "home": "/home/alice", "shell": "/bin/bash", "linger": "yes"}}},
         r"accounts\.alice\.linger: must be a boolean"),
        ({"accounts": {"alice": {"uid": 3101, "home": "/home/alice", "shell": "/bin/bash", "groups": [1]}}},
         r"accounts\.alice\.groups: every membership must be a group name string"),
        ({"installer_roots": [1]}, r"installer_roots\[0\]: 1 is not an absolute path"),
        ({"paths": {"/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": "0750", "acl": [{}]}}},
         r"ACL entry \{\} is not an allowlisted shape"),
        ({"units": {"x.service": {"path": "/etc/systemd/system/x.service", "source": "x.service", "sha256": "a" * 64,
                                   "enable": "yes"}}},
         r"units\.x.service\.enable: must be a boolean"),
        ({"sudoers_templates": {"alice-probe": {**BASE["sudoers_templates"]["alice-probe"], "prior_sha256": "a" * 64}}},
         r"sudoers_templates\.alice-probe\.prior_sha256: must be a list"),
        ({"accounts": {"alice": {"uid": 3101, "home": "/home/alice", "shell": []}}},
         r"accounts\.alice: shell \[\] is not allowlisted"),
        ({"paths": {"/srv/pantheon/shared": {"owner": [], "group": "pmail", "mode": "0750"}}},
         r"owner/group must be manifest accounts/groups or root"),
        ({"mailbox_dirs": {"alice": {"path": "/srv/pantheon/mbox-alice", "owner": "alice", "group": [], "mode": "2770"}}},
         r"owner/group must be manifest accounts/groups or root"),
        ({"sudoers_templates": {"alice-probe": {**BASE["sudoers_templates"]["alice-probe"], "grantee": []}}},
         r"grantee \[\] is not a manifest account"),
        ({"sudoers_templates": {"alice-probe": {**BASE["sudoers_templates"]["alice-probe"], "runas": []}}},
         r"run-as \[\] is neither root nor a manifest account"),
        ({"status": []}, r"status: must be a non-empty string"),
    ],
)
def test_manifest_scalar_and_nested_collection_types_fail_closed(edit, match):
    with pytest.raises(F.ManifestError, match=match):
        manifest(**edit)


def test_manifest_rejects_duplicate_and_private_group_id_collisions():
    with pytest.raises(F.ManifestError, match="duplicate gid"):
        manifest(groups={"pmail": {"gid": 3001}, "other": {"gid": 3001}})
    with pytest.raises(F.ManifestError, match="private-group gid"):
        manifest(groups={"pmail": {"gid": 3101}})


def test_non_string_unknown_nested_key_is_a_classified_manifest_error():
    doc = copy.deepcopy(BASE)
    doc["accounts"]["alice"][1] = True
    with pytest.raises(F.ManifestError, match=r"accounts\.alice: unknown fields"):
        F.load_manifest(doc)


@pytest.mark.parametrize("case", ["account-typo", "unit-typo", "legacy-string"])
def test_hostile_manifest_is_refused_before_a_root_helper_can_touch_state(case):
    doc = copy.deepcopy(BASE)
    if case == "account-typo":
        doc["accounts"]["alice"]["lingre"] = True
    elif case == "unit-typo":
        doc["units"]["x.service"]["enablee"] = True
    else:
        doc["legacy_requirements"] = ["/usr/local/sbin/legacy"]
    port = SpyPort()
    with pytest.raises(F.ManifestError):
        Hp.RootHelper(port, F.load_manifest(doc), Hp.BOOTSTRAP)
    assert port.calls == 0
    assert port.dump()["subjects"] == {}


@pytest.mark.parametrize(
    "section, key, required_field",
    [
        ("groups", "pmail", "gid"),
        ("accounts", "alice", "shell"),
        ("paths", "/srv/pantheon/shared", "mode"),
        ("mailbox_dirs", "alice", "path"),
        ("units", "x.service", "sha256"),
        ("dbus_policies", "org.fixture", "source"),
        ("sudoers_templates", "alice-probe", "commands"),
        ("legacy_requirements", 0, "note"),
    ],
)
def test_every_manifest_item_schema_refuses_missing_fields_before_helper_state(section, key, required_field):
    doc = copy.deepcopy(BASE)
    doc["dbus_policies"] = {
        "org.fixture": {
            "path": "/etc/dbus-1/system.d/org.fixture.conf",
            "source": "org.fixture.conf",
            "sha256": "b" * 64,
        }
    }
    doc["legacy_requirements"] = [{"path": "/usr/local/sbin/legacy", "note": "legacy fixture"}]
    doc[section][key].pop(required_field)
    port = SpyPort()
    with pytest.raises(F.ManifestError, match=rf"{section}.*missing fields.*{required_field}"):
        Hp.RootHelper(port, F.load_manifest(doc), Hp.BOOTSTRAP)
    assert port.calls == 0
    assert port.dump()["subjects"] == {}


@pytest.mark.parametrize(
    "tweak, match",
    [
        ({"commands": ["ALL"]}, "ALL"),
        ({"commands": ["/usr/bin/* status"]}, "exact absolute"),
        ({"commands": ["/usr/bin/env ALL"]}, "ALL"),
        ({"commands": ["/usr/bin/tee /etc/x; /bin/sh"]}, "exact absolute"),
        ({"grantee": "mallory"}, "grantee"),
        ({"runas": "postgres"}, "run-as"),
        ({"ticket": "none"}, "ticket"),
        ({"path": "/etc/sudoers.d/pantheon.probe"}, "no '.'"),
        ({"path": "/etc/sudoers"}, "direct child"),
    ],
)
def test_sudoers_templates_refuse_wide_or_untyped_grants(tweak, match):
    templates = copy.deepcopy(BASE["sudoers_templates"])
    templates["alice-probe"].update(tweak)
    with pytest.raises(F.ManifestError, match=match):
        manifest(sudoers_templates=templates)


# --- invocation context (ORG-082 §1, ORG-080 A-3) ------------------------------


@pytest.mark.parametrize("family", sorted(F.FAMILIES))
def test_the_broker_daemon_context_refuses_every_family_with_the_org082_payload(family):
    h = helper(Hp.DAEMON)
    with pytest.raises(Hp.SubstrateFamilyNotExposed) as info:
        h.admit(family, {}, Hp.ACTIVATE)
    assert info.value.payload() == {"code": "SUBSTRATE_FAMILY_NOT_EXPOSED", "family": family,
                                    "amendment": "ORG-082", "retry_context": "bootstrap-in-process-root"}
    assert info.value.dbus_name == "org.pantheon.HostBroker1.SubstrateFamilyNotExposed"
    assert h.port.calls == 0


def test_the_daemon_envelope_refuses_substrate_kinds_before_any_executor():
    with pytest.raises(OutOfEnvelopeKinds):
        require_executor_envelope(ORG082_SUBSTRATE_FAMILIES)


@pytest.mark.parametrize("phase", [Hp.STAGE, Hp.ACTIVATE])
def test_an_unprivileged_seat_context_never_reaches_root_families(phase):
    h = helper(Hp.SEAT)
    with pytest.raises(Hp.UnprivilegedContext):
        h.admit("account.ensure", {"account": "alice"}, phase)
    assert h.port.calls == 0


def test_a_scheduled_context_may_stage_but_never_activate():
    h = helper(Hp.SCHEDULED)
    with pytest.raises(Hp.ScheduledActivationRefused, match="A-3"):
        h.admit("account.ensure", {"account": "alice"}, Hp.ACTIVATE)
    step = h.admit("account.ensure", {"account": "alice"}, Hp.STAGE)
    h.apply(step)
    assert set(h.port.snapshot()) == {"staged:passwd:alice"}  # nothing live changed


def test_bootstrap_converges_and_restores():
    h = helper()
    step = h.admit("account.ensure", {"account": "alice"}, Hp.ACTIVATE)
    before = h.apply(step)
    assert before is None and h.verify(step).ok
    h.restore(step, before)
    assert h.port.snapshot() == {}


def test_account_conflict_and_drift_are_refused_never_clobbered():
    port = SpyPort()
    port.put("passwd:mallory", {"uid": 3101, "gid": 3101, "home": "/home/mallory", "shell": "/bin/bash", "locked": False})
    h = helper(port=port)
    step = h.admit("account.ensure", {"account": "alice"}, Hp.ACTIVATE)
    assert not h.preflight(step).ok
    port.state["subjects"] = {"passwd:alice": {"uid": 3101, "gid": 3101, "home": "/home/elsewhere", "shell": "/bin/bash", "locked": False}}
    assert "DRIFT" in h.preflight(step).detail


def test_group_gid_collision_is_refused_and_never_overwritten():
    port = SpyPort()
    port.put("group:pmail", {"gid": 3999})
    h = helper(port=port)
    step = h.admit("group.ensure", {"group": "pmail"}, Hp.ACTIVATE)
    evidence = h.preflight(step)
    assert (evidence.ok, evidence.detail) == (False, "group CONFLICT: pmail exists with gid 3999, expected 3001")
    with pytest.raises(Hp.PreflightRefused, match="group CONFLICT"):
        h.apply(step)
    assert port.get("group:pmail") == {"gid": 3999}


@pytest.mark.parametrize(
    "subjects, detail",
    [
        ({"group:alice": {"gid": 3101}},
         "private group CONFLICT: alice already exists with gid 3101; account.ensure requires creating it"),
        ({"group:alice": {"gid": 3999}}, "private group CONFLICT: alice exists with gid 3999, expected 3101"),
        ({"group:mallory": {"gid": 3101}}, "private group CONFLICT: gid 3101 is held by mallory"),
    ],
)
def test_account_private_group_collisions_are_refused(subjects, detail):
    port = SpyPort()
    port.load({"subjects": subjects})
    h = helper(port=port)
    step = h.admit("account.ensure", {"account": "alice"}, Hp.ACTIVATE)
    evidence = h.preflight(step)
    assert (evidence.ok, evidence.detail) == (False, detail)
    with pytest.raises(Hp.PreflightRefused, match="private group CONFLICT"):
        h.apply(step)
    assert port.snapshot() == subjects


def test_retire_locks_and_keeps_the_account():
    h = helper()
    h.apply(h.admit("account.ensure", {"account": "alice"}, Hp.ACTIVATE))
    h.apply(h.admit("account.retire", {"account": "alice"}, Hp.ACTIVATE))
    assert h.port.get("passwd:alice")["locked"] is True and h.port.get("passwd:alice")["home"] == "/home/alice"
    step = helper().admit("account.retire", {"account": "alice"}, Hp.ACTIVATE)
    assert not helper().preflight(step).ok  # nothing to retire on a fresh host


# --- sudoers (ORG-052 §8, gates 20 / 33) ----------------------------------------


def _sudo(m=None, port=None):
    m = m or manifest()
    h = helper(m=m, port=port)
    h.apply(h.admit("account.ensure", {"account": "alice"}, Hp.ACTIVATE))
    return h, h.admit("sudoers_fragment.ensure", sudo_params(m), Hp.ACTIVATE)


def test_sudoers_installs_0440_root_with_positive_and_negative_sudo_l_evidence():
    h, step = _sudo()
    h.apply(step)
    assert h.port.get("file:/etc/sudoers.d/pantheon-alice-probe") == {"sha256": sudo_params()["sha256"], "mode": "0440", "owner": "root"}
    ev = h.verify(step)
    assert ev.ok and ev.data["sudo_l_allowed"] == "/usr/local/sbin/pantheon-probe status" and ev.data["sudo_l_negative"] == "/bin/sh"


@pytest.mark.parametrize("verdict", ["warning", "error"])
def test_a_visudo_warning_or_error_refuses_and_changes_nothing(verdict):
    port = SpyPort()
    port.state["visudo"][sudo_params()["sha256"]] = verdict
    h, step = _sudo(port=port)
    with pytest.raises(Hp.PreflightRefused, match="visudo"):
        h.apply(step)
    assert h.port.get(step.subject) is None


def test_an_untracked_existing_fragment_is_refused_and_a_tracked_prior_is_replaced():
    h, step = _sudo()
    h.port.put(step.subject, {"sha256": "f" * 64, "mode": "0440", "owner": "root"})
    with pytest.raises(Hp.PreflightRefused, match="untracked"):
        h.apply(step)
    templates = copy.deepcopy(BASE["sudoers_templates"])
    templates["alice-probe"]["prior_sha256"] = ["f" * 64]
    m = manifest(sudoers_templates=templates)
    h2, step2 = _sudo(m=m, port=h.port)
    assert h2.apply(step2) == {"sha256": "f" * 64, "mode": "0440", "owner": "root"}  # the staged prior, for exact rollback


def test_a_fragment_that_would_grant_the_negative_probe_is_restored():
    templates = copy.deepcopy(BASE["sudoers_templates"])
    templates["alice-probe"]["commands"] = ["/bin/sh"]
    m = manifest(sudoers_templates=templates)
    h, step = _sudo(m=m)
    with pytest.raises(Hp.PreflightRefused, match="sudo -l"):
        h.apply(step)
    assert h.port.get(step.subject) is None


# --- the dark host port -----------------------------------------------------------


def test_live_actuation_is_structurally_disabled():
    assert P.LIVE_ACTUATION_ENABLED is False
    tree = ast.parse((PKG / "ports.py").read_text(encoding="utf-8"))
    assigns = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
               and any(isinstance(t, ast.Name) and t.id == "LIVE_ACTUATION_ENABLED" for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
    [only] = assigns
    assert isinstance(only.value, ast.Constant) and only.value.value is False
    assert not any(isinstance(n, ast.Global) for n in ast.walk(tree))
    for path in PKG.glob("*.py"):  # nothing else in the package writes it
        if path.name != "ports.py":
            assert "LIVE_ACTUATION_ENABLED" not in path.read_text(encoding="utf-8").replace("``LIVE_ACTUATION_ENABLED``", "")


def test_the_host_port_constructor_refuses_and_never_runs_a_command():
    runner = FakeCommandRunner()
    with pytest.raises(P.LiveActuationDisabled, match="dark"):
        P.HostSubstratePort(runner)
    assert runner.calls == []


def test_argv_is_fixed_lists_of_absolute_binaries():
    m = manifest()
    golden = {
        ("account.ensure", (("account", "alice"),)): [
            ["/usr/sbin/groupadd", "--gid", "3101", "alice"],
            ["/usr/sbin/useradd", "--uid", "3101", "--gid", "3101", "--home-dir", "/home/alice", "--create-home", "--shell", "/bin/bash", "alice"]],
        ("account.retire", (("account", "alice"),)): [["/usr/sbin/usermod", "--lock", "--expiredate", "1", "alice"]],
        ("group.member", (("account", "alice"), ("group", "pmail"), ("state", "present"))): [["/usr/bin/gpasswd", "--add", "alice", "pmail"]],
        ("linger.enable", (("account", "alice"),)): [["/usr/bin/loginctl", "enable-linger", "alice"]],
        ("unit.system.enable", (("state", "enabled"), ("unit", "x.service"))): [["/usr/bin/systemctl", "enable", "x.service"]],
        ("acl.converge", (("path", "/srv/pantheon/shared"),)): [["/usr/bin/setfacl", "--physical", "--set", "group::r-x,group:pmail:r-x,mask::r-x,other::---,user::rwx", "/srv/pantheon/shared"]],
        # T-5513: GNU chmod keeps a DIRECTORY's setuid/setgid bits for a 4-digit numeric mode
        # (2770 -> chmod 0000 -> 2000; 2770 -> chmod 0750 -> 2750). A fifth leading zero makes
        # the numeric mode exact, so chmod argv is always 5-digit.
        ("owner_mode.set", (("path", "/srv/pantheon/shared"),)): [
            ["/usr/bin/chown", "-h", "alice:pmail", "/srv/pantheon/shared"], ["/usr/bin/chmod", "00750", "/srv/pantheon/shared"]],
        ("mailbox_dir.retire", (("mailbox", "alice"),)): [["/usr/bin/chmod", "00000", "/srv/pantheon/mbox-alice"]],
    }
    for (family, params), want in golden.items():
        assert P.apply_argv(F.resolve(family, dict(params), m)) == want
    r = F.resolve("sudoers_fragment.ensure", sudo_params(m), m)
    argvs = P.apply_argv(r, staged_source="/run/pantheon/host-substrate/stage/abc")
    assert argvs[1] == ["/usr/sbin/visudo", "-cf", "/etc/sudoers.d/pantheon-alice-probe.pantheon-tmp"]
    assert argvs[-1] == ["/usr/bin/sudo", "-l", "-U", "alice"]
    with pytest.raises(ValueError, match="staged"):
        P.apply_argv(F.resolve("unit.system.install", {"unit": "x.service", "state": "present", "sha256": "a" * 64}, m))
    assert P.rollback_argv(F.resolve("account.ensure", {"account": "alice"}, m), None) == [["/usr/sbin/userdel", "alice"], ["/usr/sbin/groupdel", "alice"]]


def test_fixed_binaries_match_codex_recovery():
    from tools import codex_recovery

    assert (P.LOGINCTL, P.SYSTEMCTL) == (codex_recovery.LOGINCTL, codex_recovery.SYSTEMCTL)


_BANNED_MODULES = {"subprocess", "pty", "shlex", "pexpect", "multiprocessing"}
_BANNED_OS = {"system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp"}


@pytest.mark.parametrize("path", sorted(PKG.glob("*.py")) + [HANDLERS], ids=lambda p: p.name)
def test_the_package_and_handlers_import_no_subprocess_and_build_no_shell(path):
    """Actuation is only through the injected CommandRunner (tools/_organ_lifecycle.py)."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & _BANNED_MODULES, ast.dump(node)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in _BANNED_MODULES, node.module
            if node.module == "tools._organ_lifecycle":
                assert {a.name for a in node.names} <= {"CommandRunner"}  # never SubprocessCommandRunner
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
            assert node.attr not in _BANNED_OS and not node.attr.startswith(("exec", "spawn")), node.attr
        if isinstance(node, ast.keyword):
            assert node.arg != "shell", "shell= is never passed"


# --- invariants (CURRENT/approved, unwired: S1) -------------------------------------


def test_invariants_fail_on_a_fresh_host_and_pass_once_converged():
    m = manifest()
    assert not all(r["ok"] for r in I.evaluate(m, {}))
    converged = {r.subject: r.after for _cid, r in I._expect(m)}
    assert all(r["ok"] for r in I.evaluate(m, converged))


def test_invariant_list_ids_must_match_the_evaluator():
    with pytest.raises(I.InvariantListError):
        I.check_list({"schema": I.LIST_SCHEMA, "id": "x", "invariants": [{"id": "HS1-accounts"}]})


# --- T-5510: real-host argv correctness (found by the T-2918 sandbox) ----------------


def _acl_manifest(acl):
    return manifest(paths={"/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": "0750", "acl": acl}})


@pytest.mark.parametrize(
    "acl, missing",
    [
        (["group:pmail:r-x"], "user::, group::, other::"),
        (["user::rwx", "group::r-x", "group:pmail:r-x"], "other::"),
        (["user::rwx", "group::r-x", "other::---", "default:group:pmail:r-x"], "default:user::, default:group::, default:other::"),
    ],
)
def test_acl_without_base_entries_is_refused(acl, missing):
    """setfacl --set REPLACES the whole ACL, so named entries alone are a malformed ACL on a
    real host ('Missing or wrong entry'). The manifest must declare the base entries."""
    with pytest.raises(F.ManifestError, match="base entries.*" + re.escape(missing)):
        _acl_manifest(acl)


def test_acl_with_access_and_default_base_entries_is_accepted():
    acl = ["user::rwx", "group::r-x", "other::---", "group:pmail:r-x", "mask::r-x",
           "default:user::rwx", "default:group::r-x", "default:other::---", "default:group:pmail:r-x", "default:mask::r-x"]
    m = _acl_manifest(acl)
    (argv,) = P.apply_argv(F.resolve("acl.converge", {"path": "/srv/pantheon/shared"}, m))
    assert argv[:3] == ["/usr/bin/setfacl", "--physical", "--set"] and set(argv[3].split(",")) == set(acl)


def test_acl_converge_refuses_an_empty_entry_list():
    r = F.Resolved("acl.converge", "acl:/srv/pantheon/shared", {"entries": []}, {})
    with pytest.raises(ValueError, match="empty"):
        P.apply_argv(r)


def test_retire_rollback_unlocks_instead_of_recreating():
    """Re-applying the preimage resolves as account.ensure (groupadd/useradd on an existing
    account). The inverse of usermod --lock --expiredate 1 is unlock + clear the expiry."""
    m = manifest()
    r = F.resolve("account.retire", {"account": "alice"}, m)
    before = dict(r.after, locked=False)
    assert P.rollback_argv(r, before) == [["/usr/sbin/usermod", "--unlock", "--expiredate", "", "alice"]]


def test_retire_rollback_is_a_noop_when_the_preimage_was_already_locked():
    m = manifest()
    r = F.resolve("account.retire", {"account": "alice"}, m)
    assert P.rollback_argv(r, dict(r.after)) == []


def test_every_chmod_argv_is_an_exact_five_digit_mode():
    """T-5513 guard over the whole argv surface: a 4-digit chmod on a setgid directory leaves
    the setgid bit, and the helper's verify then reports drift."""
    m = manifest()
    resolved = [F.resolve("owner_mode.set", {"path": "/srv/pantheon/shared"}, m),
                F.resolve("mailbox_dir.retire", {"mailbox": "alice"}, m)]
    chmods = [argv for r in resolved for argv in P.apply_argv(r) if argv[0] == P.CHMOD]
    assert chmods and all(len(a[1]) == 5 and a[1][0] == "0" for a in chmods)


# --- T-2920 U3c: the full approved-argv surface (captured from main e1ce3dd42) ----------

_STAGED = "/run/pantheon/host-substrate/stage/abc"
_A, _B = "a" * 64, "b" * 64
_SUDO_SHA = "e61d5c8006f3dddffdd59ab6bfd626ca2c9a2a6981ed1281eb1f57f525551924"
#: Every family's apply_argv. Captured by diffing main's ports.py against this branch over
#: every family: ONLY acl.converge (+--physical) and owner_mode.set (chown +-h) differ
#: (T-5653, Recovery D15). Every other row is byte-identical to main.
FULL_SURFACE = [
    ("account.ensure", {"account": "alice"}, None, [
        ["/usr/sbin/groupadd", "--gid", "3101", "alice"],
        ["/usr/sbin/useradd", "--uid", "3101", "--gid", "3101", "--home-dir", "/home/alice", "--create-home", "--shell", "/bin/bash", "alice"]]),
    ("account.retire", {"account": "alice"}, None, [["/usr/sbin/usermod", "--lock", "--expiredate", "1", "alice"]]),
    ("group.ensure", {"group": "pmail"}, None, [["/usr/sbin/groupadd", "--gid", "3001", "pmail"]]),
    ("group.member", {"group": "pmail", "account": "alice", "state": "present"}, None, [["/usr/bin/gpasswd", "--add", "alice", "pmail"]]),
    ("group.member", {"group": "pmail", "account": "alice", "state": "absent"}, None, [["/usr/bin/gpasswd", "--delete", "alice", "pmail"]]),
    ("acl.converge", {"path": "/srv/pantheon/shared"}, None, [
        ["/usr/bin/setfacl", "--physical", "--set", "group::r-x,group:pmail:r-x,mask::r-x,other::---,user::rwx", "/srv/pantheon/shared"]]),
    ("owner_mode.set", {"path": "/srv/pantheon/shared"}, None, [
        ["/usr/bin/chown", "-h", "alice:pmail", "/srv/pantheon/shared"], ["/usr/bin/chmod", "00750", "/srv/pantheon/shared"]]),
    ("sudoers_fragment.ensure", {"template": "alice-probe", "sha256": _SUDO_SHA}, _STAGED, [
        ["/usr/bin/install", "-o", "root", "-g", "root", "-m", "0440", _STAGED, "/etc/sudoers.d/pantheon-alice-probe.pantheon-tmp"],
        ["/usr/sbin/visudo", "-cf", "/etc/sudoers.d/pantheon-alice-probe.pantheon-tmp"],
        ["/usr/bin/mv", "-f", "--", "/etc/sudoers.d/pantheon-alice-probe.pantheon-tmp", "/etc/sudoers.d/pantheon-alice-probe"],
        ["/usr/sbin/visudo", "-c"], ["/usr/bin/sudo", "-l", "-U", "alice"]]),
    ("sudoers_fragment.retire", {"template": "alice-probe"}, None, [
        ["/usr/bin/unlink", "/etc/sudoers.d/pantheon-alice-probe"], ["/usr/sbin/visudo", "-c"]]),
    ("unit.system.install", {"unit": "x.service", "state": "present", "sha256": _A}, _STAGED, [
        ["/usr/bin/install", "-o", "root", "-g", "root", "-m", "0644", _STAGED, "/etc/systemd/system/x.service"],
        ["/usr/bin/systemctl", "daemon-reload"]]),
    ("unit.system.install", {"unit": "x.service", "state": "absent", "sha256": _A}, None, [
        ["/usr/bin/unlink", "/etc/systemd/system/x.service"], ["/usr/bin/systemctl", "daemon-reload"]]),
    ("unit.system.enable", {"unit": "x.service", "state": "enabled"}, None, [["/usr/bin/systemctl", "enable", "x.service"]]),
    ("unit.system.enable", {"unit": "x.service", "state": "disabled"}, None, [["/usr/bin/systemctl", "disable", "x.service"]]),
    ("dbus_policy.install", {"policy": "p", "sha256": _B}, _STAGED, [
        ["/usr/bin/install", "-o", "root", "-g", "root", "-m", "0644", _STAGED, "/etc/dbus-1/system.d/org.pantheon.X.conf"],
        ["/usr/bin/systemctl", "reload", "dbus.service"]]),
    ("dbus_policy.retire", {"policy": "p"}, None, [
        ["/usr/bin/unlink", "/etc/dbus-1/system.d/org.pantheon.X.conf"], ["/usr/bin/systemctl", "reload", "dbus.service"]]),
    ("mailbox_dir.ensure", {"mailbox": "alice"}, None, [
        ["/usr/bin/install", "-d", "-o", "alice", "-g", "pmail", "-m", "2770", "/srv/pantheon/mbox-alice"]]),
    ("mailbox_dir.retire", {"mailbox": "alice"}, None, [["/usr/bin/chmod", "00000", "/srv/pantheon/mbox-alice"]]),
    ("linger.enable", {"account": "alice"}, None, [["/usr/bin/loginctl", "enable-linger", "alice"]]),
    ("linger.disable", {"account": "alice"}, None, [["/usr/bin/loginctl", "disable-linger", "alice"]]),
]


def _surface_manifest():
    doc = copy.deepcopy(BASE)
    doc["dbus_policies"] = {"p": {"path": "/etc/dbus-1/system.d/org.pantheon.X.conf", "source": "p.conf", "sha256": _B}}
    return F.load_manifest(doc)


def test_the_full_apply_argv_surface_is_pinned_for_every_family():
    m = _surface_manifest()
    assert {row[0] for row in FULL_SURFACE} == F.FAMILIES
    for family, params, staged, want in FULL_SURFACE:
        assert P.apply_argv(F.resolve(family, params, m), staged_source=staged) == want, family
    r = F.resolve("account.ensure", {"account": "alice"}, m)
    rr = F.resolve("account.retire", {"account": "alice"}, m)
    assert P.rollback_argv(r, None) == [["/usr/sbin/userdel", "alice"], ["/usr/sbin/groupdel", "alice"]]
    assert P.rollback_argv(rr, dict(rr.after, locked=False)) == [["/usr/sbin/usermod", "--unlock", "--expiredate", "", "alice"]]
    assert P.rollback_argv(rr, dict(rr.after)) == []


def test_the_two_ruled_argv_carry_physical_and_no_dereference_exactly_once():
    """T-5653 (Recovery, D15): setfacl --physical, chown -h; nothing else gains a flag."""
    m = _surface_manifest()
    (setfacl,) = P.apply_argv(F.resolve("acl.converge", {"path": "/srv/pantheon/shared"}, m))
    chown, chmod = P.apply_argv(F.resolve("owner_mode.set", {"path": "/srv/pantheon/shared"}, m))
    assert setfacl[:3] == [P.SETFACL, "--physical", "--set"] and setfacl.count("--physical") == 1
    assert chown[:2] == [P.CHOWN, "-h"] and chown.count("-h") == 1 and "--no-dereference" not in chown
    assert chmod == [P.CHMOD, "00750", "/srv/pantheon/shared"]
    flagged = [a for _f, _p, _s, argvs in FULL_SURFACE for a in argvs if "--physical" in a or "-h" in a]
    assert flagged == [setfacl, chown]


def test_the_residual_toctou_is_recorded_beside_the_argv():
    src = (PKG / "ports.py").read_text(encoding="utf-8")
    assert "RESIDUAL TOCTOU (T-5653" in src and "INTERMEDIATE path component" in src
    assert "installer roots (and every directory above a" in src


# --- T-2920 U3c: the T-5653 mask rule (Recovery, D15) -----------------------------------------

_B3 = ["user::rwx", "group::r-x", "other::---"]
_D3 = ["default:user::rwx", "default:group::r-x", "default:other::---"]


@pytest.mark.parametrize(
    "mode, acl, match",
    [
        ("0750", [*_B3, "mask::r-x"], "a lone mask:: "),
        ("0750", [*_B3, *_D3, "default:mask::r-x"], "a lone default:mask:: "),
        # T-5665 subsumes T-5653's at-most-one-mask check (one entry per prefix, tag, qualifier)
        ("0750", [*_B3, "group:pmail:r-x", "mask::r-x", "mask::r--"],
         r"'mask::r-x' and 'mask::r--' both set mask:: .*T-5665"),
        ("0750", [*_B3, "group:pmail:r-x", "mask::r-x", *_D3, "default:group:pmail:r-x", "default:mask::r-x",
                  "default:mask::rwx"], r"'default:mask::r-x' and 'default:mask::rwx' both set default:mask:: .*T-5665"),
        ("0750", [*_B3, "group:pmail:r-x", "mask::rwx"], r"mask::rwx must equal the mode's group bits r-x"),
        ("2770", [*_B3, "group:pmail:r-x", "mask::r-x"], r"mask::r-x must equal the mode's group bits rwx"),
    ],
    ids=["lone-mask", "lone-default-mask", "duplicate-mask", "duplicate-default-mask", "mask-rwx-mode-0750",
         "mask-vs-setgid-mode"],
)
def test_the_mask_rule_refuses(mode, acl, match):
    with pytest.raises(F.ManifestError, match=match):
        manifest(paths={"/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": mode, "acl": acl}})


@pytest.mark.parametrize(
    "mode, acl",
    [
        ("0750", [*_B3, "group:pmail:r-x", "mask::r-x"]),
        ("0770", [*_B3, "user:bob:rwx", "mask::rwx"]),
        ("2750", [*_B3, "group:pmail:r-x", "mask::r-x", *_D3, "default:user:bob:rwx", "default:mask::rwx"]),
        ("0700", [*_B3, "group:pmail:---", "mask::---"]),
    ],
    ids=["mask-equals-group-bits", "rwx", "default-mask-has-no-mode-constraint", "empty-mask"],
)
def test_the_mask_rule_accepts_a_mask_matching_the_mode(mode, acl):
    manifest(paths={"/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": mode, "acl": acl}})


def test_the_live_component_manifest_satisfies_the_mask_rule():
    import yaml

    doc = yaml.safe_load((PKG.parent / "components" / "host-substrate.manifest.yaml").read_text(encoding="utf-8"))
    F.load_manifest(doc)


# --- T-2920 U3c: the symlink guard (Recovery, T-5653 D15), simulated tier ---------------------

_GUARDED = [
    ("acl.converge", {"path": "/srv/pantheon/shared"}, "/srv/pantheon/shared"),
    ("owner_mode.set", {"path": "/srv/pantheon/shared"}, "/srv/pantheon/shared"),
    ("mailbox_dir.ensure", {"mailbox": "alice"}, "/srv/pantheon/mbox-alice"),
    ("mailbox_dir.retire", {"mailbox": "alice"}, "/srv/pantheon/mbox-alice"),
]


@pytest.mark.parametrize("family, params, path", _GUARDED, ids=[g[0] for g in _GUARDED])
@pytest.mark.parametrize("phase", [Hp.ACTIVATE, Hp.STAGE])
def test_a_symlink_subject_is_refused_in_preflight_loudly(family, params, path, phase):
    port = SpyPort()
    port.state["path_types"][path] = "symbolic link"
    h = helper(port=port)
    step = h.admit(family, params, phase)
    ev = h.preflight(step)
    assert not ev.ok and ev.data["code"] == "SYMLINK_SUBJECT_REFUSED"
    assert ev.detail.startswith("SYMLINK_SUBJECT_REFUSED: " + path + " is a symbolic link")
    with pytest.raises(Hp.SymlinkSubjectRefused, match="symbolic link") as exc:
        h.apply(step)
    assert exc.value.code == "SYMLINK_SUBJECT_REFUSED" and isinstance(exc.value, Hp.PreflightRefused)
    assert port.dump()["subjects"] == {}  # nothing was written


@pytest.mark.parametrize("family, params, path", _GUARDED, ids=[g[0] for g in _GUARDED])
def test_a_non_symlink_subject_passes_the_guard(family, params, path):
    port = SpyPort()
    port.state["path_types"][path] = "directory"
    h = helper(port=port)
    step = h.admit(family, params, Hp.ACTIVATE)
    h.apply(step)
    assert h.verify(step).ok


def test_unguarded_families_never_type_a_path():
    class Strict(SpyPort):
        def path_type(self, path):
            raise AssertionError("path_type called for an unguarded family")

    h = helper(port=Strict())
    for family, params in (("account.ensure", {"account": "alice"}), ("group.ensure", {"group": "pmail"}),
                           ("linger.enable", {"account": "alice"})):
        h.apply(h.admit(family, params, Hp.ACTIVATE))


def test_an_untypable_subject_fails_closed():
    class Broken(SpyPort):
        def path_type(self, path):
            raise P.ObservationError("stat exited 5")

    h = helper(port=Broken())
    step = h.admit("owner_mode.set", {"path": "/srv/pantheon/shared"}, Hp.ACTIVATE)
    ev = h.preflight(step)
    assert not ev.ok and "could not type the subject path" in ev.detail
    with pytest.raises(Hp.PreflightRefused, match="could not type"):
        h.apply(step)


@pytest.mark.parametrize("family, params, path", _GUARDED, ids=[g[0] for g in _GUARDED])
def test_restore_and_guarded_put_refuse_a_symlink_subject(family, params, path):
    """T-5653 'guard ALL path-taking actuation': a restore re-applies argv on the path."""
    port = SpyPort()
    h = helper(port=port)
    step = h.admit(family, params, Hp.ACTIVATE)
    before = h.apply(step)
    port.state["path_types"][path] = "symbolic link"  # swapped after apply
    after = port.dump()["subjects"]
    with pytest.raises(Hp.SymlinkSubjectRefused):
        h.restore(step, before)
    with pytest.raises(Hp.SymlinkSubjectRefused):
        h.guarded_put(step.subject, before)
    assert port.dump()["subjects"] == after


@pytest.mark.parametrize("ftype", ["symbolische Verknüpfung", "fifo", ""])
def test_a_type_outside_the_allowlist_is_refused_not_read_as_safe(ftype):
    """Review MED-1: the guard allowlists the C-locale %F texts; it never treats an
    unknown type as 'not a symlink'."""
    port = SpyPort()
    port.state["path_types"]["/srv/pantheon/shared"] = ftype
    h = helper(port=port)
    step = h.admit("owner_mode.set", {"path": "/srv/pantheon/shared"}, Hp.ACTIVATE)
    ev = h.preflight(step)
    assert not ev.ok and "unrecognised file type" in ev.detail
    with pytest.raises(Hp.PreflightRefused, match="unrecognised file type"):
        h.apply(step)
    assert port.dump()["subjects"] == {}


def test_simulated_path_types_load_backward_compatibly():
    port = P.SimulatedSubstratePort({"subjects": {"group:pmail": {"gid": 3001}}, "visudo": {}})
    assert port.path_type("/srv/pantheon/shared") is None and port.dump()["path_types"] == {}
    port.load({"subjects": {}, "visudo": {}, "path_types": {"/srv/x": "symbolic link"}})
    assert port.path_type("/srv/x") == "symbolic link"


# --- T-5657 (Recovery, D15): the ancestor walk on the simulated tier -----------------------


def test_simulated_path_stats_model_a_correct_host_with_explicit_overrides():
    port = P.SimulatedSubstratePort()
    assert port.path_stat("/srv") == P.SIMULATED_TRUSTED_DIR == ("directory", "root", "root", "0755")
    port.state["path_stats"]["/srv"] = ("directory", "bob", "bob", "0755")
    port.state["path_stats"]["/gone"] = None
    assert port.path_stat("/srv") == ("directory", "bob", "bob", "0755") and port.path_stat("/gone") is None
    port.put("owner_mode:/srv/pantheon", {"owner": "alice", "group": "pmail", "mode": "0770"})
    assert port.path_stat("/srv/pantheon") == ("directory", "alice", "pmail", "0770")  # a converged ancestor is seen


def test_simulated_path_stats_load_backward_compatibly():
    port = P.SimulatedSubstratePort({"subjects": {}, "visudo": {}, "path_types": {}})
    assert port.dump()["path_stats"] == {}
    port.load({"subjects": {}, "visudo": {}, "path_stats": {"/srv": ["directory", "root", "root", "0777"]}})
    assert port.path_stat("/srv") == ("directory", "root", "root", "0777")


def test_the_simulated_walk_sees_an_earlier_op_make_an_ancestor_untrusted():
    """No cache, on the wired tier: a manifest that converges an ancestor to a g+w mode
    makes every subject below it refuse in the SAME run."""
    m = manifest(paths={"/srv/pantheon": {"owner": "root", "group": "pmail", "mode": "0770"},
                        "/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": "0750"}})
    h = Hp.RootHelper(P.SimulatedSubstratePort(), m, Hp.BOOTSTRAP)
    h.apply(h.admit("owner_mode.set", {"path": "/srv/pantheon"}, Hp.ACTIVATE))
    with pytest.raises(Hp.AncestorRefused, match="ancestor /srv/pantheon is group-writable"):
        h.apply(h.admit("owner_mode.set", {"path": "/srv/pantheon/shared"}, Hp.ACTIVATE))


def test_the_component_rollback_path_is_walked_too():
    port = P.SimulatedSubstratePort()
    h = Hp.RootHelper(port, manifest(), Hp.BOOTSTRAP)
    port.state["path_stats"]["/srv"] = ("directory", "root", "root", "0757")
    with pytest.raises(Hp.AncestorRefused, match="ancestor /srv is other-writable"):
        h.guarded_put("acl:/srv/pantheon/shared", None)


# --- T-5665 (Recovery, D15): at most one ACL entry per (prefix, tag, qualifier) --------------


@pytest.mark.parametrize(
    "acl, first, second, key",
    [
        (["user::rwx", "user::r-x", "group::r-x", "other::---"], "user::rwx", "user::r-x", "user::"),
        (["user::rwx", "group::r-x", "other::---", "group:pmail:r-x", "group:pmail:r--", "mask::r-x"],
         "group:pmail:r-x", "group:pmail:r--", "group:pmail:"),
        (["user::rwx", "group::r-x", "other::---", "default:user::rwx", "default:user::r-x", "default:group::r-x",
          "default:other::---"], "default:user::rwx", "default:user::r-x", "default:user::"),
        (["user::rwx", "group::r-x", "group::rwx", "other::---", "group:pmail:r-x", "mask::r-x"],
         "group::r-x", "group::rwx", "group::"),
        (["user::rwx", "group::r-x", "other::---", "user:bob:rwx", "user:bob:rwx", "mask::rwx"],
         "user:bob:rwx", "user:bob:rwx", "user:bob:"),
    ],
    ids=["duplicate-user-base", "duplicate-named-qualifier", "duplicate-default", "two-group-base-with-named",
         "exact-duplicate"],
)
def test_two_acl_entries_with_one_key_are_refused_naming_both(acl, first, second, key):
    """Fails first on main (captured in the U3d report): all of these LOADED there; real
    setfacl --set keeps the last one, so verify would never converge."""
    with pytest.raises(F.ManifestError, match=re.escape(f"{first!r} and {second!r} both set {key} ") + ".*T-5665"):
        manifest(paths={"/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": "0750", "acl": acl}})


def test_the_same_qualifier_under_different_prefixes_or_tags_is_not_a_duplicate():
    manifest(paths={"/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": "0750", "acl": [
        "user::rwx", "group::r-x", "other::---", "user:alice:rwx", "group:alice:r-x", "mask::r-x",
        "default:user::rwx", "default:group::r-x", "default:other::---", "default:user:alice:rwx",
        "default:mask::rwx"]}})


@pytest.mark.parametrize(
    "entry",
    ["user:65534:r--", "group:3001:r-x", "default:user:65534:r--", "u:alice:rwx", "g::r-x", "o::---", "m::r-x",
     "d:user:alice:rwx", "d:u:alice:rwx", "default:u:alice:rwx", "user:Alice:rwx"],
)
def test_the_acl_grammar_admits_only_long_form_tags_and_name_qualifiers(entry):
    """T-5665 LOAD-BEARING PIN: the one-entry-per-key rule compares TEXTUAL keys, which is
    canonical only because _ACL_RE refuses numeric qualifiers and u:/g:/o:/m:/d: short
    forms. If the grammar ever widens, 'u:nobody' vs 'user:65534' would alias past it."""
    assert not F._ACL_RE.match(entry)
    with pytest.raises(F.ManifestError, match="is not an allowlisted shape"):
        manifest(paths={"/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": "0750",
                                                 "acl": ["user::rwx", "group::r-x", "other::---", entry]}})


def test_duplicate_mailbox_dir_paths_are_refused():
    """Review LOW-1: one mailbox_dirs entry per path, so a dir: subject has one owner."""
    dirs = {"alice": {"path": "/srv/pantheon/mbox", "owner": "alice", "group": "pmail", "mode": "2770"},
            "bob": {"path": "/srv/pantheon/mbox", "owner": "bob", "group": "pmail", "mode": "2770"}}
    with pytest.raises(F.ManifestError, match=r"mailbox_dirs\.bob\.path: '/srv/pantheon/mbox' duplicates mailbox_dirs\.alice"):
        manifest(mailbox_dirs=dirs)


def test_the_designated_owner_of_a_step_comes_from_its_own_resolved_entry():
    """Review LOW-1: with a step, D is ``step.resolved.entry``'s owner, never the first
    path match. A raw manifest (past load_manifest) carries two entries on one path."""
    doc = copy.deepcopy(BASE)
    doc["mailbox_dirs"] = {
        "alice": {"path": "/srv/pantheon/team/mbox", "owner": "alice", "group": "pmail", "mode": "2770"},
        "bob": {"path": "/srv/pantheon/team/mbox", "owner": "bob", "group": "pmail", "mode": "2770"},
    }
    m = F.Manifest(doc, "raw")
    port = P.SimulatedSubstratePort()
    port.state["path_stats"]["/srv/pantheon/team"] = ("directory", "bob", "pmail", "0750")
    h = Hp.RootHelper(port, m, Hp.BOOTSTRAP)
    step = h.admit("mailbox_dir.ensure", {"mailbox": "bob"}, Hp.ACTIVATE)
    assert h.designated_owner(step.resolved.subject, step.resolved.entry) == "bob"
    assert h.designated_owner(step.resolved.subject) == "alice"  # the path lookup's first match
    assert h.preflight(step).ok  # preflight passes the step's own entry
    alice = h.admit("mailbox_dir.ensure", {"mailbox": "alice"}, Hp.ACTIVATE)
    ev = h.preflight(alice)
    assert not ev.ok and "ancestor /srv/pantheon/team is owned by 'bob', not 'root' or 'alice'" in ev.detail


@pytest.mark.parametrize("ftype, why", [("symbolic link", "is a symbolic link"),
                                        ("regular file", "is a 'regular file', not a directory")])
def test_a_simulated_ancestor_typed_through_path_types_is_honoured(ftype, why):
    """Review LOW-3: a non-directory ``path_types`` entry wins in path_stat, so an
    ancestor modelled as a symlink never reads as a trusted directory."""
    port = P.SimulatedSubstratePort()
    port.state["path_types"]["/srv/pantheon"] = ftype
    assert port.path_stat("/srv/pantheon")[0] == ftype
    port.state["path_types"]["/srv"] = "directory"
    assert port.path_stat("/srv") == P.SIMULATED_TRUSTED_DIR
    h = Hp.RootHelper(port, manifest(), Hp.BOOTSTRAP)
    ev = h.preflight(h.admit("owner_mode.set", {"path": "/srv/pantheon/shared"}, Hp.ACTIVATE))
    assert not ev.ok and f"ancestor /srv/pantheon {why}" in ev.detail


def test_the_designated_owner_never_owns_the_root_directory():
    """A depth-1 subject's parent is "/": the D exception never extends to it."""
    m = manifest(installer_roots=["/srv"], paths={"/srv": {"owner": "alice", "group": "pmail", "mode": "0750"}})
    port = P.SimulatedSubstratePort()
    port.state["path_stats"]["/"] = ("directory", "alice", "pmail", "0755")
    h = Hp.RootHelper(port, m, Hp.BOOTSTRAP)
    assert h.designated_owner("owner_mode:/srv") == "alice"
    ev = h.ancestor_walk("owner_mode:/srv")
    assert ev is not None and "ancestor / is owned by 'alice', not 'root'" in ev.detail


def test_a_mailbox_hub_subject_with_a_post_office_owned_grandparent_is_refused_today():
    """KNOWN, VISIBLE behaviour (mason + Recovery D15, 2026-10-27): the designated-owner
    exception covers the leaf's PARENT only. The live hub is /home/post-office (post-office,
    0710) / mailbox (post-office, 0710), so a /home/post-office/mailbox/<persona> subject is
    refused at its GRANDPARENT. Recovery's later hub-layout ruling (tracked gap) changes this test."""
    doc = copy.deepcopy(BASE)
    doc["groups"] = {"pantheon-mailbox": {"gid": 3001}}
    doc["accounts"] = {"post-office": {"uid": 3101, "home": "/home/post-office", "shell": "/usr/sbin/nologin"}}
    doc["installer_roots"] = ["/home/post-office"]
    doc["paths"] = {}
    doc["mailbox_dirs"] = {"orchestrator": {"path": "/home/post-office/mailbox/orchestrator", "owner": "post-office",
                                     "group": "pantheon-mailbox", "mode": "2770"}}
    doc["sudoers_templates"] = {}
    m = F.load_manifest(doc)
    port = P.SimulatedSubstratePort()
    port.state["path_stats"].update({
        "/home/post-office": ("directory", "post-office", "pantheon-mailbox", "0710"),
        "/home/post-office/mailbox": ("directory", "post-office", "pantheon-mailbox", "0710"),
    })
    h = Hp.RootHelper(port, m, Hp.BOOTSTRAP)
    assert h.designated_owner("dir:/home/post-office/mailbox/orchestrator") == "post-office"
    ev = h.preflight(h.admit("mailbox_dir.ensure", {"mailbox": "orchestrator"}, Hp.ACTIVATE))
    assert not ev.ok and ev.data == {"code": Hp.AncestorRefused.code, "ancestor": "/home/post-office"}
    assert "ancestor /home/post-office is owned by 'post-office', not 'root'" in ev.detail
