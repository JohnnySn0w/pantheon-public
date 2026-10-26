"""T-5011 PR-B: hermes registered, with every claude/codex path unchanged.

Covers the plan §3.2 gaps 1-4 wiring (T-4924 H2): the receiver-handoff fix that
makes registration safe, launch-preparation dispatch, the submit-verify map and
hermes profile, the mid-session cap hook, the prepaid cap ceiling, usability
routing and the balance branch, the diagnostics rows, the pane-injection
plumbing, and the installer/monitor generalizations. Byte safety: the uppercase
context-file name never appears; build it by concatenation if ever needed.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import context_fill_monitor
from tools import inbox_consumer
from tools import live_harness
from tools import mailbox_wake
from tools import persona_installer
from tools import runtime_controller as rc
from tools import submit_verify
from tools import subscription_usability as su
from tools import tool_schema_budget
from tools.harness_adapter import diagnostics
from tools.harness_adapter import hermes
from tools.harness_adapter import integration as harness_integration
from tools.harness_adapter import quota
from tools.harness_adapter.quota import BalanceReading, QuotaSnapshot
from tools.harness_adapter.types import (
    FailoverPolicy,
    HarnessLaunchConfig,
    RuntimeControllerConfig,
    TmuxTarget,
)

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc)

# H0 classic-CLI renders (tui/classic2), trimmed to the bottom region.
RULE = "─" * 80
IDLE_PANE = "\n".join(
    [
        "Welcome to Hermes Agent! Type your message or /help for commands.",
        " ☤ deepseek-flash │ ctx -- │ [░░░░░░░░░░] -- │ 2s │ ⏲ 0s",
        RULE,
        "❯ Find and fix a failing test",
        RULE,
    ]
)
BUSY_PANE = "\n".join(
    [
        "● slow message",
        "  ( •_•)>⌐■-■ mulling...",
        " ☤ deepseek-flash │ ~30/1M │ [░░░░░░░░░░] ~0% │ ◷ 0.7s │ ↑ 4 t/s │ 31s │ ⏱ 3s",
        RULE,
        "☤ ❯ msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel",
        RULE,
    ]
)
BALANCE_PANE = "\n".join(["⚡ Out of credits", "Add credits with DeepSeek: https://platform.deepseek.com/top_up", RULE, "❯ ", RULE])


def _launch(harness: str) -> HarnessLaunchConfig:
    return HarnessLaunchConfig(
        harness=harness,
        profile="interactive",
        executable=Path(f"/usr/bin/{harness or 'none'}"),
        model=None,
        mode="interactive",
        meter="subscription",
        quota_source="app-server",
        native={},
        sources=("test",),
    )


def _binding(harness: str, *, communications=None, root: Path = Path("/tmp/t5011-b")) -> SimpleNamespace:
    return SimpleNamespace(
        persona="recovery",
        role="recovery",
        account="nobody",
        account_home=root,
        mailbox=root / "mail",
        launch_cwd=root,
        communications=communications,
        launch=_launch(harness),
        controller=RuntimeControllerConfig(
            tmux=TmuxTarget("pantheon-recovery", "harness", "0"),
            policy=FailoverPolicy(5.0, ()),
        ),
    )


_TELEGRAM = {
    "kind": "telegram",
    "claude_receiver_topology": "runtime-owned",
    "claude_receiver_unit": "claude-telegram-fallback.service",
    "codex_receiver_unit": "codex-telegram-poller.service",
    "tmux_session": "claude-telegram-bot-fallback",
    "tmux_pane": "0.0",
    "watchdog_units": (),
}


class RegistrationAndReceiverTests(unittest.TestCase):
    def test_hermes_is_registered_next_to_claude_and_codex(self) -> None:
        self.assertEqual(harness_integration.configured_harness_names(), frozenset({"claude", "codex", "hermes"}))
        self.assertIs(harness_integration.integration_for("hermes"), harness_integration.integration_for("Hermes"))
        self.assertIs(hermes.HermesIntegration, harness_integration.HermesIntegration)
        self.assertFalse(harness_integration.integration_for("hermes").communications_managed)

    def test_in_runtime_receiver_harness_is_still_claude(self) -> None:
        self.assertEqual(rc._in_runtime_receiver_harness(), "claude")

    def test_codex_comms_gate_still_runs_the_claude_to_codex_handoff(self) -> None:
        """T-4184 must not become a no-op once hermes is registered."""
        gate = rc._selected_out_of_runtime_receiver_gate(_binding("codex", communications=_TELEGRAM))
        self.assertEqual(gate, ("claude", "claude-to-codex"))

    def test_an_unresolvable_extra_harness_stays_ambiguous(self) -> None:
        with mock.patch.object(
            harness_integration, "configured_harness_names", return_value=frozenset({"claude", "codex", "other"})
        ):
            self.assertIsNone(rc._in_runtime_receiver_harness())

    def test_a_second_receiver_carrying_harness_stays_ambiguous(self) -> None:
        fake = SimpleNamespace(name="other", communications_managed=True)
        with harness_integration.registered_integration(fake):  # type: ignore[arg-type]
            self.assertIsNone(rc._in_runtime_receiver_harness())


class PrepareLaunchDispatchTests(unittest.TestCase):
    """PR-A LOW 2: exact-name dispatch only."""

    def test_aliased_cased_or_empty_harness_seeds_nothing(self) -> None:
        for name in ("claude-code", "Codex", "CLAUDE", ""):
            with self.subTest(harness=name), mock.patch(
                "tools.harness_adapter.integration.prepare_claude_launch"
            ) as claude_prep, mock.patch("tools.harness_adapter.integration.prepare_codex_launch") as codex_prep:
                adapter = mock.Mock()
                rc._prepare_harness_launch(_binding(name), adapter, cwd=Path("/tmp"))
                claude_prep.assert_not_called()
                codex_prep.assert_not_called()
                adapter.prepare_launch.assert_not_called()

    def test_hermes_dispatches_to_its_adapter_hook(self) -> None:
        adapter = mock.Mock()
        binding = _binding("hermes")
        with mock.patch("tools.harness_adapter.integration.prepare_codex_launch") as codex_prep, mock.patch(
            "tools.harness_adapter.integration.prepare_claude_launch"
        ) as claude_prep:
            rc._prepare_harness_launch(binding, adapter, cwd=Path("/tmp/seat"))
        adapter.prepare_launch.assert_called_once_with(binding, cwd=Path("/tmp/seat"))
        codex_prep.assert_not_called()
        claude_prep.assert_not_called()

    def test_hermes_launch_fails_closed_without_render_inputs(self) -> None:
        binding = _binding("hermes")
        adapter = hermes.HermesHarnessAdapter("system-orchestrator", account_home=binding.account_home)
        with self.assertRaises(hermes.HermesRenderError):
            rc._prepare_harness_launch(binding, adapter, cwd=binding.account_home / "seat")


class SubmitVerifyMapTests(unittest.TestCase):
    def test_explicit_map(self) -> None:
        self.assertEqual(rc._submit_verify_harness("claude"), "claude-code")
        self.assertEqual(rc._submit_verify_harness("codex"), "codex")
        self.assertEqual(rc._submit_verify_harness("hermes"), "hermes")

    def test_unknown_harness_raises_instead_of_codex(self) -> None:
        for name in ("other", "claude-code", ""):
            with self.subTest(harness=name), self.assertRaises(ValueError):
                rc._submit_verify_harness(name)


class MidSessionCapTests(unittest.TestCase):
    def test_hermes_balance_render_is_a_cap_without_reset(self) -> None:
        hit = rc._mid_session_cap_hit("hermes", BALANCE_PANE, now=NOW)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.cap_kind, "prepaid-balance")
        self.assertIsNone(hit.reset_at)
        self.assertIsNone(hit.reset_source)

    def test_hermes_idle_pane_is_not_a_cap(self) -> None:
        self.assertIsNone(rc._mid_session_cap_hit("hermes", IDLE_PANE, now=NOW))

    def test_unregistered_harness_never_classifies(self) -> None:
        self.assertIsNone(rc._mid_session_cap_hit("other", BALANCE_PANE, now=NOW))

    def test_adapter_and_integration_classifiers_agree(self) -> None:
        adapter = hermes.HermesHarnessAdapter("system-orchestrator", account_home=Path("/tmp/x"))
        self.assertEqual(
            adapter.classify_mid_session_cap(BALANCE_PANE, now=NOW),
            harness_integration.integration_for("hermes").classify_mid_session_cap(BALANCE_PANE, now=NOW),
        )


class CapResetCeilingTests(unittest.TestCase):
    def test_claude_and_codex_equal_next_weekly_reset(self) -> None:
        for harness in ("claude", "codex"):
            for hours in range(0, 24 * 8, 7):
                now = NOW + timedelta(hours=hours)
                with self.subTest(harness=harness, now=now):
                    self.assertEqual(
                        quota.cap_reset_ceiling(harness, now=now), quota.next_weekly_reset(harness, now=now)
                    )

    def test_hermes_is_a_fifteen_minute_reprobe(self) -> None:
        self.assertEqual(quota.cap_reset_ceiling("hermes", now=NOW), NOW + timedelta(minutes=15))
        self.assertEqual(
            quota.PREPAID_REPROBE_CEILING_SECONDS["hermes"], su.REPROBE_CEILING_SECONDS["hermes"]
        )

    def test_hermes_subscription_cap_cooldown_does_not_raise(self) -> None:
        until = rc._cooldown_until_for_failure("subscription_cap", now=NOW, consecutive_failures=1, harness="hermes")
        self.assertEqual(until, NOW + timedelta(minutes=15))

    def test_unknown_harness_still_raises(self) -> None:
        with self.assertRaises(KeyError):
            quota.cap_reset_ceiling("other", now=NOW)


def _balance_snapshot(band: str, *, freshness: str = "fresh") -> QuotaSnapshot:
    return QuotaSnapshot(
        profile="hermes/deepseek-direct",
        windows=(),
        observed_at=NOW,
        freshness=freshness,  # type: ignore[arg-type]
        evidence={"provider": "deepseek", "source": "provider-api", "balance_band": band},
        balance=BalanceReading(True, "USD", Decimal("5"), Decimal("0"), Decimal("5"), NOW),
    )


class UsabilityTests(unittest.TestCase):
    def test_balance_branch_maps_each_band(self) -> None:
        expected = {"ok": "usable", "low": "near-cap", "exhausted": "capped", "unknown": "unknown", "bogus": "unknown"}
        for band, verdict in expected.items():
            with self.subTest(band=band):
                got, evidence, hint = su.classify_snapshot(_balance_snapshot(band), floor_percent=5.0)
                self.assertEqual(got, verdict)
                self.assertIsNone(hint)

    def test_unavailable_balance_is_unknown(self) -> None:
        got, _, _ = su.classify_snapshot(_balance_snapshot("ok", freshness="unavailable"), floor_percent=5.0)
        self.assertEqual(got, "unknown")

    def test_hermes_never_takes_the_claude_path(self) -> None:
        def forbidden(*args, **kwargs):
            raise AssertionError("claude tier reached")

        deps = su.ProbeDeps(
            codex_snapshot=forbidden,
            claude_statusline=forbidden,
            claude_pane_cap=forbidden,
            claude_pong=forbidden,
            observer_snapshot=lambda binding: _balance_snapshot("low"),
        )
        verdict, source, evidence, hint = su._evaluate(
            _binding("hermes"), state_root=None, now=NOW, allow_spend=True, deps=deps
        )
        self.assertEqual((verdict, source), ("near-cap", "hermes-provider-api"))

    def test_reprobe_ceilings(self) -> None:
        self.assertEqual(dict(su.REPROBE_CEILING_SECONDS), {"codex": 1800.0, "claude": 3600.0, "hermes": 900.0})


class DiagnosticsTests(unittest.TestCase):
    def test_native_keys_match_the_adapter(self) -> None:
        self.assertEqual(diagnostics._NATIVE_KEYS["hermes"], set(hermes.HERMES_NATIVE_KEYS))

    def test_meter_pair(self) -> None:
        self.assertIn(("hermes", "api", "provider-api"), diagnostics._METER_QUOTA_PAIRS)

    def test_legacy_launch_refuses_hermes(self) -> None:
        for override in (None, Path("/opt/hermes/bin/hermes")):
            with self.subTest(override=override), self.assertRaises(diagnostics.RuntimeProfileError):
                diagnostics._legacy_launch_config(
                    "hermes", account_home=Path("/home/x"), executable_override=override, model_override=None
                )


class SubmitVerifyProfileTests(unittest.TestCase):
    def test_profile_shape(self) -> None:
        profile = submit_verify.PROFILES["hermes"]
        self.assertEqual(profile["settle_delay_s"], 1.2)
        self.assertEqual(profile["paste_sentinel"], "[Pasted text")
        self.assertEqual(profile["composer_glyph"], "❯")

    def test_short_injection_pending_then_submitted(self) -> None:
        profile = submit_verify.PROFILES["hermes"]
        text = "Find and fix a failing test"
        self.assertTrue(submit_verify.submit_pending(profile, IDLE_PANE, text))
        submitted = IDLE_PANE.replace("❯ Find and fix a failing test", "❯ ")
        self.assertFalse(submit_verify.submit_pending(profile, submitted, text))

    def test_collapsed_paste_is_pending(self) -> None:
        profile = submit_verify.PROFILES["hermes"]
        pane = "\n".join([RULE, "❯ [Pasted text #1: 7 lines → /home/x/.hermes/pastes/paste_1_210000.txt]", RULE])
        self.assertTrue(submit_verify.submit_pending(profile, pane, "line one\nline two\n" * 4))

    def test_idle_predicate(self) -> None:
        self.assertIs(submit_verify.hermes_pane_idle_state(IDLE_PANE), True)
        self.assertIs(submit_verify.hermes_pane_idle_state(BUSY_PANE), False)
        self.assertIsNone(submit_verify.hermes_pane_idle_state(""))
        self.assertIsNone(submit_verify.hermes_pane_idle_state("some modal\nwith no input line"))

    def test_marker_copies_match_the_adapter(self) -> None:
        self.assertEqual(submit_verify.HERMES_PANE_IDLE_PROMPT, hermes.PANE_IDLE_PROMPT)
        self.assertEqual(submit_verify.HERMES_PANE_BUSY_HINT, hermes.PANE_BUSY_HINT)
        self.assertEqual(submit_verify.HERMES_PANE_BUSY_STATUS_GLYPH, hermes.PANE_BUSY_STATUS_GLYPH)

    def test_cli_accepts_hermes(self) -> None:
        out = subprocess.run(
            [sys.executable, str(REPO / "tools" / "submit_verify.py"), "--harness", "hermes", "--pending-check", "--text", "x"],
            input="", capture_output=True, text=True, check=False,
        )
        self.assertEqual(out.returncode, 0, out.stderr)


def _bash_delay(harness: str, env: dict[str, str] | None = None) -> str:
    script = f'source "{REPO}/tools/send_keys_submit.sh" >/dev/null 2>&1; _sks_effective_enter_delay {harness}'
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
    )
    return out.stdout.strip()


class EnterDelayTests(unittest.TestCase):
    def test_claude_and_codex_keep_inject_enter_delay(self) -> None:
        self.assertEqual(_bash_delay("claude-code"), "0.7")
        self.assertEqual(_bash_delay("codex"), "0.7")
        self.assertEqual(_bash_delay("codex", {"INJECT_ENTER_DELAY": "0.3"}), "0.3")

    def test_hermes_waits_longer_unless_the_caller_set_a_delay(self) -> None:
        self.assertEqual(float(_bash_delay("hermes")), submit_verify.HERMES_SETTLE_DELAY_S)
        self.assertEqual(_bash_delay("hermes", {"INJECT_ENTER_DELAY": "0.3"}), "0.3")

    def test_inbox_consumer_delays(self) -> None:
        self.assertEqual(inbox_consumer.HERMES_INJECT_ENTER_DELAY, submit_verify.HERMES_SETTLE_DELAY_S)
        self.assertEqual(inbox_consumer._inject_enter_delay("claude-code"), inbox_consumer.INJECT_ENTER_DELAY)
        self.assertEqual(inbox_consumer._inject_enter_delay("codex"), inbox_consumer.INJECT_ENTER_DELAY)
        with mock.patch.object(inbox_consumer.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            inbox_consumer.inject("pane:0", "hello", "hermes")
        self.assertEqual(run.call_args.kwargs["env"]["INJECT_ENTER_DELAY"], "1.2")


class PaneInjectionPlumbingTests(unittest.TestCase):
    def test_live_harness_maps_hermes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "qianliyan" / "runtime-controller" / "runtime.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"schema": "pantheon-runtime-state/v1", "harness": "hermes"}))
            self.assertEqual(live_harness.resolve("qianliyan", "claude-code", state_root=Path(tmp)), "hermes")

    def test_mailbox_wake_hermes_needs_an_idle_pane(self) -> None:
        for pane, expected in ((IDLE_PANE, True), (BUSY_PANE, False), ("", False)):
            with self.subTest(expected=expected), mock.patch.object(
                mailbox_wake.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=pane)
            ):
                self.assertIs(mailbox_wake.unknown_pane_deliverable("q-main:0", "hermes"), expected)

    def test_mailbox_wake_claude_unknown_is_still_refused(self) -> None:
        with mock.patch.object(mailbox_wake.subprocess, "run") as run:
            self.assertFalse(mailbox_wake.unknown_pane_deliverable("x:0", "claude-code"))
        run.assert_not_called()


class LivePaneBasenameTests(unittest.TestCase):
    """The T-4158 live_pane source with hermes registered: a hermes pane names
    hermes (inert, its consumers act only on codex); codex still names codex."""

    def _observed(self, argv):
        with tempfile.TemporaryDirectory() as tmp:
            return rc._prior_runtime_harnesses(_binding("claude"), live_argv=argv, state_root=Path(tmp))

    def test_proc_cmdline_form(self) -> None:
        argv = ["/opt/pantheon/hermes-agent/v/.venv/bin/python", "/opt/pantheon/hermes-agent/v/.venv/bin/hermes",
                "--provider", "deepseek", "-m", "deepseek-flash"]
        self.assertEqual(self._observed(argv), {"live_pane": "hermes"})

    def test_env_launch_form(self) -> None:
        argv = ["/usr/bin/env", "-u", "HERMES_MANAGED_DIR", "--", "/opt/x/bin/hermes", "--provider", "deepseek"]
        self.assertEqual(self._observed(argv), {"live_pane": "hermes"})

    def test_codex_and_claude_unchanged(self) -> None:
        self.assertEqual(self._observed(["/usr/bin/codex", "--model", "m"]), {"live_pane": "codex"})
        self.assertEqual(self._observed(["/home/x/.npm-global/bin/claude", "--model", "m"]), {"live_pane": "claude"})


class InstallerAndMonitorTests(unittest.TestCase):
    def test_installer_normalizes_hermes(self) -> None:
        self.assertEqual(persona_installer._normalize_harness("Hermes"), "hermes")
        with self.assertRaises(persona_installer.ManifestError):
            persona_installer._normalize_harness("other")

    def test_harness_check_desired(self) -> None:
        manifest = SimpleNamespace(harness_runtime={
            "harnesses": {"hermes": {"default_profile": "deepseek-direct", "profiles": {
                "deepseek-direct": {"executable": "{account_home}/.venv/bin/hermes"}}}}})
        home = Path("/home/deepseek-fallback")
        for harness in ("claude", "codex"):
            self.assertEqual(persona_installer._harness_check_desired(manifest, harness, home), {"harness": harness})
        self.assertEqual(
            persona_installer._harness_check_desired(manifest, "hermes", home),
            {"harness": "hermes", "executable": "/home/deepseek-fallback/.venv/bin/hermes"},
        )
        self.assertEqual(
            persona_installer._harness_check_desired(SimpleNamespace(harness_runtime={}), "hermes", home),
            {"harness": "hermes", "executable": ""},
        )

    def test_context_fill_gate_treats_hermes_as_non_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instance.yaml"
            for block, expected in (({"claude": {}}, False), ({"claude": {}, "hermes": {}}, True),
                                    ({"claude": {}, "codex": {}}, True), ({"fallbacks": ["hermes/x"]}, True)):
                with self.subTest(block=block):
                    path.write_text(json.dumps({"harness_runtime": {"roles": {"engineering-orchestrator-2": block}}}))
                    self.assertIs(context_fill_monitor._manifest_runtime_references_codex("artisan", path), expected)

    def test_tool_schema_budget_says_hermes_is_unmeasured(self) -> None:
        manifest = {"roles": {"r": {"persona": "q", "account": "a"}},
                    "harness_runtime": {"roles": {"r": {"hermes": {"model": "deepseek-flash"}}}}}
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(list(tool_schema_budget.iter_profile_specs(manifest)), [])
        self.assertIn("r.hermes not measured", err.getvalue())


if __name__ == "__main__":
    unittest.main()
