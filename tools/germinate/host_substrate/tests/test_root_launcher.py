"""T-6167 PR-4: the root-to-seat launcher (host_substrate/root_launcher.py). DARK.

Pure unit tests: every seam (stat, euid, passwd, runner, nonce) is a fake, so nothing reads or
writes the real host. Inputs are classified as known-good or unknown, each with a benign
counterpart that must still be accepted.
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from tools._organ_lifecycle import CommandResult, FakeCommandRunner
from tools.germinate.components.harness_manager_root import Identity
from tools.germinate.host_substrate import ports as HP
from tools.germinate.host_substrate import root_launcher as RL
from tools.germinate.planner_sandbox.launcher import systemd_run_shape

SRC = Path(RL.__file__)
REV = "ab12cd34ef56"
FW = f"{RL.FRAMEWORK_BASE}/{REV}"
NONCE = "0123456789ab"
DIGEST = "sha256:" + "a" * 64
GOOD_DIR = RL.StatInfo("dir", 0, 0o755)


def ident(account="alice", uid=1101, groups=("pmail",)) -> Identity:
    return Identity("role-" + account, account, uid, "/bin/bash", "0750", tuple(groups), (), "observe-only")


def chain() -> dict[str, RL.StatInfo]:
    return {p: GOOD_DIR for p in (*RL.FRAMEWORK_ANCESTORS, FW)}


def stat_of(table):
    return lambda path: table.get(path)


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(RL, "ROOT_LAUNCHER_LIVE", True)
    monkeypatch.setattr(RL, "LIVE_PROFILES", MappingProxyType({"seat-run": True, "c5-canary": True}))


def build(profile="seat-run", seat="alice", **over):
    kw = dict(
        scope=f"seat-{seat}",
        mode="install" if profile == "seat-run" else "subprocess-creation",
        identities={"r": ident(seat)},
        framework_root=FW,
        stat=stat_of(chain()),
        euid=lambda: 0,
        getpwnam=lambda name: SimpleNamespace(pw_uid=1101),
        live_groups=lambda name: (name, frozenset({"pmail"})),  # primary, supplementary: equals ident()'s declaration
        nonce=NONCE,
        plan_digest=DIGEST if profile == "seat-run" else None,
    )
    kw.update(over)
    return RL.build_launch(profile, seat, **kw)


def refused(code, **kw):
    with pytest.raises(RL.LauncherRefused) as info:
        build(**kw)
    assert info.value.code == code, str(info.value)


# --- unit: exact shapes ---------------------------------------------------------------


def test_exact_argv_seat_run_profile(live):
    spec = build()
    template = RL.SEAT_CHILD_TEMPLATE.format(fw=repr(FW))
    assert list(spec.argv) == [
        "/usr/bin/systemd-run", "--wait", "--pipe", "--collect", "--quiet",
        f"--unit=pantheon-germinate-seat-alice-{NONCE}.service",
        "--property=NoNewPrivileges=yes", "--property=PrivateTmp=yes",
        "--property=ProtectKernelTunables=yes", "--property=ProtectKernelModules=yes",
        "--property=ProtectKernelLogs=yes", "--property=ProtectControlGroups=yes",
        "--property=RestrictSUIDSGID=yes", "--property=LockPersonality=yes",
        "--property=RuntimeMaxSec=1800", "--property=User=alice", "--property=WorkingDirectory=/home/alice",
        "--setenv=PATH=/usr/bin:/bin", "--setenv=LC_ALL=C.UTF-8", "--setenv=TZ=UTC",
        "--setenv=HOME=/home/alice", "--setenv=USER=alice", "--setenv=LOGNAME=alice",
        "--setenv=XDG_RUNTIME_DIR=/run/user/1101",
        "--", "/usr/bin/python3", "-I", "-B", "-c", template, "install", "alice", DIGEST,
    ]


def test_selfcheck_mode_argv_tail(live):
    spec = build(mode="selfcheck", plan_digest=None)
    assert spec.argv[-2:] == ("selfcheck", "alice")


def test_exact_argv_c5_profile_properties(live):
    spec = build("c5-canary", mode="network")
    unit = f"pantheon-germinate-c5-alice-{NONCE}"
    expected = systemd_run_shape.build_properties(
        scratch_dir=Path(f"/run/{unit}"),
        launcher_path=Path(f"{FW}/{RL.LAUNCHER_REL}"),
        seccomp_vendor_dir=Path(f"{FW}/{RL.SECCOMP_REL}"),
        system_scope=True,
    ) + ["User=alice", "SupplementaryGroups=", f"RuntimeDirectory={unit}", "WorkingDirectory=/"]
    assert list(spec.properties) == expected
    assert "TemporaryFileSystem=/:mode=755" in spec.properties
    assert "CapabilityBoundingSet=" in spec.properties  # the system-scope-only set is present
    assert "--user" not in spec.argv
    assert spec.argv[:6] == ("/usr/bin/systemd-run", "--wait", "--pipe", "--collect", "--quiet", f"--unit={unit}.service")
    assert spec.argv[-4:-1] == ("/usr/bin/python3", "-SEI", "-c")


def test_user_uid_home_pinned_from_instance_yaml(live):
    spec = build(identities={"r": ident("alice", uid=1101)})
    assert spec.uid == 1101
    assert "User=alice" in spec.properties and "WorkingDirectory=/home/alice" in spec.properties
    assert ("XDG_RUNTIME_DIR", "/run/user/1101") in spec.env
    assert ("HOME", "/home/alice") in spec.env
    # a benign second seat gets its own values, nothing carried over
    other = build(seat="bobby", identities={"r": ident("bobby", uid=1102)}, getpwnam=lambda n: SimpleNamespace(pw_uid=1102))
    assert other.uid == 1102 and "User=bobby" in other.properties and "User=alice" not in other.properties


def test_env_is_scrubbed_and_fixed(live, monkeypatch):
    for k, v in {"PYTHONPATH": "/evil", "LD_PRELOAD": "/evil.so", "HOME": "/root", "PATH": "/evil/bin"}.items():
        monkeypatch.setenv(k, v)
    for profile in ("seat-run", "c5-canary"):
        spec = build(profile)
        joined = "\0".join(spec.argv)
        assert "/evil" not in joined and "/root" not in joined
        assert dict(spec.env)["PATH"] == "/usr/bin:/bin"
        assert {k for k, _ in spec.env} >= {"PATH", "LC_ALL", "TZ"}
        assert not any(k in ("PYTHONPATH", "LD_PRELOAD") for k, _ in spec.env)
    assert {k for k, _ in build("c5-canary").env} == {"PATH", "LC_ALL", "TZ"}
    assert {k for k, _ in build().env} == {"PATH", "LC_ALL", "TZ", "HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR"}


def test_argv_is_list_with_fixed_binaries_and_dash_I_dash_B(live):
    from tools import host_broker_install as HBI

    assert RL.PYTHON3 == HBI.PYTHON3
    spec = build()
    assert isinstance(spec.argv, tuple) and all(isinstance(a, str) for a in spec.argv)
    assert spec.argv[0] == RL.SYSTEMD_RUN == "/usr/bin/systemd-run"
    child = spec.argv[spec.argv.index("--") + 1:]
    assert child[:4] == ("/usr/bin/python3", "-I", "-B", "-c")
    assert "shell" not in "".join(spec.argv[:6]).lower()
    # the runner receives a list, never a string
    runner = FakeCommandRunner()
    RL.launch(spec, runner)
    assert runner.calls == [spec.argv]


def test_unit_name_matches_fixed_pattern(live):
    import re

    pat = re.compile(r"^pantheon-germinate-(seat|c5)-[a-z][a-z0-9_-]{0,30}-[0-9a-f]{12}\.service$")
    assert pat.match(build().unit) and pat.match(build("c5-canary").unit)
    for bad in ("", "xyz", "0123456789AB", "0123456789abc", "../0123456789", "0123456789a;"):
        refused("bad-nonce", nonce=bad)


def test_property_set_is_closed(live):
    for profile in ("seat-run", "c5-canary"):
        for mode in (RL.SEAT_MODES if profile == "seat-run" else RL.CANARY_MODES):
            spec = build(profile, mode=mode, plan_digest=DIGEST if mode == "install" else None, gates_dir="/srv/gates")
            for prop in spec.properties:
                name, sep, _ = prop.partition("=")
                assert sep == "=" and name in RL.ALLOWED_PROPERTY_NAMES, prop
            assert [a for a in spec.argv if a.startswith("--property=")] == [f"--property={p}" for p in spec.properties]


def test_bootstrap_template_fixed_and_fw_path_regex_validated(live):
    spec = build()
    template = spec.argv[spec.argv.index("-c") + 1]
    assert template == RL.SEAT_CHILD_TEMPLATE.format(fw=repr(FW))
    compile(template, "<seat-child>", "exec")
    assert template.count(FW) == 1
    # only a regex-valid rev is interpolated: a quote-bearing path never reaches the template
    refused("framework-path-refused", framework_root=FW + "'; import os; '")


def test_launch_returns_runner_result(live):
    spec = build()
    runner = FakeCommandRunner(default=CommandResult(7, "out", "err"))
    res = RL.launch(spec, runner)
    assert (res.returncode, res.stdout, res.stderr, res.unit) == (7, "out", "err", spec.unit)


# --- adversarial: classification ------------------------------------------------------


@pytest.mark.parametrize("seat", ["alice", "a", "suborch2", "x-y_z", "a" * 31])
def test_seat_name_known_good_accepted(live, seat):
    spec = build(seat=seat, identities={"r": ident(seat)})
    assert spec.seat == seat


@pytest.mark.parametrize("seat", ["", "a" * 32, "Alice", "-a", "a b", "a/b", "a;b", "../x", "a\nb", "1abc", " ", "a=b", "a.b"])
def test_seat_name_unknown_refused(live, seat):
    refused("bad-seat-name", seat=seat, scope=f"seat-{seat}", identities={"r": ident("alice")})


def test_uid_below_1000_refused(live):
    refused("system-uid-refused", identities={"r": ident("alice", uid=999)}, getpwnam=lambda n: SimpleNamespace(pw_uid=999))
    # benign: the lowest fleet uid is accepted
    assert build(identities={"r": ident("alice", uid=1000)}, getpwnam=lambda n: SimpleNamespace(pw_uid=1000)).uid == 1000


def test_uid_zero_and_root_account_refused(live):
    refused("system-uid-refused", identities={"r": ident("alice", uid=0)}, getpwnam=lambda n: SimpleNamespace(pw_uid=0))
    refused("system-uid-refused", seat="root", scope="seat-root", identities={"r": ident("root", uid=1500)})


def test_uid_above_fleet_range_refused(live):
    refused("system-uid-refused", identities={"r": ident("alice", uid=60001)}, getpwnam=lambda n: SimpleNamespace(pw_uid=60001))


def test_account_not_in_instance_yaml_refused(live):
    refused("account-not-in-instance", identities={"r": ident("someone")})
    refused("account-not-in-instance", identities={})
    refused("account-not-in-instance", identities={"r": ident("alice"), "q": ident("alice", uid=1102)})


def test_uid_mismatch_with_passwd_refused(live):
    refused("uid-mismatch", getpwnam=lambda n: SimpleNamespace(pw_uid=1102))

    def missing(name):
        raise KeyError(name)

    refused("uid-mismatch", getpwnam=missing)


@pytest.mark.parametrize("group", sorted(RL.ROOT_EQUIVALENT_GROUPS))
def test_root_equivalent_group_refused(live, group):
    refused("root-equivalent-group", identities={"r": ident("alice", groups=("pmail", group))})


def test_ordinary_groups_accepted(live):
    assert build(identities={"r": ident("alice", groups=("pmail", "pantheon-mailbox"))},
                 live_groups=lambda n: (n, frozenset({"pmail", "pantheon-mailbox"}))).seat == "alice"


def test_another_seats_scope_refused(live):
    for scope in ("seat-bobby", "system", "seat-", "seat-alice2", ""):
        refused("scope-mismatch", scope=scope)


def test_non_root_caller_refused(live):
    refused("non-root-caller", euid=lambda: 1101)
    refused("non-root-caller", euid=lambda: 1)


def test_plan_digest_must_be_sha256_64_hex(live):
    for bad in (None, "", "a" * 64, "sha256:" + "a" * 63, "sha256:" + "A" * 64, "sha256:" + "g" * 64, DIGEST + "\n", "md5:" + "a" * 32):
        refused("bad-plan-digest", plan_digest=bad)


def test_bad_nonce_has_its_own_code_and_is_not_a_mode_error(live):
    refused("bad-nonce", nonce="NOT-HEX")
    refused("bad-mode", mode="selfcheck; id")  # the mode code is unchanged


# --- live group membership (Recovery D15 row 5) -------------------------------------------


def test_live_group_docker_with_a_clean_declaration_is_refused(live):
    refused("root-equivalent-group", live_groups=lambda n: (n, frozenset({"pmail", "docker"})))


@pytest.mark.parametrize("group", sorted(RL.ROOT_EQUIVALENT_GROUPS))
def test_every_root_equivalent_live_group_is_refused(live, group):
    refused("root-equivalent-group", live_groups=lambda n: (n, frozenset({"pmail", group})))


def test_live_primary_group_root_equivalent_is_refused(live):
    refused("root-equivalent-group", live_groups=lambda n: ("disk", frozenset({"pmail"})))


def test_live_extra_non_root_group_is_drift(live):
    refused("group-drift", live_groups=lambda n: (n, frozenset({"pmail", "foo"})))


def test_live_missing_a_declared_group_is_drift(live):
    refused("group-drift", live_groups=lambda n: (n, frozenset()))


def test_live_equal_to_declared_is_accepted_regardless_of_primary_name(live):
    assert build(live_groups=lambda n: ("some-primary", frozenset({"pmail"}))).seat == "alice"
    two = {"r": ident("alice", groups=("pmail", "pantheon-mailbox"))}
    assert build(identities=two, live_groups=lambda n: (n, frozenset({"pantheon-mailbox", "pmail"}))).seat == "alice"


def test_real_shape_suborch1_is_accepted(live):
    # live {suborch1, pantheon-shared, pantheon-mailbox}; declared omits the primary group
    decl = {"r": ident("suborch1", uid=1101, groups=("pantheon-mailbox", "pantheon-shared"))}
    spec = build(seat="suborch1", identities=decl,
                 live_groups=lambda n: ("suborch1", frozenset({"pantheon-shared", "pantheon-mailbox"})))
    assert spec.seat == "suborch1"


def test_real_shape_claude_is_refused_as_root_equivalent(live):
    # declared [pantheon-mailbox, pantheon-shared, sudo, users]: sudo is refused from the declaration, as today
    decl = {"r": ident("claude", uid=1101, groups=("pantheon-mailbox", "pantheon-shared", "sudo", "users"))}
    refused("root-equivalent-group", seat="claude", identities=decl,
            live_groups=lambda n: ("claude", frozenset({"sudo", "users", "pantheon-shared", "pantheon-mailbox"})))


def test_primary_group_is_not_drift_but_a_root_equivalent_primary_is_refused(live):
    refused("root-equivalent-group", live_groups=lambda n: ("docker", frozenset({"pmail"})))
    assert build(live_groups=lambda n: ("alice", frozenset({"pmail"}))).seat == "alice"


def test_live_groups_seam_is_not_called_for_an_identity_that_already_refused(live):
    calls = []
    refused("uid-mismatch", getpwnam=lambda n: SimpleNamespace(pw_uid=1102),
            live_groups=lambda n: calls.append(n) or (n, frozenset()))
    assert calls == []


def test_profile_and_mode_enums(live):
    # an unlisted profile has no pin, so it is dark before the enum is even consulted
    with pytest.raises(RL.LauncherRefused) as info:
        RL.build_launch("seat-run-x", "alice", scope="seat-alice", mode="install", identities={}, framework_root=FW,
                        stat=stat_of(chain()), euid=lambda: 0, getpwnam=lambda n: None,
                        live_groups=lambda n: (n, frozenset()), nonce=NONCE)
    assert info.value.code == "live-actuation-disabled"
    refused("bad-mode", mode="selfcheck; id")
    refused("bad-mode", profile="c5-canary", mode="install")
    refused("bad-mode", profile="seat-run", mode="network")


def test_gates_dir_classification(live):
    ok = build("c5-canary", mode="authority-holding", gates_dir="/srv/pantheon/gates")
    assert ok.argv[-1].count("/srv/pantheon/gates") == 1
    for bad in (None, "", "relative/path", "/a/../b", "/a b", "/a;b", "/a\nb", "/" + "a" * 300):
        refused("bad-gates-dir", profile="c5-canary", mode="authority-holding", gates_dir=bad)


# --- adversarial: the framework tree, one case per level -------------------------------

LEVELS = [*RL.FRAMEWORK_ANCESTORS, FW]


@pytest.mark.parametrize("level", LEVELS)
def test_framework_level_not_root_owned_refused(live, level):
    table = chain()
    table[level] = RL.StatInfo("dir", 1101, 0o755)
    refused("framework-path-refused", stat=stat_of(table))


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777, 0o770 | 0o002, 0o720])
def test_framework_level_group_or_other_writable_refused(live, level, mode):
    table = chain()
    table[level] = RL.StatInfo("dir", 0, mode)
    refused("framework-path-refused", stat=stat_of(table))


@pytest.mark.parametrize("level", LEVELS)
def test_framework_level_symlink_or_absent_refused(live, level):
    for bad in (RL.StatInfo("symlink", 0, 0o755), RL.StatInfo("other", 0, 0o644), None):
        table = chain()
        if bad is None:
            del table[level]
        else:
            table[level] = bad
        refused("framework-path-refused", stat=stat_of(table))


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o555, 0o700, 0o1755])
def test_framework_known_good_modes_accepted(live, mode):
    table = {p: RL.StatInfo("dir", 0, mode) for p in LEVELS}
    assert build(stat=stat_of(table)).uid == 1101


@pytest.mark.parametrize(
    "path",
    [
        f"{RL.FRAMEWORK_BASE}/active",
        "usr/local/lib/pantheon/framework/" + REV,
        f"{RL.FRAMEWORK_BASE}/../framework/{REV}",
        f"{RL.FRAMEWORK_BASE}/ZZZZZZZZZZZZ",
        f"{RL.FRAMEWORK_BASE}/ab12cd",
        f"{RL.FRAMEWORK_BASE}/{REV}/",
        f"{RL.FRAMEWORK_BASE}/{REV}/tools",
        "/home/alice/pantheon-config",
        "/tmp/" + REV,
        "",
        None,
    ],
)
def test_framework_path_classification_unknown_refused(live, path):
    refused("framework-path-refused", framework_root=path)


def test_framework_path_known_good_revs_accepted(live):
    for rev in ("0" * 12, "f" * 64, "ab12cd34ef56ab12"):
        path = f"{RL.FRAMEWORK_BASE}/{rev}"
        table = {p: GOOD_DIR for p in (*RL.FRAMEWORK_ANCESTORS, path)}
        assert build(framework_root=path, stat=stat_of(table)).uid == 1101


# --- pins and I/O ---------------------------------------------------------------------


def _module_assign(tree, name):
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return node.value
    raise AssertionError(f"{name} not assigned at module level")


def test_pin_literal_false_ast():
    tree = ast.parse(SRC.read_text())
    value = _module_assign(tree, "ROOT_LAUNCHER_LIVE")
    assert isinstance(value, ast.Constant) and value.value is False
    table = _module_assign(tree, "LIVE_PROFILES")
    assert isinstance(table, ast.Call) and table.func.id == "MappingProxyType" and len(table.args) == 1
    d = table.args[0]
    assert isinstance(d, ast.Dict)
    assert [k.value for k in d.keys] == list(RL.PROFILES)
    assert all(isinstance(v, ast.Constant) and v.value is False for v in d.values)
    # the neighbouring pins this PR must not touch stay literal False
    assert HP.LIVE_ACTUATION_ENABLED is False
    ports_tree = ast.parse(Path(HP.__file__).read_text())
    pv = _module_assign(ports_tree, "LIVE_ACTUATION_ENABLED")
    assert isinstance(pv, ast.Constant) and pv.value is False
    from tools.germinate.seat_backend import ports as SP

    assert all(v is False for v in SP.LIVE_ACTUATION_ENABLED.values())


def test_profile_table_is_read_only_all_false():
    assert isinstance(RL.LIVE_PROFILES, MappingProxyType)
    assert set(RL.LIVE_PROFILES) == set(RL.PROFILES)
    assert all(v is False for v in RL.LIVE_PROFILES.values())
    assert RL.ROOT_LAUNCHER_LIVE is False
    with pytest.raises(TypeError):
        RL.LIVE_PROFILES["seat-run"] = True  # type: ignore[index]
    assert RL.is_live("seat-run") is False and RL.is_live("c5-canary") is False


def test_pin_module_reads_no_override():
    tree = ast.parse(SRC.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"os", "sys", "argparse", "subprocess", "pwd", "shutil", "socket", "getpass"}, imported
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not attrs & {"environ", "getenv", "putenv", "Popen", "open"}, attrs  # no os.environ reach (sys is not imported)
    assert not names & {"environ", "getenv", "putenv", "open", "Popen"}, names


def test_launcher_imports_no_subprocess_ast():
    tree = ast.parse(SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] != "subprocess" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "subprocess"
    # benign: the same walk does flag a subprocess import when one exists
    flagged = ast.parse("import subprocess")
    assert any(isinstance(n, ast.Import) and n.names[0].name == "subprocess" for n in ast.walk(flagged))


class _Boom:
    """A seam that fails the test on any call."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        raise AssertionError("I/O reached while the launcher was dark")


PIN_CASES = {
    "global-false": (False, True),
    "profile-false": (True, False),
    "both-false": (False, False),
}


@pytest.mark.parametrize("case", sorted(PIN_CASES))
@pytest.mark.parametrize("profile", ["seat-run", "c5-canary"])
def test_pin_false_refuses_before_any_io(monkeypatch, case, profile):
    glob, prof = PIN_CASES[case]
    monkeypatch.setattr(RL, "ROOT_LAUNCHER_LIVE", glob)
    monkeypatch.setattr(RL, "LIVE_PROFILES", MappingProxyType({"seat-run": prof, "c5-canary": prof}))
    stat, euid, pwnam, lg, runner = _Boom(), _Boom(), _Boom(), _Boom(), _Boom()
    fake_runner = SimpleNamespace(run=runner)
    spies = {n: _Boom() for n in ("subprocess.run", "subprocess.Popen", "os.open", "os.stat", "os.lstat")}
    with monkeypatch.context() as m:
        m.setattr(subprocess, "run", spies["subprocess.run"])
        m.setattr(subprocess, "Popen", spies["subprocess.Popen"])
        m.setattr(os, "open", spies["os.open"])
        m.setattr(os, "stat", spies["os.stat"])
        m.setattr(os, "lstat", spies["os.lstat"])
        m.setattr(Path, "open", _Boom())
        m.setattr(Path, "read_text", _Boom())
        m.setattr(Path, "read_bytes", _Boom())
        with pytest.raises(RL.LiveActuationDisabled) as info:
            RL.build_launch(profile, "alice", scope="seat-alice", mode="selfcheck" if profile == "seat-run" else "network",
                            identities=_Boom(), framework_root=FW, stat=stat, euid=euid, getpwnam=pwnam,
                            live_groups=lg, nonce=NONCE)
        assert info.value.code == "live-actuation-disabled"
        spec = RL.LaunchSpec(profile, "alice", "x", 1101, "u.service", ("a",), (), ())
        with pytest.raises(RL.LiveActuationDisabled):
            RL.launch(spec, fake_runner)
    assert stat.calls == euid.calls == pwnam.calls == lg.calls == runner.calls == 0
    assert all(s.calls == 0 for s in spies.values())


def test_pin_false_is_the_shipped_state():
    with pytest.raises(RL.LiveActuationDisabled):
        build()
    with pytest.raises(RL.LiveActuationDisabled):
        build("c5-canary")


def test_one_profile_flipped_does_not_enable_the_other(monkeypatch):
    monkeypatch.setattr(RL, "ROOT_LAUNCHER_LIVE", True)
    monkeypatch.setattr(RL, "LIVE_PROFILES", MappingProxyType({"seat-run": False, "c5-canary": True}))
    assert build("c5-canary").profile == "c5-canary"
    with pytest.raises(RL.LiveActuationDisabled):
        build("seat-run")
