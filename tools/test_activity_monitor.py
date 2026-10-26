"""Unit tests for tools/activity_monitor.py — ORG-011 Phase B / T-0223.

Run from repo root:
    python3 -m unittest tools.test_activity_monitor -v

Covers:
  - cooldown semantics (recent / old / missing / malformed state)
  - latest_commit_ts + latest_ledger_entry_ts_for_agent
  - count_actionable_tickets frontmatter parser
  - latest_assistant_ends_without_tool_use structural signal (T-3071)
  - operator_last_inbound_age_seconds parser
  - check_p1a + check_p1b composition
  - main flow with mocked subprocess + urlopen
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.activity_monitor as am
from tools.activity_monitor import (
    AGENT_CONFIG,
    P1A_IDLE_THRESHOLD_SECONDS,
    P1B_OPERATOR_ABSENT_THRESHOLD_SECONDS,
    P1D_RATE_LIMIT_AGE_SECONDS,
    P1D_COOLDOWN_SECONDS,
    ACTIVITY_MONITOR_COOLDOWN_SECONDS,
    RATE_LIMIT_REGEX,
    capture_pane,
    check_p1d,
    count_actionable_tickets,
    _scan_actionable_tickets,
    detect_rate_limit_in_pane,
    in_cooldown,
    latest_rate_limit_assistant_ts,
    operator_last_inbound_age_seconds,
    main,
    write_poke_state,
)

# ---------------------------------------------------------------------------
# T-3072: module-wide PANTHEON_STATE_DIR isolation.
#
# activity_monitor.py now writes a best-effort fire-tracking JSONL
# (`_record_fire`) keyed off PANTHEON_STATE_DIR on every landed poke/modal-
# confirm/escalation — including from test classes that exercise
# `_dispatch_poke` / `_escalate_stuck` / `main()` WITHOUT setting their own
# PANTHEON_STATE_DIR override (e.g. TestDispatchPoke, CapModalDispatchT1369,
# TestCapPrePokeGate). Without this module-wide redirect, those tests would
# append to the REAL account's `~/.local/state/pantheon/activity_monitor/
# fires.jsonl` on whatever seat runs the suite. Classes that need their OWN
# isolated state dir (TestMain, TuiWedgeMainT1369) still set/restore
# PANTHEON_STATE_DIR in their own setUp/tearDown, which nests safely on top
# of this module-wide default (setUp runs after setUpModule; tearDown
# restores before tearDownModule tears down the module default).
# ---------------------------------------------------------------------------

_MODULE_STATE_TMP: tempfile.TemporaryDirectory | None = None
# T-5367: the poke-effectiveness signal also tails the transcripts (as the seat's
# account, via sudo). Stubbed to "no entry" module-wide so it falls back to the mocked
# latest_jsonl_mtime and no test reads a live seat's transcript; the T-5367 class
# below drives the real seam with a mocked subprocess.
_ENTRY_STUB = None


def setUpModule() -> None:
    global _MODULE_STATE_TMP
    _MODULE_STATE_TMP = tempfile.TemporaryDirectory()
    os.environ["PANTHEON_STATE_DIR"] = str(Path(_MODULE_STATE_TMP.name) / "state")
    # T-4510: hermetic against a host whose real fleet-usage dir holds a >=95% reading.
    # A nonexistent dir makes the usage gate fail open, i.e. every pre-T-4510 test is
    # unchanged. The T-4510 class below points the dir at its own temp readings.
    os.environ["PANTHEON_FLEET_USAGE_DIR"] = str(
        Path(_MODULE_STATE_TMP.name) / "no-such-fleet-usage-dir")
    global _ENTRY_STUB
    _ENTRY_STUB = patch.object(am, "_jsonl_entry_epoch", return_value=None)
    _ENTRY_STUB.start()
    # ORG-106 L2 (T-5525): hermetic against the host's real seat-state record,
    # maintenance registry and OOM hold, and against the cross-account record read.
    # Patched on seat_state (not on am), so _reload_am() cannot drop them.
    from tools import seat_state as _ss
    global _SS_STUBS
    _SS_STUBS = [patch(f"tools.seat_state.{n}", return_value=_ss.Contribution())
                 for n in ("_read_seat_state", "_read_seat_state_cross",
                           "_read_maintenance", "_read_oom")]
    for _p in _SS_STUBS:
        _p.start()


def _reload_am():
    """Reload activity_monitor for the rest of the suite and re-apply the T-5367 entry
    stub: a reload rebinds the real _jsonl_entry_epoch over the patch (T-5400)."""
    import importlib
    importlib.reload(am)
    if _ENTRY_STUB is not None:
        _ENTRY_STUB.stop()
        _ENTRY_STUB.start()


_SS_STUBS: list = []


def tearDownModule() -> None:
    for _p in _SS_STUBS:
        _p.stop()
    if _ENTRY_STUB is not None:
        _ENTRY_STUB.stop()
    os.environ.pop("PANTHEON_STATE_DIR", None)
    os.environ.pop("PANTHEON_FLEET_USAGE_DIR", None)
    if _MODULE_STATE_TMP is not None:
        _MODULE_STATE_TMP.cleanup()


# ---------------------------------------------------------------------------
# AGENT_CONFIG sanity
# ---------------------------------------------------------------------------


class TestAgentConfig(unittest.TestCase):
    def test_orchestrator_present(self) -> None:
        self.assertIn("orchestrator", AGENT_CONFIG)
        cfg = AGENT_CONFIG["orchestrator"]
        self.assertEqual(cfg["tmux_target_pane"], "claude-telegram-bot:0")
        self.assertEqual(cfg["sudo_user"], "claude")
        self.assertEqual(cfg["ledger_agent_id"], "orchestrator")
        self.assertIn("orchestrator", cfg["ticket_targets"])
        # P1.c (ORG-037 Layer 3): orchestrator must carry a teams_dir to be eligible.
        self.assertEqual(cfg["teams_dir"], Path("/home/claude/.claude/teams"))
        # T-3557(e): orchestrator carries NO queue_states override → defaults to
        # inbox+active. orchestrator is the always-on inbox-drainer, so an idle inbox
        # IS a signal; this guards that the sub-orch active-only tune did not
        # silently narrow the HO.
        self.assertNotIn("queue_states", cfg)

    def test_specialist_recovery_not_yet_covered(self) -> None:
        # ORG-011 Phase B is orchestrator-only initially; specialist + recovery come post-empirical.
        self.assertNotIn("specialist", AGENT_CONFIG)
        self.assertNotIn("recovery", AGENT_CONFIG)

    def test_orchestrator_jsonl_dir_follows_the_seat_launch_shape(self) -> None:
        # T-4159: derived, so it flips to -home-claude-pantheon-config when orchestrator's
        # T-3650 commit B marks it control-plane, instead of freezing on a dead dir
        # and false-flagging a live orchestrator idle.
        import importlib.util
        from unittest import mock
        from tools import agent_role_map
        self.assertEqual(AGENT_CONFIG["orchestrator"]["jsonl_dir"],
                         Path(agent_role_map.live_session_projects_dir("claude")))
        # Non-vacuous half: a FRESH copy of the module (own name, not the cached
        # import) built while orchestrator's binding carries the cutover marker must pick
        # up the -pantheon-config dir. A hardcoded literal fails here.
        real = agent_role_map.instance_binding
        def binding(persona):
            b = real(persona)
            return {**b, "supervisor": "control-plane"} if persona == "orchestrator" else b
        spec = importlib.util.spec_from_file_location(
            "_activity_monitor_t4159_probe", Path(agent_role_map.__file__).with_name("activity_monitor.py"))
        fresh = importlib.util.module_from_spec(spec)
        with mock.patch.object(agent_role_map, "instance_binding", side_effect=binding):
            spec.loader.exec_module(fresh)
        self.assertEqual(fresh.AGENT_CONFIG["orchestrator"]["jsonl_dir"],
                         Path("/home/claude/.claude/projects/-home-claude-pantheon-config"))

    def test_artisan_mason_covered_narrow_scope(self) -> None:
        # T-3555 (the operator TG 9289): the two ORG-043 sub-orchestrator seats get
        # await/cap-modal/rate-limit coverage — NARROW scope, NO P1.c teams_dir.
        for agent, account, pane in (("artisan", "suborch2", "suborch2-main:0"),
                                     ("mason", "suborch1", "suborch1-main:0")):
            cfg = AGENT_CONFIG[agent]
            self.assertEqual(cfg["sudo_user"], account)
            self.assertEqual(cfg["tmux_target_pane"], pane)
            self.assertEqual(cfg["ledger_agent_id"], agent)
            self.assertIn(agent, cfg["ticket_targets"])
            # Active jsonl is the -pantheon-config project (consume-runtime launches
            # cwd=<account>/pantheon-config), NOT the month-stale plain -home-<account>.
            self.assertTrue(
                str(cfg["jsonl_dir"]).endswith(f"-home-{account}-pantheon-config"),
                f"{agent} jsonl_dir must point at the active -pantheon-config project",
            )
            self.assertEqual(cfg["pantheon_config_path"], Path(f"/home/{account}/pantheon-config"))
            # NARROW scope (T-3555): no teams_dir → structurally excluded from the
            # P1.c Layer-3 worker-silence witness apparatus (msg_1326 veto reversed
            # only for these two, and only for the await/modal/rate-limit predicates).
            self.assertNotIn("teams_dir", cfg)
            # T-3557(e): sub-orch seats count ACTIVE tickets only — an unclaimed
            # inbox ticket is undispatched, not work the seat is stuck on. This is
            # the wiring guard: without it the function-level active-only tests
            # would pass while the real monitor still counted inbox (false-fire).
            self.assertEqual(cfg["queue_states"], ("active",))


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


class TestInCooldown(unittest.TestCase):
    def test_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(in_cooldown(Path(tmp) / "x.json", 1800))

    def test_recent_state_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            p.write_text(json.dumps({"ts": int(time.time()) - 60}))
            self.assertTrue(in_cooldown(p, 1800))

    def test_old_state_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            p.write_text(json.dumps({"ts": int(time.time()) - 3600}))
            self.assertFalse(in_cooldown(p, 1800))

    def test_malformed_state_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            p.write_text("not json")
            self.assertFalse(in_cooldown(p, 1800))


class TestWritePokeState(unittest.TestCase):
    def test_writes_expected_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sub" / "state.json"
            write_poke_state(p, "orchestrator", "p1a")
            data = json.loads(p.read_text())
            self.assertEqual(data["agent"], "orchestrator")
            self.assertEqual(data["predicate"], "p1a")
            self.assertIsInstance(data["ts"], int)
            # Legacy call → consecutive_ineffective defaults to 0, no pre-mtime.
            self.assertEqual(data["consecutive_ineffective"], 0)
            self.assertNotIn("pre_poke_jsonl_mtime", data)

    def test_writes_bounded_poke_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            write_poke_state(p, "orchestrator", "p1c",
                             pre_poke_jsonl_mtime=12345, consecutive_ineffective=2,
                             escalated=True)
            data = am.read_poke_state(p)
            self.assertEqual(data["pre_poke_jsonl_mtime"], 12345)
            self.assertEqual(data["consecutive_ineffective"], 2)
            self.assertTrue(data["escalated"])

    def test_read_poke_state_absent_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(am.read_poke_state(Path(tmp) / "nope.json"))


class TestPokeEffectivenessDecision(unittest.TestCase):
    """T-1363 — bounded-poke: suppress once pokes stop advancing the session.

    The poke-once-vs-skip fork is the max_ineffective parameter (N), so these
    pin both N=1 (poke-once-then-escalate) and N=0 (skip-pokes) ends.
    """

    def test_first_fire_no_prev_pokes(self) -> None:
        suppress, consec, _ = am.poke_effectiveness_decision(None, 1000, "p1c", 1)
        self.assertFalse(suppress)
        self.assertEqual(consec, 0)

    def test_effective_poke_resets_counter(self) -> None:
        # prior poke recorded pre-mtime 1000; jsonl now 2000 → it advanced → worked.
        prev = {"predicate": "p1c", "pre_poke_jsonl_mtime": 1000, "consecutive_ineffective": 3}
        suppress, consec, _ = am.poke_effectiveness_decision(prev, 2000, "p1c", 1)
        self.assertFalse(suppress)
        self.assertEqual(consec, 0)

    def test_ineffective_increments_below_threshold(self) -> None:
        # pre-mtime 1000, jsonl still 1000 → didn't advance → ineffective. N=2 → no suppress yet.
        prev = {"predicate": "p1c", "pre_poke_jsonl_mtime": 1000, "consecutive_ineffective": 0}
        suppress, consec, _ = am.poke_effectiveness_decision(prev, 1000, "p1c", 2)
        self.assertFalse(suppress)
        self.assertEqual(consec, 1)

    def test_ineffective_suppresses_at_threshold_n1(self) -> None:
        prev = {"predicate": "p1c", "pre_poke_jsonl_mtime": 1000, "consecutive_ineffective": 0}
        suppress, consec, reason = am.poke_effectiveness_decision(prev, 1000, "p1c", 1)
        self.assertTrue(suppress)
        self.assertEqual(consec, 1)
        self.assertIn("suppress", reason)

    def test_accumulates_across_cycles(self) -> None:
        prev = {"predicate": "p1c", "pre_poke_jsonl_mtime": 1000, "consecutive_ineffective": 2}
        suppress, consec, _ = am.poke_effectiveness_decision(prev, 1000, "p1c", 3)
        self.assertTrue(suppress)   # 2 + 1 = 3 >= 3
        self.assertEqual(consec, 3)

    def test_n_zero_skips_pokes_entirely(self) -> None:
        # The 'poke less' extreme: N=0 → suppress on the very first fire, never poke.
        suppress, consec, _ = am.poke_effectiveness_decision(None, 1000, "p1c", 0)
        self.assertTrue(suppress)
        self.assertEqual(consec, 0)

    def test_predicate_mismatch_resets(self) -> None:
        # Last poke was a different predicate → don't carry its counter.
        prev = {"predicate": "p1a", "pre_poke_jsonl_mtime": 1000, "consecutive_ineffective": 5}
        suppress, consec, _ = am.poke_effectiveness_decision(prev, 1000, "p1c", 1)
        self.assertFalse(suppress)
        self.assertEqual(consec, 0)


class TestRecordFire(unittest.TestCase):
    """T-3072 — `_record_fire`: the meta-drift alarm's producer helper."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp / "state")

    def tearDown(self) -> None:
        os.environ.pop("PANTHEON_STATE_DIR", None)
        self._tmp.cleanup()

    def _fires_path(self) -> Path:
        return self.tmp / "state" / "activity_monitor" / "fires.jsonl"

    def test_appends_one_jsonl_line(self) -> None:
        am._record_fire("orchestrator", "p1a")
        lines = self._fires_path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["agent"], "orchestrator")
        self.assertEqual(rec["predicate"], "p1a")
        self.assertIn("ts", rec)
        self.assertIn("ts_iso", rec)

    def test_multiple_fires_append_not_overwrite(self) -> None:
        am._record_fire("orchestrator", "p1a")
        am._record_fire("orchestrator", "p1b")
        lines = self._fires_path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        predicates = [json.loads(line)["predicate"] for line in lines]
        self.assertEqual(predicates, ["p1a", "p1b"])

    def test_failure_is_swallowed_and_logged_not_raised(self) -> None:
        with patch.object(Path, "mkdir", side_effect=OSError("disk full")):
            with redirect_stdout(io.StringIO()) as out:
                am._record_fire("orchestrator", "p1a")  # must not raise
        self.assertIn("_record_fire", out.getvalue())
        self.assertFalse(self._fires_path().exists())

    def test_respects_pantheon_state_dir_override(self) -> None:
        self.assertEqual(
            am._fires_log_path(),
            self.tmp / "state" / "activity_monitor" / "fires.jsonl",
        )


class TestDispatchPoke(unittest.TestCase):
    """T-1363 — _dispatch_poke: legacy passthrough (flag off) + bounded suppress."""

    def _cfg(self) -> dict:
        return {"tmux_target_pane": "x:0", "sudo_user": "claude",
                "jsonl_dir": Path("/x"), "pantheon_config_path": Path("/x")}

    def test_legacy_path_pokes_and_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "send_poke", return_value=True) as poke, \
                 patch.object(am, "_record_fire") as record_fire:
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1a", "PROMPT", sp,
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 0)
            poke.assert_called_once()
            self.assertEqual(am.read_poke_state(sp)["predicate"], "p1a")
            record_fire.assert_called_once_with("orchestrator", "p1a")  # T-3072

    def test_legacy_path_send_failure_returns_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "send_poke", return_value=False), \
                 patch.object(am, "_record_fire") as record_fire:
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1a", "P", sp,
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 1)
            record_fire.assert_not_called()  # T-3072 — a failed send is not a fire

    def test_bounded_first_fire_pokes_and_records_premtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "send_poke", return_value=True) as poke, \
                 patch.object(am, "_session_cyber_blocked", return_value=False), \
                 patch.object(am, "_record_fire") as record_fire, \
                 patch.object(am, "latest_jsonl_mtime", return_value=5000):
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1c", "P", sp,
                                       bounded_enabled=True, max_ineffective=1)
            self.assertEqual(rc, 0)
            poke.assert_called_once()
            data = am.read_poke_state(sp)
            self.assertEqual(data["pre_poke_jsonl_mtime"], 5000)
            self.assertEqual(data["consecutive_ineffective"], 0)
            record_fire.assert_called_once_with("orchestrator", "p1c")  # T-3072

    def test_bounded_suppresses_when_prior_ineffective(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            # Prior poke recorded pre-mtime 5000; jsonl STILL 5000 → ineffective.
            am.write_poke_state(sp, "orchestrator", "p1c", pre_poke_jsonl_mtime=5000,
                                consecutive_ineffective=0)
            with patch.object(am, "send_poke", return_value=True) as poke, \
                 patch.object(am, "_session_cyber_blocked", return_value=False), \
                 patch.object(am, "_route_failure_alert") as alert, \
                 patch.object(am, "_record_fire") as record_fire, \
                 patch.object(am, "latest_jsonl_mtime", return_value=5000):
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1c", "P", sp,
                                       bounded_enabled=True, max_ineffective=1)
            self.assertEqual(rc, 0)
            poke.assert_not_called()  # SUPPRESSED — the token-saving behavior
            self.assertTrue(am.read_poke_state(sp)["escalated"])
            alert.assert_called_once()  # escalates to Recovery instead of re-poking
            # T-3072: escalation fire is keyed on VOCAB ("stuck-poke"), not the
            # triggering predicate ("p1c") — each escalation class tracks drift
            # independently.
            record_fire.assert_called_once_with("orchestrator", "stuck-poke")

    def test_bounded_cyber_blocked_suppresses_even_when_jsonl_advanced(self) -> None:
        """The pre-poke gate: a cyber-blocked session emits refusals that ADVANCE
        the jsonl, which the effectiveness signal would read as 'poke worked'.
        The gate must suppress BEFORE that check — never poke a cyber-blocked
        session (it needs a restart)."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            # Prior poke at mtime 5000; jsonl ADVANCED to 9000 (refusals) → the
            # effectiveness check alone would call this "effective" and re-poke.
            am.write_poke_state(sp, "orchestrator", "p1c", pre_poke_jsonl_mtime=5000,
                                consecutive_ineffective=0)
            with patch.object(am, "send_poke", return_value=True) as poke, \
                 patch.object(am, "_session_cyber_blocked", return_value=True), \
                 patch.object(am, "_route_failure_alert") as alert, \
                 patch.object(am, "_record_fire") as record_fire, \
                 patch.object(am, "latest_jsonl_mtime", return_value=9000):
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1c", "P", sp,
                                       bounded_enabled=True, max_ineffective=1)
            self.assertEqual(rc, 0)
            poke.assert_not_called()  # gate wins over the (fooled) effectiveness signal
            self.assertTrue(am.read_poke_state(sp)["escalated"])
            alert.assert_called_once()  # cyber → escalate to Recovery for restart
            record_fire.assert_called_once_with("orchestrator", "cyber-refusal")  # T-3072 vocab key

    def test_escalation_alerts_once_then_stays_quiet(self) -> None:
        """Bounded escalation: alert Recovery ONCE on the escalated transition, NOT
        every subsequent cycle (else the alert is the same unbounded-repeat the
        poke bounding was meant to kill)."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "send_poke", return_value=True), \
                 patch.object(am, "_session_cyber_blocked", return_value=True), \
                 patch.object(am, "_route_failure_alert") as alert, \
                 patch.object(am, "_record_fire") as record_fire, \
                 patch.object(am, "latest_jsonl_mtime", return_value=9000):
                # Three consecutive cycles, cyber-blocked the whole time.
                for _ in range(3):
                    am._dispatch_poke(self._cfg(), "orchestrator", "p1c", "P", sp,
                                      bounded_enabled=True, max_ineffective=1)
            self.assertEqual(alert.call_count, 1, "alert fires once, then bounded-quiet")
            # T-3072: fire-tracking is NOT bounded the same way — the underlying
            # condition is still observably true every cycle, so it records all 3.
            self.assertEqual(record_fire.call_count, 3)
            record_fire.assert_called_with("orchestrator", "cyber-refusal")

    def test_escalation_routing_failure_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "send_poke", return_value=True), \
                 patch.object(am, "_session_cyber_blocked", return_value=True), \
                 patch.object(am, "_route_failure_alert", side_effect=RuntimeError("recovery down")), \
                 patch.object(am, "_record_fire") as record_fire, \
                 patch.object(am, "latest_jsonl_mtime", return_value=9000):
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1c", "P", sp,
                                       bounded_enabled=True, max_ineffective=1)
            self.assertEqual(rc, 0, "escalation routing failure must not wedge the monitor")
            self.assertTrue(am.read_poke_state(sp)["escalated"])  # still marks escalated
            # T-3072: fire-tracking runs BEFORE alert routing inside
            # _escalate_stuck, so a routing failure must not suppress it either.
            record_fire.assert_called_once_with("orchestrator", "cyber-refusal")

    def test_legacy_path_ignores_cyber_gate(self) -> None:
        """Ship-dark: with bounded off, the pre-poke gate does NOT run — legacy
        behavior is byte-for-byte unchanged even for a cyber-blocked session."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "send_poke", return_value=True) as poke, \
                 patch.object(am, "_session_cyber_blocked", return_value=True) as gate, \
                 patch.object(am, "_record_fire") as record_fire:
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1a", "P", sp,
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 0)
            poke.assert_called_once()  # legacy pokes regardless
            gate.assert_not_called()   # gate not even consulted when bounded off
            record_fire.assert_called_once_with("orchestrator", "p1a")  # T-3072 legacy path still records


class TestDispatchPokeBusyGuard(unittest.TestCase):
    """T-3599 — activity_monitor must DEFER (not exit-1) when the target pane is
    BUSY (generating / manage-view). Part B of the T-3579 fix: part A taught
    inbox_consumer.pane_idle_state to classify the manage-view footer as BUSY
    (False); this makes _dispatch_poke actually CONSULT it. Mirrors mailbox_wake's
    busy-defer (T-2681/T-3554): a generating pane QUEUES an injected submit rather
    than running it, so a poke there is doomed and a return-1 mislabels a benign
    busy pane as a unit-failure (pantheon-activity-monitor@<seat> OnFailure -> recovery).

    Every test patches `_pane_idle_probe` so the pane state is deterministic and no
    real tmux/sudo capture happens.
    """

    def _cfg(self) -> dict:
        return {"tmux_target_pane": "x:0", "sudo_user": "claude",
                "jsonl_dir": Path("/x"), "pantheon_config_path": Path("/x")}

    # --- Guard 1: decide-time skip -----------------------------------------
    def test_decide_time_busy_pane_defers_without_poking_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "_pane_idle_probe", return_value=False), \
                 patch.object(am, "send_poke") as poke, \
                 patch.object(am, "_record_fire") as record_fire:
                rc = am._dispatch_poke(self._cfg(), "artisan", "p1b", "P", sp,
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 0)          # deferred, NOT exit-1
            poke.assert_not_called()          # a doomed submit is never attempted
            record_fire.assert_not_called()   # a deferral is not a fire

    def test_decide_time_busy_pane_defers_without_poking_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "_pane_idle_probe", return_value=False), \
                 patch.object(am, "send_poke") as poke, \
                 patch.object(am, "_record_fire") as record_fire:
                rc = am._dispatch_poke(self._cfg(), "artisan", "p1b", "P", sp,
                                       bounded_enabled=True, max_ineffective=1)
            self.assertEqual(rc, 0)
            poke.assert_not_called()
            record_fire.assert_not_called()

    def test_decide_time_unknown_pane_still_pokes(self) -> None:
        """None = UNKNOWN must NOT skip a poke (fail-open) — a foreign/unreadable
        pane behaves exactly as before the guard."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "_pane_idle_probe", return_value=None), \
                 patch.object(am, "send_poke", return_value=True) as poke, \
                 patch.object(am, "_record_fire"):
                rc = am._dispatch_poke(self._cfg(), "artisan", "p1b", "P", sp,
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 0)
            poke.assert_called_once()  # UNKNOWN pokes as before

    def test_decide_time_idle_pane_pokes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "_pane_idle_probe", return_value=True), \
                 patch.object(am, "send_poke", return_value=True) as poke, \
                 patch.object(am, "_record_fire"):
                rc = am._dispatch_poke(self._cfg(), "artisan", "p1b", "P", sp,
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 0)
            poke.assert_called_once()  # idle pane pokes normally

    # --- Guard 2: send-fail soft-defer -------------------------------------
    def test_send_fail_on_busy_pane_soft_defers_legacy(self) -> None:
        """TOCTOU: pane reads not-busy at decide-time (here UNKNOWN), the send then
        FAILS and a re-probe shows the pane is now BUSY -> soft-defer (return 0),
        not exit-1. The poke lost nothing — the mail it re-injects stays queued."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "_pane_idle_probe", side_effect=[None, False]), \
                 patch.object(am, "send_poke", return_value=False), \
                 patch.object(am, "_record_fire") as record_fire:
                rc = am._dispatch_poke(self._cfg(), "artisan", "p1b", "P", sp,
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 0)
            record_fire.assert_not_called()  # a failed send is not a fire

    def test_send_fail_on_busy_pane_soft_defers_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "_pane_idle_probe", side_effect=[None, False]), \
                 patch.object(am, "send_poke", return_value=False), \
                 patch.object(am, "_session_cyber_blocked", return_value=False), \
                 patch.object(am, "latest_jsonl_mtime", return_value=5000), \
                 patch.object(am, "_record_fire") as record_fire:
                rc = am._dispatch_poke(self._cfg(), "artisan", "p1b", "P", sp,
                                       bounded_enabled=True, max_ineffective=1)
            self.assertEqual(rc, 0)
            record_fire.assert_not_called()

    def test_send_fail_on_idle_pane_still_exit1(self) -> None:
        """A genuinely IDLE pane that rejected the submit is the real T-2245
        send-liveness anomaly — keep exit-1 so it still alarms."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "_pane_idle_probe", side_effect=[True, True]), \
                 patch.object(am, "send_poke", return_value=False), \
                 patch.object(am, "_record_fire"):
                rc = am._dispatch_poke(self._cfg(), "artisan", "p1b", "P", sp,
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 1)

    def test_send_fail_on_unknown_pane_still_exit1(self) -> None:
        """UNKNOWN at re-probe is NOT proof-of-busy -> keep exit-1 (fail-safe: an
        unreadable pane must not silently swallow a real send failure)."""
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            with patch.object(am, "_pane_idle_probe", side_effect=[None, None]), \
                 patch.object(am, "send_poke", return_value=False), \
                 patch.object(am, "_record_fire"):
                rc = am._dispatch_poke(self._cfg(), "artisan", "p1b", "P", sp,
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 1)


class TestPaneIdleProbeKeepsAgentsBusy(unittest.TestCase):
    """T-4284: delivery reads a turn-ended pane with a running background agent as
    IDLE, but the P1.b poke is a NUDGE and must not prod an agent waiting on its own
    delegated work. Only the probe's background_agents_busy=True keeps that skip, and
    every T-3599 test above patches _pane_idle_probe wholesale, so this runs the real
    probe on live captures with only capture_pane stubbed."""

    TESTDATA = REPO_ROOT / "tools" / "testdata"

    def test_turn_ended_with_running_agent_reads_busy(self) -> None:
        cfg = {"tmux_target_pane": "x:0", "sudo_user": "claude"}
        for name in ("recovery_turn_ended_bg_agent_pane.txt", "orchestrator_turn_ended_bg_agents_pane.txt"):
            pane = (self.TESTDATA / name).read_text(encoding="utf-8")
            with self.subTest(name), patch.object(am, "capture_pane", return_value=pane):
                self.assertIs(am._pane_idle_probe(cfg), False)

    def test_capture_pane_escapes_flag_composes_minus_e(self) -> None:
        """T-4319: escapes=True must reach tmux as -e; the default must not."""
        done = subprocess.CompletedProcess([], 0, stdout="pane", stderr="")
        for escapes, want in ((True, True), (False, False)):
            with self.subTest(escapes=escapes), \
                    patch.object(am.subprocess, "run", return_value=done) as run:
                am.capture_pane("x:0", "claude", escapes=escapes)
                cmd = " ".join(run.call_args.args[0])
                self.assertEqual(bool(re.search(r"capture-pane -p -e -t x:0", cmd)), want, cmd)

    def test_probe_captures_with_escapes(self) -> None:
        """T-4284: the probe needs DIM to tell a prompt suggestion from typed text."""
        cfg = {"tmux_target_pane": "x:0", "sudo_user": "claude"}
        pane = (self.TESTDATA / "artisan_idle_ghost_suggestion_pane_ansi.txt").read_text(encoding="utf-8")
        with patch.object(am, "capture_pane", return_value=pane) as cap:
            self.assertIs(am._pane_idle_probe(cfg), True)
        self.assertEqual(cap.call_args.kwargs, {"escapes": True})


# ---------------------------------------------------------------------------
# await-state regex
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# count_actionable_tickets
# ---------------------------------------------------------------------------


def _ticket_text(target: str, depends_on: str = "[]") -> str:
    return (
        "---\n"
        f"id: T-9999\n"
        f"target: {target}\n"
        f"depends_on: {depends_on}\n"
        "---\n"
        "# test\n"
    )


class TestCountActionableTickets(unittest.TestCase):
    """Cross-account read via sudo + awk pipeline. Mock subprocess.run for fixtures."""

    def _mock_run(self, stdout: str, returncode: int = 0):
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    def test_counts_orchestrator_target_only(self) -> None:
        # awk pipeline output: one line per ticket "<path>|<target>|<depends_on>|<not_before>|<labels>"
        stdout = (
            "/r/tickets/inbox/T-1000.md|orchestrator|[]||\n"
            "/r/tickets/inbox/T-1001.md|recovery|[]||\n"
            "/r/tickets/active/T-1002.md|orchestrator|[]||\n"
            "/r/tickets/active/T-1003.md|specialist|[]||\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator", "orchestrator"}), "claude")
        self.assertEqual(count, 2)

    def test_skips_depends_on_unmet(self) -> None:
        stdout = (
            "/r/tickets/inbox/T-1000.md|orchestrator|[]||\n"
            "/r/tickets/inbox/T-1001.md|orchestrator|[T-0099]||\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)

    def test_skips_not_before_in_future(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stdout = (
            f"/r/tickets/inbox/T-1000.md|orchestrator|[]|{future}|\n"
            "/r/tickets/inbox/T-1001.md|orchestrator|[]||\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)

    def test_counts_not_before_in_past(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stdout = f"/r/tickets/inbox/T-1000.md|orchestrator|[]|{past}|\n"
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)

    def test_counts_not_before_absent(self) -> None:
        stdout = (
            "/r/tickets/inbox/T-1000.md|orchestrator|[]||\n"
            "/r/tickets/inbox/T-1001.md|orchestrator|[]|null|\n"
            "/r/tickets/inbox/T-1002.md|orchestrator|[]|~|\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 3)

    def test_date_only_not_before_parsed_as_midnight_utc(self) -> None:
        future_date = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        past_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        stdout = (
            f"/r/tickets/inbox/T-1000.md|orchestrator|[]|{future_date}|\n"
            f"/r/tickets/inbox/T-1001.md|orchestrator|[]|{past_date}|\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)

    def test_unparseable_not_before_counted(self) -> None:
        # Defensive: unparseable values fall back to existing-behavior (counted).
        stdout = "/r/tickets/inbox/T-1000.md|orchestrator|[]|garbage-not-a-date|\n"
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)

    def test_returns_zero_on_subprocess_failure(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run("", returncode=1)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 0)

    def test_scan_returns_none_on_subprocess_failure(self) -> None:
        """T-3570: the underlying scan distinguishes a FAILED probe (None) from
        a confirmed-empty queue (0). The count wrapper coerces None -> 0."""
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run("", returncode=1)
            self.assertIsNone(
                _scan_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
            )

    def test_scan_returns_none_on_timeout(self) -> None:
        with patch("tools.activity_monitor.subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertIsNone(
                _scan_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
            )

    def test_scan_returns_zero_int_on_confirmed_empty(self) -> None:
        """rc=0 with no matching tickets is a CONFIRMED-empty queue: 0, not
        None. This is the positive can't-fire signal RC2 relies on."""
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run("", returncode=0)
            n = _scan_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(n, 0)
        self.assertIsNotNone(n)

    def test_returns_zero_on_empty_output(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run("")
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 0)

    def test_returns_zero_on_subprocess_error(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd="find", timeout=30)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 0)

    def test_sudo_user_threads_into_argv(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run("")
            count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude-research")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["sudo", "-nu", "claude-research"])

    def test_skips_interactive_required_label(self) -> None:
        # Ticket with interactive-required label must NOT be counted even when
        # target matches, deps are empty, and not_before is absent.
        stdout = "/r/tickets/inbox/T-1000.md|orchestrator|[]||[activity-monitor, interactive-required]\n"
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 0)

    def test_counts_non_interactive_label(self) -> None:
        # Ticket without interactive-required label must be counted normally.
        stdout = "/r/tickets/inbox/T-1000.md|orchestrator|[]||[activity-monitor, p1a]\n"
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)

    def test_interactive_required_in_mixed_queue(self) -> None:
        # Queue: one interactive-required (skipped) + one plain (counted) -> 1.
        stdout = (
            "/r/tickets/inbox/T-1000.md|orchestrator|[]||[interactive-required]\n"
            "/r/tickets/inbox/T-1001.md|orchestrator|[]||[p1a, predicate-fix]\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)

    def test_queue_states_default_counts_inbox_and_active(self) -> None:
        # T-3557(e) baseline / orchestrator: the DEFAULT queue_states counts BOTH inbox
        # and active — an idle inbox is a real "why aren't you draining" signal
        # for the always-on HO. This guards that the default is unchanged.
        stdout = (
            "/r/tickets/inbox/T-1000.md|orchestrator|[]||\n"
            "/r/tickets/active/T-1001.md|orchestrator|[]||\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 2)

    def test_queue_states_active_only_skips_inbox(self) -> None:
        # T-3557(e) sub-orch: with queue_states=("active",) an UNCLAIMED inbox
        # ticket does NOT count. This is exactly the mason idle-with-inbox-only
        # false-fire (T-3457 sat unclaimed in inbox) reduced to a no-fire:
        # queue_count 0 → p1a/p1b bail before ever escalating.
        stdout = "/r/tickets/inbox/T-3457.md|mason|[]||[adr-088, build]\n"
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(
                Path("/r"), frozenset({"mason"}), "suborch1", queue_states=("active",))
        self.assertEqual(count, 0)

    def test_queue_states_active_only_counts_active(self) -> None:
        # T-3557(e): NOT vacuous — an ACTIVE (claimed, in-progress) ticket still
        # counts under active-only, so a seat wedged MID-WORK still fires. This is
        # the reliability property the tune must preserve (real-queue → fire); it
        # falsifies an over-narrowing that would blanket-zero the sub-orch queue.
        stdout = (
            "/r/tickets/inbox/T-3457.md|mason|[]||[build]\n"
            "/r/tickets/active/T-9000.md|mason|[]||[build]\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(
                Path("/r"), frozenset({"mason"}), "suborch1", queue_states=("active",))
        self.assertEqual(count, 1)


# ---------------------------------------------------------------------------
# bridge inbound age parser
# ---------------------------------------------------------------------------


class TestOperatorLastInboundAge(unittest.TestCase):
    def test_recent_inbound_short_age(self) -> None:
        now = datetime.now(timezone.utc)
        iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        # Reformat to bridge style (+00:00 instead of +0000)
        iso = iso[:-2] + ":" + iso[-2:]
        health = {"last_inbound_at": {"orchestrator": iso}}
        age = operator_last_inbound_age_seconds(health, "orchestrator")
        self.assertIsNotNone(age)
        self.assertLess(age, 10)

    def test_old_inbound_long_age(self) -> None:
        health = {"last_inbound_at": {"orchestrator": "2026-01-01T00:00:00+00:00"}}
        age = operator_last_inbound_age_seconds(health, "orchestrator")
        self.assertIsNotNone(age)
        self.assertGreater(age, 30 * 24 * 3600)  # > 30 days

    def test_missing_key_returns_none(self) -> None:
        health = {"last_inbound_at": {}}
        self.assertIsNone(operator_last_inbound_age_seconds(health, "orchestrator"))

    def test_malformed_string_returns_none(self) -> None:
        health = {"last_inbound_at": {"orchestrator": "not-a-timestamp"}}
        self.assertIsNone(operator_last_inbound_age_seconds(health, "orchestrator"))


# ---------------------------------------------------------------------------
# check_p1a
# ---------------------------------------------------------------------------


class TestCheckP1a(unittest.TestCase):
    def _cfg(self, repo_path: Path) -> dict:
        return {
            "tmux_target_pane": "x:0",
            "sudo_user": "claude",
            "jsonl_dir": Path("/x"),
            "ledger_agent_id": "orchestrator",
            "ticket_targets": frozenset({"orchestrator"}),
            "pantheon_config_path": repo_path,
            "bridge_inbound_key": "orchestrator",
        }

    def test_idle_with_queue_fires(self) -> None:
        old_ts = int(time.time()) - 7200  # 2h ago
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=old_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=old_ts), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(Path("/x")), idle_threshold_seconds=3600)
        self.assertTrue(fired)

    def test_recent_ledger_suppresses_fire(self) -> None:
        """Recent ledger entry by Orchestrator suppresses fire."""
        recent_ts = int(time.time()) - 60  # 1m ago
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=recent_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=0), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(Path("/x")), idle_threshold_seconds=3600)
        self.assertFalse(fired)

    def test_recent_jsonl_mtime_suppresses_fire(self) -> None:
        """Jsonl mtime within threshold suppresses fire even when ledger is old.

        Catches the b9-orchestration case: Orchestrator is producing tokens (jsonl mtime
        bumping) but not ledger-ing during mid-cycle.
        """
        recent_jsonl = int(time.time()) - 30  # 30s ago
        old_ts = int(time.time()) - 7200
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=old_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=recent_jsonl), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(Path("/x")), idle_threshold_seconds=3600)
        self.assertFalse(fired)

    def test_idle_but_empty_queue_does_not_fire(self) -> None:
        old_ts = int(time.time()) - 7200
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=0), \
             patch.object(am, "latest_jsonl_mtime", return_value=0), \
             patch.object(am, "count_actionable_tickets", return_value=0):
            fired = am.check_p1a(self._cfg(Path("/x")), idle_threshold_seconds=3600)
        self.assertFalse(fired)

    def test_no_activity_signal_fail_open(self) -> None:
        # All probes return None → fail-open (don't fire).
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=None), \
             patch.object(am, "latest_jsonl_mtime", return_value=None), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(Path("/x")), idle_threshold_seconds=3600)
        self.assertFalse(fired)

    def test_commit_ts_not_consulted_per_t0283(self) -> None:
        """Per T-0283: latest_commit_ts was dropped because it's repo-wide and
        not Orchestrator-tagged. Recovery-side commits used to keep Orchestrator looking active.

        This test asserts the function no longer calls latest_commit_ts even
        if it's monkey-patched to return recent activity.
        """
        old_ts = int(time.time()) - 7200
        with patch.object(am, "latest_commit_ts") as commit_probe, \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=old_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=old_ts), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            commit_probe.return_value = int(time.time()) - 60  # recent, but irrelevant
            fired = am.check_p1a(self._cfg(Path("/x")), idle_threshold_seconds=3600)
        # P1.a fires because ledger+jsonl are old, regardless of commit.
        self.assertTrue(fired)
        # latest_commit_ts must NOT have been called.
        commit_probe.assert_not_called()


# ---------------------------------------------------------------------------
# T-0644: blocked_by:event exclusion + terminal-state cooldown stretch.
# ---------------------------------------------------------------------------


class TestCountActionableTicketsT0644(unittest.TestCase):
    """T-0644: extend count_actionable_tickets to also exclude tickets with
    `blocked_by: event` frontmatter (event-driven holds gated on a structural
    witness, not on HO-availability).
    """

    def _mock_run(self, stdout: str, returncode: int = 0):
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    def test_skips_blocked_by_event(self) -> None:
        # New 6-field shape: path|target|deps|nb|labels|blocked_by
        stdout = (
            "/r/tickets/inbox/T-1000.md|orchestrator|[]|||\n"
            "/r/tickets/inbox/T-1001.md|orchestrator|[]|||event\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1, "blocked_by:event must be excluded from count")

    def test_blocked_by_other_values_counted(self) -> None:
        # Only the literal 'event' value triggers the exclusion. depends_on
        # (which is a separate gate already enforced) shouldn't double-trip.
        stdout = (
            "/r/tickets/inbox/T-1000.md|orchestrator|[]||||\n"
            "/r/tickets/inbox/T-1001.md|orchestrator|[]||labels|other\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 2)

    def test_backward_compat_5_field_lines(self) -> None:
        # Pre-T-0644 fixtures (5-field, no blocked_by column) must still parse.
        stdout = "/r/tickets/inbox/T-1000.md|orchestrator|[]||\n"
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            count = count_actionable_tickets(Path("/r"), frozenset({"orchestrator"}), "claude")
        self.assertEqual(count, 1)


class TestLatestTerminalStateLedger(unittest.TestCase):
    """T-0644: scan ledger for HO-tagged entries whose action contains
    'terminal_state' or 'queue_audit'."""

    def _mock_run(self, stdout: str, returncode: int = 0):
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    def test_returns_ts_of_terminal_state_entry(self) -> None:
        # Newest-first (the tool tacs the file).
        stdout = (
            '{"agent":"orchestrator","ts":"2026-05-28T03:00:00Z","action":"ho_queue_terminal_state_audit"}\n'
            '{"agent":"orchestrator","ts":"2026-05-27T22:00:00Z","action":"t0123_resolved_x"}\n'
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_terminal_state_ledger_ts_for_agent(Path("/r"), "claude", "orchestrator")
        from datetime import datetime, timezone
        expected = int(datetime(2026, 5, 28, 3, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ts, expected)

    def test_returns_none_when_no_terminal_state_entry(self) -> None:
        stdout = (
            '{"agent":"orchestrator","ts":"2026-05-28T03:00:00Z","action":"t0644_filed"}\n'
            '{"agent":"orchestrator","ts":"2026-05-27T22:00:00Z","action":"t0123_resolved_x"}\n'
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_terminal_state_ledger_ts_for_agent(Path("/r"), "claude", "orchestrator")
        self.assertIsNone(ts)

    def test_queue_audit_action_also_matches(self) -> None:
        stdout = '{"agent":"orchestrator","ts":"2026-05-28T01:00:00Z","action":"ho_queue_audit_post_arc"}\n'
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_terminal_state_ledger_ts_for_agent(Path("/r"), "claude", "orchestrator")
        self.assertIsNotNone(ts)

    def test_other_agent_entries_skipped(self) -> None:
        stdout = '{"agent":"recovery","ts":"2026-05-28T01:00:00Z","action":"recovery_terminal_state_audit"}\n'
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_terminal_state_ledger_ts_for_agent(Path("/r"), "claude", "orchestrator")
        self.assertIsNone(ts)

    def test_command_reads_ledger_union_subfolder_shards(self) -> None:
        # T-0148: the probe must read across the per-week subfolder shards
        # (ledger/<seg>/<seg>.jsonl, T-4517) plus the legacy flat file, not just {year}.jsonl.
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run("")
            am.latest_terminal_state_ledger_ts_for_agent(Path("/r"), "claude", "orchestrator")
        shell_cmd = run.call_args[0][0][-1]
        self.assertIn("/r/ledger/*/", shell_cmd)
        self.assertIn("/r/ledger/2026.jsonl", shell_cmd)
        self.assertIn("tac", shell_cmd)

    def test_returns_max_ts_not_first_when_stale_emitted_first(self) -> None:
        # T-0148 regression (review finding #1): the union emits legacy lines
        # last → tac yields legacy FIRST, so in a transition window a STALE
        # (older) matching entry can appear before the newest. The probe must
        # return max(ts), NOT first-match. OLD entry first, NEW entry second.
        from datetime import datetime, timezone
        stdout = (
            '{"agent":"orchestrator","ts":"2026-05-01T00:00:00Z","action":"ho_queue_terminal_state_audit"}\n'
            '{"agent":"orchestrator","ts":"2026-05-28T12:00:00Z","action":"ho_queue_terminal_state_audit"}\n'
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_terminal_state_ledger_ts_for_agent(Path("/r"), "claude", "orchestrator")
        expected = int(datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ts, expected)


class TestLatestLedgerEntryTs(unittest.TestCase):
    """T-0148: latest_ledger_entry_ts_for_agent reads the ledger union and must
    return the newest matching ts regardless of source-concatenation order."""

    def _mock_run(self, stdout: str, returncode: int = 0):
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    def test_returns_max_ts_not_first_when_stale_emitted_first(self) -> None:
        # Review finding #1, sibling probe: OLD entry first, NEW entry second →
        # must return the NEW ts (max), not the first-after-tac stale one.
        from datetime import datetime, timezone
        stdout = (
            '{"agent":"orchestrator","ts":"2026-05-01T00:00:00Z","action":"x"}\n'
            '{"agent":"orchestrator","ts":"2026-05-29T09:00:00Z","action":"y"}\n'
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_ledger_entry_ts_for_agent(Path("/r"), "claude", "orchestrator")
        expected = int(datetime(2026, 5, 29, 9, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ts, expected)

    def test_other_agent_skipped_returns_none(self) -> None:
        stdout = '{"agent":"recovery","ts":"2026-05-29T09:00:00Z","action":"y"}\n'
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._mock_run(stdout)
            ts = am.latest_ledger_entry_ts_for_agent(Path("/r"), "claude", "orchestrator")
        self.assertIsNone(ts)


class TestLedgerUnionTacCmd(unittest.TestCase):
    """T-0148: _ledger_union_tac_cmd shell-string shape."""

    def test_includes_subfolder_glob_and_legacy_and_tac(self) -> None:
        cmd = am._ledger_union_tac_cmd(Path("/repo"), 500)
        self.assertIn("/repo/ledger/*/", cmd)
        self.assertIn("/repo/ledger/2026.jsonl", cmd)
        self.assertIn("| tac | head -n 500", cmd)

    def test_n_parameter_threaded_through(self) -> None:
        cmd = am._ledger_union_tac_cmd(Path("/repo"), 42)
        self.assertIn("head -n 42", cmd)

    def test_non_shard_subdir_cannot_crowd_out_the_newest_shard(self) -> None:
        """T-4517: ledger/appetite/<seg>.jsonl sorts after ledger/2026-W40/, so a
        `*/*.jsonl` glob put 1000 appetite lines last and `tac | head -500` saw
        nothing else. Only <seg>/<seg>.jsonl is a shard (ledger_shard's rule)."""
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / "ledger"
            (ledger / "2026-W40").mkdir(parents=True)
            (ledger / "appetite").mkdir()
            newest = {"id": "x", "agent": "orchestrator",
                      "action": "ho_queue_terminal_state_audit", "ts": "2026-10-01T06:30:56Z"}
            (ledger / "2026-W40" / "2026-W40.jsonl").write_text(json.dumps(newest) + "\n")
            (ledger / "appetite" / "2026-W40.jsonl").write_text(
                "".join(json.dumps({"appetite_row": i}) + "\n" for i in range(600)))
            out = subprocess.run(["bash", "-c", am._ledger_union_tac_cmd(Path(td), 500)],
                                 capture_output=True, text=True, check=False).stdout
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual(lines[0].get("action"), "ho_queue_terminal_state_audit")
        self.assertFalse(any("appetite_row" in row for row in lines))


class TestCheckP1aTerminalStateStretch(unittest.TestCase):
    """T-0644: when the latest agent ledger entry is a terminal-state audit,
    P1.a uses ACTIVITY_MONITOR_P1A_TERMINAL_STATE_IDLE_SECONDS instead of
    the normal idle threshold, so we don't re-poke HO every cycle on the
    same terminal-state ack."""

    def _cfg(self) -> dict:
        return {
            "tmux_target_pane": "x:0",
            "sudo_user": "claude",
            "jsonl_dir": Path("/x"),
            "ledger_agent_id": "orchestrator",
            "ticket_targets": frozenset({"orchestrator"}),
            "pantheon_config_path": Path("/x"),
            "bridge_inbound_key": "orchestrator",
        }

    def test_terminal_state_ack_within_4hr_suppresses_fire(self) -> None:
        # ledger entry 90 min ago (>60 min default threshold; would normally fire).
        # But it's a terminal-state ack, so stretched threshold (4hr) applies → no fire.
        ack_ts = int(time.time()) - 90 * 60
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=ack_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=0), \
             patch.object(am, "latest_terminal_state_ledger_ts_for_agent", return_value=ack_ts), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(), idle_threshold_seconds=3600)
        self.assertFalse(fired)

    def test_terminal_state_ack_past_4hr_does_fire(self) -> None:
        # ack was 5hr ago → past stretched threshold → fire.
        ack_ts = int(time.time()) - 5 * 60 * 60
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=ack_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=0), \
             patch.object(am, "latest_terminal_state_ledger_ts_for_agent", return_value=ack_ts), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(), idle_threshold_seconds=3600)
        self.assertTrue(fired)

    def test_jsonl_activity_after_terminal_state_uses_normal_threshold(self) -> None:
        # ack was 5hr ago, but jsonl mtime is 90 min ago — the latest activity
        # is the jsonl write, which is NOT a terminal-state ack. Use normal
        # 60min threshold; 90min idle → fire.
        old_ack_ts = int(time.time()) - 5 * 60 * 60
        jsonl_ts = int(time.time()) - 90 * 60
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=old_ack_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=jsonl_ts), \
             patch.object(am, "latest_terminal_state_ledger_ts_for_agent", return_value=old_ack_ts), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(), idle_threshold_seconds=3600)
        self.assertTrue(fired)

    def test_no_terminal_state_uses_normal_threshold(self) -> None:
        # Old ledger, no terminal-state ack at all → normal 60min threshold.
        old_ts = int(time.time()) - 90 * 60
        with patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=old_ts), \
             patch.object(am, "latest_jsonl_mtime", return_value=0), \
             patch.object(am, "latest_terminal_state_ledger_ts_for_agent", return_value=None), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1a(self._cfg(), idle_threshold_seconds=3600)
        self.assertTrue(fired)


# ---------------------------------------------------------------------------
# check_p1b
# ---------------------------------------------------------------------------


class TestCheckP1b(unittest.TestCase):
    def _cfg(self, repo_path: Path) -> dict:
        return {
            "tmux_target_pane": "x:0",
            "sudo_user": "claude",
            "jsonl_dir": Path("/x"),
            "ledger_agent_id": "orchestrator",
            "ticket_targets": frozenset({"orchestrator"}),
            "pantheon_config_path": repo_path,
            "bridge_inbound_key": "orchestrator",
        }

    def setUp(self) -> None:
        # T-3404: check_p1b now consults the terminal-state ledger ack on the
        # fire path. Default it to None (no ack) so tests not exercising it don't
        # hit the real ledger subprocess; the suppression tests below override it.
        p = patch.object(am, "latest_terminal_state_ledger_ts_for_agent", return_value=None)
        p.start()
        self.addCleanup(p.stop)
        # T-3709: check_p1b now also consults the recent-write-activity gate on
        # the fire path. Default it to False (no recent commit → don't suppress)
        # so tests not exercising it stay hermetic (no real git subprocess) and
        # keep their expected fire behavior; the T-3709 tests below override it.
        w = patch.object(am, "agent_committed_recently", return_value=False)
        w.start()
        self.addCleanup(w.stop)

    def test_all_three_predicates_fire(self) -> None:
        with patch.object(am, "latest_telegram_inbound_ts", return_value=1700000000), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_ends_without_tool_use", return_value=True), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertTrue(fired)

    def test_operator_recent_inbound_suppresses(self) -> None:
        # the operator inbound = NOW → operator-present → suppress before signals evaluated.
        import time as _time
        with patch.object(am, "latest_telegram_inbound_ts", return_value=int(_time.time())), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertFalse(fired)

    def test_no_await_state_suppresses(self) -> None:
        with patch.object(am, "latest_telegram_inbound_ts", return_value=1700000000), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_ends_without_tool_use", return_value=False), \
             patch.object(am, "latest_assistant_interactive_tool_use", return_value=None), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertFalse(fired)

    def test_empty_queue_suppresses(self) -> None:
        with patch.object(am, "latest_telegram_inbound_ts", return_value=1700000000), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_ends_without_tool_use", return_value=True), \
             patch.object(am, "count_actionable_tickets", return_value=0):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertFalse(fired)

    def test_operator_ts_unavailable_fail_closed_and_evaluates_downstream(self) -> None:
        """T-3075: when latest_telegram_inbound_ts returns None (no marker in
        jsonl, or probe error), Gate-1 fail-CLOSED — treat as operator-absent
        AT-THRESHOLD and proceed to evaluate Signals 1-2 + queue-count.
        Prior semantics (fail-open, silent bail) suppressed the whole poke
        for 10 weeks after pantheon-bridge decom."""
        # Gate-1 None → treated as absent; downstream signals decide the fire.
        # Below: structural-await + queue-count both hit → poke fires.
        with patch.object(am, "latest_telegram_inbound_ts", return_value=None), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_ends_without_tool_use", return_value=True), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertTrue(fired)

    def test_operator_ts_unavailable_fail_closed_still_gated_by_downstream(self) -> None:
        """T-3075: fail-CLOSED on Gate-1 doesn't force the poke — downstream
        signals still gate. Here no await-state + no interactive-tool → no poke."""
        with patch.object(am, "latest_telegram_inbound_ts", return_value=None), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_ends_without_tool_use", return_value=False), \
             patch.object(am, "latest_assistant_interactive_tool_use", return_value=None), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertFalse(fired)

    # ---- T-0297: tool_use interactive-menu await-state ----

    def test_tool_use_askuserquestion_fires_via_t0297(self) -> None:
        """Latest assistant turn = AskUserQuestion tool_use → P1.b fires even
        when the structural signal is False (turn ends in tool_use, not text)."""
        with patch.object(am, "latest_telegram_inbound_ts", return_value=1700000000), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_ends_without_tool_use", return_value=False), \
             patch.object(am, "latest_assistant_interactive_tool_use", return_value="AskUserQuestion"), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertTrue(fired)

    def test_tool_use_enterplanmode_fires_via_t0297(self) -> None:
        """EnterPlanMode tool_use also triggers await-state per the allowlist."""
        with patch.object(am, "latest_telegram_inbound_ts", return_value=1700000000), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_ends_without_tool_use", return_value=False), \
             patch.object(am, "latest_assistant_interactive_tool_use", return_value="EnterPlanMode"), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertTrue(fired)

    def test_structural_signal_alone_still_fires(self) -> None:
        """Structural signal continues to work standalone; tool_use returns None."""
        with patch.object(am, "latest_telegram_inbound_ts", return_value=1700000000), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_ends_without_tool_use", return_value=True), \
             patch.object(am, "latest_assistant_interactive_tool_use", return_value=None), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertTrue(fired)

    def test_neither_signal_does_not_fire(self) -> None:
        """Structural signal False + tool_use is None → no await-state → no fire."""
        with patch.object(am, "latest_telegram_inbound_ts", return_value=1700000000), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")), \
             patch.object(am, "latest_assistant_ends_without_tool_use", return_value=False), \
             patch.object(am, "latest_assistant_interactive_tool_use", return_value=None), \
             patch.object(am, "count_actionable_tickets", return_value=1):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertFalse(fired)

    # ---- T-3404: terminal-state ack suppresses P1.b (was P1.a-only) ----

    def _firing_ctx(self):
        """All P1.b fire conditions met (the operator absent + await + queue)."""
        import contextlib
        cm = contextlib.ExitStack()
        cm.enter_context(patch.object(am, "latest_telegram_inbound_ts", return_value=1700000000))
        cm.enter_context(patch.object(am, "find_latest_jsonl", return_value=Path("/x/sess.jsonl")))
        cm.enter_context(patch.object(am, "latest_assistant_ends_without_tool_use", return_value=True))
        cm.enter_context(patch.object(am, "count_actionable_tickets", return_value=1))
        return cm

    def test_terminal_state_ack_within_window_suppresses(self) -> None:
        """HO logged a terminal-state ack 30min ago and it's still the latest
        LEDGER entry → P1.b suppressed even though all fire conditions hold. This
        is the churn fix: the 'hold stable' text turns don't write the ledger, so
        the ack stays latest and the suppression persists across cycles."""
        ack_ts = int(time.time()) - 30 * 60
        with self._firing_ctx(), \
             patch.object(am, "latest_terminal_state_ledger_ts_for_agent", return_value=ack_ts), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=ack_ts):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertFalse(fired)

    def test_terminal_state_ack_past_window_does_fire(self) -> None:
        """Ack was 5hr ago (past the 4hr window) → re-fire (a fresh 'still holding?'
        check)."""
        ack_ts = int(time.time()) - 5 * 60 * 60
        with self._firing_ctx(), \
             patch.object(am, "latest_terminal_state_ledger_ts_for_agent", return_value=ack_ts), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=ack_ts):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertTrue(fired)

    def test_real_ledger_work_after_ack_supersedes_and_fires(self) -> None:
        """A non-terminal ledger entry AFTER the ack (real new HO work) means the
        ack is no longer the latest ledger activity → suppression lifts → fire."""
        ack_ts = int(time.time()) - 30 * 60
        newer_work_ts = int(time.time()) - 5 * 60
        with self._firing_ctx(), \
             patch.object(am, "latest_terminal_state_ledger_ts_for_agent", return_value=ack_ts), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=newer_work_ts):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertTrue(fired)

    # ---- T-3709: recent-write-activity suppresses P1.b ----

    def test_recent_write_activity_suppresses(self) -> None:
        """All P1.b fire conditions hold (the operator absent + await + queue), but the
        agent committed git-visible output within the write-activity window →
        busy-productive, not idle-waiting → suppress. The T-3709 fix: a
        continuously-working HO is no longer indistinguishable from an idle one."""
        with self._firing_ctx(), \
             patch.object(am, "agent_committed_recently", return_value=True):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertFalse(fired)

    def test_no_recent_write_activity_still_fires(self) -> None:
        """All fire conditions hold and NO recent commit → genuinely idle-waiting
        → poke still fires. Confirms the gate is AND-NOT (suppresses only on
        positive write-activity), not a blanket new suppression."""
        with self._firing_ctx(), \
             patch.object(am, "agent_committed_recently", return_value=False):
            fired = am.check_p1b(self._cfg(Path("/x")), "http://bridge", 15 * 60)
        self.assertTrue(fired)


class TestAgentCommittedRecently(unittest.TestCase):
    """T-3709 helper unit tests — the recent-write-activity git probe.

    Fail-CLOSED contract: any error/timeout/nonzero-rc must return False (do NOT
    suppress → preserve the P1.b fire safety-net). Only a clean rc=0 with
    non-empty stdout (a matching commit hash) counts as 'recently committed'.
    """

    def _run(self, *, returncode=0, stdout="", raises=None) -> bool:
        with patch.object(am.subprocess, "run") as run:
            if raises is not None:
                run.side_effect = raises
            else:
                run.return_value = MagicMock(returncode=returncode, stdout=stdout)
            return am.agent_committed_recently(Path("/repo"), "claude", "orchestrator", 900)

    def test_nonempty_output_is_recent(self) -> None:
        self.assertTrue(self._run(returncode=0, stdout="abc123def\n"))

    def test_empty_output_is_not_recent(self) -> None:
        self.assertFalse(self._run(returncode=0, stdout="\n"))

    def test_nonzero_returncode_fails_closed(self) -> None:
        self.assertFalse(self._run(returncode=128, stdout="fatal: not a git repo\n"))

    def test_timeout_fails_closed(self) -> None:
        self.assertFalse(self._run(raises=subprocess.TimeoutExpired(cmd="git", timeout=30)))

    def test_oserror_fails_closed(self) -> None:
        self.assertFalse(self._run(raises=OSError("boom")))

    def test_author_and_since_passed_to_git(self) -> None:
        """The probe must author-filter (agent's own commits only) and window-
        bound via --since, so a pulled peer commit or an old commit doesn't count."""
        with patch.object(am.subprocess, "run") as run:
            run.return_value = MagicMock(returncode=0, stdout="")
            am.agent_committed_recently(Path("/repo"), "claude", "orchestrator", 900)
            argv = run.call_args[0][0]
        self.assertIn("--author=orchestrator", argv)
        self.assertIn("--since=900 seconds ago", argv)


class TestLatestAssistantInteractiveToolUse(unittest.TestCase):
    """T-0297 helper unit tests."""

    def _jsonl_result(self, lines, returncode=0):
        return MagicMock(returncode=returncode, stdout="\n".join(lines), stderr="")

    def test_askuserquestion_detected(self):
        entry = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T22:38:42.899Z",
            "message": {"content": [
                {"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": []}}
            ]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            name = am.latest_assistant_interactive_tool_use(Path("/x.jsonl"), "claude")
        self.assertEqual(name, "AskUserQuestion")

    def test_enterplanmode_detected(self):
        entry = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T22:38:42.899Z",
            "message": {"content": [
                {"type": "tool_use", "name": "EnterPlanMode", "input": {}}
            ]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            name = am.latest_assistant_interactive_tool_use(Path("/x.jsonl"), "claude")
        self.assertEqual(name, "EnterPlanMode")

    def test_non_blocking_tool_use_returns_none(self):
        """Bash/Read/etc. are not user-blocking — return None even though it's a tool_use."""
        entry = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T22:38:42.899Z",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
            ]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            name = am.latest_assistant_interactive_tool_use(Path("/x.jsonl"), "claude")
        self.assertIsNone(name)

    def test_text_only_assistant_returns_none(self):
        entry = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T22:38:42.899Z",
            "message": {"content": [
                {"type": "text", "text": "All done."}
            ]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            name = am.latest_assistant_interactive_tool_use(Path("/x.jsonl"), "claude")
        self.assertIsNone(name)

    def test_subprocess_failure_returns_none(self):
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([], returncode=1)
            name = am.latest_assistant_interactive_tool_use(Path("/x.jsonl"), "claude")
        self.assertIsNone(name)

    def test_picks_most_recent_assistant(self):
        """tac order — first assistant entry in tac output is the most recent
        in file order. An older AskUserQuestion shouldn't fire if the most-recent
        assistant turn is text."""
        recent_text = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T23:00:00.000Z",
            "message": {"content": [{"type": "text", "text": "Implementing now."}]},
        })
        older_aq = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T22:00:00.000Z",
            "message": {"content": [{"type": "tool_use", "name": "AskUserQuestion", "input": {}}]},
        })
        # tac emits most-recent file line first.
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([recent_text, older_aq])
            name = am.latest_assistant_interactive_tool_use(Path("/x.jsonl"), "claude")
        self.assertIsNone(name)  # Most-recent assistant was text, not tool_use.


class TestLatestAssistantEndsWithoutToolUse(unittest.TestCase):
    """T-3071 structural Signal-1: True iff the latest assistant turn's final
    content-block has type=='text'. Replaces language-regex AWAIT_STATE_REGEX."""

    def _jsonl_result(self, lines, returncode=0):
        return MagicMock(returncode=returncode, stdout="\n".join(lines), stderr="")

    def test_text_only_turn_is_await(self) -> None:
        entry = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Implementation landed."}]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            result = am.latest_assistant_ends_without_tool_use(Path("/x.jsonl"), "claude")
        self.assertTrue(result)

    def test_tool_use_only_turn_is_not_await(self) -> None:
        entry = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            result = am.latest_assistant_ends_without_tool_use(Path("/x.jsonl"), "claude")
        self.assertFalse(result)

    def test_mixed_tool_use_last_is_not_await(self) -> None:
        entry = json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "Reading the file first."},
                {"type": "tool_use", "name": "Read", "input": {}},
            ]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            result = am.latest_assistant_ends_without_tool_use(Path("/x.jsonl"), "claude")
        self.assertFalse(result)

    def test_mixed_text_last_is_await(self) -> None:
        entry = json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {}},
                {"type": "text", "text": "Done — smoke green."},
            ]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            result = am.latest_assistant_ends_without_tool_use(Path("/x.jsonl"), "claude")
        self.assertTrue(result)

    def test_no_assistant_turn_in_window_is_not_await(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([])
            result = am.latest_assistant_ends_without_tool_use(Path("/x.jsonl"), "claude")
        self.assertFalse(result)

    def test_subprocess_failure_is_not_await(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([], returncode=1)
            result = am.latest_assistant_ends_without_tool_use(Path("/x.jsonl"), "claude")
        self.assertFalse(result)

    def test_no_text_block_anywhere_is_not_await(self) -> None:
        """Final block isn't text (thinking-only turn) → fail-closed, no fire."""
        entry = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": "considering..."}]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            result = am.latest_assistant_ends_without_tool_use(Path("/x.jsonl"), "claude")
        self.assertFalse(result)

    def test_empty_content_list_is_not_await(self) -> None:
        entry = json.dumps({"type": "assistant", "message": {"content": []}})
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([entry])
            result = am.latest_assistant_ends_without_tool_use(Path("/x.jsonl"), "claude")
        self.assertFalse(result)

    def test_picks_most_recent_assistant(self) -> None:
        """tac order — first assistant entry in tac output is most-recent in
        file order. An older tool_use-terminal turn shouldn't suppress await
        if the most-recent assistant turn ends in text."""
        recent_text = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Standing by."}]},
        })
        older_tool_use = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([recent_text, older_tool_use])
            result = am.latest_assistant_ends_without_tool_use(Path("/x.jsonl"), "claude")
        self.assertTrue(result)


class TestDetectCyberRefusal(unittest.TestCase):
    """T-1363 increment 3 — cyber-safeguard refusal detector. Fixture mirrors the
    REAL 2026-06-22 Specialist refusal jsonl (stop_details.category == 'cyber')."""

    def _jsonl_result(self, lines, returncode=0):
        return MagicMock(returncode=returncode, stdout="\n".join(lines), stderr="")

    # The real captured shape (explanation prose trimmed — we key on structure).
    CYBER_REFUSAL = {
        "type": "assistant",
        "isApiErrorMessage": True,
        "message": {
            "role": "assistant", "stop_reason": "refusal",
            "stop_details": {"type": "refusal", "category": "cyber",
                             "explanation": "...blocked under Anthropic's Usage Policy..."},
            "content": [{"type": "text", "text": "..."}],
        },
    }

    def test_pure_classifier_true_on_cyber(self):
        self.assertTrue(am.is_cyber_refusal_message(self.CYBER_REFUSAL))

    def test_pure_classifier_false_on_other_refusal_category(self):
        msg = json.loads(json.dumps(self.CYBER_REFUSAL))
        msg["message"]["stop_details"]["category"] = "harmful_content"
        self.assertFalse(am.is_cyber_refusal_message(msg),
                         "only the cyber category routes to restart")

    def test_pure_classifier_false_on_non_refusal(self):
        ok = {"type": "assistant", "message": {"stop_reason": "end_turn",
              "content": [{"type": "text", "text": "done"}]}}
        self.assertFalse(am.is_cyber_refusal_message(ok))

    def test_pure_classifier_false_on_missing_stop_details(self):
        msg = {"type": "assistant", "message": {"stop_reason": "refusal"}}
        self.assertFalse(am.is_cyber_refusal_message(msg))

    def test_detect_true_when_latest_assistant_is_cyber_refusal(self):
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([json.dumps(self.CYBER_REFUSAL)])
            self.assertTrue(am.detect_cyber_refusal(Path("/x.jsonl"), "claude-research"))

    def test_detect_false_when_recovered(self):
        """A newer non-refusal assistant turn = recovered → not blocked (tac order:
        most-recent line first)."""
        recovered = json.dumps({"type": "assistant", "message": {
            "stop_reason": "end_turn", "content": [{"type": "text", "text": "back"}]}})
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([recovered, json.dumps(self.CYBER_REFUSAL)])
            self.assertFalse(am.detect_cyber_refusal(Path("/x.jsonl"), "claude-research"))

    def test_detect_skips_trailing_non_assistant_lines(self):
        """A user/tool_result line after the refusal must not hide it — the detector
        scans to the most-recent ASSISTANT line."""
        user_retry = json.dumps({"type": "user", "message": {"content": "retry"}})
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([user_retry, json.dumps(self.CYBER_REFUSAL)])
            self.assertTrue(am.detect_cyber_refusal(Path("/x.jsonl"), "claude-research"))

    def test_detect_fails_safe_on_subprocess_error(self):
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_result([], returncode=1)
            self.assertFalse(am.detect_cyber_refusal(Path("/x.jsonl"), "claude-research"))


# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# P1.d — rate-limit signal sources
# ---------------------------------------------------------------------------


class TestRateLimitRegex(unittest.TestCase):
    def test_matches_claude_code_synthetic_error(self) -> None:
        self.assertIsNotNone(RATE_LIMIT_REGEX.search(
            "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited"
        ))

    def test_matches_bare_rate_limited(self) -> None:
        self.assertIsNotNone(RATE_LIMIT_REGEX.search("rate limited"))
        self.assertIsNotNone(RATE_LIMIT_REGEX.search("Rate Limited"))

    def test_no_match_on_normal_output(self) -> None:
        self.assertIsNone(RATE_LIMIT_REGEX.search(
            "Implementation landed at d4e0f4d. Tests green. Ready when you say go."
        ))

    def test_no_match_on_429_text_without_rate_limit_phrase(self) -> None:
        # An HTTP 429 in tool output that doesn't contain the rate-limit phrase
        # shouldn't trip the regex (it's specifically about Claude Code's
        # synthetic-assistant-message error class).
        self.assertIsNone(RATE_LIMIT_REGEX.search("HTTP 429 too many requests on j17 echo-test"))


class TestDetectRateLimitInPane(unittest.TestCase):
    def test_bottom_match_fires(self) -> None:
        pane = (
            "earlier tool calls\n"
            "more output\n"
            "I'll handle this now.\n"
            "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited\n"
        )
        self.assertTrue(detect_rate_limit_in_pane(pane))

    def test_top_only_match_with_lots_of_activity_below_does_not_fire(self) -> None:
        # rate-limit phrase is in the FIRST line; tail-cap is 60 — if the pane
        # has > 60 lines of activity since then, the match is outside the tail.
        lines = ["API Error: Rate limited at start of session"]
        lines.extend(f"normal activity line {i}" for i in range(70))
        pane = "\n".join(lines)
        self.assertFalse(detect_rate_limit_in_pane(pane, tail_lines=60))

    def test_match_within_tail_window_fires(self) -> None:
        # rate-limit phrase WITHIN the bottom 60 lines.
        lines = [f"normal activity line {i}" for i in range(50)]
        lines.append("API Error: Server is temporarily limiting requests · Rate limited")
        lines.extend(f"prompt frame line {i}" for i in range(3))
        pane = "\n".join(lines)
        self.assertTrue(detect_rate_limit_in_pane(pane, tail_lines=60))


class TestCapturePane(unittest.TestCase):
    def test_returns_stdout_on_success(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="pane contents", stderr="")
            out = capture_pane("claude-telegram-bot:0", "claude")
        self.assertEqual(out, "pane contents")

    def test_returns_none_on_failure(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="no session")
            out = capture_pane("claude-telegram-bot:0", "claude")
        self.assertIsNone(out)

    def test_returns_none_on_timeout(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd="tmux", timeout=10)
            out = capture_pane("claude-telegram-bot:0", "claude")
        self.assertIsNone(out)

    def test_sudo_user_threads_into_argv(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            capture_pane("claude-telegram-bot:0", "claude-research")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["sudo", "-nu", "claude-research"])


class TestLatestRateLimitAssistantTs(unittest.TestCase):
    def _jsonl_run(self, lines: list[str], returncode: int = 0):
        return MagicMock(returncode=returncode, stdout="\n".join(lines), stderr="")

    def test_finds_structured_rate_limit_entry(self) -> None:
        """Real Claude Code rate-limit entries have isApiErrorMessage + error=rate_limit."""
        entry = json.dumps({
            "type": "assistant",
            "isApiErrorMessage": True,
            "error": "rate_limit",
            "timestamp": "2026-05-12T13:08:13.737Z",
            "message": {"content": [{"type": "text", "text": "API Error: Rate limited"}]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_run([entry])
            ts = latest_rate_limit_assistant_ts(Path("/x.jsonl"), "claude")
        # 2026-05-12T13:08:13.737Z → epoch.
        expected = int(datetime(2026, 5, 12, 13, 8, 13, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ts, expected)

    def test_text_only_fallback_matches(self) -> None:
        """If structured flags absent but text content matches, still detect."""
        entry = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T13:08:13.737Z",
            "message": {"content": [{"type": "text", "text": "API Error: Server is temporarily limiting requests · Rate limited"}]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_run([entry])
            ts = latest_rate_limit_assistant_ts(Path("/x.jsonl"), "claude")
        self.assertIsNotNone(ts)

    def test_returns_none_when_no_rate_limit_entry(self) -> None:
        entry = json.dumps({
            "type": "assistant",
            "timestamp": "2026-05-12T13:08:13.737Z",
            "message": {"content": [{"type": "text", "text": "Normal response."}]},
        })
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_run([entry])
            ts = latest_rate_limit_assistant_ts(Path("/x.jsonl"), "claude")
        self.assertIsNone(ts)

    def test_returns_none_on_subprocess_failure(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_run([], returncode=1)
            ts = latest_rate_limit_assistant_ts(Path("/x.jsonl"), "claude")
        self.assertIsNone(ts)

    def test_picks_most_recent_when_multiple(self) -> None:
        """tac order — first matching line in tac output is the most recent in file order."""
        recent = json.dumps({
            "type": "assistant",
            "isApiErrorMessage": True,
            "error": "rate_limit",
            "timestamp": "2026-05-12T14:00:00.000Z",
            "message": {"content": [{"type": "text", "text": "Rate limited"}]},
        })
        older = json.dumps({
            "type": "assistant",
            "isApiErrorMessage": True,
            "error": "rate_limit",
            "timestamp": "2026-05-12T13:00:00.000Z",
            "message": {"content": [{"type": "text", "text": "Rate limited"}]},
        })
        # tac would emit the most-recent file line first.
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._jsonl_run([recent, older])
            ts = latest_rate_limit_assistant_ts(Path("/x.jsonl"), "claude")
        expected = int(datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ts, expected)


# ---------------------------------------------------------------------------
# check_p1d composition
# ---------------------------------------------------------------------------


class TestCheckP1d(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = {
            "tmux_target_pane": "claude-telegram-bot:0",
            "sudo_user": "claude",
            "jsonl_dir": Path("/home/claude/.claude/projects/-home-claude"),
            "pantheon_config_path": Path("/home/claude/pantheon-config"),
        }

    def test_fires_when_pane_shows_rate_limit_and_stale(self) -> None:
        # Pane has the rate-limit string; jsonl ts is 10min ago; threshold 5min.
        stale_ts = int(time.time()) - 10 * 60
        with patch.object(am, "capture_pane", return_value="API Error: Rate limited"), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x.jsonl")), \
             patch.object(am, "latest_rate_limit_assistant_ts", return_value=stale_ts):
            self.assertTrue(check_p1d(self.cfg, 5 * 60))

    def test_does_not_fire_when_pane_clean(self) -> None:
        with patch.object(am, "capture_pane", return_value="Implementation landed at d4e0f4d. Tests green."):
            self.assertFalse(check_p1d(self.cfg, 5 * 60))

    def test_does_not_fire_when_too_recent(self) -> None:
        recent_ts = int(time.time()) - 30  # 30s ago
        with patch.object(am, "capture_pane", return_value="API Error: Rate limited"), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x.jsonl")), \
             patch.object(am, "latest_rate_limit_assistant_ts", return_value=recent_ts):
            self.assertFalse(check_p1d(self.cfg, 5 * 60))

    def test_does_not_fire_when_pane_unavailable_fail_open(self) -> None:
        with patch.object(am, "capture_pane", return_value=None):
            self.assertFalse(check_p1d(self.cfg, 5 * 60))

    def test_does_not_fire_when_jsonl_missing(self) -> None:
        # Pane shows rate-limit, but no jsonl found — could be a fresh agent;
        # without timestamp we can't gate on staleness, so don't fire.
        with patch.object(am, "capture_pane", return_value="API Error: Rate limited"), \
             patch.object(am, "find_latest_jsonl", return_value=None):
            self.assertFalse(check_p1d(self.cfg, 5 * 60))

    def test_does_not_fire_when_jsonl_has_no_rate_limit_entry(self) -> None:
        # Pane shows it, but jsonl doesn't have the structured entry — possibly
        # stale pane state from before logs were rotated. Don't fire.
        with patch.object(am, "capture_pane", return_value="API Error: Rate limited"), \
             patch.object(am, "find_latest_jsonl", return_value=Path("/x.jsonl")), \
             patch.object(am, "latest_rate_limit_assistant_ts", return_value=None):
            self.assertFalse(check_p1d(self.cfg, 5 * 60))


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


class TestCheckP1c(unittest.TestCase):
    """ORG-037 Layer 3 (T-1319) — active-team + dispatcher-turn-firing-stopped.

    The narrow "forgot-to-arm-Layer-1" residual. The whole point of the design
    is that it fires ONLY in the genuine stall quadrant and never in steady
    state — these tests pin both directions.
    """

    def _cfg(self, with_teams: bool = True) -> dict:
        cfg = {
            "tmux_target_pane": "x:0",
            "sudo_user": "claude",
            "jsonl_dir": Path("/x"),
            "ledger_agent_id": "orchestrator",
            "ticket_targets": frozenset({"orchestrator"}),
            "pantheon_config_path": Path("/x"),
            "bridge_inbound_key": "orchestrator",
        }
        if with_teams:
            cfg["teams_dir"] = Path("/x/teams")
        return cfg

    def test_fires_on_active_team_stale_jsonl_fresh_teammate(self) -> None:
        stale = int(time.time()) - 3600  # 1h ago (> 30min threshold)
        fresh_team = int(time.time()) - 600  # 10min ago — newer than main jsonl
        with patch.object(am, "active_team_inboxes", return_value=True), \
             patch.object(am, "latest_jsonl_mtime", return_value=stale), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=stale), \
             patch.object(am, "newest_team_artifact_mtime", return_value=fresh_team):
            fired = am.check_p1c(self._cfg(), stale_threshold_seconds=1800)
        self.assertTrue(fired)

    def test_no_team_suppresses(self) -> None:
        stale = int(time.time()) - 3600
        with patch.object(am, "active_team_inboxes", return_value=False), \
             patch.object(am, "latest_jsonl_mtime", return_value=stale), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=stale), \
             patch.object(am, "newest_team_artifact_mtime", return_value=int(time.time())):
            fired = am.check_p1c(self._cfg(), stale_threshold_seconds=1800)
        self.assertFalse(fired)

    def test_fresh_jsonl_suppresses(self) -> None:
        """A working Layer-1 keeps the main jsonl fresh → never fires. This is
        the narrowing that prevents recreating the broad P1.c surface."""
        fresh = int(time.time()) - 60  # 1min ago — within threshold
        with patch.object(am, "active_team_inboxes", return_value=True), \
             patch.object(am, "latest_jsonl_mtime", return_value=fresh), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=0), \
             patch.object(am, "newest_team_artifact_mtime", return_value=int(time.time())):
            fired = am.check_p1c(self._cfg(), stale_threshold_seconds=1800)
        self.assertFalse(fired)

    def test_recent_ledger_suppresses(self) -> None:
        """Ledger activity (even without a fresh jsonl) counts as activity."""
        stale_jsonl = int(time.time()) - 3600
        recent_ledger = int(time.time()) - 60
        with patch.object(am, "active_team_inboxes", return_value=True), \
             patch.object(am, "latest_jsonl_mtime", return_value=stale_jsonl), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=recent_ledger), \
             patch.object(am, "newest_team_artifact_mtime", return_value=int(time.time())):
            fired = am.check_p1c(self._cfg(), stale_threshold_seconds=1800)
        self.assertFalse(fired)

    def test_stale_teammate_suppresses_when_sharpening_on(self) -> None:
        """§7 sharpening: a teams dir whose newest artifact is OLDER than the
        dispatcher's last turn = a long-dead stale dir, not a dropped sub-result
        → the legit-idle-with-stale-dir false fire is eliminated."""
        stale = int(time.time()) - 3600
        older_team = stale - 600  # teammate artifact OLDER than the main jsonl
        with patch.object(am, "active_team_inboxes", return_value=True), \
             patch.object(am, "latest_jsonl_mtime", return_value=stale), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=stale), \
             patch.object(am, "newest_team_artifact_mtime", return_value=older_team):
            fired = am.check_p1c(
                self._cfg(), stale_threshold_seconds=1800, require_fresh_teammate=True
            )
        self.assertFalse(fired)

    def test_bare_predicate_fires_without_fresh_teammate(self) -> None:
        """Sharpening OFF (the documented brittle-fallback) → fires on
        active-team + stale alone; the teammate probe is not even consulted."""
        stale = int(time.time()) - 3600
        with patch.object(am, "active_team_inboxes", return_value=True), \
             patch.object(am, "latest_jsonl_mtime", return_value=stale), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=stale), \
             patch.object(am, "newest_team_artifact_mtime") as nt:
            fired = am.check_p1c(
                self._cfg(), stale_threshold_seconds=1800, require_fresh_teammate=False
            )
        self.assertTrue(fired)
        nt.assert_not_called()

    def test_no_teams_dir_never_fires(self) -> None:
        """An agent without teams_dir configured is ineligible — bails before
        any probe (no spurious subprocess)."""
        stale = int(time.time()) - 3600
        with patch.object(am, "active_team_inboxes") as ati, \
             patch.object(am, "latest_jsonl_mtime", return_value=stale), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=stale):
            fired = am.check_p1c(self._cfg(with_teams=False), stale_threshold_seconds=1800)
        self.assertFalse(fired)
        ati.assert_not_called()

    def test_no_activity_signal_fail_open(self) -> None:
        """Both jsonl + ledger unavailable → can't determine activity → fail-open."""
        with patch.object(am, "active_team_inboxes", return_value=True), \
             patch.object(am, "latest_jsonl_mtime", return_value=None), \
             patch.object(am, "latest_ledger_entry_ts_for_agent", return_value=None), \
             patch.object(am, "newest_team_artifact_mtime", return_value=int(time.time())):
            fired = am.check_p1c(self._cfg(), stale_threshold_seconds=1800)
        self.assertFalse(fired)


class TestTeamHelpers(unittest.TestCase):
    """The active_team_inboxes + newest_team_artifact_mtime probes (T-1319)."""

    def _run(self, stdout: str = "", rc: int = 0):
        return MagicMock(returncode=rc, stdout=stdout, stderr="")

    def test_active_team_inboxes_true(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._run("/home/claude/.claude/teams/session-x/inboxes\n")
            self.assertTrue(am.active_team_inboxes(Path("/t"), "claude"))

    def test_active_team_inboxes_false_on_empty(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._run("")
            self.assertFalse(am.active_team_inboxes(Path("/t"), "claude"))

    def test_active_team_inboxes_fail_closed_on_error(self) -> None:
        with patch("tools.activity_monitor.subprocess.run", side_effect=OSError):
            self.assertFalse(am.active_team_inboxes(Path("/t"), "claude"))

    def test_newest_team_artifact_mtime_parses_float(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._run("1782600000.1234\n")
            self.assertEqual(am.newest_team_artifact_mtime(Path("/t"), "claude"), 1782600000)

    def test_newest_team_artifact_mtime_none_on_empty(self) -> None:
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = self._run("")
            self.assertIsNone(am.newest_team_artifact_mtime(Path("/t"), "claude"))

    def test_newest_team_artifact_mtime_none_on_error(self) -> None:
        with patch("tools.activity_monitor.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("c", 1)):
            self.assertIsNone(am.newest_team_artifact_mtime(Path("/t"), "claude"))


class TestMain(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp / "state")

    def tearDown(self) -> None:
        for k in (
            "PANTHEON_STATE_DIR",
            "ACTIVITY_MONITOR_COOLDOWN_SECONDS",
            "ACTIVITY_MONITOR_P1D_COOLDOWN_SECONDS",
            "ACTIVITY_MONITOR_P1A_IDLE_SECONDS",
            "ACTIVITY_MONITOR_P1B_OPERATOR_SECONDS",
            "ACTIVITY_MONITOR_P1D_AGE_SECONDS",
            "ACTIVITY_MONITOR_BRIDGE_HEALTH_URL",
            "PANTHEON_LAYER3_ENABLED",
            "ACTIVITY_MONITOR_P1C_STALE_SECONDS",
            "ACTIVITY_MONITOR_P1C_COOLDOWN_SECONDS",
            "ACTIVITY_MONITOR_P1C_REQUIRE_FRESH_TEAMMATE",
            "ACTIVITY_MONITOR_BOUNDED_POKE_ENABLED",
            "ACTIVITY_MONITOR_MAX_INEFFECTIVE_POKES",
        ):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def _fires(self) -> list[dict]:
        """T-3072: read back the real fire-log this TestMain class's own
        PANTHEON_STATE_DIR produces — `_record_fire` is NOT mocked in this
        class, so these are end-to-end wiring assertions at the main() level."""
        path = self.tmp / "state" / "activity_monitor" / "fires.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_cooldown_short_circuits(self) -> None:
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"ts": int(time.time()) - 60, "predicate": "p1a"}))
        with patch.object(am, "check_p1a") as p1a, \
             patch.object(am, "check_p1b") as p1b, \
             patch.object(am, "check_p1d") as p1d:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        # Cooldown should have short-circuited before predicates were even checked.
        p1a.assert_not_called()
        p1b.assert_not_called()
        p1d.assert_not_called()

    def _hold(self, **kw):
        from tools import seat_state as ss
        return ss.Contribution(**kw)

    def _ss_readers(self, record=None, maint=None, oom=None):
        from tools import seat_state as ss
        none = ss.Contribution()
        return [
            patch("tools.seat_state._read_seat_state", return_value=record or none),
            patch("tools.seat_state._read_seat_state_cross", return_value=record or none),
            patch("tools.seat_state._read_maintenance", return_value=maint or none),
            patch("tools.seat_state._read_oom", return_value=oom or none),
        ]

    def _main_held(self, record=None, maint=None, oom=None, p1a=True, p1b=True, p1d=True):
        import contextlib
        with contextlib.ExitStack() as st:
            for pt in self._ss_readers(record, maint, oom):
                st.enter_context(pt)
            m = {
                "p1a": st.enter_context(patch.object(am, "check_p1a", return_value=p1a)),
                "p1b": st.enter_context(patch.object(am, "check_p1b", return_value=p1b)),
                "p1d": st.enter_context(patch.object(am, "check_p1d", return_value=p1d)),
                "poke": st.enter_context(patch.object(am, "send_poke", return_value=True)),
            }
            st.enter_context(patch.object(am, "_pane_idle_probe", return_value=None))
            rc = main(["--agent", "orchestrator"])
        return rc, m

    def test_seat_maintenance_skips_every_predicate_T5244(self) -> None:
        # mason 2026-10-19 19:00Z: a held seat was poked, the pane was gone, the poke
        # failed -> exit 1 -> a false unit-failure alert. A held seat is never acted on.
        c = self._hold(source="maintenance", intake_blocked=True, remediation_vetoed=True)
        rc, m = self._main_held(maint=c)
        self.assertEqual(rc, 0)
        for k in ("poke", "p1a", "p1b", "p1d"):
            m[k].assert_not_called()

    def test_oom_hold_skips_every_predicate(self) -> None:
        # Q1: the OOM hold is honoured through the reader (launch_blocked).
        c = self._hold(source="oom-hold", intake_blocked=True, launch_blocked=True)
        rc, m = self._main_held(oom=c)
        self.assertEqual(rc, 0)
        for k in ("poke", "p1a", "p1b", "p1d"):
            m[k].assert_not_called()

    def test_deep_sleep_skips_every_predicate(self) -> None:
        c = self._hold(source="seat-state", state="deep-sleep", intake_blocked=True,
                       launch_blocked=True, remediation_vetoed=True)
        rc, m = self._main_held(record=c)
        self.assertEqual(rc, 0)
        for k in ("poke", "p1a", "p1b", "p1d"):
            m[k].assert_not_called()

    def test_paused_intake_skips_queue_pokes_but_keeps_liveness(self) -> None:
        # A paused seat is alive and must stay watched: P1.a/P1.b are not even
        # evaluated, but P1.d (rate-limit recovery) still runs and fires.
        c = self._hold(source="seat-state", state="paused-intake", intake_blocked=True)
        rc, m = self._main_held(record=c)
        self.assertEqual(rc, 0)
        m["p1a"].assert_not_called()
        m["p1b"].assert_not_called()
        m["p1d"].assert_called_once()
        m["poke"].assert_called()  # the P1.d retry-inject

    def test_paused_intake_keeps_the_wedge_predicate(self) -> None:
        c = self._hold(source="seat-state", state="paused-intake", intake_blocked=True)
        import contextlib
        with contextlib.ExitStack() as st:
            for pt in self._ss_readers(record=c):
                st.enter_context(pt)
            st.enter_context(patch.dict(os.environ, {"ACTIVITY_MONITOR_TUI_WEDGE_ENABLED": "1"}))
            tw = st.enter_context(patch.object(am, "check_tui_wedge", return_value=(False, "no")))
            st.enter_context(patch.object(am, "check_p1a", return_value=True))
            st.enter_context(patch.object(am, "check_p1b", return_value=True))
            st.enter_context(patch.object(am, "check_p1d", return_value=False))
            self.assertEqual(main(["--agent", "orchestrator"]), 0)
        tw.assert_called_once()

    def test_no_hold_runs_p1a_as_before(self) -> None:
        rc, m = self._main_held(p1d=False)
        self.assertEqual(rc, 0)
        m["p1a"].assert_called_once()

    def test_seat_hold_gate_fails_open(self) -> None:
        # A reader fault must read "not held" so a real wedge is still poked.
        with patch("tools.seat_state.effective_for", side_effect=OSError("boom")):
            self.assertIsNone(am._seat_hold("orchestrator"))
        rc, m = self._main_held(record=self._hold(fault="seat-state: boom"), p1d=False)
        m["p1a"].assert_called_once()

    def test_p1a_fire_pokes_and_records(self) -> None:
        with patch.object(am, "check_p1a", return_value=True), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False), \
             patch.object(am, "_pane_idle_probe", return_value=None), \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        poke.assert_called_once()
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertTrue(state.exists())
        self.assertEqual(json.loads(state.read_text())["predicate"], "p1a")
        self.assertEqual([f["predicate"] for f in self._fires()], ["p1a"])  # T-3072

    def test_p1b_fire_pokes_and_records(self) -> None:
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=True), \
             patch.object(am, "check_p1d", return_value=False), \
             patch.object(am, "_pane_idle_probe", return_value=None), \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        poke.assert_called_once()
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertEqual(json.loads(state.read_text())["predicate"], "p1b")
        self.assertEqual([f["predicate"] for f in self._fires()], ["p1b"])  # T-3072

    def test_p1b_kill_switch_skips_p1b_but_not_p1d(self) -> None:
        # T-4683: ACTIVITY_MONITOR_P1B_ENABLED=0 must stop P1.b even when it
        # would fire (the threshold override cannot: fail-closed on a fresh
        # session), while P1.d recovery still runs.
        with patch.dict(os.environ, {"ACTIVITY_MONITOR_P1B_ENABLED": "0"}), \
             patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=True) as p1b, \
             patch.object(am, "check_p1d", return_value=True), \
             patch.object(am, "_pane_idle_probe", return_value=None), \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        p1b.assert_not_called()
        self.assertEqual([f["predicate"] for f in self._fires()], ["p1d"])
        self.assertIn("rate-limit", poke.call_args[0][1].lower())

    def test_p1b_threshold_override_alone_does_not_pause_fresh_session(self) -> None:
        # T-4683 root cause, pinned: with no telegram inbound in the jsonl,
        # check_p1b treats the operator as absent for exactly the threshold, so even
        # a one-year threshold passes the absent gate.
        cfg = {"jsonl_dir": "/x", "sudo_user": "claude",
               "pantheon_config_path": "/x", "ticket_targets": ["orchestrator"]}
        with patch.object(am, "find_latest_jsonl", return_value=Path("/x/s.jsonl")), \
             patch.object(am, "latest_telegram_inbound_ts", return_value=None), \
             patch.object(am, "latest_assistant_ends_without_tool_use", return_value=True) as sig1, \
             patch.object(am, "latest_assistant_interactive_tool_use", return_value=None), \
             patch.object(am, "count_actionable_tickets", return_value=0):
            am.check_p1b(cfg, "", 31536000)
        sig1.assert_called_once()  # got past the absent gate

    def test_p1d_fire_pokes_and_records(self) -> None:
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=True), \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        poke.assert_called_once()
        # Retry-inject prompt is the one sent to the pane.
        called_prompt = poke.call_args[0][1]
        self.assertIn("rate-limit", called_prompt.lower())
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertEqual(json.loads(state.read_text())["predicate"], "p1d")
        self.assertEqual([f["predicate"] for f in self._fires()], ["p1d"])  # T-3072

    def test_p1a_takes_precedence_over_p1b(self) -> None:
        """When both fire, P1.a wins (stronger signal: completely-idle)."""
        with patch.object(am, "check_p1a", return_value=True), \
             patch.object(am, "check_p1b", return_value=True) as p1b, \
             patch.object(am, "check_p1d", return_value=True) as p1d, \
             patch.object(am, "send_poke", return_value=True):
            main(["--agent", "orchestrator"])
        # Neither P1.b nor P1.d should be checked (short-circuit on P1.a true).
        p1b.assert_not_called()
        p1d.assert_not_called()

    def test_p1b_takes_precedence_over_p1d(self) -> None:
        """When P1.a is false but P1.b + P1.d both fire, P1.b wins."""
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=True), \
             patch.object(am, "check_p1d", return_value=True) as p1d, \
             patch.object(am, "send_poke", return_value=True):
            main(["--agent", "orchestrator"])
        p1d.assert_not_called()

    def test_p1d_cooldown_shorter_than_p1a(self) -> None:
        """P1.d cooldown (15min) is per-predicate — recent P1.d fire blocks
        re-fire within 15min; a 16min-old P1.d fire DOESN'T (whereas equivalent
        old P1.a fire would still block at 16min)."""
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        state.parent.mkdir(parents=True)
        # 16 min ago — past P1.d's 15min cooldown but inside P1.a's 30min
        state.write_text(json.dumps({"ts": int(time.time()) - 16 * 60, "predicate": "p1d"}))
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=True) as p1d, \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        # Cooldown lookup keyed on last-predicate=p1d → 15min → expired → fire allowed.
        p1d.assert_called()
        poke.assert_called_once()

    def test_p1a_cooldown_still_blocks_at_20min(self) -> None:
        """P1.a's 30min cooldown still blocks at 20min."""
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"ts": int(time.time()) - 20 * 60, "predicate": "p1a"}))
        with patch.object(am, "check_p1a") as p1a, \
             patch.object(am, "check_p1b") as p1b, \
             patch.object(am, "check_p1d") as p1d:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        p1a.assert_not_called()
        p1b.assert_not_called()
        p1d.assert_not_called()

    def test_no_predicate_bails_clean(self) -> None:
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False), \
             patch.object(am, "send_poke") as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        poke.assert_not_called()
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertFalse(state.exists())

    def test_p1c_disabled_by_default_not_evaluated(self) -> None:
        """Ship dark: with PANTHEON_LAYER3_ENABLED unset, check_p1c is never
        even called (the `p1c_enabled and ...` short-circuit) — committed code
        is inert everywhere until a per-agent flag flip."""
        self.assertNotIn("PANTHEON_LAYER3_ENABLED", os.environ)
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False), \
             patch.object(am, "check_p1c") as p1c, \
             patch.object(am, "send_poke") as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        p1c.assert_not_called()
        poke.assert_not_called()

    def test_p1c_enabled_fires_pokes_and_records(self) -> None:
        os.environ["PANTHEON_LAYER3_ENABLED"] = "1"
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False), \
             patch.object(am, "check_p1c", return_value=True), \
             patch.object(am, "_pane_idle_probe", return_value=None), \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        poke.assert_called_once()
        # The liveness-backstop prompt is the one sent to the pane.
        called_prompt = poke.call_args[0][1]
        self.assertIn("liveness backstop", called_prompt.lower())
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertEqual(json.loads(state.read_text())["predicate"], "p1c")
        self.assertEqual([f["predicate"] for f in self._fires()], ["p1c"])  # T-3072

    def test_p1c_only_reached_after_abd_false(self) -> None:
        """Precedence: P1.c is the last gate — an earlier predicate firing
        short-circuits before check_p1c is consulted, even when enabled."""
        os.environ["PANTHEON_LAYER3_ENABLED"] = "1"
        with patch.object(am, "check_p1a", return_value=True), \
             patch.object(am, "check_p1c") as p1c, \
             patch.object(am, "send_poke", return_value=True):
            main(["--agent", "orchestrator"])
        p1c.assert_not_called()

    def test_poke_failure_returns_1(self) -> None:
        with patch.object(am, "check_p1a", return_value=True), \
             patch.object(am, "_pane_idle_probe", return_value=None), \
             patch.object(am, "send_poke", return_value=False):
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 1)
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertFalse(state.exists())
        self.assertEqual(self._fires(), [])  # T-3072 — a failed send is not a fire

    def test_env_override_threshold(self) -> None:
        """ACTIVITY_MONITOR_P1A_IDLE_SECONDS env override is threaded through."""
        os.environ["ACTIVITY_MONITOR_P1A_IDLE_SECONDS"] = "60"
        with patch.object(am, "check_p1a") as p1a, \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False):
            p1a.return_value = False
            main(["--agent", "orchestrator"])
        kwargs = p1a.call_args[1] if p1a.call_args[1] else {}
        positional = p1a.call_args[0]
        actual = positional[1] if len(positional) > 1 else kwargs.get("idle_threshold_seconds")
        self.assertEqual(actual, 60)

    def test_p1d_env_override_age(self) -> None:
        """ACTIVITY_MONITOR_P1D_AGE_SECONDS env override is threaded through."""
        os.environ["ACTIVITY_MONITOR_P1D_AGE_SECONDS"] = "120"
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d") as p1d:
            p1d.return_value = False
            main(["--agent", "orchestrator"])
        positional = p1d.call_args[0]
        actual = positional[1] if len(positional) > 1 else p1d.call_args[1].get("age_threshold_seconds")
        self.assertEqual(actual, 120)


# ---------------------------------------------------------------------------
# T-0538: send_poke() now delegates to send_keys_submit.sh via sudo-wrapped bash -c
# ---------------------------------------------------------------------------


class TestSendPoke(unittest.TestCase):
    """T-0538: send_poke() must invoke the target user's send_keys_submit.sh
    via sudo -nu + XDG_RUNTIME_DIR in the bash -c string."""

    def _cfg(self) -> dict:
        return {
            "tmux_target_pane": "claude-telegram-bot:0",
            "sudo_user": "claude",
            "jsonl_dir": Path("/home/claude/.claude/projects/-home-claude"),
            "ledger_agent_id": "orchestrator",
            "ticket_targets": frozenset({"orchestrator", "orchestrator"}),
            "pantheon_config_path": Path("/home/claude/pantheon-config"),
            "bridge_inbound_key": "orchestrator",
        }

    def _make_run(self, returncode=0):
        return MagicMock(returncode=returncode, stdout="", stderr="")

    def test_send_poke_uses_sudo_nu(self):
        # T-0538: command must start with sudo -nu <sudo_user>
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(0)) as mock_run:
            am.send_poke(self._cfg(), "poke prompt")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[:3], ["sudo", "-nu", "claude"])

    def test_send_poke_invokes_helper_path(self):
        # T-0538: bash -c string must reference the target's helper path
        expected_helper = str(Path("/home/claude/pantheon-config") / "tools" / "send_keys_submit.sh")
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(0)) as mock_run:
            am.send_poke(self._cfg(), "poke prompt")
        cmd = mock_run.call_args[0][0]
        # cmd is: ["sudo", "-nu", user, "bash", "-c", "<shell string>"]
        bash_c_str = cmd[-1]
        self.assertIn(expected_helper, bash_c_str)

    def test_send_poke_includes_xdg_runtime_dir(self):
        # T-0538: XDG_RUNTIME_DIR must be set in the bash -c string (cross-account tmux)
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(0)) as mock_run:
            am.send_poke(self._cfg(), "poke prompt")
        cmd = mock_run.call_args[0][0]
        bash_c_str = cmd[-1]
        self.assertIn("XDG_RUNTIME_DIR", bash_c_str)

    def test_send_poke_includes_pane_and_prompt(self):
        # T-0538: pane target and prompt text both in bash -c string
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(0)) as mock_run:
            am.send_poke(self._cfg(), "my poke text")
        cmd = mock_run.call_args[0][0]
        bash_c_str = cmd[-1]
        self.assertIn("claude-telegram-bot:0", bash_c_str)
        self.assertIn("my poke text", bash_c_str)

    def test_send_poke_success_returns_true(self):
        # T-0538: exit 0 → True
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(0)):
            result = am.send_poke(self._cfg(), "prompt")
        self.assertTrue(result)

    def test_send_poke_failure_returns_false(self):
        # T-0538: exit ≠ 0 → False
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(1)):
            result = am.send_poke(self._cfg(), "prompt")
        self.assertFalse(result)

    def test_send_poke_timeout_returns_false(self):
        # T-0538: TimeoutExpired → False
        with patch("tools.activity_monitor.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="sudo", timeout=10)):
            result = am.send_poke(self._cfg(), "prompt")
        self.assertFalse(result)

    def test_send_poke_os_error_returns_false(self):
        # T-0538: OSError → False
        with patch("tools.activity_monitor.subprocess.run",
                   side_effect=OSError("no such file")):
            result = am.send_poke(self._cfg(), "prompt")
        self.assertFalse(result)

    def test_send_poke_single_subprocess_call(self):
        # T-0538: single call (helper does text+enter+verify internally), not two
        with patch("tools.activity_monitor.subprocess.run",
                   return_value=self._make_run(0)) as mock_run:
            am.send_poke(self._cfg(), "prompt")
        self.assertEqual(mock_run.call_count, 1)


class TestCapStateClassifier(unittest.TestCase):
    """T-1369: the two measured cap strings classify OPPOSITELY, and discussing
    a cap string must never read as being capped."""

    def _turn(self, text, err=True):
        return {"type": "assistant", "isApiErrorMessage": err,
                "message": {"content": [{"type": "text", "text": text}]}}

    def test_session_limit_verbatim_specimen(self):
        # orchestrator, 38x, 2026-08-02. U+00B7 separator — a paraphrase regex misses it.
        kind, hint = am.classify_cap_message(
            self._turn("You've hit your session limit \u00b7 resets 9:40am (UTC)"))
        self.assertEqual(kind, "session_limit")
        self.assertEqual(hint, "9:40am (UTC)")

    def test_spend_limit_verbatim_specimen(self):
        # artisan, 59x, the 48h outage (T-2387).
        kind, hint = am.classify_cap_message(self._turn(
            "You've hit your monthly spend limit \u00b7 raise it at "
            "claude.ai/settings/usage?from=cc_cli_limit_message"))
        self.assertEqual(kind, "spend_limit")
        self.assertIsNone(hint)

    def test_discussing_a_cap_string_is_not_being_capped(self):
        # Load-bearing: this fleet's tickets QUOTE both wire strings verbatim.
        # Without the isApiErrorMessage guard, recovery reads as permanently capped.
        self.assertIsNone(am.classify_cap_message(self._turn(
            "the specimen is: You've hit your session limit \u00b7 resets 5pm (UTC)",
            err=False)))

    def test_non_assistant_and_malformed_are_none(self):
        self.assertIsNone(am.classify_cap_message({"type": "user"}))
        self.assertIsNone(am.classify_cap_message(
            {"type": "assistant", "isApiErrorMessage": True, "message": {"content": None}}))

    def test_ordinary_api_error_is_not_a_cap(self):
        # -32603 entitlement, 500s, etc. — different class, different routing (T-2386).
        self.assertIsNone(am.classify_cap_message(self._turn(
            "Internal error: Your organization does not have access to Claude.")))


class TestCapPrePokeGate(unittest.TestCase):
    """T-1369 dispatch branches. MEASURED motivation: the waker kicked a
    session-capped orchestrator 8x over 3.5h — every kick delivered, every turn an
    error. Bounded, correct, useless."""

    def _cfg(self):
        return {"jsonl_dir": Path("/nonexistent"), "sudo_user": "claude",
                "pane_target": "x:0"}

    def _run(self, cap, sp):
        with patch.object(am, "send_poke", return_value=True) as poke, \
             patch.object(am, "_session_cap_state", return_value=cap), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cyber_blocked", return_value=False) as cyber, \
             patch.object(am, "_route_failure_alert") as alert, \
             patch.object(am, "_record_fire") as record_fire, \
             patch.object(am, "latest_jsonl_mtime", return_value=5000):
            rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1c", "P", sp,
                                   bounded_enabled=True, max_ineffective=1)
        return rc, poke, alert, cyber, record_fire

    def test_session_limit_suppresses_poke_AND_escalation(self):
        # Self-clears at the printed time — paging a human for a state with an
        # expiry is the alarm that teaches people to skip alarms.
        with tempfile.TemporaryDirectory() as tmp:
            rc, poke, alert, _, record_fire = self._run(
                ("session_limit", "9:40am (UTC)"), Path(tmp) / "s.json")
        self.assertEqual(rc, 0)
        poke.assert_not_called()
        alert.assert_not_called()
        # T-3072: neither a poke landed nor an escalation fired — nothing to
        # record. Self-clearing states are deliberately invisible to the
        # drift alarm, same as they are to the alert path.
        record_fire.assert_not_called()

    def test_spend_limit_suppresses_poke_and_escalates(self):
        # Holds until a human raises it — alert + hold, exactly once.
        with tempfile.TemporaryDirectory() as tmp:
            rc, poke, alert, _, record_fire = self._run(
                ("spend_limit", None), Path(tmp) / "s.json")
        self.assertEqual(rc, 0)
        poke.assert_not_called()
        alert.assert_called_once()
        self.assertIn("spend-cap", str(alert.call_args))
        record_fire.assert_called_once_with("orchestrator", "spend-cap")  # T-3072 vocab key

    def test_no_cap_falls_through_to_cyber_gate(self):
        # The gate must be a pass-through when healthy — the cyber check still runs.
        with tempfile.TemporaryDirectory() as tmp:
            rc, poke, alert, cyber, record_fire = self._run(None, Path(tmp) / "s.json")
        self.assertEqual(rc, 0)
        cyber.assert_called_once()
        poke.assert_called_once()  # healthy path still pokes
        record_fire.assert_called_once_with("orchestrator", "p1c")  # T-3072 normal poke path

    def test_ship_dark_flag_off_bypasses_cap_gate(self):
        # bounded_enabled=False must reproduce legacy behaviour exactly — the
        # cap gate lives inside the bounded block.
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(am, "send_poke", return_value=True) as poke, \
                 patch.object(am, "_session_cap_state") as capfn:
                rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1a", "P",
                                       Path(tmp) / "s.json",
                                       bounded_enabled=False, max_ineffective=1)
            self.assertEqual(rc, 0)
            capfn.assert_not_called()
            poke.assert_called_once()


class CapModalDetectorT1369(unittest.TestCase):
    """T-1369 branch 4: the pane-only /rate-limit-options modal.

    Fixture is the REAL pane captured off orchestrator 2026-08-02 while it sat wedged
    ~50 min past its own printed reset — not a hand-typed approximation. That
    distinction already paid: the real capture carries a second '❯' on the shell
    prompt line ('❯ /rate-limit-options') above the options, which a first-match
    cursor scan misreads as "cursor not on option 1".
    """

    SPECIMEN = REPO_ROOT / "tools" / "testdata" / "orchestrator_cap_modal_pane.txt"

    def _specimen(self):
        return self.SPECIMEN.read_text(encoding="utf-8")

    def test_the_specimen_still_carries_what_the_test_relies_on(self):
        """A fixture that quietly loses its distinguishing feature turns every
        assertion below into a tautology."""
        raw = self._specimen()
        self.assertIn("\x1b", raw, "specimen should still be the -e capture")
        self.assertEqual(raw.count("❯"), 2,
                         "specimen must keep BOTH the prompt glyph and the "
                         "menu cursor — that pair is the bug it encodes")

    def test_real_specimen_is_safe_to_dismiss(self):
        self.assertEqual(am.classify_cap_modal(self._specimen()), am.MODAL_SAFE)

    def test_ansi_stripped_form_classifies_identically(self):
        """Production capture_pane uses `-p` without `-e`, so the live text has
        no escapes. Both forms must take the same path."""
        stripped = am._strip_ansi(self._specimen())
        self.assertNotIn("\x1b", stripped)
        self.assertEqual(am.classify_cap_modal(stripped), am.MODAL_SAFE)

    def test_cursor_on_option_2_is_UNSAFE(self):
        """The money branch. Enter confirms whatever is highlighted, and option
        2 buys a plan — the operator's lane."""
        text = am._strip_ansi(self._specimen())
        moved = (text.replace("❯ 1. Stop", "  1. Stop")
                     .replace("  2. Upgrade", "❯ 2. Upgrade"))
        self.assertEqual(am.classify_cap_modal(moved), am.MODAL_UNSAFE)

    def test_no_cursor_on_any_option_is_UNSAFE(self):
        text = am._strip_ansi(self._specimen()).replace("❯ 1. Stop", "  1. Stop")
        self.assertEqual(am.classify_cap_modal(text), am.MODAL_UNSAFE)

    def test_a_different_menu_is_NOT_this_modal(self):
        """'What do you want to do?' alone is a generic TUI prompt; matching it
        would make every future menu look like the cap modal."""
        self.assertIsNone(am.classify_cap_modal(
            "What do you want to do?\n❯ 1. Retry\n  2. Abort\n"))

    def test_ordinary_pane_and_empty_input_are_None(self):
        self.assertIsNone(am.classify_cap_modal("╭─────╮\n│ > │\n╰─────╯"))
        self.assertIsNone(am.classify_cap_modal(""))
        self.assertIsNone(am.classify_cap_modal(None))

    def test_no_configured_pane_degrades_instead_of_shelling_out(self):
        """This seam sits on the path EVERY poke takes. A cfg without a pane
        (specialist-a2a has none by construction) must return None rather than raise
        or run a live capture against a sibling's session."""
        with patch("tools.activity_monitor.capture_pane") as cap:
            self.assertIsNone(am._session_cap_modal({"sudo_user": "claude"}))
            self.assertIsNone(am._session_cap_modal({"tmux_target_pane": ""}))
            cap.assert_not_called()

    def test_the_cap_ERROR_text_alone_is_not_the_modal(self):
        """The jsonl branch owns that state. A session-limit line on the pane
        without the menu must not trigger a keystroke."""
        self.assertIsNone(am.classify_cap_modal(
            "You've hit your session limit · resets 2:50pm (UTC)"))


class TestClassifyPermissionModalT3558(unittest.TestCase):
    """T-3558(b2): the Claude Code tool-permission modal — a DISTINCT wedge class
    from the cap modal. Fixture is a REAL capture (modal region) off artisan/suborch2
    2026-09-07 (the Read(//tmp/**) push-gate wedge Recovery cleared), not hand-typed.
    DETECTION/DIAGNOSTIC only — nothing answers the prompt.
    """

    PERMISSION_SPECIMEN = REPO_ROOT / "tools" / "testdata" / "artisan_permission_modal_pane.txt"
    CAP_SPECIMEN = REPO_ROOT / "tools" / "testdata" / "orchestrator_cap_modal_pane.txt"

    def _perm(self):
        return self.PERMISSION_SPECIMEN.read_text(encoding="utf-8")

    def test_specimen_still_carries_the_distinguishing_tokens(self):
        """A fixture that quietly loses its distinguishing tokens makes the
        assertions below tautologies (the CapModal lesson)."""
        raw = self._perm()
        self.assertIn("Do you want to proceed?", raw)
        self.assertIn("Tab to amend", raw)
        self.assertIn("don't ask again for Read(//tmp/**)", raw)

    def test_real_specimen_names_the_requested_permission(self):
        self.assertEqual(am.classify_permission_modal(self._perm()), "Read(//tmp/**)")

    def test_does_not_collide_with_the_cap_modal(self):
        # The cap modal must NOT read as a permission modal — its footer is
        # 'Enter to confirm · Esc to cancel' (no 'Tab to amend').
        self.assertIsNone(am.classify_permission_modal(
            self.CAP_SPECIMEN.read_text(encoding="utf-8")))

    def test_cap_classifier_does_not_match_the_permission_specimen(self):
        # Reciprocal non-collision: the permission modal must NOT read as the cap modal.
        self.assertIsNone(am.classify_cap_modal(self._perm()))

    def test_requires_both_question_and_footer(self):
        # 'Do you want to proceed?' without the 'Tab to amend' footer is not enough.
        self.assertIsNone(am.classify_permission_modal(self._perm().replace("Tab to amend", "")))

    def test_generic_label_when_dont_ask_line_absent(self):
        # A permission modal with no 'don't ask again for X' line still classifies
        # (generic label) — detection must not hinge on that one option line.
        text = ("Do you want to proceed?\n ❯ 1. Yes\n   3. No\n\n"
                " Esc to cancel · Tab to amend\n")
        self.assertEqual(am.classify_permission_modal(text), am.PERMISSION_MODAL)

    def test_ansi_stripped_form_classifies_identically(self):
        self.assertEqual(am.classify_permission_modal("\x1b[1m" + self._perm()), "Read(//tmp/**)")

    def test_empty_and_ordinary_pane_are_None(self):
        self.assertIsNone(am.classify_permission_modal(""))
        self.assertIsNone(am.classify_permission_modal(None))
        self.assertIsNone(am.classify_permission_modal("╭─────╮\n│ > │\n╰─────╯"))


class PreviewChipT3558(unittest.TestCase):
    """T-3558(b2), preview-chip slice: a FOCUSED artifact-preview chip takes the
    composer's Enter, so a wake/poke never submits. Both fixtures are REAL: artisan's
    2026-09-07 bytes from its pipe-pane log (pane-timed-artisan.log, offset ~17.27MB),
    replayed through a VT emulator (pyte) at the pane's own 209x50 geometry (read off
    the stream's escapes), not hand-typed. The unfocused frame is the same session
    seconds earlier: the chip present but harmless."""

    FOCUSED = REPO_ROOT / "tools" / "testdata" / "artisan_preview_chip_focused_pane.txt"
    UNFOCUSED = REPO_ROOT / "tools" / "testdata" / "artisan_preview_chip_unfocused_pane.txt"
    PERMISSION_SPECIMEN = REPO_ROOT / "tools" / "testdata" / "artisan_permission_modal_pane.txt"
    CAP_SPECIMEN = REPO_ROOT / "tools" / "testdata" / "orchestrator_cap_modal_pane.txt"

    def _read(self, p):
        return p.read_text(encoding="utf-8")

    def test_specimens_still_carry_the_distinguishing_tokens(self):
        self.assertIn("❯ ⧉  t2738-preview · Enter to open · x to dismiss", self._read(self.FOCUSED))
        self.assertIn("⧉  t2738-preview · ctrl+] to open", self._read(self.UNFOCUSED))

    def test_focused_chip_is_named(self):
        self.assertEqual(am.classify_preview_chip(self._read(self.FOCUSED)),
                         f"{am.PREVIEW_CHIP}:t2738-preview")

    def test_unfocused_chip_is_not_a_wedge(self):
        self.assertIsNone(am.classify_preview_chip(self._read(self.UNFOCUSED)))

    def test_prose_quoting_the_chip_is_not_a_wedge(self):
        # This ticket's own body quotes the chip text; a pane showing it must not
        # read as wedged. Only the chip's own line shape counts.
        prose = ("  classes: (1) the ARTIFACT-PREVIEW CHIP intercepting the composer Enter "
                 "('preview chip · Enter to open · x to dismiss'); (2) the permission modal\n")
        self.assertIsNone(am.classify_preview_chip(prose))

    def test_no_collision_with_the_other_wedge_classes(self):
        for spec in (self.PERMISSION_SPECIMEN, self.CAP_SPECIMEN):
            with self.subTest(spec=spec.name):
                self.assertIsNone(am.classify_preview_chip(self._read(spec)))
        for spec in (self.FOCUSED, self.UNFOCUSED):
            with self.subTest(spec=spec.name):
                self.assertIsNone(am.classify_permission_modal(self._read(spec)))
                self.assertIsNone(am.classify_cap_modal(self._read(spec)))

    def test_ansi_and_empty(self):
        self.assertEqual(am.classify_preview_chip("\x1b[2m" + self._read(self.FOCUSED)),
                         f"{am.PREVIEW_CHIP}:t2738-preview")
        self.assertIsNone(am.classify_preview_chip(""))
        self.assertIsNone(am.classify_preview_chip(None))

    def test_classify_wedge_covers_both_detection_classes(self):
        self.assertEqual(am.classify_wedge(self._read(self.PERMISSION_SPECIMEN)), "Read(//tmp/**)")
        self.assertEqual(am.classify_wedge(self._read(self.FOCUSED)), f"{am.PREVIEW_CHIP}:t2738-preview")
        self.assertIsNone(am.classify_wedge(self._read(self.UNFOCUSED)))


class CapModalDispatchT1369(unittest.TestCase):
    """The dispatch branch: dismiss when safe, never touch the keyboard when not,
    and bound the repeats."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.state = self.d / "poke.json"
        self.cfg = {
            "tmux_target_pane": "claude-telegram-bot:0",
            "sudo_user": "claude",
            "jsonl_dir": str(self.d),
            "pantheon_config_path": str(REPO_ROOT),
        }

    def _dispatch(self, modal, **patches):
        stack = [
            patch("tools.activity_monitor._session_cap_modal", return_value=modal),
            patch("tools.activity_monitor.latest_jsonl_mtime", return_value=1),
            patch("tools.activity_monitor._session_cap_state", return_value=None),
            patch("tools.activity_monitor._session_cyber_blocked", return_value=False),
            # T-3599: these tests assert send_modal_confirm/send_poke/return-code
            # semantics, NOT busy-guard semantics — pin the guard's pane probe to
            # None (fail-open) so it never hits the REAL seat pane. Without this the
            # decide-time guard defers (rc 0, no send) whenever the running seat's
            # pane reads busy, which is a non-hermetic, seat-dependent failure
            # (orchestrator REBOUND). Busy-guard behavior has its own TestDispatchPokeBusyGuard.
            patch("tools.activity_monitor._pane_idle_probe", return_value=None),
        ]
        with contextlib.ExitStack() as es:
            for p in stack:
                es.enter_context(p)
            sender = es.enter_context(
                patch("tools.activity_monitor.send_modal_confirm",
                      return_value=patches.get("send_ok", True)))
            poker = es.enter_context(patch("tools.activity_monitor.send_poke"))
            alerter = es.enter_context(
                patch("tools.activity_monitor._route_failure_alert"))
            record_fire = es.enter_context(
                patch("tools.activity_monitor._record_fire"))
            rc = am._dispatch_poke(
                self.cfg, "orchestrator", "idle", "hello", self.state,
                bounded_enabled=True, max_ineffective=3)
        return rc, sender, poker, alerter, record_fire

    def test_safe_modal_sends_a_bare_enter_and_never_a_poke(self):
        rc, sender, poker, _, record_fire = self._dispatch(am.MODAL_SAFE)
        self.assertEqual(rc, 0)
        sender.assert_called_once()
        poker.assert_not_called()
        record_fire.assert_called_once_with("orchestrator", "idle")  # T-3072

    def test_unsafe_modal_sends_NOTHING_and_escalates(self):
        """The load-bearing one: option 2 spends money."""
        rc, sender, poker, alerter, record_fire = self._dispatch(am.MODAL_UNSAFE)
        self.assertEqual(rc, 0)
        sender.assert_not_called()
        poker.assert_not_called()
        alerter.assert_called_once()
        self.assertIn("spends", alerter.call_args[0][1])
        record_fire.assert_called_once_with("orchestrator", "cap-modal-unsafe")  # T-3072

    def test_dismissals_are_BOUNDED_then_escalate(self):
        for i in range(am.MAX_MODAL_DISMISSALS):
            _, sender, _, _, record_fire = self._dispatch(am.MODAL_SAFE)
            sender.assert_called_once()
            record_fire.assert_called_once_with("orchestrator", "idle")  # T-3072
            self.assertEqual(
                json.loads(self.state.read_text())["modal_dismissals"], i + 1)
        _, sender, _, alerter, record_fire = self._dispatch(am.MODAL_SAFE)
        sender.assert_not_called()
        alerter.assert_called_once()
        record_fire.assert_called_once_with("orchestrator", "cap-modal")  # T-3072 escalation vocab

    def test_no_modal_falls_through_to_the_existing_gates(self):
        """The new branch must not swallow the jsonl cap / cyber / poke paths."""
        rc, sender, poker, _, _ = self._dispatch(None)
        sender.assert_not_called()
        poker.assert_called_once()

    def test_a_failed_send_reports_rc_1(self):
        rc, sender, _, _, record_fire = self._dispatch(am.MODAL_SAFE, send_ok=False)
        self.assertEqual(rc, 1)
        sender.assert_called_once()
        record_fire.assert_not_called()  # T-3072 — a failed modal-confirm is not a fire


class ProductiveLivenessT2387(unittest.TestCase):
    """T-2387: a seat erroring every turn must stop reading as ALIVE.

    artisan hit the monthly spend cap and produced 23 turns / 23 errors on 07-30
    and 22 / 22 on 07-31 — 59 identical failures over two days — while every
    check asking "did it produce a turn" answered YES throughout. jsonl mtime
    advances on an error exactly as it does on real work.

    The fix keys on the OUTCOME BOOLEAN `isApiErrorMessage`, not on a status
    code or an error string, so it covers the spend cap (429), api-health (400),
    model-outage (404) and classes nobody has met. The tree already carries four
    detectors keyed to specific error identities — one of them a literal
    substring match on "monthly spend limit" — and each new class arrived as a
    new narrow detector. That is the pattern this breaks.
    """

    ERR = {"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 429,
           "timestamp": "2026-07-31T12:00:00.000Z"}
    OK = {"type": "assistant", "timestamp": "2026-07-29T09:00:00.000Z"}
    USER = {"type": "user", "timestamp": "2026-07-31T12:00:01.000Z"}

    def _run(self, records):
        """Drive latest_productive_jsonl_mtime over canned records, newest-first."""
        with patch("tools.context_fill_monitor.find_latest_jsonl",
                   return_value=Path("/fake/s.jsonl")), \
             patch("tools.context_fill_monitor.read_recent_records",
                   return_value=records):
            return am.latest_productive_jsonl_mtime(Path("/fake"), "codex", "artisan")

    def test_THE_REGRESSION_all_error_turns_report_no_productive_activity(self):
        """59 error turns and nothing else → None, not a fresh timestamp."""
        self.assertIsNone(self._run([dict(self.ERR) for _ in range(59)]))

    def test_error_turns_do_not_mask_an_older_real_turn(self):
        """Newest-first: errors on top, a real turn beneath → the REAL one wins."""
        got = self._run([dict(self.ERR), dict(self.ERR), dict(self.OK)])
        self.assertEqual(got, int(datetime(2026, 7, 29, 9, 0,
                                           tzinfo=timezone.utc).timestamp()))

    def test_non_assistant_records_are_skipped_not_counted(self):
        """A user record is not the seat doing work."""
        self.assertIsNone(self._run([dict(self.USER), dict(self.ERR)]))

    def test_CONTROL_a_healthy_seat_still_reads_active(self):
        """Without this, the guards above could pass by always returning None —
        which would mark every seat idle and poke the whole fleet."""
        got = self._run([dict(self.OK)])
        self.assertIsNotNone(got)
        self.assertEqual(got, int(datetime(2026, 7, 29, 9, 0,
                                           tzinfo=timezone.utc).timestamp()))

    def test_read_failure_returns_None_fail_open(self):
        with patch("tools.context_fill_monitor.find_latest_jsonl", return_value=None):
            self.assertIsNone(
                am.latest_productive_jsonl_mtime(Path("/fake"), "codex", "artisan"))


class ProductiveLivenessShipDarkT2387(unittest.TestCase):
    """The switch is default-OFF and only the LIVENESS sites are switched.

    Poke-effectiveness asks a DIFFERENT question — "did the session take a turn
    at all" — for which an error turn legitimately COUNTS (the poke landed).
    Those callers must keep the turn-counting signal. Conflating the two is the
    defect; swapping both would be the same defect with the sign flipped.
    """

    def test_THE_DEFAULT_ITSELF_is_OFF_when_the_env_var_is_absent(self):
        """The other switch tests PATCH the flag, so none of them exercise the
        DEFAULT — flipping the module default to "1" passed all of them. Caught
        by mutating the source during the non-vacuity pass, which is the whole
        argument for running one. Re-derive the constant under a clean env.
        """
        import importlib
        with patch.dict(os.environ, {}, clear=True):
            reloaded = importlib.reload(am)
            try:
                self.assertFalse(
                    reloaded.PRODUCTIVE_LIVENESS_ENABLED,
                    "T-2387 must ship DARK — arming changes which seats look idle "
                    "and therefore which get poked (T-1126 supervised first-fire)")
            finally:
                _reload_am()  # restore for the rest of the suite

    def test_arming_requires_exactly_1_not_any_truthy_string(self):
        import importlib
        for val, expected in (("1", True), ("0", False), ("true", False), ("", False)):
            with patch.dict(os.environ,
                            {"PANTHEON_PRODUCTIVE_LIVENESS_ENABLED": val}, clear=True):
                reloaded = importlib.reload(am)
                try:
                    self.assertEqual(reloaded.PRODUCTIVE_LIVENESS_ENABLED, expected,
                                     f"env value {val!r}")
                finally:
                    _reload_am()

    def test_default_is_OFF_and_delegates_to_the_unchanged_signal(self):
        with patch.object(am, "PRODUCTIVE_LIVENESS_ENABLED", False), \
             patch.object(am, "latest_jsonl_mtime", return_value=1234) as legacy, \
             patch.object(am, "latest_productive_jsonl_mtime") as productive:
            self.assertEqual(am.liveness_jsonl_mtime(Path("/d"), "u", "a"), 1234)
        legacy.assert_called_once()
        productive.assert_not_called()

    def test_armed_uses_the_outcome_aware_signal(self):
        with patch.object(am, "PRODUCTIVE_LIVENESS_ENABLED", True), \
             patch.object(am, "latest_jsonl_mtime") as legacy, \
             patch.object(am, "latest_productive_jsonl_mtime", return_value=99) as prod:
            self.assertEqual(am.liveness_jsonl_mtime(Path("/d"), "u", "a"), 99)
        prod.assert_called_once()
        legacy.assert_not_called()

    def test_poke_effectiveness_sites_still_use_the_turn_counting_signal(self):
        """Guards the split itself, by reading the source: every remaining
        bare `latest_jsonl_mtime(` CALL must be a poke-effectiveness site."""
        import re
        src = Path(am.__file__).read_text(encoding="utf-8")
        body = src.split("def liveness_jsonl_mtime", 1)[1].split("\n\n\n", 1)[1]
        calls = re.findall(r"^\s*(?:cur_mtime|pre_poke_jsonl_mtime)\s*=\s*latest_jsonl_activity\(",
                           body, re.M)
        # T-5367: the turn-counting signal is now latest_jsonl_activity (newest
        # timestamped entry, still counting error turns); no bare mtime call is left.
        self.assertEqual(re.findall(r"\blatest_jsonl_mtime\(", body), [],
                         "a poke-effectiveness site fell back to the raw mtime (T-5367)")
        self.assertGreaterEqual(
            len(calls), 7,
            "expected the poke-effectiveness sites to keep latest_jsonl_mtime; "
            "if they were switched to the productive signal, an error turn would "
            "stop counting as 'the poke landed' and pokes would look ineffective")
        # ...and the two LIVENESS sites must have been switched. Both halves of
        # the split are asserted; checking only one would let a wholesale swap
        # in either direction pass.
        self.assertEqual(
            body.count("liveness_jsonl_mtime("), 3,
            "expected exactly the three liveness sites (check_p1a, Layer-3 "
            "dormancy, check_tui_wedge) to use the switched signal — a wedge "
            "measures a PRODUCTIVE-turn freeze, so an error turn must not reset "
            "it (T-1369 adds the third; same T-2387 reason as the first two)")


class TuiWedgeDetectorT1369(unittest.TestCase):
    """T-1369 final branch — the TUI-alive-but-not-emitting wedge detector.

    Threshold is MEASURED (tui_wedge_sampler, orchestrator n=110 max 779s); the tests
    below drive the composition, not the number. The number is env-overridable
    and exercised at a small value so the duration gate is testable."""

    def _cfg(self) -> dict:
        return {"tmux_target_pane": "x:0", "sudo_user": "claude",
                "jsonl_dir": Path("/x"), "agent": "orchestrator"}

    # ---- _pane_fp ----
    def test_pane_fp_none_is_none(self) -> None:
        self.assertIsNone(am._pane_fp(None))

    def test_pane_fp_equal_for_same_text_differs_for_different(self) -> None:
        self.assertEqual(am._pane_fp("abc"), am._pane_fp("abc"))
        self.assertNotEqual(am._pane_fp("abc"), am._pane_fp("abd"))

    # ---- pane_is_animating ----
    def test_animating_true_when_pane_changes(self) -> None:
        with patch.object(am, "capture_pane", side_effect=["screen-A", "screen-B"]), \
             patch.object(am.time, "sleep"):
            self.assertTrue(am.pane_is_animating(self._cfg(), 0))

    def test_animating_false_when_pane_still(self) -> None:
        with patch.object(am, "capture_pane", side_effect=["idle> ", "idle> "]), \
             patch.object(am.time, "sleep"):
            self.assertFalse(am.pane_is_animating(self._cfg(), 0))

    def test_animating_none_when_capture_fails(self) -> None:
        # A failed capture is NOT evidence of motion — must return None, never True.
        with patch.object(am, "capture_pane", side_effect=["screen-A", None]), \
             patch.object(am.time, "sleep"):
            self.assertIsNone(am.pane_is_animating(self._cfg(), 0))

    def test_animating_none_when_no_pane_configured(self) -> None:
        self.assertIsNone(am.pane_is_animating({"sudo_user": "claude"}, 0))

    # ---- check_tui_wedge composition ----
    def _frozen(self, seconds_ago: int):
        """Patch liveness so the seat looks frozen `seconds_ago` seconds."""
        return patch.object(am, "liveness_jsonl_mtime",
                            return_value=int(time.time()) - seconds_ago)

    def test_fires_on_long_freeze_moving_pane_no_known_cause(self) -> None:
        with self._frozen(3600), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cap_state", return_value=None), \
             patch.object(am, "_session_cyber_blocked", return_value=False), \
             patch.object(am, "capture_pane", return_value="⠋ Thinking…"), \
             patch.object(am, "pane_is_animating", return_value=True):
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertTrue(fired)

    def test_duration_gate_not_fired_when_recent(self) -> None:
        with self._frozen(60), \
             patch.object(am, "pane_is_animating", return_value=True) as anim:
            fired, reason = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)
        anim.assert_not_called()  # cheap gate short-circuits before the probe

    def test_fail_open_when_no_mtime(self) -> None:
        with patch.object(am, "liveness_jsonl_mtime", return_value=0):
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)

    def test_defers_to_cap_modal(self) -> None:
        with self._frozen(3600), \
             patch.object(am, "_session_cap_modal", return_value=am.MODAL_SAFE), \
             patch.object(am, "pane_is_animating", return_value=True) as anim:
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)
        anim.assert_not_called()  # a capped seat is not even probed for motion

    def test_defers_to_cap_state(self) -> None:
        with self._frozen(3600), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cap_state", return_value=("spend_limit", None)), \
             patch.object(am, "pane_is_animating", return_value=True):
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)

    def test_defers_to_cyber_block(self) -> None:
        with self._frozen(3600), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cap_state", return_value=None), \
             patch.object(am, "_session_cyber_blocked", return_value=True), \
             patch.object(am, "pane_is_animating", return_value=True):
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)

    def test_still_pane_not_a_wedge(self) -> None:
        # Long freeze, no known cause, but pane STILL → idle-at-prompt, p1a's job.
        with self._frozen(3600), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cap_state", return_value=None), \
             patch.object(am, "_session_cyber_blocked", return_value=False), \
             patch.object(am, "pane_is_animating", return_value=False):
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)

    # T-4712: orchestrator 2026-10-08 01:30Z, idle main prompt + a 36-min background
    # implementer. Its elapsed timer ticks, so the pane "animates".
    _BG_AGENT_PANE = (
        "❯ \n  ⏸ manual mode on · 3 shells · ← for agents · ↓ to manage\n"
        "  ◯ implementer        Polling full tools pytest log  36m 7s · ↓ 265.2k tokens\n")

    def _unexplained_moving(self, pane_text):
        return [self._frozen(3600),
                patch.object(am, "_session_cap_modal", return_value=None),
                patch.object(am, "_session_cap_state", return_value=None),
                patch.object(am, "_session_cyber_blocked", return_value=False),
                patch.object(am, "capture_pane", return_value=pane_text),
                patch.object(am, "pane_is_animating", return_value=True)]

    def test_background_agent_footer_is_not_a_wedge(self) -> None:
        from contextlib import ExitStack
        with ExitStack() as st:
            for p in self._unexplained_moving(self._BG_AGENT_PANE):
                st.enter_context(p)
            fired, reason = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)
        self.assertIn("background agent", reason)

    # T-5605: orchestrator 2026-10-26 ~17:10Z. This Claude Code build renders background
    # agents as a footer agent list ('● main' then one '◯ <name> <task> <elapsed>
    # · ↓ <tokens>' row each) with NEITHER '⧉' nor '↓ to manage'. tui_wedge paged
    # a false WEDGED while 4 subagents ran.
    _AGENT_LIST_PANE = (
        "✢ Newspapering… (23s · ↓ 1.2k tokens)\n"
        "❯ \n"
        "  ⏵⏵ bypass permissions on · 5 shells\n"
        "  ● main\n"
        "  ◯ implementer    Checking smoke1.log for crash  1h 41m 18s · ↓ 361.2k tokens\n"
        "  ◯ installer-arc  Installer hardening arc orche…         9s · ↑ 114.5k tokens\n"
        "  ◯ implementer    Committing persona_installer …  1h 1m 32s · ↓ 130.0k tokens\n")

    def _agent_list_patches(self, first, second):
        return [self._frozen(3600),
                patch.object(am, "_session_cap_modal", return_value=None),
                patch.object(am, "_session_cap_state", return_value=None),
                patch.object(am, "_session_cyber_blocked", return_value=False),
                patch.object(am, "capture_pane", side_effect=[first, second]),
                patch.object(am.time, "sleep"),
                patch.object(am, "pane_is_animating", return_value=True)]

    def test_agent_list_footer_is_not_a_wedge(self) -> None:
        # A running row's timer moves between the two captures.
        later = self._AGENT_LIST_PANE.replace("1h 41m 18s", "1h 41m 20s")
        from contextlib import ExitStack
        with ExitStack() as st:
            for p in self._agent_list_patches(self._AGENT_LIST_PANE, later):
                st.enter_context(p)
            fired, reason = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)
        self.assertIn("background agent", reason)

    def test_agent_list_of_finished_rows_still_fires(self) -> None:
        # Finished agents keep their rows with FROZEN timers (orchestrator 17:10Z == 17:37Z):
        # an unchanged list must not mask a wedge.
        from contextlib import ExitStack
        with ExitStack() as st:
            for p in self._agent_list_patches(self._AGENT_LIST_PANE, self._AGENT_LIST_PANE):
                st.enter_context(p)
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertTrue(fired)

    def test_agent_list_with_only_idle_rows_still_fires(self) -> None:
        # Only rows with a RUNNING elapsed timer + token counter count; a list of
        # idle agents does not explain an animating, frozen main session.
        pane = "❯ \n  ● main\n  ◯ installer-arc  Installer hardening arc orche…   idle\n"
        from contextlib import ExitStack
        with ExitStack() as st:
            for p in self._unexplained_moving(pane):
                st.enter_context(p)
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertTrue(fired)

    def test_background_agent_glyph_is_not_a_wedge(self) -> None:
        from contextlib import ExitStack
        with ExitStack() as st:
            for p in self._unexplained_moving("⧉ impl-t4712 running\n"):
                st.enter_context(p)
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)

    def test_bg_check_capture_failure_still_fires(self) -> None:
        # A failed capture is not evidence of a background agent: fall through.
        from contextlib import ExitStack
        with ExitStack() as st:
            for p in self._unexplained_moving(None):
                st.enter_context(p)
            fired, _ = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertTrue(fired)

    def test_capture_failure_does_not_fire(self) -> None:
        # animating is None (capture failed) → must NOT fire (fail-open).
        with self._frozen(3600), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cap_state", return_value=None), \
             patch.object(am, "_session_cyber_blocked", return_value=False), \
             patch.object(am, "pane_is_animating", return_value=None):
            fired, reason = am.check_tui_wedge(self._cfg(), 1800, 2)
        self.assertFalse(fired)
        # Distinguish the fail-open (capture failed) reason from the "pane STILL"
        # reason — gives the dedicated None branch teeth (else it is an
        # equivalent-outcome mutant since `not None` also returns not-fired).
        self.assertIn("capture", reason)


class TuiWedgeMainT1369(unittest.TestCase):
    """T-1369 — main() wiring for the wedge branch: ship-dark by default, and
    when enabled it escalates a restart RECOMMENDATION (never auto-restarts) and
    pre-empts the poke predicates."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp / "state")

    def tearDown(self) -> None:
        for k in ("PANTHEON_STATE_DIR", "ACTIVITY_MONITOR_TUI_WEDGE_ENABLED"):
            os.environ.pop(k, None)
        self._tmp.cleanup()

    def test_ship_dark_flag_off_never_calls_wedge(self) -> None:
        os.environ.pop("ACTIVITY_MONITOR_TUI_WEDGE_ENABLED", None)
        with patch.object(am, "in_cooldown", return_value=False), \
             patch.object(am, "check_tui_wedge") as wedge, \
             patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False):
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        wedge.assert_not_called()

    def test_enabled_wedge_escalates_and_preempts_p1a(self) -> None:
        os.environ["ACTIVITY_MONITOR_TUI_WEDGE_ENABLED"] = "1"
        with patch.object(am, "in_cooldown", return_value=False), \
             patch.object(am, "check_tui_wedge", return_value=(True, "wedged")), \
             patch.object(am, "latest_jsonl_mtime", return_value=1000), \
             patch.object(am, "_route_failure_alert") as alert, \
             patch.object(am, "check_p1a") as p1a:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        alert.assert_called_once()          # paged Recovery with the restart recommendation
        p1a.assert_not_called()             # wedge pre-empts the poke predicates
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertTrue(json.loads(state.read_text())["escalated"])
        # T-3072: recorded under the escalation VOCAB ("tui-wedge"), not the
        # literal predicate string ("tui_wedge") main() passes to _escalate_stuck.
        fires_path = self.tmp / "state" / "activity_monitor" / "fires.jsonl"
        fires = [json.loads(line) for line in fires_path.read_text().splitlines()]
        self.assertEqual([f["predicate"] for f in fires], ["tui-wedge"])

    def test_wedge_page_suppressed_when_quiet_attributable(self) -> None:
        # T-2632 PART 2 wiring: a fired wedge whose quiet is ATTRIBUTABLE to a
        # declared signal (e.g. an active context_fill_hold) must NOT page Recovery —
        # the freeze is explained (a held session sitting in a long turn), not a
        # wedge. Downgrades the ALERT only; this branch never restarts. This is the
        # suppress-direction mutation guard (without the gate, alert WOULD fire).
        os.environ["ACTIVITY_MONITOR_TUI_WEDGE_ENABLED"] = "1"
        with patch.object(am, "in_cooldown", return_value=False), \
             patch.object(am, "check_tui_wedge", return_value=(True, "wedged")), \
             patch.object(am, "latest_jsonl_mtime", return_value=1000), \
             patch.object(am, "quiet_is_attributable",
                          return_value=(True, "context_fill_hold active")), \
             patch.object(am, "_route_failure_alert") as alert, \
             patch.object(am, "check_p1a") as p1a:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        alert.assert_not_called()   # attributable quiet → page SUPPRESSED
        p1a.assert_not_called()     # wedge branch still preempts the poke predicates
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        self.assertFalse(state.exists(), "no escalated state written on a suppressed page")

    def test_enabled_wedge_pages_once_across_cycles(self) -> None:
        os.environ["ACTIVITY_MONITOR_TUI_WEDGE_ENABLED"] = "1"
        with patch.object(am, "in_cooldown", return_value=False), \
             patch.object(am, "check_tui_wedge", return_value=(True, "wedged")), \
             patch.object(am, "latest_jsonl_mtime", return_value=1000), \
             patch.object(am, "_route_failure_alert") as alert:
            for _ in range(3):
                main(["--agent", "orchestrator"])
        self.assertEqual(alert.call_count, 1, "one page per episode, then bounded-quiet")

    def test_enabled_not_wedged_falls_through_to_predicates(self) -> None:
        os.environ["ACTIVITY_MONITOR_TUI_WEDGE_ENABLED"] = "1"
        with patch.object(am, "in_cooldown", return_value=False), \
             patch.object(am, "check_tui_wedge", return_value=(False, "fresh")), \
             patch.object(am, "check_p1a", return_value=False) as p1a, \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=False):
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        p1a.assert_called_once()  # not wedged → normal predicate flow runs


class QuietAttributionTests(unittest.TestCase):
    """T-2632 PART 2 (asserted-only core): quiet_is_attributable — a fail-safe
    query that reports whether a seat's quiet is explained by a DECLARED signal.
    The load-bearing property is the fail-safe direction: any error/ambiguity must
    return (False, ...) so the wedge signal proceeds — never masked."""

    def test_attributable_when_hold_active(self) -> None:
        with patch("tools.context_fill_hold.hold_status",
                   return_value={"active": True, "reason": "operator soak"}):
            ok, reason = am.quiet_is_attributable("orchestrator", now=1000)
        self.assertTrue(ok)
        self.assertIn("operator soak", reason)

    def test_unattributed_when_no_hold_and_no_signals(self) -> None:
        with patch("tools.context_fill_hold.hold_status", return_value={"active": False}):
            ok, reason = am.quiet_is_attributable("orchestrator", now=1000)
        self.assertFalse(ok)
        self.assertIn("unattributed", reason)

    def test_injected_signal_attributes(self) -> None:
        # The wiring pass's unit-disabled probe plugs in here.
        with patch("tools.context_fill_hold.hold_status", return_value={"active": False}):
            ok, reason = am.quiet_is_attributable(
                "specialist", now=1000,
                signals=[lambda a, n: (True, "session unit disabled (TG-6694 pause)")],
            )
        self.assertTrue(ok)
        self.assertIn("disabled", reason)

    def test_injected_signal_none_falls_through_to_unattributed(self) -> None:
        with patch("tools.context_fill_hold.hold_status", return_value={"active": False}):
            ok, reason = am.quiet_is_attributable(
                "specialist", now=1000, signals=[lambda a, n: None]
            )
        self.assertFalse(ok)

    def test_failsafe_a_raising_signal_never_attributes(self) -> None:
        # A broken signal must be swallowed and treated as 'no signal' — erring
        # toward NOT attributable (wedge proceeds), never toward masking.
        def boom(a, n):
            raise RuntimeError("probe blew up")
        with patch("tools.context_fill_hold.hold_status", return_value={"active": False}):
            ok, _ = am.quiet_is_attributable("orchestrator", now=1000, signals=[boom])
        self.assertFalse(ok)

    def test_failsafe_hold_lookup_error_never_masks(self) -> None:
        # If the hold lookup itself throws, quiet must read as UNATTRIBUTED so the
        # wedge still escalates — the T-2615 'never mask a wedge' invariant.
        with patch("tools.context_fill_hold.hold_status", side_effect=OSError("state dir gone")):
            ok, _ = am.quiet_is_attributable("orchestrator", now=1000)
        self.assertFalse(ok)

    def test_first_truthy_signal_wins_hold_precedes_injected(self) -> None:
        # Asserted hold is checked before injected signals; its reason is returned.
        called = []
        with patch("tools.context_fill_hold.hold_status",
                   return_value={"active": True, "reason": "held"}):
            ok, reason = am.quiet_is_attributable(
                "orchestrator", now=1000,
                signals=[lambda a, n: called.append(1) or (True, "should not reach")],
            )
        self.assertTrue(ok)
        self.assertIn("held", reason)
        self.assertEqual(called, [], "hold short-circuits before injected signals")


class TestLatestTelegramInboundTs(unittest.TestCase):
    """T-3075 helper unit tests — Gate-1 rewire from bridge to jsonl-parse."""

    def _jsonl_line(self, timestamp: str, telegram_ts: str, chat_id: str = "1") -> str:
        return json.dumps({
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": timestamp,
            "sessionId": "test",
            "content": (
                f'<channel source="plugin:telegram:telegram" chat_id="{chat_id}" '
                f'message_id="1" user="X" user_id="{chat_id}" ts="{telegram_ts}">\n'
                f'body\n</channel>'
            ),
        })

    def _mock_run(self, lines, returncode=0):
        return MagicMock(returncode=returncode, stdout="\n".join(lines), stderr="")

    def test_extracts_latest_ts(self):
        lines = [
            self._jsonl_line("2026-08-01T00:00:00Z", "2026-08-22T18:00:00.000Z"),
            self._jsonl_line("2026-08-01T00:00:00Z", "2026-08-22T17:00:00.000Z"),
            self._jsonl_line("2026-08-01T00:00:00Z", "2026-08-22T18:05:00.000Z"),
        ]
        with patch("subprocess.run", return_value=self._mock_run(lines)):
            ts = am.latest_telegram_inbound_ts(Path("/x"), "claude")
        # 18:05Z should win — later than 18:00 and 17:00.
        from datetime import datetime as _dt
        expected = int(_dt.fromisoformat("2026-08-22T18:05:00.000+00:00").timestamp())
        self.assertEqual(ts, expected)

    def test_no_telegram_lines_returns_none(self):
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
            json.dumps({"type": "queue-operation", "operation": "enqueue", "content": "unrelated"}),
        ]
        with patch("subprocess.run", return_value=self._mock_run(lines)):
            ts = am.latest_telegram_inbound_ts(Path("/x"), "claude")
        self.assertIsNone(ts)

    def test_subprocess_error_returns_none(self):
        with patch("subprocess.run", return_value=self._mock_run([], returncode=1)):
            ts = am.latest_telegram_inbound_ts(Path("/x"), "claude")
        self.assertIsNone(ts)

    def test_subprocess_timeout_returns_none(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tac", 30)):
            ts = am.latest_telegram_inbound_ts(Path("/x"), "claude")
        self.assertIsNone(ts)

    def test_ignores_non_enqueue_and_non_queue_operation(self):
        # Malformed but with the marker string — must NOT count (type/operation filter).
        lines = [
            json.dumps({
                "type": "queue-operation", "operation": "dequeue",
                "content": '<channel source="plugin:telegram:telegram" ts="2026-08-22T20:00:00Z">'
            }),
            json.dumps({
                "type": "assistant", "operation": "enqueue",
                "content": '<channel source="plugin:telegram:telegram" ts="2026-08-22T21:00:00Z">'
            }),
        ]
        with patch("subprocess.run", return_value=self._mock_run(lines)):
            ts = am.latest_telegram_inbound_ts(Path("/x"), "claude")
        self.assertIsNone(ts)

    def test_idle_user_turn_inbound_counts(self):
        # T-4212: a message to an IDLE HO is a `user` turn, not an enqueue.
        lines = [json.dumps({"type": "user", "message": {"role": "user", "content": (
            '<channel source="plugin:telegram:telegram" chat_id="1" message_id="1" '
            'user="X" user_id="1" ts="2026-08-22T19:00:00.000Z">\nhi\n</channel>')}})]
        with patch("subprocess.run", return_value=self._mock_run(lines)):
            ts = am.latest_telegram_inbound_ts(Path("/x"), "claude")
        from datetime import datetime as _dt
        self.assertEqual(ts, int(_dt.fromisoformat("2026-08-22T19:00:00+00:00").timestamp()))

    def test_mid_turn_queued_command_inbound_counts(self):
        # T-4212: delivered mid-turn = a `queued_command` attachment.
        lines = [json.dumps({"type": "attachment", "attachment": {"type": "queued_command", "prompt": (
            '<channel source="plugin:telegram:telegram" chat_id="1" message_id="2" '
            'user="X" user_id="1" ts="2026-08-22T19:30:00.000Z">\nhi\n</channel>')}})]
        with patch("subprocess.run", return_value=self._mock_run(lines)):
            ts = am.latest_telegram_inbound_ts(Path("/x"), "claude")
        from datetime import datetime as _dt
        self.assertEqual(ts, int(_dt.fromisoformat("2026-08-22T19:30:00+00:00").timestamp()))

    def test_quoted_tag_in_assistant_prose_does_not_count(self):
        lines = [json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": (
            '<channel source="plugin:telegram:telegram" ts="2026-08-22T22:00:00Z">')}]}})]
        with patch("subprocess.run", return_value=self._mock_run(lines)):
            ts = am.latest_telegram_inbound_ts(Path("/x"), "claude")
        self.assertIsNone(ts)

    def test_malformed_ts_ignored(self):
        lines = [
            self._jsonl_line("2026-08-01T00:00:00Z", "not-a-timestamp"),
        ]
        with patch("subprocess.run", return_value=self._mock_run(lines)):
            ts = am.latest_telegram_inbound_ts(Path("/x"), "claude")
        # Best-effort parse fails → None (rather than crash).
        self.assertIsNone(ts)


class TestWedgeSpecimenCapture(unittest.TestCase):
    """T-3558(b1): fail-open, flag-gated pane-specimen capture at wedge onset."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("PANTHEON_STATE_DIR")
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp / "state")

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("PANTHEON_STATE_DIR", None)
        else:
            os.environ["PANTHEON_STATE_DIR"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self) -> dict:
        return {"tmux_target_pane": "x:0", "sudo_user": "claude"}

    def test_capture_writes_specimen_with_pane_text(self) -> None:
        with patch.object(am, "capture_pane", return_value="WEDGE PANE TEXT\n❯ x to dismiss"):
            path, cls = am._capture_wedge_specimen(self._cfg(), "artisan", "stuck-poke")
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        self.assertIn("x to dismiss", path.read_text())
        self.assertEqual(path.parent, self.tmp / "state" / "activity_monitor" / "specimens")

    def test_capture_names_permission_modal_class_in_header(self) -> None:
        # T-3558(b2) wiring: a captured permission-modal pane gets a
        # '# detected-class: <perm>' header (the classifier's real caller — this
        # is the guard that classify_permission_modal is not dead code).
        pane = ("Do you want to proceed?\n ❯ 1. Yes\n"
                "   2. Yes, and don't ask again for Read(//tmp/**)\n   3. No\n\n"
                " Esc to cancel · Tab to amend\n")
        with patch.object(am, "capture_pane", return_value=pane):
            path, cls = am._capture_wedge_specimen(self._cfg(), "artisan", "stuck-poke")
        body = path.read_text()
        self.assertEqual(cls, "Read(//tmp/**)")  # returned class (for alert naming)
        self.assertTrue(body.startswith("# detected-class: Read(//tmp/**)\n"), body[:60])
        self.assertIn("Do you want to proceed?", body)  # raw pane still preserved

    def test_capture_none_when_pane_empty(self) -> None:
        with patch.object(am, "capture_pane", return_value=None):
            path, cls = am._capture_wedge_specimen(self._cfg(), "artisan", "stuck-poke")
        self.assertIsNone(path)
        self.assertFalse((self.tmp / "state" / "activity_monitor" / "specimens").exists())

    def test_capture_failopen_when_capture_raises(self) -> None:
        with patch.object(am, "capture_pane", side_effect=RuntimeError("tmux gone")):
            path, cls = am._capture_wedge_specimen(self._cfg(), "artisan", "stuck-poke")
        self.assertIsNone(path)  # swallowed, never raises

    def test_capture_prunes_to_keep_newest(self) -> None:
        with patch.object(am, "_WEDGE_SPECIMEN_KEEP", 3), \
             patch.object(am, "capture_pane", return_value="pane"):
            for _ in range(6):
                am._capture_wedge_specimen(self._cfg(), "mason", "stuck-poke")
                time.sleep(0.01)  # distinct mtimes for the newest-N prune
        d = self.tmp / "state" / "activity_monitor" / "specimens"
        self.assertEqual(len(list(d.glob("*.txt"))), 3)

    def test_escalate_captures_when_flag_on(self) -> None:
        sp = self.tmp / "state.json"
        os.environ["ACTIVITY_MONITOR_WEDGE_SPECIMEN_CAPTURE"] = "1"
        try:
            with patch.object(am, "_route_failure_alert"), \
                 patch.object(am, "capture_pane", return_value="live wedge pane") as cap:
                am._escalate_stuck(self._cfg(), "artisan", "p1b", "MSG", "stuck-poke",
                                   sp, cur_mtime=5000, consecutive=1)
            cap.assert_called_once()
            specimens = list((self.tmp / "state" / "activity_monitor" / "specimens").glob("*.txt"))
            self.assertEqual(len(specimens), 1)
        finally:
            os.environ.pop("ACTIVITY_MONITOR_WEDGE_SPECIMEN_CAPTURE", None)

    def test_escalate_alert_names_wedge_class_when_recognised(self) -> None:
        # T-3558(b2): when the flag is on AND the pane is a recognised permission
        # modal, the Recovery-facing alert names the class (off the SAME capture).
        sp = self.tmp / "state.json"
        os.environ["ACTIVITY_MONITOR_WEDGE_SPECIMEN_CAPTURE"] = "1"
        pane = ("Do you want to proceed?\n ❯ 1. Yes\n"
                "   2. Yes, and don't ask again for Read(//tmp/**)\n   3. No\n\n"
                " Esc to cancel · Tab to amend\n")
        try:
            with patch.object(am, "_route_failure_alert") as alert, \
                 patch.object(am, "capture_pane", return_value=pane):
                am._escalate_stuck(self._cfg(), "artisan", "p1b", "MSG", "stuck-poke",
                                   sp, cur_mtime=5000, consecutive=1)
            alert.assert_called_once()
            routed_msg = alert.call_args[0][1]
            self.assertIn("detected wedge class", routed_msg)
            self.assertIn("Read(//tmp/**)", routed_msg)
            self.assertIn("MSG", routed_msg)  # original message preserved
        finally:
            os.environ.pop("ACTIVITY_MONITOR_WEDGE_SPECIMEN_CAPTURE", None)

    def test_escalate_alert_names_a_focused_preview_chip(self) -> None:
        # T-3558(b2) chip slice, driven by the REAL replayed specimen: the alert
        # names the chip, and its hint is the by-hand clear, not the permission one.
        sp = self.tmp / "state.json"
        os.environ["ACTIVITY_MONITOR_WEDGE_SPECIMEN_CAPTURE"] = "1"
        pane = (REPO_ROOT / "tools" / "testdata"
                / "artisan_preview_chip_focused_pane.txt").read_text(encoding="utf-8")
        try:
            with patch.object(am, "_route_failure_alert") as alert, \
                 patch.object(am, "capture_pane", return_value=pane):
                am._escalate_stuck(self._cfg(), "artisan", "p1b", "MSG", "stuck-poke",
                                   sp, cur_mtime=5000, consecutive=1)
            routed_msg = alert.call_args[0][1]
            self.assertIn("detected wedge class → preview_chip:t2738-preview", routed_msg)
            self.assertIn("clear it by hand (x, then Enter)", routed_msg)
            self.assertNotIn("permission prompt", routed_msg)
            spec = next((self.tmp / "state" / "activity_monitor" / "specimens").glob("*.txt"))
            self.assertTrue(spec.read_text().startswith(
                "# detected-class: preview_chip:t2738-preview\n"))
        finally:
            os.environ.pop("ACTIVITY_MONITOR_WEDGE_SPECIMEN_CAPTURE", None)

    def test_escalate_alert_unchanged_when_class_unrecognised(self) -> None:
        # An unrecognised pane leaves the alert message exactly as passed (no
        # spurious "detected wedge class" suffix).
        sp = self.tmp / "state.json"
        os.environ["ACTIVITY_MONITOR_WEDGE_SPECIMEN_CAPTURE"] = "1"
        try:
            with patch.object(am, "_route_failure_alert") as alert, \
                 patch.object(am, "capture_pane", return_value="just an ordinary idle pane"):
                am._escalate_stuck(self._cfg(), "artisan", "p1b", "MSG-ONLY", "stuck-poke",
                                   sp, cur_mtime=5000, consecutive=1)
            self.assertEqual(alert.call_args[0][1], "MSG-ONLY")
        finally:
            os.environ.pop("ACTIVITY_MONITOR_WEDGE_SPECIMEN_CAPTURE", None)

    def test_escalate_no_capture_when_flag_off(self) -> None:
        # Default (flag absent): capture_pane must NOT be called — this is the
        # guard that the existing escalation tests keep their no-tmux behavior.
        sp = self.tmp / "state.json"
        os.environ.pop("ACTIVITY_MONITOR_WEDGE_SPECIMEN_CAPTURE", None)
        with patch.object(am, "_route_failure_alert"), \
             patch.object(am, "capture_pane") as cap:
            am._escalate_stuck(self._cfg(), "artisan", "p1b", "MSG", "stuck-poke",
                               sp, cur_mtime=5000, consecutive=1)
        cap.assert_not_called()

    def test_escalate_capture_only_on_transition_not_repeat(self) -> None:
        # Capture rides the False->True escalated transition (the `not already`
        # branch), so a persistent wedge captures ONCE, not every cycle.
        sp = self.tmp / "state.json"
        os.environ["ACTIVITY_MONITOR_WEDGE_SPECIMEN_CAPTURE"] = "1"
        try:
            with patch.object(am, "_route_failure_alert"), \
                 patch.object(am, "capture_pane", return_value="pane") as cap:
                for _ in range(3):
                    am._escalate_stuck(self._cfg(), "artisan", "p1b", "MSG", "stuck-poke",
                                       sp, cur_mtime=5000, consecutive=1)
            cap.assert_called_once()
        finally:
            os.environ.pop("ACTIVITY_MONITOR_WEDGE_SPECIMEN_CAPTURE", None)


class P1bHandledWaitBackoffT3991(unittest.TestCase):
    """T-3991 — P1.b handled-wait backoff: streak counter, ticket probe, and the
    combined cooldown decision that stretches p1b once a repeated wait is ticketed."""

    GAP = 90 * 60  # matches P1B_BACKOFF_STREAK_GAP_SECONDS default

    def _fire(self, ts: int, predicate: str = "p1b", agent: str = "orchestrator") -> dict:
        return {"ts": ts, "predicate": predicate, "agent": agent}

    # --- count_consecutive_p1b_fires (pure) -------------------------------

    def test_streak_empty(self) -> None:
        self.assertEqual(am.count_consecutive_p1b_fires([], 1000, self.GAP), (0, None))

    def test_streak_single_in_window(self) -> None:
        now = 10_000
        fires = [self._fire(now - 60)]
        self.assertEqual(
            am.count_consecutive_p1b_fires(fires, now, self.GAP), (1, now - 60)
        )

    def test_streak_three_consecutive(self) -> None:
        now = 100_000
        fires = [
            self._fire(now - 3600),  # oldest
            self._fire(now - 1800),
            self._fire(now - 60),    # newest
        ]
        count, start = am.count_consecutive_p1b_fires(fires, now, self.GAP)
        self.assertEqual(count, 3)
        self.assertEqual(start, now - 3600)

    def test_streak_unsorted_input_handled(self) -> None:
        now = 100_000
        fires = [
            self._fire(now - 60),
            self._fire(now - 3600),
            self._fire(now - 1800),
        ]
        # Same run regardless of input order — the function sorts.
        self.assertEqual(
            am.count_consecutive_p1b_fires(fires, now, self.GAP), (3, now - 3600)
        )

    def test_streak_breaks_on_internal_gap(self) -> None:
        now = 100_000
        # Oldest fire is > GAP before the middle one → the run stops at the two recent.
        fires = [
            self._fire(now - (self.GAP * 3)),  # far past — beyond the gap
            self._fire(now - 1800),
            self._fire(now - 60),
        ]
        count, start = am.count_consecutive_p1b_fires(fires, now, self.GAP)
        self.assertEqual(count, 2)          # NOT 3 — the gap severs the old fire
        self.assertEqual(start, now - 1800)

    def test_streak_stale_when_newest_beyond_gap(self) -> None:
        now = 100_000
        fires = [self._fire(now - (self.GAP + 60))]  # newest already too old
        self.assertEqual(am.count_consecutive_p1b_fires(fires, now, self.GAP), (0, None))

    def test_streak_breaks_on_other_predicate(self) -> None:
        now = 100_000
        fires = [
            self._fire(now - 1800, predicate="p1b"),
            self._fire(now - 900, predicate="p1a"),  # a different predicate fired
            self._fire(now - 60, predicate="p1b"),
        ]
        # Only the trailing p1b counts; the p1a severs the run.
        count, start = am.count_consecutive_p1b_fires(fires, now, self.GAP)
        self.assertEqual(count, 1)
        self.assertEqual(start, now - 60)

    # --- _parse_ticket_created_epoch --------------------------------------

    def test_parse_created_z(self) -> None:
        got = am._parse_ticket_created_epoch(" 2026-09-25T01:50:00Z")
        want = int(datetime(2026, 9, 25, 1, 50, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(got, want)

    def test_parse_created_date_only(self) -> None:
        got = am._parse_ticket_created_epoch("2026-09-25")
        want = int(datetime(2026, 9, 25, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(got, want)

    def test_parse_created_garbage_none(self) -> None:
        self.assertIsNone(am._parse_ticket_created_epoch("not-a-date"))

    # --- has_recent_operator_ticket (subprocess mocked) --------------------

    def _run_result(self, stdout: str) -> MagicMock:
        r = MagicMock()
        r.stdout = stdout
        return r

    def test_operator_ticket_present_after_since(self) -> None:
        since = int(datetime(2026, 9, 25, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        out = "created: 2026-09-25T05:00:00Z\n"
        with patch.object(am.subprocess, "run", return_value=self._run_result(out)):
            self.assertTrue(am.has_recent_operator_ticket(Path("/x"), "claude", since))

    def test_operator_ticket_only_before_since(self) -> None:
        since = int(datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc).timestamp())
        out = "created: 2026-09-25T05:00:00Z\n"  # earlier than since → no evidence
        with patch.object(am.subprocess, "run", return_value=self._run_result(out)):
            self.assertFalse(am.has_recent_operator_ticket(Path("/x"), "claude", since))

    def test_operator_ticket_none(self) -> None:
        since = 0
        with patch.object(am.subprocess, "run", return_value=self._run_result("")):
            self.assertFalse(am.has_recent_operator_ticket(Path("/x"), "claude", since))

    def test_operator_ticket_subprocess_error_is_false(self) -> None:
        with patch.object(am.subprocess, "run", side_effect=OSError("boom")):
            self.assertFalse(am.has_recent_operator_ticket(Path("/x"), "claude", 0))

    # --- p1b_effective_cooldown (streak + ticket combined) ----------------

    def _cfg(self) -> dict:
        return {"pantheon_config_path": Path("/home/claude/pantheon-config"),
                "sudo_user": "claude"}

    def test_cooldown_disabled_returns_base_no_reads(self) -> None:
        with patch.object(am, "_read_recent_fires") as rf, \
                patch.object(am, "has_recent_operator_ticket") as ht:
            got = am.p1b_effective_cooldown(
                "orchestrator", self._cfg(), 1800, enabled=False,
                backoff_cooldown=3600, after_fires=2, streak_gap_seconds=self.GAP)
        self.assertEqual(got, 1800)
        rf.assert_not_called()
        ht.assert_not_called()

    def test_cooldown_streak_below_threshold_returns_base_no_ticket_probe(self) -> None:
        now = 100_000
        fires = [self._fire(now - 60)]  # streak 1 < after_fires 2
        with patch.object(am, "_read_recent_fires", return_value=fires), \
                patch.object(am, "has_recent_operator_ticket") as ht:
            got = am.p1b_effective_cooldown(
                "orchestrator", self._cfg(), 1800, enabled=True,
                backoff_cooldown=3600, after_fires=2,
                streak_gap_seconds=self.GAP, now=now)
        self.assertEqual(got, 1800)
        ht.assert_not_called()  # lazy: no sudo probe when the streak gate fails

    def test_cooldown_streak_met_but_no_ticket_returns_base(self) -> None:
        now = 100_000
        fires = [self._fire(now - 1800), self._fire(now - 60)]  # streak 2
        with patch.object(am, "_read_recent_fires", return_value=fires), \
                patch.object(am, "has_recent_operator_ticket", return_value=False):
            got = am.p1b_effective_cooldown(
                "orchestrator", self._cfg(), 1800, enabled=True,
                backoff_cooldown=3600, after_fires=2,
                streak_gap_seconds=self.GAP, now=now)
        self.assertEqual(got, 1800)

    def test_cooldown_streak_met_and_ticketed_returns_backoff(self) -> None:
        now = 100_000
        fires = [self._fire(now - 1800), self._fire(now - 60)]  # streak 2
        with patch.object(am, "_read_recent_fires", return_value=fires), \
                patch.object(am, "has_recent_operator_ticket", return_value=True) as ht:
            got = am.p1b_effective_cooldown(
                "orchestrator", self._cfg(), 1800, enabled=True,
                backoff_cooldown=3600, after_fires=2,
                streak_gap_seconds=self.GAP, now=now)
        self.assertEqual(got, 3600)
        # probed with the streak-start ts as the since bound
        _, kwargs = ht.call_args if ht.call_args.kwargs else (ht.call_args.args, {})
        self.assertEqual(ht.call_args.args[2], now - 1800)

    def test_read_recent_fires_filters_by_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prev = os.environ.get("PANTHEON_STATE_DIR")
            os.environ["PANTHEON_STATE_DIR"] = str(Path(tmp) / "state")
            try:
                am._record_fire("orchestrator", "p1b")
                am._record_fire("specialist", "p1b")
                am._record_fire("orchestrator", "p1a")
                got = am._read_recent_fires("orchestrator")
                self.assertEqual([r["predicate"] for r in got], ["p1b", "p1a"])
                self.assertTrue(all(r["agent"] == "orchestrator" for r in got))
            finally:
                if prev is None:
                    os.environ.pop("PANTHEON_STATE_DIR", None)
                else:
                    os.environ["PANTHEON_STATE_DIR"] = prev


class TestSendPokeQuoting(unittest.TestCase):
    """T-4141: send_poke built its `bash -c` string with json.dumps(prompt), which is a
    DOUBLE-quoted shell string, so backticks and $(...) in the prompt were EXECUTED as
    the target user (P1B_POKE_PROMPT's backticked ls arrived as '()'). The prompt must
    reach the helper byte-for-byte and run nothing."""

    def test_prompt_reaches_helper_verbatim_and_executes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            tools_dir = os.path.join(d, "tools")
            os.makedirs(tools_dir)
            got, side = os.path.join(d, "got.txt"), os.path.join(d, "side-effect")
            with open(os.path.join(tools_dir, "send_keys_submit.sh"), "w") as fh:
                fh.write('#!/bin/bash\nprintf "%s" "$2" > "' + got + '"\n')
            prompt = ("check (`ls ~/.claude/teams/*/inboxes/`) and $(touch " + side + ") "
                      "and 'quotes' \"dq\" $HOME \\ end")
            cfg = {"tmux_target_pane": "main:0.0", "sudo_user": "nobody",
                   "pantheon_config_path": d}
            with patch.object(am.subprocess, "run") as run:
                run.return_value = MagicMock(returncode=0)
                self.assertTrue(am.send_poke(cfg, prompt))
            cmd = run.call_args[0][0]
            self.assertEqual(cmd[:3], ["sudo", "-nu", "nobody"])
            self.assertEqual(cmd[3:5], ["bash", "-c"])
            # Run the exact script the monitor would hand to sudo, minus the sudo hop.
            subprocess.run(["bash", "-c", cmd[5]], check=True, timeout=10)
            with open(got) as fh:
                self.assertEqual(fh.read(), prompt)
            self.assertFalse(os.path.exists(side), "prompt text was executed as shell")


class TestFlushStuckPoke(unittest.TestCase):
    """T-4141: a poke that never submitted sits in the composer, the busy-guard then
    reads the pane BUSY (text under the manage footer) and defers every later poke,
    and later injections append to it (artisan 2026-09-28, stuck ~1h until one manual
    Enter). Before poking, flush a stuck poke with ONE bare Enter, bounded."""

    def _pane(self, footer):
        return ("● earlier output\n" + "─" * 60 + "\n❯ " + am.P1B_POKE_PROMPT + "\n"
                + "─" * 60 + "\n  " + footer + "\n")

    def _dispatch(self, pane_text, state_path):
        cfg = {"tmux_target_pane": "main:0.0", "sudo_user": "nobody",
               "jsonl_dir": "/nonexistent", "pantheon_config_path": "/nonexistent"}
        with patch.object(am, "capture_pane", return_value=pane_text), \
             patch.object(am, "send_bare_enter", return_value=True) as enter, \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = am._dispatch_poke(cfg, "artisan", "p1b", am.P1B_POKE_PROMPT, state_path,
                                   bounded_enabled=False, max_ineffective=3)
        return rc, enter, poke

    def test_stuck_poke_on_a_not_busy_pane_gets_one_enter_and_no_new_poke(self):
        with tempfile.TemporaryDirectory() as d:
            rc, enter, poke = self._dispatch(
                self._pane("⏸ manual mode on · 2 shells · ↓ to manage"), Path(d) / "s.json")
            self.assertEqual(rc, 0)
            enter.assert_called_once()
            poke.assert_not_called()

    def test_no_flush_while_generating(self):
        with tempfile.TemporaryDirectory() as d:
            _, enter, _ = self._dispatch(
                self._pane("✻ Working… (esc to interrupt)"), Path(d) / "s.json")
            enter.assert_not_called()

    def test_flush_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "s.json"
            pane = self._pane("⏸ manual mode on · 2 shells · ↓ to manage")
            calls = sum(self._dispatch(pane, state)[1].call_count
                        for _ in range(am.MAX_STUCK_POKE_REPRESSES + 2))
            self.assertEqual(calls, am.MAX_STUCK_POKE_REPRESSES)


class FleetUsageGateT4510(unittest.TestCase):
    """T-4510 — at >=95% of the fleet's shared weekly Max the periodic anti-idle/queue
    pokes (P1.a, P1.b) suppress themselves; every RECOVERY action stays live at 99%.

    The gate sits at the content-poke send points inside _dispatch_poke, AFTER the
    modal / cap / cyber / effectiveness branches — a gate at the top would block the
    cap-modal Enter-confirm at exactly the usage level where that modal appears."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.fleet = self.tmp / "fleet-usage"
        self.fleet.mkdir()
        patcher = patch.dict(os.environ, {
            "PANTHEON_FLEET_USAGE_DIR": str(self.fleet),
            "PANTHEON_STATE_DIR": str(self.tmp / "state"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cfg(self) -> dict:
        return {"tmux_target_pane": "x:0", "sudo_user": "claude",
                "jsonl_dir": Path("/x"), "pantheon_config_path": Path("/x")}

    def _reading(self, pct, *, seat="orchestrator", resets_in=3 * 86400, window="seven_day"):
        now = time.time()
        doc = {"schema": "pantheon-fleet-usage/v1", "ts": now - 5, "seat": seat,
               "seven_day": None, "five_hour": None}
        doc[window] = {"used_percentage": pct, "resets_at": now + resets_in}
        (self.fleet / f"{seat}.json").write_text(json.dumps(doc), encoding="utf-8")

    def _dispatch(self, predicate="p1a", *, bounded, sp=None):
        sp = sp or (self.tmp / "poke_state.json")
        buf = io.StringIO()
        with patch.object(am, "_flush_stuck_poke", return_value=False), \
             patch.object(am, "_pane_idle_probe", return_value=None), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cap_state", return_value=None), \
             patch.object(am, "_session_cyber_blocked", return_value=False), \
             patch.object(am, "latest_jsonl_mtime", return_value=5000), \
             patch.object(am, "_record_fire") as fire, \
             patch.object(am, "send_poke", return_value=True) as poke, \
             redirect_stdout(buf):
            rc = am._dispatch_poke(self._cfg(), "orchestrator", predicate, "PROMPT", sp,
                                   bounded_enabled=bounded, max_ineffective=1)
        return rc, poke, fire, buf.getvalue(), sp

    # -- suppression at/above the threshold ---------------------------------------
    def test_p1a_and_p1b_are_suppressed_at_99_in_both_modes(self) -> None:
        self._reading(99)
        for predicate in ("p1a", "p1b"):
            for bounded in (False, True):
                rc, poke, fire, _out, sp = self._dispatch(predicate, bounded=bounded)
                # the sentinel, not an exit code: main() must fall through to P1.d/P1.c
                self.assertEqual(rc, am.USAGE_SUPPRESSED, (predicate, bounded))
                poke.assert_not_called()
                fire.assert_not_called()
                self.assertIsNone(am.read_poke_state(sp),
                                  "no poke state => no cooldown consumed, resumes at once")

    def test_suppressed_at_exactly_95(self) -> None:
        self._reading(95)
        _rc, poke, *_ = self._dispatch("p1a", bounded=False)
        poke.assert_not_called()

    def test_below_threshold_still_pokes(self) -> None:
        self._reading(94)
        for bounded in (False, True):
            rc, poke, fire, _out, _sp = self._dispatch("p1a", bounded=bounded)
            self.assertEqual(rc, 0)
            poke.assert_called_once()
            fire.assert_called_once()

    def test_expired_reading_fails_open(self) -> None:
        self._reading(99, resets_in=-10)
        _rc, poke, *_ = self._dispatch("p1b", bounded=True)
        poke.assert_called_once()

    def test_no_readings_fails_open(self) -> None:
        _rc, poke, *_ = self._dispatch("p1a", bounded=False)
        poke.assert_called_once()

    def test_kill_switch_env_disables_the_gate(self) -> None:
        self._reading(99)
        with patch.dict(os.environ, {"PANTHEON_USAGE_GATE_ENABLED": "0"}):
            _rc, poke, *_ = self._dispatch("p1a", bounded=False)
        poke.assert_called_once()

    def test_pokes_resume_when_the_reading_expires(self) -> None:
        self._reading(99, resets_in=3600)
        _rc, poke, *_ = self._dispatch("p1a", bounded=False)
        poke.assert_not_called()
        self._reading(99, resets_in=-1)   # the window rolled
        _rc, poke, *_ = self._dispatch("p1a", bounded=False)
        poke.assert_called_once()

    # -- logging -------------------------------------------------------------------
    def test_suppression_logs_once_per_window_per_predicate_not_once_per_tick(self) -> None:
        self._reading(99)
        outs = [self._dispatch("p1a", bounded=False)[3] for _ in range(3)]
        self.assertIn("fleet usage gate", outs[0])
        self.assertIn("p1a", outs[0])
        self.assertEqual(outs[1], "")
        self.assertEqual(outs[2], "")
        # p1b is its own consumer key: logs once, then quiet
        self.assertIn("fleet usage gate", self._dispatch("p1b", bounded=False)[3])
        self.assertEqual(self._dispatch("p1b", bounded=False)[3], "")

    def test_a_new_window_logs_again(self) -> None:
        self._reading(99, resets_in=3 * 86400)
        self.assertIn("fleet usage gate", self._dispatch("p1a", bounded=False)[3])
        self._reading(99, resets_in=6 * 86400)   # different resets_at => new window
        self.assertIn("fleet usage gate", self._dispatch("p1a", bounded=False)[3])

    # -- what is deliberately NOT gated ---------------------------------------------
    def test_p1c_is_not_gated(self) -> None:
        # Recovery-adjacent liveness backstop; ships dark. Gating it is a one-entry change.
        self._reading(99)
        _rc, poke, *_ = self._dispatch("p1c", bounded=False)
        poke.assert_called_once()
        self.assertNotIn("p1c", am.USAGE_GATED_PREDICATES)

    # -- recovery carve-out: unaffected at 99% ---------------------------------------
    def test_cap_modal_dismissal_still_fires_at_99(self) -> None:
        self._reading(99)
        sp = self.tmp / "poke_state.json"
        with patch.object(am, "_flush_stuck_poke", return_value=False), \
             patch.object(am, "_pane_idle_probe", return_value=None), \
             patch.object(am, "_session_cap_modal", return_value=am.MODAL_SAFE), \
             patch.object(am, "send_modal_confirm", return_value=True) as confirm, \
             patch.object(am, "latest_jsonl_mtime", return_value=5000), \
             patch.object(am, "_record_fire"), \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1a", "PROMPT", sp,
                                   bounded_enabled=True, max_ineffective=1)
        self.assertEqual(rc, 0)
        confirm.assert_called_once()     # recovery: the modal Enter-confirm still goes out
        poke.assert_not_called()         # and no content poke rides along

    def test_spend_cap_and_cyber_escalations_still_fire_at_99(self) -> None:
        self._reading(99)
        sp = self.tmp / "poke_state.json"
        for cap, cyber, vocab in ((("spend_limit", None), False, "spend-cap"),
                                  (None, True, "cyber-refusal")):
            with patch.object(am, "_flush_stuck_poke", return_value=False), \
                 patch.object(am, "_pane_idle_probe", return_value=None), \
                 patch.object(am, "_session_cap_modal", return_value=None), \
                 patch.object(am, "latest_jsonl_mtime", return_value=5000), \
                 patch.object(am, "_record_fire"), \
                 patch.object(am, "send_poke", return_value=True), \
                 patch.object(am, "_session_cap_state", return_value=cap), \
                 patch.object(am, "_session_cyber_blocked", return_value=cyber), \
                 patch.object(am, "_escalate_stuck") as esc:
                am._dispatch_poke(self._cfg(), "orchestrator", "p1a", "PROMPT", sp,
                                  bounded_enabled=True, max_ineffective=1)
            esc.assert_called_once()
            self.assertEqual(esc.call_args.kwargs["vocab"], vocab)

    def test_effectiveness_escalation_still_fires_at_99(self) -> None:
        self._reading(99)
        sp = self.tmp / "poke_state.json"
        am.write_poke_state(sp, "orchestrator", "p1a", pre_poke_jsonl_mtime=5000,
                            consecutive_ineffective=0)
        with patch.object(am, "_flush_stuck_poke", return_value=False), \
             patch.object(am, "_pane_idle_probe", return_value=None), \
             patch.object(am, "_session_cap_modal", return_value=None), \
             patch.object(am, "_session_cap_state", return_value=None), \
             patch.object(am, "_session_cyber_blocked", return_value=False), \
             patch.object(am, "latest_jsonl_mtime", return_value=5000), \
             patch.object(am, "_record_fire"), \
             patch.object(am, "_escalate_stuck") as esc, \
             patch.object(am, "send_poke", return_value=True) as poke:
            am._dispatch_poke(self._cfg(), "orchestrator", "p1a", "PROMPT", sp,
                              bounded_enabled=True, max_ineffective=1)
        esc.assert_called_once()
        poke.assert_not_called()

    def test_stuck_poke_flush_still_runs_at_99(self) -> None:
        self._reading(99)
        sp = self.tmp / "poke_state.json"
        with patch.object(am, "_flush_stuck_poke", return_value=True) as flush, \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = am._dispatch_poke(self._cfg(), "orchestrator", "p1a", "PROMPT", sp,
                                   bounded_enabled=False, max_ineffective=1)
        self.assertEqual(rc, 0)
        flush.assert_called_once()
        poke.assert_not_called()

    def test_p1d_retry_inject_still_fires_through_main_at_99(self) -> None:
        self._reading(99)
        with patch.object(am, "check_p1a", return_value=False), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=True), \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        poke.assert_called_once()
        self.assertIn("rate-limit", poke.call_args[0][1].lower())

    def test_tui_wedge_escalation_still_fires_through_main_at_99(self) -> None:
        self._reading(99)
        with patch.dict(os.environ, {"ACTIVITY_MONITOR_TUI_WEDGE_ENABLED": "1"}), \
             patch.object(am, "in_cooldown", return_value=False), \
             patch.object(am, "check_tui_wedge", return_value=(True, "wedged")), \
             patch.object(am, "latest_jsonl_mtime", return_value=1000), \
             patch.object(am, "quiet_is_attributable", return_value=(False, "unattributed")), \
             patch.object(am, "_route_failure_alert") as alert:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        alert.assert_called_once()

    # -- main()-level wiring ---------------------------------------------------------
    def test_p1a_and_p1b_through_main_are_suppressed_at_99_and_resume_below(self) -> None:
        state = self.tmp / "state" / "activity_monitor_orchestrator_last_poke.json"
        for check_a, check_b in ((True, False), (False, True)):
            self._reading(99)
            with patch.object(am, "check_p1a", return_value=check_a), \
                 patch.object(am, "check_p1b", return_value=check_b), \
                 patch.object(am, "check_p1d", return_value=False), \
                 patch.object(am, "_pane_idle_probe", return_value=None), \
                 patch.object(am, "send_poke", return_value=True) as poke:
                rc = main(["--agent", "orchestrator"])
            self.assertEqual(rc, 0)
            poke.assert_not_called()
            self.assertFalse(state.exists(), "a suppressed poke must not start a cooldown")
            self._reading(40)
            with patch.object(am, "check_p1a", return_value=check_a), \
                 patch.object(am, "check_p1b", return_value=check_b), \
                 patch.object(am, "check_p1d", return_value=False), \
                 patch.object(am, "_pane_idle_probe", return_value=None), \
                 patch.object(am, "send_poke", return_value=True) as poke:
                main(["--agent", "orchestrator"])
            poke.assert_called_once()
            state.unlink()   # drop the cooldown the resumed poke wrote

    def test_gate_error_fails_open(self) -> None:
        with patch.object(am.fleet_usage_gate, "evaluate", side_effect=RuntimeError("boom")):
            _rc, poke, *_ = self._dispatch("p1a", bounded=False)
        poke.assert_called_once()

    # -- REBOUND #1: a suppressed P1.a/P1.b must NOT shadow the P1.d retry-inject ------
    # The existing main()-level test above hand-sets check_p1d=False, which hides the
    # ordering. These drive the REAL main() ordering: P1.a/P1.b dispatch, then P1.d, then P1.c.
    # check_p1a counts error turns as non-activity (T-2387), so a seat stuck on an API-error
    # abort for >1h with queue>=1 satisfies BOTH p1a and p1d at once.

    def _main_pokes(self, *, p1a=False, p1b=False, p1c=False, p1d=False, extra_env=None):
        """Run the real main() with the predicates stubbed to the given truth values;
        returns (rc, send_poke mock, check_p1d mock)."""
        env = {"PANTHEON_LAYER3_ENABLED": "1"} if p1c else {}
        env.update(extra_env or {})
        with patch.dict(os.environ, env), \
             patch.object(am, "check_p1a", return_value=p1a), \
             patch.object(am, "check_p1b", return_value=p1b), \
             patch.object(am, "check_p1c", return_value=p1c), \
             patch.object(am, "check_p1d", return_value=p1d) as c1d, \
             patch.object(am, "_pane_idle_probe", return_value=None), \
             patch.object(am, "_flush_stuck_poke", return_value=False), \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        return rc, poke, c1d

    def _sent_prompts(self, poke) -> list:
        return [c.args[1] for c in poke.call_args_list]

    def test_suppressed_p1a_does_not_shadow_p1d_retry_at_99(self) -> None:
        self._reading(99)
        rc, poke, c1d = self._main_pokes(p1a=True, p1d=True)
        self.assertEqual(rc, 0)
        c1d.assert_called_once()
        self.assertEqual(self._sent_prompts(poke), [am.P1D_RETRY_PROMPT],
                         "the recovery retry-inject must still go out; no p1a poke with it")

    def test_suppressed_p1b_does_not_shadow_p1d_retry_at_99(self) -> None:
        self._reading(99)
        rc, poke, c1d = self._main_pokes(p1b=True, p1d=True)
        self.assertEqual(rc, 0)
        c1d.assert_called_once()
        self.assertEqual(self._sent_prompts(poke), [am.P1D_RETRY_PROMPT])

    def test_p1a_and_p1b_both_true_still_reaches_p1d_at_99(self) -> None:
        self._reading(99)
        rc, poke, _c1d = self._main_pokes(p1a=True, p1b=True, p1d=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self._sent_prompts(poke), [am.P1D_RETRY_PROMPT])

    def test_p1d_send_failure_after_a_suppressed_p1a_still_returns_1(self) -> None:
        # The fall-through must not swallow P1.d's own failure semantics.
        self._reading(99)
        with patch.object(am, "check_p1a", return_value=True), \
             patch.object(am, "check_p1b", return_value=False), \
             patch.object(am, "check_p1d", return_value=True), \
             patch.object(am, "_pane_idle_probe", return_value=None), \
             patch.object(am, "_flush_stuck_poke", return_value=False), \
             patch.object(am, "send_poke", return_value=False):
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 1)

    def test_suppressed_p1a_falls_through_to_the_ungated_p1c(self) -> None:
        self._reading(99)
        rc, poke, _c1d = self._main_pokes(p1a=True, p1c=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self._sent_prompts(poke), [am.P1C_POKE_PROMPT])

    def test_below_threshold_p1a_still_returns_early_and_p1d_is_not_reached(self) -> None:
        # Today's order, unchanged: a delivered p1a poke ends the cycle.
        self._reading(40)
        rc, poke, c1d = self._main_pokes(p1a=True, p1d=True)
        self.assertEqual(rc, 0)
        c1d.assert_not_called()
        self.assertEqual(self._sent_prompts(poke), [am.P1A_POKE_PROMPT])

    def test_no_reading_p1b_still_returns_early_and_p1d_is_not_reached(self) -> None:
        rc, poke, c1d = self._main_pokes(p1b=True, p1d=True)
        self.assertEqual(rc, 0)
        c1d.assert_not_called()
        self.assertEqual(self._sent_prompts(poke), [am.P1B_POKE_PROMPT])

    def test_a_non_gate_early_return_from_p1a_is_not_turned_into_a_fall_through(self) -> None:
        # e.g. the stuck-poke flush returns 0 (handled): that must still end main(), not
        # be mistaken for the gate's sentinel.
        self._reading(40)
        with patch.object(am, "check_p1a", return_value=True), \
             patch.object(am, "check_p1d", return_value=True) as c1d, \
             patch.object(am, "_flush_stuck_poke", return_value=True), \
             patch.object(am, "send_poke", return_value=True) as poke:
            rc = main(["--agent", "orchestrator"])
        self.assertEqual(rc, 0)
        c1d.assert_not_called()
        poke.assert_not_called()


class PokeLandedKeysOnEntriesT5367(unittest.TestCase):
    """T-5367: an idle metadata rewrite advances the jsonl mtime with no new turn. The
    poke-landed check must not read that as 'the session took a turn'."""

    def setUp(self):
        _ENTRY_STUB.stop()  # drive the real seam here
        self.addCleanup(_ENTRY_STUB.start)

    @staticmethod
    def _rec(ts, typ="assistant"):
        return json.dumps({"type": typ, "timestamp": ts})

    def _stdout(self, *tails):
        return b"".join(b"\0" + t.encode() for t in tails)

    def test_entry_epoch_is_the_max_timestamp_across_the_newest_files(self):
        t1 = "cut-first-line\n" + self._rec("2026-10-22T10:00:00Z") + "\n"
        t2 = ("cut\n" + self._rec("2026-10-22T11:00:00Z", "user") + "\n"
              + json.dumps({"type": "last-prompt", "timestamp": "2026-10-22T15:00:00Z"}) + "\n"
              + '{"type": "ai-title"}\n')
        done = subprocess.CompletedProcess([], 0, stdout=self._stdout(t2, t1), stderr=b"")
        with patch.object(am.subprocess, "run", return_value=done) as run:
            got = am._jsonl_entry_epoch(Path("/j"), "u")
        self.assertEqual(got, datetime(2026, 10, 22, 11, tzinfo=timezone.utc).timestamp(),
                         "metadata types and untimestamped lines never count")
        self.assertIn("sudo", run.call_args.args[0][0])

    def test_entry_epoch_is_none_on_failure_or_no_entries(self):
        with patch.object(am.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertIsNone(am._jsonl_entry_epoch(Path("/j"), "u"))
        empty = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"")
        with patch.object(am.subprocess, "run", return_value=empty):
            self.assertIsNone(am._jsonl_entry_epoch(Path("/j"), "u"))

    def test_activity_is_the_entry_when_an_idle_rewrite_moved_the_mtime(self):
        with patch.object(am, "latest_jsonl_mtime", return_value=5000), \
             patch.object(am, "_jsonl_entry_epoch", return_value=1000.7):
            self.assertEqual(am.latest_jsonl_activity(Path("/j"), "u"), 1000)

    def test_activity_is_capped_by_mtime_and_falls_back_to_it(self):
        with patch.object(am, "latest_jsonl_mtime", return_value=5000), \
             patch.object(am, "_jsonl_entry_epoch", return_value=9000.0):
            self.assertEqual(am.latest_jsonl_activity(Path("/j"), "u"), 5000)
        with patch.object(am, "latest_jsonl_mtime", return_value=5000), \
             patch.object(am, "_jsonl_entry_epoch", return_value=None):
            self.assertEqual(am.latest_jsonl_activity(Path("/j"), "u"), 5000)
        with patch.object(am, "latest_jsonl_mtime", return_value=None), \
             patch.object(am, "_jsonl_entry_epoch") as entry:
            self.assertIsNone(am.latest_jsonl_activity(Path("/j"), "u"))
        entry.assert_not_called()

    def test_idle_rewrite_after_a_poke_that_did_not_land_counts_ineffective(self):
        """The defect: pre-poke the newest turn is at 1000. The poke never lands, but
        Claude Code rewrites a metadata line, so the mtime moves 1000 -> 2000."""
        with patch.object(am, "latest_jsonl_mtime", return_value=1000), \
             patch.object(am, "_jsonl_entry_epoch", return_value=1000.0):
            pre = am.latest_jsonl_activity(Path("/j"), "u")
        prev = {"predicate": "p1a", "pre_poke_jsonl_mtime": pre, "consecutive_ineffective": 0}
        with patch.object(am, "latest_jsonl_mtime", return_value=2000), \
             patch.object(am, "_jsonl_entry_epoch", return_value=1000.0):
            cur = am.latest_jsonl_activity(Path("/j"), "u")
        suppress, consecutive, _ = am.poke_effectiveness_decision(prev, cur, "p1a", 1)
        self.assertEqual((suppress, consecutive), (True, 1), "the poke did not land")
        with patch.object(am, "latest_jsonl_mtime", return_value=2000), \
             patch.object(am, "_jsonl_entry_epoch", return_value=1990.0):
            cur = am.latest_jsonl_activity(Path("/j"), "u")
        self.assertEqual(am.poke_effectiveness_decision(prev, cur, "p1a", 1)[:2], (False, 0),
                         "a real turn after the poke still reads as landed")


class EntryLivenessShipDarkT5400(unittest.TestCase):
    """T-5400: the unarmed-productive liveness default reads entries only when armed."""

    def test_flag_parses_only_1_as_armed(self):
        import importlib
        for val, expected in (("1", True), ("0", False), ("", False), ("true", False)):
            with patch.dict(os.environ, {"PANTHEON_ENTRY_LIVENESS_ENABLED": val}):
                reloaded = importlib.reload(am)
                try:
                    self.assertEqual(reloaded.ENTRY_LIVENESS_ENABLED, expected, f"env {val!r}")
                finally:
                    _reload_am()

    def test_a_reload_keeps_the_suite_hermetic(self):
        _reload_am()
        self.assertIsNone(am._jsonl_entry_epoch(Path("/nonexistent"), "nobody"))
        self.assertIsInstance(am._jsonl_entry_epoch, MagicMock,
                              "the module-wide stub survives a reload")

    def test_default_off_keeps_the_raw_mtime(self):
        with patch.object(am, "ENTRY_LIVENESS_ENABLED", False), \
             patch.object(am, "PRODUCTIVE_LIVENESS_ENABLED", False), \
             patch.object(am, "latest_jsonl_mtime", return_value=1234), \
             patch.object(am, "latest_jsonl_activity") as act:
            self.assertEqual(am.liveness_jsonl_mtime(Path("/d"), "u", "a"), 1234)
        act.assert_not_called()

    def test_armed_reads_the_newest_entry(self):
        with patch.object(am, "ENTRY_LIVENESS_ENABLED", True), \
             patch.object(am, "PRODUCTIVE_LIVENESS_ENABLED", False), \
             patch.object(am, "latest_jsonl_mtime", return_value=5000), \
             patch.object(am, "_jsonl_entry_epoch", return_value=1000.0):
            self.assertEqual(am.liveness_jsonl_mtime(Path("/d"), "u", "a"), 1000,
                             "an idle rewrite at 5000 must not read as activity")

    def test_productive_arming_still_wins(self):
        with patch.object(am, "ENTRY_LIVENESS_ENABLED", True), \
             patch.object(am, "PRODUCTIVE_LIVENESS_ENABLED", True), \
             patch.object(am, "latest_productive_jsonl_mtime", return_value=77), \
             patch.object(am, "latest_jsonl_activity") as act:
            self.assertEqual(am.liveness_jsonl_mtime(Path("/d"), "u", "a"), 77)
        act.assert_not_called()



class TestActionableRolePooledT5515(unittest.TestCase):
    """T-5515: since the ORG-088 role pooling (10-19) tickets target role:<R> and name
    their holder in delivered_to. Matching on target alone left every seat's count at 0,
    so p1b (stuck with work) could never fire."""

    T2918_FRONTMATTER = (  # captured from tickets/active/T-2918.md, 2026-10-26
        "---\n"
        "id: T-2918\n"
        "created: 2026-08-12T00:00:00Z\n"
        "requester: orchestrator\n"
        "target: role:engineering_orchestrator\n"
        "retargeted_at: 2026-10-19T18:36:17Z\n"
        "state: active\n"
        "depends_on: []\n"
        "delivered_to: mason\n"
        "---\n"
        "# GE root helper disposable-host acceptance\n"
    )

    def test_mocked_line_with_delivered_to_counts(self) -> None:
        stdout = (
            "/r/tickets/active/T-2918.md|role:engineering_orchestrator|[]||||mason\n"
            "/r/tickets/active/T-5505.md|role:engineering_orchestrator|[]||||artisan\n"
        )
        with patch("tools.activity_monitor.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
            n = count_actionable_tickets(Path("/r"), frozenset({"mason"}), "suborch1", queue_states=("active",))
        self.assertEqual(n, 1)

    def test_real_awk_over_captured_frontmatter(self) -> None:
        """Runs the REAL awk pipeline (sudo stripped) over a real-shaped ticket file."""
        with tempfile.TemporaryDirectory() as d:
            active = Path(d) / "tickets" / "active"
            active.mkdir(parents=True)
            (Path(d) / "tickets" / "inbox").mkdir()
            (active / "T-2918.md").write_text(self.T2918_FRONTMATTER)
            with patch("tools.activity_monitor._sudo", return_value=[]):
                mason = _scan_actionable_tickets(Path(d), frozenset({"mason"}), "suborch1", ("active",))
                artisan = _scan_actionable_tickets(Path(d), frozenset({"artisan"}), "suborch2", ("active",))
        self.assertEqual((mason, artisan), (1, 0))

if __name__ == "__main__":
    unittest.main()

