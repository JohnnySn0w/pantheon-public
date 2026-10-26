"""Tests for tools/echo-cargo.sh (T-4822). Run: python3 -m unittest tools.test_echo_cargo"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

WRAPPER = Path(__file__).resolve().parent / "echo-cargo.sh"
FAKE_CARGO = """#!/usr/bin/env bash
echo "TARGET=${CARGO_TARGET_DIR:-}"
echo "UMASK=$(umask)"
echo "ARGS=$*"
"""


class EchoCargoWrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        cargo = self.bin / "cargo"
        cargo.write_text(FAKE_CARGO)
        cargo.chmod(0o755)
        self.shared = root / "echo-target"
        self.shared.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args, **env_over):
        env = {k: v for k, v in os.environ.items() if k != "CARGO_TARGET_DIR"}
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["PANTHEON_ECHO_TARGET_DIR"] = str(self.shared)
        env.update(env_over)
        return subprocess.run(["bash", str(WRAPPER), *args], capture_output=True, text=True,
                              env=env, check=False)

    def test_defaults_to_shared_target_umask_002_and_passes_args(self):
        r = self._run("test", "-p", "echo-core")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"TARGET={self.shared}", r.stdout)
        self.assertIn("UMASK=0002", r.stdout)
        self.assertIn("ARGS=test -p echo-core", r.stdout)

    def test_adopt_runs_before_cargo_on_the_shared_target(self):
        r = self._run("build")
        self.assertIn("[echo_target_adopt]", r.stderr)

    def test_inherited_target_wins_and_skips_adopt(self):
        own = Path(self.tmp.name) / "private-target"
        own.mkdir()
        r = self._run("build", CARGO_TARGET_DIR=str(own))
        self.assertIn(f"TARGET={own}", r.stdout)
        self.assertNotIn("[echo_target_adopt]", r.stderr)

    def test_adopt_failure_never_blocks_the_build(self):
        r = self._run("build", ECHO_ADOPT_PACKAGES="--not-a-valid-arg-shape--")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ARGS=build", r.stdout)

    def test_missing_cargo_exits_127(self):
        env_path = "/usr/bin:/bin"
        r = subprocess.run(["bash", str(WRAPPER), "build"], capture_output=True, text=True,
                           env={"PATH": env_path, "HOME": self.tmp.name,
                                "PANTHEON_ECHO_TARGET_DIR": str(self.shared)}, check=False)
        self.assertEqual(r.returncode, 127)


    def test_worktree_switch_runs_on_the_shared_target_T5152(self):
        ws = Path(self.tmp.name) / "ws"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "lib.rs").write_text("//\n")
        meta = ('{"workspace_root": "%s", "workspace_members": ["c"], "packages": '
                '[{"id": "c", "targets": [{"kind": ["lib"], "src_path": "%s/src/lib.rs"}]}]}') % (ws, ws)
        cargo = self.bin / "cargo"
        cargo.write_text('#!/usr/bin/env bash\nif [[ "$1" == metadata ]]; then echo \'%s\'; exit 0; fi\n%s'
                         % (meta, FAKE_CARGO.split("\n", 1)[1]))
        env = {k: v for k, v in os.environ.items() if k != "CARGO_TARGET_DIR"}
        env.update(PATH=f"{self.bin}:{env['PATH']}", PANTHEON_ECHO_TARGET_DIR=str(self.shared))
        r = subprocess.run(["bash", str(WRAPPER), "build"], capture_output=True, text=True,
                           env=env, cwd=ws, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ARGS=build", r.stdout)
        self.assertIn("[echo_worktree_switch] workspace changed", r.stderr)
        self.assertEqual((self.shared / ".echo-last-workspace").read_text().strip(), str(ws))


    def _slow_cargo(self, log: Path) -> None:
        cargo = self.bin / "cargo"
        cargo.write_text('#!/usr/bin/env bash\n[[ "$1" == metadata ]] && exit 1\n'
                         f'echo "start $2 $(date +%s.%N)" >> {log}\nsleep 1\n'
                         f'echo "end $2 $(date +%s.%N)" >> {log}\n')

    def _concurrent(self, **env_over) -> list[tuple[str, str, float]]:
        log = Path(self.tmp.name) / "runs.log"
        self._slow_cargo(log)
        env = {k: v for k, v in os.environ.items() if k not in ("CARGO_TARGET_DIR", "ECHO_CARGO_LOCK_HELD")}
        env.update(PATH=f"{self.bin}:{env['PATH']}", PANTHEON_ECHO_TARGET_DIR=str(self.shared), **env_over)
        procs = [subprocess.Popen(["bash", str(WRAPPER), "build", tag], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for tag in ("A", "B")]
        for p in procs:
            p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0)
        return [(w, t, float(ts)) for w, t, ts in (ln.split() for ln in log.read_text().splitlines())]

    def test_wrapped_builds_on_the_shared_target_never_overlap_T5296(self):
        ev = sorted(self._concurrent(), key=lambda e: e[2])
        self.assertEqual([e[0] for e in ev], ["start", "end", "start", "end"], ev)
        self.assertEqual(ev[0][1], ev[1][1])     # one build ran start-to-end before the other began
        self.assertTrue((self.shared / ".echo-wrapper-lock").exists())

    def test_nested_wrapper_skips_the_lock_T5296(self):
        ev = sorted(self._concurrent(ECHO_CARGO_LOCK_HELD="1"), key=lambda e: e[2])
        self.assertEqual([e[0] for e in ev], ["start", "start", "end", "end"], ev)


if __name__ == "__main__":
    unittest.main()
