"""Hermetic tests for fleet_snapshot_liveness_check.py (T-3791).

NO real systemctl / git / mailbox: every timer-state and commit read goes through
an injected fake runner (the fleet_clone_currency_check test pattern), so the suite
is deterministic and safe on any box. Paired positive/negative controls throughout:
a HEALTHY timer does NOT alert; a DISABLED timer and a STALE-commit timer DO; the
skip-list suppresses a dispositioned dead timer; an unreadable timer/commit
classifies UNKNOWN and alerts (fail-closed). A trivially-broken always-HEALTHY
impl fails every negative control here (the red-check).
"""
from __future__ import annotations

import getpass
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.fleet_snapshot_liveness_check as fsl

DAY = 86400.0
NOW = 1_800_000_000.0  # fixed reference epoch for deterministic ages


def _target(account="suborch2", timer="pantheon-agent-state-snapshot.timer",
            repo="/home/suborch2/.local/state/pantheon/agent-state-history",
            threshold=3.0, persona="artisan", oncalendar="daily", change_driven=True):
    return fsl.SnapshotTarget(persona=persona, account=account, timer=timer,
                              commit_repo=repo, oncalendar=oncalendar,
                              recency_threshold_days=threshold, change_driven=change_driven)


def make_state_run(active="active", enabled="enabled", unreachable=False):
    """Fake for systemctl is-active/is-enabled: returns the state token on stdout
    (even on non-zero rc, like real systemctl). `unreachable` simulates a sudo/bus
    failure (empty stdout) → the UNKNOWN path."""
    def _run(argv, **kwargs):
        joined = " ".join(argv)
        if unreachable:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="sudo: a password is required")
        if "is-active" in joined:
            return subprocess.CompletedProcess(argv, 0 if active == "active" else 3,
                                               stdout=active + "\n", stderr="")
        if "is-enabled" in joined:
            rc = 0 if enabled in ("enabled", "enabled-runtime") else 1
            return subprocess.CompletedProcess(argv, rc, stdout=enabled + "\n", stderr="")
        raise AssertionError(f"unexpected systemctl argv: {argv}")
    return _run


def make_commit_run(epoch=None, fail=False, garbage=False):
    """Fake for `git log -1 --format=%ct`."""
    def _run(argv, **kwargs):
        if fail:
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: not a git repository")
        if garbage:
            return subprocess.CompletedProcess(argv, 0, stdout="notanumber\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=f"{int(epoch)}\n", stderr="")
    return _run


def make_source_run(mtime=None, fail=False, garbage=False):
    """Fake for the T-3992 `git ls-files | xargs stat` newest-tracked-mtime probe.
    fail → non-zero rc (repo unreadable); garbage → non-numeric stdout; mtime None
    → empty stdout (no tracked files) — all three map to the UNKNOWN source path."""
    def _run(argv, **kwargs):
        if fail:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        if garbage:
            return subprocess.CompletedProcess(argv, 0, stdout="notanumber\n", stderr="")
        if mtime is None:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=f"{int(mtime)}\n", stderr="")
    return _run


class TargetDiscovery(unittest.TestCase):
    def test_persona_derived_from_account_role_map(self):
        with mock.patch.object(fsl.arm, "account_persona",
                                side_effect=lambda a: {"claude": "orchestrator", "exocortex": "archivist"}.get(a)):
            targets = fsl.discover_targets()
        by_key = {t.key: t for t in targets}
        self.assertEqual(by_key["claude:pantheon-agent-state-snapshot.timer"].persona, "orchestrator")
        self.assertEqual(by_key["exocortex:archivist-state-snapshot.timer"].persona, "archivist")

    def test_registry_includes_the_incident_and_all_agent_state_timers(self):
        keys = {t.key for t in fsl.discover_targets()}
        # The T-3790 incident timer (exocortex-data) retired with the exocortex app (2026-10-16).
        self.assertNotIn("exocortex:exocortex-data-snapshot.timer", keys)
        self.assertIn("exocortex:archivist-state-snapshot.timer", keys)
        # Every agent-state-bearing account.
        for acct in ("claude", "claude-research", "claude-fallback", "suborch1", "suborch2"):
            self.assertIn(f"{acct}:pantheon-agent-state-snapshot.timer", keys)

    def test_unknown_account_persona_falls_back_to_account_name(self):
        with mock.patch.object(fsl.arm, "account_persona", return_value=None):
            targets = fsl.discover_targets()
        self.assertEqual(targets[0].persona, targets[0].account)

    def test_threshold_override_replaces_every_target_threshold(self):
        targets = fsl.discover_targets(threshold_override=9.0)
        self.assertTrue(all(t.recency_threshold_days == 9.0 for t in targets))

    def test_change_driven_flags_match_repo_semantics(self):
        by_key = {t.key: t for t in fsl.discover_targets()}
        # The only change-driven repo (exocortex-data) retired with the exocortex app.
        self.assertFalse(any(t.change_driven for t in by_key.values()))
        # Idle-prone commit-on-change repos are STATE-only monitored.
        self.assertFalse(by_key["exocortex:archivist-state-snapshot.timer"].change_driven)
        for acct in ("claude", "claude-research", "claude-fallback", "suborch1", "suborch2"):
            self.assertFalse(by_key[f"{acct}:pantheon-agent-state-snapshot.timer"].change_driven)


class ChangeDrivenGating(unittest.TestCase):
    """The recency (STALE) leg is gated on change_driven. STATE is checked for
    EVERY target (idle-proof primary signal); recency only for change-driven repos."""

    def _check(self, *, change_driven, active="active", enabled="enabled",
               unreachable=False, commit_epoch=None, commit_fail=False,
               threshold=3.0, now=NOW, source_mtime=NOW, source_fail=False):
        # source_mtime defaults to NOW so an old-commit change-driven target reads
        # STALE for the RIGHT reason (source written since the stale snapshot) — the
        # T-3992 quiescent-suppress path is exercised by SnapshotSourceQuiescenceT3992.
        t = _target(threshold=threshold, change_driven=change_driven)
        return fsl.check_target(
            t, now=now,
            state_run=make_state_run(active=active, enabled=enabled, unreachable=unreachable),
            commit_run=make_commit_run(epoch=commit_epoch, fail=commit_fail),
            source_run=make_source_run(mtime=source_mtime, fail=source_fail),
        )

    # --- idle-prone (change_driven=False): commit-age NEVER alerts --------
    def test_idle_prone_ancient_commit_with_active_timer_is_HEALTHY(self):
        # The live artisan / archivist-state case: 50-day-old commit, timer
        # active+enabled → must be HEALTHY (no STALE on an idle-prone repo).
        r = self._check(change_driven=False, commit_epoch=NOW - 50 * DAY)
        self.assertEqual(r["verdict"], fsl.HEALTHY)
        self.assertIn("informational", r["detail"])

    def test_idle_prone_disabled_timer_STILL_alerts(self):
        # Gating recency must NOT blunt the STATE signal: a dead idle-prone timer
        # still flags (this is the whole point — a skip would have masked it).
        r = self._check(change_driven=False, enabled="disabled", commit_epoch=NOW - 50 * DAY)
        self.assertEqual(r["verdict"], fsl.DISABLED_OR_INACTIVE)

    def test_idle_prone_inactive_timer_STILL_alerts(self):
        r = self._check(change_driven=False, active="inactive", commit_epoch=NOW)
        self.assertEqual(r["verdict"], fsl.DISABLED_OR_INACTIVE)

    def test_idle_prone_unreadable_commit_is_HEALTHY_not_unknown(self):
        # commit is informational-only for a STATE-only target, so its
        # unreadability does not degrade the verdict (STATE passed).
        r = self._check(change_driven=False, commit_fail=True)
        self.assertEqual(r["verdict"], fsl.HEALTHY)

    def test_idle_prone_unreadable_STATE_is_UNKNOWN(self):
        # Fail-closed STILL applies to the STATE leg for every target.
        r = self._check(change_driven=False, unreachable=True, commit_epoch=NOW - 1 * DAY)
        self.assertEqual(r["verdict"], fsl.UNKNOWN)

    # --- change-driven (change_driven=True): recency IS an alert signal ---
    def test_change_driven_old_commit_is_STALE(self):
        r = self._check(change_driven=True, commit_epoch=NOW - 50 * DAY)
        self.assertEqual(r["verdict"], fsl.STALE)

    def test_change_driven_fresh_commit_is_HEALTHY(self):
        r = self._check(change_driven=True, commit_epoch=NOW - 1 * DAY)
        self.assertEqual(r["verdict"], fsl.HEALTHY)

    def test_change_driven_unreadable_commit_is_UNKNOWN(self):
        r = self._check(change_driven=True, commit_fail=True)
        self.assertEqual(r["verdict"], fsl.UNKNOWN)


class CheckTargetClassification(unittest.TestCase):
    def _check(self, *, active="active", enabled="enabled", unreachable=False,
               commit_epoch=None, commit_fail=False, commit_garbage=False,
               threshold=3.0, now=NOW, change_driven=True,
               source_mtime=NOW, source_fail=False):
        # Defaults to change_driven=True: this class exercises the commit-recency
        # (STALE) leg, which only runs for change-driven targets. The
        # change_driven=False (STATE-only) gating lives in ChangeDrivenGating.
        # source_mtime defaults to NOW (source freshly written) so an old commit
        # reads STALE for the real-break reason; the quiescent-suppress path lives
        # in SnapshotSourceQuiescenceT3992.
        t = _target(threshold=threshold, change_driven=change_driven)
        return fsl.check_target(
            t, now=now,
            state_run=make_state_run(active=active, enabled=enabled, unreachable=unreachable),
            commit_run=make_commit_run(epoch=commit_epoch, fail=commit_fail, garbage=commit_garbage),
            source_run=make_source_run(mtime=source_mtime, fail=source_fail),
        )

    # --- positive control -------------------------------------------------
    def test_active_enabled_recent_commit_is_HEALTHY(self):
        r = self._check(commit_epoch=NOW - 1 * DAY)  # 1 day old, threshold 3d
        self.assertEqual(r["verdict"], fsl.HEALTHY)

    # --- negative controls (these break an always-HEALTHY impl) -----------
    def test_disabled_timer_is_DISABLED_OR_INACTIVE(self):
        r = self._check(enabled="disabled", commit_epoch=NOW - 1 * DAY)
        self.assertEqual(r["verdict"], fsl.DISABLED_OR_INACTIVE)

    def test_inactive_timer_is_DISABLED_OR_INACTIVE(self):
        r = self._check(active="inactive", commit_epoch=NOW - 1 * DAY)
        self.assertEqual(r["verdict"], fsl.DISABLED_OR_INACTIVE)

    def test_dead_timer_is_flagged_even_with_a_fresh_commit(self):
        # The T-3790 shape: a disabled timer must flag REGARDLESS of commit age —
        # state is checked before recency, so a fresh commit cannot mask a dead timer.
        r = self._check(active="inactive", enabled="disabled", commit_epoch=NOW)
        self.assertEqual(r["verdict"], fsl.DISABLED_OR_INACTIVE)

    def test_old_commit_with_healthy_timer_is_STALE(self):
        r = self._check(commit_epoch=NOW - 10 * DAY)  # 10 days old, threshold 3d
        self.assertEqual(r["verdict"], fsl.STALE)

    def test_unreadable_timer_state_is_UNKNOWN(self):
        r = self._check(unreachable=True, commit_epoch=NOW - 1 * DAY)
        self.assertEqual(r["verdict"], fsl.UNKNOWN)

    def test_unreadable_commit_is_UNKNOWN(self):
        r = self._check(commit_fail=True)
        self.assertEqual(r["verdict"], fsl.UNKNOWN)

    def test_nonnumeric_commit_output_is_UNKNOWN(self):
        r = self._check(commit_garbage=True)
        self.assertEqual(r["verdict"], fsl.UNKNOWN)

    # --- boundary ---------------------------------------------------------
    def test_commit_exactly_at_threshold_is_HEALTHY(self):
        r = self._check(commit_epoch=NOW - 3 * DAY, threshold=3.0)  # exactly 3d, not > 3d
        self.assertEqual(r["verdict"], fsl.HEALTHY)

    def test_commit_just_over_threshold_is_STALE(self):
        r = self._check(commit_epoch=NOW - 3 * DAY - 60, threshold=3.0)  # 3d + 1min
        self.assertEqual(r["verdict"], fsl.STALE)

    def test_enabled_runtime_token_is_accepted(self):
        r = self._check(enabled="enabled-runtime", commit_epoch=NOW - 1 * DAY)
        self.assertEqual(r["verdict"], fsl.HEALTHY)


class SelfVsSudoAndNoPassword(unittest.TestCase):
    """Recovery reads its own seat directly; siblings via NOPASSWD `sudo -n -u`. The
    invariant the task pins: NO sudo password, NO root.env — ever."""

    def _capture(self, account, me):
        captured = []

        def state_run(argv, **kwargs):
            captured.append((argv, kwargs))
            joined = " ".join(argv)
            tok = "active" if "is-active" in joined else "enabled"
            return subprocess.CompletedProcess(argv, 0, stdout=tok + "\n", stderr="")

        def commit_run(argv, **kwargs):
            captured.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout=f"{int(NOW)}\n", stderr="")

        t = _target(account=account, repo=f"/home/{account}/repo")
        with mock.patch.object(fsl.getpass, "getuser", return_value=me):
            fsl.check_target(t, now=NOW, state_run=state_run, commit_run=commit_run)
        return captured

    def test_self_account_uses_no_sudo(self):
        captured = self._capture(account="claude-fallback", me="claude-fallback")
        for argv, _ in captured:
            self.assertNotIn("sudo", argv, f"self-account command must not sudo: {argv}")

    def test_sibling_uses_sudo_n_and_never_a_password(self):
        captured = self._capture(account="exocortex", me="claude-fallback")
        self.assertTrue(captured)
        for argv, kwargs in captured:
            self.assertEqual(argv[:4], ["sudo", "-n", "-u", "exocortex"],
                              f"sibling must use passwordless `sudo -n -u`: {argv}")
            self.assertNotIn("-S", argv)  # -S = read password from stdin (the T-2798 leg we must NOT use)
            joined = " ".join(str(a) for a in argv)
            self.assertNotIn("root.env", joined)
            # No password ever piped via input=.
            self.assertNotIn("input", kwargs)

    def test_read_only_verbs_only(self):
        """The only commands that ever reach a seat are read-only: is-active,
        is-enabled, git log. No mutating verb is ever assembled."""
        captured = self._capture(account="suborch1", me="claude-fallback")
        mutating = {"restart", "enable", "disable", "start", "stop", "commit", "add",
                    "push", "pull", "reset", "checkout"}
        for argv, _ in captured:
            joined = " ".join(str(a) for a in argv)
            for verb in mutating:
                self.assertNotIn(f" {verb}", f" {joined} ",
                                  f"mutating verb '{verb}' reached a seat: {argv}")


class NeedsAlert(unittest.TestCase):
    def _r(self, verdict):
        return {"persona": "artisan", "account": "suborch2", "timer": "t", "key": "suborch2:t",
                "verdict": verdict, "detail": ""}

    def test_healthy_not_flagged(self):
        self.assertEqual(fsl.needs_alert([self._r(fsl.HEALTHY)]), [])

    def test_disabled_flagged(self):
        r = self._r(fsl.DISABLED_OR_INACTIVE)
        self.assertEqual(fsl.needs_alert([r]), [r])

    def test_stale_flagged(self):
        r = self._r(fsl.STALE)
        self.assertEqual(fsl.needs_alert([r]), [r])

    def test_unknown_flagged(self):
        r = self._r(fsl.UNKNOWN)
        self.assertEqual(fsl.needs_alert([r]), [r])

    def test_skipped_not_flagged(self):
        self.assertEqual(fsl.needs_alert([self._r(fsl.SKIPPED)]), [])


class SkipListLoad(unittest.TestCase):
    """Fail-open at every failure mode — a broken skip-list must NEVER suppress."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_missing_file_returns_empty(self):
        self.assertEqual(fsl.load_skip_list(self.tmp / "nope.yaml"), [])

    def test_happy_path(self):
        p = self.tmp / "s.yaml"
        p.write_text("skips:\n  - account: exocortex\n    timer: archivist-state-snapshot.timer\n"
                     "    disposition_ticket: T-1\n    expected_state: active\n    reason: r\n")
        out = fsl.load_skip_list(p)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["account"], "exocortex")

    def test_parse_error_returns_empty(self):
        p = self.tmp / "b.yaml"
        p.write_text(": :\n  - not\n  even [\n")
        self.assertEqual(fsl.load_skip_list(p), [])

    def test_non_mapping_root_returns_empty(self):
        p = self.tmp / "l.yaml"
        p.write_text("- a\n- b\n")
        self.assertEqual(fsl.load_skip_list(p), [])

    def test_skips_not_a_list_returns_empty(self):
        p = self.tmp / "sc.yaml"
        p.write_text("skips: nope\n")
        self.assertEqual(fsl.load_skip_list(p), [])

    def test_empty_file_returns_empty(self):
        p = self.tmp / "e.yaml"
        p.write_text("")
        self.assertEqual(fsl.load_skip_list(p), [])

    def test_shipped_skip_list_parses(self):
        # The file shipped alongside the tool must parse to a list (empty is fine).
        out = fsl.load_skip_list(fsl.SKIP_LIST_PATH_DEFAULT)
        self.assertIsInstance(out, list)


class SkipListIsActive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        for s in ("inbox", "active", "done"):
            (self.tmp / "tickets" / s).mkdir(parents=True)

    def _touch(self, state, tid):
        (self.tmp / "tickets" / state / f"{tid}.md").write_text("stub\n")

    def _entry(self, **kw):
        base = {"account": "exocortex", "timer": "archivist-state-snapshot.timer",
                "disposition_ticket": "T-1", "expected_state": "active", "reason": "dormant"}
        base.update(kw)
        return base

    def test_active_when_ticket_in_expected_state(self):
        self._touch("active", "T-1")
        active, reason = fsl.is_skip_active(self._entry(), repo_root=self.tmp)
        self.assertTrue(active)
        self.assertIn("T-1", reason)

    def test_expired_when_ticket_moved(self):
        self._touch("done", "T-1")
        active, reason = fsl.is_skip_active(self._entry(), repo_root=self.tmp)
        self.assertFalse(active)
        self.assertIn("EXPIRED", reason)

    def test_expired_when_ticket_missing(self):
        active, reason = fsl.is_skip_active(self._entry(disposition_ticket="T-999"), repo_root=self.tmp)
        self.assertFalse(active)
        self.assertIn("not found", reason)

    def test_malformed_entry_missing_timer_ignored(self):
        # (account, timer) BOTH required — a skip missing the timer must be ignored
        # (a coarse account-only skip would over-suppress exocortex's two timers).
        active, reason = fsl.is_skip_active(
            {"account": "exocortex", "disposition_ticket": "T-1", "expected_state": "active"},
            repo_root=self.tmp)
        self.assertFalse(active)
        self.assertIn("malformed", reason)

    def test_invalid_expected_state_ignored(self):
        self._touch("active", "T-1")
        active, reason = fsl.is_skip_active(self._entry(expected_state="blocked"), repo_root=self.tmp)
        self.assertFalse(active)
        self.assertIn("expected_state", reason)


class SkipListApply(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        for s in ("inbox", "active", "done"):
            (self.tmp / "tickets" / s).mkdir(parents=True)

    def _touch(self, state, tid):
        (self.tmp / "tickets" / state / f"{tid}.md").write_text("stub\n")

    def _skip(self, account="exocortex", timer="archivist-state-snapshot.timer", tid="T-1", state="active"):
        return {"account": account, "timer": timer, "disposition_ticket": tid,
                "expected_state": state, "reason": "dormant"}

    def _result(self, verdict, account="exocortex", timer="archivist-state-snapshot.timer"):
        return {"persona": "archivist", "account": account, "timer": timer,
                "key": f"{account}:{timer}", "verdict": verdict, "detail": "d"}

    def test_dead_timer_with_active_skip_becomes_skipped(self):
        self._touch("active", "T-1")
        out = fsl.apply_skip_list([self._result(fsl.DISABLED_OR_INACTIVE)], [self._skip()], repo_root=self.tmp)
        self.assertEqual(out[0]["verdict"], fsl.SKIPPED)
        self.assertEqual(out[0]["original_verdict"], fsl.DISABLED_OR_INACTIVE)
        self.assertIn("T-1", out[0]["skip_reason"])

    def test_stale_with_active_skip_becomes_skipped(self):
        self._touch("active", "T-1")
        out = fsl.apply_skip_list([self._result(fsl.STALE)], [self._skip()], repo_root=self.tmp)
        self.assertEqual(out[0]["verdict"], fsl.SKIPPED)

    def test_healthy_never_suppressed(self):
        self._touch("active", "T-1")
        out = fsl.apply_skip_list([self._result(fsl.HEALTHY)], [self._skip()], repo_root=self.tmp)
        self.assertEqual(out[0]["verdict"], fsl.HEALTHY)

    def test_expired_skip_stays_flagged_with_note(self):
        self._touch("done", "T-1")  # expected active
        out = fsl.apply_skip_list([self._result(fsl.STALE)], [self._skip()], repo_root=self.tmp)
        self.assertEqual(out[0]["verdict"], fsl.STALE)
        self.assertIn("skip attempted but not applied", out[0]["detail"])

    def test_skip_keyed_by_account_AND_timer_not_account_alone(self):
        # A skip for exocortex's archivist-state timer must NOT suppress exocortex's
        # OTHER timer (exocortex-data) — the account-collision case account-only
        # keying would get wrong.
        self._touch("active", "T-1")
        results = [
            self._result(fsl.DISABLED_OR_INACTIVE, timer="archivist-state-snapshot.timer"),
            self._result(fsl.DISABLED_OR_INACTIVE, timer="exocortex-data-snapshot.timer"),
        ]
        out = fsl.apply_skip_list(results, [self._skip(timer="archivist-state-snapshot.timer")], repo_root=self.tmp)
        by_timer = {r["timer"]: r for r in out}
        self.assertEqual(by_timer["archivist-state-snapshot.timer"]["verdict"], fsl.SKIPPED)
        self.assertEqual(by_timer["exocortex-data-snapshot.timer"]["verdict"], fsl.DISABLED_OR_INACTIVE)

    def test_apply_never_mutates_input(self):
        self._touch("active", "T-1")
        results = [self._result(fsl.STALE)]
        original = [dict(r) for r in results]
        fsl.apply_skip_list(results, [self._skip()], repo_root=self.tmp)
        self.assertEqual(results, original)

    def test_skipped_never_in_needs_alert(self):
        self._touch("active", "T-1")
        results = [
            self._result(fsl.DISABLED_OR_INACTIVE, timer="archivist-state-snapshot.timer"),
            self._result(fsl.STALE, account="suborch2", timer="pantheon-agent-state-snapshot.timer"),
        ]
        out = fsl.apply_skip_list(results, [self._skip(timer="archivist-state-snapshot.timer")], repo_root=self.tmp)
        flagged = fsl.needs_alert(out)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["account"], "suborch2")


class RenderAndAlert(unittest.TestCase):
    def test_render_lists_flagged_and_skipped(self):
        flagged = [{"persona": "archivist", "account": "exocortex",
                    "timer": "exocortex-data-snapshot.timer",
                    "verdict": fsl.DISABLED_OR_INACTIVE, "detail": "dead"}]
        skipped = [{"persona": "artisan", "account": "suborch2",
                    "timer": "pantheon-agent-state-snapshot.timer",
                    "original_verdict": fsl.STALE, "skip_reason": "idle per T-2",
                    "detail": "..."}]
        out = fsl.render_alert(flagged, skipped=skipped)
        self.assertIn("exocortex-data-snapshot.timer", out)
        self.assertIn("DISABLED_OR_INACTIVE", out)
        self.assertIn("dispositioned-mitigation carve-outs", out)
        self.assertIn("T-2", out)

    def test_render_no_skipped_footer_when_empty(self):
        flagged = [{"persona": "archivist", "account": "exocortex", "timer": "t",
                    "verdict": fsl.STALE, "detail": "x"}]
        self.assertNotIn("carve-outs", fsl.render_alert(flagged, skipped=[]))

    def test_alert_sends_to_orchestrator_as_recovery(self):
        flagged = [{"persona": "archivist", "account": "exocortex",
                    "timer": "exocortex-data-snapshot.timer",
                    "verdict": fsl.DISABLED_OR_INACTIVE, "detail": "dead 49d"}]
        with mock.patch("tools.mailbox.send") as msend:
            fsl.alert(flagged)
        msend.assert_called_once()
        args, kwargs = msend.call_args
        self.assertEqual(args[0], f"{fsl.arm.mailbox_hub()}/orchestrator")
        self.assertIn("exocortex", args[1])
        self.assertEqual(args[2], "recovery")  # sender
        self.assertIn("1", kwargs.get("subject", ""))


class MainFlow(unittest.TestCase):
    def test_flagless_run_refuses(self):
        self.assertEqual(fsl.main([]), 64)

    def test_dry_run_never_alerts(self):
        flagged = [{"persona": "archivist", "account": "exocortex",
                    "timer": "exocortex-data-snapshot.timer",
                    "key": "exocortex:exocortex-data-snapshot.timer",
                    "verdict": fsl.DISABLED_OR_INACTIVE, "detail": "x"}]
        with mock.patch.object(fsl, "discover_targets", return_value=[]), \
             mock.patch.object(fsl, "sweep", return_value=flagged), \
             mock.patch.object(fsl, "apply_skip_list", return_value=flagged), \
             mock.patch.object(fsl, "alert") as malert:
            rc = fsl.main(["--dry-run"])
        self.assertEqual(rc, 0)
        malert.assert_not_called()

    def test_check_alerts_when_flagged(self):
        flagged = [{"persona": "archivist", "account": "exocortex",
                    "timer": "exocortex-data-snapshot.timer",
                    "key": "exocortex:exocortex-data-snapshot.timer",
                    "verdict": fsl.DISABLED_OR_INACTIVE, "detail": "x"}]
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(fsl, "discover_targets", return_value=[]), \
                 mock.patch.object(fsl, "sweep", return_value=flagged), \
                 mock.patch.object(fsl, "apply_skip_list", return_value=flagged), \
                 mock.patch.object(fsl, "STATE_DIR", Path(td)), \
                 mock.patch.object(fsl, "STATE_PATH", Path(td) / "state.json"), \
                 mock.patch.object(fsl, "alert") as malert:
                rc = fsl.main(["--check"])
            self.assertEqual(rc, 0)
            malert.assert_called_once()
            self.assertEqual(malert.call_args[1].get("skipped"), [])

    def test_once_is_an_alias_for_check(self):
        with mock.patch.object(fsl, "discover_targets", return_value=[]), \
             mock.patch.object(fsl, "sweep", return_value=[]), \
             mock.patch.object(fsl, "alert") as malert:
            rc = fsl.main(["--once"])
        self.assertEqual(rc, 0)
        malert.assert_not_called()

    def test_check_no_alert_when_all_healthy(self):
        with mock.patch.object(fsl, "discover_targets", return_value=[]), \
             mock.patch.object(fsl, "sweep", return_value=[]), \
             mock.patch.object(fsl, "alert") as malert:
            rc = fsl.main(["--check"])
        self.assertEqual(rc, 0)
        malert.assert_not_called()

    def test_cooldown_suppresses_second_alert_same_class(self):
        flagged = [{"persona": "archivist", "account": "exocortex",
                    "timer": "exocortex-data-snapshot.timer",
                    "key": "exocortex:exocortex-data-snapshot.timer",
                    "verdict": fsl.DISABLED_OR_INACTIVE, "detail": "x"}]
        with tempfile.TemporaryDirectory() as td:
            statep = Path(td) / "state.json"
            with mock.patch.object(fsl, "discover_targets", return_value=[]), \
                 mock.patch.object(fsl, "sweep", return_value=flagged), \
                 mock.patch.object(fsl, "apply_skip_list", return_value=flagged), \
                 mock.patch.object(fsl, "STATE_DIR", Path(td)), \
                 mock.patch.object(fsl, "STATE_PATH", statep):
                with mock.patch.object(fsl, "alert") as malert1:
                    fsl.main(["--check"])
                    malert1.assert_called_once()  # first fire alerts
                with mock.patch.object(fsl, "alert") as malert2:
                    fsl.main(["--check"])
                    malert2.assert_not_called()   # second fire within cooldown: suppressed
            # State file recorded the cooldown key.
            self.assertTrue(any("DISABLED_OR_INACTIVE" in k for k in json.loads(statep.read_text())))


class SnapshotSourceQuiescenceT3992(unittest.TestCase):
    """T-3992 — a change-driven target with an old commit only STALEs when the
    SOURCE was actually written since the last snapshot; a quiescent source (nothing
    to snapshot) is HEALTHY, not a false STALE. Fail-closed on an unreadable source."""

    def _check(self, *, commit_epoch, source_mtime=None, source_fail=False,
               source_garbage=False, threshold=3.0, now=NOW):
        t = _target(threshold=threshold, change_driven=True)
        return fsl.check_target(
            t, now=now,
            state_run=make_state_run(),  # active+enabled
            commit_run=make_commit_run(epoch=commit_epoch),
            source_run=make_source_run(mtime=source_mtime, fail=source_fail,
                                       garbage=source_garbage),
        )

    def test_old_commit_quiescent_source_is_HEALTHY(self):
        # The live exocortex-data case: 50d-old commit, but the tracked source has
        # NOT been written since (mtime older than the commit) → nothing to snapshot.
        r = self._check(commit_epoch=NOW - 50 * DAY, source_mtime=NOW - 55 * DAY)
        self.assertEqual(r["verdict"], fsl.HEALTHY)
        self.assertIn("T-3992", r["detail"])
        self.assertIn("quiescent", r["detail"])
        self.assertEqual(r["source_mtime"], int(NOW - 55 * DAY))

    def test_old_commit_source_written_since_is_STALE(self):
        # Source written AFTER the last snapshot but no commit followed → real break.
        r = self._check(commit_epoch=NOW - 50 * DAY, source_mtime=NOW - 1 * DAY)
        self.assertEqual(r["verdict"], fsl.STALE)
        self.assertIn("source written since last snapshot", r["detail"])

    def test_source_mtime_equal_commit_is_HEALTHY(self):
        # Boundary: source_mtime == commit_epoch → not written SINCE → suppress.
        r = self._check(commit_epoch=NOW - 50 * DAY, source_mtime=NOW - 50 * DAY)
        self.assertEqual(r["verdict"], fsl.HEALTHY)

    def test_old_commit_source_unreadable_is_STALE_failclosed(self):
        # Cannot verify quiescence → must NOT downgrade the break: STALE, fail-closed.
        r = self._check(commit_epoch=NOW - 50 * DAY, source_fail=True)
        self.assertEqual(r["verdict"], fsl.STALE)
        self.assertIn("fail-closed", r["detail"])

    def test_old_commit_source_garbage_is_STALE_failclosed(self):
        r = self._check(commit_epoch=NOW - 50 * DAY, source_garbage=True)
        self.assertEqual(r["verdict"], fsl.STALE)

    def test_old_commit_no_tracked_files_is_STALE_failclosed(self):
        # Empty ls-files output → UNKNOWN source → fail-closed STALE.
        r = self._check(commit_epoch=NOW - 50 * DAY, source_mtime=None)
        self.assertEqual(r["verdict"], fsl.STALE)

    def test_fresh_commit_never_consults_source(self):
        # age within threshold → HEALTHY before the source probe; a source_run that
        # raises if called proves the probe is not paid on the healthy common path.
        def _boom(argv, **kwargs):
            raise AssertionError("source_run must not be called for a fresh commit")
        t = _target(threshold=3.0, change_driven=True)
        r = fsl.check_target(
            t, now=NOW,
            state_run=make_state_run(),
            commit_run=make_commit_run(epoch=NOW - 1 * DAY),
            source_run=_boom,
        )
        self.assertEqual(r["verdict"], fsl.HEALTHY)

    def test_newest_source_mtime_uses_ls_files_not_find(self):
        # Guard the tracked-only contract: the probe must shell `git ls-files`
        # (tracked) — an untracked artifact must not count as a source write.
        captured = {}
        def _cap(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout="123\n", stderr="")
        t = _target(account=getpass.getuser(), change_driven=True)
        fsl._newest_source_mtime(t, run=_cap)
        joined = " ".join(captured["argv"])
        self.assertIn("git ls-files", joined)
        self.assertNotIn("find ", joined)


if __name__ == "__main__":
    unittest.main()
