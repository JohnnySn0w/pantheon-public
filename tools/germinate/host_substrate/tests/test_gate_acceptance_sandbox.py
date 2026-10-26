"""T-2918: disposable-host acceptance of the GE root helper (ORG-052 gates 17, 19, 20, 33, 34, 36).

The REAL ``RootHelper`` drives the REAL Recovery-approved argv (``ports.apply_argv`` /
``rollback_argv``) against the userns sandbox described in ``sandbox/__init__``.
Tier honesty (ORG-052 gate 8):

* accounts, groups, memberships, owner/mode, ACLs, mailbox dirs, unit and D-Bus
  policy FILES, sudoers fragments, visudo: REAL binaries on a private /etc;
* linger and unit enable/reload: FAKE loginctl/systemctl ("fake-port tier");
* ``sudo -l``: PARSER-DERIVED via cvtsudoers (T-2918 ruling A1); real ``sudo -l``,
  real systemd and a real system bus are T-2924's.

Every sandbox test skips where the sandbox cannot run (CI included); the
real-host guards in conftest.py and test_real_host_guards.py never skip.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tools.germinate.host_substrate import families as F
from tools.germinate.host_substrate import helper as Hp
from tools.germinate.host_substrate import invariants as I
from tools.germinate.host_substrate import ports as P

from .sandbox import launcher
from .sandbox.port import PARSER_SOURCE, ArgvGap, SandboxSubstratePort

AVAILABLE, WHY = launcher.availability()
needs_sandbox = pytest.mark.skipif(not AVAILABLE, reason=WHY or "sandbox unavailable")

UNIT_BYTES = b"[Unit]\nDescription=T-2918 fixture unit\n[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=multi-user.target\n"
DBUS_BYTES = b'<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"\n "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">\n<busconfig/>\n'
UNIT_SHA = hashlib.sha256(UNIT_BYTES).hexdigest()
DBUS_SHA = hashlib.sha256(DBUS_BYTES).hexdigest()
TEMPLATE = {"id": "alice-probe", "path": "/etc/sudoers.d/pantheon-alice-probe", "grantee": "alice", "runas": "root",
            "commands": ["/usr/local/sbin/pantheon-probe status"], "ticket": "T-2918", "expiry": "2026-12-31"}
PRIOR_TEMPLATE = {**TEMPLATE, "commands": ["/usr/local/sbin/pantheon-probe version"]}
ACL = ["user::rwx", "group::r-x", "group:pmail:r-x", "mask::r-x", "other::---"]  # base entries declared (T-5510 contract)

MANIFEST = {
    "schema": F.MANIFEST_SCHEMA,
    "groups": {"pmail": {"gid": 3001}},
    "accounts": {"alice": {"uid": 3101, "home": "/home/alice", "shell": "/bin/bash", "linger": True, "groups": ["pmail"]}},
    "installer_roots": ["/srv/pantheon"],
    "paths": {"/srv/pantheon/shared": {"owner": "alice", "group": "pmail", "mode": "0750", "acl": ACL}},
    "mailbox_dirs": {"alice": {"path": "/srv/pantheon/mbox-alice", "owner": "alice", "group": "pmail", "mode": "2770"}},
    "units": {"t2918.service": {"path": "/etc/systemd/system/t2918.service", "source": "fixture/t2918.service",
                                "sha256": UNIT_SHA, "enable": True}},
    "dbus_policies": {"t2918": {"path": "/etc/dbus-1/system.d/org.pantheon.T2918Fixture.conf",
                                "source": "fixture/t2918.conf", "sha256": DBUS_SHA}},
    "sudoers_templates": {"alice-probe": TEMPLATE},
}
SOURCES = {UNIT_SHA: UNIT_BYTES, DBUS_SHA: DBUS_BYTES}
SHA = F.sha256_text(F.sudoers_text(TEMPLATE))
FRAG = TEMPLATE["path"]

#: (family, params) in bootstrap order.
BOOTSTRAP_OPS = [
    ("group.ensure", {"group": "pmail"}),
    ("account.ensure", {"account": "alice"}),
    ("group.member", {"group": "pmail", "account": "alice", "state": "present"}),
    ("owner_mode.set", {"path": "/srv/pantheon/shared"}),
    ("acl.converge", {"path": "/srv/pantheon/shared"}),
    ("mailbox_dir.ensure", {"mailbox": "alice"}),
    ("linger.enable", {"account": "alice"}),
    ("unit.system.install", {"unit": "t2918.service", "state": "present", "sha256": UNIT_SHA}),
    ("unit.system.enable", {"unit": "t2918.service", "state": "enabled"}),
    ("dbus_policy.install", {"policy": "t2918", "sha256": DBUS_SHA}),
    ("sudoers_fragment.ensure", {"template": "alice-probe", "sha256": SHA}),
]
WATCHED = ["/etc/sudoers.d", "/etc/systemd/system", "/etc/dbus-1/system.d", "/srv", "/var/lib/systemd/linger", "/home"]


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    if not AVAILABLE:
        pytest.skip(WHY)
    sb = launcher.Sandbox(tmp_path_factory.mktemp("t2918-sandbox"))
    yield sb
    sb.close()


@pytest.fixture
def sb(sandbox):
    sandbox.reset()
    sandbox.mkdir("/srv/pantheon/shared")
    return sandbox


def make(sb, manifest=MANIFEST, context=Hp.BOOTSTRAP):
    m = F.load_manifest(manifest)
    port = SandboxSubstratePort(sb, m, SOURCES)
    return Hp.RootHelper(port, m, context), port


def state(sb):
    """Everything the families can touch, as observed inside the sandbox."""

    return {
        "trees": {path: sb.tree(path) for path in WATCHED},
        "passwd": sb.run(["/usr/bin/getent", "passwd"]).stdout,
        "group": sb.run(["/usr/bin/getent", "group"]).stdout,
        "shadow": sb.run(["/usr/bin/getent", "shadow"]).stdout,
        "sudoers_d": sb.tree("/etc/sudoers.d"),
    }


def converge(helper, ops=BOOTSTRAP_OPS, phase=Hp.ACTIVATE):
    done = []
    for family, params in ops:
        step = helper.admit(family, params, phase)
        before = helper.apply(step)
        evidence = helper.verify(step)
        assert evidence.ok, (family, evidence.detail)
        done.append((step, before))
    return done


# --- the fixture itself -----------------------------------------------------------


@needs_sandbox
def test_the_sandbox_is_a_private_userns_with_a_private_etc(sb):
    who = sb.call({"op": "whoami"})
    assert who["uid"] == 0
    first = who["uid_map"].split()
    assert first[0] == "0" and first[1] != "0"  # namespace root is NOT the real root
    with pytest.raises(launcher.SandboxError, match="not allowlisted"):
        sb.run([P.SUDO, "-l"])  # A1: the agent never runs sudo
    # review r2a: the agent runs from the sandbox root, never pytest's real cwd
    assert sb.run(["/usr/bin/python3", "-I", "-c", "import os; print(os.getcwd())"]).stdout.strip() == "/mnt"
    with pytest.raises(launcher.SandboxError, match="relative path"):
        sb.run([P.CHMOD, "0644", "anything"])


# --- E17 ----------------------------------------------------------------------------------


@needs_sandbox
def test_e17_forbidden_values_never_reach_the_sandbox(sb):
    helper, port = make(sb)
    before = state(sb)
    forbidden = ("shell", "argv", "executable", "path", "content", "env", "sudo_target", "uid", "group",
                 "unit_text", "sudoers_text", "helper_op", "credential")
    for family, params in BOOTSTRAP_OPS:
        for field in forbidden:
            hostile = {**params, field: "/bin/sh -c id"}
            if field in params:  # a legit key carrying a hostile value instead
                hostile[field] = "/bin/sh -c id"
            with pytest.raises(Hp.HelperRefusal):
                helper.admit(family, hostile, Hp.ACTIVATE)
    with pytest.raises(Hp.UnknownOperation):
        helper.admit("shell.exec", {}, Hp.ACTIVATE)
    assert port.runner.calls == []
    assert state(sb) == before


# --- convergence and exact rollback (the gate 32 shape, bootstrap context) ----------------


@needs_sandbox
def test_bootstrap_converges_every_family_and_the_invariants_hold(sb):
    helper, port = make(sb)
    converge(helper)
    m = helper.manifest
    observed = {r.subject: port.get(r.subject) for _cid, r in I._expect(m)}
    results = I.evaluate(m, observed)
    assert all(r["ok"] for r in results), results
    assert sb.read("/var/lib/systemd/linger/alice") == b""  # fake-port tier
    assert any(call[0] == P.SETFACL and call[2] == ",".join(sorted(ACL)) for call in port.runner.calls)


@needs_sandbox
def test_restorable_families_roll_back_exactly(sb):
    helper, port = make(sb)
    converge(helper, BOOTSTRAP_OPS[:2])  # group + account are prerequisites
    restorable = [op for op in BOOTSTRAP_OPS[2:] if op[0] != "mailbox_dir.ensure"]
    for family, params in restorable:
        unit_file = None
        if family == "unit.system.enable":  # the unit file must exist to be enabled
            [(unit_file, _absent)] = converge(helper, [BOOTSTRAP_OPS[7]])
        before_state = state(sb)
        step = helper.admit(family, params, Hp.ACTIVATE)
        before = helper.apply(step)
        assert helper.verify(step).ok, family
        helper.restore(step, before)
        assert port.get(step.subject) == before, family
        assert state(sb)["trees"] == before_state["trees"], family
        assert state(sb)["group"] == before_state["group"], family
        if unit_file is not None:
            helper.restore(unit_file, None)  # and remove the prerequisite unit file again


@needs_sandbox
def test_mailbox_and_group_removal_have_no_argv(sb):
    """Gate 34 / closed enum: there is no family that deletes a mailbox dir or a group."""

    helper, port = make(sb)
    done = converge(helper, BOOTSTRAP_OPS[:6])
    for step, before in done:
        if step.resolved.family in ("mailbox_dir.ensure", "group.ensure"):
            with pytest.raises(ArgvGap):
                helper.restore(step, before)


# --- E19 ----------------------------------------------------------------------------------

_BROKER_LOAD = (
    "import sys; sys.dont_write_bytecode = True; sys.path.insert(0, {repo!r});"
    "from tools.host_control.runtime_table import load_daemon_runtime, DaemonStartRefused\n"
    "try:\n    rt = load_daemon_runtime(host_operation_families={{'persona.install'}})\n"
    "    print('LOADED', rt.loaded_op_registry_digest)\n"
    "except DaemonStartRefused as exc:\n    print('REFUSED', exc)\n"
)


@needs_sandbox
def test_e19_the_broker_loads_unprivileged_and_refuses_namespace_root(sb):
    code = _BROKER_LOAD.format(repo="/mnt/repo")  # the read-only repo view (enter.sh)
    # the broker's dedicated service account (dist unit: User=pantheon-hostbroker)
    made = sb.run([P.USERADD, "--uid", "3201", "--user-group", "--no-create-home", "--home-dir", "/nonexistent",
                   "--shell", "/usr/sbin/nologin", "pantheon-hostbroker"])
    assert made.returncode == 0, made
    as_root = sb.run(["/usr/bin/python3", "-I", "-c", code])
    assert as_root.stdout.startswith("REFUSED") and "uid 0" in as_root.stdout, as_root
    as_broker = sb.run(["/usr/bin/setpriv", "--reuid", "3201", "--regid", "3201", "--clear-groups",
                        "/usr/bin/python3", "-I", "-c", code])
    assert as_broker.stdout.startswith("LOADED sha256:"), as_broker
    from tools.host_control.runtime_table import load_checkout_op_registry

    assert as_broker.stdout.split()[1] == load_checkout_op_registry().digest  # the engine's registry (review B1)
    # the broker uid cannot write a helper-owned path, and holds no supplementary groups
    write = sb.run(["/usr/bin/setpriv", "--reuid", "3201", "--regid", "3201", "--clear-groups",
                    P.INSTALL, "-m", "0440", "/dev/null", "/etc/sudoers.d/pantheon-broker-was-here"])
    assert write.returncode != 0
    groups = sb.run(["/usr/bin/setpriv", "--reuid", "3201", "--regid", "3201", "--clear-groups",
                     "/usr/bin/python3", "-I", "-c", "import os; print(os.getgroups())"])
    assert groups.stdout.strip() == "[]"


@needs_sandbox
def test_e19_the_root_helper_leaves_both_journals_untouched(sb):
    for name, uid in (("broker", 3201), ("approval", 3202)):
        sb.mkdir(f"/srv/journals/{name}")
        sb.write(f"/srv/journals/{name}/journal.sqlite3", f"{name} journal bytes".encode(), 0o600, uid, uid)
    before = sb.tree("/srv/journals")
    helper, port = make(sb)
    converge(helper)
    assert sb.tree("/srv/journals") == before
    assert not any("/srv/journals" in " ".join(call) for call in port.runner.calls)


# --- E20 ----------------------------------------------------------------------------------


@needs_sandbox
def test_e20_sudoers_apply_runs_the_exact_sequence_with_parser_derived_evidence(sb):
    helper, port = make(sb)
    converge(helper, BOOTSTRAP_OPS[:2])
    port.runner.calls.clear()
    step = helper.admit("sudoers_fragment.ensure", {"template": "alice-probe", "sha256": SHA}, Hp.ACTIVATE)
    helper.apply(step)
    mutating = [c for c in port.runner.calls if c[0] in (P.INSTALL, P.VISUDO, P.MV)]
    tmp = FRAG + ".pantheon-tmp"
    assert mutating[-4:] == [
        (P.INSTALL, "-o", "root", "-g", "root", "-m", "0440", mutating[-4][7], tmp),
        (P.VISUDO, "-cf", tmp),
        (P.MV, "-f", "--", tmp, FRAG),
        (P.VISUDO, "-c"),
    ]
    assert not any(c[0] == P.SUDO for c in port.runner.calls)  # A1
    assert port.get(f"file:{FRAG}") == {"sha256": SHA, "mode": "0440", "owner": "root"}
    assert sb.read(FRAG) == F.sudoers_text(TEMPLATE).encode()
    parsed = port.sudo_evidence[-1]
    assert parsed["source"] == PARSER_SOURCE and parsed["ok"]
    assert parsed["allowed"] == TEMPLATE["commands"] and parsed["runas"] == ["root"]
    assert parsed["negative_probe_allowed"] is False
    assert helper.verify(step).ok


@needs_sandbox
def test_e20_real_visudo_errors_and_warnings_fail_closed(sb):
    helper, port = make(sb)
    converge(helper, BOOTSTRAP_OPS[:2])
    step = helper.admit("sudoers_fragment.ensure", {"template": "alice-probe", "sha256": SHA}, Hp.ACTIVATE)
    for bad, verdict in (
        (b"alice ALL=(root) NOPASSWD /usr/local/sbin/pantheon-probe status\nbogus line\n", "error"),
        (b"alice ALL=(root) NOPASSWD: /usr/local/sbin/pantheon-probe status\nCmnd_Alias UNUSED = /bin/true\n", "warning"),
    ):
        port.content[SHA] = bad  # what real visudo would see if the rendering went wrong
        assert port.visudo_check(SHA) == verdict
        before = sb.tree("/etc/sudoers.d")
        with pytest.raises(Hp.PreflightRefused, match=verdict):
            helper.apply(step)
        assert sb.tree("/etc/sudoers.d") == before


def test_e20_malformed_and_overbroad_templates_are_refused_before_any_host():
    """The manifest gate (no host needed): every gate-20 shape fails closed."""

    for tweak in (
        {"commands": ["ALL"]}, {"commands": ["/usr/bin/x ALL"]}, {"commands": ["/usr/bin/* --x"]},
        {"commands": ["/usr/bin/x; id"]}, {"commands": ["/usr/bin/x $(id)"]}, {"commands": ["relative/cmd"]},
        {"commands": []}, {"grantee": "mallory"}, {"runas": "mallory"}, {"ticket": "none"},
        {"expiry": "never"}, {"id": "other-id"}, {"unknown_field": "x"},
    ):
        with pytest.raises(F.ManifestError):
            F.load_manifest({**MANIFEST, "sudoers_templates": {"alice-probe": {**TEMPLATE, **tweak}}})
    for path in ("/etc/sudoers.d/pantheon.alice", "/etc/sudoers", "/etc/sudoers.d/sub/x", "/tmp/x"):
        with pytest.raises(F.ManifestError):
            F.load_manifest({**MANIFEST, "sudoers_templates": {"alice-probe": {**TEMPLATE, "path": path}}})


@needs_sandbox
def test_e20_untracked_fragments_and_mode_drift_fail_closed(sb):
    helper, port = make(sb)
    converge(helper, BOOTSTRAP_OPS[:2])
    sb.write(FRAG, b"# someone else's fragment\n", 0o440)
    step = helper.admit("sudoers_fragment.ensure", {"template": "alice-probe", "sha256": SHA}, Hp.ACTIVATE)
    with pytest.raises(Hp.PreflightRefused, match="untracked"):
        helper.apply(step)
    assert sb.read(FRAG) == b"# someone else's fragment\n"
    sb.reset()
    sb.mkdir("/srv/pantheon/shared")
    helper, port = make(sb)
    converge(helper, BOOTSTRAP_OPS[:2] + [BOOTSTRAP_OPS[-1]])
    assert port.runner.run([P.CHMOD, "0644", FRAG]).returncode == 0
    assert not helper.verify(step).ok  # a bad mode is drift, never "converged"


@needs_sandbox
def test_e20_rollback_is_exact_for_an_absent_and_a_tracked_prior_fragment(sb):
    helper, port = make(sb)
    converge(helper, BOOTSTRAP_OPS[:2])
    step = helper.admit("sudoers_fragment.ensure", {"template": "alice-probe", "sha256": SHA}, Hp.ACTIVATE)
    empty = sb.tree("/etc/sudoers.d")
    before = helper.apply(step)
    assert before is None
    helper.restore(step, before)
    assert sb.tree("/etc/sudoers.d") == empty
    # a TRACKED prior revision is replaced, then restored byte for byte
    prior_bytes = F.sudoers_text(PRIOR_TEMPLATE).encode()
    prior_sha = hashlib.sha256(prior_bytes).hexdigest()
    tracked = {**MANIFEST, "sudoers_templates": {"alice-probe": {**TEMPLATE, "prior_sha256": [prior_sha]}}}
    helper, port = make(sb, tracked)
    sb.write(FRAG, prior_bytes, 0o440)
    prior_tree = sb.tree("/etc/sudoers.d")
    step = helper.admit("sudoers_fragment.ensure", {"template": "alice-probe", "sha256": SHA}, Hp.ACTIVATE)
    before = helper.apply(step)
    assert before == {"sha256": prior_sha, "mode": "0440", "owner": "root"}
    assert sb.read(FRAG) != prior_bytes
    helper.restore(step, before)
    assert sb.tree("/etc/sudoers.d") == prior_tree
    assert sb.read(FRAG) == prior_bytes


# --- E33 ----------------------------------------------------------------------------------


@needs_sandbox
def test_e33_one_human_approved_fragment_applies_verifies_revokes_and_rolls_back(sb, tmp_path):
    approval = {"approved_by": "fixture-human", "template": "alice-probe", "sha256": SHA, "ticket": "T-2918",
                "note": "FIXTURE approval record; the real Telegram/engine grant path is T-5236 / T-2924"}
    helper, port = make(sb)
    converge(helper, BOOTSTRAP_OPS[:2])
    assert approval["sha256"] == F.sha256_text(F.sudoers_text(helper.manifest.section("sudoers_templates")["alice-probe"]))

    def snap(label):
        return {
            "label": label,
            "visudo_c": sb.run([P.VISUDO, "-c"]).stdout,
            "fragment": port.get(f"file:{FRAG}"),
            "sudo_l": port.parser_sudo_l("alice"),
        }

    evidence = {"approval": approval, "tier": "disposable userns host; sudo -l parser-derived", "steps": [snap("before")]}
    ensure = helper.admit("sudoers_fragment.ensure", {"template": "alice-probe", "sha256": SHA}, Hp.ACTIVATE)
    helper.apply(ensure)
    assert helper.verify(ensure).ok
    evidence["steps"].append(snap("applied"))
    revoke = helper.admit("sudoers_fragment.retire", {"template": "alice-probe"}, Hp.ACTIVATE)
    before_revoke = helper.apply(revoke)
    assert helper.verify(revoke).ok
    evidence["steps"].append(snap("revoked"))
    helper.restore(revoke, before_revoke)  # roll the revocation back
    assert helper.verify(ensure).ok
    evidence["steps"].append(snap("revocation-rolled-back"))
    out = tmp_path / "e33-evidence.json"
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True))

    before, applied, revoked, rolled = evidence["steps"]
    assert before["fragment"] is None and before["sudo_l"]["allowed"] == []
    assert applied["fragment"] == {"sha256": SHA, "mode": "0440", "owner": "root"}
    assert applied["sudo_l"]["allowed"] == TEMPLATE["commands"] and not applied["sudo_l"]["negative_probe_allowed"]
    assert "pantheon-alice-probe: parsed OK" in applied["visudo_c"]
    assert revoked["fragment"] is None and revoked["sudo_l"]["allowed"] == []
    assert rolled["fragment"] == applied["fragment"] and rolled["sudo_l"] == applied["sudo_l"]
    assert all(s["sudo_l"]["source"] == PARSER_SOURCE for s in evidence["steps"])
    assert json.loads(out.read_text())["approval"]["approved_by"] == "fixture-human"


# --- E34 ----------------------------------------------------------------------------------


@needs_sandbox
def test_e34_retirement_disables_the_seat_and_jobs_and_keeps_home_and_mailbox(sb):
    helper, port = make(sb)
    converge(helper, BOOTSTRAP_OPS[:7])
    sb.write("/home/alice/notes.txt", b"alice's work\n", 0o600, 3101, 3101)
    sb.write("/srv/pantheon/mbox-alice/msg-0001", b"mail for alice\n", 0o660, 3101, 3001)
    home, mailbox = sb.tree("/home/alice"), sb.tree("/srv/pantheon/mbox-alice")
    port.runner.calls.clear()
    for family, params in (("account.retire", {"account": "alice"}), ("linger.disable", {"account": "alice"}),
                           ("mailbox_dir.retire", {"mailbox": "alice"})):
        step = helper.admit(family, params, Hp.ACTIVATE)
        helper.apply(step)
        assert helper.verify(step).ok, family
    # F4, fixed by T-5513 (#1295): the 5-digit `chmod 00000` clears the setgid
    # bit too, so the 2770 mailbox ends EXACTLY at 0000 and verify converges.
    assert port.get("dir:/srv/pantheon/mbox-alice")["mode"] == "0000"
    assert [P.CHMOD, "00000", "/srv/pantheon/mbox-alice"] in [list(c) for c in port.runner.calls]
    assert sb.run(["/usr/bin/getent", "passwd", "alice"]).returncode == 0  # the account record stays
    shadow = sb.run(["/usr/bin/getent", "shadow", "alice"]).stdout.strip().split(":")
    assert shadow[1].startswith("!") and shadow[7] == "1"  # locked AND expired: no login, no jobs
    assert sb.read("/var/lib/systemd/linger/alice") is None  # user manager no longer lingers (fake-port tier)
    assert b"loginctl disable-linger alice" in sb.read("/var/lib/systemd/pantheon-fake-systemd.log")
    assert sb.tree("/home/alice") == home  # home untouched, byte for byte
    assert sb.tree("/srv/pantheon/mbox-alice") == mailbox  # mailbox contents untouched
    assert port.get("dir:/srv/pantheon/mbox-alice")["owner"] == "alice"  # locked, not removed
    assert not any(c[0] in (P.USERDEL, P.GROUPDEL) for c in port.runner.calls)


def test_e34_no_reachable_argv_deletes_an_account_home_or_mailbox():
    m = F.load_manifest(MANIFEST)
    for family in F.FAMILIES:
        for params in ({"account": "alice"}, {"mailbox": "alice"}, {"template": "alice-probe"}):
            try:
                r = F.resolve(family, params, m)
            except F.ParamError:
                continue
            try:
                argvs = P.apply_argv(r, staged_source="/staged")
            except ValueError:
                continue
            for argv in argvs:
                assert argv[0] not in (P.USERDEL, P.GROUPDEL) and "--remove" not in argv and "-r" not in argv, (family, argv)
    r = F.resolve("account.ensure", {"account": "alice"}, m)
    for argv in P.rollback_argv(r, None):  # undoing a CREATE removes the record, never the home
        assert "--remove" not in argv and "-r" not in argv
    with pytest.raises(Hp.UnknownOperation):
        Hp.RootHelper(P.SimulatedSubstratePort(), m, Hp.BOOTSTRAP).admit("account.delete", {"account": "alice"}, Hp.ACTIVATE)


# --- E36 ----------------------------------------------------------------------------------


@needs_sandbox
def test_e36_the_broker_daemon_context_is_refused_for_every_family_in_the_sandbox(sb):
    helper, port = make(sb, context=Hp.DAEMON)
    before = state(sb)
    for family, params in BOOTSTRAP_OPS + [("sudoers_fragment.retire", {"template": "alice-probe"}),
                                           ("account.retire", {"account": "alice"})]:
        with pytest.raises(Hp.SubstrateFamilyNotExposed) as info:
            helper.admit(family, params, Hp.ACTIVATE)
        assert info.value.payload()["retry_context"] == Hp.BOOTSTRAP
    assert port.runner.calls == [] and state(sb) == before


# --- T-5510 (Recovery, #1293 @9a4a6c0ac): F2/F3 fixed; Recovery's exact contract ---------------------


def test_f2_a_manifest_acl_without_base_entries_is_refused():
    for acl in (["group:pmail:r-x"], ["user::rwx", "group::r-x", "group:pmail:r-x"],
                ["user::rwx", "group::r-x", "other::---", "default:group:pmail:r-x"]):
        doc = {**MANIFEST, "paths": {"/srv/pantheon/shared": {**MANIFEST["paths"]["/srv/pantheon/shared"], "acl": acl}}}
        with pytest.raises(F.ManifestError):
            F.load_manifest(doc)
    r = F.resolve("acl.converge", {"path": "/srv/pantheon/shared"}, F.load_manifest(MANIFEST))
    assert P.apply_argv(r) == [[P.SETFACL, "--set", ",".join(sorted(ACL)), "/srv/pantheon/shared"]]


def test_f3_rollback_of_a_retire_is_the_exact_unlock_argv():
    m = F.load_manifest(MANIFEST)
    r = F.resolve("account.retire", {"account": "alice"}, m)
    unlocked = {**r.after, "locked": False}
    assert P.rollback_argv(r, unlocked) == [[P.USERMOD, "--unlock", "--expiredate", "", "alice"]]
    assert P.rollback_argv(r, {**r.after, "locked": True}) == []


@needs_sandbox
def test_f3_rolling_back_a_retire_restores_the_account_in_the_sandbox(sb):
    helper, port = make(sb)
    converge(helper, BOOTSTRAP_OPS[:2])
    shadow_before = sb.run(["/usr/bin/getent", "shadow", "alice"]).stdout
    step = helper.admit("account.retire", {"account": "alice"}, Hp.ACTIVATE)
    before = helper.apply(step)
    helper.restore(step, before)
    assert port.get("passwd:alice") == before
    assert sb.run(["/usr/bin/getent", "shadow", "alice"]).stdout.split(":")[7] == shadow_before.split(":")[7]
