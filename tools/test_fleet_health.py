"""fleet_health tests (T-1049): per-agent classification, the fleet verdict, and
main() exit codes. The `run` seam is stubbed — no live sudo/systemctl/bwrap."""

from __future__ import annotations

import unittest
from unittest import mock

from tools import fleet_health as fh


def _runner(*, svc="active", session="yes", bwrap="ok", ran=True):
    """Build a stub `run(user, inner)` that answers each check kind."""
    def _f(user, inner, timeout=15):
        if not ran:
            return (None, "", False)
        if "is-active" in inner:
            return (0, svc, True)
        if "has-session" in inner:
            return (0, session, True)
        if "bwrap" in inner:
            return (0 if bwrap == "ok" else 1, bwrap, True)
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


if __name__ == "__main__":
    unittest.main()
