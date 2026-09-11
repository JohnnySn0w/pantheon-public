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


class ExpectedPolicySkipKeyTests(unittest.TestCase):
    """T-3210: expected_policy_skip is a coarse reason-match TOKEN (one of
    persona_bringup.py's four policy-skip reasons), not a free-text label --
    validated at load (regex), not just isinstance(str), because it lives in
    the same binding dict tools/persona_cutover_resolve.py's BespokeIdentity.
    as_env() prints as an unquoted `KEY=value` line for a bash `eval` (see
    _validated_expected_policy_skip's docstring in agent_role_map.py)."""

    def _write_manifest(self, root, extra_line: str = "") -> "pathlib.Path":
        import textwrap

        manifest = root / "instance.yaml"
        manifest.write_text(
            textwrap.dedent(
                f"""\
                roles:
                  specialist:
                    persona: specialist
                    account: claude-research
                    {extra_line}
                """
            ),
            encoding="utf-8",
        )
        return manifest

    def test_valid_token_round_trips_through_instance_binding(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._write_manifest(
                pathlib.Path(tmp), "expected_policy_skip: runtime_viability_blocked"
            )
            bindings = m._load_instance_bindings(manifest)
            self.assertEqual(
                bindings["specialist"]["expected_policy_skip"], "runtime_viability_blocked"
            )

    def test_free_text_label_is_rejected_not_passed_through(self):
        # T-3210 Q1 disposition: a free-text label (parens/spaces) must NOT
        # reach the loaded binding -- it would word-split under the unquoted
        # bash eval BespokeIdentity.as_env() performs.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._write_manifest(
                pathlib.Path(tmp), 'expected_policy_skip: "not-cut-over (T-3210)"'
            )
            bindings = m._load_instance_bindings(manifest)
            self.assertNotIn("expected_policy_skip", bindings["specialist"])

    def test_absent_by_default(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            manifest = self._write_manifest(pathlib.Path(tmp))
            bindings = m._load_instance_bindings(manifest)
            self.assertNotIn("expected_policy_skip", bindings["specialist"])

    def test_token_charset_is_eval_safe_in_a_real_subshell(self):
        # The as_env-safety assertion Q1 asks for: prove the APPROVED charset
        # (not the full BespokeIdentity/persona_cutover_resolve.py pipeline,
        # which does not read this key today -- only bindings future
        # consumers might inherit) survives an unquoted `KEY=value` eval
        # unchanged, the exact shape tools/persona_cutover.sh/persona_
        # rollback.sh use (see AsEnvBashSafetyTest in
        # tools/test_persona_cutover_resolve.py).
        import subprocess

        value = "runtime_viability_blocked"
        self.assertRegex(value, m._EXPECTED_POLICY_SKIP_RE)
        result = subprocess.run(
            ["bash", "-c", f"PCX_EXPECTED_POLICY_SKIP={value}\necho \"$PCX_EXPECTED_POLICY_SKIP\""],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), value)


class CairnAliasTests(unittest.TestCase):
    """T-3478: cairn (the codex peer-daemon's self-name) must be a VALID ticket
    target and must normalize to the codex account across all three resolve
    axes. Before the fix, cairn passed through unchanged everywhere and was not
    in the accepted-target set, so ticket_lint's retarget-to-originator bounce of
    a cairn-requested ticket raised `unknown target 'cairn'` and the hourly
    pantheon-ticket-lint unit exited 1."""

    def test_resolve_persona_collapses_cairn_to_codex(self):
        # The load-bearing one: ticket target/requester normalization. Pre-fix
        # this returned "cairn" (identity passthrough).
        self.assertEqual(m.resolve_persona("cairn"), "codex")

    def test_resolve_role_collapses_cairn_to_codex(self):
        # Ledger agent-id axis: an alias must not survive into a ledger write.
        self.assertEqual(m.resolve_role("cairn"), "codex")

    def test_resolve_instance_role_collapses_cairn_to_codex_grants(self):
        # repos.yaml grants axis: cairn resolves to codex's instance role-key.
        self.assertEqual(m.resolve_instance_role("cairn"), "peer-daemon-codex")

    def test_cairn_is_an_accepted_target(self):
        self.assertIn("cairn", m.valid_target_values())

    def test_codex_and_other_names_unaffected(self):
        # Regression guard: the alias must not disturb existing resolution.
        self.assertEqual(m.resolve_persona("codex"), "codex")
        self.assertEqual(m.resolve_persona("orchestrator"), "orchestrator")
        self.assertEqual(m.resolve_persona("recovery"), "recovery")
        self.assertEqual(m.resolve_role("codex"), "codex")
        for name in ("orchestrator", "specialist", "recovery", "codex", "operator"):
            self.assertIn(name, m.valid_target_values())


if __name__ == "__main__":
    unittest.main()
