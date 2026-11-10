"""T-6033: the canonical tailscale-serve-restore script (held in the SOP) re-applies both line forms
of services.conf: <name>:<port> root proxies and <name>:/<path>:/<dir> path mounts.

The script is extracted from sop/standard/adding-a-new-tailscale-service.md, so the copy that gets
installed is the copy under test. It runs against a fake tailscale binary (TAILSCALE_BIN) that
reports BackendState=Running and logs each serve invocation."""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SOP = Path(__file__).resolve().parent.parent / "sop/standard/adding-a-new-tailscale-service.md"


def _script() -> str:
    text = SOP.read_text()
    m = re.search(r"### `/usr/local/sbin/tailscale-serve-restore`.*?```bash\n(.*?)```", text, re.S)
    assert m, "restore script block not found in the SOP"
    return m.group(1)


class RestoreScriptTest(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.log = self.td / "calls.log"
        fake = self.td / "tailscale"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == status ]]; then echo \'{"BackendState": "Running"}\'; exit 0; fi\n'
            f'printf "%s\\n" "$*" >> {self.log}\n'
            'if [[ "$*" == *FAILME* ]]; then exit 3; fi\n')
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.script = self.td / "restore.sh"
        self.script.write_text(_script())
        self.mountdir = self.td / "updates"
        self.mountdir.mkdir()

    def run_conf(self, conf_text):
        conf = self.td / "services.conf"
        conf.write_text(conf_text)
        env = dict(os.environ, TAILSCALE_BIN=str(self.td / "tailscale"))
        proc = subprocess.run(["bash", str(self.script), str(conf)], env=env,
                              capture_output=True, text=True, timeout=30)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return proc, calls

    def test_root_proxy_and_path_mount(self):
        proc, calls = self.run_conf(
            "# comment\n\nkino:8443\n"
            f"echo-recorder:8447\necho-recorder:/updates:{self.mountdir}\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls, [
            "serve --service=svc:kino --bg --https=443 http://127.0.0.1:8443",
            "serve --service=svc:echo-recorder --bg --https=443 http://127.0.0.1:8447",
            f"serve --service=svc:echo-recorder --bg --https=443 --set-path=/updates {self.mountdir}",
        ])

    def test_missing_mount_dir_is_skipped_loudly_and_others_still_apply(self):
        proc, calls = self.run_conf(f"echo-recorder:/updates:{self.td}/nope\nkino:8443\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("is not a directory", proc.stderr)
        self.assertEqual(calls, ["serve --service=svc:kino --bg --https=443 http://127.0.0.1:8443"])

    def test_malformed_lines_are_skipped_and_flagged(self):
        proc, calls = self.run_conf("bad:notaport\nalso:/p:/d:extra\nrel:updates:/x\nkino:8443\n")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr.count("malformed"), 3)
        self.assertEqual(calls, ["serve --service=svc:kino --bg --https=443 http://127.0.0.1:8443"])

    def test_one_failed_apply_does_not_drop_the_rest(self):
        proc, calls = self.run_conf("FAILME:8000\nkino:8443\n")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
