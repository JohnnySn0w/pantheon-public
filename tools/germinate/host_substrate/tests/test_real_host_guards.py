"""T-2918: the never-touch-the-real-host guards themselves. Always run (CI included).

The negative tests attempt real-host writes and mutator launches on purpose.
The audit hook blocks each BEFORE the syscall. Every launch target is a
NONEXISTENT path that merely shares a mutator's basename (review N6), so even a
regressed hook executes nothing real, and the account could not write these
paths anyway. OS permissions are the real barrier; these are tripwires.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .sandbox import agent, guards

PROBE = "/etc/pantheon-t2918-guard-probe"
GHOST = "/nonexistent-t2918"  # never exists: a regressed hook runs nothing
AGENT = Path(__file__).parent / "sandbox" / "agent.py"


# --- the audit hook: writes ----------------------------------------------------------


def test_the_audit_hook_blocks_a_write_open_under_real_etc():
    with guards.expect_violation():
        open(PROBE, "w")  # noqa: SIM115 - the hook raises before the file is opened
    assert not os.path.exists(PROBE)


def test_the_audit_hook_blocks_an_os_level_create_flag_open():
    with guards.expect_violation():
        os.open("/srv/pantheon-t2918-guard-probe", os.O_WRONLY | os.O_CREAT, 0o600)


@pytest.mark.parametrize(
    "action",
    [
        lambda: os.mkdir("/srv/pantheon-t2918-guard-probe"),
        lambda: os.chmod("/etc/pantheon-t2918-guard-probe", 0o644),
        lambda: os.rename("/etc/pantheon-t2918-a", "/etc/pantheon-t2918-b"),
        lambda: Path("/home/pantheon-t2918-nobody/x").write_text("x"),
        lambda: Path("/var/lib/systemd/linger/pantheon-t2918").touch(),
    ],
    ids=["mkdir-srv", "chmod-etc", "rename-etc", "write-other-home", "touch-linger"],
)
def test_the_audit_hook_blocks_filesystem_mutations_under_real_roots(action):
    with guards.expect_violation():
        action()


# --- the audit hook: process launches (review N1/N6) -----------------------------------


@pytest.mark.parametrize("name", ["useradd", "visudo", "systemctl", "sudo", "setfacl", "loginctl"])
def test_the_audit_hook_blocks_mutator_subprocesses_by_basename(name):
    with guards.expect_violation():
        subprocess.run([f"{GHOST}/{name}", "--version"], capture_output=True, check=False)


@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/bin/env", f"{GHOST}/useradd", "x"],
        ["/usr/bin/env", "FOO=1", f"{GHOST}/visudo", "-c"],
        ["/usr/bin/timeout", "5", f"{GHOST}/systemctl", "enable", "x"],
        ["/bin/sh", "-c", f"{GHOST}/usermod --lock x"],
        ["/bin/sh", "-c", f"true && {GHOST}/sudo -l"],
        ["/bin/bash", "-c", f"echo ok; {GHOST}/gpasswd --add a b"],
        [sys.executable, "-c", "pass"],  # python -c cannot be inspected: refused outright
    ],
    ids=["env", "env-assign", "timeout", "sh-c", "sh-c-chain", "bash-c-chain", "python-c"],
)
def test_the_audit_hook_looks_through_wrappers(argv):
    with guards.expect_violation():
        subprocess.run(argv, capture_output=True, check=False, timeout=10)


@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/bin/timeout", "-s", "KILL", "5", f"{GHOST}/useradd", "x"],
        ["/usr/bin/env", "-u", "X", f"{GHOST}/useradd", "x"],
        ["/usr/bin/timeout", "--signal", "TERM", "-k", "1", "5", f"{GHOST}/visudo", "-c"],
        ["/usr/bin/nice", "-n", "5", f"{GHOST}/usermod", "--lock", "x"],
        ["/usr/bin/stdbuf", "-o", "L", f"{GHOST}/chown", "root", "/etc/x"],
    ],
    ids=["timeout-s-KILL", "env-u-X", "timeout-long-opts", "nice-n", "stdbuf-o"],
)
def test_wrapper_options_that_take_a_value_are_not_mis_skipped(argv):
    """Review r2b: each wrapper is parsed with its own option arity."""

    with guards.expect_violation():
        subprocess.run(argv, capture_output=True, check=False, timeout=10)


def test_flock_with_a_lockfile_operand_is_looked_through(tmp_path):
    with guards.expect_violation():
        subprocess.run(["/usr/bin/flock", str(tmp_path / "LOCK"), f"{GHOST}/useradd", "x"],
                       capture_output=True, check=False, timeout=10)


@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/bin/env", "--bogus-option", f"{GHOST}/harmless"],
        ["/usr/bin/timeout", "-Z", "5", f"{GHOST}/harmless"],
        ["/usr/bin/env", "-S", f"{GHOST}/useradd x"],
        ["/usr/bin/flock", "-c", f"{GHOST}/useradd x", "/tmp/lock"],
        ["/usr/bin/xargs", f"{GHOST}/harmless"],
        ["/usr/bin/podman", "run", "x"],
    ],
    ids=["env-unknown-opt", "timeout-unknown-opt", "env-S", "flock-c", "xargs", "podman-run"],
)
def test_a_wrapper_the_table_cannot_parse_is_refused(argv):
    """Fail closed: unsure means refuse (decided over guessing an arity)."""

    assert guards.offending(argv) is not None


def test_known_benign_wrapped_commands_and_the_sandbox_launcher_pass():
    from .sandbox import launcher

    assert guards.offending(["/usr/bin/timeout", "-s", "KILL", "5", "/usr/bin/true"]) is None
    assert guards.offending(["/usr/bin/env", "-u", "X", "A=1", "/usr/bin/true"]) is None
    assert guards.offending(launcher._launch_argv(Path("/tmp/x"), "nonce")) is None


def test_the_audit_hook_blocks_os_system_posix_spawn_and_exec():
    with guards.expect_violation():
        os.system(f"{GHOST}/useradd x")
    with guards.expect_violation():
        os.posix_spawn(f"{GHOST}/visudo", [f"{GHOST}/visudo", "-c"], dict(os.environ))
    with guards.expect_violation():
        os.execv(f"{GHOST}/sudo", [f"{GHOST}/sudo", "-l"])  # nonexistent: never replaces this process


def test_the_session_basetemp_is_admitted_but_its_siblings_are_not(tmp_path_factory, tmp_path):
    """CI runs with --basetemp=../pytest-basetemp (under /opt, xdist workers in
    popen-gwN): the session's own basetemp is writable, a sibling is not."""

    base = tmp_path_factory.getbasetemp()
    (tmp_path / "ok").write_text("ok")  # under this session's basetemp: admitted
    sibling = base.parent / "pantheon-t2918-basetemp-sibling"
    if guards._protected(str(sibling)):  # the basetemp sits under a protected root (CI shape)
        with guards.expect_violation():
            open(sibling, "w")  # noqa: SIM115 - blocked before the open
        assert not sibling.exists()
    saved = guards._State.temp_roots
    try:  # the rule itself, wherever this run's basetemp lives
        guards._State.temp_roots = ()
        root = "/opt/actions-runner-pc/_work/pantheon-config/pytest-basetemp"
        guards.allow_temp_root(f"{root}/popen-gw1")
        assert not guards._protected(f"{root}/popen-gw1/test_x0/ok")
        assert guards._protected(f"{root}/x")  # the shared xdist root is NOT admitted
        assert guards._protected(f"{root}/popen-gw10/x")  # nor a prefix-sharing sibling worker
        assert guards._protected(f"{root}/popen-gw1/../x")  # nor a dot-dot escape
        assert guards._protected("/opt/actions-runner-pc/_work/pantheon-config/x")
        assert not guards._protected("/opt/actions-runner-pc/_work/pantheon-config/tools/__pycache__/m.cpython-312.pyc")
        assert guards._protected("/home/someone/notes.txt")  # no whole-home exemption any more
    finally:
        guards._State.temp_roots = saved


def test_benign_launches_reads_and_tmp_writes_stay_allowed(tmp_path):
    (tmp_path / "ok").write_text("ok")
    assert Path("/etc/passwd").read_text()  # reads are never blocked
    assert subprocess.run(["/usr/bin/true"], check=False).returncode == 0
    assert guards.offending(["/bin/sh", "-c", "echo hi | wc -l"]) is None


# --- the real-host state check (review N5) ---------------------------------------------


def test_the_fingerprint_detects_a_change(tmp_path):
    target = tmp_path / "f"
    target.write_text("a")
    before = guards.fingerprint([str(tmp_path), str(target)])
    assert guards.fingerprint([str(tmp_path), str(target)]) == before
    target.write_text("b")
    assert guards.fingerprint([str(tmp_path), str(target)]) != before
    (tmp_path / "g").write_text("new entry")
    assert guards.fingerprint([str(tmp_path)]) != guards.fingerprint([str(tmp_path / "missing")])


def test_the_residue_scan_finds_fixture_names_and_ids(tmp_path):
    db = tmp_path / "passwd"
    db.write_text("root:x:0:0::/root:/bin/bash\nalice:x:3101:3101::/home/alice:/bin/bash\nzed:x:3201:9::/:/bin/sh\n")
    assert guards.fixture_residue([str(db)]) == [f"{db}:alice", f"{db}:zed"]
    assert guards.fixture_residue() == []  # the REAL databases carry none


def test_the_state_check_is_narrow_and_covers_every_shadowed_root():
    """Narrow (no whole /etc, /home or account database hash) yet every root the
    sandbox shadows has at least one touchable path checked."""

    assert not {"/etc", "/home", "/etc/passwd", "/etc/group", "/etc/shadow"} & set(guards.TOUCHABLE_PATHS)
    enter = (Path(__file__).parent / "sandbox" / "enter.sh").read_text()
    for real in ("/etc", "/home", "/srv", "/var/lib/systemd", "/usr/bin/loginctl", "/usr/bin/systemctl", "/mnt"):
        assert f" {real}\n" in enter or f" {real} " in enter, real
        assert any(p == real or p.startswith(real + "/") for p in guards.TOUCHABLE_PATHS), real


# --- the in-sandbox agent's own guard (review B2) ----------------------------------------


def test_the_agent_refuses_on_the_real_host_before_ready(tmp_path):
    """Run agent.py DIRECTLY on the real host: it must refuse before printing
    'ready' (it fails opening the private-only marker; nothing else runs)."""

    done = subprocess.run([sys.executable, "-I", str(AGENT), "bogus-nonce", str(tmp_path)],
                          capture_output=True, text=True, timeout=30, input="")
    assert done.returncode != 0
    assert "ready" not in done.stdout
    assert ".pantheon-sandbox-marker" in done.stderr or "PermissionError" in done.stderr


@pytest.fixture
def fake_sandbox(tmp_path):
    """A same-device stand-in for the sandbox: marker, uid_map, mount and roots."""

    (tmp_path / "etc").mkdir()
    marker = tmp_path / "etc" / "marker"
    marker.write_text("good-nonce\n")
    uid_map = tmp_path / "uid_map"
    uid_map.write_text("         0       1007          1\n         1     558752      65536\n")
    return {"marker": str(marker), "uid_map": str(uid_map), "roots": [str(tmp_path / "etc")],
            "geteuid": lambda: 0, "mount": str(tmp_path)}


def _check(fake, nonce="good-nonce", **override):
    kw = {**fake, **override}
    mount = kw.pop("mount")
    agent.check_sandbox(nonce, mount, **kw)


def test_the_agent_guard_accepts_a_genuine_sandbox_shape(fake_sandbox):
    _check(fake_sandbox)  # positive control: the negatives below fail for ONE reason each


def test_the_agent_guard_refuses_a_marker_mismatch(fake_sandbox):
    with pytest.raises(PermissionError, match="marker"):
        _check(fake_sandbox, nonce="other-nonce")
    with pytest.raises(PermissionError, match="marker"):
        _check(fake_sandbox, nonce="")


def test_the_agent_guard_refuses_when_uid_0_is_the_real_root(fake_sandbox, tmp_path):
    identity = tmp_path / "uid_map_identity"
    identity.write_text("         0          0 4294967295\n")
    with pytest.raises(PermissionError, match="real root"):
        _check(fake_sandbox, uid_map=str(identity))
    with pytest.raises(PermissionError, match="namespace root"):
        _check(fake_sandbox, geteuid=lambda: 1007)


def test_the_agent_guard_refuses_a_root_off_the_sandbox_tmpfs(fake_sandbox):
    other_device = "/proc"  # never the same st_dev as a tmp dir
    assert os.stat(other_device).st_dev != os.stat(fake_sandbox["mount"]).st_dev
    with pytest.raises(PermissionError, match="not on the sandbox tmpfs"):
        _check(fake_sandbox, roots=fake_sandbox["roots"] + [other_device])


# --- the agent's path-argument check (review N2) -----------------------------------------


@pytest.fixture
def configured():
    agent.configure("n", "/mnt")
    yield
    agent.configure("", "")


@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/bin/install", "-m", "0440", "/mnt/staging/x", "/etc/sudoers.d/pantheon-x"],
        ["/usr/bin/mv", "-f", "--", "/etc/sudoers.d/a.pantheon-tmp", "/etc/sudoers.d/a"],
        ["/usr/bin/chmod", "0000", "/srv/pantheon/mbox-alice"],
        ["/usr/bin/setpriv", "--reuid", "3201", "/usr/bin/install", "-m", "0440", "/dev/null", "/etc/sudoers.d/x"],
        ["/usr/bin/python3", "-I", "-c", "print(1)"],
        ["/usr/sbin/visudo", "-cf", "/mnt/staging/abc"],
        ["/usr/sbin/visudo", "-c"],
        ["/usr/bin/setfacl", "--set", "user::rwx,group::r-x,other::---", "/srv/pantheon/shared"],
        ["/usr/bin/install", "-d", "-o", "alice", "-g", "pmail", "-m", "2770", "/srv/pantheon/mbox-alice"],
        ["/usr/bin/chown", "alice:pmail", "/srv/pantheon/shared"],
        ["/usr/bin/chmod", "00000", "/srv/pantheon/mbox-alice"],
        # T-5653: the ruled shapes
        ["/usr/bin/setfacl", "--physical", "--set", "user::rwx,group::r-x,other::---", "/srv/pantheon/shared"],
        ["/usr/bin/chown", "-h", "alice:pmail", "/srv/pantheon/shared"],
    ],
)
def test_sandbox_path_arguments_are_admitted(configured, argv):
    agent.check_paths(argv)


def test_the_ruled_flags_are_not_mistaken_for_path_or_owner_operands():
    """T-5653: --physical and -h take no value, so the path check still sees exactly the subject."""
    assert agent.path_arguments(["/usr/bin/setfacl", "--physical", "--set", "user::rwx", "/srv/a"]) == ["/srv/a"]
    assert agent.path_arguments(["/usr/bin/chown", "-h", "alice:pmail", "/srv/a"]) == ["/srv/a"]


@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/bin/install", "-m", "0644", "/mnt/staging/x", "/tmp/escape"],
        ["/usr/bin/mv", "-f", "--", "/etc/sudoers.d/x", "/run/user/1007/x"],
        ["/usr/bin/chown", "root", "/mnt/repo/tools/x.py"],
        ["/usr/bin/unlink", "/var/tmp/x"],
        ["/usr/bin/setpriv", "--reuid", "3201", "/usr/bin/chmod", "0777", "/dev/shm/x"],
        ["/usr/bin/setpriv", "--reuid", "3201", "/usr/bin/setpriv", "/usr/bin/true"],
        ["/usr/bin/python3", "-c", "print(1)"],
        ["/usr/bin/python3", "/mnt/repo/x.py"],
    ],
)
def test_out_of_sandbox_path_arguments_are_refused(configured, argv):
    with pytest.raises(PermissionError):
        agent.check_paths(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/bin/chown", "root", "notes.txt"],
        ["/usr/bin/chmod", "0644", "x/y"],
        ["/usr/bin/chmod", "0644", "../escape"],
        ["/usr/bin/mv", "-f", "--", "a", "/etc/sudoers.d/b"],
        ["/usr/bin/mv", "-f", "--", "/etc/sudoers.d/a", "b"],
        ["/usr/bin/install", "-m", "0440", "src", "/etc/sudoers.d/x"],
        ["/usr/bin/install", "-o", "root", "-m", "0440", "/mnt/staging/x", "dest"],
        ["/usr/bin/unlink", "./x"],
        ["/usr/sbin/visudo", "-cf", "frag"],
        ["/usr/bin/setfacl", "--set", "user::rwx,group::r-x,other::---", "rel"],
        ["/usr/bin/setpriv", "--reuid", "3201", "/usr/bin/chmod", "0777", "rel"],
        ["/usr/bin/install", "-m"],  # an option missing its value
    ],
    ids=["chown-bare", "chmod-rel", "chmod-dotdot", "mv-rel-src", "mv-rel-dst", "install-rel-src",
         "install-rel-dst", "unlink-dot", "visudo-rel", "setfacl-rel", "setpriv-rel", "install-missing-value"],
)
def test_relative_path_arguments_are_refused(configured, argv):
    """Review r2a: a relative argument would resolve against the agent's cwd."""

    with pytest.raises(PermissionError):
        agent.check_paths(argv)


def test_sandbox_code_is_never_imported_from_a_live_path():
    """The sandbox port runs real argv; it must stay test-tier."""

    root = Path(__file__).resolve().parents[3]  # tools/
    for path in root.rglob("*.py"):
        if "/tests/" in str(path) or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "host_substrate.tests" in node.module:
                raise AssertionError(f"{path} imports the test-tier sandbox")
