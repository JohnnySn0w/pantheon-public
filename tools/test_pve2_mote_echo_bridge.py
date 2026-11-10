"""Tests for tools/pve2_mote_echo_bridge.py (T-6220): render fails closed, lint pins the route.

The live QoS-1/reconnect evidence needs real brokers and runs on pve2 via
tools/pve2_mote_echo_bridge_evidence.py; these tests need no mosquitto binary.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pve2_mote_echo_bridge as b  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = (ROOT / "hosts/pve2/mote-echo-bridge/bridge.conf.template").read_text(encoding="utf-8")
EXAMPLE = (ROOT / "hosts/pve2/mote-echo-bridge/upstream.env.example").read_text(encoding="utf-8")

GOOD = (
    "UPSTREAM_HOST=echo.example.net\nUPSTREAM_PORT=8883\nUPSTREAM_TLS=true\n"
    "UPSTREAM_USERNAME=pve2relay\nUPSTREAM_PASSWORD=s3cr3t-token\n"
)


def render(env_text, template=TEMPLATE):
    return b.render_text(template, env_text, conf_dir="/etc/pantheon/mote-echo-bridge",
                         state_dir="/var/lib/pantheon/mote-echo-bridge")


class Lint(unittest.TestCase):
    def test_shipped_template_carries_exactly_the_approved_route(self):
        self.assertEqual(b.lint_text(TEMPLATE), [])

    def test_extra_topic_wildcard_and_qos0_are_caught(self):
        for bad in (TEMPLATE + 'topic echo/# both 1 "" ""\n',
                    TEMPLATE.replace("topic echo/home/tap/tts in 1", "topic echo/home/tap/tts in 0"),
                    TEMPLATE.replace("cleansession false\n", "")):
            with self.subTest(bad=bad[-60:]):
                self.assertNotEqual(b.lint_text(bad), [])


class Render(unittest.TestCase):
    def test_good_env_fills_every_marker_and_keeps_the_route(self):
        out = render(GOOD)
        self.assertNotRegex(out, r"@[A-Z_]+@")
        self.assertIn("address echo.example.net:8883", out)
        self.assertIn("bridge_capath /etc/ssl/certs\nbridge_insecure false", out)
        self.assertIn("listener 1883 127.0.0.1\nallow_anonymous true", out)
        self.assertEqual(b.lint_text(out), [])

    def test_shipped_example_is_refused_until_engineering_fills_it(self):
        with self.assertRaisesRegex(b.Refused, "placeholder"):
            render(EXAMPLE)

    def test_refusals_fail_closed(self):
        cases = {
            "missing key": GOOD.replace("UPSTREAM_PASSWORD=s3cr3t-token\n", ""),
            "unknown key": GOOD + "UPSTREAM_TOPIC=echo/#\n",
            "duplicate key": GOOD + "UPSTREAM_PORT=1883\n",
            "whitespace password": GOOD.replace("s3cr3t-token", "has space"),
            "CHANGE_ME": GOOD.replace("pve2relay", "CHANGE_ME"),
            "bad port": GOOD.replace("8883", "99999"),
            "bad tls": GOOD.replace("UPSTREAM_TLS=true", "UPSTREAM_TLS=yes"),
            "bad host": GOOD.replace("echo.example.net", "echo.example.net\naddress evil:1"),
            "lan bind without auth": GOOD + "LOCAL_BIND=192.168.2.216\n",
            "health port clash": GOOD + "LOCAL_PORT=18839\n",
            "relative cafile": GOOD + "UPSTREAM_CAFILE=ca.pem\n",
            "cafile without tls": GOOD.replace("UPSTREAM_TLS=true", "UPSTREAM_TLS=false")
            + "UPSTREAM_CAFILE=/etc/x.pem\n",
        }
        for name, env in cases.items():
            with self.subTest(name):
                with self.assertRaises(b.Refused):
                    render(env)

    def test_refusal_message_never_echoes_a_secret(self):
        with self.assertRaises(b.Refused) as cm:
            render(GOOD.replace("s3cr3t-token", "s3cr3t token"))
        self.assertNotIn("s3cr3t", str(cm.exception))

    def test_marker_text_inside_a_credential_is_kept_literally(self):
        # Specialist #1753 note 4: one pass, so a value containing @MARKER@ is neither re-substituted
        # nor falsely refused as an unfilled marker.
        out = render(GOOD.replace("s3cr3t-token", "p@UPSTREAM_HOST@w"))
        self.assertIn("remote_password p@UPSTREAM_HOST@w", out)

    def test_lan_bind_with_password_file_disables_anonymous(self):
        out = render(GOOD + "LOCAL_BIND=192.168.2.216\nLOCAL_PASSWORD_FILE=/etc/pantheon/secrets/m.passwd\n")
        self.assertIn("listener 1883 192.168.2.216\nallow_anonymous false\npassword_file "
                      "/etc/pantheon/secrets/m.passwd", out)

    def test_cli_missing_credential_file_exits_2_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "bridge.conf"
            rc = b.main(["render", "--template", str(ROOT / "hosts/pve2/mote-echo-bridge/bridge.conf.template"),
                         "--env", str(Path(d) / "absent.env"), "--out", str(out),
                         "--conf-dir", "/x", "--state-dir", "/y"])
            self.assertEqual(rc, 2)
            self.assertFalse(out.exists())

    def test_cli_render_writes_mode_0600(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / "u.env"
            env.write_text(GOOD, encoding="utf-8")
            out = Path(d) / "bridge.conf"
            rc = b.main(["render", "--template", str(ROOT / "hosts/pve2/mote-echo-bridge/bridge.conf.template"),
                         "--env", str(env), "--out", str(out), "--conf-dir", "/x", "--state-dir", "/y"])
            self.assertEqual(rc, 0)
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)


class Health(unittest.TestCase):
    def _run(self, rc, stdout, stderr=""):
        return lambda *_a, **_k: subprocess.CompletedProcess([], rc, stdout, stderr)

    def test_connected_is_0(self):
        self.assertEqual(b.health(run=self._run(0, "1\n"))[0], 0)

    def test_disconnected_is_1(self):
        rc, detail = b.health(run=self._run(0, "0\n"))
        self.assertEqual(rc, 1)
        self.assertIn("DISCONNECTED", detail)

    def test_no_state_or_broker_down_is_unknown_1(self):
        for run in (self._run(27, ""), self._run(1, "", "Connection refused")):
            rc, detail = b.health(run=run)
            self.assertEqual(rc, 1)
            self.assertIn("UNKNOWN", detail)

    def test_missing_client_binary_is_unknown_1(self):
        def run(*_a, **_k):
            raise FileNotFoundError("mosquitto_sub")
        self.assertEqual(b.health(run=run)[0], 1)


if __name__ == "__main__":
    unittest.main()
