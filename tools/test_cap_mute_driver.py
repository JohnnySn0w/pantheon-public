"""Tests for cap_mute_driver.py (T-3844; cap-flag source re-pointed to the T-4037
mid-session-cap detector, T-4041).

Hermetic: a temp notify-mute registry + an injected cap reader, so every branch runs
without a live fleet or sudo. The load-bearing invariants under test:
  - a manual/operator mute is NEVER overwritten (capped) or cleared (uncapped);
  - an UNREADABLE / STALE cap flag never auto-mutes (fail-open toward alerting);
  - the stuck-detector backstop drops a too-old cap-mute so a seat can't be silenced
    forever;
  - OBSERVE mode (arm=False, the default) writes NOTHING — the T-4041 supervised-first-
    fire gate — reporting the `would-*` disposition instead;
  - the cap flag is True only for modal_present AND a FRESH observed_at.
"""
import subprocess
import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest import mock

try:
    from tools import cap_mute_driver as cmd
    from tools import notify_policy
except ImportError:  # bare run with cwd=tools/
    import cap_mute_driver as cmd  # type: ignore
    import notify_policy  # type: ignore


NOW = datetime(2026, 9, 27, 23, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestCapMuteDriver(unittest.TestCase):
    """The armed reconcile core (arm=True): drives the live registry."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.reg = str(Path(self._tmp.name) / "notify_mute.json")
        self.addCleanup(self._tmp.cleanup)

    @staticmethod
    def _cap(mapping):
        """read_cap stub: persona -> True|False|None."""
        return lambda persona, account: mapping.get(persona)

    def test_capped_unmuted_gets_cap_muted(self) -> None:
        d = cmd.drive_seat("mason", "suborch1", registry=self.reg, arm=True,
                           read_cap=self._cap({"mason": True}))
        self.assertEqual(d, "muted")
        entry = notify_policy.list_muted(self.reg).get("mason")
        self.assertEqual(entry["reason"], cmd.CAP_REASON)
        self.assertEqual(entry["by"], cmd.CAP_MUTE_BY)

    def test_capped_already_cap_muted_holds(self) -> None:
        notify_policy.mute("mason", by=cmd.CAP_MUTE_BY, reason=cmd.CAP_REASON, registry=self.reg)
        d = cmd.drive_seat("mason", "suborch1", registry=self.reg, arm=True,
                           read_cap=self._cap({"mason": True}))
        self.assertEqual(d, "hold:capped")

    def test_uncapped_cap_muted_gets_unmuted(self) -> None:
        notify_policy.mute("mason", by=cmd.CAP_MUTE_BY, reason=cmd.CAP_REASON, registry=self.reg)
        d = cmd.drive_seat("mason", "suborch1", registry=self.reg, arm=True,
                           read_cap=self._cap({"mason": False}))
        self.assertEqual(d, "unmuted:cap-lifted")
        self.assertFalse(notify_policy.is_muted("mason", self.reg))

    def test_manual_mute_never_overwritten_when_capped(self) -> None:
        notify_policy.mute("mason", by="operator", reason="deliberate-offline", registry=self.reg)
        d = cmd.drive_seat("mason", "suborch1", registry=self.reg, arm=True,
                           read_cap=self._cap({"mason": True}))
        self.assertEqual(d, "skip:manual-mute")
        self.assertEqual(notify_policy.list_muted(self.reg)["mason"]["reason"], "deliberate-offline")

    def test_manual_mute_never_cleared_when_uncapped(self) -> None:
        notify_policy.mute("mason", by="operator", reason="deliberate-offline", registry=self.reg)
        d = cmd.drive_seat("mason", "suborch1", registry=self.reg, arm=True,
                           read_cap=self._cap({"mason": False}))
        self.assertEqual(d, "noop:uncapped")
        self.assertTrue(notify_policy.is_muted("mason", self.reg))  # manual mute survives

    def test_unreadable_cap_leaves_state_and_never_mutes(self) -> None:
        d = cmd.drive_seat("mason", "suborch1", registry=self.reg, arm=True,
                           read_cap=self._cap({"mason": None}))
        self.assertEqual(d, "skip:unreadable")
        self.assertFalse(notify_policy.is_muted("mason", self.reg))

    def test_uncapped_unmuted_is_noop(self) -> None:
        d = cmd.drive_seat("mason", "suborch1", registry=self.reg, arm=True,
                           read_cap=self._cap({"mason": False}))
        self.assertEqual(d, "noop:uncapped")

    def test_backstop_unmutes_stuck_breaker(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=cmd.MAX_CAP_MUTE_HOURS + 1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        notify_policy.mute("mason", by=cmd.CAP_MUTE_BY, reason=cmd.CAP_REASON,
                           registry=self.reg, now_iso=old)
        d = cmd.drive_seat("mason", "suborch1", registry=self.reg, arm=True,
                           read_cap=self._cap({"mason": True}))
        self.assertEqual(d, "backstop-unmute:stuck-breaker")
        self.assertFalse(notify_policy.is_muted("mason", self.reg))

    def test_backstop_does_not_fire_on_fresh_cap_mute(self) -> None:
        # A recent cap-mute + still-capped must HOLD, not backstop-unmute.
        notify_policy.mute("mason", by=cmd.CAP_MUTE_BY, reason=cmd.CAP_REASON, registry=self.reg)
        d = cmd.drive_seat("mason", "suborch1", registry=self.reg, arm=True,
                           read_cap=self._cap({"mason": True}))
        self.assertEqual(d, "hold:capped")
        self.assertTrue(notify_policy.is_muted("mason", self.reg))

    def test_run_returns_per_persona_dispositions(self) -> None:
        res = cmd.run(("mason", "artisan"), registry=self.reg, arm=True,
                      read_cap=self._cap({"mason": True, "artisan": False}),
                      log=lambda *a, **k: None)
        self.assertEqual(res["mason"], "muted")
        self.assertEqual(res["artisan"], "noop:uncapped")


class TestObserveMode(unittest.TestCase):
    """arm=False (the DEFAULT): the T-4041 supervised-first-fire gate — reports the
    would-* disposition and writes NOTHING to the live registry."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.reg = str(Path(self._tmp.name) / "notify_mute.json")
        self.addCleanup(self._tmp.cleanup)

    @staticmethod
    def _cap(mapping):
        return lambda persona, account: mapping.get(persona)

    def test_default_is_observe_would_mute_writes_nothing(self) -> None:
        d = cmd.drive_seat("mason", "suborch1", registry=self.reg,  # no arm= -> default False
                           read_cap=self._cap({"mason": True}))
        self.assertEqual(d, "would-mute")
        self.assertFalse(notify_policy.is_muted("mason", self.reg))  # NOTHING written

    def test_observe_would_unmute_leaves_mute_in_place(self) -> None:
        notify_policy.mute("mason", by=cmd.CAP_MUTE_BY, reason=cmd.CAP_REASON, registry=self.reg)
        d = cmd.drive_seat("mason", "suborch1", registry=self.reg, arm=False,
                           read_cap=self._cap({"mason": False}))
        self.assertEqual(d, "would-unmute:cap-lifted")
        self.assertTrue(notify_policy.is_muted("mason", self.reg))  # still muted (no write)

    def test_observe_would_backstop_leaves_mute_in_place(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=cmd.MAX_CAP_MUTE_HOURS + 1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        notify_policy.mute("mason", by=cmd.CAP_MUTE_BY, reason=cmd.CAP_REASON,
                           registry=self.reg, now_iso=old)
        d = cmd.drive_seat("mason", "suborch1", registry=self.reg, arm=False,
                           read_cap=self._cap({"mason": True}))
        self.assertEqual(d, "would-backstop-unmute:stuck-breaker")
        self.assertTrue(notify_policy.is_muted("mason", self.reg))  # still muted (no write)

    def test_observe_readonly_dispositions_unchanged(self) -> None:
        # skip/hold/noop are identical in both modes (no write in either).
        self.assertEqual(
            cmd.drive_seat("mason", "suborch1", registry=self.reg, arm=False,
                           read_cap=self._cap({"mason": None})), "skip:unreadable")
        self.assertEqual(
            cmd.drive_seat("mason", "suborch1", registry=self.reg, arm=False,
                           read_cap=self._cap({"mason": False})), "noop:uncapped")


class TestObserveNotice(unittest.TestCase):
    """T-4057: in OBSERVE mode, mail recovery once when a seat first reaches would-mute and
    once when that cap episode ends, so the supervised first fire is not missed."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.reg = str(Path(self._tmp.name) / "notify_mute.json")
        self.state = Path(self._tmp.name) / "notice.json"
        self.addCleanup(self._tmp.cleanup)
        self.sent: list[list[str]] = []

    def _run(self, capped, arm=False, notify=None):
        with mock.patch.object(cmd, "instance_binding", lambda p: {"account": "suborch1"}):
            return cmd.run(("mason",), registry=self.reg, arm=arm,
                           read_cap=lambda persona, account: capped,
                           log=lambda *a, **k: None,
                           notify=notify or self.sent.append, notice_state=self.state)

    def test_first_would_mute_notifies_once(self) -> None:
        self._run(True)
        self._run(True)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("mason: would-mute", self.sent[0][0])

    def test_episode_end_notifies_and_rearms(self) -> None:
        self._run(True)
        self._run(False)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("episode ended", self.sent[1][0])
        self._run(True)  # a new cap episode notifies again
        self.assertEqual(len(self.sent), 3)

    def test_uncapped_without_episode_is_silent(self) -> None:
        self._run(False)
        self._run(None)  # unreadable leaves state alone
        self.assertEqual(self.sent, [])

    def test_armed_mode_never_notifies(self) -> None:
        self._run(True, arm=True)
        self.assertEqual(self.sent, [])

    def test_notify_failure_does_not_break_run_and_retries(self) -> None:
        def boom(lines):
            raise OSError("hub down")
        res = self._run(True, notify=boom)
        self.assertEqual(res["mason"], "would-mute")
        self._run(True)  # state was not recorded, so the notice is retried
        self.assertEqual(len(self.sent), 1)


class TestCapFlagFromState(unittest.TestCase):
    """The PURE predicate: modal_present AND fresh observed_at => capped."""

    def test_modal_present_fresh_is_capped(self) -> None:
        state = {"modal_present": True, "observed_at": _iso(NOW - timedelta(seconds=30))}
        self.assertIs(cmd.cap_flag_from_state(state, NOW), True)

    def test_modal_absent_is_not_capped(self) -> None:
        self.assertIs(cmd.cap_flag_from_state({"modal_present": False}, NOW), False)
        self.assertIs(cmd.cap_flag_from_state({}, NOW), False)  # cleared

    def test_modal_present_but_stale_is_unknown(self) -> None:
        state = {"modal_present": True, "observed_at": _iso(NOW - timedelta(seconds=700))}
        self.assertIsNone(cmd.cap_flag_from_state(state, NOW))

    def test_modal_present_but_no_observed_at_is_unknown(self) -> None:
        self.assertIsNone(cmd.cap_flag_from_state({"modal_present": True}, NOW))
        self.assertIsNone(cmd.cap_flag_from_state({"modal_present": True, "observed_at": "junk"}, NOW))

    def test_candidate_modal_kind_is_never_a_trigger(self) -> None:
        # A candidate (unconfirmed) modal with no modal_present must NOT read as capped.
        state = {"modal_present": False, "candidate_modal_kind": "weekly-limit",
                 "observed_at": _iso(NOW)}
        self.assertIs(cmd.cap_flag_from_state(state, NOW), False)

    def test_non_dict_is_unknown(self) -> None:
        self.assertIsNone(cmd.cap_flag_from_state(["not", "a", "dict"], NOW))


class TestReadMidSessionCap(unittest.TestCase):
    """The live cross-account read seam. SELF-account must read DIRECTLY (Recovery's sudoers
    rejects self-sudo, so a missing self file must map to False/not-capped, never
    None/unknown — otherwise the recovery seat is permanently unreadable + never cap-muted).
    Other accounts read via sudo, disambiguating missing (not-capped) from a genuine
    fault (unknown -> fail-open)."""

    @staticmethod
    def _proc(returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)

    @staticmethod
    def _state_json(modal_present, observed_at):
        import json
        return json.dumps({"schema": cmd.MID_SESSION_CAP_SCHEMA,
                           "modal_present": modal_present, "observed_at": observed_at})

    # --- self-account: direct read, NO sudo ---
    def test_self_account_missing_file_is_not_capped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "does-not-exist.json")
            with mock.patch.object(cmd.getpass, "getuser", return_value="me"), \
                 mock.patch.object(cmd.subprocess, "run",
                                   side_effect=AssertionError("must NOT sudo for self")), \
                 mock.patch.object(cmd, "_mid_session_cap_path", return_value=path):
                self.assertIs(cmd.read_mid_session_cap("recovery", "me", now=NOW), False)

    def test_self_account_modal_present_fresh_is_capped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "mid-session-cap.json"
            path.write_text(self._state_json(True, _iso(NOW - timedelta(seconds=30))))
            with mock.patch.object(cmd.getpass, "getuser", return_value="me"), \
                 mock.patch.object(cmd, "_mid_session_cap_path", return_value=str(path)):
                self.assertIs(cmd.read_mid_session_cap("recovery", "me", now=NOW), True)

    def test_self_account_modal_absent_is_not_capped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "mid-session-cap.json"
            path.write_text(self._state_json(False, _iso(NOW)))
            with mock.patch.object(cmd.getpass, "getuser", return_value="me"), \
                 mock.patch.object(cmd, "_mid_session_cap_path", return_value=str(path)):
                self.assertIs(cmd.read_mid_session_cap("recovery", "me", now=NOW), False)

    def test_self_account_modal_present_stale_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "mid-session-cap.json"
            path.write_text(self._state_json(True, _iso(NOW - timedelta(seconds=700))))
            with mock.patch.object(cmd.getpass, "getuser", return_value="me"), \
                 mock.patch.object(cmd, "_mid_session_cap_path", return_value=str(path)):
                self.assertIsNone(cmd.read_mid_session_cap("recovery", "me", now=NOW))

    def test_self_account_read_fault_is_unknown(self) -> None:
        with mock.patch.object(cmd.getpass, "getuser", return_value="me"), \
             mock.patch.object(cmd, "_mid_session_cap_path", return_value="/x"), \
             mock.patch.object(Path, "read_text", side_effect=PermissionError("eacces")):
            self.assertIsNone(cmd.read_mid_session_cap("recovery", "me", now=NOW))

    def test_empty_file_is_not_capped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "mid-session-cap.json"
            path.write_text("")
            with mock.patch.object(cmd.getpass, "getuser", return_value="me"), \
                 mock.patch.object(cmd, "_mid_session_cap_path", return_value=str(path)):
                self.assertIs(cmd.read_mid_session_cap("recovery", "me", now=NOW), False)

    # --- other account: via sudo ---
    def test_other_account_missing_is_not_capped(self) -> None:
        with mock.patch.object(cmd.getpass, "getuser", return_value="recovery-acct"), \
             mock.patch.object(cmd.subprocess, "run",
                               return_value=self._proc(1, "", "cat: ...: No such file or directory")):
            self.assertIs(cmd.read_mid_session_cap("artisan", "suborch2", now=NOW), False)

    def test_other_account_fault_is_unknown(self) -> None:
        with mock.patch.object(cmd.getpass, "getuser", return_value="recovery-acct"), \
             mock.patch.object(cmd.subprocess, "run",
                               return_value=self._proc(1, "", "Permission denied")):
            self.assertIsNone(cmd.read_mid_session_cap("artisan", "suborch2", now=NOW))

    def test_other_account_capped(self) -> None:
        with mock.patch.object(cmd.getpass, "getuser", return_value="recovery-acct"), \
             mock.patch.object(cmd.subprocess, "run",
                               return_value=self._proc(0, self._state_json(True, _iso(NOW)))):
            self.assertIs(cmd.read_mid_session_cap("artisan", "suborch2", now=NOW), True)

    def test_other_account_timeout_is_unknown(self) -> None:
        with mock.patch.object(cmd.getpass, "getuser", return_value="recovery-acct"), \
             mock.patch.object(cmd.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("cat", 15)):
            self.assertIsNone(cmd.read_mid_session_cap("artisan", "suborch2", now=NOW))

    def test_malformed_json_is_unknown(self) -> None:
        with mock.patch.object(cmd.getpass, "getuser", return_value="recovery-acct"), \
             mock.patch.object(cmd.subprocess, "run",
                               return_value=self._proc(0, "{not json")):
            self.assertIsNone(cmd.read_mid_session_cap("artisan", "suborch2", now=NOW))


class RegistryUnreadableTests(unittest.TestCase):
    """T-5396: a torn registry must not be rewritten by the driver, and one seat's
    refusal must not stop the others being driven."""

    def test_torn_registry_is_not_rewritten_and_every_seat_is_reported(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            reg = Path(d) / "mute.json"
            reg.write_text('{"schema": "x", "muted": {"specialist": {"by": "operator"}}')   # torn
            torn = reg.read_text()
            logs = []
            with mock.patch.object(cmd, "instance_binding", return_value={"account": "acct"}):
                res = cmd.run(("orchestrator", "recovery"), registry=reg, arm=True,
                                      read_cap=lambda p, a: True, log=logs.append)
            self.assertEqual(reg.read_text(), torn, "the driver rewrote a registry it could not read")
            self.assertEqual(res, {"orchestrator": "error:registry-unreadable",
                                   "recovery": "error:registry-unreadable"})


if __name__ == "__main__":
    unittest.main()
