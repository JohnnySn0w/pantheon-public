#!/usr/bin/env python3
"""Tests for the mailbox SYSTEM — the composition, not the library (T-2245).

WHY THIS FILE EXISTS, in one measurement: 4266 unit tests were green for the whole
of mnemosyne's 90-minute delivery outage on 2026-07-28, and the monitor built that
afternoon to catch exactly that ran 23 times and alerted ZERO times. Both halves are
covered at the unit level. `tools/test_mail_staleness_monitor.py` is thorough — 47
tests — and asserts alert COUNTS on hand-built `InboxObservation` objects. What has
never been exercised is the composition:

    observe()  ->  evaluate()  ->  deliver()

against a real fixture maildir, with the assertion made AT THE ALERT SINK. That is
the stratum the outage lived in. `detect()` returning something is not the property
anyone cares about; somebody being TOLD is. Twice now the monitor has returned the
right answer into a swallowing path (T-2240 suppression-after-evaluate poisoning the
re-alert throttle; T-2257 a suppression predicate comparing two co-growing ages, so
its sign could never flip and suppression was permanent).

THE HARD CONSTRAINT, and the reason the flagship test is shaped the way it is: the
outage SELF-RECOVERED, and self-recovery is what made it invisible. A suite that only
asserts eventual recovery passes straight through 90 minutes of dead mail. So the
assertion here is not "mail eventually moves" — it is "a gap was SURFACED while it
was open, and self-recovery does not retroactively erase that".

WHAT IS DELIBERATELY NOT CLAIMED. Known gaps are carried as
`unittest.expectedFailure` markers rather than as red tests, skips, or green lints.
A red test forever trains everyone to ignore red. A skip is decoration that rots
silently. An expected failure documents the gap, stays green-as-expected while the
gap is real, and FLIPS TO UNEXPECTED SUCCESS — a FAILING run — the moment any
surfacing path lands, breaking the suite until someone promotes it to a real
assertion. It cannot go stale in either direction.

  - `test_observation_distinguishes_unacked_from_undelivered` — the ack-path
    confound. STILL A MARKER; nothing has given mailbox.py an in-progress marker.
  - `test_bridge_unit_declares_a_surfacing_path` — the missing OnFailure= line.
    NO LONGER A MARKER. It was one for about two hours, then T-2264 landed at
    bc004378f, the marker flipped to an unexpected success and failed the run
    unprompted, and it was promoted to a real assertion. The mechanism paid for
    itself the same night it was introduced; the history is kept in that test's
    docstring because it is the most useful thing in this file for the next
    person deciding between a marker and a red test.

`unittest.expectedFailure` ONLY, never `pytest.mark.xfail`. Verified on this box:
expectedFailure fails the run (exit 1) on unexpected success under BOTH
`python3 -m unittest` and `python3 -m pytest`; `pytest.mark.xfail` defaults to
strict=False, where an XPASS does NOT fail the suite, and there is no pytest ini here
setting `xfail_strict`. An xfail marker would be the same silent-green disease this
file exists to kill.

FIXTURES ARE DISPOSABLE TMPDIRS, ALWAYS. Nothing here reads, writes, drains, or
stats anything under /home/the-ancestor/. Every hub path is a `TemporaryDirectory`
created by the test itself; the monitor's real hub default is never used, because
every entry point takes an explicit hub and every one of them is given one.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import tools.mail_staleness_monitor as mon  # noqa: E402
import tools.mailbox as mailbox  # noqa: E402
from tools._lease import Lease  # noqa: E402


def _provision(hub: pathlib.Path, name: str) -> pathlib.Path:
    """A real maildir under a DISPOSABLE hub, created by the real provisioner."""
    box = hub / name
    mailbox.provision(box, mode=0o2770)
    return box


class AlertSinkCompositionTest(unittest.TestCase):
    """ITEM 1 — the flagship. observe() -> evaluate() -> deliver(), end to end,
    against a real fixture maildir, asserted AT THE SINK.

    The composition is driven through `mon.main(["--hub", ...])` rather than by
    calling the three stages by hand. That is deliberate and it is the lesson
    `test_main_CALLS_coercion_BEFORE_evaluate` already paid for: a test that
    reproduces the correct ordering itself cannot detect the wrong ordering in
    production. main() is where observe/drain_progress/_coerce_draining/evaluate/
    suppress_draining/deliver are actually wired together, so main() is what gets
    driven.

    Two seams are substituted, both because the real thing would leave this
    machine:
      - `_persona_panes` -> {} : pane capture for a persona name shells out via
        `sudo -u <account> tmux capture-pane`. An empty map means every fixture
        inbox is pane-UNKNOWN by construction (capture_pane_idle returns None for
        an inbox with no pane entry), so nothing is executed and no other account
        is touched. Unknown-pane inboxes are covered at the COARSE tier, which is
        the real 12-sample threshold — not lowered for the test.
      - `inbox_consumer.route_failure_alert` -> a recording sink. This is THE
        assertion surface. deliver() imports it inside the function body, so the
        patch is what deliver() actually resolves at call time.

    State and drain-clock persistence are redirected to the tmpdir but keep the
    REAL serialization code in the loop (the module's own load/save functions,
    bound to a temp path), because "the clock persists across samples" is part of
    what makes suppression expire.
    """

    OUTAGE_AGE_S = 90 * 60  # the T-2242 window, replayed

    def setUp(self):
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.hub = pathlib.Path(self._td.name) / "hub"
        self.hub.mkdir()
        state_dir = pathlib.Path(self._td.name) / "state"
        state_dir.mkdir()
        self.state_path = state_dir / "monitor.json"
        self.clock_path = state_dir / "drain_clock.json"

        # Names chosen so that even WITHOUT the _persona_panes patch nothing could
        # shell out to another account. Belt and braces on the never-touch-a-real-
        # seat constraint; the patch is the actual mechanism.
        self.frozen = "fixture-frozen"
        self.draining = "fixture-draining"
        import mailbox_wake as mw
        for name in (self.frozen, self.draining):
            self.assertNotIn(name, mw.AGENT_PANE,
                             "fixture inbox names must not collide with real personas")

        self.frozen_box = _provision(self.hub, self.frozen)
        self.draining_box = _provision(self.hub, self.draining)

        self.sink: list[tuple[str, str, str]] = []
        self._stderr = io.StringIO()

        real_load_state = mon.load_state
        real_save_state = mon.save_state
        real_load_clock = mon.load_drain_clock
        real_save_clock = mon.save_drain_clock
        sp, cp = self.state_path, self.clock_path
        for target, repl in (
            ("load_state", lambda path=sp: real_load_state(path)),
            ("save_state", lambda st, path=sp: real_save_state(st, path)),
            ("load_drain_clock", lambda path=cp: real_load_clock(path)),
            ("save_drain_clock", lambda c, path=cp: real_save_clock(c, path)),
            ("_persona_panes", lambda: {}),
        ):
            p = mock.patch.object(mon, target, repl)
            p.start()
            self.addCleanup(p.stop)

        import inbox_consumer
        p = mock.patch.object(
            inbox_consumer, "route_failure_alert",
            lambda agent, msg, vocab: self.sink.append((agent, msg, vocab)))
        p.start()
        self.addCleanup(p.stop)

    # ------------------------------------------------------------------ helpers
    def _freeze_a_backlog(self, count: int = 3, sender: str = "test-harness") -> None:
        """A real backlog delivered by the real producer, aged into the outage.

        Messages go in through `mailbox.send()` — staged in tmp/, fsynced, renamed
        into new/ — not hand-placed, so the fixture is what production produces.
        Their mtimes are backdated so the alert text carries a real outage age;
        cur/ is never touched, which is what "nobody is draining this" looks like
        on disk.
        """
        for i in range(count):
            mailbox.send(self.frozen_box, {"body": f"stuck message {i}"},
                         sender=sender, subject=f"frozen-{i}")
        old = time.time() - self.OUTAGE_AGE_S
        for p in (self.frozen_box / "new").iterdir():
            os.utime(p, (old, old))

    def _drain_one_round(self, box: pathlib.Path) -> None:
        """One round of a genuinely healthy-but-busy consumer, via the real path.

        Send, then really `consume()` (which acks by rename into cur/, advancing
        cur/'s directory mtime — the T-2257 discriminator), then send again so the
        inbox still has a live backlog when it is observed. This is the noise case
        suppression exists for: an inbox with unread mail that is nonetheless
        moving.
        """
        mailbox.send(box, {"body": "work"}, sender="test-harness", subject="w")
        res = mailbox.consume(box, lambda msg: None)
        self.assertFalse(res["skipped"], "fixture drain must not be skipped")
        self.assertTrue(res["processed"], "fixture drain must actually ack something")
        mailbox.send(box, {"body": "next"}, sender="test-harness", subject="n")

    def _sample(self) -> None:
        """One monitor run — the whole real pipeline, including delivery."""
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(self._stderr):
            rc = mon.main(["--hub", str(self.hub)])
        self.assertEqual(rc, 0)

    def _sink_lines_for(self, inbox: str) -> list[str]:
        return [msg for _agent, msg, _vocab in self.sink if f" {inbox} " in msg]

    def _assert_sink_is_real(self) -> None:
        """deliver() swallows delivery exceptions and prints to stderr. A silent
        sink and a broken sink look identical from the test's side unless this is
        checked, which is the same failure-presents-as-absence shape the whole
        ticket is about."""
        err = self._stderr.getvalue()
        self.assertNotIn("delivery failed", err,
                         f"the alert sink was never actually reached: {err!r}")
        self.assertNotIn("coverage-alert delivery failed", err)
        self.assertNotIn("MONITOR COVERAGE INCOMPLETE",
                         "".join(m for _a, m, _v in self.sink),
                         "fixture hub did not fully enumerate — the run is not a "
                         "clean all-clear and no absence assertion below is valid")

    # -------------------------------------------------------------------- tests
    def test_a_frozen_inbox_puts_an_alert_INTO_THE_SINK(self):
        """THE outage, replayed: mail in new/, cur/ never advancing, nobody home.

        The assertion is not that detect()/evaluate() returned something. It is
        that `route_failure_alert` — the real delivery call, resolved by deliver()
        at call time — was HANDED an alert naming the frozen inbox.
        """
        self._freeze_a_backlog()
        for _ in range(mon.COARSE_SAMPLES):
            self._sample()

        self._assert_sink_is_real()
        lines = self._sink_lines_for(self.frozen)
        self.assertTrue(lines,
                        "a frozen inbox produced NO alert at the sink — this is the "
                        "90-minute outage the suite exists to catch")
        self.assertTrue(all(v == "mail_staleness" for _a, _m, v in self.sink))
        self.assertIn("unread", lines[0])

    def test_a_draining_inbox_stays_SILENT_at_the_sink(self):
        """The converse, and it matters as much: a monitor that fires on healthy
        gets switched off, after which the real outage goes unnoticed too. This
        inbox always has unread mail — it is just also acking it, every sample."""
        for _ in range(mon.COARSE_SAMPLES + 2):
            self._drain_one_round(self.draining_box)
            self._sample()

        self._assert_sink_is_real()
        self.assertEqual(self._sink_lines_for(self.draining), [],
                         "a demonstrably draining inbox must never alert")

    def test_both_directions_in_one_run(self):
        """Frozen and draining side by side under one monitor, one hub, one state
        file. Discrimination between them is the whole product."""
        self._freeze_a_backlog()
        for _ in range(mon.COARSE_SAMPLES):
            self._drain_one_round(self.draining_box)
            self._sample()

        self._assert_sink_is_real()
        self.assertTrue(self._sink_lines_for(self.frozen),
                        "the frozen inbox must alert")
        self.assertEqual(self._sink_lines_for(self.draining), [],
                         "the draining inbox must not alert")

    def test_the_SENDER_is_told_by_the_real_chain_not_a_hand_built_alert(self):
        """T-2332 — the one seam the unit tests meet at instead of crossing.

        recovery's T-2267 landed sender attribution and tested it well, including
        `test_deliver_wires_sender_notices_to_real_alerts`, whose docstring names
        the obvious hazard ("a correct notify_senders that deliver() never calls
        is the same presence-without-activation defect"). But that test calls
        `mon.deliver([self._alert()], ...)` — the alert is CONSTRUCTED BY THE
        TEST. So the chain is proven in two halves that meet at a hand-built
        object, and nothing asserts that an alert produced by the REAL evaluate(),
        from a REAL frozen maildir seen by the REAL observe(), carries the fields
        notify_senders reads.

        That join is where this class of defect has actually lived, twice: the
        staleness suite's 47 tests asserted alert counts against hand-built
        InboxObservation objects, and a 90-minute outage went through the seam.
        This drives mon.main() and asserts at the SECOND sink — the sender's own
        maildir — so both halves are crossed rather than met.

        NOT asserted here, and it must not be inverted into a health signal
        (recovery's own residual): notices fire only for inboxes the detector already
        alerts on, so silence toward a sender is NOT evidence of a healthy
        recipient. This asserts the positive path only.
        """
        sender = "fixture-sender"
        sender_box = _provision(self.hub, sender)
        self.assertEqual(list((sender_box / "new").iterdir()), [],
                         "fixture precondition: the sender's box starts empty")

        self._freeze_a_backlog(count=3, sender=sender)
        for _ in range(mon.COARSE_SAMPLES):
            self._sample()

        self._assert_sink_is_real()
        self.assertTrue(self._sink_lines_for(self.frozen),
                        "precondition: the recipient-side alert must have fired, "
                        "or this test proves nothing about the sender path")

        notices = list((sender_box / "new").iterdir())
        self.assertEqual(len(notices), 1,
                         "the sender of the stuck mail must be told, by the same "
                         "run that alerted the recipient — one notice, from the "
                         "real chain, not a hand-built alert")

        payload = json.loads(notices[0].read_text(encoding="utf-8"))["payload"]
        self.assertEqual(payload.get("kind"), "undrained_queue")
        self.assertEqual(payload.get("inbox"), self.frozen,
                         "the notice must name the inbox that is not draining")
        self.assertEqual(payload.get("count"), 3,
                         "count must match what the fixture actually staged — a "
                         "notice with the wrong count is a notice built from "
                         "something other than the observation")
        self.assertAlmostEqual(payload.get("oldest_age_s"), self.OUTAGE_AGE_S,
                               delta=120,
                               msg="oldest_age_s must carry the REAL backdated age "
                                   "from the maildir, which is the field that can "
                                   "only come through observe()")

    def test_sender_attribution_does_not_ACK_the_mail_it_reads(self):
        """Read-only, preserved across the FULL chain rather than at unit level.

        Attribution opens the very messages the alert is about. If it ever acked
        them, the detector would drain the queue it exists to report on — and the
        inbox would look healthy at the next sample BECAUSE it was inspected.
        recovery proved this at unit level by injecting an ack into the reader; this
        asserts the property survives where the reader actually runs.
        """
        sender = "fixture-sender"
        _provision(self.hub, sender)
        self._freeze_a_backlog(count=3, sender=sender)
        before = sorted(p.name for p in (self.frozen_box / "new").iterdir())

        for _ in range(mon.COARSE_SAMPLES):
            self._sample()

        after = sorted(p.name for p in (self.frozen_box / "new").iterdir())
        self.assertEqual(after, before,
                         "the stalled mail must still be in new/ — attribution "
                         "reads it and must never move it")
        self.assertEqual(list((self.frozen_box / "cur").iterdir()), [],
                         "nothing may be acked into cur/ by observation")

    def test_SELF_RECOVERY_does_not_erase_the_surfaced_gap(self):
        """THE HARDEST CONSTRAINT OF THE ARC, stated as an assertion.

        mnemosyne's bridge died at ~19:20Z and came back by itself at 20:50:14Z.
        Because it self-recovered, every after-the-fact observation of the system
        was healthy, and a suite asserting eventual recovery would have passed
        straight through the ninety minutes. It was surfaced only because the operator
        asked.

        So: run the outage, then let the inbox recover through the real consume
        path, then keep sampling until the monitor reports it clean. Assert the
        gap was surfaced WHILE IT WAS OPEN and that the alert is still on the
        record afterwards. Recovery is asserted too — but recovery alone is
        explicitly not sufficient, and this test fails if the only thing that
        happened was recovery.
        """
        self._freeze_a_backlog()
        for _ in range(mon.COARSE_SAMPLES):
            self._sample()
        alerts_during_the_gap = list(self._sink_lines_for(self.frozen))
        self.assertTrue(
            alerts_during_the_gap,
            "the gap closed itself and NOTHING was ever surfaced — this is exactly "
            "the 2026-07-28 outage and the suite must fail here")

        # --- self-recovery: the consumer comes back and drains the backlog ---
        res = mailbox.consume(self.frozen_box, lambda msg: None)
        self.assertEqual(len(res["processed"]), 3, "recovery must really drain")
        self.assertGreaterEqual(len(list((self.frozen_box / "cur").iterdir())), 3,
                                "the aged backlog must really be acked into cur/")

        # PAST the re-alert throttle deliberately. With only a few post-recovery
        # samples the "stops alerting" half of the assertion below would pass off
        # REALERT_AFTER_SAMPLES rather than off recovery — true for the wrong
        # reason, which is the vacuity this whole file is aimed at. Sampling
        # beyond the throttle means the only thing that can keep the sink quiet
        # is the inbox genuinely draining.
        for _ in range(mon.REALERT_AFTER_SAMPLES + 1):
            self._drain_one_round(self.frozen_box)
            self._sample()

        self._assert_sink_is_real()
        self.assertEqual(self._sink_lines_for(self.frozen), alerts_during_the_gap,
                         "recovery must neither erase the gap alert nor keep "
                         "alerting once the queue is genuinely moving again")


class LeaseLivenessDiscriminationTest(unittest.TestCase):
    """ITEM 2 — dead consumer vs slow consumer, via the ONLY surface that carries
    liveness.

    From the maildir these two are the same picture: mail in new/, cur/ not
    advancing, no filesystem trace either way (see the expected-failure marker
    below for why that is structural, not an oversight). The consumer lease is the
    one place a PID is recorded, so it is the one place the question "is anybody
    still working this queue?" has an answer.

    The demonstration is deliberately built so the holder RECORD is identical in
    both verdicts — the same dict object is judged twice. Only the world changes
    between the two assertions. That makes it impossible for the test to pass by
    reading something out of the record itself.
    """

    HOLDER = (
        "import sys, time\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from _lease import Lease\n"
        "lease = Lease(sys.argv[2], ttl_secs=7200, label='slow-consumer')\n"
        "assert lease.acquire(), 'holder failed to acquire'\n"
        "time.sleep(600)\n"
    )

    def setUp(self):
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.hub = pathlib.Path(self._td.name) / "hub"
        self.hub.mkdir()
        self.box = _provision(self.hub, "fixture-leased")
        self.lock = self.box / ".consumer.lock"

    def _start_live_holder(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", self.HOLDER, str(TOOLS_DIR), str(self.lock)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self._kill, proc)
        probe = Lease(self.lock)
        deadline = time.time() + 15
        while time.time() < deadline:
            rec = probe.read_record()
            if rec and rec.get("pid") == proc.pid:
                return proc
            if proc.poll() is not None:
                self.fail(f"holder died at startup: {proc.stderr.read()!r}")
            time.sleep(0.05)
        self.fail("holder never recorded itself on the lease within 15s")

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        if proc.stderr is not None and not proc.stderr.closed:
            proc.stderr.close()

    def test_lease_distinguishes_a_DEAD_consumer_from_a_SLOW_one(self):
        holder = self._start_live_holder()
        probe = Lease(self.lock, ttl_secs=900)
        record = probe.read_record()
        self.assertIsNotNone(record)
        self.assertEqual(record["pid"], holder.pid)

        # --- SLOW: the pid is alive, so the holder is not stale and is not reaped.
        now = int(time.time())
        self.assertTrue(Lease._pid_alive(record["pid"]))
        self.assertFalse(probe._holder_is_stale(record, now),
                         "a live holder inside its own TTL is SLOW, not dead")
        self.assertFalse(probe.acquire(reap_stale=True),
                         "a slow consumer must be backed off from, never reaped — "
                         "reaping it is how a healthy long drain becomes an outage")
        self.assertFalse(probe.held())

        # --- DEAD: same record, same TTL, same clock. Only the process is gone.
        self._kill(holder)
        deadline = time.time() + 10
        while Lease._pid_alive(record["pid"]) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(Lease._pid_alive(record["pid"]))
        self.assertTrue(probe._holder_is_stale(record, now),
                        "a holder whose pid is gone is DEAD and must be reapable — "
                        "otherwise its lease wedges the inbox forever")

        # And the consequence that actually matters: delivery can resume.
        takeover = Lease(self.lock, ttl_secs=900, label="successor")
        self.assertTrue(takeover.acquire(),
                        "a dead holder's lease must be takeable or mail never moves again")
        takeover.release()

    def test_the_verdict_comes_from_the_LIVENESS_PROBE_not_the_record(self):
        """Same bytes, two verdicts. If this test ever passes by reading a field
        out of the record, the discrimination has silently moved to a surface that
        cannot see a process die."""
        holder = self._start_live_holder()
        probe = Lease(self.lock, ttl_secs=900)
        record = probe.read_record()
        on_disk_while_alive = self.lock.read_bytes()
        alive_verdict = probe._holder_is_stale(record, int(time.time()))

        self._kill(holder)
        deadline = time.time() + 10
        while Lease._pid_alive(record["pid"]) and time.time() < deadline:
            time.sleep(0.05)
        dead_verdict = probe._holder_is_stale(record, int(time.time()))

        self.assertEqual(on_disk_while_alive, self.lock.read_bytes(),
                         "the lockfile must be byte-identical across the two verdicts")
        self.assertNotEqual(alive_verdict, dead_verdict,
                            "dead and slow must be distinguishable")


class MidTurnLegibilityTest(unittest.TestCase):
    """CASE 2 — single-flight delivery behind a long turn, asserted as LEGIBILITY
    and deliberately NOT as a latency bound.

    The fault: delivery is single-flight, so a 25-minute turn is a 25-minute mail
    delay for every sender queued behind it. Recovery's T-2242 note has the sharp
    version — queue DEPTH alone cannot separate "18 unread and draining" from
    "18 unread and dead", because both are the number 18. Oldest-message-age in
    new/ is the observable that can, and it is the one this asserts.

    WHY NOT A BOUND, and this is a constraint on the test rather than a limitation
    of it: choosing what the acceptable delay IS — batch, priority-lane, or
    accept-and-make-legible — is a design decision parked in the operator's lane on
    T-2240. A CI assertion of the form "delay < N" would encode an unmade decision
    as settled, and would then have to be renegotiated by whoever makes it.
    Legibility is true under all three options and pre-empts none. So: assert the
    age is REPORTED and CORRECT. Assert nothing about whether it is acceptable.

    Time is injected via observe()'s `now=` parameter. Nothing here sleeps — a
    test that sleeps to age a message buys a slower suite and a flakier one, and
    still only proves the clock advances.
    """

    NOW = 1_700_000_000.0
    IN_FLIGHT_AGE_S = 25 * 60   # the long turn
    QUEUED_AGE_S = 5 * 60       # a sender who arrived behind it

    def setUp(self):
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = pathlib.Path(self._td.name)

    def _aged_message(self, box: pathlib.Path, subject: str, age_s: float) -> None:
        """One real message, delivered by the real producer, aged by fiat."""
        mailbox.send(box, {"body": subject}, sender="test-harness", subject=subject)
        newest = max((box / "new").iterdir(), key=lambda p: p.stat().st_mtime)
        stamp = self.NOW - age_s
        os.utime(newest, (stamp, stamp))

    def test_oldest_unread_age_is_observable_and_correct_mid_turn(self):
        """A consumer is genuinely mid-turn; the queue behind it must still be
        legible, and legible means the VALUE is right, not that the field exists.

        A field that is present and always None is the same failure-presents-as-
        absence defect one stratum down — asserting presence alone would be the
        exact vacuity this file exists to prevent. So the age is checked against
        the injected clock.
        """
        hub = self.root / "hub"
        hub.mkdir()
        box = _provision(hub, "fixture-midturn")
        self._aged_message(box, "in-flight", self.IN_FLIGHT_AGE_S)
        self._aged_message(box, "queued-behind", self.QUEUED_AGE_S)

        entered, gate = threading.Event(), threading.Event()
        result: dict = {}

        def blocking_handler(msg):
            entered.set()
            gate.wait(30)

        worker = threading.Thread(
            target=lambda: result.update(mailbox.consume(box, blocking_handler)),
            daemon=True)
        worker.start()
        self.addCleanup(gate.set)
        self.assertTrue(entered.wait(15), "handler never started — fixture is invalid")

        try:
            obs = mon.observe(hub, now=self.NOW,
                              pane_fn=lambda name, panes: None)["fixture-midturn"]
        finally:
            gate.set()
            worker.join(20)

        self.assertIsNotNone(
            obs.oldest_unread_age_s,
            "oldest-message-age must be OBSERVABLE while a turn is in flight — "
            "an unreported age is indistinguishable from no backlog at all")
        self.assertAlmostEqual(
            obs.oldest_unread_age_s, self.IN_FLIGHT_AGE_S, places=3,
            msg="the reported age must be CORRECT against the injected clock, not "
                "merely present — a field that is always None is the same "
                "silent-absence defect one stratum down")
        self.assertEqual(obs.unread, 2,
                         "mid-turn, nothing has been acked yet: both messages are "
                         "still in new/ (no seen flag, no in-progress rename)")

    def test_oldest_age_separates_two_inboxes_at_IDENTICAL_depth(self):
        """Recovery's T-2242 point, made executable: depth cannot tell draining from
        dead; age can. Two inboxes, the SAME unread count, one fresh and one
        stalled. `unread` is identical — so anything keying on depth alone is
        blind here — and `oldest_unread_age_s` is what separates them.

        Still no bound: this asserts only that the two are DISTINGUISHABLE, never
        which of them is acceptable.
        """
        hub = self.root / "hub"
        hub.mkdir()
        fresh = _provision(hub, "fixture-fresh")
        stalled = _provision(hub, "fixture-stalled")
        for _ in range(3):
            self._aged_message(fresh, "recent", 30.0)
            self._aged_message(stalled, "ancient", 3 * 60 * 60)

        obs = mon.observe(hub, now=self.NOW, pane_fn=lambda name, panes: None)

        self.assertEqual(obs["fixture-fresh"].unread, obs["fixture-stalled"].unread,
                         "fixture precondition: identical depth, or the test proves "
                         "nothing about depth being insufficient")
        # Checked before the arithmetic below: a None age would otherwise surface as
        # a TypeError deep in assertAlmostEqual, and an opaque error in CI is a worse
        # guard than a failing assertion that says what broke.
        for name in ("fixture-fresh", "fixture-stalled"):
            self.assertIsNotNone(obs[name].oldest_unread_age_s,
                                 f"{name}: oldest-age went silent — depth is then the "
                                 "only observable left, and depth is what cannot "
                                 "separate draining from dead")
        self.assertAlmostEqual(obs["fixture-fresh"].oldest_unread_age_s, 30.0, places=3)
        self.assertAlmostEqual(obs["fixture-stalled"].oldest_unread_age_s,
                               3 * 60 * 60, places=3)
        self.assertNotAlmostEqual(
            obs["fixture-fresh"].oldest_unread_age_s,
            obs["fixture-stalled"].oldest_unread_age_s, places=3,
            msg="oldest-age must separate what depth cannot — this is the whole "
                "reason the field is worth reporting")


class KnownGapsTest(unittest.TestCase):
    """ITEMS 3 and 4 — gaps that are real today, marked so they cannot go stale.

    Each of these is expected to FAIL. A run that reports them as unexpected
    successes is a failing run, and that is the point: the day a surfacing path
    lands, this file breaks and somebody has to promote the marker to a real
    assertion.
    """

    def setUp(self):
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = pathlib.Path(self._td.name)

    # ------------------------------------------------------------------ ITEM 3
    def test_observation_distinguishes_unacked_from_undelivered(self):
        """THE ACK-PATH CONFOUND — A REAL ASSERTION, and it was an
        `@unittest.expectedFailure` marker for about ninety minutes.

        THE CONFOUND. Reading tools/mailbox.py's `_drain()`: a message is
        `read_text()`, handed to the handler, and only renamed into cur/ AFTER the
        handler returns. There is no seen flag, no in-progress rename, no cur/
        touch until ack. So a consumer mid-turn on a message leaves ZERO trace IN
        THE MAILDIR, and these two states are still BYTE-IDENTICAL on disk today:

            (i)  undelivered            — nobody has ever looked at it
            (ii) delivered-but-unacked  — a handler is running on it right now

        That is what made two agents independently misdiagnose the 2026-07-28
        outage: new/ depth returns the same value under both.

        HOW IT CLOSED, and the mechanism is worth more than the fact. The marker
        was written predicting its own promotion — "if mailbox.py ever grows an
        in-progress marker, OR observe() EVER LEARNS TO REPORT ONE, this flips to
        an unexpected success and forces the promotion." The second branch is what
        happened. Recovery's T-2283 (29432c560, 2026-07-29T01:07:59Z) added
        `consumer_lease_held` to InboxObservation, and it checks LIVENESS rather
        than mere presence — a lock whose holder pid is gone is a tombstone and
        reads False. So:

            (i)  no consumer            -> consumer_lease_held False
            (ii) live consumer mid-turn -> consumer_lease_held True

        NOTE WHAT DID *NOT* HAPPEN: nobody added a seen flag or an in-progress
        rename. The maildir is exactly as ambiguous as it was. The gap closed
        because the OBSERVATION widened past the maildir to a surface that carries
        liveness — which is the same conclusion this suite reached independently in
        LeaseLivenessDiscriminationTest, that the lease is the one place a PID is
        recorded and therefore the one place "is anybody still working this queue?"
        has an answer.

        RESIDUAL, deliberately not asserted here: states (i) and (iii)
        consumer-is-dead BOTH read consumer_lease_held False, so this assertion
        does not separate them. That separation is LeaseLivenessDiscriminationTest's
        job and it is covered there. Three states, two observables, both now real.

        State (ii) is produced by the REAL consume path with a blocking handler,
        not by hand-placing files: the point is what production actually leaves
        mid-turn, and a hand-built approximation would assert against an assumption
        rather than against the code.
        """
        now = 1_700_000_000.0
        stamp = now - 600

        hubs = {}
        for label in ("undelivered", "unacked"):
            hub = self.root / label
            hub.mkdir()
            box = _provision(hub, "consumer")
            mailbox.send(box, {"body": "identical payload"},
                         sender="test-harness", subject="s")
            for p in (box / "new").iterdir():
                os.utime(p, (stamp, stamp))
            hubs[label] = (hub, box)

        entered, gate = threading.Event(), threading.Event()
        result: dict = {}

        def blocking_handler(msg):
            entered.set()
            gate.wait(30)

        worker = threading.Thread(
            target=lambda: result.update(
                mailbox.consume(hubs["unacked"][1], blocking_handler)),
            daemon=True)
        worker.start()
        self.addCleanup(gate.set)
        self.assertTrue(entered.wait(15), "handler never started — fixture is invalid")

        try:
            seen = {
                label: mon.observe(hub, now=now, pane_fn=lambda name, panes: None)["consumer"]
                for label, (hub, _box) in hubs.items()
            }
        finally:
            gate.set()
            worker.join(20)
        self.assertEqual(len(result.get("processed", [])), 1,
                         "the fixture consumer must have really acked its message "
                         "after the gate opened")

        self.assertNotEqual(
            seen["undelivered"], seen["unacked"],
            "undelivered and delivered-but-unacked are indistinguishable from the "
            "maildir — no seen flag, no in-progress rename, no cur/ touch until ack")

    # ------------------------------------------------------------------ ITEM 4
    def test_bridge_unit_declares_a_surfacing_path(self):
        """THE 90 MINUTES, IN ONE LINE OF UNIT CONFIG. A REAL ASSERTION — and it
        was an `@unittest.expectedFailure` marker for about two hours.

        READ THIS PART FIRST; IT IS WHY THE MECHANISM EXISTS. When this file was
        written, `tools/systemd/pantheon-acp-bridge@.service` carried no
        `OnFailure=` while twenty sibling units each carried
        `OnFailure=pantheon-unit-failure-alert@%n.service`. The night's two
        failures, side by side:

          pantheon-mailbox-wake@metis.service   HAS OnFailure -> failed 21:47:04Z,
            routed to recovery, surfaced within minutes by an uninvolved third party.
          pantheon-acp-bridge@mnemosyne.service NO OnFailure  -> died ~19:20Z,
            restarted 20:50:14Z, ninety minutes invisible.

        Same box, same evening, same delivery path; the outcomes tracked that one
        line. So the gap was recorded here as an expected failure rather than as a
        red test (which trains everyone to ignore red), a skip (which rots
        silently), or a green lint (which would have asserted nothing), and the
        fix was filed as T-2264 to recovery.

        T-2264 LANDED AT bc004378f ROUGHLY AN HOUR LATER, and this marker did
        exactly what a marker is for: it flipped to UNEXPECTED SUCCESS and failed
        the run — against the real unit content, in the window between recovery's
        commit and this branch's push, with nobody watching for it. It was
        promoted to a real assertion because it fired, not because someone
        remembered to come back. That is the whole argument for the mechanism,
        and it is kept here on purpose: an expected failure cannot go stale in
        either direction, and this is the worked instance.

        One correction rides along, because the reasoning that nearly buried the
        fix is more instructive than the fix. An earlier reading of mine held that
        `Restart=always` with no StartLimit override would keep OnFailure from
        ever firing on a slow-cadence crash — i.e. that adding the line would not
        have helped. MEASURED AND REFUTED: two throwaway --user units, and the
        variant that never reached the failed state still fired OnFailure once per
        failure. Confident, well-reasoned, internally consistent, and false; it
        was one dispatch away from landing on recovery's surface as a design
        constraint. Running it cost under a minute.

        THE ACP_UNIT SEAM IS STILL LOAD-BEARING, and its role has inverted. It
        used to be how you watched the marker flip green; it is now how you watch
        a real assertion go red. Point ACP_UNIT at a copy of the pre-T-2264 unit
        (no OnFailure line) and this test must FAIL. An assertion never watched
        failing is not evidence — which is the defect this whole file is about.
        """
        unit = pathlib.Path(os.environ.get(
            "ACP_UNIT", str(REPO_ROOT / "tools" / "systemd" / "pantheon-acp-bridge@.service")))
        self.assertRegex(unit.read_text(encoding="utf-8"), r"(?m)^OnFailure=")


if __name__ == "__main__":
    unittest.main()
