"""T-5480: tools/harness_adapter/codex_shell_box.py (hook block + trust wiring in a codex config.toml).

Intent: a codex seat's config.toml carries the shell-box PreToolUse hook and its codex trust
hash in ONE write, idempotently, removable. All homes are temp dirs; codex's app-server is a
stub (``fake_read_hooks``). Nothing touches a real ~/.codex.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path

from tools.harness_adapter import codex_shell_box as csb

WORKER_GUARD = """\
model = "gpt-x"

[[hooks.PreToolUse]]
matcher = "Bash|apply_patch"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "/usr/bin/python3 -I /h/pantheon-config/tools/hooks/worker-guard.py"
timeout = 30

[hooks.state."KEY0"]
trusted_hash = "sha256:guard"

[projects."/h"]
trust_level = "trusted"
"""


def fake_read_hooks(executable, *, codex_home, cwd=None, timeout=15.0):
    """Stand-in for codex hooks/list: hash covers matcher+hook definition, like codex's."""
    text = (Path(codex_home) / "config.toml").read_text()
    parsed = tomllib.loads(text) if text.strip() else {}
    state = (parsed.get("hooks") or {}).get("state") or {}
    out = []
    for g, group in enumerate((parsed.get("hooks") or {}).get("PreToolUse") or []):
        for h, hook in enumerate(group.get("hooks") or []):
            key = f"{os.path.realpath(codex_home)}/config.toml:pre_tool_use:{g}:{h}"
            digest = "sha256:" + hashlib.sha256(
                json.dumps([group.get("matcher"), hook], sort_keys=True).encode()).hexdigest()
            stored = (state.get(key) or {}).get("trusted_hash")
            out.append({"key": key, "command": hook.get("command"), "eventName": "preToolUse",
                        "currentHash": digest, "enabled": True,
                        "trustStatus": "trusted" if stored == digest else "modified" if stored else "untrusted"})
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        (self.home / ".codex").mkdir(parents=True)
        self.codex_home = self.home / ".codex"
        oom = self.home / "pantheon-config/tools/oom"
        oom.mkdir(parents=True)
        for name in ("pantheon-codex-shell-box-hook.py", "pantheon-shell-box.sh"):
            (oom / name).write_text("#!/bin/sh\n")
            (oom / name).chmod(0o755)
        self.exe = Path("/nonexistent/codex")

    def wire(self, text, **kw):
        return csb.wire(text, self.codex_home, self.home, executable=self.exe,
                        read_hooks=kw.pop("read_hooks", fake_read_hooks), **kw)


class CommandTests(Base):
    def test_command_and_paths(self):
        self.assertEqual(csb.hook_command(self.home),
                         f"/usr/bin/python3 -I {self.home}/pantheon-config/tools/oom/pantheon-codex-shell-box-hook.py")
        self.assertEqual(csb.hook_script(self.home).name, "pantheon-codex-shell-box-hook.py")
        self.assertEqual(csb.box_script(self.home).name, "pantheon-shell-box.sh")


class WireTests(Base):
    def test_absent_config_is_wired_and_trusted_in_one_text(self):
        cls, new = self.wire("")
        self.assertEqual(cls, "absent")
        parsed = tomllib.loads(new)
        [group] = parsed["hooks"]["PreToolUse"]
        self.assertEqual(group["matcher"], "Bash")
        [hook] = group["hooks"]
        self.assertEqual((hook["type"], hook["timeout"], hook["command"]),
                         ("command", 10, csb.hook_command(self.home)))
        key = f"{os.path.realpath(self.codex_home)}/config.toml:pre_tool_use:0:0"
        self.assertEqual(parsed["hooks"]["state"][key]["trusted_hash"], fake_read_hooks(
            self.exe, codex_home=self._scratch(new))[0]["currentHash"])

    def _scratch(self, text):
        d = Path(self._tmp.name) / "scratch"
        d.mkdir(exist_ok=True)
        (d / "config.toml").write_text(text)
        return d

    def test_worker_guard_survives_and_ours_is_appended_second(self):
        cls, new = self.wire(WORKER_GUARD)
        self.assertEqual(cls, "absent")
        self.assertTrue(new.startswith(WORKER_GUARD.rstrip("\n")))
        parsed = tomllib.loads(new)
        self.assertEqual(len(parsed["hooks"]["PreToolUse"]), 2)
        self.assertEqual(parsed["hooks"]["state"]["KEY0"], {"trusted_hash": "sha256:guard"})
        key = f"{os.path.realpath(self.codex_home)}/config.toml:pre_tool_use:1:0"
        self.assertIn(key, parsed["hooks"]["state"])

    def test_idempotent(self):
        _, once = self.wire(WORKER_GUARD)
        cls, twice = self.wire(once)
        self.assertEqual((cls, twice), ("matching", once))

    def test_block_without_trust_gets_only_the_hash(self):
        _, wired = self.wire(WORKER_GUARD)
        key = f"{os.path.realpath(self.codex_home)}/config.toml:pre_tool_use:1:0"
        untrusted = wired.split(f'[hooks.state."{key}"]')[0].rstrip("\n") + "\n\n" + \
            wired.split('trusted_hash = "', 2)[2].split("\n", 1)[1].lstrip("\n")
        self.assertNotIn(key, tomllib.loads(untrusted).get("hooks", {}).get("state", {}))
        cls, fixed = self.wire(untrusted)
        self.assertEqual(cls, "untrusted")
        self.assertIn(key, tomllib.loads(fixed)["hooks"]["state"])

    def test_stale_hash_is_conflicting_not_overwritten(self):
        _, wired = self.wire("")
        key = f"{os.path.realpath(self.codex_home)}/config.toml:pre_tool_use:0:0"
        bad = wired.replace(wired.split('trusted_hash = "')[1].split('"')[0], "sha256:stale")
        with self.assertRaises(csb.WiringRefused):
            self.wire(bad)

    def test_changed_definition_is_refused(self):
        _, wired = self.wire("")
        with self.assertRaises(csb.WiringRefused):
            self.wire(wired.replace('matcher = "Bash"', 'matcher = "Bash|apply_patch"'))
        with self.assertRaises(csb.WiringRefused):
            self.wire(wired.replace("timeout = 10", "timeout = 99"))

    def test_unparseable_toml_is_refused(self):
        with self.assertRaises(csb.WiringRefused):
            self.wire("this is = = not toml")

    def test_missing_script_or_box_is_refused(self):
        csb.hook_script(self.home).unlink()
        with self.assertRaises(csb.WiringRefused):
            self.wire("")
        csb.hook_script(self.home).write_text("x")
        csb.hook_script(self.home).chmod(0o755)
        csb.box_script(self.home).chmod(0o644)
        with self.assertRaises(csb.WiringRefused):
            self.wire("")

    def test_unreadable_hash_is_refused(self):
        with self.assertRaises(csb.WiringRefused):
            self.wire("", read_hooks=lambda *a, **k: None)


class StatusTests(Base):
    def test_status_of_wired_and_unwired(self):
        self.assertFalse(csb.status("", self.codex_home, self.home)["block_present"])
        _, wired = self.wire(WORKER_GUARD)
        st = csb.status(wired, self.codex_home, self.home)
        self.assertTrue(st["block_present"] and st["definition_ok"] and st["trusted_hash"])
        self.assertTrue(st["hook_executable"] and st["box_executable"])
        self.assertEqual(st["problems"], [])

    def test_status_names_each_problem(self):
        _, wired = self.wire("")
        bad = wired.replace('matcher = "Bash"', 'matcher = "Bash|apply_patch"')
        self.assertTrue(csb.status(bad, self.codex_home, self.home)["problems"])
        csb.box_script(self.home).chmod(0o644)
        self.assertFalse(csb.status(wired, self.codex_home, self.home)["box_executable"])


class RemoveTests(Base):
    def test_remove_restores_the_previous_document(self):
        for original in ("", WORKER_GUARD, 'model = "x"\n'):
            with self.subTest(original=original[:20]):
                _, wired = self.wire(original)
                stripped = csb.remove(wired, self.codex_home, self.home)
                self.assertEqual(tomllib.loads(stripped), tomllib.loads(original) if original.strip() else {})

    def test_remove_refuses_when_not_the_last_group(self):
        _, wired = self.wire("")
        later = wired + '\n[[hooks.PreToolUse]]\nmatcher = "x"\n\n[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "/y"\n'
        with self.assertRaises(csb.WiringRefused):
            csb.remove(later, self.codex_home, self.home)

    def test_remove_when_absent_returns_none(self):
        self.assertIsNone(csb.remove(WORKER_GUARD, self.codex_home, self.home))


if __name__ == "__main__":
    unittest.main()
