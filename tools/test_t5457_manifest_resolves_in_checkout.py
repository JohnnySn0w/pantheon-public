"""T-5457: manifest readers resolve instance.yaml inside the checkout under test.

Fails if PANTHEON_INSTANCE_MANIFEST leaks into a test run (a seat session that
inherited the consume unit's live-clone path), because then a worktree run would
silently validate the LIVE manifest instead of its own.
"""

import os
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import persona_bringup  # noqa: E402
import proactive_swap_init  # noqa: E402


class ManifestResolvesInCheckoutTests(unittest.TestCase):
    def assert_in_checkout(self, path: Path) -> None:
        self.assertEqual(Path(path).resolve(), (REPO / "instance.yaml").resolve())

    def test_env_override_absent_in_test_runs(self) -> None:
        self.assertNotIn(
            "PANTHEON_INSTANCE_MANIFEST", os.environ,
            "leaked from the seat launch; restart the seat onto the T-5457 runtime-controller code",
        )

    def test_proactive_swap_init_default_is_this_checkout(self) -> None:
        self.assert_in_checkout(proactive_swap_init._default_manifest())

    def test_persona_bringup_default_is_this_checkout(self) -> None:
        self.assert_in_checkout(persona_bringup.DEFAULT_MANIFEST)


if __name__ == "__main__":
    unittest.main()
