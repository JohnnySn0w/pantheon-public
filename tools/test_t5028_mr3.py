"""T-5028 MR-3a: (a-harness) `harness restart --runtime` onto a held runtime is
refused unless --override-hold; (f) the quarantine tool and status surface;
(f+) an override with no outcome after the grace is flagged; fleet_health rows.

tmp_path stand-ins only: the ledger is a tmp dir (ledger_dir=) or the landing is
patched, tmux kill / handoff are patched, alerts are mocked, fleet_health uses a
fake `run`. No git, no real state dirs.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

try:
    from tools import _seat_state_isolation
except ImportError:  # pragma: no cover
    import _seat_state_isolation  # type: ignore[no-redef]
from tools import fleet_health
from tools import harness
from tools import harness_state as hs
from tools import runtime_controller as rc
from tools import test_t5021_runtime_hold_both_planes as t5021


def setUpModule() -> None:
    _seat_state_isolation.start()


def tearDownModule() -> None:
    _seat_state_isolation.stop()


def _entries(ledger_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(ledger_dir.rglob("*.jsonl")):
        rows += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def _evidence(root: Path, persona: str = "orchestrator") -> list[dict]:
    path = root / "personas" / persona / "runtime-controller" / "controller-evidence.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "state"
        self.personas = self.root / "personas"
        self.ledger = base / "ledger"
        self.personas.mkdir(parents=True)
        self.ledger.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()


class RestartOverrideHoldTests(_Base):
    def _seed_active(self) -> None:
        path = hs.active_runtime_path("orchestrator", root=self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": hs.SCHEMA_ACTIVE_RUNTIME, "runtime": "claude/anthropic-code",
                                    "tmux_session": "s1"}), encoding="utf-8")

    def _run(self, extra, *, confirmed=True, live=True, order=None):
        from tools import handoff_first

        order = order if order is not None else []
        outcome = handoff_first.HandoffOutcome(confirmed, "ok" if confirmed else "no ack", Path("/x/HANDOFF.md"))

        def ask(persona):
            order.append("handoff")
            return outcome

        real_write = hs.write_holds

        def write_holds(*a, **k):
            order.append("clear-hold")
            return real_write(*a, **k)

        def kill(session, *, dry_run):
            order.append("kill" if not dry_run else "kill-dry")
            return True

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("tools.harness._tmux_session_live", return_value=live), \
                mock.patch("tools.harness._request_handoff_first", side_effect=ask), \
                mock.patch("tools.harness._kill_tmux_session", side_effect=kill) as killed, \
                mock.patch.object(hs, "write_holds", side_effect=write_holds), \
                mock.patch.object(rc, "_route_failure_alert") as routed:
            code = harness.main(
                ["orchestrator", "restart", "--reason", "drill", *extra], root=self.root, ledger_dir=self.ledger
            )
        return code, out.getvalue(), err.getvalue(), killed, routed, order

    def test_restart_onto_a_held_runtime_is_refused_before_handoff_and_kill(self) -> None:
        self._seed_active()
        t5021._hold(self.personas, "orchestrator", "codex/fallback")
        code, _out, err, killed, routed, order = self._run(["--runtime", "codex/fallback"])
        self.assertEqual(code, 2)
        self.assertIn("operator-held", err)
        self.assertIn("--override-hold", err)
        self.assertEqual(order, [])
        killed.assert_not_called()
        routed.assert_not_called()
        self.assertEqual(_entries(self.ledger), [])
        self.assertIn("codex/fallback", rc.load_holds("orchestrator", state_root=self.personas))

    def test_malformed_holds_refuse_the_restart(self) -> None:
        self._seed_active()
        path = rc._holds_path("orchestrator", state_root=self.personas)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")
        with mock.patch.object(rc, "_route_failure_alert"):
            code, _out, _err, killed, _routed, _order = self._run(["--runtime", "codex/fallback"])
        self.assertEqual(code, 2)
        killed.assert_not_called()

    def test_malformed_holds_with_override_refuses_without_killing(self) -> None:
        self._seed_active()
        path = rc._holds_path("orchestrator", state_root=self.personas)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")
        code, _out, err, killed, _routed, _order = self._run(["--runtime", "codex/fallback", "--override-hold"])
        self.assertEqual(code, 2)
        self.assertIn("malformed", err)
        killed.assert_not_called()

    def test_override_runs_handoff_then_clear_then_kill_and_lands_two_entries(self) -> None:
        self._seed_active()
        t5021._hold(self.personas, "orchestrator", "codex/fallback")
        code, out, _err, _killed, routed, order = self._run(["--runtime", "codex/fallback", "--override-hold"])
        entries = _entries(self.ledger)
        evidence = _evidence(self.root)
        self.assertEqual(code, 0)
        self.assertEqual(order, ["handoff", "clear-hold", "kill"])
        self.assertNotIn("codex/fallback", rc.load_holds("orchestrator", state_root=self.personas))
        self.assertEqual([e["id"].rsplit("-override-", 1)[1] for e in entries], ["clear-hold", "restart"])
        clear, restart = entries
        self.assertIn({"type": "ledger", "ref": restart["id"]}, clear["links"])
        self.assertIn({"type": "ledger", "ref": clear["id"]}, restart["links"])
        self.assertIn("hold on codex/fallback cleared (--override-hold)", out)
        events = [row["event"] for row in evidence]
        self.assertEqual(events.count(hs.OVERRIDE_EVENT), 1)
        self.assertEqual(events.count(hs.OVERRIDE_OUTCOME_EVENT), 1)
        outcome = next(row for row in evidence if row["event"] == hs.OVERRIDE_OUTCOME_EVENT)
        self.assertEqual(outcome["correlation_id"], restart["id"])
        self.assertTrue(outcome["hold_still_cleared"])
        self.assertEqual(outcome["ledger"], "landed")
        routed.assert_called_once()
        self.assertIn("--override-hold", routed.call_args.args[1])

    def test_refused_handoff_with_override_keeps_the_hold(self) -> None:
        self._seed_active()
        t5021._hold(self.personas, "orchestrator", "codex/fallback")
        code, _out, _err, killed, routed, order = self._run(
            ["--runtime", "codex/fallback", "--override-hold"], confirmed=False
        )
        self.assertEqual(code, 2)
        self.assertEqual(order, ["handoff"])
        killed.assert_not_called()
        routed.assert_not_called()
        self.assertIn("codex/fallback", rc.load_holds("orchestrator", state_root=self.personas))

    def test_override_on_an_unheld_runtime_is_a_noop_row_only(self) -> None:
        self._seed_active()
        code, _out, _err, _killed, routed, order = self._run(["--runtime", "codex/fallback", "--override-hold"])
        events = [row["event"] for row in _evidence(self.root)]
        self.assertEqual(code, 0)
        self.assertEqual(order, ["handoff", "kill"])
        routed.assert_not_called()
        self.assertEqual(events, ["operator_swap_override_hold_noop"])
        self.assertEqual([e["id"].rsplit("-override-", 1)[1] for e in _entries(self.ledger)], ["restart"])

    def test_dry_run_with_override_changes_nothing(self) -> None:
        self._seed_active()
        t5021._hold(self.personas, "orchestrator", "codex/fallback")
        code, out, _err, _killed, routed, order = self._run(
            ["--runtime", "codex/fallback", "--override-hold", "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertIn("would clear the hold on codex/fallback", out)
        self.assertNotIn("clear-hold", order)
        self.assertIn("codex/fallback", rc.load_holds("orchestrator", state_root=self.personas))
        self.assertEqual(_evidence(self.root), [])
        routed.assert_not_called()

    def test_malformed_last_attempt_with_clear_cooldown_refuses_before_anything_changes(self) -> None:
        """Review MINOR 1: validated before the hold gate, the handoff and the kill."""
        self._seed_active()
        t5021._hold(self.personas, "orchestrator", "codex/fallback")
        path = hs.last_attempt_path("orchestrator", "codex/fallback", root=self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")
        code, _out, err, killed, routed, order = self._run(
            ["--runtime", "codex/fallback", "--override-hold", "--clear-cooldown"]
        )
        self.assertEqual(code, 2)
        self.assertIn("malformed JSON", err)
        self.assertEqual(order, [])
        killed.assert_not_called()
        routed.assert_not_called()
        self.assertEqual(_entries(self.ledger), [])
        self.assertEqual(_evidence(self.root), [])
        self.assertIn("codex/fallback", rc.load_holds("orchestrator", state_root=self.personas))

    def test_a_failure_after_the_kill_still_lands_entries_and_the_outcome(self) -> None:
        """Review MINOR 1: a late failure is landed and loud, not lost."""
        self._seed_active()
        t5021._hold(self.personas, "orchestrator", "codex/fallback")
        with mock.patch.object(hs, "clear_cooldown", side_effect=OSError("disk full")):
            code, _out, err, _killed, routed, order = self._run(
                ["--runtime", "codex/fallback", "--override-hold", "--clear-cooldown"]
            )
        entries = _entries(self.ledger)
        outcome = next(row for row in _evidence(self.root) if row["event"] == hs.OVERRIDE_OUTCOME_EVENT)
        self.assertEqual(code, 2)
        self.assertEqual(order, ["handoff", "clear-hold", "kill"])
        self.assertEqual([e["id"].rsplit("-override-", 1)[1] for e in entries], ["clear-hold", "restart"])
        self.assertIn("disk full", entries[1]["summary"])
        self.assertFalse(outcome["ok"])
        self.assertIn("disk full", outcome["error"])
        self.assertIn("FAILED LATE", routed.call_args.args[1])
        self.assertIn("disk full", err)

    def test_override_hold_requires_runtime(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = harness.main(["orchestrator", "restart", "--override-hold", "--reason", "x"], root=self.root,
                                ledger_dir=self.ledger)
        self.assertEqual(code, 2)
        self.assertIn("--override-hold requires --runtime", err.getvalue())

    def test_unheld_restart_without_override_is_unchanged(self) -> None:
        self._seed_active()
        code, _out, _err, _killed, routed, order = self._run(["--runtime", "codex/fallback"])
        self.assertEqual(code, 0)
        self.assertEqual(order, ["handoff", "kill"])
        self.assertEqual(_evidence(self.root), [])
        routed.assert_not_called()


class QuarantineToolTests(_Base):
    def _quarantine(self, entry_id: str = "2026-10-15T01:00:00Z-harness-specialist-override-hold") -> dict:
        entry = {"id": entry_id, "ts": "2026-10-15T01:00:00Z", "agent": "research", "action": "harness_override",
                 "summary": "hold on specialist: claude/x", "links": [{"type": "persona", "ref": "specialist"},
                                                                 {"type": "runtime", "ref": "claude/x"}]}
        harness._append_quarantine(self.root, entry, "id already on origin/main with different content")
        return entry

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = harness.main(argv, root=self.root, ledger_dir=self.ledger)
        return code, out.getvalue(), err.getvalue()

    def test_list_shows_open_rows(self) -> None:
        entry = self._quarantine()
        code, out, _err = self._main(["specialist", "quarantine", "list"])
        self.assertEqual(code, 0)
        self.assertIn("1 open", out)
        self.assertIn(entry["id"], out)
        self.assertEqual(_entries(self.ledger), [])

    def test_land_lands_a_fresh_entry_carrying_the_original_then_tombstones(self) -> None:
        entry = self._quarantine()
        code, _out, _err = self._main(["specialist", "quarantine", "land", entry["id"], "--reason", "fix"])
        landed = _entries(self.ledger)
        self.assertEqual(code, 0)
        self.assertEqual(len(landed), 1)
        new = landed[0]
        self.assertNotEqual(new["id"], entry["id"])
        self.assertTrue(new["id"].endswith("-override-quarantine-land"))
        self.assertIn({"type": "ledger", "ref": entry["id"]}, new["links"])
        self.assertIn(json.dumps(entry, sort_keys=True), new["summary"])
        self.assertEqual(harness._open_quarantine(self.root), [])
        code, out, _err = self._main(["specialist", "quarantine", "list"])
        self.assertIn("none open", out)

    def test_land_that_fails_keeps_the_row_open(self) -> None:
        entry = self._quarantine()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(harness, "_land_with_spool", side_effect=RuntimeError("boom")), \
                mock.patch.object(harness, "_append_spool", side_effect=OSError("disk")):
            code = harness.main(["specialist", "quarantine", "land", entry["id"], "--reason", "fix"], root=self.root)
        self.assertEqual(code, 2)
        self.assertEqual(len(harness._open_quarantine(self.root)), 1)

    def test_land_that_spools_tombstones(self) -> None:
        entry = self._quarantine()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(harness, "_land_with_spool", return_value=(False, "offline")):
            code = harness.main(["specialist", "quarantine", "land", entry["id"], "--reason", "fix"], root=self.root)
        self.assertEqual(code, 2)
        self.assertEqual(harness._open_quarantine(self.root), [])

    def test_drop_tombstones_under_the_spool_lock_and_lands_a_drop_record(self) -> None:
        entry = self._quarantine()
        real_lock = harness._spool_lock
        with mock.patch.object(harness, "_spool_lock", side_effect=real_lock) as lock:
            code, _out, _err = self._main(["specialist", "quarantine", "drop", entry["id"], "--reason", "dup"])
        landed = _entries(self.ledger)
        rows = harness.quarantine_path(self.root).read_text(encoding="utf-8").splitlines()
        self.assertEqual(code, 0)
        lock.assert_called()
        self.assertTrue(landed[0]["id"].endswith("-override-quarantine-drop"))
        self.assertEqual(json.loads(rows[-1])["tombstone"], entry["id"])
        self.assertEqual(json.loads(rows[-1])["action"], "dropped")
        self.assertEqual(harness._open_quarantine(self.root), [])

    def test_unknown_or_tombstoned_id_exits_2(self) -> None:
        entry = self._quarantine()
        code, _out, err = self._main(["specialist", "quarantine", "drop", "nope", "--reason", "x"])
        self.assertEqual(code, 2)
        self.assertIn("no open quarantined entry", err)
        self._main(["specialist", "quarantine", "drop", entry["id"], "--reason", "x"])
        code, _out, err = self._main(["specialist", "quarantine", "land", entry["id"], "--reason", "x"])
        self.assertEqual(code, 2)

    def test_a_concurrent_land_of_the_same_id_is_refused(self) -> None:
        """Review MINOR 3: one land/drop per id at a time."""
        import fcntl
        import os

        entry = self._quarantine()
        held = harness._quarantine_id_lock(self.root, entry["id"])
        try:
            code, _out, err = self._main(["specialist", "quarantine", "land", entry["id"], "--reason", "x"])
        finally:
            os.close(held)
        self.assertEqual(code, 2)
        self.assertIn("is running", err)
        self.assertEqual(_entries(self.ledger), [])
        self.assertEqual(len(harness._open_quarantine(self.root)), 1)
        # Released afterwards: the next run proceeds.
        code, _out, _err = self._main(["specialist", "quarantine", "land", entry["id"], "--reason", "x"])
        self.assertEqual(code, 0)
        del fcntl

    def test_land_and_drop_need_an_id_and_a_reason(self) -> None:
        code, _out, err = self._main(["specialist", "quarantine", "land"])
        self.assertEqual(code, 2)
        self.assertIn("quarantine land requires <entry-id>", err)
        code, _out, err = self._main(["specialist", "quarantine", "drop", "x"])
        self.assertEqual(code, 2)
        self.assertIn("--reason is required for 'quarantine drop'", err)
        code, _out, err = self._main(["specialist", "quarantine", "list", "x"])
        self.assertEqual(code, 2)
        self.assertIn("quarantine list takes no <entry-id>", err)

    def test_status_counts_open_rows_only(self) -> None:
        first = self._quarantine("a-1")
        self._quarantine("a-2")
        self._main(["specialist", "quarantine", "drop", first["id"], "--reason", "dup"])
        args = harness.build_parser().parse_args(["specialist", "status"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            harness._do_status(args, root=self.root, pane_probe=lambda b: None)
        self.assertIn("quarantined ledger entries: 1 ", out.getvalue())


class OrphanOverrideTests(_Base):
    def _row(self, event: str, correlation: str, *, age_s: float) -> str:
        at = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat().replace("+00:00", "Z")
        return json.dumps({"event": event, "correlation_id": correlation, "destination": "codex/fallback",
                           "recorded_at": at}, sort_keys=True)

    def test_pure_detection(self) -> None:
        now = datetime.now(timezone.utc)
        lines = [
            self._row(hs.OVERRIDE_EVENT, "old-orphan", age_s=1200),
            self._row(hs.OVERRIDE_EVENT, "old-finished", age_s=1200),
            self._row(hs.OVERRIDE_OUTCOME_EVENT, "old-finished", age_s=1100),
            self._row(hs.OVERRIDE_EVENT, "young", age_s=60),
            "not json",
        ]
        orphans = hs.orphan_override_rows(lines, now=now)
        self.assertEqual([o["correlation_id"] for o in orphans], ["old-orphan"])

    def test_status_flags_an_override_without_outcome(self) -> None:
        log = self.personas / "orchestrator" / "runtime-controller" / "controller-evidence.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text(self._row(hs.OVERRIDE_EVENT, "c-1", age_s=1800) + "\n", encoding="utf-8")
        args = harness.build_parser().parse_args(["orchestrator", "status"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            harness._do_status(args, root=self.root, pane_probe=lambda b: None)
        self.assertIn("override without outcome: codex/fallback c-1", out.getvalue())

    def test_status_is_silent_when_every_override_has_an_outcome(self) -> None:
        log = self.personas / "orchestrator" / "runtime-controller" / "controller-evidence.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text(self._row(hs.OVERRIDE_EVENT, "c-1", age_s=1800) + "\n"
                       + self._row(hs.OVERRIDE_OUTCOME_EVENT, "c-1", age_s=1700) + "\n", encoding="utf-8")
        args = harness.build_parser().parse_args(["orchestrator", "status"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            harness._do_status(args, root=self.root, pane_probe=lambda b: None)
        self.assertNotIn("override without outcome", out.getvalue())


class FleetHealthHarnessOverridesTests(unittest.TestCase):
    SEP = "@@T5028@@"  # pinned literal: fleet_health._HARNESS_OVERRIDES_SPLIT

    def test_separator_constant_is_pinned(self) -> None:
        self.assertEqual(fleet_health._HARNESS_OVERRIDES_SPLIT, self.SEP)

    def _run_returning(self, out: str, ran: bool = True):
        calls = []

        def run(user, inner, **_kw):
            calls.append(inner)
            return 0, out, ran
        return run, calls

    def _orphan(self) -> str:
        at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        return json.dumps({"event": hs.OVERRIDE_EVENT, "correlation_id": "c-9", "destination": "claude/x",
                           "recorded_at": at}, sort_keys=True)

    def test_clean_renders_no_row(self) -> None:
        run, calls = self._run_returning(f"\n{self.SEP}\n\n{self.SEP}\n")
        self.assertIsNone(fleet_health._harness_overrides_check("specialist", "claude-research", run=run))
        self.assertIn("harness-overrides/quarantine.jsonl", calls[0])
        self.assertIn("specialist/runtime-controller/controller-evidence.jsonl", calls[0])

    def test_unreadable_renders_no_row(self) -> None:
        run, _calls = self._run_returning("", ran=False)
        self.assertIsNone(fleet_health._harness_overrides_check("specialist", "claude-research", run=run))

    def test_every_read_is_capped(self) -> None:
        """Review MINOR 2: spool and quarantine reads are bounded like the evidence read."""
        run, calls = self._run_returning(f"\n{self.SEP}\n\n{self.SEP}\n")
        fleet_health._harness_overrides_check("specialist", "claude-research", run=run)
        self.assertNotIn("cat ", calls[0])
        self.assertEqual(calls[0].count("tail -c 2000000"), 3)

    def test_counts_and_orphans_warn(self) -> None:
        q = [json.dumps({"entry": {"id": "a"}, "reason": "r"}), json.dumps({"entry": {"id": "b"}, "reason": "r"}),
             json.dumps({"tombstone": "b", "action": "dropped"})]
        out = "{\"id\": \"u1\"}\n" + self.SEP + "\n" + "\n".join(q) + "\n" + self.SEP + "\n" + self._orphan() + "\n"
        run, _calls = self._run_returning(out)
        row = fleet_health._harness_overrides_check("specialist", "claude-research", run=run)
        self.assertEqual(row["status"], fleet_health.WARN)
        self.assertIn("1 unlanded", row["detail"])
        self.assertIn("1 quarantined", row["detail"])
        self.assertIn("claude/x c-9", row["detail"])

    def test_check_agent_includes_the_row(self) -> None:
        out = "{\"id\": \"u1\"}\n" + self.SEP + "\n" + self.SEP + "\n"

        def run(user, inner, **_kw):
            if self.SEP in inner:
                return 0, out, True
            return 0, "active", True
        with mock.patch.object(fleet_health, "_policy_skip_check", return_value=None), \
                mock.patch.object(fleet_health, "_is_paused", return_value=False):
            result = fleet_health.check_agent("specialist", fleet_health.FLEET["specialist"], run=run)
        self.assertIn("harness-overrides", [c["name"] for c in result["checks"]])


class DocstringTests(unittest.TestCase):
    def test_cooldown_split_is_documented_next_to_the_code(self) -> None:
        self.assertIn("Two cooldown sources exist by design", rc.__doc__)
        self.assertIn("_controller_cooldown_skip", rc.__doc__)


if __name__ == "__main__":
    unittest.main()
