"""Tests for tools/fleet_reboot.py — Recovery-driven coordinated fleet reboot (T-1049).

The orchestration logic is exercised with every side effect injected: per-agent
quiesce, the reboot_safety pre-sweep, the Chronicle archive, and the reboot call.
Asserts the protocol invariants: driver quiesced last, advisory vs strict
pre-check, a failed claude quiesce withholds the reboot, codex WARN never blocks,
and reboot only fires under --execute (and not under --skip-reboot).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_reboot as fr  # noqa: E402


def _silent(*_a, **_k):
    return None


class Recorder:
    """Records the sequence of quiesce/reboot calls so order + gating can be asserted."""

    def __init__(self, *, handoff_status="OK", codex_status="OK", driver_status="OK"):
        self.calls = []
        self.handoff_status = handoff_status
        self.codex_status = codex_status
        self.driver_status = driver_status
        self.reboot_called = False
        self.chronicle_called_with = None
        self.preflight_ok = True

    def quiesce_handoff(self, agent, cfg, *, dry_run):
        self.calls.append(("handoff", agent, dry_run))
        return {"agent": agent, "status": self.handoff_status, "action": "handoff_pause"}

    def quiesce_repo(self, agent, cfg, *, dry_run, stop_services):
        self.calls.append(("repo", agent, dry_run, stop_services))
        return {"agent": agent, "status": self.codex_status, "action": "repo_clean"}

    def quiesce_drv(self, agent, cfg, *, chronicle_archive, dry_run):
        self.calls.append(("driver", agent, dry_run))
        chronicle_archive(cfg["handoff_file"])  # exercise the injected archive
        return {"agent": agent, "status": self.driver_status, "action": "driver"}

    def chronicle_archive(self, handoff_file):
        self.chronicle_called_with = handoff_file
        return True, "archived"

    def reboot_preflight(self, *, dry_run):
        self.calls.append(("preflight", dry_run))
        return {"status": "OK" if self.preflight_ok else "FAIL", "detail": "root route probe"}

    def reboot(self, *, dry_run):
        self.reboot_called = True
        self.calls.append(("reboot", dry_run))
        return {"status": "OK", "action": "reboot"}


def _clean_sweep():
    repos = [
        {"agent": agent, "status": "OK", "repo": f"/{agent}"}
        for agent in fr.FLEET
    ]
    continuity = [
        {"agent": agent, "status": "OK", "continuity": f"{agent}:0"}
        for agent in fr.FLEET
    ]
    return repos + continuity


def _dirty_sweep():
    return [{"agent": "orchestrator", "status": "FAIL", "repo": "/y"}]


def _orchestrate(rec, **kw):
    defaults = dict(
        execute=False, strict=False, skip_reboot=False, stop_codex_services=False,
        pre_sweep=_clean_sweep,
        quiesce_handoff=rec.quiesce_handoff,
        quiesce_repo=rec.quiesce_repo,
        quiesce_drv=rec.quiesce_drv,
        chronicle_archive=rec.chronicle_archive,
        reboot=rec.reboot,
        reboot_preflight=rec.reboot_preflight,
        log=_silent,
    )
    defaults.update(kw)
    return fr.orchestrate(**defaults)


class TestOrder(unittest.TestCase):
    def test_driver_quiesced_last(self):
        rec = Recorder()
        _orchestrate(rec)
        quiesce_agents = [c[1] for c in rec.calls if c[0] in ("handoff", "repo", "driver")]
        self.assertEqual(quiesce_agents[-1], "recovery")
        # recovery must be the only "driver"-strategy call
        self.assertEqual([c for c in rec.calls if c[0] == "driver"][0][1], "recovery")

    def test_all_agents_quiesced(self):
        rec = Recorder()
        _orchestrate(rec)
        agents = {c[1] for c in rec.calls if c[0] in ("handoff", "repo", "driver")}
        self.assertEqual(agents, {
            "orchestrator", "specialist", "codex", "recovery",
            "archivist", "mason", "artisan", "watchman",
        })

    def test_driver_archives_handoff(self):
        rec = Recorder()
        _orchestrate(rec)
        self.assertEqual(rec.chronicle_called_with, fr.FLEET["recovery"]["handoff_file"])


class TestDryRunVsExecute(unittest.TestCase):
    def test_dry_run_passes_dry_true_and_reboots_in_dry(self):
        rec = Recorder()
        report = _orchestrate(rec, execute=False)
        # every quiesce call sees dry_run=True
        for c in rec.calls:
            if c[0] in ("handoff", "repo", "driver"):
                self.assertTrue(c[-1] if c[0] != "repo" else c[2])
        # reboot() still invoked but with dry_run=True
        self.assertTrue(rec.reboot_called)
        self.assertEqual(report["exit"], 0)

    def test_execute_passes_dry_false(self):
        rec = Recorder()
        _orchestrate(rec, execute=True)
        handoff_calls = [c for c in rec.calls if c[0] == "handoff"]
        self.assertTrue(all(c[2] is False for c in handoff_calls))

    def test_skip_reboot_does_not_reboot(self):
        rec = Recorder()
        report = _orchestrate(rec, execute=True, skip_reboot=True)
        self.assertFalse(rec.reboot_called)
        self.assertEqual(report["exit"], 0)


class TestPreCheck(unittest.TestCase):
    def test_strict_import_unavailable_aborts_before_quiesce_or_reboot(self):
        from unittest.mock import patch
        rec = Recorder()
        with patch.object(fr, "reboot_safety", None):
            report = _orchestrate(rec, strict=True, pre_sweep=None)
        self.assertEqual(report["exit"], 2)
        self.assertFalse(report["pre_check"]["clean"])
        self.assertEqual(rec.calls, [])
        self.assertFalse(rec.reboot_called)

    def test_advisory_dirty_continues(self):
        rec = Recorder()
        report = _orchestrate(rec, pre_sweep=_dirty_sweep, strict=False)
        self.assertEqual(report["exit"], 0)
        self.assertTrue(rec.reboot_called)  # advisory: dirty doesn't stop us

    def test_strict_dirty_aborts(self):
        rec = Recorder()
        report = _orchestrate(rec, pre_sweep=_dirty_sweep, strict=True)
        self.assertEqual(report["exit"], 2)
        self.assertFalse(rec.reboot_called)
        # no quiesce should have run either
        self.assertEqual(rec.calls, [])

    def test_strict_clean_proceeds(self):
        rec = Recorder()
        report = _orchestrate(rec, pre_sweep=_clean_sweep, strict=True)
        self.assertEqual(report["exit"], 0)
        self.assertTrue(rec.reboot_called)


class TestQuiesceGating(unittest.TestCase):
    def test_handoff_fail_withholds_reboot(self):
        rec = Recorder(handoff_status="FAIL")
        report = _orchestrate(rec, execute=True)
        self.assertEqual(report["exit"], 1)
        self.assertFalse(rec.reboot_called)

    def test_handoff_fail_stops_before_driver(self):
        # orchestrator fails first -> specialist/codex/recovery never quiesced, reboot withheld
        rec = Recorder(handoff_status="FAIL")
        _orchestrate(rec, execute=True)
        agents = [c[1] for c in rec.calls if c[0] in ("handoff", "repo", "driver")]
        self.assertEqual(agents, ["orchestrator"])

    def test_codex_warn_never_blocks(self):
        rec = Recorder(codex_status="WARN")
        report = _orchestrate(rec, execute=True)
        self.assertEqual(report["exit"], 0)
        self.assertTrue(rec.reboot_called)


class TestCodexServices(unittest.TestCase):
    def test_stop_codex_services_flag_propagates(self):
        rec = Recorder()
        _orchestrate(rec, stop_codex_services=True)
        repo_calls = [c for c in rec.calls if c[0] == "repo"]
        self.assertTrue(repo_calls[0][3])  # stop_services=True passed through

    def test_default_does_not_stop_codex_services(self):
        rec = Recorder()
        _orchestrate(rec, stop_codex_services=False)
        repo_calls = [c for c in rec.calls if c[0] == "repo"]
        self.assertFalse(repo_calls[0][3])


class TestRegistry(unittest.TestCase):
    EXPECTED = {
        "orchestrator", "specialist", "codex", "recovery",
        "archivist", "mason", "artisan", "watchman",
    }

    def test_registry_covers_every_current_control_plane_seat(self):
        self.assertEqual(set(fr.FLEET), self.EXPECTED)
        self.assertEqual(set(fr.QUIESCE_ORDER), self.EXPECTED)

    def test_quiesce_order_is_exact_and_has_no_duplicates(self):
        self.assertEqual(fr.QUIESCE_ORDER, [
            "orchestrator", "specialist", "codex", "archivist", "mason", "artisan",
            "watchman", "recovery",
        ])
        self.assertEqual(len(fr.QUIESCE_ORDER), len(set(fr.QUIESCE_ORDER)))

    def test_post_boot_health_covers_same_fleet(self):
        self.assertIsNotNone(fr.fleet_health)
        self.assertEqual(set(fr.fleet_health.FLEET), self.EXPECTED)

    def test_quiesce_order_driver_last(self):
        self.assertEqual(fr.QUIESCE_ORDER[-1], fr.DRIVER)

    def test_every_quiesce_agent_in_fleet(self):
        for a in fr.QUIESCE_ORDER:
            self.assertIn(a, fr.FLEET)

    def test_run_pre_check_clean(self):
        clean, results = fr.run_pre_check(sweep=_clean_sweep)
        self.assertTrue(clean)

    def test_run_pre_check_dirty(self):
        clean, results = fr.run_pre_check(sweep=_dirty_sweep)
        self.assertFalse(clean)

    def test_run_pre_check_warn_is_still_clean(self):
        results = _clean_sweep()
        results[0]["status"] = "WARN"
        clean, _ = fr.run_pre_check(sweep=lambda: results)
        self.assertTrue(clean)

    def test_missing_seat_result_is_fail_closed(self):
        incomplete = [
            {"agent": agent, "status": "OK", "repo": f"/{agent}"}
            for agent in self.EXPECTED - {"mason"}
        ] + [
            {"agent": agent, "status": "OK", "continuity": f"{agent}:0"}
            for agent in self.EXPECTED
        ]
        clean, results = fr.run_pre_check(sweep=lambda: incomplete)
        self.assertFalse(clean)
        self.assertTrue(any(r["agent"] == "mason" and r["status"] == fr.FAIL
                            for r in results))

    def test_missing_continuity_result_is_fail_closed(self):
        incomplete = [r for r in _clean_sweep()
                      if not (r["agent"] == "artisan" and "continuity" in r)]
        clean, results = fr.run_pre_check(sweep=lambda: incomplete)
        self.assertFalse(clean)
        self.assertTrue(any(r["agent"] == "artisan" and r["status"] == fr.FAIL
                            for r in results))


class TestHandoffPauseIdentity(unittest.TestCase):
    """T-4159: the pause call names the seat by its instance.yaml restart identity,
    so a control-plane seat never gets the retired bespoke unit."""

    def _argv(self, agent, target=None, raises=False):
        from unittest.mock import patch
        seen = {}
        def run(argv, timeout=900):
            seen["argv"] = argv
            return 0, ""
        fake = type("ARM", (), {})()
        if raises:
            def rt(_p): raise KeyError(_p)
        else:
            def rt(_p): return target
        fake.restart_target = rt
        with patch.object(fr, "agent_role_map", fake):
            fr.quiesce_handoff_pause(agent, fr.FLEET[agent], run=run, dry_run=False)
        return seen["argv"][-1]

    def test_control_plane_seat_gets_consume_unit_and_flag(self):
        inner = self._argv("orchestrator", {"service": "pantheon-persona-consume@orchestrator.service",
                                     "supervisor": "control-plane"})
        self.assertIn("--service pantheon-persona-consume@orchestrator.service", inner)
        self.assertIn("--supervisor control-plane", inner)
        self.assertNotIn("claude-telegram.service", inner)

    def test_bespoke_seat_unchanged(self):
        inner = self._argv("orchestrator", {"service": "claude-telegram.service", "supervisor": "bespoke"})
        self.assertIn("--service claude-telegram.service", inner)
        self.assertIn("--supervisor bespoke", inner)

    def test_resolution_failure_falls_back_to_static_entry(self):
        inner = self._argv("orchestrator", raises=True)
        self.assertIn(f"--service {fr.FLEET['orchestrator']['service']}", inner)
        self.assertIn("--supervisor bespoke", inner)

    def test_each_new_seat_uses_its_own_control_plane_identity_and_handoff(self):
        expected = {
            "archivist": ("pantheon-persona-consume@archivist.service",
                           "claude-telegram-bot-exocortex:0", "/home/exocortex/HANDOFF.md"),
            "mason": ("pantheon-persona-consume@mason.service",
                      "suborch1-main:0", "/home/suborch1/HANDOFF.md"),
            "artisan": ("pantheon-persona-consume@artisan.service",
                      "suborch2-main:0", "/home/suborch2/HANDOFF.md"),
            "watchman": ("pantheon-persona-consume@watchman.service",
                         "codex-fallback-main:0", "/home/codex-fallback/HANDOFF.md"),
        }
        for agent, (service, pane, handoff) in expected.items():
            with self.subTest(agent=agent):
                inner = self._argv(agent, {"service": service, "supervisor": "control-plane"})
                self.assertIn(f"--service {service}", inner)
                self.assertIn("--supervisor control-plane", inner)
                self.assertIn(f"--target-pane {pane}", inner)
                self.assertIn(f"--handoff-file {handoff}", inner)


class TestT6288RootRouteAndSeatPaths(unittest.TestCase):
    """T-6288: Recovery (the driver) has no root and other seats cannot read Recovery's clone."""

    def _quiesce_inner(self, agent):
        seen = {}
        def run(argv, timeout=900):
            seen["argv"] = argv
            return 0, ""
        fr.quiesce_handoff_pause(agent, fr.FLEET[agent], run=run, dry_run=False)
        return seen["argv"]

    def test_each_seat_runs_its_own_clone_never_the_drivers(self):
        for agent, cfg in fr.FLEET.items():
            if cfg["strategy"] != "handoff_pause":
                continue
            with self.subTest(agent=agent):
                argv = self._quiesce_inner(agent)
                self.assertEqual(argv[:4], ["sudo", "-n", "-u", cfg["user"]])
                self.assertNotIn("/home/claude-fallback/", argv[-1])
                self.assertIn("$HOME/pantheon-config/tools/handoff_and_restart.sh", argv[-1])

    def test_reboot_goes_through_pantheon_sudo_on_the_claude_seat(self):
        seen = {}
        def run(argv, timeout=900):
            seen["argv"] = argv
            return 0, ""
        fr.trigger_reboot(run=run, dry_run=False)
        argv = seen["argv"]
        self.assertNotEqual(argv[:3], ["sudo", "-n", "systemctl"])
        self.assertEqual(argv[:6], ["sudo", "-n", "-u", "claude", "bash", "-lc"])  # T-6297
        self.assertIn("pantheon-config/tools/pantheon-sudo systemctl reboot", argv[-1])
        self.assertIn("</dev/null", argv[-1])  # pantheon-sudo refuses piped stdin (T-5713)

    def test_failed_root_preflight_quiesces_nobody_and_never_reboots(self):
        rec = Recorder()
        rec.preflight_ok = False
        report = _orchestrate(rec, execute=True)
        self.assertEqual(report["exit"], 3)
        self.assertEqual([c[0] for c in rec.calls], ["preflight"])
        self.assertFalse(rec.reboot_called)

    def test_preflight_runs_before_any_quiesce(self):
        rec = Recorder()
        _orchestrate(rec, execute=True)
        self.assertEqual(rec.calls[0][0], "preflight")

    def test_skip_reboot_needs_no_root_preflight(self):
        rec = Recorder()
        rec.preflight_ok = False
        report = _orchestrate(rec, execute=True, skip_reboot=True)
        self.assertEqual(report["exit"], 0)
        self.assertNotIn("preflight", [c[0] for c in rec.calls])


class TestCLI(unittest.TestCase):
    def test_cli_strict_import_unavailable_exits_2(self):
        from unittest.mock import patch
        with patch.object(fr, "reboot_safety", None):
            self.assertEqual(fr.main(["--strict"]), 2)

    def test_cli_dry_run_default_exit_0(self):
        # Real side-effect funcs but dry_run=True everywhere -> no live ops.
        # Inject a clean sweep via monkeypatching reboot_safety.sweep is overkill;
        # instead drive orchestrate directly is covered above. Here just ensure the
        # CLI parses and returns an int in dry-run with the real (no-op in dry) path
        # but a stubbed pre-sweep through orchestrate is not reachable from main();
        # so we only assert main() returns 0 when nothing dirty by stubbing module.
        orig = fr.reboot_safety
        try:
            class _Stub:
                @staticmethod
                def sweep():
                    return _clean_sweep()
            fr.reboot_safety = _Stub
            # Replace live side-effects so the dry run touches nothing real.
            rc = fr.main([])
        finally:
            fr.reboot_safety = orig
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
