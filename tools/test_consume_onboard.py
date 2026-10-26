"""Tests for tools/consume_onboard.py (T-2679).

Generalizes sop/standard/onboarding-a-mailbox-first-seat-onto-the-control-
plane.md's step-3 hand-recipe into a persona-parameterized tool. Uses the
same fixture-manifest-in-a-tmpdir shape as test_t2611_recovery_persona_apply.py:
a real `tools/systemd/pantheon-persona-consume@.{service,timer}` template
pair (static, already reviewed) read from the REAL repo checkout, but every
filesystem mutation lands under a per-test tempdir `home` -- nothing here
ever touches this host's real /home, systemd --user, or tmux. The
systemctl/tmux/pgrep seam (CommandRunner) and the mailbox group-membership
seam (GroupMembership) are always fake-injected; no test depends on a real
account, group, or live process.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from tools.consume_onboard import (
    CommandResult,
    FakeCommandRunner,
    FakeGroupMembership,
    OnboardError,
    load_seat_context,
    onboard,
    rollback,
    verify,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PERSONA = "testpersona"
ACCOUNT = "testacct"
ROLE = "sub-orchestrator-9"


def _manifest_dict(mailbox_hub: str, required_units: tuple[str, ...] = ()) -> dict:
    mailbox = {
        "hub": mailbox_hub,
        "group": "pantheon-mailbox",
        "probe_peer": "orchestrator",
    }
    if required_units:
        mailbox["required_units"] = list(required_units)
    return {
        "manifest_version": 2,
        "persona_installer": {
            "schema": "pantheon-persona-install/v1",
            "framework": {"source": "pantheon-config", "revision": "test-rev"},
            "paths": {
                "repo": "pantheon-config",
                "unit_dir": ".config/systemd/user",
                "state_dir": ".local/state/pantheon/persona-installer",
            },
            "mailbox": mailbox,
            "systemd": {
                "service_template": "tools/systemd/pantheon-persona-consume@.service",
                "timer_template": "tools/systemd/pantheon-persona-consume@.timer",
            },
            "policy": {"bundle": ["rulebook/", "sop/", "architecture/adr/"]},
            "security": {
                "credential_mode": "references-only",
                "prohibit_auth_copy": True,
            },
        },
        "instance": {"id": "test-instance"},
        "roles": {
            ROLE: {
                "persona": PERSONA,
                "account": ACCOUNT,
                "harness": "claude-code",
            },
        },
        "harness_runtime": {
            "schema": "pantheon-harness-runtime/v1",
            "roles": {
                ROLE: {
                    "tmux": {"session": f"{PERSONA}-main"},
                    "claude": {"model": "claude-opus-4-8"},
                },
            },
        },
    }


class ConsumeOnboardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.home = self.tmp_path / "home"
        self.home.mkdir()
        self.mailbox_hub = self.tmp_path / "mailbox"
        self.manifest_path = self.tmp_path / "instance.yaml"

    def _write_manifest(self, required_units: tuple[str, ...] = ()) -> None:
        doc = _manifest_dict(str(self.mailbox_hub), required_units=required_units)
        self.manifest_path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    def _make_mailbox_hub(self) -> None:
        for sub in ("new", "cur", "tmp"):
            (self.mailbox_hub / PERSONA / sub).mkdir(parents=True)

    def _load_ctx(self, required_units: tuple[str, ...] = ()):
        self._write_manifest(required_units=required_units)
        return load_seat_context(self.manifest_path, PERSONA, REPO_ROOT, self.home)


class LoadSeatContextTests(ConsumeOnboardTestCase):
    def test_persona_not_bound_raises_clean_error(self) -> None:
        self._write_manifest()
        with self.assertRaises(OnboardError):
            load_seat_context(self.manifest_path, "nonexistent-persona", REPO_ROOT, self.home)

    def test_resolves_tmux_session_and_model_from_harness_runtime(self) -> None:
        ctx = self._load_ctx()
        self.assertEqual(ctx.tmux_session, f"{PERSONA}-main")
        self.assertEqual(ctx.model, "claude-opus-4-8")
        self.assertEqual(ctx.consume_service, f"pantheon-persona-consume@{PERSONA}.service")
        self.assertEqual(ctx.consume_timer, f"pantheon-persona-consume@{PERSONA}.timer")


class OnboardDryRunTests(ConsumeOnboardTestCase):
    def test_dry_run_makes_no_mutations_and_no_runner_calls(self) -> None:
        self._make_mailbox_hub()
        ctx = self._load_ctx()
        runner = FakeCommandRunner()
        report = onboard(ctx, runner, FakeGroupMembership(True, "ok"), execute=False)
        self.assertTrue(report.dry_run)
        self.assertEqual(runner.calls, [])
        self.assertFalse(ctx.unit_dir.exists())
        self.assertTrue(report.planned)


class OnboardExecuteTests(ConsumeOnboardTestCase):
    def test_first_activation_creates_units_and_enables(self) -> None:
        self._make_mailbox_hub()
        ctx = self._load_ctx()
        runner = FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-enabled", ctx.consume_timer): CommandResult(1, "disabled\n", ""),
                ("tmux", "has-session", "-t", ctx.tmux_session): CommandResult(1, "", "no session"),
            }
        )
        report = onboard(
            ctx, runner, FakeGroupMembership(True, "ok"), execute=True, current_account=ACCOUNT
        )
        self.assertFalse(report.dry_run)
        service_path = ctx.unit_dir / "pantheon-persona-consume@.service"
        timer_path = ctx.unit_dir / "pantheon-persona-consume@.timer"
        self.assertTrue(service_path.is_file())
        self.assertTrue(timer_path.is_file())
        content = service_path.read_text(encoding="utf-8")
        self.assertIn("# Managed by pantheon persona installer", content)
        self.assertIn(f"persona={PERSONA}", content)
        self.assertIn(f"role={ROLE}", content)
        self.assertTrue(ctx.receipts_dir.is_dir())
        self.assertTrue(ctx.personas_state_dir.is_dir())
        self.assertIn(("systemctl", "--user", "daemon-reload"), runner.calls)
        self.assertIn(
            ("systemctl", "--user", "enable", "--now", ctx.consume_timer), runner.calls
        )
        self.assertFalse(report.stale_pane_detected)

    def test_second_execute_is_idempotent_no_stale_pane_check(self) -> None:
        self._make_mailbox_hub()
        ctx = self._load_ctx()
        runner1 = FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-enabled", ctx.consume_timer): CommandResult(1, "disabled\n", ""),
                ("tmux", "has-session", "-t", ctx.tmux_session): CommandResult(1, "", ""),
            }
        )
        onboard(ctx, runner1, FakeGroupMembership(True, "ok"), execute=True, current_account=ACCOUNT)

        runner2 = FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-enabled", ctx.consume_timer): CommandResult(0, "enabled\n", ""),
            }
        )
        report2 = onboard(
            ctx, runner2, FakeGroupMembership(True, "ok"), execute=True, current_account=ACCOUNT
        )
        tmux_calls = [call for call in runner2.calls if call and call[0] == "tmux"]
        self.assertEqual(tmux_calls, [])
        self.assertFalse(any(item.startswith("wrote ") for item in report2.changed))
        self.assertFalse(any(item.startswith("created ") for item in report2.changed))

    def test_stale_pane_blocks_first_activation_without_kill_flag(self) -> None:
        self._make_mailbox_hub()
        ctx = self._load_ctx()
        runner = FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-enabled", ctx.consume_timer): CommandResult(1, "disabled\n", ""),
                ("tmux", "has-session", "-t", ctx.tmux_session): CommandResult(0, "", ""),
            }
        )
        with self.assertRaises(OnboardError):
            onboard(ctx, runner, FakeGroupMembership(True, "ok"), execute=True, current_account=ACCOUNT)
        self.assertNotIn(
            ("systemctl", "--user", "enable", "--now", ctx.consume_timer), runner.calls
        )
        self.assertNotIn(("tmux", "kill-session", "-t", ctx.tmux_session), runner.calls)

    def test_kill_stale_pane_flag_kills_then_enables(self) -> None:
        self._make_mailbox_hub()
        ctx = self._load_ctx()
        runner = FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-enabled", ctx.consume_timer): CommandResult(1, "disabled\n", ""),
                ("tmux", "has-session", "-t", ctx.tmux_session): CommandResult(0, "", ""),
            }
        )
        report = onboard(
            ctx,
            runner,
            FakeGroupMembership(True, "ok"),
            execute=True,
            kill_stale_pane=True,
            current_account=ACCOUNT,
        )
        self.assertTrue(report.stale_pane_killed)
        kill_call = ("tmux", "kill-session", "-t", ctx.tmux_session)
        enable_call = ("systemctl", "--user", "enable", "--now", ctx.consume_timer)
        self.assertIn(kill_call, runner.calls)
        self.assertIn(enable_call, runner.calls)
        self.assertLess(runner.calls.index(kill_call), runner.calls.index(enable_call))

    def test_wrong_account_refuses_execute(self) -> None:
        self._make_mailbox_hub()
        ctx = self._load_ctx()
        runner = FakeCommandRunner()
        with self.assertRaises(OnboardError):
            onboard(
                ctx,
                runner,
                FakeGroupMembership(True, "ok"),
                execute=True,
                current_account="someone-else",
            )
        self.assertEqual(runner.calls, [])
        self.assertFalse(ctx.unit_dir.exists())


class MailboxPreconditionTests(ConsumeOnboardTestCase):
    def test_mailbox_hub_absent_refuses_execute(self) -> None:
        ctx = self._load_ctx()  # deliberately skip _make_mailbox_hub()
        runner = FakeCommandRunner()
        with self.assertRaises(OnboardError):
            onboard(ctx, runner, FakeGroupMembership(True, "ok"), execute=True, current_account=ACCOUNT)
        self.assertFalse(ctx.unit_dir.exists())

    def test_group_membership_missing_refuses_execute(self) -> None:
        self._make_mailbox_hub()
        ctx = self._load_ctx()
        runner = FakeCommandRunner()
        with self.assertRaises(OnboardError):
            onboard(
                ctx,
                runner,
                FakeGroupMembership(False, f"{ACCOUNT} is not a member of pantheon-mailbox"),
                execute=True,
                current_account=ACCOUNT,
            )
        self.assertFalse(ctx.unit_dir.exists())

    def test_required_unit_not_enabled_refuses_execute(self) -> None:
        self._make_mailbox_hub()
        unit = f"pantheon-mailbox-wake@{PERSONA}.timer"
        ctx = self._load_ctx(required_units=(unit,))
        runner = FakeCommandRunner(
            scripted={("systemctl", "--user", "is-enabled", unit): CommandResult(1, "disabled\n", "")}
        )
        with self.assertRaises(OnboardError):
            onboard(ctx, runner, FakeGroupMembership(True, "ok"), execute=True, current_account=ACCOUNT)

    def test_required_unit_enabled_passes(self) -> None:
        self._make_mailbox_hub()
        unit = f"pantheon-mailbox-wake@{PERSONA}.timer"
        ctx = self._load_ctx(required_units=(unit,))
        runner = FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-enabled", unit): CommandResult(0, "enabled\n", ""),
                ("systemctl", "--user", "is-enabled", ctx.consume_timer): CommandResult(1, "disabled\n", ""),
                ("tmux", "has-session", "-t", ctx.tmux_session): CommandResult(1, "", ""),
            }
        )
        report = onboard(ctx, runner, FakeGroupMembership(True, "ok"), execute=True, current_account=ACCOUNT)
        self.assertFalse(report.dry_run)

    def test_empty_required_units_is_a_trivial_pass(self) -> None:
        # today's real instance.yaml carries no required_units key -- confirm the
        # gate is a no-op until the mailbox ADR (T-2683) populates it.
        self._make_mailbox_hub()
        ctx = self._load_ctx()
        self.assertEqual(ctx.required_mailbox_units, ())


class VerifyTests(ConsumeOnboardTestCase):
    def _runtime_json(self, ctx) -> Path:
        path = ctx.personas_state_dir / PERSONA / "runtime-controller" / "runtime.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        return path

    def test_all_green(self) -> None:
        self._make_mailbox_hub()
        ctx = self._load_ctx()
        self._runtime_json(ctx)
        runner = FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-failed", ctx.consume_service): CommandResult(1, "active\n", ""),
                ("systemctl", "--user", "is-enabled", ctx.consume_timer): CommandResult(0, "enabled\n", ""),
                (
                    "systemctl", "--user", "show", ctx.consume_timer,
                    "-p", "NextElapseUSecRealtime", "-p", "NextElapseUSecMonotonic",
                ): CommandResult(
                    0, "NextElapseUSecRealtime=Mon 2026-08-10 00:00:00 UTC\nNextElapseUSecMonotonic=0\n", ""
                ),
                ("pgrep", "-u", ACCOUNT, "-a", "-f", r"\.npm-global/bin/clau[d]e"): CommandResult(
                    0, "12345 claude --model claude-opus-4-8 --permission-mode bypassPermissions\n", ""
                ),
            }
        )
        checks = verify(ctx, runner)
        by_name = {c.name: c for c in checks}
        self.assertTrue(by_name["service.not_failed"].ok)
        self.assertTrue(by_name["timer.enabled"].ok)
        self.assertTrue(by_name["timer.next_elapse_finite"].ok)
        self.assertTrue(by_name["runtime.channel_less_argv"].ok)
        self.assertTrue(by_name["runtime.json_present"].ok)
        self.assertIsNone(by_name["mailbox.round_trip_selfwake"].ok)

    def test_red_case_failed_service_missing_runtime_json_and_channels_present(self) -> None:
        self._make_mailbox_hub()
        ctx = self._load_ctx()
        runner = FakeCommandRunner(
            scripted={
                ("systemctl", "--user", "is-failed", ctx.consume_service): CommandResult(0, "failed\n", ""),
                ("systemctl", "--user", "is-enabled", ctx.consume_timer): CommandResult(1, "disabled\n", ""),
                (
                    "systemctl", "--user", "show", ctx.consume_timer,
                    "-p", "NextElapseUSecRealtime", "-p", "NextElapseUSecMonotonic",
                ): CommandResult(0, "NextElapseUSecRealtime=n/a\nNextElapseUSecMonotonic=0\n", ""),
                ("pgrep", "-u", ACCOUNT, "-a", "-f", r"\.npm-global/bin/clau[d]e"): CommandResult(
                    0,
                    "12345 claude --model claude-opus-4-8 --permission-mode bypassPermissions "
                    "--channels plugin:telegram@x\n",
                    "",
                ),
            }
        )
        checks = verify(ctx, runner)
        by_name = {c.name: c for c in checks}
        self.assertFalse(by_name["service.not_failed"].ok)
        self.assertFalse(by_name["timer.enabled"].ok)
        self.assertFalse(by_name["timer.next_elapse_finite"].ok)
        self.assertFalse(by_name["runtime.channel_less_argv"].ok)
        self.assertFalse(by_name["runtime.json_present"].ok)

    def test_pgrep_pattern_avoids_contiguous_claude_substring(self) -> None:
        self._make_mailbox_hub()
        ctx = self._load_ctx()
        runner = FakeCommandRunner()
        verify(ctx, runner)
        pgrep_calls = [call for call in runner.calls if call and call[0] == "pgrep"]
        self.assertTrue(pgrep_calls)
        for call in pgrep_calls:
            self.assertNotIn("claude", " ".join(call))


class RollbackTests(ConsumeOnboardTestCase):
    def test_dry_run_makes_no_calls(self) -> None:
        self._make_mailbox_hub()
        ctx = self._load_ctx()
        runner = FakeCommandRunner()
        report = rollback(ctx, runner, execute=False)
        self.assertTrue(report.dry_run)
        self.assertEqual(runner.calls, [])

    def test_execute_disables_and_resets_failed(self) -> None:
        self._make_mailbox_hub()
        ctx = self._load_ctx()
        runner = FakeCommandRunner()
        report = rollback(ctx, runner, execute=True)
        self.assertFalse(report.dry_run)
        self.assertIn(
            ("systemctl", "--user", "disable", "--now", ctx.consume_timer), runner.calls
        )
        self.assertIn(("systemctl", "--user", "reset-failed", ctx.consume_service), runner.calls)


if __name__ == "__main__":
    unittest.main()
