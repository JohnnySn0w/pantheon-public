"""Tests for agent_role_map's live-session project-slug helper (T-2687/T-4159) and
restart-target resolver (T-4159).

The slug helper is the single source of truth for the `-pantheon-config` project-dir
slug, consumed by context_fill_monitor / keep_going / doc_traversal_report /
activity_monitor / tui_wedge_sampler. T-4159 keys it on the LAUNCH SHAPE
(`supervisor: control-plane`, or a bespoke-relaunch pantheon-config-cwd seat), not on
role membership.
"""
import contextlib
import io
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import agent_role_map as m  # noqa: E402


def _seat(persona, role, supervisor=None):
    """Patch one seat's identity: persona, instance role, and binding supervisor."""
    binding = {"account": "x", "restart_service": "bespoke.service",
               "restart_mode": "user", "restart_pane": "sess:0"}
    if supervisor:
        binding["supervisor"] = supervisor
    return [
        mock.patch.object(m, "account_persona", return_value=persona),
        mock.patch.object(m, "resolve_instance_role", return_value=role),
        mock.patch.object(m, "instance_binding", return_value=binding),
    ]


def _with(patches):
    stack = contextlib.ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack


class LiveSessionSlugTest(unittest.TestCase):
    def test_control_plane_seat_gets_pantheon_config_slug(self):
        # A control-plane seat launches cwd=<account>/pantheon-config (runtime scope
        # WorkingDirectory), so its live jsonl dir is `-home-<account>-pantheon-config`.
        patches = _seat("specialist", "specialist", supervisor="control-plane")
        with _with(patches):
            self.assertEqual(m.live_session_project_slug("claude-research"),
                             "-home-claude-research-pantheon-config")
            self.assertEqual(
                m.live_session_projects_dir("claude-research"),
                "/home/claude-research/.claude/projects/-home-claude-research-pantheon-config")

    def test_bespoke_recovery_gets_plain_slug(self):
        # T-4159: recovery is bespoke (home cwd) until T-3026's commit B. T-2715 keyed on
        # role membership and pointed recovery at -pantheon-config, which holds no jsonl
        # since T-3082 reverted recovery to bespoke (2026-08-23).
        patches = _seat("recovery", "head-system-orchestrator")
        with _with(patches):
            self.assertEqual(m.live_session_project_slug("claude-fallback"), "-home-claude-fallback")

    def test_recovery_and_orchestrator_flip_with_the_cutover_marker(self):
        # T-4159: the T-3650 hand-move's commit B adds `supervisor: control-plane`;
        # the slug must follow it with no second edit.
        for persona, role, acct in (("recovery", "head-system-orchestrator", "claude-fallback"),
                                    ("orchestrator", "head-engineering-orchestrator", "claude")):
            patches = _seat(persona, role)
            with _with(patches):
                self.assertEqual(m.live_session_project_slug(acct), f"-home-{acct}", persona)
            patches = _seat(persona, role, supervisor="control-plane")
            with _with(patches):
                self.assertEqual(m.live_session_project_slug(acct),
                                 f"-home-{acct}-pantheon-config", persona)

    def test_engineering_orchestrator_seats_get_pantheon_config_slug(self):
        # T-2715: mason/artisan launch cwd=pantheon-config (bespoke relaunch, captured
        # cwd) with supervisor=None; they stay on the -pantheon-config slug.
        for persona, role, acct in (("mason", "engineering-orchestrator-1", "suborch1"),
                                    ("artisan", "engineering-orchestrator-2", "suborch2")):
            patches = _seat(persona, role)
            with _with(patches):
                self.assertEqual(m.live_session_project_slug(acct),
                                 f"-home-{acct}-pantheon-config", persona)

    def test_head_memory_orchestrator_is_plain(self):
        # archivist (head-memory-orchestrator) is mailbox-first at the operator's pane.
        patches = _seat("archivist", "head-memory-orchestrator")
        with _with(patches):
            self.assertEqual(m.live_session_project_slug("exocortex"), "-home-exocortex")

    def test_unknown_account_fails_open_to_plain(self):
        # An account with no persona binding must not raise and must not fabricate a
        # -pantheon-config slug; plain is the safe default.
        with mock.patch.object(m, "account_persona", return_value=None):
            self.assertEqual(m.live_session_project_slug("runner"), "-home-runner")

    def test_live_bindings_follow_the_launch_shape_rule(self):
        # Regression guard against the LIVE binding (manifest or fallback), stated as the
        # rule rather than a per-seat snapshot so it survives each seat's cutover commit:
        # -pantheon-config iff supervisor == control-plane or a bespoke-relaunch
        # engineering-orchestrator seat.
        for acct in ("claude-fallback", "claude", "claude-research", "suborch1", "suborch2", "exocortex"):
            persona = m.account_persona(acct)
            want = (m.instance_binding(persona).get("supervisor") == "control-plane"
                    or m.resolve_instance_role(persona) in m._BESPOKE_PANTHEON_CONFIG_CWD_ROLE_KEYS)
            self.assertEqual(m.live_session_project_slug(acct).endswith("-pantheon-config"), want, acct)


class RestartTargetTest(unittest.TestCase):
    """T-4159: one derivation of a seat's restart identity for every consumer."""

    BESPOKE = {"account": "claude-fallback", "restart_service": "claude-telegram-fallback.service",
               "restart_mode": "user", "restart_pane": "claude-telegram-bot-fallback:0"}

    def test_bespoke_seat_uses_its_bespoke_unit(self):
        with mock.patch.object(m, "instance_binding", return_value=dict(self.BESPOKE)):
            self.assertEqual(m.restart_target("recovery"), {
                "supervisor": "bespoke", "service": "claude-telegram-fallback.service",
                "mode": "user", "pane": "claude-telegram-bot-fallback:0",
                "session_unit": "claude-telegram-fallback.service"})

    def test_control_plane_seat_never_names_the_bespoke_unit(self):
        with mock.patch.object(m, "instance_binding",
                               return_value={**self.BESPOKE, "supervisor": "control-plane"}):
            t = m.restart_target("recovery")
        self.assertEqual(t["supervisor"], "control-plane")
        self.assertEqual(t["service"], "pantheon-persona-consume@recovery.service")
        self.assertEqual(t["session_unit"], "pantheon-runtime-controller@recovery.service")
        self.assertEqual(t["pane"], "claude-telegram-bot-fallback:0")
        self.assertNotIn("claude-telegram-fallback.service", t.values())

    def test_unbound_persona_raises_rather_than_guessing(self):
        with mock.patch.object(m, "instance_binding", return_value={"account": "suborch1"}):
            with self.assertRaises(KeyError):
                m.restart_target("mason")

    def test_live_artisan_is_control_plane(self):
        # T-5299: artisan completed its cutover; restart-target must resolve, not raise.
        t = m.restart_target("artisan")
        self.assertEqual(t["supervisor"], "control-plane")
        self.assertEqual(t["service"], "pantheon-persona-consume@artisan.service")
        self.assertEqual(t["session_unit"], "pantheon-runtime-controller@artisan.service")
        self.assertEqual(t["pane"], "suborch2-main:0")
        self.assertEqual(t["mode"], "user")

    def test_live_specialist_is_control_plane(self):
        # specialist is the one live control-plane seat today (T-2658).
        t = m.restart_target("specialist")
        self.assertEqual(t["session_unit"], "pantheon-runtime-controller@specialist.service")

    def test_cli_prints_shell_lines(self):
        buf = io.StringIO()
        with mock.patch.object(m, "instance_binding", return_value=dict(self.BESPOKE)), \
             contextlib.redirect_stdout(buf):
            rc = m._main(["restart-target", "recovery"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().splitlines(), [
            "SUPERVISOR=bespoke", "SERVICE=claude-telegram-fallback.service", "MODE=user",
            "PANE=claude-telegram-bot-fallback:0", "SESSION_UNIT=claude-telegram-fallback.service"])

    def test_cli_refuses_unsafe_values_and_unknown_personas(self):
        bad = {**self.BESPOKE, "restart_service": "x.service; rm -rf ~"}
        for binding, persona in ((bad, "recovery"), ({"account": "suborch1"}, "mason")):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(m, "instance_binding", return_value=binding), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = m._main(["restart-target", persona])
            self.assertEqual(rc, 2, persona)
        self.assertEqual(m._main(["bogus"]), 64)


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


class ConsultantAliasTests(unittest.TestCase):
    """T-3478: consultant (the codex peer-daemon's self-name) must be a VALID ticket
    target and must normalize to the codex account across all three resolve
    axes. Before the fix, consultant passed through unchanged everywhere and was not
    in the accepted-target set, so ticket_lint's retarget-to-originator bounce of
    a consultant-requested ticket raised `unknown target 'consultant'` and the hourly
    pantheon-ticket-lint unit exited 1."""

    def test_resolve_persona_collapses_consultant_to_codex(self):
        # The load-bearing one: ticket target/requester normalization. Pre-fix
        # this returned "consultant" (identity passthrough).
        self.assertEqual(m.resolve_persona("consultant"), "codex")

    def test_resolve_role_collapses_consultant_to_codex(self):
        # Ledger agent-id axis: an alias must not survive into a ledger write.
        # consultant collapses to codex, whose ORG-100 (T-3814) forward audit-id is head_auditor.
        self.assertEqual(m.resolve_role("consultant"), "head_auditor")

    def test_resolve_instance_role_collapses_consultant_to_codex_grants(self):
        # repos.yaml grants axis: consultant resolves to codex's instance role-key.
        self.assertEqual(m.resolve_instance_role("consultant"), "peer-daemon-codex")

    def test_consultant_is_an_accepted_target(self):
        self.assertIn("consultant", m.valid_target_values())

    def test_codex_and_other_names_unaffected(self):
        # Regression guard: the alias must not disturb existing resolution.
        self.assertEqual(m.resolve_persona("codex"), "codex")
        # orchestrator is orchestrator's OLD (pre-ORG-100) audit-id, kept as a legacy
        # PERSONA_ALIASES entry (T-3814): resolve_persona still maps it to orchestrator so
        # historical [orchestrator] ledger/tickets stay resolvable.
        self.assertEqual(m.resolve_persona("orchestrator"), "orchestrator")
        self.assertEqual(m.resolve_persona("recovery"), "recovery")
        # ORG-100 (T-3814) forward audit-ids.
        self.assertEqual(m.resolve_role("orchestrator"), "head_engineering_orchestrator")
        self.assertEqual(m.resolve_persona("head_engineering_orchestrator"), "orchestrator")
        self.assertEqual(m.resolve_role("codex"), "head_auditor")
        self.assertEqual(m.resolve_persona("head_auditor"), "codex")
        self.assertEqual(m.resolve_role("recovery"), "head_system_orchestrator")
        self.assertEqual(m.resolve_persona("head_system_orchestrator"), "recovery")
        self.assertEqual(m.resolve_role("archivist"), "head_memory_orchestrator")
        self.assertEqual(m.resolve_persona("head_memory_orchestrator"), "archivist")
        self.assertEqual(m.resolve_role("mason"), "engineering_orchestrator")
        self.assertEqual(m.resolve_role("artisan"), "engineering_orchestrator")
        # B-LEGACY: the five old audit-ids ARE the persona names, so they
        # reverse-resolve via persona-name identity passthrough (no alias needed).
        for name in ("recovery", "codex", "archivist", "mason", "artisan"):
            self.assertEqual(m.resolve_persona(name), name)
        for name in ("orchestrator", "specialist", "recovery", "codex", "operator"):
            self.assertIn(name, m.valid_target_values())


class NonInjectiveRoleIdTests(unittest.TestCase):
    """T-4044: a role-class audit-id held by MORE THAN ONE persona
    (engineering_orchestrator -> mason + artisan, ORG-100/T-3814) must NOT be
    reverse-mapped to an arbitrary seat. Before the fix, `_ROLE_PERSONA_MAP =
    {v: k ...}` kept whichever key came last (artisan), so
    resolve_persona('engineering_orchestrator') silently returned 'artisan' and a
    bare `--target engineering_orchestrator` misrouted."""

    def test_engineering_orchestrator_is_flagged_non_injective(self):
        import tools.agent_role_map as m
        self.assertIn("engineering_orchestrator", m.NON_INJECTIVE_ROLE_IDS)
        self.assertEqual(
            m.personas_for_role_id("engineering_orchestrator"), ["artisan", "mason"]
        )

    def test_non_injective_role_id_resolves_to_itself_not_a_seat(self):
        # FAILS pre-T-4044: returned 'artisan' (last reverse-map key).
        import tools.agent_role_map as m
        self.assertEqual(
            m.resolve_persona("engineering_orchestrator"), "engineering_orchestrator"
        )

    def test_injective_role_ids_still_reverse_resolve(self):
        # The fix must not drop the injective reverse mappings.
        import tools.agent_role_map as m
        self.assertEqual(m.resolve_persona("head_engineering_orchestrator"), "orchestrator")
        self.assertEqual(m.resolve_persona("head_research_orchestrator"), "specialist")
        self.assertEqual(m.resolve_persona("head_system_orchestrator"), "recovery")
        self.assertEqual(m.resolve_persona("head_auditor"), "codex")
        self.assertEqual(m.resolve_persona("head_memory_orchestrator"), "archivist")

    def test_reverse_map_has_no_non_injective_entries(self):
        import tools.agent_role_map as m
        for rid in m.NON_INJECTIVE_ROLE_IDS:
            self.assertNotIn(rid, m._ROLE_PERSONA_MAP)


class LedgerIdentityTest(unittest.TestCase):
    """T-4737: ORG-001 ledger agent/executor (the operator TG 11474, orchestrator B3)."""

    def test_identity_is_role_id_and_seat_slot(self):
        self.assertEqual(
            m.ledger_identity("artisan"), ("engineering_orchestrator", "engineering-orchestrator-2")
        )
        self.assertEqual(
            m.ledger_identity("mason"), ("engineering_orchestrator", "engineering-orchestrator-1")
        )
        self.assertEqual(
            m.ledger_identity("orchestrator"),
            ("head_engineering_orchestrator", "head-engineering-orchestrator"),
        )
        self.assertEqual(m.ledger_identity("specialist"), ("head_research_orchestrator", "specialist"))

    def test_legacy_history_entries_still_match(self):
        self.assertTrue(m.ledger_entry_by({"agent": "orchestrator"}, "orchestrator"))
        self.assertTrue(m.ledger_entry_by({"agent": "artisan"}, "artisan"))
        self.assertTrue(m.ledger_entry_by({"agent": "recovery"}, "recovery"))
        self.assertFalse(m.ledger_entry_by({"agent": "mason"}, "artisan"))

    def test_new_injective_role_matches_without_executor(self):
        e = {"agent": "head_engineering_orchestrator"}
        self.assertTrue(m.ledger_entry_by(e, "orchestrator"))
        self.assertTrue(m.ledger_entry_by(e, "orchestrator"))
        self.assertFalse(m.ledger_entry_by(e, "recovery"))

    def test_shared_role_needs_the_seat_slot(self):
        mine = {"agent": "engineering_orchestrator", "executor": "engineering-orchestrator-2"}
        theirs = {"agent": "engineering_orchestrator", "executor": "engineering-orchestrator-1"}
        bare = {"agent": "engineering_orchestrator"}
        with_session = {"agent": "engineering_orchestrator", "executor": "engineering-orchestrator-2 sess-1"}
        self.assertTrue(m.ledger_entry_by(mine, "artisan"))
        self.assertFalse(m.ledger_entry_by(theirs, "artisan"))
        self.assertTrue(m.ledger_entry_by(theirs, "mason"))
        self.assertFalse(m.ledger_entry_by(bare, "artisan"))
        self.assertFalse(m.ledger_entry_by(bare, "mason"))
        self.assertTrue(m.ledger_entry_by(with_session, "artisan"))
        # review F5: a slot that merely starts with the seat's slot is not it.
        longer = {"agent": "engineering_orchestrator", "executor": "engineering-orchestrator-22"}
        self.assertFalse(m.ledger_entry_by(longer, "artisan"))
        prefix_of_mason = {"agent": "engineering_orchestrator", "executor": "engineering-orchestrator-10"}
        self.assertFalse(m.ledger_entry_by(prefix_of_mason, "mason"))

    def test_no_agent_matches_no_one(self):
        self.assertFalse(m.ledger_entry_by({}, "artisan"))
        self.assertFalse(m.ledger_entry_by({"agent": ""}, "artisan"))


if __name__ == "__main__":
    unittest.main()
