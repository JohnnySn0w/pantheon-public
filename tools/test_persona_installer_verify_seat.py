"""T-5449: ``verify-seat`` -- the read-only post-install check.

Fixture tree under a tmp root plus a fake command runner. Nothing here touches a
real home, user manager, tmux, or a live seat.
"""

from __future__ import annotations

import copy
import io
import json
import subprocess
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools import persona_installer as _pim
from tools import persona_installer_apply as pia
from tools import persona_installer_verify_seat as vs
from tools.test_persona_installer_periodics import PeriodicCase

UID = 4242
GOOD_SHOW = "ActiveState=active\nSubState=running\nMainPID=4242\nNRestarts=0\n"
CONTROLLER = "pantheon-runtime-controller@recovery.service"
PANE = "pantheon-recovery:harness.0"
T0 = "2026-10-24T18:00:00Z"
T_ATTACH = "2026-10-24T18:00:05.123456Z"


def _result(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(list(argv), rc, out, err)


class VerifyCase(PeriodicCase):
    def setUp(self) -> None:
        super().setUp()
        self.plan_ = self.plan()
        self.state_root = Path(
            next(o for o in self.plan_.operations if o.operation_id == "service.runtime-controller.enable-observe")
            .desired["persona_state_root"]
        )
        self.commands: list[tuple[str, ...]] = []
        self.enabled = {
            o.target for o in self.plan_.operations
            if o.kind in {"timer-enable", "seat-periodic-timer-enable", "seat-service-enable-observe"}
        }
        self.active: set[str] = set()
        self.show = GOOD_SHOW
        self.show_rc = 0
        self.pane_pid: str | None = "555"
        self.install_units()
        self.install_containment()
        self.write_receipt(pre_start="555")
        self.write_evidence([("controller_start_attached_live_runtime", T_ATTACH, 555)])
        self.binding = types.SimpleNamespace(
            controller=types.SimpleNamespace(
                mid_session_cap_swap_enabled=True,
                fallback_return_enabled=True,
                policy=types.SimpleNamespace(fallbacks=("codex/x",)),
            ),
        )
        self.viability_ok = True

    # -- fixture helpers ------------------------------------------------
    def install_units(self) -> None:
        for op in self.plan_.operations:
            if op.kind in {"user-unit-install", "seat-periodic-unit-install"}:
                path = Path(op.target)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(_pim._desired_content(op))

    def install_containment(self) -> None:
        """T-5454: a wired seat (settings key, executable wrapper, reachable manager)."""
        op = next(o for o in self.plan_.operations if o.kind == _pim.SETTINGS_ENV_KIND)
        settings, script = Path(op.target), Path(op.desired["env_value"])
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"env": {op.desired["env_key"]: str(script)}}))
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        xdg = self.root / "xdg"
        (xdg / "systemd").mkdir(parents=True, exist_ok=True)
        (xdg / "systemd" / "private").write_text("")
        self.env = {"XDG_RUNTIME_DIR": str(xdg)}

    def write_receipt(self, *, pre_start: str | None, created_at: str = T0, phase: str = "complete") -> None:
        directory = Path(self.plan_.provenance["receipt_dir"])
        directory.mkdir(parents=True, exist_ok=True)
        evidence = {}
        if pre_start is not None:
            evidence["controller_pre_start_pane_pid"] = pre_start
        receipt = {
            "schema": _pim.RECEIPT_SCHEMA, "receipt_id": "r-" + created_at, "phase": phase,
            "plan_digest": self.plan_.digest, "instance": self.plan_.instance, "persona": "recovery",
            "role": self.plan_.role, "account": self.plan_.account, "actor": {}, "mutations": [],
            "evidence": [{
                "operation_id": "service.runtime-controller.enable-observe", "layer": 1,
                "kind": "seat-service-enable-observe", "ok": True, "detail": "ok", "evidence": evidence,
            }],
            "created_at": created_at,
        }
        (directory / f"r-{created_at}.json").write_text(json.dumps(receipt))
        (directory / f"r-{created_at}.plan.json").write_text("{}")  # sidecar must be ignored

    def write_evidence(self, rows) -> None:
        path = self.state_root / "recovery" / "runtime-controller" / "controller-evidence.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(
            json.dumps({"schema": "pantheon-runtime-controller-evidence/v1", "event": e,
                        "persona": "recovery", "recorded_at": t, "pid": pid}) + "\n"
            for e, t, pid in rows))

    def runner(self, argv):
        argv = tuple(argv)
        self.commands.append(argv)
        if argv[:3] == ("systemctl", "--user", "is-enabled"):
            ok = argv[3] in self.enabled
            return _result(argv, 0 if ok else 1, "enabled\n" if ok else "disabled\n")
        if argv[:3] == ("systemctl", "--user", "is-active"):
            ok = argv[3] in self.active
            return _result(argv, 0 if ok else 3, "active\n" if ok else "inactive\n")
        if argv[:3] == ("systemctl", "--user", "show"):
            return _result(argv, self.show_rc, self.show, "boom" if self.show_rc else "")
        if argv[:2] == ("tmux", "display-message"):
            if self.pane_pid is None:
                return _result(argv, 1, "", "can't find pane")
            return _result(argv, 0, f"pantheon-recovery\tharness\t0\t{self.pane_pid}\n")
        raise AssertionError(f"unexpected command {argv}")

    def hermetic(self) -> dict:
        """T-5454: the containment section reads env and PATH. EVERY verify_seat call in
        this file goes through these seams (a healthy user manager under a tmp
        XDG_RUNTIME_DIR and a stub ``which``), so no test reads the host's real
        XDG_RUNTIME_DIR, systemd socket or PATH. A CI runner has no user manager."""
        return {"env": self.env, "which": lambda name: "/usr/bin/systemd-run"}

    def verify(self, persona="recovery"):
        with mock.patch.object(vs.runtime_controller, "validate_initial_viability_state",
                               side_effect=self._viability), \
                mock.patch.object(vs.persona_bringup, "load_binding", return_value=self.binding), \
                mock.patch.object(vs.persona_bringup, "quota_check_supported",
                                  return_value=(True, "supported")):
            return vs.verify_seat(
                self.manifest_path, persona, root=self.root / "projection", run=self.runner,
                repo_root=pia.REPO_ROOT, euid=UID, uid_of=lambda account: UID,
                **self.hermetic(),
            )

    def _viability(self, persona, **kwargs):
        self.viability_kwargs = kwargs
        if not self.viability_ok:
            raise ValueError("no initial-viability.json")
        return {"ok": True}

    def checks(self, report, section):
        return {c["name"]: c for c in report["sections"][section]}

    def failed(self, report):
        return {f"{c['section']}:{c['name']}" for c in report["failed"]}


class HealthySeatTests(VerifyCase):
    def test_a_fully_installed_seat_passes_every_section(self) -> None:
        report = self.verify()
        self.assertTrue(report["ok"], report["failed"])
        self.assertEqual(
            set(report["sections"]),
            {"units", "enablement", "ho-resumer", "controller", "viability", "attach", "comms", "containment", "flip"},
        )
        self.assertEqual(report["persona"], "recovery")
        self.assertEqual(report["failed"], [])

    def test_a_missing_user_manager_fails_the_seat_through_verify_seat(self) -> None:
        # T-5454: the check stays meaningful end to end, not only in the unit tests
        for label, env, which in (
            ("xdg-unset", {}, lambda name: "/usr/bin/systemd-run"),
            ("no-private-socket", {"XDG_RUNTIME_DIR": str(self.root / "nowhere")},
             lambda name: "/usr/bin/systemd-run"),
            ("no-systemd-run", self.env, lambda name: None),
        ):
            with self.subTest(case=label):
                with mock.patch.object(vs.runtime_controller, "validate_initial_viability_state",
                                       side_effect=self._viability), \
                        mock.patch.object(vs.persona_bringup, "load_binding", return_value=self.binding), \
                        mock.patch.object(vs.persona_bringup, "quota_check_supported",
                                          return_value=(True, "supported")):
                    report = vs.verify_seat(
                        self.manifest_path, "recovery", root=self.root / "projection", run=self.runner,
                        repo_root=pia.REPO_ROOT, euid=UID, uid_of=lambda account: UID,
                        env=env, which=which,
                    )
                self.assertEqual(self.failed(report), {"containment:user-manager"})

    def test_unwired_containment_fails_the_seat_in_its_own_section(self) -> None:
        # T-5454
        Path(next(o for o in self.plan_.operations if o.kind == _pim.SETTINGS_ENV_KIND).target).unlink()
        report = self.verify()
        self.assertFalse(report["ok"])
        self.assertEqual(self.failed(report), {"containment:settings-key"})

    def test_expected_primary_runtime_and_provenance_flag_reach_the_viability_check(self) -> None:
        self.verify()
        self.assertEqual(self.viability_kwargs["require_source_provenance"], True)
        self.assertRegex(self.viability_kwargs["expect_selected"], r"^[a-z0-9-]+/[a-z0-9-]+$")
        self.assertEqual(self.viability_kwargs["state_root"], self.state_root)

    def test_only_the_closed_read_only_command_set_is_issued(self) -> None:
        self.verify()
        self.assertTrue(self.commands)
        allowed = {("systemctl", "--user", "is-enabled"), ("systemctl", "--user", "is-active"),
                   ("systemctl", "--user", "show")}
        for argv in self.commands:
            self.assertTrue(argv[:3] in allowed or argv[:2] == ("tmux", "display-message"), argv)

    def test_it_refuses_to_run_as_another_account(self) -> None:
        with self.assertRaises(pia.ApplyRefused):
            vs.verify_seat(self.manifest_path, "recovery", root=self.root / "projection", run=self.runner,
                           repo_root=pia.REPO_ROOT, euid=UID + 1, uid_of=lambda account: UID,
                           **self.hermetic())


class UnitChecks(VerifyCase):
    def test_missing_unit_fails(self) -> None:
        Path(next(o.target for o in self.plan_.operations
                  if o.operation_id == "periodic.keep-going.timer-unit")).unlink()
        report = self.verify()
        self.assertIn("units:periodic.keep-going.timer-unit", self.failed(report))

    def test_missing_banner_fails_but_a_different_revision_in_the_banner_passes(self) -> None:
        target = Path(next(o.target for o in self.plan_.operations
                           if o.operation_id == "periodic.inbox-consumer.timer-unit"))
        lines = target.read_text().splitlines(keepends=True)
        target.write_text(lines[0] + "# instance=x role=y persona=recovery harness=claude revision=OLDER\n"
                          + "".join(lines[2:]))
        self.assertTrue(self.verify()["ok"])
        target.write_text("".join(lines[2:]))  # banner stripped
        self.assertIn("units:periodic.inbox-consumer.timer-unit", self.failed(self.verify()))

    def test_drifted_body_fails(self) -> None:
        target = Path(next(o.target for o in self.plan_.operations
                           if o.operation_id == "periodic.restart-drain.dropin"))
        target.write_text(target.read_text() + "Environment=EXTRA=1\n")
        self.assertIn("units:periodic.restart-drain.dropin", self.failed(self.verify()))

    def test_every_planned_unit_family_is_checked(self) -> None:
        names = set(self.checks(self.verify(), "units"))
        for needle in ("connective.service-unit", "connective.timer-unit", "quota.timer-unit",
                       "periodic.inbox-consumer.timer-unit", "periodic.keep-going.timer-unit",
                       "periodic.restart-drain.timer-unit", "periodic.restart-drain.dropin",
                       "periodic.unit-failure-alert.service-unit", "service.runtime-controller.unit"):
            self.assertIn(needle, names)

    def test_not_enabled_timer_or_controller_fails(self) -> None:
        for unit in ("pantheon-persona-quota@recovery.timer", CONTROLLER, "pantheon-persona-consume@recovery.timer"):
            with self.subTest(unit=unit):
                self.enabled.discard(unit)
                self.assertIn(f"enablement:{unit}", self.failed(self.verify()))
                self.enabled.add(unit)


class ResumerAndControllerChecks(VerifyCase):
    def test_resumer_enabled_or_active_fails_absent_or_disabled_passes(self) -> None:
        self.assertTrue(self.verify()["ok"])
        self.enabled.add(_pim.HO_RESUMER_TIMER_UNIT)
        self.assertIn(f"ho-resumer:{_pim.HO_RESUMER_TIMER_UNIT}", self.failed(self.verify()))
        self.enabled.discard(_pim.HO_RESUMER_TIMER_UNIT)
        self.active.add(_pim.HO_RESUMER_SERVICE_UNIT)
        self.assertIn(f"ho-resumer:{_pim.HO_RESUMER_SERVICE_UNIT}", self.failed(self.verify()))

    def test_controller_with_earlier_restarts_passes_and_reports_them(self) -> None:
        # T-5478: a long-lived adopted controller may carry restarts from earlier in its life
        self.show = GOOD_SHOW.replace("NRestarts=0", "NRestarts=2")
        report = self.verify()
        self.assertNotIn("controller:controller-state", self.failed(report))
        detail = json.loads(report["sections"]["controller"][0]["detail"])
        self.assertEqual(detail["NRestarts"], "2")

    def test_controller_must_be_active_running_with_a_pid(self) -> None:
        for bad in (GOOD_SHOW.replace("MainPID=4242", "MainPID=0"),
                    GOOD_SHOW.replace("running", "auto-restart"),
                    GOOD_SHOW.replace("ActiveState=active", "ActiveState=failed"),
                    "garbage"):
            with self.subTest(show=bad):
                self.show = bad
                self.assertIn("controller:controller-state", self.failed(self.verify()))

    def test_controller_show_argv_is_pinned(self) -> None:
        # T-5517: the typed read must emit the exact argv the private show emitted
        self.verify()
        shows = [c for c in self.commands if c[:3] == ("systemctl", "--user", "show")]
        self.assertEqual(shows, [(
            "systemctl", "--user", "show", CONTROLLER, "-p", "ActiveState", "-p", "SubState",
            "-p", "MainPID", "-p", "NRestarts",
        )])

    def test_malformed_controller_output_fails_the_check_with_a_reason(self) -> None:
        for bad in (
            "",
            "ActiveState=active\nSubState=running\nMainPID=4242\n",
            GOOD_SHOW + "NRestarts=0\n",
            GOOD_SHOW + "Extra=1\n",
            GOOD_SHOW.replace("4242", "abc"),
            GOOD_SHOW.replace("4242", "04242"),
            GOOD_SHOW.replace("NRestarts=0", "NRestarts="),
            "garbled line\n",
        ):
            with self.subTest(show=bad):
                self.show = bad
                report = self.verify()
                self.assertIn("controller:controller-state", self.failed(report))
                self.assertTrue(report["sections"]["controller"][0]["detail"])

    def test_controller_show_nonzero_exit_fails_with_stderr(self) -> None:
        self.show, self.show_rc = "", 1
        report = self.verify()
        self.assertIn("controller:controller-state", self.failed(report))
        self.assertIn("boom", report["sections"]["controller"][0]["detail"])

    def test_viability_failure_is_reported_not_raised(self) -> None:
        self.viability_ok = False
        report = self.verify()
        self.assertIn("viability:initial-viability", self.failed(report))
        self.assertIn("no initial-viability.json", json.dumps(report))


class AttachChecks(VerifyCase):
    def test_no_complete_receipt_fails(self) -> None:
        for path in Path(self.plan_.provenance["receipt_dir"]).glob("*"):
            path.unlink()
        self.assertIn("attach:install-receipt", self.failed(self.verify()))

    def test_attach_line_must_come_after_the_receipt(self) -> None:
        self.write_evidence([("controller_start_attached_live_runtime", "2026-10-24T17:59:59Z", 555)])
        self.assertIn("attach:attach-line", self.failed(self.verify()))

    def test_respawn_instead_of_attach_fails(self) -> None:
        self.write_evidence([("controller_start_respawn_over_foreign_pane", T_ATTACH, 555)])
        self.assertIn("attach:attach-line", self.failed(self.verify()))

    def test_pane_pid_unchanged_across_the_start_is_three_way_equal(self) -> None:
        self.assertTrue(self.verify()["ok"])
        # live pane pid differs from the attach line: pane replaced since the attach
        self.pane_pid = "999"
        self.assertIn("attach:attach-pid-matches-live-pane", self.failed(self.verify()))
        self.pane_pid = None
        self.assertIn("attach:attach-pid-matches-live-pane", self.failed(self.verify()))

    def test_attach_pid_must_equal_the_recorded_pre_start_pid(self) -> None:
        self.write_receipt(pre_start="111")  # pane before start was 111, attach says 555
        self.assertIn("attach:attach-pid-matches-pre-start", self.failed(self.verify()))

    def test_absent_or_missing_pre_start_record_fails_with_a_clear_reason(self) -> None:
        for pre in ("absent", None):
            with self.subTest(pre=pre):
                self.write_receipt(pre_start=pre)
                report = self.verify()
                check = self.checks(report, "attach")["attach-pid-matches-pre-start"]
                self.assertFalse(check["ok"])
                self.assertIn("pre-start", check["detail"])

    # -- T-5479: controller-launch witness (live seat converged, then restarted) --
    def launch_shape(self, *, launch_at="2026-10-24T18:00:07Z", pid=555, started="2026-10-24T18:00:06Z",
                     argv=None) -> None:
        """No attach line; the controller launched the runtime after the receipt."""
        path = self.state_root / "recovery" / "runtime-controller" / "controller-evidence.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": "pantheon-runtime-controller-evidence/v1", "event": "post_start_command",
            "persona": "recovery", "recorded_at": launch_at, "background": "false", "outcome": "ok",
            "argv": argv or f"tools/arm_pane_log.sh recovery {PANE}",
        }) + "\n")
        if pid is not None:
            (self.state_root / "recovery" / "active-runtime.json").write_text(json.dumps(
                {"schema": "pantheon-persona-active-runtime/v1", "pid": pid, "started_ts": started}))

    def test_attach_line_shape_is_named_in_the_detail(self) -> None:
        report = self.verify()
        self.assertTrue(report["ok"])
        self.assertIn("shape=attach-line", self.checks(report, "attach")["attach-line"]["detail"])

    def test_controller_launch_shape_passes(self) -> None:
        self.launch_shape()
        report = self.verify()
        self.assertTrue(report["ok"], report["failed"])
        self.assertIn("shape=controller-launch", self.checks(report, "attach")["attach-line"]["detail"])
        self.assertTrue(self.checks(report, "attach")["attach-pid-matches-pre-start"]["ok"])

    def test_controller_launch_line_before_the_receipt_fails(self) -> None:
        self.launch_shape(launch_at="2026-10-24T17:59:59Z")
        failed = self.failed(self.verify())
        self.assertIn("attach:attach-line", failed)

    def test_controller_launch_pid_must_match_the_live_pane(self) -> None:
        self.launch_shape(pid=111)
        self.assertIn("attach:attach-pid-matches-live-pane", self.failed(self.verify()))

    def test_controller_launch_needs_a_fresh_active_runtime_record(self) -> None:
        self.launch_shape(started="2026-10-24T17:59:00Z")
        self.assertIn("attach:attach-line", self.failed(self.verify()))
        self.launch_shape(pid=None)
        self.assertIn("attach:attach-line", self.failed(self.verify()))

    def test_controller_launch_line_for_another_pane_fails(self) -> None:
        self.launch_shape(argv="tools/arm_pane_log.sh recovery pantheon-other:harness.0")
        self.assertIn("attach:attach-line", self.failed(self.verify()))

    def test_the_newest_complete_receipt_is_the_reference(self) -> None:
        self.write_receipt(pre_start="111", created_at="2026-10-24T10:00:00Z")  # older, wrong
        self.write_receipt(pre_start="555", created_at="2026-10-24T19:00:00Z", phase="failed")  # not complete
        self.assertTrue(self.verify()["ok"])


class FlipChecks(VerifyCase):
    def test_flip_failures_are_reported_in_their_own_section_and_fail_the_seat(self) -> None:
        self.binding.controller.mid_session_cap_swap_enabled = False
        report = self.verify()
        self.assertFalse(report["ok"])
        self.assertEqual(self.failed(report), {"flip:mid_session_cap_swap_enabled"})
        self.binding.controller.mid_session_cap_swap_enabled = True
        self.binding.controller.fallback_return_enabled = False
        self.assertEqual(self.failed(self.verify()), {"flip:fallback_return_enabled"})

    def test_quota_support_is_required_when_a_fallback_exists(self) -> None:
        with mock.patch.object(vs.persona_bringup, "quota_check_supported",
                               return_value=(False, "no-manifest-fallback")):
            with mock.patch.object(vs.persona_bringup, "load_binding", return_value=self.binding), \
                    mock.patch.object(vs.runtime_controller, "validate_initial_viability_state",
                                      side_effect=self._viability):
                report = vs.verify_seat(self.manifest_path, "recovery", root=self.root / "projection",
                                        run=self.runner, repo_root=pia.REPO_ROOT, euid=UID,
                                        uid_of=lambda a: UID, **self.hermetic())
        self.assertEqual(self.failed(report), {"flip:quota_check_supported"})
        self.assertIn("no-manifest-fallback", json.dumps(report))

    def test_no_manifest_fallback_means_flip_is_not_applicable(self) -> None:
        self.binding.controller.policy.fallbacks = ()
        self.binding.controller.mid_session_cap_swap_enabled = False
        report = self.verify()
        self.assertTrue(report["ok"])
        self.assertEqual([c["name"] for c in report["sections"]["flip"]], ["flip-applicable"])

    def test_flip_reads_the_effective_binding_for_real_instance_personas(self) -> None:
        # truthful against the REAL instance.yaml, whatever the schema default is
        from tools import persona_bringup

        real = pia.REPO_ROOT / "instance.yaml"
        for persona in ("recovery", "mason", "orchestrator"):
            with self.subTest(persona=persona):
                binding = persona_bringup.load_binding(real, persona)
                checks = vs.flip_checks(real, persona)
                if not binding.controller.policy.fallbacks:
                    self.assertEqual([c["name"] for c in checks], ["flip-applicable"])
                    continue
                by_name = {c["name"]: c for c in checks}
                self.assertEqual(by_name["mid_session_cap_swap_enabled"]["ok"],
                                 binding.controller.mid_session_cap_swap_enabled)
                self.assertEqual(by_name["fallback_return_enabled"]["ok"],
                                 binding.controller.fallback_return_enabled)
                self.assertEqual(by_name["quota_check_supported"]["ok"],
                                 persona_bringup.quota_check_supported(binding, manifest_path=real)[0])


class CommsChecks(VerifyCase):
    """A comms seat = persona with a codex_telegram_bridge entry. The fixture
    manifest has none for recovery, so build the bridge pieces by hand."""

    def setUp(self) -> None:
        super().setUp()
        self.release_root = self.root / "release"
        (self.release_root / "releases" / "REV1").mkdir(parents=True)
        (self.release_root / "current").symlink_to(self.release_root / "releases" / "REV1")
        self.codex_cfg = self.root / ".codex" / "config.toml"  # T-5451: shared readiness default path
        self.codex_cfg.parent.mkdir(parents=True, exist_ok=True)
        self.bridge = types.SimpleNamespace(
            codex_receiver_unit="codex-telegram-poller.service",
            current_release=self.release_root / "current",
            source=types.SimpleNamespace(revision="REV1"),
            mcp_name="pantheon_telegram",
            account_home=self.root,
        )
        controller_unit = next(o for o in self.plan_.operations if o.operation_id == "service.runtime-controller.unit")
        self.poller = Path(controller_unit.target).parent / "codex-telegram-poller.service"
        self.poller.write_text("# " + _pim.MANAGED_MARKER + "\n[Service]\n")
        self.codex_cfg.write_text("[mcp_servers.pantheon_telegram]\ncommand = \"python3\"\n")
        self.selected = "claude"

    def comms(self):
        with mock.patch.object(vs, "_bridge_for", return_value=self.bridge), \
                mock.patch.object(vs, "_selected_harness", side_effect=lambda *a, **k: self.selected), \
                mock.patch.object(vs.codex_telegram_bridge, "_expected_mcp_table", return_value={}), \
                mock.patch.object(vs.codex_telegram_bridge, "mcp_registration_compatibility",
                                  side_effect=lambda data, **k: True if "pantheon_telegram" in data.get("mcp_servers", {}) else None):
            return {c["name"]: c for c in vs.comms_checks(
                self.manifest_path, "recovery", self.plan_, self.runner, root=self.root / "projection")}

    def test_a_correct_comms_seat_passes(self) -> None:
        for check in self.comms().values():
            self.assertTrue(check["ok"], check)

    def test_poller_unit_missing_or_active_under_claude_fails(self) -> None:
        self.active.add("codex-telegram-poller.service")
        self.assertFalse(self.comms()["poller-inactive-while-claude"]["ok"])
        self.active.clear()
        self.poller.unlink()
        self.assertFalse(self.comms()["poller-unit-installed"]["ok"])

    def test_active_poller_is_fine_when_the_selected_runtime_is_codex(self) -> None:
        self.selected = "codex"
        self.active.add("codex-telegram-poller.service")
        self.assertTrue(self.comms()["poller-inactive-while-claude"]["ok"])

    def test_release_current_must_point_at_the_pinned_revision(self) -> None:
        (self.release_root / "releases" / "OTHER").mkdir()
        (self.release_root / "current").unlink()
        (self.release_root / "current").symlink_to(self.release_root / "releases" / "OTHER")
        self.assertFalse(self.comms()["release-current-pinned"]["ok"])
        (self.release_root / "current").unlink()
        self.assertFalse(self.comms()["release-current-pinned"]["ok"])

    def test_mcp_registration_must_exist(self) -> None:
        self.codex_cfg.write_text("")
        self.assertFalse(self.comms()["codex-mcp-registered"]["ok"])

    def test_a_non_comms_persona_has_an_empty_applicable_marker(self) -> None:
        names = [c["name"] for c in vs.comms_checks(
            self.manifest_path, "recovery", self.plan_, self.runner, root=self.root / "projection")]
        self.assertEqual(names, ["comms-applicable"])


class CliTests(VerifyCase):
    def test_exit_code_and_json_report(self) -> None:
        for ok, code in ((True, 0), (False, 1)):
            with self.subTest(ok=ok):
                out = io.StringIO()
                with mock.patch.object(vs, "verify_seat", return_value={"ok": ok, "failed": []}), \
                        redirect_stdout(out):
                    exit_code = _pim.main(["verify-seat", "--manifest", str(self.manifest_path),
                                           "--persona", "recovery"])
                self.assertEqual(exit_code, code)
                self.assertEqual(json.loads(out.getvalue())["ok"], ok)


if __name__ == "__main__":
    unittest.main()
