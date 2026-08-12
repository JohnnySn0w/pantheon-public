"""Tests for agent_role_map's live-session project-slug helper (T-2687).

The helper is the single source of truth for the control-plane `-pantheon-config`
project-dir slug, consumed by context_fill_monitor / keep_going / doc_traversal_report.
"""
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import agent_role_map as m  # noqa: E402


class LiveSessionSlugTest(unittest.TestCase):
    def test_consume_plane_seat_gets_pantheon_config_slug(self):
        # A consume-plane seat launches cwd=<account>/pantheon-config (persona-consume
        # WorkingDirectory), so its live jsonl dir is `-home-<account>-pantheon-config`.
        with mock.patch.object(m, "account_persona", return_value="recovery"), \
             mock.patch.object(m, "resolve_instance_role", return_value="recovery"):
            self.assertEqual(
                m.live_session_project_slug("claude-fallback"),
                "-home-claude-fallback-pantheon-config")
            self.assertEqual(
                m.live_session_projects_dir("claude-fallback"),
                "/home/claude-fallback/.claude/projects/-home-claude-fallback-pantheon-config")

    def test_home_cwd_seat_gets_plain_slug(self):
        # orchestrator (orchestrator) is not consume-plane — home-cwd, plain slug.
        with mock.patch.object(m, "account_persona", return_value="orchestrator"), \
             mock.patch.object(m, "resolve_instance_role", return_value="orchestrator"):
            self.assertEqual(m.live_session_project_slug("claude"), "-home-claude")

    def test_sub_orchestrator_seats_get_pantheon_config_slug(self):
        # T-2715: metis/atlas ARE consume-plane (cwd=pantheon-config, verified against their
        # freshest live jsonl) despite supervisor=None. Keying on the consume-plane role set
        # covers them where the old supervisor proxy did not.
        with mock.patch.object(m, "account_persona", return_value="metis"), \
             mock.patch.object(m, "resolve_instance_role", return_value="sub-orchestrator-1"):
            self.assertEqual(m.live_session_project_slug("suborch1"),
                             "-home-suborch1-pantheon-config")
        with mock.patch.object(m, "account_persona", return_value="atlas"), \
             mock.patch.object(m, "resolve_instance_role", return_value="sub-orchestrator-2"):
            self.assertEqual(m.live_session_project_slug("suborch2"),
                             "-home-suborch2-pantheon-config")

    def test_second_head_is_plain(self):
        # mnemosyne (second-head) is mailbox-first at the operator's pane — NOT consume-plane.
        with mock.patch.object(m, "account_persona", return_value="mnemosyne"), \
             mock.patch.object(m, "resolve_instance_role", return_value="second-head"):
            self.assertEqual(m.live_session_project_slug("exocortex"), "-home-exocortex")

    def test_unknown_account_fails_open_to_plain(self):
        # An account with no persona binding must not raise and must not fabricate a
        # -pantheon-config slug — plain is the safe default.
        with mock.patch.object(m, "account_persona", return_value=None):
            self.assertEqual(m.live_session_project_slug("runner"), "-home-runner")

    def test_live_bindings_all_four_consume_seats_are_pantheon_config(self):
        # Regression guard on the ACTUAL fix, against the live binding (manifest or
        # fallback): recovery + specialist + metis + atlas resolve to the -pantheon-config slug;
        # orchestrator (HO) + mnemosyne (second-head) do not. Each was verified against its
        # freshest live jsonl dir (T-2715).
        for acct in ("claude-fallback", "claude-research", "suborch1", "suborch2"):
            self.assertTrue(
                m.live_session_project_slug(acct).endswith("-pantheon-config"), acct)
        for acct in ("claude", "exocortex"):
            self.assertFalse(
                m.live_session_project_slug(acct).endswith("-pantheon-config"), acct)


if __name__ == "__main__":
    unittest.main()
