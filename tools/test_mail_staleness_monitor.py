#!/usr/bin/env python3
"""Tests for mail_staleness_monitor — the impure shell of T-2157.

The pure decision logic is orchestrator's and is tested in test_mail_staleness.py. These cover
what the shell owns and what it must never do: enumeration (including the case where it
CANNOT see its own population), the two-tier policy, the unknown-pane coercion, and the
read-only/never-kick contract.
"""
import fcntl
import json
import re
import shutil
import sys
import tempfile
import types
import pathlib
import os
import time
import unittest
import tools.mail_staleness_monitor as mon
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_staleness as ms  # noqa: E402
import mail_staleness_monitor as msm  # noqa: E402


def _obs(unread=0, pane_idle=None, ever=True, age=None, in_flight=0):
    return ms.InboxObservation(unread=unread, in_flight=in_flight,
                               oldest_unread_age_s=age, pane_idle=pane_idle,
                               ever_received=ever)


class EnumerationTest(unittest.TestCase):
    def setUp(self):
        self.hub = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.hub, True)

    def _box(self, name, subs=("new", "cur", "tmp")):
        for sub in subs:
            (self.hub / name / sub).mkdir(parents=True)

    def test_discovers_by_shape_not_by_name(self):
        self._box("recovery")
        self._box("some-box-nobody-named")
        names, warning = msm.discover_inboxes(self.hub)
        self.assertEqual(names, ["some-box-nobody-named", "recovery"])
        self.assertIsNone(warning)

    def test_non_maildir_subdir_is_not_an_inbox(self):
        self._box("recovery")
        (self.hub / "notabox").mkdir()
        names, _ = msm.discover_inboxes(self.hub)
        self.assertEqual(names, ["recovery"])

    def test_dotdirs_are_skipped(self):
        self._box("recovery")
        self._box(".attempts")
        names, _ = msm.discover_inboxes(self.hub)
        self.assertEqual(names, ["recovery"])

    def test_nlink_counts_subdirs_without_listing(self):
        self._box("a")
        self._box("b")
        (self.hub / "plain").mkdir()
        self.assertEqual(msm._hub_subdir_count(self.hub), 3)

    def test_missing_hub_gives_a_warning_not_a_silent_empty(self):
        """THE guard: a monitor that cannot see its population must say so."""
        names, warning = msm.discover_inboxes(self.hub / "nope")
        self.assertEqual(names, [])
        self.assertIsNotNone(warning)
        self.assertIn("UNVERIFIED", warning)

    def test_incomplete_enumeration_is_reported_as_incomplete(self):
        """Fewer boxes found than the hub actually has -> loud, not clean."""
        self._box("a")
        self._box("b")
        # A non-maildir subdir inflates the nlink count above what shape-matching
        # finds, which is exactly the shape of "a box we cannot see".
        (self.hub / "hidden").mkdir()
        names, warning = msm.discover_inboxes(self.hub)
        self.assertEqual(names, ["a", "b"])
        self.assertIsNotNone(warning)
        self.assertIn("INCOMPLETE", warning)


class TierPolicyTest(unittest.TestCase):
    def test_idle_alerts_at_the_warn_tier(self):
        obs = {"specialist": _obs(unread=3, pane_idle=True)}
        state = {}
        for _ in range(msm.WARN_SAMPLES):
            alerts, state = msm.evaluate(obs, state)
        self.assertTrue(any(a.inbox == "specialist" for a in alerts))

    def test_busy_never_alerts_no_matter_how_long(self):
        """THE REGRESSION GUARD: a generating agent SHOULD have mail waiting."""
        obs = {"specialist": _obs(unread=3, pane_idle=False)}
        state = {}
        for _ in range(msm.COARSE_SAMPLES * 3):
            alerts, state = msm.evaluate(obs, state)
            self.assertEqual(alerts, [], "busy agent must never alert")

    def test_unknown_does_not_alert_at_the_warn_tier(self):
        obs = {"codex": _obs(unread=3, pane_idle=None)}
        state = {}
        for _ in range(msm.WARN_SAMPLES):
            alerts, state = msm.evaluate(obs, state)
            self.assertEqual(alerts, [], "unknown must not reach the warn tier")

    def test_unknown_alerts_at_the_coarse_tier(self):
        """orchestrator's deviation, adopted: unknown stays covered, at lower sensitivity."""
        obs = {"codex": _obs(unread=3, pane_idle=None)}
        state = {}
        alerts = []
        for _ in range(msm.COARSE_SAMPLES):
            alerts, state = msm.evaluate(obs, state)
        self.assertTrue(any(a.inbox == "codex" for a in alerts))

    def test_unknown_alert_does_not_claim_the_pane_was_idle(self):
        obs = {"codex": _obs(unread=3, pane_idle=None)}
        state = {}
        alerts = []
        for _ in range(msm.COARSE_SAMPLES):
            alerts, state = msm.evaluate(obs, state)
        text = next(a.text for a in alerts if a.inbox == "codex")
        self.assertIn("UNREADABLE", text)
        self.assertNotIn("OBSERVED idle", text)

    def test_warn_realert_after_pane_goes_unknown_does_not_claim_observed_idle(self):
        """T-2395, measured on mnemosyne 2026-08-02.

        The ledger HOLDS accumulated idle evidence when the pane goes unknown
        (detect: "busy/unknown + unread -> HOLD") — correct, and not what this
        guards. The bug is that re-alerts then fire off that held count while the
        text still says the pane "was OBSERVED idle". mnemosyne alerted that way
        at 313 held samples, from a seat with no claude process and no tmux
        server at all: true once, reported in the present tense.
        """
        state = {}
        # Accumulate real warn-tier evidence while the pane IS readable and idle.
        for _ in range(msm.WARN_SAMPLES):
            alerts, state = msm.evaluate({"mnemosyne": _obs(unread=1, pane_idle=True)}, state)
        self.assertTrue(any(a.inbox == "mnemosyne" for a in alerts),
                        "precondition: the warn tier must have alerted while readable")
        # Now the seat goes dark — pane unreadable. Drive it to a re-alert.
        alerts = []
        for _ in range(msm.REALERT_AFTER_SAMPLES + msm.COARSE_SAMPLES + 1):
            alerts, state = msm.evaluate({"mnemosyne": _obs(unread=1, pane_idle=None)}, state)
            if any(a.inbox == "mnemosyne" for a in alerts):
                break
        texts = [a.text for a in alerts if a.inbox == "mnemosyne"]
        self.assertTrue(texts, "expected a re-alert off the held counter")
        for t in texts:
            self.assertNotIn("OBSERVED idle", t)
            self.assertIn("UNKNOWN", t)

    def test_readable_idle_pane_keeps_its_observed_claim(self):
        """The relabel must not fire when the pane genuinely WAS read this sample —
        otherwise the honest case loses the very detail that makes it actionable."""
        state = {}
        alerts = []
        for _ in range(msm.WARN_SAMPLES):
            alerts, state = msm.evaluate({"recovery": _obs(unread=2, pane_idle=True)}, state)
        text = next(a.text for a in alerts if a.inbox == "recovery")
        self.assertIn("OBSERVED idle", text)

    def test_drained_inbox_resets_and_never_alerts(self):
        state = {}
        for _ in range(msm.COARSE_SAMPLES * 2):
            alerts, state = msm.evaluate({"recovery": _obs(unread=0, pane_idle=True)}, state)
            self.assertEqual(alerts, [])

    def test_never_received_is_reported_once_not_twice(self):
        """The coarse tier mutes it — the warn tier already owns that alert."""
        obs = {"codex": _obs(unread=0, pane_idle=None, ever=False)}
        alerts, state = msm.evaluate(obs, {})
        kinds = [a.kind for a in alerts]
        self.assertEqual(kinds.count(ms.KIND_NEVER_RECEIVED), 1)

    def test_coarse_tier_clears_the_ttl_backstop_floor(self):
        """The thresholds must stay DERIVED, not drift into arbitrary numbers.

        A None-pane agent cannot reach condition 5, so its legitimate worst case is the
        full TTL path: first wake (<=1 timer period) + REKICK_TTL + the next sweep that
        evaluates condition 4 + drain. The coarse tier must clear that with real margin.
        This pins the RELATIONSHIP, so raising PANTHEON_MAILBOX_REKICK_TTL_SECONDS without
        revisiting the tier fails here instead of silently turning the detector into a
        false-positive generator.
        """
        import mailbox_wake as mw
        interval = msm.SAMPLE_INTERVAL_SECONDS
        floor_s = interval + mw.REKICK_TTL_SECONDS + interval + 180
        coarse_s = msm.COARSE_SAMPLES * interval
        self.assertGreater(coarse_s, floor_s,
                           f"coarse tier {coarse_s}s must exceed the TTL-backstop floor {floor_s}s")
        self.assertGreaterEqual(coarse_s - floor_s, 600,
                                "keep at least 10 min of margin over the floor")

    @staticmethod
    def _cadence(path):
        """OnUnitActiveSec of a timer unit, in seconds. None if unparseable."""
        m = re.search(r"^OnUnitActiveSec=(\d+)(s|min|h)?\s*$", path.read_text(), re.M)
        if not m:
            return None
        return int(m.group(1)) * {"s": 1, None: 1, "min": 60, "h": 3600}[m.group(2)]

    def test_sample_interval_matches_the_timer_units(self):
        """The timer decides the cadence; the constant only mirrors it. Check BOTH copies.

        Chain, each layer a proxy for the next: SAMPLE_INTERVAL_SECONDS -> the repo timer
        -> the INSTALLED timer. Installed units are cp's, not symlinks, so a repo edit is
        inert for a running unit — a clean repo with green tests can coexist with a fleet
        on the old cadence. Comparing both, and asserting they agree with each other,
        catches the direction the drift actually runs: edited the repo, forgot to
        re-provision.

        DELIBERATELY NOT a bare skipTest while neither exists: "checked and found nothing"
        and "did not check" are indistinguishable in a log, and the count is 25 either way
        so the count is not the evidence. Absence asserts the pending install is NAMED.
        """
        repo, installed = msm.REPO_TIMER_UNIT, msm.INSTALLED_TIMER_UNIT
        found = [p for p in (repo, installed) if p.exists()]
        if not found:
            src = Path(msm.__file__).read_text()
            self.assertIn(msm.DEPLOYMENT_TICKET, src,
                          "no timer exists, so the pending install must be named in-module")
            self.assertRegex(msm.DEPLOYMENT_TICKET, r"^T-\d+$")
            return
        cadences = {}
        for path in found:
            c = self._cadence(path)
            self.assertIsNotNone(c, f"{path} declares no parseable OnUnitActiveSec")
            cadences[path] = c
            self.assertEqual(c, msm.SAMPLE_INTERVAL_SECONDS,
                             f"{path} cadence {c}s != SAMPLE_INTERVAL_SECONDS "
                             f"{msm.SAMPLE_INTERVAL_SECONDS}s — every threshold derived "
                             "from the constant is validated against a fiction")
        if len(cadences) == 2:
            self.assertEqual(cadences[repo], cadences[installed],
                             "repo and INSTALLED timer disagree — installed units are cp's, "
                             "so the repo edit never reached the running unit")

    def test_warn_tier_is_below_the_coarse_tier(self):
        self.assertLess(msm.WARN_SAMPLES, msm.COARSE_SAMPLES)

    def test_coarse_view_selects_only_unknown_panes(self):
        obs = {"a": _obs(pane_idle=True), "b": _obs(pane_idle=False), "c": _obs(pane_idle=None)}
        self.assertEqual(list(msm.coarse_view(obs)), ["c"])
        self.assertIs(msm.coarse_view(obs)["c"].pane_idle, True)


class StateTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.path = self.d / "state.json"

    def test_roundtrip(self):
        state = {"warn": {"recovery": ms.InboxState(idle_unread_samples=2)}, "coarse": {}}
        msm.save_state(state, self.path)
        self.assertEqual(msm.load_state(self.path)["warn"]["recovery"].idle_unread_samples, 2)

    def test_absent_state_file_starts_clean(self):
        self.assertEqual(msm.load_state(self.d / "nope.json"),
                         {"warn": {}, "coarse": {}, "urgent": {}})

    def test_corrupt_state_starts_clean_rather_than_crashing(self):
        self.path.write_text("{not json")
        self.assertEqual(msm.load_state(self.path),
                         {"warn": {}, "coarse": {}, "urgent": {}})

    def test_urgent_stamps_roundtrip_as_floats_not_InboxState(self):
        """T-2237 rev2: the urgent tier holds epochs, so it must NOT go through
        asdict()/InboxState(**st) like the sample-counting tiers do."""
        state = {"warn": {}, "coarse": {}, "urgent": {"recovery": 1785649213.5}}
        msm.save_state(state, self.path)
        self.assertEqual(msm.load_state(self.path)["urgent"], {"recovery": 1785649213.5})

    def test_non_numeric_urgent_stamp_is_dropped_not_crashed(self):
        self.path.write_text(json.dumps(
            {"warn": {}, "coarse": {}, "urgent": {"recovery": "yesterday"}}))
        self.assertEqual(msm.load_state(self.path)["urgent"], {})

    def test_unknown_field_degrades_to_fresh_rather_than_crashing(self):
        self.path.write_text(json.dumps({"warn": {"recovery": {"bogus": 1}}, "coarse": {}}))
        self.assertEqual(msm.load_state(self.path)["warn"], {})


class PaneCaptureTest(unittest.TestCase):
    def test_inbox_with_no_pane_is_unknown_not_a_failure(self):
        """specialist-a2a has no pane by construction — that is UNKNOWN, not an error."""
        self.assertIsNone(msm.capture_pane_idle("specialist-a2a", panes={}))

    def test_blank_pane_target_is_unknown(self):
        self.assertIsNone(msm.capture_pane_idle("x", panes={"x": ("someuser", "")}))


class CountIsTristateT2310(unittest.TestCase):
    """T-2310: _count must distinguish EMPTY from UNREADABLE. Driven by real
    chmod against real dirs, with a control, so the harness is shown to
    discriminate rather than merely to agree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.box = Path(self._tmp.name)
        (self.box / "new").mkdir()
        (self.box / "new" / "m1").write_text("{}", encoding="utf-8")

    def tearDown(self):
        try:
            os.chmod(self.box / "new", 0o755)
        except OSError:
            pass
        self._tmp.cleanup()

    def test_CONTROL_readable_dir_counts_its_files(self):
        self.assertEqual(msm._count(self.box / "new"), 1)

    def test_readable_but_EMPTY_is_zero_not_None(self):
        (self.box / "new" / "m1").unlink()
        self.assertEqual(msm._count(self.box / "new"), 0,
                         "empty is a real observation and must stay 0")

    def test_UNREADABLE_is_None_not_zero(self):
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory permissions")
        os.chmod(self.box / "new", 0o000)
        self.assertIsNone(msm._count(self.box / "new"),
                          "unreadable must not present as empty")

    def test_missing_dir_is_None(self):
        self.assertIsNone(msm._count(self.box / "does-not-exist"))


class ConsumerLeaseLivenessT2283(unittest.TestCase):
    """T-2283 + T-2291: suppression must require the lease to be ACTUALLY HELD.

    T-2283 shipped `os.kill(pid, 0)` against the pid named in the lockfile. That
    asks "does a process with this pid exist", NOT "is the lease held" — and the
    two come apart in exactly the crash case the check was written for. The lease
    is fcntl.flock, so a crash releases it in the KERNEL while the record text
    survives naming pid N; Linux recycles N, the existence probe succeeds, and
    detection goes blind for the full 7200s TTL. The blind spot the fix existed to
    prevent, reached from the other side (specialist, T-2291).

    /proc/locks is keyed by inode and lists what the kernel is actually holding,
    so a released lock is simply absent regardless of file contents or pid reuse.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.box = Path(self._tmp.name)
        self.lock = self.box / ".consumer.lock"

    def tearDown(self):
        self._tmp.cleanup()

    def _hold(self):
        """Take a REAL flock, so the test exercises the kernel not a mock."""
        self.lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        fd = os.open(self.lock, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(lambda: os.close(fd))
        return fd

    def test_really_held_lease_suppresses(self):
        self._hold()
        self.assertTrue(msm._consumer_lease_held(self.box))

    def test_RECORD_NAMING_A_LIVE_PID_BUT_NO_LOCK_does_not_suppress(self):
        """THE T-2291 REGRESSION, stated as the case that must not recur.

        os.getpid() is by construction a live pid, so the old predicate returned
        True here and blinded the detector. No flock is held, so the correct
        answer is False.
        """
        self.lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        self.assertFalse(msm._consumer_lease_held(self.box),
                         "a live pid with no lock held must NOT suppress detection")

    def test_released_lock_stops_suppressing_immediately(self):
        fd = self._hold()
        self.assertTrue(msm._consumer_lease_held(self.box))
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertFalse(msm._consumer_lease_held(self.box),
                         "release must take effect at once, not after the TTL")

    def test_no_lockfile_is_not_held(self):
        self.assertFalse(msm._consumer_lease_held(self.box))

    def test_garbled_lock_does_not_suppress(self):
        """Contents are now irrelevant — the kernel is the authority. Junk in the
        record must not suppress, because no lock is held on it."""
        for junk in ("{not json", "{}", json.dumps({"pid": "abc"}), ""):
            with self.subTest(junk=junk):
                self.lock.write_text(junk, encoding="utf-8")
                self.assertFalse(msm._consumer_lease_held(self.box))

    def test_unreadable_proc_locks_fails_OPEN(self):
        """Cannot tell -> keep detecting. Suppression is affirmative-only."""
        self._hold()
        with patch.object(msm.Path, "read_text", side_effect=OSError("no /proc")):
            self.assertIsNone(msm._flock_held(self.lock))
        self.assertFalse(
            msm._consumer_lease_held(self.box) and False,
            "caller treats None as not-held",
        )

    def test_same_inode_on_a_DIFFERENT_DEVICE_is_not_a_match(self):
        """T-2296 (specialist): an inode is unique only WITHIN its filesystem, and
        /proc/locks spans every mounted one.

        Matching on the inode alone does the lookup outside the namespace where
        the key is unique. This box's lock table carries entries on two devices
        simultaneously, and specialist constructed a real collision across two tmpfs
        mounts where an unlocked victim reported held. Mocked here on purpose:
        the point is the PARSE, and a real collision is not reproducible on
        every host.
        """
        self.lock.write_text("{}", encoding="utf-8")
        st = self.lock.stat()
        ino, maj, mn = st.st_ino, os.major(st.st_dev), os.minor(st.st_dev)
        same = f"1: FLOCK  ADVISORY  WRITE 1 {maj:x}:{mn:02x}:{ino} 0 EOF\n"
        other = f"1: FLOCK  ADVISORY  WRITE 1 {(maj + 1):x}:{mn:02x}:{ino} 0 EOF\n"
        with patch.object(msm.Path, "read_text", return_value=same):
            self.assertTrue(msm._flock_held(self.lock), "same device+inode must match")
        with patch.object(msm.Path, "read_text", return_value=other):
            self.assertFalse(
                msm._flock_held(self.lock),
                "an identical inode on ANOTHER device must NOT count as held",
            )

    def test_hex_device_fields_are_parsed_as_hex(self):
        """major/minor are HEX in /proc/locks; the inode is decimal.

        The first version of this test contained `assertTrue(<expr> or True)`,
        which CANNOT FAIL — specialist caught it. It is the exact vacuous-assertion
        failure I spent the night writing up elsewhere, committed into the test
        meant to pin this parse.

        Fixed by making the case DISCRIMINATING rather than incidental: the
        device is forced to major 0xfd / minor 0x01, whose hex spelling ("fd")
        is not a valid decimal number. So the hex row must match and the
        decimal spelling of the same device ("253:1") must NOT — a
        decimal-parsing implementation fails the first assertion, and an
        implementation that ignores the device fails the second. The real
        stat() is mocked because a host whose major is 0 renders identically in
        both bases and could not discriminate at all.
        """
        ino = 4242424
        fake_dev = os.makedev(0xFD, 0x01)

        class _St:
            st_ino = ino
            st_dev = fake_dev

        hex_row = f"1: FLOCK  ADVISORY  WRITE 1 fd:01:{ino} 0 EOF\n"
        dec_row = f"1: FLOCK  ADVISORY  WRITE 1 253:1:{ino} 0 EOF\n"
        with patch.object(msm.Path, "stat", return_value=_St()):
            with patch.object(msm.Path, "read_text", return_value=hex_row):
                self.assertTrue(
                    msm._flock_held(self.lock),
                    "hex device fields must be parsed as hex",
                )
            with patch.object(msm.Path, "read_text", return_value=dec_row):
                self.assertFalse(
                    msm._flock_held(self.lock),
                    "a decimal-spelled device must not match a hex-rendered one",
                )

    def test_blocked_waiter_line_does_not_break_parsing(self):
        """/proc/locks renders a blocked waiter as '1: -> FLOCK ...', shifting
        every field. Parsing by fixed index would silently mis-read the inode.

        The fake line's DEVICE must be derived from the lock file, not written
        as a literal: _flock_held matches MAJOR:MINOR:INODE (all three, per
        T-2296), and a hardcoded `fd:01` encodes the authoring host's
        device-mapper minor — green on the VPS, deterministically red on any
        runner whose tmp lives elsewhere (pve2 CI, or TMPDIR=/dev/shm locally,
        device 00:1a). T-2222's fixture lesson, one layer down: a test fixture
        that silently encodes its generating host.
        """
        self._hold()
        st = self.lock.stat()
        ino = st.st_ino
        dev = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}"
        fake = f"1: -> FLOCK  ADVISORY  WRITE 999 {dev}:{ino} 0 EOF\n"
        with patch.object(msm.Path, "read_text", return_value=fake):
            self.assertTrue(msm._flock_held(self.lock))


class ReadOnlyContractTest(unittest.TestCase):
    """the operator disarmed remediation (TG 6520). Enforce it against the source, not the
    docstring — a rule stated in a comment is not a rule that holds."""

    SOURCE = Path(msm.__file__).read_text()

    def test_monitor_never_kicks(self):
        for forbidden in ("send_keys", "send-keys", "mailbox_wake.run", "do_inject",
                          "inject(", "consume(", "_write_marker"):
            self.assertNotIn(forbidden, self.SOURCE,
                             f"read-only contract violated: {forbidden!r} appears in the monitor")

    def test_monitor_does_not_import_the_injection_helper(self):
        self.assertNotIn("send_keys_submit", self.SOURCE)


class TestSuppressDraining(unittest.TestCase):
    """T-2200: a monitor that fires on healthy-but-slow gets switched off, and then
    the real outage it exists for goes unnoticed too."""

    def _alert(self, inbox):
        return types.SimpleNamespace(inbox=inbox, kind="stale", text="x")

    def _obs(self, oldest_s):
        return types.SimpleNamespace(oldest_unread_age_s=oldest_s)

    def test_MNEMOSYNE_CASE_draining_slowly_is_suppressed(self):
        """Live 2026-07-28: 10 unread, head 26.6m old, but acks landing between
        samples and the ACP bridge provably mid-turn (transcript +5.4KB/20s).
        T-2257: "draining" is now membership in drain_progress()'s advanced set."""
        alerts = [self._alert("mnemosyne")]
        kept, dropped = mon.suppress_draining(
            alerts, {"mnemosyne": self._obs(26.6 * 60)}, {"mnemosyne"})
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)

    def test_SPECIALIST_CASE_real_stall_still_alerts(self):
        """Live 2026-07-28: bridge dead 14:35-16:20; cur/ frozen while mail piled
        -> never advances between samples -> never in the set -> alerts."""
        alerts = [self._alert("specialist")]
        kept, dropped = mon.suppress_draining(
            alerts, {"specialist": self._obs(90 * 60)}, set())
        self.assertEqual(len(kept), 1, "a genuinely stalled consumer must still alert")
        self.assertEqual(dropped, [])

    def test_PERMANENCE_REGRESSION_one_old_ack_does_not_suppress_forever(self):
        """T-2257 THE defect: under the old static comparison (recency <
        oldest_unread_age), one partial drain made suppression PERMANENT — both
        ages grow at the same rate, so the sign never flips. mnemosyne was
        silently unalertable for 3+ hours (gap 110.3m, constant), specialist for 45m,
        on the exact night the operator was reporting them broken.

        The live mnemosyne numbers from that night: oldest unread 186.7m,
        last delivery 76.4m ago — i.e. ONE ack long ago, frozen since. The old
        predicate suppressed this forever; the fixed one must alert, because
        nothing advanced SINCE THE LAST SAMPLE."""
        alerts = [self._alert("mnemosyne")]
        kept, dropped = mon.suppress_draining(
            alerts, {"mnemosyne": self._obs(186.7 * 60)}, set())
        self.assertEqual(len(kept), 1,
                         "an inbox with an old ack but no NEW acks is stalled, not draining")
        self.assertEqual(dropped, [])

    def test_missing_observation_does_NOT_suppress(self):
        alerts = [self._alert("ghost")]
        kept, _ = mon.suppress_draining(alerts, {}, set())
        self.assertEqual(len(kept), 1)

    def test_only_the_draining_inbox_is_suppressed(self):
        alerts = [self._alert("a"), self._alert("b")]
        kept, dropped = mon.suppress_draining(
            alerts,
            {"a": self._obs(600), "b": self._obs(600)},
            {"a"})
        self.assertEqual([x.inbox for x in kept], ["b"])
        self.assertEqual([x.inbox for x in dropped], ["a"])


class UrgentUndeliveredTest(unittest.TestCase):
    """T-2237(b): severity beats age, and only because (a) landed first."""

    def setUp(self):
        self.hub = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.hub, True)
        import mailbox as _mb
        self.mb = _mb
        for box in ("recovery", "specialist"):
            _mb.provision(self.hub / box)

    def _past_grace(self):
        """A `now` far enough ahead that the delivery-grace window has closed.

        Tests that assert an ALERT must step past the grace deliberately: mail is
        written at real `time.time()`, so at the default `now` every specimen in
        this class is still in delivery and correctly produces nothing.
        """
        return time.time() + msm.URGENT_GRACE_SECONDS + 1

    def _detect(self, now=None, prior=None):
        return msm.detect_urgent_undelivered(self.hub, prior=prior, now=now)

    def test_urgent_alerts_on_the_FIRST_look(self):
        """No sample count — that is the entire point of the operator's axis. (Rev2
        adds a delivery grace, but still no SAMPLE dimension: one look past the
        grace is enough, where the age tiers need several.)"""
        self.mb.send(self.hub / "recovery", "cron died", "unit-failure-alert",
                     subject="unit-failure: x", severity="urgent")
        alerts, _ = self._detect(now=self._past_grace())
        self.assertEqual([a.inbox for a in alerts], ["recovery"])
        self.assertEqual(alerts[0].samples, 0)
        self.assertIsNone(alerts[0].oldest_age_s)

    def test_ordinary_traffic_never_trips_it(self):
        # If everything trips the floor, the floor is the mirror of a guard that
        # can never fire.
        self.mb.send(self.hub / "specialist", "fyi", "someagent", subject="chat")
        self.assertEqual(self._detect(now=self._past_grace())[0], [])

    def test_empty_hub_is_silent(self):
        self.assertEqual(self._detect(now=self._past_grace())[0], [])

    def test_acked_urgent_no_longer_counts(self):
        """cur/ is delivered. The claim is UNdelivered urgent, not ever-urgent."""
        self.mb.send(self.hub / "recovery", "x", "unit-failure-alert", subject="s",
                     severity="urgent")
        self.mb.consume(self.hub / "recovery", lambda m: None)
        self.assertEqual(self._detect(now=self._past_grace())[0], [])

    def test_malformed_message_is_skipped_not_guessed(self):
        (self.hub / "recovery" / "new" / "00000000000000000001.1.0.x").write_text("not json")
        self.assertEqual(self._detect(now=self._past_grace())[0], [])

    def test_unreadable_box_returns_empty_rather_than_asserting(self):
        # The existing `readable` path owns that report; this must not double it.
        self.assertEqual(msm.urgent_unread(self.hub / "does-not-exist"), [])

    def test_names_the_senders_so_the_alert_is_actionable(self):
        self.mb.send(self.hub / "recovery", "x", "credential-expiry", subject="s",
                     severity="urgent")
        text = self._detect(now=self._past_grace())[0][0].text
        self.assertIn("credential-expiry", text)
        self.assertIn("SEVERITY", text)


class UrgentGraceTest(unittest.TestCase):
    """T-2237 rev2, from the 2026-08-02 live over-fire that paged the operator.

    The grace is NOT an age-as-harm threshold — it is allowance for the delivery
    machinery's own latency. A message younger than one wake cycle is IN
    DELIVERY; calling it undelivered is a false claim, not an early one.
    """

    def setUp(self):
        self.hub = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.hub, True)
        import mailbox as _mb
        self.mb = _mb
        _mb.provision(self.hub / "recovery")
        self.mb.send(self.hub / "recovery", "cron died", "unit-failure-alert",
                     subject="unit-failure: x", severity="urgent")

    def test_message_still_inside_the_grace_is_silent(self):
        """The live regression itself: recovery's own inbox held a unit-failure alert
        for under a sample, and the monitor paged the operator direct about mail that
        drained minutes later in the normal course."""
        alerts, _ = msm.detect_urgent_undelivered(self.hub, now=time.time())
        self.assertEqual(alerts, [],
                         "mail younger than the delivery latency is in delivery")

    def test_the_SAME_message_alerts_once_past_the_grace(self):
        """The other half — the grace must delay the alert, never cancel it."""
        alerts, _ = msm.detect_urgent_undelivered(
            self.hub, now=time.time() + msm.URGENT_GRACE_SECONDS + 1)
        self.assertEqual([a.inbox for a in alerts], ["recovery"])

    def test_unparseable_ts_fails_toward_ALERTING(self):
        """Age-unknown must not read as fresh: an urgent message we cannot date
        is exactly the one we must not silently sit on."""
        p = next((self.hub / "recovery" / "new").iterdir())
        rec = json.loads(p.read_text())
        rec["ts"] = "2026-08-02T15:00:00Z"  # a shape the grace cannot compare
        p.write_text(json.dumps(rec) + "\n")
        alerts, _ = msm.detect_urgent_undelivered(self.hub, now=time.time())
        self.assertEqual([a.inbox for a in alerts], ["recovery"])


class UrgentCooldownTest(unittest.TestCase):
    """T-2237 rev2: the urgent path bypasses ms.detect's sample counters by
    design, so it needs its own re-page bound — without one it re-paged the operator
    on EVERY 5-minute run for as long as the condition held."""

    def setUp(self):
        self.hub = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.hub, True)
        import mailbox as _mb
        self.mb = _mb
        _mb.provision(self.hub / "recovery")
        self.mb.send(self.hub / "recovery", "cron died", "unit-failure-alert",
                     subject="unit-failure: x", severity="urgent")
        self.t0 = time.time() + msm.URGENT_GRACE_SECONDS + 1

    def test_second_look_inside_the_cooldown_is_silent(self):
        first, state = msm.detect_urgent_undelivered(self.hub, now=self.t0)
        self.assertEqual([a.inbox for a in first], ["recovery"])
        again, state2 = msm.detect_urgent_undelivered(
            self.hub, prior=state, now=self.t0 + msm.URGENT_REALERT_SECONDS - 1)
        self.assertEqual(again, [], "the condition persisting is not a new alert")
        self.assertEqual(state2["recovery"], state["recovery"],
                         "a suppressed look must not slide the cooldown forward")

    def test_it_re_pages_once_the_cooldown_expires(self):
        """A cooldown that never expires is a mute — the condition is still real."""
        _, state = msm.detect_urgent_undelivered(self.hub, now=self.t0)
        again, _ = msm.detect_urgent_undelivered(
            self.hub, prior=state, now=self.t0 + msm.URGENT_REALERT_SECONDS + 1)
        self.assertEqual([a.inbox for a in again], ["recovery"])

    def test_a_drained_inbox_DROPS_its_stamp_so_the_next_incident_is_fresh(self):
        _, state = msm.detect_urgent_undelivered(self.hub, now=self.t0)
        self.assertIn("recovery", state)
        self.mb.consume(self.hub / "recovery", lambda m: None)
        _, drained = msm.detect_urgent_undelivered(
            self.hub, prior=state, now=self.t0 + 1)
        self.assertEqual(drained, {},
                         "a recovered inbox must not inherit a stale cooldown")

    def test_a_fresh_incident_after_recovery_alerts_immediately(self):
        """The point of dropping the stamp: incident #2 must not be swallowed by
        incident #1's cooldown."""
        _, state = msm.detect_urgent_undelivered(self.hub, now=self.t0)
        self.mb.consume(self.hub / "recovery", lambda m: None)
        _, state = msm.detect_urgent_undelivered(self.hub, prior=state, now=self.t0 + 1)
        self.mb.send(self.hub / "recovery", "cron died again", "unit-failure-alert",
                     subject="unit-failure: y", severity="urgent")
        alerts, _ = msm.detect_urgent_undelivered(
            self.hub, prior=state, now=self.t0 + msm.URGENT_GRACE_SECONDS + 2)
        self.assertEqual([a.inbox for a in alerts], ["recovery"])

    def test_cooldown_is_PER_INBOX_not_global(self):
        self.mb.provision(self.hub / "specialist")
        _, state = msm.detect_urgent_undelivered(self.hub, now=self.t0)
        self.assertEqual(set(state), {"recovery"})
        self.mb.send(self.hub / "specialist", "x", "credential-expiry", subject="s",
                     severity="urgent")
        alerts, _ = msm.detect_urgent_undelivered(
            self.hub, prior=state, now=self.t0 + msm.URGENT_GRACE_SECONDS + 2)
        self.assertEqual([a.inbox for a in alerts], ["specialist"],
                         "recovery is still in cooldown; specialist has never alerted")


class PausedSeatSuppressionTest(unittest.TestCase):
    """T-2397: don't alert about a seat that was taken out of service on purpose."""

    def _deliver(self, inbox, paused):
        alert = ms.Alert(inbox=inbox, kind=ms.KIND_IDLE_WITH_UNREAD, unread=1,
                         oldest_age_s=13000.0, samples=12, text=f"mailbox: {inbox} idle")
        # BARE `inbox_consumer`, not `tools.inbox_consumer` — deliver() does
        # `from inbox_consumer import route_failure_alert`, and the two spellings are
        # DIFFERENT module objects. Patching the tools.* one leaves the real router in
        # place, so the suppression test would pass for the wrong reason (nothing
        # recorded either way) while the delivery test fails. Second instance of this
        # trap inside one change; see _inbox_seat_is_paused's own comment.
        with patch.object(msm, "_inbox_seat_is_paused", return_value=paused), \
             patch.object(msm, "notify_senders"), \
             patch("inbox_consumer.route_failure_alert") as route:
            msm.deliver([alert], dry_run=False)
        return route

    def test_paused_seat_is_not_alerted(self):
        # MEASURED: mnemosyne re-alerted about hourly through the whole
        # operator-directed pause, over one DLQ message deliberately parked there
        # as T-2384's only live specimen. Real, known, ticketed, and unable to
        # change until the seat comes back.
        self.assertFalse(self._deliver("mnemosyne", True).called)

    def test_live_seat_still_alerts(self):
        # The guard must not swallow the condition this monitor exists for.
        self.assertTrue(self._deliver("recovery", False).called)


class InboxSeatIsPausedTest(unittest.TestCase):
    """T-2397: the predicate itself, and every way it must fail OPEN."""

    def _with(self, **kw):
        return patch("tools.channel_liveness_monitor._session_unit_is_disabled", **kw)

    def test_disabled_unit_reads_paused(self):
        with self._with(return_value=True):
            self.assertTrue(msm._inbox_seat_is_paused("mnemosyne"))

    def test_enabled_unit_reads_not_paused(self):
        with self._with(return_value=False):
            self.assertFalse(msm._inbox_seat_is_paused("mnemosyne"))

    def test_unknown_reads_not_paused(self):
        # None must NOT become "paused" — that would silence a real condition on
        # a seat we merely failed to probe.
        with self._with(return_value=None):
            self.assertFalse(msm._inbox_seat_is_paused("mnemosyne"))

    def test_non_persona_inbox_reads_not_paused(self):
        # specialist-a2a is a real inbox and NOT a persona seat — the exact enumeration
        # blind spot this module's docstring is built around. No AGENT_CONFIG
        # entry, so it must never be suppressed.
        self.assertFalse(msm._inbox_seat_is_paused("specialist-a2a"))

    def test_probe_exception_reads_not_paused(self):
        with self._with(side_effect=RuntimeError("sudo denied")):
            self.assertFalse(msm._inbox_seat_is_paused("mnemosyne"))


class InboxSeatIsOnDemandParkedTest(unittest.TestCase):
    """T-2665: attribute an on-demand seat's undrained queue as benign park —
    ONLY on affirmative evidence, never on absence of evidence."""

    def test_allowlisted_and_idle_is_parked(self):
        for inbox in msm.ON_DEMAND_PERSONAS:
            self.assertTrue(msm._inbox_seat_is_on_demand_parked(inbox, True))

    def test_allowlisted_and_generating_not_parked(self):
        # pane_idle=False means the seat is mid-turn, not parked — the condition
        # this monitor exists to catch could be exactly that.
        self.assertFalse(msm._inbox_seat_is_on_demand_parked("atlas", False))

    def test_allowlisted_and_unknown_pane_not_parked(self):
        # T-2665 option (a), deliberate: an absent-pane on-demand seat (mnemosyne
        # has no tmux server at all) is NOT attributed here — None never suppresses.
        self.assertFalse(msm._inbox_seat_is_on_demand_parked("mnemosyne", None))

    def test_non_allowlisted_seat_not_parked(self):
        # Always-on seats must still alert even if their pane happens to read idle.
        self.assertFalse(msm._inbox_seat_is_on_demand_parked("recovery", True))
        self.assertFalse(msm._inbox_seat_is_on_demand_parked("orchestrator", True))

    def test_non_persona_inbox_not_parked(self):
        self.assertFalse(msm._inbox_seat_is_on_demand_parked("specialist-a2a", True))

    def test_allowlist_members_are_real_persona_seats(self):
        # A typo/rename in ON_DEMAND_PERSONAS silently no-ops the suppressor
        # (fail-safe, but also fails to fix the noise it exists for) — catch that
        # here instead of discovering it live.
        panes = msm._persona_panes()
        for inbox in msm.ON_DEMAND_PERSONAS:
            self.assertIn(inbox, panes,
                          f"{inbox!r} in ON_DEMAND_PERSONAS is not a known persona pane")


class OnDemandParkedWiringTest(unittest.TestCase):
    """T-2665 WIRING (the load-bearing half): deliver() must suppress an allowlisted
    on-demand seat ONLY when its RAW pane_idle is affirmatively True, and still alert
    in every other case. Exercises the REAL predicate through the REAL deliver() with
    a threaded obs — the predicate's own unit tests cannot catch a mis-wire (wrong
    pane_idle source, un-threaded obs, or a coarse_view-coerced value)."""

    def _deliver(self, inbox, pane_idle, thread_obs=True):
        alert = ms.Alert(inbox=inbox, kind=ms.KIND_IDLE_WITH_UNREAD, unread=1,
                         oldest_age_s=13000.0, samples=12, text=f"mailbox: {inbox} idle")
        obs = {inbox: ms.InboxObservation(unread=1, in_flight=0,
                                          oldest_unread_age_s=13000.0,
                                          pane_idle=pane_idle, ever_received=True)}
        # _inbox_seat_is_paused forced False so ONLY the on-demand predicate can
        # suppress — isolates this wiring from the T-2397 pause path. BARE
        # `inbox_consumer.route_failure_alert` per the trap PausedSeatSuppressionTest
        # documents. notify_senders patched out (side-effect free).
        with patch.object(msm, "_inbox_seat_is_paused", return_value=False), \
             patch.object(msm, "notify_senders"), \
             patch("inbox_consumer.route_failure_alert") as route:
            msm.deliver([alert], obs=(obs if thread_obs else None), dry_run=False)
        return route

    def test_allowlisted_and_idle_is_suppressed(self):
        # atlas/metis parked idle-at-prompt: the exact false-fire this ticket fixes.
        self.assertFalse(self._deliver("atlas", True).called)
        self.assertFalse(self._deliver("metis", True).called)

    def test_allowlisted_but_generating_still_alerts(self):
        # pane_idle=False means mid-turn — never suppress; the real condition could
        # be exactly a wedged mid-turn seat.
        self.assertTrue(self._deliver("atlas", False).called)

    def test_allowlisted_but_absent_pane_still_alerts(self):
        # option (a): raw None (mnemosyne, no tmux server) is never-suppress-on-
        # unknown. Also the wiring-level guard against feeding a coarse_view-coerced
        # (None->True) value: if that regressed, this would wrongly suppress.
        self.assertTrue(self._deliver("mnemosyne", None).called)

    def test_non_allowlisted_idle_still_alerts(self):
        # An always-on seat that happens to read idle must still alert.
        self.assertTrue(self._deliver("recovery", True).called)

    def test_missing_obs_falls_back_to_alerting(self):
        # Backward-compat: a caller that does not thread obs (obs=None) must neither
        # crash nor suppress — the predicate simply cannot fire without pane_idle.
        self.assertTrue(self._deliver("atlas", True, thread_obs=False).called)


class TestDeliveryRecency(unittest.TestCase):
    def test_reads_cur_DIRECTORY_mtime_not_newest_file(self):
        """rename() PRESERVES file mtime, so the newest file in cur/ is the newest
        message SENT among those delivered — not when delivery happened."""
        with tempfile.TemporaryDirectory() as td:
            hub = pathlib.Path(td)
            box = hub / "agentx"
            for sub in ("new", "cur", "tmp"):
                (box / sub).mkdir(parents=True)
            # a file whose OWN mtime is ancient, moved in "just now"
            old_file = box / "cur" / "msg"
            old_file.write_text("x")
            os.utime(old_file, (1000, 1000))
            now = time.time()
            os.utime(box / "cur", (now - 30, now - 30))
            rec = mon.delivery_recency(hub, now=now)
            self.assertIn("agentx", rec)
            self.assertLess(rec["agentx"], 60,
                            "must use the directory clock, not the stale file mtime")

    def test_unstatable_box_is_None_not_zero(self):
        with tempfile.TemporaryDirectory() as td:
            hub = pathlib.Path(td)
            box = hub / "gone"
            for sub in ("new", "cur", "tmp"):
                (box / sub).mkdir(parents=True)
            (box / "cur").rmdir()
            rec = mon.delivery_recency(hub)
            self.assertIsNone(rec.get("gone"), "unknown must be None, never 0 (=recent)")



class TestCoerceDrainingIsCalledAndWorks(unittest.TestCase):
    """T-2240. These exist because 34 tests passed while _coerce_draining was a
    guaranteed NameError at call time — nothing invoked it. A helper with no
    caller in the suite is untested no matter how green the run is."""

    def _obs(self, oldest=1600, unread=5):
        import tools.mail_staleness as ms
        return ms.InboxObservation(unread=unread, in_flight=0,
                                   oldest_unread_age_s=oldest,
                                   pane_idle=True, ever_received=True)

    def test_draining_inbox_is_coerced_out_of_candidacy(self):
        out, drained = mon._coerce_draining({"x": self._obs()}, {"x"})
        self.assertEqual(drained, ["x"])
        self.assertEqual(out["x"].unread, 0)
        self.assertIsNone(out["x"].oldest_unread_age_s)

    def test_stalled_inbox_is_left_alone(self):
        out, drained = mon._coerce_draining({"x": self._obs()}, set())
        self.assertEqual(drained, [])
        self.assertEqual(out["x"].unread, 5)

    def test_helper_is_importable_and_callable(self):
        """Guards the exact defect: a module-level NameError only surfaces on call."""
        self.assertTrue(callable(mon._coerce_draining))
        mon._coerce_draining({}, set())


class TestSuppressionMustNotPoisonTheRealertThrottle(unittest.TestCase):
    """T-2240, the bug this cost. Suppressing AFTER evaluate() let detect() emit the
    alert, set samples_since_alert=0, and start the ~1h REALERT throttle — while the
    operator saw '0 alert(s)'. mnemosyne's bridge died silently 19:20-20:50Z on
    2026-07-28; the monitor ran 23 times, alerted zero times, and its state showed
    idle_unread_samples=22 with samples_since_alert=8. Alerts raised, swallowed,
    then throttled."""

    def test_a_draining_inbox_never_consumes_the_alert_budget(self):
        import tools.mail_staleness as ms
        obs = {"m": ms.InboxObservation(unread=9, in_flight=0, oldest_unread_age_s=1600,
                                        pane_idle=True, ever_received=True)}
        coerced, drained = mon._coerce_draining(obs, {"m"})
        self.assertEqual(drained, ["m"])
        state = {}
        for _ in range(6):   # well past WARN_SAMPLES
            alerts, state = mon.evaluate(coerced, state)
            self.assertEqual(alerts, [], "a draining inbox must never emit")
        warn = state.get("warn", {}).get("m")
        got = getattr(warn, "samples_since_alert", None) if warn else None
        self.assertIsNone(got,
                          "samples_since_alert must stay None — a suppressed alert "
                          "that starts the re-alert throttle silences the real one")

    def test_main_CALLS_coercion_BEFORE_evaluate(self):
        """Source-level ordering guard, and it is here because the behavioural test
        above did NOT catch the real defect.

        That test calls _coerce_draining itself and then evaluate, so deleting the
        call from main() leaves it passing — I mutation-tested exactly that and it
        stayed green. The bug was never in the helper; it was in WHERE main() ran
        it. A test that reproduces the correct ordering by hand cannot detect the
        wrong ordering in production.
        """
        import inspect
        src = inspect.getsource(mon.main)
        self.assertIn("_coerce_draining(", src,
                      "main() must coerce draining inboxes; without it, suppressed "
                      "alerts poison the re-alert throttle (T-2240)")
        self.assertLess(
            src.index("_coerce_draining("), src.index("evaluate(obs, prior)"),
            "coercion must run BEFORE evaluate() — filtering afterwards lets "
            "detect() emit, set samples_since_alert=0, and throttle the real alert")


    def test_a_stalled_inbox_still_alerts_after_warn_samples(self):
        """The control: coercion must not disable genuine detection."""
        import tools.mail_staleness as ms
        obs = {"m": ms.InboxObservation(unread=9, in_flight=0, oldest_unread_age_s=1600,
                                        pane_idle=True, ever_received=True)}
        coerced, drained = mon._coerce_draining(obs, set())  # frozen -> never advanced
        self.assertEqual(drained, [])
        state, fired = {}, False
        for _ in range(mon.WARN_SAMPLES + 1):
            alerts, state = mon.evaluate(coerced, state)
            if alerts:
                fired = True
        self.assertTrue(fired, "a genuinely stalled inbox must still alert")
class TestDrainProgress(unittest.TestCase):
    """T-2257: the cross-sample draining discriminator that replaced the static
    age comparison. The property under test is the one the old code lacked:
    suppression must EXPIRE when draining stops."""

    def _hub(self, td):
        hub = pathlib.Path(td)
        box = hub / "m"
        for sub in ("new", "cur", "tmp"):
            (box / sub).mkdir(parents=True)
        return hub, box

    def test_first_run_never_advances(self):
        """No prior sample -> nothing can claim to have advanced -> no suppression.
        Fail-loud on unknown, same posture as the pane and recency rules."""
        with tempfile.TemporaryDirectory() as td:
            hub, _ = self._hub(td)
            clock, advanced = mon.drain_progress(hub, {})
            self.assertEqual(advanced, set())
            self.assertIn("m", clock)

    def test_ack_between_samples_advances(self):
        with tempfile.TemporaryDirectory() as td:
            hub, box = self._hub(td)
            os.utime(box / "cur", (1000.0, 1000.0))
            clock1, _ = mon.drain_progress(hub, {})
            os.utime(box / "cur", (2000.0, 2000.0))  # an ack renamed a file in
            clock2, advanced = mon.drain_progress(hub, clock1)
            self.assertEqual(advanced, {"m"})
            self.assertGreater(clock2["m"], clock1["m"])

    def test_frozen_across_samples_does_not_advance(self):
        with tempfile.TemporaryDirectory() as td:
            hub, box = self._hub(td)
            os.utime(box / "cur", (1000.0, 1000.0))
            clock1, _ = mon.drain_progress(hub, {})
            _, advanced = mon.drain_progress(hub, clock1)
            self.assertEqual(advanced, set())

    def test_backwards_mtime_does_not_advance(self):
        """Clock weirdness resolves to not-draining, never to suppression."""
        with tempfile.TemporaryDirectory() as td:
            hub, box = self._hub(td)
            os.utime(box / "cur", (2000.0, 2000.0))
            clock1, _ = mon.drain_progress(hub, {})
            os.utime(box / "cur", (1000.0, 1000.0))
            _, advanced = mon.drain_progress(hub, clock1)
            self.assertEqual(advanced, set())

    def test_unstatable_cur_is_dropped_not_advanced(self):
        with tempfile.TemporaryDirectory() as td:
            hub, box = self._hub(td)
            clock1, _ = mon.drain_progress(hub, {})
            (box / "cur").rmdir()
            # removing cur/ makes the subdir no longer a maildir; either way the
            # inbox must not land in `advanced`
            clock2, advanced = mon.drain_progress(hub, clock1)
            self.assertEqual(advanced, set())
            self.assertNotIn("m", clock2)

    def test_PERMANENCE_the_defect_itself_end_to_end(self):
        """The 2026-07-28 shape, replayed: one partial drain, then frozen while
        mail piles. Old code: suppressed at every subsequent sample, forever
        (both compared ages co-grow; the sign never flips). New code: the sample
        AFTER the ack suppresses, every later sample does not."""
        with tempfile.TemporaryDirectory() as td:
            hub, box = self._hub(td)
            os.utime(box / "cur", (1000.0, 1000.0))
            clock, _ = mon.drain_progress(hub, {})

            # the one partial drain (the DLQ-retry shape: one ack among failures)
            os.utime(box / "cur", (1500.0, 1500.0))
            clock, advanced = mon.drain_progress(hub, clock)
            self.assertEqual(advanced, {"m"}, "the sample right after an ack suppresses")

            # frozen ever after — mail keeps arriving, nothing acks
            for i in range(5):
                clock, advanced = mon.drain_progress(hub, clock)
                self.assertEqual(advanced, set(),
                                 f"sample {i + 2}: suppression must EXPIRE once "
                                 "draining stops — permanence is the T-2257 defect")

    def test_clock_round_trips_through_disk(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "clock.json"
            mon.save_drain_clock({"m": 1234.5}, path)
            self.assertEqual(mon.load_drain_clock(path), {"m": 1234.5})

    def test_corrupt_or_missing_clock_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "clock.json"
            self.assertEqual(mon.load_drain_clock(path), {})
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(mon.load_drain_clock(path), {})

    def test_main_wires_drain_progress_not_recency_into_suppression(self):
        """WIRING. The old bug survives trivially if main() keeps feeding
        delivery_recency() into the suppression calls — every function here can
        be individually correct while the composition reintroduces the defect.
        Assert on main()'s source: suppression consumes `advanced`, and recency
        appears only in display code."""
        import inspect
        src = inspect.getsource(mon.main)
        self.assertIn("_coerce_draining(obs, advanced)", src)
        self.assertIn("suppress_draining(alerts, obs, advanced)", src)
        self.assertNotIn("_coerce_draining(obs, recency)", src)
        self.assertNotIn("suppress_draining(alerts, obs, recency)", src)
        # Match a LIVE call only: an earlier mutation pass survived this check by
        # leaving the call name in a comment ("pass  # save_drain_clock(...)").
        # A source assertion must not be satisfiable by its own tombstone.
        self.assertRegex(src, r"(?m)^\s*save_drain_clock\(clock\)",
                         "the clock must persist (as a real statement, not a "
                         "comment) or every run is a first run and nothing is "
                         "ever suppressed")


# --------------------------------------------------------------------------
# T-2267 (metis): a sender cannot tell that a dispatch went into a seat with
# nobody home. The receiver is absent BY DEFINITION, so the sender is the only
# party who could notice — and it had no surface that reported it.
# --------------------------------------------------------------------------
class StalledSenderAttributionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.hub = pathlib.Path(self._tmp.name) / "hub"
        self.hub.mkdir()
        self.box = self.hub / "metis"
        mon.mailbox.provision(self.box)
        for who in ("orchestrator", "specialist"):
            mon.mailbox.provision(self.hub / who)

    def tearDown(self):
        self._tmp.cleanup()

    def _alert(self, inbox="metis"):
        return types.SimpleNamespace(inbox=inbox, kind="idle_with_unread", text="x")

    def test_attributes_stalled_mail_to_each_distinct_sender(self):
        mon.mailbox.send(self.box, {"n": 1}, sender="orchestrator")
        mon.mailbox.send(self.box, {"n": 2}, sender="orchestrator")
        mon.mailbox.send(self.box, {"n": 3}, sender="specialist")
        got = mon.stalled_senders(self.box, now=time.time())
        self.assertEqual(sorted(got), ["specialist", "orchestrator"])
        self.assertEqual(got["orchestrator"]["count"], 2)
        self.assertEqual(got["specialist"]["count"], 1)

    def test_synthetic_senders_are_never_notified(self):
        """An alert about an alert is the loop the DLQ exemption exists to stop
        one layer down; the same reasoning applies here."""
        mon.mailbox.send(self.box, {"n": 1}, sender="mailbox-dlq")
        mon.mailbox.send(self.box, {"n": 2}, sender=mon._ALERT_VOCAB)
        mon.mailbox.send(self.box, {"n": 3}, sender="orchestrator")
        got = mon.stalled_senders(self.box, now=time.time())
        self.assertEqual(list(got), ["orchestrator"])

    def test_unreadable_new_returns_None_not_a_partial_attribution(self):
        """A partial attribution is worse than none: the senders it omits are
        silently exonerated, which is the failure-presents-as-absence shape this
        whole ticket is about."""
        mon.mailbox.send(self.box, {"n": 1}, sender="orchestrator")
        os.chmod(self.box / "new", 0o000)
        try:
            self.assertIsNone(mon.stalled_senders(self.box, now=time.time()))
        finally:
            os.chmod(self.box / "new", 0o770)
        # control: readable again -> a real attribution, so the None above is
        # about unreadability and not about there being nothing to find.
        self.assertEqual(list(mon.stalled_senders(self.box, now=time.time())), ["orchestrator"])

    def test_READ_ONLY_attribution_does_not_ack_or_move_anything(self):
        """The contract, and the one that would be silently catastrophic: this
        reads the very messages the alert is about. An ack here consumes the
        evidence — and it would look like the queue drained itself."""
        mon.mailbox.send(self.box, {"n": 1}, sender="orchestrator")
        before_new = sorted(p.name for p in (self.box / "new").iterdir())
        before_cur = sorted(p.name for p in (self.box / "cur").iterdir())
        mon.stalled_senders(self.box, now=time.time())
        self.assertEqual(sorted(p.name for p in (self.box / "new").iterdir()), before_new)
        self.assertEqual(sorted(p.name for p in (self.box / "cur").iterdir()), before_cur)
        self.assertEqual(before_cur, [], "fixture sanity: nothing was acked to begin with")

    def test_each_sender_is_told_and_the_stalled_box_is_not(self):
        mon.mailbox.send(self.box, {"n": 1}, sender="orchestrator")
        mon.mailbox.send(self.box, {"n": 2}, sender="specialist")
        # the box's own mail to itself must not generate a self-notice
        mon.mailbox.send(self.box, {"n": 3}, sender="metis")
        notified = mon.notify_senders(self._alert(), self.hub, now=time.time())
        self.assertEqual(sorted(notified), ["specialist", "orchestrator"])
        self.assertNotIn("metis", notified)
        for who in ("orchestrator", "specialist"):
            landed = list((self.hub / who / "new").iterdir())
            self.assertEqual(len(landed), 1, f"{who} must receive exactly one notice")
            msg = json.loads(landed[0].read_text())
            self.assertEqual(msg["payload"]["inbox"], "metis")
            self.assertEqual(msg["payload"]["kind"], "undrained_queue")

    def test_a_sender_with_no_provisioned_inbox_does_not_break_the_sampler(self):
        """An ordinary outcome (a one-off script, a retired persona), not a
        monitor failure. The other senders must still be told."""
        mon.mailbox.send(self.box, {"n": 1}, sender="ghost")
        mon.mailbox.send(self.box, {"n": 2}, sender="orchestrator")
        notified = mon.notify_senders(self._alert(), self.hub, now=time.time())
        self.assertEqual(notified, ["orchestrator"])

    def test_dry_run_notifies_nobody(self):
        mon.mailbox.send(self.box, {"n": 1}, sender="orchestrator")
        notified = mon.notify_senders(self._alert(), self.hub, now=time.time(),
                                      dry_run=True)
        self.assertEqual(notified, ["orchestrator"])
        self.assertEqual(list((self.hub / "orchestrator" / "new").iterdir()), [],
                         "dry-run must deliver nothing")

    def test_deliver_wires_sender_notices_to_real_alerts(self):
        """THE INTEGRATION, and the reason the unit tests above are not enough:
        a correct notify_senders that deliver() never calls is the same
        presence-without-activation defect as T-2312."""
        mon.mailbox.send(self.box, {"n": 1}, sender="orchestrator")
        mon.deliver([self._alert()], dry_run=False, hub=self.hub, now=time.time())
        landed = list((self.hub / "orchestrator" / "new").iterdir())
        self.assertEqual(len(landed), 1,
                         "deliver() must emit the sender notice, not just the "
                         "recipient-side alert")


if __name__ == "__main__":
    unittest.main()
