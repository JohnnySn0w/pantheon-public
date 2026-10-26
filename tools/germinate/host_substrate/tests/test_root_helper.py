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
    "paths": {"/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": "0750", "acl": ["user::rwx", "group::r-x", "other::---", "group:pmail:r-x"]}},
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
        ("acl.converge", (("path", "/srv/pantheon/shared"),)): [["/usr/bin/setfacl", "--set", "group::r-x,group:pmail:r-x,other::---,user::rwx", "/srv/pantheon/shared"]],
        # T-5513: GNU chmod keeps a DIRECTORY's setuid/setgid bits for a 4-digit numeric mode
        # (2770 -> chmod 0000 -> 2000; 2770 -> chmod 0750 -> 2750). A fifth leading zero makes
        # the numeric mode exact, so chmod argv is always 5-digit.
        ("owner_mode.set", (("path", "/srv/pantheon/shared"),)): [
            ["/usr/bin/chown", "alice:pmail", "/srv/pantheon/shared"], ["/usr/bin/chmod", "00750", "/srv/pantheon/shared"]],
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
    acl = ["user::rwx", "group::r-x", "other::---", "group:pmail:r-x",
           "default:user::rwx", "default:group::r-x", "default:other::---", "default:group:pmail:r-x"]
    m = _acl_manifest(acl)
    (argv,) = P.apply_argv(F.resolve("acl.converge", {"path": "/srv/pantheon/shared"}, m))
    assert argv[:2] == ["/usr/bin/setfacl", "--set"] and set(argv[2].split(",")) == set(acl)


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
