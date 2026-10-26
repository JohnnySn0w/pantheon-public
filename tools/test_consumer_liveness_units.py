"""Tests for the unit-list generalization of tools/consumer_liveness_monitor.py (T-4220).

Run from repo root:
    python3 -m unittest tools.test_consumer_liveness_units -v

WHY. The monitor's signal (`now - ExecMainExitTimestamp`) was wired to one thing, the templated
`pantheon-inbox-consumer@<agent>` units. T-4220 generalizes it to a registry of plain oneshot
service + timer pairs (`WATCHED_UNITS`) and registers `pantheon-cascade-sweep.service` on the
`claude` seat: a sweep that fires for a while and then goes quiet is invisible to OnFailure (no run
started, so none failed) and to verify_startup C8 (boot-time, never-armed only).

Layers, in the order that matters:
  1. AgentPathPinned : the inbox-consumer path is BYTE-IDENTICAL. Every detail string and every
                       probe command is pinned as a golden literal. These were captured from the
                       pre-refactor module, so they pass before and after the change -- that is the
                       point of a characterization test, not a defect.
  2. Ops probes      : the exact `systemctl --user` commands for a plain unit, and the "could not
                       ask" (rc != 0) vs "asked, found nothing" (rc == 0, empty) split.
  3. classify        : the shared verdict logic, run with a unit (its own threshold + wording).
  4. check_all       : routing (alert as the OWNER persona), state keying, cooldown, streak, mutes,
                       and that an explicit `agents=[...]` never probes a unit.
  5. registry        : the cascade-sweep entry is consistent with its real unit files.
  6. CLI             : `--unit KEY`.

Fixture-string note: the byte-sequence trigger is built at runtime via source-time concatenation
("HERME" + "S.md") so the contiguous bytes never appear in this file (standing rule).
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tools import consumer_liveness_monitor as clm

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEMD = REPO_ROOT / "tools" / "systemd"
TRIGGER = "HERME" + "S.md"  # never written contiguously in source

NOW = 1_800_000_000
SUB = {"orchestrator": "claude", "recovery": "claude-fallback"}
UNIT = clm.WatchedUnit(
    key="cascade-sweep",
    service="pantheon-cascade-sweep.service",
    timer="pantheon-cascade-sweep.timer",
    owner="orchestrator",
    max_silence_secs=3600,
    label="ORG-013 cascade sweep",
)
THR = UNIT.max_silence_secs


class ScriptedOps(clm.Ops):
    """The REAL Ops probe methods over a scripted `_sudo_user`, recording every command."""

    def __init__(self, replies):
        self.calls: list[tuple[str, str]] = []
        self._replies = list(replies)

    def _sudo_user(self, account, cmd):
        self.calls.append((account, cmd))
        return self._replies.pop(0)


class FakeUnitOps:
    """Feeds scripted unit state; records alerts. Agent probes are FORBIDDEN (units-only runs)."""

    def __init__(self, *, now=NOW, enablement=None, service=None):
        self._now = now
        self._enablement = enablement or {}
        self._service = service or {}
        self.alerts: list[tuple[str, str, str]] = []
        self.unit_calls: list[tuple[str, str, str]] = []

    def now(self):
        return self._now

    def unit_enablement(self, account, timer):
        self.unit_calls.append(("enablement", account, timer))
        return self._enablement.get(timer)

    def unit_last_exit(self, account, service):
        self.unit_calls.append(("last_exit", account, service))
        return self._service.get(service)

    def timer_enablement(self, account, agent):
        raise AssertionError("an agent probe ran in a units-only check")

    def service_last_exit(self, account, agent):
        raise AssertionError("an agent probe ran in a units-only check")

    def alert(self, msg, vocab, agent):
        self.alerts.append((agent, vocab, msg))


def _stalled_ops():
    return FakeUnitOps(
        enablement={UNIT.timer: "enabled"},
        service={UNIT.service: {"exit_epoch": NOW - THR - 300, "result": "success",
                                "active_state": "inactive"}})


def _run_units(ops, state_dir, *, units=None, **kw):
    return clm.check_all(ops=ops, state_dir=Path(state_dir), agents=[],
                         units=[UNIT] if units is None else units, substrate=SUB,
                         dry_run=kw.pop("dry_run", False), **kw)


# ---------------------------------------------------------------------------
# 1. The inbox-consumer path is byte-identical
# ---------------------------------------------------------------------------

class AgentPathPinned(unittest.TestCase):
    """Golden literals from the pre-T-4220 module. A wording drift here is a regression."""

    def _classify(self, **kw):
        r = clm.classify_agent("recovery", **kw)
        return r["verdict"], r["detail"]

    def test_every_verdict_and_detail_string_is_unchanged(self):
        base = dict(account="claude-fallback", now=NOW)
        stale = {"exit_epoch": NOW - 1800 - 300, "result": "success", "active_state": "inactive"}
        cases = {
            "no_account": (dict(account=None, enablement=None, service=None, now=NOW),
                           ("UNKNOWN", "recovery: no account known (absent from the substrate map) — unmeasurable.")),
            "unaskable": (dict(base, enablement=None, service=None),
                          ("UNKNOWN", "recovery: could not query claude-fallback's user manager (denied sudo / dead manager / timeout). Liveness is unmeasurable — NOT a green result.")),
            "missing": (dict(base, enablement="missing", service=None),
                        ("NO_TIMER", "recovery: no inbox-consumer unit on claude-fallback. delivery_reachability (MISSING) owns this alarm — not re-paged here.")),
            "disabled": (dict(base, enablement="disabled", service=None),
                         ("DISABLED", "recovery: inbox-consumer timer on claude-fallback is switched OFF — an authorised no, not a stall.")),
            "svc_none": (dict(base, enablement="enabled", service=None),
                         ("UNKNOWN", "recovery: timer enabled but the service state on claude-fallback could not be read — unmeasurable, NOT green.")),
            "midcycle": (dict(base, enablement="enabled",
                              service={"exit_epoch": None, "result": "", "active_state": "activating"}),
                         ("LIVE", "recovery: consumer is mid-cycle (ActiveState=activating); exit-timestamp transiently cleared during activation — alive, not never-fired (T-3099).")),
            "never": (dict(base, enablement="enabled",
                           service={"exit_epoch": None, "result": "", "active_state": "inactive"}),
                      ("UNKNOWN", "recovery: timer enabled but the consumer has no completed-run timestamp yet (never fired / just enabled). verify_startup C8 owns the never-fired case — held UNKNOWN here, not silently green.")),
            "stalled": (dict(base, enablement="enabled", service=stale),
                        ("STALLED", "recovery: inbox-consumer on claude-fallback last COMPLETED a cycle 35 min ago (cadence 10 min, threshold 30 min). The timer is enabled but has stopped delivering cycles — a stall the empty-queue detectors cannot see, and OnFailure does not catch because no run started to fail. (ActiveState=inactive)")),
            "stalled_failed": (dict(base, enablement="enabled",
                                    service=dict(stale, result="exit-code", active_state="failed")),
                               ("STALLED", "recovery: inbox-consumer on claude-fallback last COMPLETED a cycle 35 min ago (cadence 10 min, threshold 30 min). The timer is enabled but has stopped delivering cycles — a stall the empty-queue detectors cannot see, and OnFailure does not catch because no run started to fail. Last run Result=exit-code (ActiveState=failed)")),
            "live": (dict(base, enablement="enabled",
                          service={"exit_epoch": NOW - 120, "result": "success", "active_state": "inactive"}),
                     ("LIVE", "recovery: inbox-consumer completed a cycle 2 min ago (<= 30 min threshold).")),
        }
        for name, (kwargs, want) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self._classify(**kwargs), want)

    def test_agent_probe_commands_are_unchanged(self):
        ops = ScriptedOps([(0, "pantheon-inbox-consumer@.timer static\n"), (0, "enabled\n")])
        self.assertEqual(ops.timer_enablement("claude-fallback", "recovery"), "enabled")
        self.assertEqual(ops.calls, [
            ("claude-fallback", "systemctl --user list-unit-files 'pantheon-inbox-consumer@.timer' --no-legend"),
            ("claude-fallback", "systemctl --user is-enabled pantheon-inbox-consumer@recovery.timer 2>&1"),
        ])
        ops = ScriptedOps([(0, "ExecMainExitTimestamp=@1799999000\nResult=success\nActiveState=inactive\n")])
        self.assertEqual(ops.service_last_exit("claude-fallback", "recovery"),
                         {"exit_epoch": 1799999000, "result": "success", "active_state": "inactive"})
        self.assertEqual(ops.calls, [
            ("claude-fallback",
             "systemctl --user show --timestamp=unix pantheon-inbox-consumer@recovery.service "
             "-p ExecMainExitTimestamp -p Result -p ActiveState"),
        ])


# ---------------------------------------------------------------------------
# 2. Ops probes for a plain (non-templated) unit
# ---------------------------------------------------------------------------

class UnitOpsProbes(unittest.TestCase):

    def test_enabled_unit_issues_list_then_is_enabled_on_the_timer_file(self):
        ops = ScriptedOps([(0, "pantheon-cascade-sweep.timer enabled enabled\n"), (0, "enabled\n")])
        self.assertEqual(ops.unit_enablement("claude", UNIT.timer), "enabled")
        self.assertEqual(ops.calls, [
            ("claude", "systemctl --user list-unit-files 'pantheon-cascade-sweep.timer' --no-legend"),
            ("claude", "systemctl --user is-enabled pantheon-cascade-sweep.timer 2>&1"),
        ])

    def test_could_not_ask_is_none_not_missing(self):
        ops = ScriptedOps([(255, "")])
        self.assertIsNone(ops.unit_enablement("claude", UNIT.timer))
        self.assertEqual(len(ops.calls), 1)

    def test_asked_and_found_nothing_is_missing(self):
        ops = ScriptedOps([(0, "  \n")])
        self.assertEqual(ops.unit_enablement("claude", UNIT.timer), "missing")
        self.assertEqual(len(ops.calls), 1, "a missing unit must not be probed for enablement")

    def test_disabled_and_unrecognised_states(self):
        for state, want in (("disabled", "disabled"), ("masked", "disabled"), ("static", "enabled"),
                            ("enabled-runtime", "enabled"), ("bogus", None), ("", None)):
            with self.subTest(state=state):
                ops = ScriptedOps([(0, "x\n"), (0, f"{state}\n")])
                self.assertEqual(ops.unit_enablement("claude", UNIT.timer), want)

    def test_last_exit_reads_the_service_and_parses_the_unix_timestamp(self):
        ops = ScriptedOps([(0, "ExecMainExitTimestamp=@1799999000\nResult=success\nActiveState=inactive\n")])
        self.assertEqual(ops.unit_last_exit("claude", UNIT.service),
                         {"exit_epoch": 1799999000, "result": "success", "active_state": "inactive"})
        self.assertEqual(ops.calls, [
            ("claude",
             "systemctl --user show --timestamp=unix pantheon-cascade-sweep.service "
             "-p ExecMainExitTimestamp -p Result -p ActiveState"),
        ])

    def test_last_exit_unaskable_or_unknown_unit_is_none_and_never_run_is_epoch_none(self):
        self.assertIsNone(ScriptedOps([(1, "")]).unit_last_exit("claude", UNIT.service))
        self.assertIsNone(ScriptedOps([(0, "")]).unit_last_exit("claude", UNIT.service))
        never = ScriptedOps([(0, "ExecMainExitTimestamp=\nResult=success\nActiveState=inactive\n")])
        self.assertIsNone(never.unit_last_exit("claude", UNIT.service)["exit_epoch"])


# ---------------------------------------------------------------------------
# 3. The shared verdict logic, run with a unit
# ---------------------------------------------------------------------------

class ClassifyUnit(unittest.TestCase):

    def _classify(self, **kw):
        kw.setdefault("account", "claude")
        kw.setdefault("now", NOW)
        return clm.classify_agent(UNIT.key, unit=UNIT, **kw)

    def _svc(self, age, **extra):
        return dict({"exit_epoch": NOW - age, "result": "success", "active_state": "inactive"}, **extra)

    def test_live_inside_the_unit_threshold(self):
        v = self._classify(enablement="enabled", service=self._svc(60))
        self.assertEqual(v["verdict"], clm.LIVE)
        self.assertIn("ORG-013 cascade sweep", v["detail"])

    def test_the_unit_threshold_governs_not_the_inbox_consumer_one(self):
        # 2000s is past the inbox-consumer's 1800s but well inside this unit's 3600s.
        age = 2000
        self.assertEqual(self._classify(enablement="enabled", service=self._svc(age))["verdict"], clm.LIVE)
        agent = clm.classify_agent("recovery", account="claude-fallback", enablement="enabled",
                                   service=self._svc(age), now=NOW)
        self.assertEqual(agent["verdict"], clm.STALLED)

    def test_boundary_is_strictly_greater_than_the_threshold(self):
        self.assertEqual(self._classify(enablement="enabled", service=self._svc(THR))["verdict"], clm.LIVE)
        self.assertEqual(self._classify(enablement="enabled", service=self._svc(THR + 1))["verdict"],
                         clm.STALLED)

    def test_freshness_same_exit_flips_when_now_advances(self):
        svc = self._svc(300)
        self.assertEqual(self._classify(enablement="enabled", service=svc)["verdict"], clm.LIVE)
        later = self._classify(enablement="enabled", service=svc, now=svc["exit_epoch"] + THR + 60)
        self.assertEqual(later["verdict"], clm.STALLED)

    def test_stalled_detail_names_the_unit_account_and_threshold_and_no_inbox_consumer(self):
        v = self._classify(enablement="enabled", service=self._svc(THR + 300, result="exit-code",
                                                                    active_state="failed"))
        self.assertEqual(v["verdict"], clm.STALLED)
        d = v["detail"]
        for needle in ("cascade-sweep", "ORG-013 cascade sweep", "claude", "65 min", "60 min",
                       "OnFailure", "Result=exit-code", "ActiveState=failed"):
            self.assertIn(needle, d)
        self.assertNotIn("inbox-consumer", d)
        self.assertNotIn("delivery_reachability", d)

    def test_mid_run_sample_is_live_and_a_dead_activation_is_not(self):
        for active in ("activating", "active", "reloading"):
            with self.subTest(active=active):
                v = self._classify(enablement="enabled",
                                   service={"exit_epoch": None, "result": "", "active_state": active})
                self.assertEqual(v["verdict"], clm.LIVE)
        for inert in ("inactive", "failed", ""):
            with self.subTest(inert=inert):
                v = self._classify(enablement="enabled",
                                   service={"exit_epoch": None, "result": "", "active_state": inert})
                self.assertEqual(v["verdict"], clm.UNKNOWN)
                self.assertNotIn("consumer", v["detail"])

    def test_no_timer_is_report_only_and_does_not_borrow_another_detectors_alarm(self):
        v = self._classify(enablement="missing", service=None)
        self.assertEqual(v["verdict"], clm.NO_TIMER)
        self.assertNotIn(clm.NO_TIMER, clm.ALERTING)
        for needle in ("pantheon-cascade-sweep.timer", "claude", "not installed"):
            self.assertIn(needle, v["detail"])
        self.assertNotIn("delivery_reachability", v["detail"])
        self.assertNotIn("inbox-consumer", v["detail"])

    def test_disabled_is_report_only(self):
        v = self._classify(enablement="disabled", service=None)
        self.assertEqual(v["verdict"], clm.DISABLED)
        self.assertNotIn("inbox-consumer", v["detail"])
        self.assertIn("pantheon-cascade-sweep.timer", v["detail"])

    def test_no_timer_and_disabled_state_the_fact_and_only_the_possibility_of_intent(self):
        """The report must not assert WHY the unit is gone: a deleted or disabled timer is not
        always a deliberate lifecycle act, and nothing pages it. So it names the consequence and
        the way back, and hedges the intent (T-4220 rev N3)."""
        for enablement, hedge, fix in (
                ("missing", "If this wasn't a deliberate removal (ORG-068), ",
                 "reinstall per tools/systemd/README.md"),
                ("disabled", "If this wasn't a deliberate disable (ORG-068), ",
                 "re-enable per tools/systemd/README.md")):
            with self.subTest(enablement=enablement):
                d = self._classify(enablement=enablement, service=None)["detail"]
                self.assertIn(hedge, d)
                self.assertIn(fix, d)
                self.assertIn("Reported, not paged", d)
                for asserted in ("an authorised no", "not a stall", "a deliberate removal (ORG-068), not"):
                    self.assertNotIn(asserted, d)

    def test_the_consequence_clause_comes_from_the_unit_and_is_omitted_when_it_has_none(self):
        # The registered cascade sweep carries its own consequence, and the report uses it.
        sweep = next(u for u in clm.WATCHED_UNITS if u.key == "cascade-sweep")
        self.assertIn("resolve-time-only cascade", sweep.consequence)
        for enablement in ("missing", "disabled"):
            d = clm.classify_agent(sweep.key, unit=sweep, account="claude", enablement=enablement,
                                   service=None, now=NOW)["detail"]
            self.assertIn("the sweep has degraded to resolve-time-only cascade", d)
        # A unit registered without one gets a report with no dangling clause.
        for enablement in ("missing", "disabled"):
            d = self._classify(enablement=enablement, service=None)["detail"]
            self.assertNotIn("degraded", d)
            self.assertNotIn(";;", d)
            self.assertNotIn("; ;", d)
            self.assertNotIn(", ,", d)

    def test_unmeasurable_is_never_green(self):
        self.assertEqual(self._classify(enablement=None, service=None)["verdict"], clm.UNKNOWN)
        self.assertEqual(self._classify(enablement="enabled", service=None)["verdict"], clm.UNKNOWN)
        self.assertEqual(self._classify(account=None, enablement=None, service=None)["verdict"],
                         clm.UNKNOWN)


# ---------------------------------------------------------------------------
# 4. check_all over units
# ---------------------------------------------------------------------------

class CheckAllUnits(unittest.TestCase):

    def test_a_stalled_unit_alerts_once_routed_as_its_owner_persona(self):
        with TemporaryDirectory() as d:
            ops = _stalled_ops()
            res = _run_units(ops, d)
            self.assertEqual([(r["agent"], r["verdict"], r["alerted"]) for r in res],
                             [("cascade-sweep", clm.STALLED, True)])
            self.assertEqual(len(ops.alerts), 1)
            owner, vocab, msg = ops.alerts[0]
            self.assertEqual(owner, "orchestrator")  # route_failure_alert routes non-recovery personas via Recovery
            self.assertEqual(vocab, "consumer-liveness (STALLED)")
            self.assertIn("cascade-sweep", msg)
            # The probe went to the OWNER's account: orchestrator -> claude.
            self.assertEqual(ops.unit_calls[0], ("enablement", "claude", UNIT.timer))
            self.assertEqual(ops.unit_calls[1], ("last_exit", "claude", UNIT.service))

    def test_state_is_keyed_by_the_unit_key_not_the_owner(self):
        with TemporaryDirectory() as d:
            _run_units(_stalled_ops(), d)
            state = json.loads((Path(d) / clm._STATE_NAME).read_text())
            self.assertEqual(list(state), ["cascade-sweep"])
            self.assertEqual(state["cascade-sweep"]["last_verdict"], clm.STALLED)
            self.assertEqual(state["cascade-sweep"]["last_alert_verdict"], clm.STALLED)

    def test_a_repeat_stall_is_throttled_by_the_cooldown(self):
        with TemporaryDirectory() as d:
            ops = _stalled_ops()
            _run_units(ops, d, cooldown_secs=6 * 3600)
            _run_units(ops, d, cooldown_secs=6 * 3600)
            self.assertEqual(len(ops.alerts), 1)

    def test_live_unit_is_silent_and_does_not_probe_the_service_when_not_enabled(self):
        with TemporaryDirectory() as d:
            live = FakeUnitOps(enablement={UNIT.timer: "enabled"},
                               service={UNIT.service: {"exit_epoch": NOW - 60, "result": "success"}})
            self.assertEqual(_run_units(live, d)[0]["verdict"], clm.LIVE)
            self.assertEqual(live.alerts, [])
            off = FakeUnitOps(enablement={UNIT.timer: "disabled"})
            self.assertEqual(_run_units(off, d)[0]["verdict"], clm.DISABLED)
            self.assertEqual([c[0] for c in off.unit_calls], ["enablement"])
            self.assertEqual(off.alerts, [])

    def test_unknown_is_streak_gated_for_a_unit_too(self):
        with TemporaryDirectory() as d:
            ops = FakeUnitOps(enablement={UNIT.timer: None})
            for _ in range(clm.UNKNOWN_ALERT_STREAK - 1):
                self.assertFalse(_run_units(ops, d)[0]["alerted"])
            self.assertEqual(ops.alerts, [])
            self.assertTrue(_run_units(ops, d)[0]["alerted"])
            self.assertEqual(len(ops.alerts), 1)

    def test_an_owner_missing_from_the_substrate_map_is_unknown_not_a_crash(self):
        with TemporaryDirectory() as d:
            res = clm.check_all(ops=_stalled_ops(), state_dir=Path(d), agents=[], units=[UNIT],
                                substrate={})
            self.assertEqual(res[0]["verdict"], clm.UNKNOWN)
            self.assertIn("no account known", res[0]["detail"])

    def test_per_unit_and_fleet_mutes_silence_it_and_lapse(self):
        far, past = "2999-01-01T00:00:00Z", "2000-01-01T00:00:00Z"
        for name, until, want in (
                ("consumer-liveness.cascade-sweep.disabled", far, clm.DISABLED),
                (clm._DISABLE_NAME, far, clm.DISABLED),
                ("consumer-liveness.cascade-sweep.disabled", past, clm.STALLED),
                ("consumer-liveness.orchestrator.disabled", far, clm.STALLED),  # a different key: no effect
        ):
            with self.subTest(name=name, until=until), TemporaryDirectory() as d:
                (Path(d) / name).write_text(json.dumps({"reason": "planned window", "until": until}))
                ops = _stalled_ops()
                res = _run_units(ops, d)
                self.assertEqual(res[0]["verdict"], want)
                self.assertEqual(len(ops.alerts), 0 if want == clm.DISABLED else 1)

    def test_explicit_agents_never_probe_a_unit(self):
        with TemporaryDirectory() as d:
            ops = mock.Mock()
            ops.now.return_value = NOW
            ops.timer_enablement.return_value = "enabled"
            ops.service_last_exit.return_value = {"exit_epoch": NOW - 60, "result": "success"}
            res = clm.check_all(ops=ops, state_dir=Path(d), agents=["recovery"], substrate=SUB)
            self.assertEqual([r["agent"] for r in res], ["recovery"])
            ops.unit_enablement.assert_not_called()
            ops.unit_last_exit.assert_not_called()

    def test_default_run_checks_the_seats_then_the_registry(self):
        with TemporaryDirectory() as d, \
                mock.patch.object(clm, "_seat_inventory", return_value=["recovery"]), \
                mock.patch.object(clm, "WATCHED_UNITS", (UNIT,)):
            ops = FakeUnitOps(enablement={UNIT.timer: "enabled"},
                              service={UNIT.service: {"exit_epoch": NOW - 60, "result": "success"}})
            ops.timer_enablement = lambda account, agent: "enabled"
            ops.service_last_exit = lambda account, agent: {"exit_epoch": NOW - 60, "result": "success"}
            res = clm.check_all(ops=ops, state_dir=Path(d), substrate=SUB)
            self.assertEqual([r["agent"] for r in res], ["recovery", "cascade-sweep"])

    def test_an_unreadable_roster_still_reports_it_and_still_checks_the_units(self):
        with TemporaryDirectory() as d, \
                mock.patch.object(clm, "_seat_inventory", return_value=[]), \
                mock.patch.object(clm, "WATCHED_UNITS", (UNIT,)):
            res = clm.check_all(ops=_stalled_ops(), state_dir=Path(d), substrate=SUB)
            self.assertEqual([(r["agent"], r["verdict"]) for r in res],
                             [("*", clm.UNKNOWN), ("cascade-sweep", clm.STALLED)])

    def test_units_only_run_does_not_report_an_unknown_roster(self):
        with TemporaryDirectory() as d:
            res = _run_units(_stalled_ops(), d)
            self.assertEqual([r["agent"] for r in res], ["cascade-sweep"])


# ---------------------------------------------------------------------------
# 5. The registry entry is consistent with its real unit files
# ---------------------------------------------------------------------------

def _directive(path: Path, key: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.partition("=")[2].strip()
    raise AssertionError(f"{path.name}: no {key}= line")


def _max_gap_secs(on_calendar: str) -> int:
    m = re.fullmatch(r"\*-\*-\* \*:([0-9]{2}(?:,[0-9]{2})*):00", on_calendar)
    if not m:
        raise AssertionError(
            f"OnCalendar={on_calendar!r} is not the `*-*-* *:MM,MM:00` shape this guard understands: "
            "teach _max_gap_secs the new shape AND revisit WatchedUnit.max_silence_secs in lockstep")
    minutes = sorted(int(x) for x in m.group(1).split(","))
    gaps = [b - a for a, b in zip(minutes, minutes[1:])] + [60 - minutes[-1] + minutes[0]]
    return max(gaps) * 60


class Registry(unittest.TestCase):
    entry = next((u for u in clm.WATCHED_UNITS if u.key == "cascade-sweep"), None)

    def test_the_cascade_sweep_is_registered_against_the_claude_seat(self):
        self.assertIsNotNone(self.entry, "pantheon-cascade-sweep must be in WATCHED_UNITS (T-4220)")
        self.assertEqual((self.entry.service, self.entry.timer, self.entry.owner),
                         ("pantheon-cascade-sweep.service", "pantheon-cascade-sweep.timer", "orchestrator"))
        # The unit is installed on the claude seat; the owner persona must resolve to that account.
        self.assertEqual(clm._substrate().get("orchestrator"), "claude")

    def test_the_entry_names_real_unit_files_that_belong_together(self):
        self.assertTrue((SYSTEMD / self.entry.service).is_file())
        self.assertTrue((SYSTEMD / self.entry.timer).is_file())
        self.assertEqual(_directive(SYSTEMD / self.entry.timer, "Unit"), self.entry.service)

    def test_the_threshold_tolerates_two_missed_fires_of_the_real_timer(self):
        gap = _max_gap_secs(_directive(SYSTEMD / self.entry.timer, "OnCalendar"))
        self.assertEqual(gap, 15 * 60)
        # A run late by one jittered fire plus two whole missed fires must still read LIVE.
        self.assertGreaterEqual(self.entry.max_silence_secs, 3 * gap)

    def test_keys_are_unique_and_never_a_persona_name(self):
        keys = [u.key for u in clm.WATCHED_UNITS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertFalse(set(keys) & set(clm._substrate()),
                         "a unit key shares the state file and mute-file namespace with persona names")


# ---------------------------------------------------------------------------
# 6. CLI: --unit KEY
# ---------------------------------------------------------------------------

class CliUnitFlag(unittest.TestCase):

    def _main(self, argv, ops):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(clm, "Ops", return_value=ops), \
                mock.patch.object(clm, "_substrate", return_value=SUB), \
                mock.patch.object(clm, "WATCHED_UNITS", (UNIT,)):
            try:
                rc = clm.main(argv)
            except SystemExit as exc:  # argparse
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def test_unit_flag_checks_only_that_unit_and_dry_run_never_alerts(self):
        with TemporaryDirectory() as d:
            ops = _stalled_ops()
            rc, out, _ = self._main(["--unit", "cascade-sweep", "--dry-run", "--json",
                                     "--state-dir", d], ops)
            self.assertEqual(rc, 0)
            res = json.loads(out)
            self.assertEqual([(r["agent"], r["verdict"], r["alerted"]) for r in res],
                             [("cascade-sweep", clm.STALLED, False)])
            self.assertEqual(ops.alerts, [])

    def test_unknown_unit_key_and_combining_with_agent_are_usage_errors(self):
        with TemporaryDirectory() as d:
            self.assertEqual(self._main(["--unit", "nonesuch", "--state-dir", d], _stalled_ops())[0], 2)
            self.assertEqual(self._main(["--unit", "cascade-sweep", "--agent", "recovery",
                                         "--state-dir", d], _stalled_ops())[0], 2)


class Hygiene(unittest.TestCase):
    def test_touched_files_are_free_of_the_trigger_bytes(self):
        for path in (REPO_ROOT / "tools" / "consumer_liveness_monitor.py", Path(__file__)):
            with self.subTest(path=path.name):
                self.assertNotIn(TRIGGER, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
