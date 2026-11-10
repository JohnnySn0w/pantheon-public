"""fleet_health tests (T-1049): per-agent classification, the fleet verdict, and
main() exit codes. The `run` seam is stubbed — no live sudo/systemctl/bwrap."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools import fleet_health as fh


def _runner(*, svc="active", session="yes", bwrap="ok", enabled="enabled", ran=True,
            policy_skip: str | None = None):
    """Build a stub `run(user, inner)` that answers each check kind.

    `enabled` defaults to "enabled" (not paused) so every pre-T-2706 test that
    doesn't care about pause-state keeps exercising the normal check path.
    `policy_skip` (T-3210) is the raw JSON text `cat <persona>.policyskip.json`
    would print; None (default) reproduces "no counter file" (empty stdout)."""
    def _f(user, inner, timeout=15):
        if not ran:
            return (None, "", False)
        if "is-enabled" in inner:
            return (0, enabled, True)
        if "is-active" in inner:
            return (0, svc, True)
        if "has-session" in inner:
            return (0, session, True)
        if "bwrap" in inner:
            return (0 if bwrap == "ok" else 1, bwrap, True)
        if "policyskip.json" in inner:
            return (0, policy_skip or "", True)
        return (0, "", True)
    return _f


class CheckAgent(unittest.TestCase):
    def test_all_ok(self):
        r = fh.check_agent("codex", fh.FLEET["codex"], run=_runner())
        self.assertEqual(r["status"], fh.OK)
        self.assertTrue(all(c["status"] == fh.OK for c in r["checks"]))
        # codex has 2 services + session + bwrap = 4 checks
        self.assertEqual(len(r["checks"]), 4)

    def test_service_down_is_fail(self):
        r = fh.check_agent("recovery", fh.FLEET["recovery"], run=_runner(svc="inactive"))
        self.assertEqual(r["status"], fh.FAIL)
        self.assertTrue(any(c["status"] == fh.FAIL and c["name"].startswith("svc:")
                            for c in r["checks"]))

    def test_missing_session_is_fail(self):
        r = fh.check_agent("orchestrator", fh.FLEET["orchestrator"], run=_runner(session="no"))
        self.assertEqual(r["status"], fh.FAIL)
        sess = [c for c in r["checks"] if c["name"] == "session"][0]
        self.assertEqual(sess["status"], fh.FAIL)
        self.assertIn("MISSING", sess["detail"])

    def test_codex_bwrap_broken_is_fail_with_sysctl_hint(self):
        r = fh.check_agent("codex", fh.FLEET["codex"], run=_runner(bwrap="broken"))
        self.assertEqual(r["status"], fh.FAIL)
        bw = [c for c in r["checks"] if c["name"] == "bwrap-userns"][0]
        self.assertEqual(bw["status"], fh.FAIL)
        self.assertIn("apparmor_restrict_unprivileged_userns", bw["detail"])

    def test_non_codex_has_no_bwrap_check(self):
        r = fh.check_agent("specialist", fh.FLEET["specialist"], run=_runner())
        self.assertFalse(any(c["name"] == "bwrap-userns" for c in r["checks"]))

    def test_unqueryable_is_warn_not_fail(self):
        r = fh.check_agent("recovery", fh.FLEET["recovery"], run=_runner(ran=False))
        self.assertEqual(r["status"], fh.WARN)  # a sweep that can't run never FAILs


class RenderAndMain(unittest.TestCase):
    def test_render_all_healthy(self):
        results = [{"agent": "recovery", "status": fh.OK, "checks": []}]
        self.assertIn("all healthy", fh.render(results))

    def test_render_degraded_names_failures(self):
        results = [
            {"agent": "recovery", "status": fh.OK, "checks": []},
            {"agent": "codex", "status": fh.FAIL,
             "checks": [{"name": "bwrap-userns", "status": fh.FAIL, "detail": "x"}]},
        ]
        out = fh.render(results)
        self.assertIn("DEGRADED", out)
        self.assertIn("codex", out)

    def test_render_warnings_only(self):
        results = [{"agent": "orchestrator", "status": fh.WARN, "checks": []}]
        self.assertIn("healthy with warnings", fh.render(results))

    def test_main_exit_0_when_healthy(self):
        with mock.patch.object(fh, "sweep",
                               return_value=[{"agent": "recovery", "status": fh.OK, "checks": []}]), \
             mock.patch.object(fh, "failed_units_sweep",
                               return_value=[{"scope": "system", "status": fh.OK, "units": [], "detail": ""}]):
            self.assertEqual(fh.main([]), 0)

    def test_main_exit_1_when_any_fail(self):
        with mock.patch.object(fh, "sweep",
                               return_value=[{"agent": "codex", "status": fh.FAIL, "checks": []}]), \
             mock.patch.object(fh, "failed_units_sweep", return_value=[]):
            self.assertEqual(fh.main([]), 1)

    def test_main_single_agent_arg(self):
        with mock.patch.object(fh, "sweep", return_value=[]) as m, \
             mock.patch.object(fh, "failed_units_sweep", return_value=[]):
            fh.main(["--agent", "codex"])
            m.assert_called_once_with(["codex"])

    def test_main_exit_1_when_failed_unit_even_if_agents_healthy(self):
        with mock.patch.object(fh, "sweep",
                               return_value=[{"agent": "recovery", "status": fh.OK, "checks": []}]), \
             mock.patch.object(fh, "failed_units_sweep",
                               return_value=[{"scope": "system", "status": fh.FAIL,
                                              "units": ["mosquitto.service"], "detail": "mosquitto.service"}]):
            self.assertEqual(fh.main([]), 1)

    def test_main_no_failed_sweep_skips_it(self):
        with mock.patch.object(fh, "sweep",
                               return_value=[{"agent": "recovery", "status": fh.OK, "checks": []}]), \
             mock.patch.object(fh, "failed_units_sweep") as m:
            self.assertEqual(fh.main(["--no-failed-sweep"]), 0)
            m.assert_not_called()


class FailedUnitsSweep(unittest.TestCase):
    def test_parse_empty(self):
        self.assertEqual(fh._parse_failed_units(""), [])

    def test_parse_extracts_unit_names(self):
        out = ("mosquitto.service loaded failed failed Mosquitto MQTT Broker\n"
               "foo.timer        loaded failed failed Foo timer")
        self.assertEqual(fh._parse_failed_units(out), ["mosquitto.service", "foo.timer"])

    def test_parse_ignores_non_unit_lines(self):
        self.assertEqual(fh._parse_failed_units("garbage line no unit here"), [])

    def test_sweep_clean_all_ok(self):
        run = lambda user, inner, timeout=15: (0, "", True)  # noqa: E731
        recs = fh.failed_units_sweep(["recovery", "orchestrator"], run=run)
        self.assertTrue(all(r["status"] == fh.OK for r in recs))
        self.assertIn("system", [r["scope"] for r in recs])

    def test_sweep_flags_failed_unit_as_fail(self):
        def run(user, inner, timeout=15):
            if "--user" not in inner:  # system scope
                return (0, "mosquitto.service loaded failed failed X", True)
            return (0, "", True)
        recs = fh.failed_units_sweep(["recovery"], run=run)
        sysrec = next(r for r in recs if r["scope"] == "system")
        self.assertEqual(sysrec["status"], fh.FAIL)
        self.assertIn("mosquitto.service", sysrec["units"])

    def test_sweep_unqueryable_is_warn(self):
        run = lambda user, inner, timeout=15: (None, "", False)  # noqa: E731
        recs = fh.failed_units_sweep(["recovery"], run=run)
        self.assertTrue(all(r["status"] == fh.WARN for r in recs))

    def test_sweep_dedups_users(self):
        seen = []
        def run(user, inner, timeout=15):
            if "--user" in inner:
                seen.append(user)
            return (0, "", True)
        # all four agents are distinct users, so 4 user-scope calls expected
        fh.failed_units_sweep(list(fh.FLEET), run=run)
        self.assertEqual(sorted(seen), sorted({fh.FLEET[a]["user"] for a in fh.FLEET}))


class RenderWithFailedUnits(unittest.TestCase):
    def test_render_shows_failed_units_and_degrades(self):
        agents = [{"agent": "recovery", "status": fh.OK, "checks": []}]
        failed = [{"scope": "system", "status": fh.FAIL,
                   "units": ["mosquitto.service"], "detail": "mosquitto.service"}]
        out = fh.render(agents, failed)
        self.assertIn("mosquitto.service", out)
        self.assertIn("DEGRADED", out)

    def test_render_without_failed_arg_is_backward_compatible(self):
        agents = [{"agent": "recovery", "status": fh.OK, "checks": []}]
        self.assertIn("all healthy", fh.render(agents))


class ControlPlaneProbe(unittest.TestCase):
    """T-2685: control-plane seats (specialist — T-2633 cutover; recovery — T-3026
    cutover, 2026-09-29) must be probed via the persistently-active consume
    TIMER, not the disabled bespoke unit and not the oneshot consume SERVICE
    (never is-active after it exits). Recovery was NOT control-plane pre-T-3026
    (T-2684 reverted T-3082, 2026-08-23: the consume plane alone had moved,
    the interactive session still lived on the bespoke claude-telegram-
    fallback.service, so a timer probe would have reported healthy while the
    actual session was dead). T-3026 moved the interactive session onto the
    control plane, so recovery is on the consume-timer probe, exactly as specialist.
    T-2953 (2026-10-24): orchestrator cut over to the control plane; mirrors recovery
    (T-3026), so orchestrator is on the consume-timer probe too."""

    def test_orchestrator_fleet_health_uses_consume_timer_post_t2953(self):
        self.assertEqual(fh.FLEET["orchestrator"]["services"],
                         ["pantheon-persona-consume@orchestrator.timer"])

    def test_control_plane_services_derive_from_consume_timer(self):
        self.assertEqual(fh.FLEET["recovery"]["services"],
                         ["pantheon-persona-consume@recovery.timer"])
        self.assertEqual(fh.FLEET["specialist"]["services"],
                         ["pantheon-persona-consume@specialist.timer"])

    def test_control_plane_healthy_when_timer_active(self):
        r = fh.check_agent("recovery", fh.FLEET["recovery"], run=_runner())
        self.assertEqual(r["status"], fh.OK)
        r = fh.check_agent("specialist", fh.FLEET["specialist"], run=_runner())
        self.assertEqual(r["status"], fh.OK)

    def test_control_plane_fail_when_timer_inactive(self):
        r = fh.check_agent("recovery", fh.FLEET["recovery"], run=_runner(svc="inactive"))
        self.assertEqual(r["status"], fh.FAIL)
        svc = [c for c in r["checks"] if c["name"].startswith("svc:")][0]
        self.assertIn("pantheon-persona-consume@recovery.timer", svc["name"])

    def test_codex_stays_bespoke(self):
        self.assertEqual(fh.FLEET["codex"]["services"],
                         ["codex-daemon.service", "codex-telegram-poller.service"])


class SubOrchSeats(unittest.TestCase):
    """T-2690: mason (suborch1) + artisan (suborch2) are consume control-plane
    seats (T-2677) but their instance.yaml `roles:` binding does NOT yet carry
    `supervisor: control-plane` (a manifest gap, distinct from T-2691 —
    flagged separately), so `_liveness_services` cannot auto-derive their
    timer the way it does for recovery/specialist. Their FLEET entries pass the
    already-verified-live consume timer explicitly instead (bespoke list) —
    the T-2677 resolve note confirms both timers armed + firing green.

    Mailbox-first, NO telegram channel (T-2677): no telegram-poller or
    mailbox-wake unit belongs in either seat's services."""

    def test_mason_services_is_explicit_consume_timer(self):
        self.assertEqual(fh.FLEET["mason"]["services"],
                         ["pantheon-persona-consume@mason.timer"])

    def test_artisan_services_is_explicit_consume_timer(self):
        self.assertEqual(fh.FLEET["artisan"]["services"],
                         ["pantheon-persona-consume@artisan.timer"])

    def test_mason_and_artisan_carry_no_telegram_or_mailbox_wake_unit(self):
        for persona in ("mason", "artisan"):
            for svc in fh.FLEET[persona]["services"]:
                self.assertNotIn("telegram", svc)
                self.assertNotIn("mailbox-wake", svc)

    def test_mason_and_artisan_user_and_session(self):
        self.assertEqual(fh.FLEET["mason"]["user"], "suborch1")
        self.assertEqual(fh.FLEET["mason"]["session"], "suborch1-main")
        self.assertEqual(fh.FLEET["artisan"]["user"], "suborch2")
        self.assertEqual(fh.FLEET["artisan"]["session"], "suborch2-main")

    def test_mason_healthy_when_timer_active(self):
        r = fh.check_agent("mason", fh.FLEET["mason"], run=_runner())
        self.assertEqual(r["status"], fh.OK)

    def test_artisan_healthy_when_timer_active(self):
        r = fh.check_agent("artisan", fh.FLEET["artisan"], run=_runner())
        self.assertEqual(r["status"], fh.OK)

    def test_mason_fail_when_timer_inactive(self):
        r = fh.check_agent("mason", fh.FLEET["mason"], run=_runner(svc="inactive"))
        self.assertEqual(r["status"], fh.FAIL)
        svc = [c for c in r["checks"] if c["name"].startswith("svc:")][0]
        self.assertIn("pantheon-persona-consume@mason.timer", svc["name"])

    def test_no_bwrap_check_for_suborch_seats(self):
        for persona in ("mason", "artisan"):
            r = fh.check_agent(persona, fh.FLEET[persona], run=_runner())
            self.assertFalse(any(c["name"] == "bwrap-userns" for c in r["checks"]))


class PausedSeat(unittest.TestCase):
    """T-2706: a deliberately-paused seat (session-owning unit disabled) reads
    PAUSED, not FAIL — mirrors channel_liveness_monitor's is-enabled
    discriminator (disabled -> paused, NOT is-active/inactive)."""

    def test_disabled_service_reads_paused_not_fail(self):
        r = fh.check_agent("archivist", fh.FLEET["archivist"],
                           run=_runner(enabled="disabled"))
        self.assertEqual(r["status"], fh.PAUSED)
        self.assertFalse(any(c["status"] == fh.FAIL for c in r["checks"]))
        self.assertFalse(any(c["status"] == fh.WARN for c in r["checks"]))

    def test_paused_short_circuits_before_normal_checks(self):
        # Even though svc/session would read FAIL if judged normally, the
        # disabled short-circuit means they're never probed at all — one check.
        r = fh.check_agent("archivist", fh.FLEET["archivist"],
                           run=_runner(enabled="disabled", svc="inactive", session="no"))
        self.assertEqual(r["status"], fh.PAUSED)
        self.assertEqual(len(r["checks"]), 1)

    def test_masked_service_also_reads_paused(self):
        r = fh.check_agent("archivist", fh.FLEET["archivist"],
                           run=_runner(enabled="masked"))
        self.assertEqual(r["status"], fh.PAUSED)

    def test_enabled_but_inactive_service_still_fails(self):
        # Crash != pause: an enabled unit that's merely not active is a real FAIL.
        r = fh.check_agent("archivist", fh.FLEET["archivist"],
                           run=_runner(enabled="enabled", svc="inactive"))
        self.assertEqual(r["status"], fh.FAIL)

    def test_control_plane_seat_not_misread_as_paused(self):
        # recovery's session-owning unit is the consume TIMER; healthy == enabled+active.
        # Must judge normally (OK), never PAUSED, on a healthy control-plane seat.
        r = fh.check_agent("recovery", fh.FLEET["recovery"], run=_runner(enabled="enabled"))
        self.assertEqual(r["status"], fh.OK)

    def test_unqueryable_is_enabled_probe_judges_normally_not_paused(self):
        # ran=False means EVERY probe (including is-enabled) is unqueryable ->
        # _is_paused returns None -> falls through to the normal (WARN) path,
        # never silently reads PAUSED for a seat we simply couldn't ask.
        r = fh.check_agent("archivist", fh.FLEET["archivist"], run=_runner(ran=False))
        self.assertEqual(r["status"], fh.WARN)


class ArchivistFleetEntry(unittest.TestCase):
    def test_archivist_user_session_services(self):
        self.assertEqual(fh.FLEET["archivist"]["user"], "exocortex")
        self.assertEqual(fh.FLEET["archivist"]["session"], "claude-telegram-bot-exocortex")
        # T-5063: cut over to the control plane (2026-10-18), so the unit fleet_health
        # checks is the consume timer, as for specialist and recovery.
        self.assertEqual(fh.FLEET["archivist"]["services"],
                         ["pantheon-persona-consume@archivist.timer"])

    def test_archivist_healthy_when_enabled_and_active(self):
        r = fh.check_agent("archivist", fh.FLEET["archivist"], run=_runner())
        self.assertEqual(r["status"], fh.OK)


class RenderAndExitCodeWithPaused(unittest.TestCase):
    def test_render_paused_does_not_degrade_fleet(self):
        results = [
            {"agent": "recovery", "status": fh.OK, "checks": []},
            {"agent": "archivist", "status": fh.PAUSED,
             "checks": [{"name": "paused:x", "status": fh.PAUSED, "detail": "x disabled"}]},
        ]
        out = fh.render(results)
        self.assertNotIn("DEGRADED", out)
        self.assertIn("paused", out)
        self.assertIn("archivist", out)

    def test_render_paused_alongside_warn_does_not_degrade(self):
        results = [
            {"agent": "orchestrator", "status": fh.WARN, "checks": []},
            {"agent": "archivist", "status": fh.PAUSED, "checks": []},
        ]
        out = fh.render(results)
        self.assertNotIn("DEGRADED", out)
        self.assertIn("healthy with warnings", out)
        self.assertIn("archivist", out)
        self.assertIn("paused", out)

    def test_main_exit_0_when_only_non_ok_is_paused(self):
        with mock.patch.object(
            fh, "sweep",
            return_value=[
                {"agent": "recovery", "status": fh.OK, "checks": []},
                {"agent": "archivist", "status": fh.PAUSED, "checks": []},
            ],
        ), mock.patch.object(
            fh, "failed_units_sweep",
            return_value=[{"scope": "system", "status": fh.OK, "units": [], "detail": ""}],
        ):
            self.assertEqual(fh.main([]), 0)


class SudoDenial(unittest.TestCase):
    """T-2718: a `sudo -n` auth/setup denial must classify as 'never ran' (WARN),
    never as the inner command's verdict (FAIL). The dangerous direction — softening a
    genuinely-dead seat's real FAIL to WARN — must NOT happen."""

    def test_denial_markers_detected(self):
        for stderr in (
            "sudo: a password is required",
            "sudo: a password is required\n",
            "sudo: a terminal is required to read the password",
            "sudo: no askpass program specified",
            "sudo: unknown user: suborch9",
            "sudo: nonexistent is not allowed to execute",
        ):
            self.assertTrue(fh._sudo_denied(1, stderr), stderr)

    def test_rc_zero_is_never_denial(self):
        self.assertFalse(fh._sudo_denied(0, "sudo: a password is required"))

    def test_real_command_failure_is_not_denial(self):
        # The inner command RAN and returned non-zero — a genuine FAIL, not a sudo
        # denial. Must not be softened to WARN even though rc != 0.
        self.assertFalse(fh._sudo_denied(3, "Failed to get properties: Unit not found"))
        self.assertFalse(fh._sudo_denied(1, ""))
        self.assertFalse(fh._sudo_denied(1, "inactive"))

    def test_run_sudo_denied_returns_not_ran(self):
        # End-to-end: a non-current user + a sudo-denial stderr => ran=False.
        import getpass
        other = "definitely-not-" + getpass.getuser()
        fake = mock.Mock(returncode=1, stdout="", stderr="sudo: a password is required")
        with mock.patch.object(fh.subprocess, "run", return_value=fake):
            rc, out, ran = fh._run(other, "systemctl --user is-active foo.service")
        self.assertFalse(ran)

    def test_run_real_failure_still_ran(self):
        # Same non-current user, but the inner command actually ran and failed
        # (rc=3, non-sudo stderr) => ran=True so the seat's real FAIL is preserved.
        import getpass
        other = "definitely-not-" + getpass.getuser()
        fake = mock.Mock(returncode=3, stdout="inactive", stderr="")
        with mock.patch.object(fh.subprocess, "run", return_value=fake):
            rc, out, ran = fh._run(other, "systemctl --user is-active foo.service")
        self.assertTrue(ran)
        self.assertEqual(out, "inactive")

    def test_non_recovery_seat_denial_is_warn_not_fail(self):
        # The full T-2718 repro at the check_agent level: every cross-account probe is
        # sudo-denied => the seat is WARN (unqueryable), NOT FAIL — so no false DEGRADED.
        def denied_run(user, inner, timeout=15):
            return (1, "", False)  # what _run now returns for a sudo denial
        r = fh.check_agent("orchestrator", fh.FLEET["orchestrator"], run=denied_run)
        self.assertEqual(r["status"], fh.WARN)
        self.assertTrue(all(c["status"] == fh.WARN for c in r["checks"]))


class PolicySkipVisibility(unittest.TestCase):
    """T-3210: a consume seat whose attempt stage is 100% policy-skipped
    completes every fire (exit 0/5) and reads healthy to every OTHER check --
    this is the row that closes that gap."""

    def _skip_json(self, reason="runtime_viability_blocked", count=27,
                    since="2026-08-01T00:00:00Z"):
        import json
        return json.dumps({"reason": reason, "count": count, "since": since})

    def test_no_counter_file_no_row(self):
        r = fh.check_agent("specialist", fh.FLEET["specialist"], run=_runner())
        self.assertFalse(any(c["name"] == "consume:policy-skip" for c in r["checks"]))

    def test_corrupt_counter_file_no_row(self):
        r = fh.check_agent("specialist", fh.FLEET["specialist"],
                            run=_runner(policy_skip="{not json"))
        self.assertFalse(any(c["name"] == "consume:policy-skip" for c in r["checks"]))

    def test_below_floor_no_row(self):
        r = fh.check_agent(
            "specialist", fh.FLEET["specialist"],
            run=_runner(policy_skip=self._skip_json(count=fh.POLICY_SKIP_ROW_MIN_FIRES - 1)),
        )
        self.assertFalse(any(c["name"] == "consume:policy-skip" for c in r["checks"]))

    def test_at_floor_row_renders(self):
        r = fh.check_agent(
            "specialist", fh.FLEET["specialist"],
            run=_runner(policy_skip=self._skip_json(count=fh.POLICY_SKIP_ROW_MIN_FIRES)),
        )
        row = next(c for c in r["checks"] if c["name"] == "consume:policy-skip")
        self.assertIn("runtime_viability_blocked", row["detail"])
        self.assertIn(f"({fh.POLICY_SKIP_ROW_MIN_FIRES} fires)", row["detail"])

    def test_no_manifest_key_is_warn_unexpected(self):
        with mock.patch.object(fh, "_instance_binding", return_value={}):
            r = fh.check_agent("specialist", fh.FLEET["specialist"],
                                run=_runner(policy_skip=self._skip_json()))
        row = next(c for c in r["checks"] if c["name"] == "consume:policy-skip")
        self.assertEqual(row["status"], fh.WARN)
        self.assertIn("unexpected", row["detail"])
        self.assertEqual(r["status"], fh.WARN)  # folds into the agent's overall status

    def test_live_manifest_has_no_standing_skip_marker_for_consume_seats(self):
        # T-5311: live-pane drift no longer blocks, so a persisted
        # runtime_viability_blocked streak on a consume seat is a real WARN.
        for persona in ("specialist", "recovery", "mason", "artisan", "archivist"):
            with self.subTest(persona=persona):
                self.assertNotIn("expected_policy_skip", fh._instance_binding(persona))

    def test_manifest_match_is_ok_expected(self):
        with mock.patch.object(
            fh, "_instance_binding",
            return_value={"expected_policy_skip": "runtime_viability_blocked"},
        ):
            r = fh.check_agent("specialist", fh.FLEET["specialist"],
                                run=_runner(policy_skip=self._skip_json()))
        row = next(c for c in r["checks"] if c["name"] == "consume:policy-skip")
        self.assertEqual(row["status"], fh.OK)
        self.assertIn("expected (manifest expected_policy_skip)", row["detail"])
        # an OK-status row must not itself push the agent's overall status
        # (all other checks OK) above OK.
        self.assertEqual(r["status"], fh.OK)

    def test_prior_receiver_block_is_not_masked_by_expected_viability_block(self):
        # T-4179: specialist expects runtime_viability_blocked; a prior-receiver
        # refusal persists its own token (prior_receiver_blocked) -> WARN.
        with mock.patch.object(
            fh, "_instance_binding",
            return_value={"expected_policy_skip": "runtime_viability_blocked"},
        ):
            r = fh.check_agent(
                "specialist", fh.FLEET["specialist"],
                run=_runner(policy_skip=self._skip_json(reason="prior_receiver_blocked")),
            )
        row = next(c for c in r["checks"] if c["name"] == "consume:policy-skip")
        self.assertEqual(row["status"], fh.WARN)
        self.assertIn("prior_receiver_blocked", row["detail"])
        self.assertIn("unexpected (manifest expects runtime_viability_blocked)", row["detail"])

    def _row(self, reason, *, manifest=None):
        with mock.patch.object(fh, "_instance_binding", return_value=manifest or {}):
            r = fh.check_agent(
                "specialist", fh.FLEET["specialist"],
                run=_runner(policy_skip=self._skip_json(reason=reason)),
            )
        return r, next(c for c in r["checks"] if c["name"] == "consume:policy-skip")

    def test_seat_maintenance_is_ok_while_window_active_T5285(self):
        with mock.patch("tools.notify_policy.in_maintenance", return_value=True):
            r, row = self._row("seat_maintenance", manifest={"expected_policy_skip": "backoff"})
        self.assertEqual(row["status"], fh.OK)
        self.assertIn("expected (seat maintenance window)", row["detail"])
        self.assertEqual(r["status"], fh.OK)

    def test_seat_maintenance_stale_streak_after_window_is_warn_T5285(self):
        with mock.patch("tools.notify_policy.in_maintenance", return_value=False):
            _, row = self._row("seat_maintenance")
        self.assertEqual(row["status"], fh.WARN)
        self.assertIn("unexpected", row["detail"])

    def test_seat_maintenance_unreadable_registry_is_warn_T5285(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # a directory where the registry file should be: read_text raises OSError
            with mock.patch.dict(os.environ, {"PANTHEON_MAINTENANCE_REGISTRY": tmp}):
                _, row = self._row("seat_maintenance")
        self.assertEqual(row["status"], fh.WARN)

    def test_seat_maintenance_check_raising_is_warn_T5285(self):
        with mock.patch("tools.notify_policy.in_maintenance", side_effect=RuntimeError("boom")):
            _, row = self._row("seat_maintenance")
        self.assertEqual(row["status"], fh.WARN)

    def test_all_runtimes_held_is_ok_expected_T5285(self):
        r, row = self._row("all_runtimes_held")
        self.assertEqual(row["status"], fh.OK)
        self.assertIn("expected (all runtimes held)", row["detail"])
        self.assertEqual(r["status"], fh.OK)

    def test_other_reasons_unchanged_T5285(self):
        for reason in ("backoff", "runtime_viability_blocked", "relaunch_window_exhausted"):
            with self.subTest(reason=reason):
                _, row = self._row(reason)
                self.assertEqual(row["status"], fh.WARN)
                self.assertTrue(row["detail"].endswith("unexpected"))

    def test_manifest_mismatch_names_the_expected_reason(self):
        with mock.patch.object(
            fh, "_instance_binding", return_value={"expected_policy_skip": "backoff"},
        ):
            r = fh.check_agent(
                "specialist", fh.FLEET["specialist"],
                run=_runner(policy_skip=self._skip_json(reason="runtime_viability_blocked")),
            )
        row = next(c for c in r["checks"] if c["name"] == "consume:policy-skip")
        self.assertEqual(row["status"], fh.WARN)
        self.assertIn("unexpected (manifest expects backoff)", row["detail"])

    def test_paused_seat_short_circuits_before_policy_skip_check(self):
        r = fh.check_agent(
            "archivist", fh.FLEET["archivist"],
            run=_runner(enabled="disabled", policy_skip=self._skip_json()),
        )
        self.assertEqual(r["status"], fh.PAUSED)
        self.assertEqual(len(r["checks"]), 1)

    def test_render_shows_ok_status_policy_skip_row(self):
        # The one deliberate exception to render()'s "only non-OK checks
        # print" convention: this row is the expected-not-cut-over RECORD,
        # not a pass/fail probe, so it must be visible even at OK.
        results = [{
            "agent": "specialist", "status": fh.OK,
            "checks": [{"name": "consume:policy-skip", "status": fh.OK,
                        "detail": "policy-skipped runtime_viability_blocked for 3.0d "
                                  "(27 fires) — expected (manifest expected_policy_skip)"}],
        }]
        out = fh.render(results)
        self.assertIn("consume:policy-skip", out)
        self.assertIn("expected (manifest expected_policy_skip)", out)


def _seat(**kw):
    from tools.seat_state_testkit import eff
    return mock.patch("tools.seat_state.safe_effective_for", return_value=eff(**kw))


class SeatStateT5527(unittest.TestCase):
    """ORG-106 L4b (T-5527): a sleeping seat is ASLEEP not FAIL; a broken sleep is a FAIL."""

    def _agent(self, seat, **run_kw):
        with seat:
            return fh.check_agent("specialist", fh.FLEET["specialist"], run=_runner(**run_kw))

    def test_asleep_and_down_is_asleep_not_fail(self):
        r = self._agent(_seat(state="deep-sleep", asleep=True), svc="inactive", session="no")
        self.assertEqual(r["status"], fh.ASLEEP)
        self.assertEqual([c["name"] for c in r["checks"]], ["seat-state"])
        self.assertIn("asleep", r["checks"][0]["detail"])
        self.assertIn("operator", r["checks"][0]["detail"])

    def test_asleep_with_live_session_is_sleep_breach_fail(self):
        for run_kw in ({"svc": "active", "session": "no"}, {"svc": "inactive", "session": "yes"}):
            r = self._agent(_seat(state="deep-sleep", asleep=True), **run_kw)
            self.assertEqual(r["status"], fh.FAIL, run_kw)
            self.assertTrue(any(c["name"] == "sleep_breach" and c["status"] == fh.FAIL
                                for c in r["checks"]), run_kw)

    def test_sleep_breach_is_not_hidden_by_oom_hold_or_maintenance(self):
        r = self._agent(_seat(state="deep-sleep", asleep=True, oom=True, maintenance=True))
        self.assertEqual(r["status"], fh.FAIL)
        self.assertTrue(any(c["name"] == "sleep_breach" for c in r["checks"]))

    def test_sleep_breach_beats_deliberately_paused_unit(self):
        r = self._agent(_seat(state="deep-sleep", asleep=True), enabled="disabled", session="yes")
        self.assertEqual(r["status"], fh.FAIL)

    def test_oom_hold_with_live_session_alone_is_not_a_breach(self):
        r = self._agent(_seat(oom=True))
        self.assertEqual(r["status"], fh.OK)
        self.assertFalse(any(c["name"] == "sleep_breach" for c in r["checks"]))

    def test_draining_is_judged_normally_with_annotation(self):
        r = self._agent(_seat(state="deep-sleep", asleep=False))
        self.assertEqual(r["status"], fh.OK)
        note = next(c for c in r["checks"] if c["name"] == "seat-state")
        self.assertIn("draining", note["detail"])

    def test_paused_intake_is_judged_normally_with_paused_annotation(self):
        r = self._agent(_seat(state="paused-intake", expires="2026-10-27T00:00:00Z"))
        self.assertEqual(r["status"], fh.OK)
        note = next(c for c in r["checks"] if c["name"] == "seat-state")
        self.assertIn("paused", note["detail"])
        self.assertIn("2026-10-27T00:00:00Z", note["detail"])
        down = self._agent(_seat(state="paused-intake"), svc="inactive")
        self.assertEqual(down["status"], fh.FAIL)   # a paused seat that is down is still down

    def test_unknown_reader_result_is_normal(self):
        with mock.patch("tools.seat_state.safe_effective_for", return_value=None):
            r = fh.check_agent("specialist", fh.FLEET["specialist"], run=_runner())
        self.assertEqual(r["status"], fh.OK)
        self.assertFalse(any(c["name"] == "seat-state" for c in r["checks"]))

    def test_render_and_summary_show_asleep_and_paused_without_degrading(self):
        results = [
            {"agent": "specialist", "status": fh.ASLEEP, "checks": [
                {"name": "seat-state", "status": fh.OK, "detail": "asleep (deep-sleep)"}]},
            {"agent": "recovery", "status": fh.OK, "checks": [
                {"name": "seat-state", "status": fh.OK, "detail": "paused (operator)"}]},
        ]
        out = fh.render(results)
        self.assertIn("ASLEEP", out)
        self.assertIn("seat-state: paused (operator)", out)
        self.assertIn("FLEET: all healthy", out)
        self.assertIn("specialist asleep", out)
        self.assertNotIn("DEGRADED", out)

    def test_main_exit_zero_for_asleep(self):
        with mock.patch.object(fh, "sweep", return_value=[
                {"agent": "specialist", "status": fh.ASLEEP, "checks": []}]):
            self.assertEqual(fh.main(["--no-failed-sweep"]), 0)

    def _skip_row(self, reason, seat):
        import json as _json
        raw = _json.dumps({"reason": reason, "count": 40, "since": "2026-10-20T00:00:00Z"})
        with seat, mock.patch.object(fh, "_instance_binding", return_value={}):
            r = fh.check_agent("specialist", fh.FLEET["specialist"], run=_runner(policy_skip=raw))
        return r, next(c for c in r["checks"] if c["name"] == "consume:policy-skip")

    def test_seat_paused_and_seat_sleep_skips_are_expected_only_while_state_holds(self):
        _, row = self._skip_row("seat_paused", _seat(state="paused-intake"))
        self.assertEqual(row["status"], fh.OK)
        self.assertIn("seat paused", row["detail"])
        _, row = self._skip_row("seat_sleep", _seat(state="deep-sleep", asleep=False))
        self.assertEqual(row["status"], fh.OK)
        _, row = self._skip_row("seat_paused", _seat())
        self.assertEqual(row["status"], fh.WARN)
        _, row = self._skip_row("seat_sleep", _seat(state="paused-intake"))
        self.assertEqual(row["status"], fh.WARN)


class ChronicleTranscriptAndExport(unittest.TestCase):
    """T-6118 PR-4b: transcript ratio + export size per seat per day (--chronicle). Soft: never FAIL."""

    NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.export = Path(self._tmp.name) / "export"
        self.export.mkdir()
        env = mock.patch.dict(os.environ, {"PANTHEON_CHRONICLE_EXPORT_ROOT": str(self.export)})
        env.start()
        self.addCleanup(env.stop)

    def _file(self, seat, date, name, size):
        d = self.export / seat / date
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(b"x" * size)

    @staticmethod
    def _cov_run(ratio_by_persona):
        seen = []

        def run(user, inner, timeout=15):
            seen.append((user, inner))
            persona = inner.split("--persona ")[1].split()[0]
            if persona not in ratio_by_persona:
                return (None, "", False)
            out = ratio_by_persona[persona]
            return (0, out if isinstance(out, str) else json.dumps(out), True)
        run.seen = seen
        return run

    def _cov(self, ratio, eligible=15, with_t=14, hollow=3):
        status = "OK" if ratio is None or ratio >= 0.9 else "FAIL"
        return {"status": status, "ratio": ratio, "eligible": eligible, "with_transcript": with_t,
                "hollow": hollow, "detail": "d", "threshold": 0.9}

    def test_ratio_ok_and_below_threshold_is_warn_not_fail(self):
        run = self._cov_run({"recovery": self._cov(0.9333), "specialist": self._cov(0.5, 10, 5), "artisan": "garbage"})
        recs = fh.transcript_coverage_sweep(["recovery", "specialist", "artisan", "watchman"], run=run)
        self.assertEqual([r["status"] for r in recs], [fh.OK, fh.WARN, fh.WARN, fh.WARN])
        self.assertEqual(recs[0]["ratio"], 0.9333)
        self.assertEqual(run.seen[0][0], fh.FLEET["recovery"]["user"])
        self.assertIn("transcript-coverage --persona recovery --days 7 --json", run.seen[0][1])
        self.assertIn("could not query", recs[3]["detail"])

    def test_null_ratio_is_ok(self):
        recs = fh.transcript_coverage_sweep(["recovery"], run=self._cov_run({"recovery": self._cov(None, 0, 0, 2)}))
        self.assertEqual(recs[0]["status"], fh.OK)

    def test_export_sizes_per_seat_per_day_and_codex_maps_to_consultant(self):
        self._file("recovery", "2026-11-04", "a.md", 100)
        self._file("recovery", "2026-11-04", "a.transcript.jsonl.gz", 2000)
        self._file("recovery", "2026-11-03", "b.md", 7)
        self._file("recovery", "2026-10-01", "old.md", 999)          # outside window
        self._file("consultant", "2026-11-04", "c.md", 50)
        self._file("codex", "2026-11-04", "wrong.md", 5000)      # not the export seat for codex
        (self.export / ".ingested" / "recovery" / "2026-11-04").mkdir(parents=True)
        (self.export / ".ingested" / "recovery" / "2026-11-04" / "r.receipt.json").write_bytes(b"y" * 9)
        os.symlink("/etc/hostname", self.export / "recovery" / "2026-11-04" / "link.md")
        sizes, note = fh.export_size_sweep(["recovery", "codex", "specialist"], now=self.NOW)
        self.assertIsNone(note)
        self.assertEqual(sizes["recovery"], {"2026-11-04": 2100, "2026-11-03": 7})
        self.assertEqual(sizes["consultant"], {"2026-11-04": 50})
        self.assertNotIn("codex", sizes)
        self.assertEqual(sizes["specialist"], {})

    def test_symlinked_seat_and_date_dirs_are_never_followed(self):
        big = Path(tempfile.mkdtemp(dir=self.export.parent))
        self.addCleanup(lambda: __import__("shutil").rmtree(big, ignore_errors=True))
        (big / "2026-11-04").mkdir()
        (big / "2026-11-04" / "huge.md").write_bytes(b"x" * 5000)
        (big / "huge.md").write_bytes(b"x" * 5000)
        os.symlink(big, self.export / "consultant")                    # symlinked SEAT dir (consultant = codex's seat)
        (self.export / "recovery").mkdir(exist_ok=True)
        os.symlink(big / "2026-11-04", self.export / "recovery" / "2026-11-04")  # symlinked DATE dir
        self._file("recovery", "2026-11-03", "real.md", 7)
        sizes, note = fh.export_size_sweep(["recovery", "codex"], now=self.NOW)
        self.assertIsNone(note)
        self.assertEqual(sizes["consultant"], {})
        self.assertEqual(sizes["recovery"], {"2026-11-03": 7})

    def test_missing_or_unreadable_tree_is_a_note_not_a_crash(self):
        with mock.patch.dict(os.environ, {"PANTHEON_CHRONICLE_EXPORT_ROOT": str(self.export / "nope")}):
            sizes, note = fh.export_size_sweep(["recovery"], now=self.NOW)
        self.assertEqual(sizes, {})
        self.assertIn("export tree unreadable/unprovisioned", note)

    def test_render_line(self):
        self._file("recovery", "2026-11-04", "a.md", 2200000)
        recs = fh.transcript_coverage_sweep(["recovery"], run=self._cov_run({"recovery": self._cov(0.9333)}))
        sizes, note = fh.export_size_sweep(["recovery"], now=self.NOW)
        text = fh.render_chronicle_export(recs, sizes, note)
        self.assertIn("chronicle-export recovery 7d ratio 0.93 (14/15, hollow 3) | 2026-11-04 2.1M", text)

    def test_main_json_adds_keys_and_exit_code_is_unchanged(self):
        self._file("recovery", "2026-11-04", "a.md", 10)
        run = self._cov_run({a: self._cov(0.1, 10, 1) for a in fh.FLEET})
        buf = io.StringIO()
        chron = [{"agent": "recovery", "status": fh.OK, "detail": "ok"}]
        with mock.patch.object(fh, "sweep", return_value=[]), mock.patch.object(fh, "_run", run), \
                mock.patch.object(fh, "chronicle_coverage_sweep", return_value=chron), \
                contextlib.redirect_stdout(buf):
            rc = fh.main(["--agent", "recovery", "--no-failed-sweep", "--chronicle", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["chronicle"], chron)
        self.assertEqual(payload["transcript_coverage"][0]["status"], fh.WARN)
        self.assertEqual(payload["export_bytes"]["recovery"], {"2026-11-04": 10})

    def test_main_text_prints_export_line(self):
        run = self._cov_run({"recovery": self._cov(0.1, 10, 1)})
        buf = io.StringIO()
        with mock.patch.object(fh, "sweep", return_value=[]), mock.patch.object(fh, "_run", run), \
                mock.patch.object(fh, "chronicle_coverage_sweep", return_value=[]), \
                contextlib.redirect_stdout(buf):
            rc = fh.main(["--agent", "recovery", "--no-failed-sweep", "--chronicle"])
        self.assertEqual(rc, 0)
        self.assertIn("chronicle-export recovery 7d ratio 0.1", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
