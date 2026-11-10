"""T-4531 step 2: inert staging of the claude `openrouter-api` profile.

Nothing may reference the profile yet; base_url and the T-6005 override flag belong
to the future worker role binding.
"""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml

from tools.harness_adapter import diagnostics
from tools.harness_adapter.claude import CLAUDE_NATIVE_KEYS
from tools.harness_adapter.diagnostics import RuntimeProfileError, resolve_launch_config
from tools.test_harness_runtime_profiles import _manifest

REPO = Path(__file__).resolve().parent.parent
PROFILE = "openrouter-api"


def _real() -> dict:
    return yaml.safe_load((REPO / "instance.yaml").read_text(encoding="utf-8"))


class NativeKeyParityTests(unittest.TestCase):
    def test_diagnostics_copy_matches_adapter(self) -> None:
        self.assertEqual(diagnostics._NATIVE_KEYS["claude"], set(CLAUDE_NATIVE_KEYS))


class RealManifestInertTests(unittest.TestCase):
    def test_profile_declared_api_meter(self) -> None:
        claude = _real()["harness_runtime"]["harnesses"]["claude"]
        prof = claude["profiles"][PROFILE]
        self.assertEqual(
            (prof["mode"], prof["meter"], prof["quota_source"]),
            ("interactive", "api", "provider-api"),
        )
        self.assertEqual(
            prof["executable"], claude["profiles"]["interactive-subscription"]["executable"]
        )

    def test_default_profile_unchanged(self) -> None:
        claude = _real()["harness_runtime"]["harnesses"]["claude"]
        self.assertEqual(claude["default_profile"], "interactive-subscription")

    def test_no_role_references_profile(self) -> None:
        roles = _real()["harness_runtime"]["roles"]
        for name, role in roles.items():
            for harness, binding in role.items():
                if isinstance(binding, dict):
                    self.assertNotEqual(
                        binding.get("profile"), PROFILE, f"{name}.{harness} references {PROFILE}"
                    )
            for fb in role.get("fallbacks") or []:
                self.assertNotIn(PROFILE, str(fb), f"{name} fallback references {PROFILE}")


class SyntheticBindingTests(unittest.TestCase):
    def _manifest(self, native: dict) -> dict:
        m = copy.deepcopy(_manifest())
        claude = m["harness_runtime"]["harnesses"]["claude"]
        claude["profiles"][PROFILE] = {
            "executable": "{account_home}/bin/claude",
            "mode": "interactive",
            "meter": "api",
            "quota_source": "provider-api",
        }
        m["harness_runtime"]["roles"]["recovery"]["claude"] = {
            "profile": PROFILE,
            "model": "manifest-model",
            "native": native,
        }
        return m

    def _resolve(self, native: dict):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            exe = home / "bin" / "claude"
            exe.parent.mkdir()
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            return resolve_launch_config(
                self._manifest(native),
                role="recovery",
                account_home=home,
                selected_harness="claude",
            )

    def test_base_url_binding_loads(self) -> None:
        launch = self._resolve({"base_url": "https://openrouter.ai/api"})
        self.assertEqual(launch.native["base_url"], "https://openrouter.ai/api")

    def test_unknown_native_key_still_raises(self) -> None:
        with self.assertRaisesRegex(RuntimeProfileError, "bogus"):
            self._resolve({"base_url": "https://openrouter.ai/api", "bogus": 1})


if __name__ == "__main__":
    unittest.main()
