"""T-2633: schema/acceptance test pinning harness_runtime.roles.specialist's
launch shape against the REAL checked-in instance.yaml.

No prior test does this (searched: tools/test_bringup_acceptance_optionals_
{recovery,specialist}.py test unrelated optional watchdog modules, not launch shape;
tools/test_harness_runtime_profiles.py exercises resolve_launch_config /
resolve_controller_config generically via synthetic in-memory fixtures, never
against the checked-in file). Without this, a schema regression in the
specialist stanza would go uncaught until Specialist's live cutover -- exactly the
"inert artifact should be schema-guarded" arc-intent for T-2633's manifest
edit (deliverable 1).

Kept independent of tools/test_persona_cutover_resolve.py's correctness gate
(which also exercises this stanza, through resolve_persona()): this file pins
the raw manifest content directly via yaml.safe_load, so a regression in the
resolver itself can't mask a manifest regression, and vice versa.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTANCE_YAML = REPO_ROOT / "instance.yaml"

EXPECTED_MODEL = "claude-opus-5-5"
EXPECTED_CHANNELS = "plugin:telegram@claude-plugins-official"
EXPECTED_SESSION = "claude-research-bot"


class SpecialistLaunchShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        data = yaml.safe_load(INSTANCE_YAML.read_text(encoding="utf-8"))
        self.specialist = data["harness_runtime"]["roles"]["specialist"]

    def test_model_is_opus_5_5(self) -> None:
        # T-4526 (the operator TG 7988, 2026-10-04): "can you get specialist onto opus
        # 5.5?" Reverses T-4021 (TG 3304, opus 4.8). Effort stays xhigh per
        # the T-3084 fleet-effort-policy.
        self.assertEqual(self.specialist["claude"]["model"], EXPECTED_MODEL)

    def test_channels_is_telegram_plugin(self) -> None:
        self.assertEqual(
            self.specialist["claude"]["native"].get("channels"), EXPECTED_CHANNELS
        )

    def test_permission_mode_is_bypass(self) -> None:
        self.assertEqual(self.specialist["claude"]["native"].get("permission_mode"), "bypassPermissions")

    def test_tmux_session_is_claude_research_bot(self) -> None:
        self.assertEqual(self.specialist["tmux"]["session"], EXPECTED_SESSION)

    def test_post_start_carries_arm_pane_log_before_recovery_boot_kick(self) -> None:
        # Ordering: arm_pane_log first; the T-4092/T-2629 telegram getUpdates-409
        # never-give-up re-assert second (foreground, idempotent + fail-open);
        # recovery_boot_kick last with background:true. (arm_pane_log stays before
        # recovery_boot_kick -- the 409 re-assert was inserted between them.)
        post_start = self.specialist["post_start"]
        self.assertEqual(len(post_start), 3)
        self.assertIn("arm_pane_log.sh", post_start[0]["command"][0])
        self.assertNotIn("background", post_start[0])
        self.assertIn("telegram_poller_409_patch.sh", post_start[1]["command"][0])
        self.assertNotIn("background", post_start[1])
        self.assertIn("recovery_boot_kick.sh", post_start[2]["command"][0])
        self.assertTrue(post_start[2]["background"])

    def test_codex_quota_failover_fallback(self) -> None:
        # T-2927 (operator-directed, TG 7797/7844) SUPERSEDES the original T-2633
        # decision that specialist gets no codex fallback. With GEPA bulk rollouts
        # eating Max-sub quota, every claude-only consume-plane seat (specialist/
        # specialist, sub-orchestrator-1/mason, sub-orchestrator-2/artisan) now carries a
        # codex quota-failover fallback so the quota gate has a swap target.
        self.assertEqual(
            self.specialist.get("fallbacks"), ["codex/interactive-chatgpt"]
        )
        codex = self.specialist["codex"]
        self.assertEqual(codex["profile"], "interactive-chatgpt")
        self.assertEqual(codex["model"], "gpt-5.6-sol")
        overrides = codex["native"]["config_overrides"]
        self.assertEqual(overrides["approval_policy"], "never")
        self.assertEqual(overrides["model_reasoning_effort"], "high")

    def test_persona_binding_unchanged(self) -> None:
        # Deliverable 1 touches ONLY harness_runtime.roles.specialist -- the
        # top-level roles.specialist binding (persona/account/harness) that
        # ticket-lint and agent_role_map read must be untouched.
        data = yaml.safe_load(INSTANCE_YAML.read_text(encoding="utf-8"))
        binding = data["roles"]["specialist"]
        self.assertEqual(binding["persona"], "specialist")
        self.assertEqual(binding["account"], "claude-research")
        self.assertEqual(binding["harness"], "claude-code")


if __name__ == "__main__":
    unittest.main()
