"""T-4533 / T-4643 step 4: fleet-usage launch env. Only PANTHEON_FLEET_USAGE_SEAT is injected;
the mod itself loads from managed settings, never CLAUDE_CODE_PLUGIN_DIRS."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import fleet_usage_env as fue

PLUGIN = "CLAUDE_CODE_PLUGIN_DIRS"
SEAT = "PANTHEON_FLEET_USAGE_SEAT"


def _home(root: Path, *, mod: bool = True, settings_env: dict | None = None, raw: str | None = None) -> Path:
    if mod:
        (root / "pantheon-config" / "tools" / "mods" / "fleet-usage").mkdir(parents=True)
    if settings_env is not None or raw is not None:
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.json").write_text(
            raw if raw is not None else json.dumps({"env": settings_env})
        )
    return root


class FleetUsageLaunchEnvTests(unittest.TestCase):
    def test_only_the_seat_key_for_a_seat_with_no_settings_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(Path(tmp), settings_env={})
            self.assertEqual(fue.fleet_usage_launch_env("mason", home), {SEAT: "mason"})

    def test_never_injects_plugin_dirs(self) -> None:
        # T-4643 step 4: the managed org mod replaces the sideload (which managed settings lock out).
        with tempfile.TemporaryDirectory() as tmp:
            for persona in ("mason", "artisan", "archivist"):
                self.assertNotIn(PLUGIN, fue.fleet_usage_launch_env(persona, _home(Path(tmp) / persona)))

    def test_seat_is_injected_without_a_mod_dir_in_the_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(fue.fleet_usage_launch_env("mason", _home(Path(tmp), mod=False)), {SEAT: "mason"})

    def test_a_seat_the_settings_already_set_is_not_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(Path(tmp), settings_env={SEAT: "orchestrator"})
            self.assertEqual(fue.fleet_usage_launch_env("orchestrator", home), {})

    def test_empty_persona_injects_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(fue.fleet_usage_launch_env("", _home(Path(tmp))), {})

    def test_unreadable_settings_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(Path(tmp), raw="{not json")
            self.assertEqual(fue.fleet_usage_launch_env("mason", home), {SEAT: "mason"})

    def test_cli_prints_key_value_lines_and_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(Path(tmp), settings_env={})
            argv = [sys.executable, str(Path(fue.__file__)), "--persona", "mason", "--home", str(home)]
            out = subprocess.run(argv, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout.splitlines(), [f"{SEAT}=mason"])


if __name__ == "__main__":
    unittest.main()
