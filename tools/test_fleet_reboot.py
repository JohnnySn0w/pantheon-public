"""Tests for tools/fleet_reboot.py — Recovery-driven coordinated fleet reboot (T-1049).

The orchestration logic is exercised with every side effect injected: per-agent
quiesce, the reboot_safety pre-sweep, the graveyard archive, and the reboot call.
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
        self.graveyard_called_with = None

    def quiesce_handoff(self, agent, cfg, *, dry_run):
        self.calls.append(("handoff", agent, dry_run))
        return {"agent": agent, "status": self.handoff_status, "action": "handoff_pause"}

    def quiesce_repo(self, agent, cfg, *, dry_run, stop_services):
        self.calls.append(("repo", agent, dry_run, stop_services))
        return {"agent": agent, "status": self.codex_status, "action": "repo_clean"}

    def quiesce_drv(self, agent, cfg, *, graveyard_archive, dry_run):
        self.calls.append(("driver", agent, dry_run))
        graveyard_archive(cfg["handoff_file"])  # exercise the injected archive
        return {"agent": agent, "status": self.driver_status, "action": "driver"}

    def graveyard_archive(self, handoff_file):
        self.graveyard_called_with = handoff_file
        return True, "archived"

    def reboot(self, *, dry_run):
        self.reboot_called = True
        self.calls.append(("reboot", dry_run))
        return {"status": "OK", "action": "reboot"}


def _clean_sweep():
    return [{"agent": "recovery", "status": "OK", "repo": "/x"}]


def _dirty_sweep():
    return [{"agent": "orchestrator", "status": "FAIL", "repo": "/y"}]


def _orchestrate(rec, **kw):
    defaults = dict(
        execute=False, strict=False, skip_reboot=False, stop_codex_services=False,
        pre_sweep=_clean_sweep,
        quiesce_handoff=rec.quiesce_handoff,
        quiesce_repo=rec.quiesce_repo,
        quiesce_drv=rec.quiesce_drv,
        graveyard_archive=rec.graveyard_archive,
        reboot=rec.reboot,
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
        self.assertEqual(agents, {"orchestrator", "specialist", "codex", "recovery"})

    def test_driver_archives_handoff(self):
        rec = Recorder()
        _orchestrate(rec)
        self.assertEqual(rec.graveyard_called_with, fr.FLEET["recovery"]["handoff_file"])


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
        clean, _ = fr.run_pre_check(sweep=lambda: [{"agent": "a", "status": "WARN"}])
        self.assertTrue(clean)


class TestCLI(unittest.TestCase):
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
