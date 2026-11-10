"""T-5006: tools/stage-echo-worktree.sh must honour an ECHO_CANONICAL override, so a seat
other than HO (mason/artisan, each with its own ~/echo-prismatic) can stage a lane."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "stage-echo-worktree.sh"


class EchoCanonicalOverrideTest(unittest.TestCase):
    def _run(self, env_extra):
        env = {k: v for k, v in os.environ.items() if k != "ECHO_CANONICAL"}
        env["ECHO_STAGE_DISK_FLOOR_GB"] = "0"  # T-4514: hermetic; host free space must not decide the result
        env.update(env_extra)
        return subprocess.run(["bash", str(SCRIPT), "T-0000"], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_override_is_the_path_the_script_checks(self):
        with tempfile.TemporaryDirectory() as td:
            missing = str(Path(td) / "no-such-echo-clone")
            proc = self._run({"ECHO_CANONICAL": missing})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"{missing} not found", proc.stderr)

    def test_default_is_unchanged(self):
        src = SCRIPT.read_text()
        self.assertIn('ECHO_CANONICAL="${ECHO_CANONICAL:-/home/claude/echo-prismatic}"', src)


class LaneTargetDefaultTest(unittest.TestCase):
    """T-6014: the shared lane target-dir defaults under $HOME, not /home/claude, so a
    non-claude seat staging from its own clone doesn't die on mkdir /home/claude."""

    def test_lane_target_defaults_under_home(self):
        ticket = f"T-6014-test-{os.getpid()}"
        wt = Path(f"/tmp/echo-{ticket}")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            origin, canon, home = td / "origin.git", td / "canon", td / "home"
            home.mkdir()
            git = lambda *a, cwd=None: subprocess.run(["git", *a], cwd=cwd, check=True,
                                                      capture_output=True, text=True)
            git("init", "-q", "--bare", "-b", "main", str(origin))
            git("clone", "-q", str(origin), str(canon))
            (canon / ".cargo").mkdir()
            (canon / ".cargo" / "config.toml").write_text(
                '[env]\nORT_DYLIB_PATH = { value = "vendor/x.so", relative = true }\n')
            git("-c", "user.name=t", "-c", "user.email=t@t", "add", ".", cwd=canon)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed", cwd=canon)
            git("push", "-q", "origin", "HEAD:main", cwd=canon)
            env = {k: v for k, v in os.environ.items()
                   if k not in ("ECHO_CANONICAL", "ECHO_STAGE_LANE_TARGET_DIR", "ECHO_STAGE_SHARED_TARGET")}
            env.update({"HOME": str(home), "ECHO_CANONICAL": str(canon), "ECHO_STAGE_DISK_FLOOR_GB": "0",
                        "GIT_CONFIG_GLOBAL": "/dev/null"})
            try:
                proc = subprocess.run(["bash", str(SCRIPT), ticket], env=env,
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                cfg = (wt / ".cargo" / "config.toml").read_text()
                self.assertIn(f'target-dir = "{home}/.cache/echo-lane-target"', cfg)
                self.assertTrue((home / ".cache" / "echo-lane-target").is_dir())
            finally:
                subprocess.run(["git", "-C", str(canon), "worktree", "remove", "--force", str(wt)],
                               capture_output=True)
                subprocess.run(["rm", "-rf", str(wt)])


if __name__ == "__main__":
    unittest.main()
