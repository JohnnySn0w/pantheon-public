"""T-6167 PR-4: the C5 system-manager transport (root launcher, `c5-canary` profile). DARK.

CI proves argv, property sets, shim behaviour and outcome parsing with fakes. It cannot prove
kernel confinement: that is the supervised real-host step (T-3296), which this file does not
claim. The user-scope typed-SKIP path (test_launcher_c5.py) stays honest and is re-pinned here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from tools.germinate import live_run
from tools.germinate.host_substrate import root_launcher as RL
from tools.germinate.host_substrate.tests.test_root_launcher import FW, NONCE, build
from tools.germinate.planner_sandbox.launcher import systemd_run_shape

UNIT = f"pantheon-germinate-c5-alice-{NONCE}"


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(RL, "ROOT_LAUNCHER_LIVE", True)
    monkeypatch.setattr(RL, "LIVE_PROFILES", MappingProxyType({"seat-run": True, "c5-canary": True}))


def _spec(mode):
    return build("c5-canary", mode=mode, gates_dir="/srv/pantheon/gates")


@pytest.mark.parametrize("mode", RL.CANARY_MODES)
def test_c5_3_4_5_canary_argv_under_system_transport(live, mode):
    spec = _spec(mode)
    assert "--user" not in spec.argv
    assert spec.argv[0] == "/usr/bin/systemd-run"
    assert f"--unit={UNIT}.service" in spec.argv
    for needed in (
        "--property=TemporaryFileSystem=/:mode=755",
        f"--property=BindPaths=/run/{UNIT}",
        f"--property=BindReadOnlyPaths={FW}/{RL.LAUNCHER_REL}",
        f"--property=BindReadOnlyPaths={FW}/{RL.SECCOMP_REL}",
        "--property=User=alice",
        "--property=SupplementaryGroups=",
        f"--property=RuntimeDirectory={UNIT}",
        "--property=CapabilityBoundingSet=",
        "--property=PrivateNetwork=yes",
        "--property=NoNewPrivileges=yes",
    ):
        assert needed in spec.argv, needed
    sep = spec.argv.index("--")
    assert spec.argv[sep + 1:sep + 4] == ("/usr/bin/python3", "-SEI", "-c")


def test_system_scope_set_is_the_documented_one_and_extras_are_ours():
    expected = systemd_run_shape.build_properties(
        scratch_dir=Path(f"/run/{UNIT}"),
        launcher_path=Path(f"{FW}/{RL.LAUNCHER_REL}"),
        seccomp_vendor_dir=Path(f"{FW}/{RL.SECCOMP_REL}"),
        system_scope=True,
    )
    assert "CapabilityBoundingSet=" in expected and "ProtectProc=invisible" in expected
    extras = ["User=alice", "SupplementaryGroups=", f"RuntimeDirectory={UNIT}", "WorkingDirectory=/"]
    for p in extras:
        assert p not in expected


def test_c5_properties_match_exactly(live):
    expected = systemd_run_shape.build_properties(
        scratch_dir=Path(f"/run/{UNIT}"),
        launcher_path=Path(f"{FW}/{RL.LAUNCHER_REL}"),
        seccomp_vendor_dir=Path(f"{FW}/{RL.SECCOMP_REL}"),
        system_scope=True,
    ) + ["User=alice", "SupplementaryGroups=", f"RuntimeDirectory={UNIT}", "WorkingDirectory=/"]
    for mode in RL.CANARY_MODES:
        assert list(_spec(mode).properties) == expected


def _template(spec):
    return spec.argv[spec.argv.index("-c") + 1]


def test_canary_shim_templates_compile_and_name_the_launcher(live):
    for mode in RL.CANARY_MODES:
        t = _template(_spec(mode))
        compile(t, "<c5>", "exec")
        assert repr(f"{FW}/{RL.LAUNCHER_REL}") in t
        assert "import subprocess" not in t and "os.system" not in t and "shell=" not in t
    assert "--canary=network" in _template(_spec("network"))
    assert "/srv/pantheon/gates" in _template(_spec("authority-holding"))
    assert "/srv/pantheon/gates" not in _template(_spec("network"))
    assert "plan_install" in _template(_spec("data-only-return"))


def test_canary_shim_execs_the_launcher_with_stdin_at_eof(tmp_path):
    """Run the shim text for real against a stand-in launcher: it must exec it under -SEI with the
    canary argv, stdin at EOF, and the launcher's stdout passes straight through."""

    fake = tmp_path / "launcher.py"
    fake.write_text(
        "import sys\n"
        "data = sys.stdin.read()\n"
        "print('CANARY_RESULT:DENIED:argv=' + ','.join(sys.argv[1:]) + ':stdin=' + repr(data) + ':flags=' + str(sys.flags.isolated))\n"
    )
    template = RL.CANARY_TEMPLATE.format(launcher=repr(str(fake)), args=", ".join(repr(a) for a in ["--canary=network", "/scratch"]))
    done = subprocess.run([sys.executable, "-c", template], capture_output=True, text=True, timeout=30, cwd=str(tmp_path))
    assert done.returncode == 0, done.stderr
    out = done.stdout.strip()
    assert out == "CANARY_RESULT:DENIED:argv=--canary=network,/scratch:stdin='':flags=1"
    assert live_run.parse_canary("network", live_run.RL.LaunchResult(0, done.stdout, "", "u")).verdict == "green"


def test_plan_shim_writes_the_component_then_execs_plan_install(tmp_path):
    fake = tmp_path / "launcher.py"
    fake.write_text(
        "import sys, json\n"
        "ns = {}\n"
        "exec(open(sys.argv[2]).read(), ns)\n"
        "print(json.dumps(ns['plan_install'](None)))\n"
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    template = RL.CANARY_PLAN_TEMPLATE.format(
        scratch=repr(str(scratch)), source=repr(RL.CANARY_COMPONENT_SOURCE), launcher=repr(str(fake))
    )
    done = subprocess.run([sys.executable, "-c", template], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert (scratch / "canary_component.py").read_text() == RL.CANARY_COMPONENT_SOURCE
    assert live_run.parse_canary("data-only-return", live_run.RL.LaunchResult(0, done.stdout, "", "u")).verdict == "green"


def test_c5_canary_outcome_parsing_red_green_through_launch(live):
    class Runner:
        def __init__(self, out):
            self.out = out

        def run(self, argv):
            from tools._organ_lifecycle import CommandResult

            return CommandResult(0, self.out, "")

    spec = _spec("host-mutation")
    assert live_run.parse_canary("host-mutation", RL.launch(spec, Runner("CANARY_RESULT:DENIED:ENOENT\n"))).verdict == "green"
    assert live_run.parse_canary("host-mutation", RL.launch(spec, Runner("CANARY_RESULT:ALLOWED:wrote\n"))).verdict == "red"


def test_c5_user_scope_skip_preserved(monkeypatch):
    """The user-scope path is unchanged: no system-only properties, and the TemporaryFileSystem
    probe still raises the typed PrimitiveUnavailable (which test_launcher_c5.py turns into a SKIP)."""

    props = systemd_run_shape.build_properties(
        scratch_dir=Path("/s"), launcher_path=Path("/l.py"), seccomp_vendor_dir=Path("/v"),
    )
    assert "CapabilityBoundingSet=" not in props and "ProtectProc=invisible" not in props
    assert "TemporaryFileSystem=/:mode=755" in props
    from tools.germinate.planner_sandbox.harness import sandbox_runner

    monkeypatch.setattr(
        sandbox_runner, "run_in_sandbox",
        lambda *a, **k: SimpleNamespace(returncode=226, stdout="", stderr="status=226/NAMESPACE"),
    )
    with pytest.raises(systemd_run_shape.PrimitiveUnavailable) as info:
        systemd_run_shape.probe_temporary_filesystem_support(scratch_dir=Path("/s"))
    assert info.value.name == "temporary-filesystem" and "226" in info.value.reason


def test_system_transport_adds_no_new_properties_to_the_shape_builder():
    import inspect

    sig = inspect.signature(systemd_run_shape.build_properties)
    assert list(sig.parameters) == ["scratch_dir", "launcher_path", "seccomp_vendor_dir", "component_module_dir",
                                    "system_scope", "include_filesystem_root"]
