"""test_mailbox_wake.py — the wake half of the ORG-026 mailbox (T-0772).

Covers the decision logic (peek / debounce / wake), the marker advance-only-on-success
rule, and the wake-only invariant (the waker NEVER moves/acks a message). The live
tmux inject is stubbed in every case but one — these are pure-logic tests, no pane
required. The exception is T-2245's
test_real_inject_into_a_dead_seat_fails_and_leaves_the_mail_unread, which drives the
REAL inject against a uuid-named target that cannot exist (and skips where tmux does
not) — nothing here ever addresses a live agent pane.
"""
import contextlib
import fcntl
import io
import os
import re
import shutil
import subprocess
import sys
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mailbox  # noqa: E402
import pathlib  # noqa: E402
import mailbox_wake as mw  # noqa: E402


_SS_PATCHES: list = []


def setUpModule():
    """T-5389: no test may read a REAL pane. Several run() paths reach tmux through
    defaults a test does not pass (pane_tail, the def-default capture/enter fns), and
    inside a live seat's tmux they read that seat's pane: a generating seat turned the
    exit-8 case into a defer. Unsetting TMUX alone is not enough (tmux falls back to
    the default socket), so also point TMUX_TMPDIR at an empty private dir: every
    ambient tmux call then finds no server. Tests that start their own server already
    build a private TMUX_TMPDIR on top of this."""
    tmp = tempfile.TemporaryDirectory(prefix="t5389-no-tmux-")
    unittest.addModuleCleanup(tmp.cleanup)
    env = mock.patch.dict(os.environ, {"TMUX_TMPDIR": tmp.name})
    env.start()
    unittest.addModuleCleanup(env.stop)
    os.environ.pop("TMUX", None)
    # ORG-106 L2 (T-5525): run() now reads the seat-state reader. Stub its record / OOM
    # sources (and the cross-account record read, which would shell out) to "nothing
    # held" so the host's real state never changes what these tests see. Maintenance
    # stays the env-pointed registry below. Started here (not at import) so the module
    # cleanup stops them and they cannot leak into other test modules in the same process.
    from tools import seat_state as _ss
    for _name in ("_read_seat_state", "_read_seat_state_cross", "_read_oom"):
        _pt = mock.patch(f"tools.seat_state.{_name}", return_value=_ss.Contribution())
        _pt.start()
        _SS_PATCHES.append(_pt)


def tearDownModule():
    # pytest does not run unittest module cleanups for these, so stop explicitly.
    while _SS_PATCHES:
        _SS_PATCHES.pop().stop()

# T-5277: run() consults the shared seat-maintenance registry. Point it at a path that does not
# exist so a live hold on this host (e.g. mason) can never change what these tests see.
os.environ["PANTHEON_MAINTENANCE_REGISTRY"] = os.path.join(
    tempfile.gettempdir(), f"t5277-no-registry-{os.getpid()}.json")


# T-4313: tmux's three ways of saying "that target is not there", reproduced on tmux 3.4:
#   session_gone  a server is up, the session is not   -> "can't find session: <name>"
#   no_server     a stale socket file, no listener      -> "no server running on <socket>"
#   no_socket     the socket dir exists, no socket file -> "error connecting to <socket>
#                 (No such file or directory)"  (a fresh CI runner, PR #547 run 36605020297)
_DEAD_TARGET_FORMS = (
    ("session_gone", re.compile(r"can't find session")),
    ("no_server", re.compile(r"no server running on ")),
    ("no_socket", re.compile(r"error connecting to \S+ \(No such file or directory\)")),
)


def _dead_target_reason(diag: str) -> str | None:
    """Which dead-target form `diag` shows, or None for any other failure."""
    return next((name for name, rx in _DEAD_TARGET_FORMS if rx.search(diag)), None)


class DeadTargetReasonTest(unittest.TestCase):
    """T-4313: each form is recognised; other failures are not."""

    def test_each_tmux_dead_target_form(self):
        cases = {
            "can't find session: nosuch-t2245-abc": "session_gone",
            "no server running on /tmp/tmux-1000/default": "no_server",
            "error connecting to /tmp/tmux-1000/default (No such file or directory)": "no_socket",
        }
        for diag, want in cases.items():
            with self.subTest(diag=diag):
                self.assertEqual(_dead_target_reason(diag), want)

    def test_other_failures_are_not_a_dead_target(self):
        for diag in ("", "send_keys_submit.sh: No such file or directory",
                     "error connecting to /tmp/tmux-1000/default (Permission denied)",
                     "error connecting to /tmp/x/default (File name too long)"):
            with self.subTest(diag=diag):
                self.assertIsNone(_dead_target_reason(diag))

    def test_real_tmux_no_server_forms(self):
        """Drive the REAL inject at a private tmux socket dir (TMUX unset, so no live
        server can be reached) in both no-server states."""
        if shutil.which("tmux") is None:
            self.skipTest("tmux absent")
        if not mw._SEND_KEYS_HELPER.exists():
            self.skipTest(f"{mw._SEND_KEYS_HELPER} absent")
        import socket
        for want in ("no_socket", "no_server"):
            with self.subTest(want=want):
                tmp = tempfile.mkdtemp(prefix="t4313", dir="/tmp")  # short: socket paths are length-limited
                self.addCleanup(shutil.rmtree, tmp, True)
                if want == "no_server":
                    sock_dir = Path(tmp) / f"tmux-{os.getuid()}"
                    sock_dir.mkdir(mode=0o700)
                    s = socket.socket(socket.AF_UNIX)
                    s.bind(str(sock_dir / "default"))
                    s.close()  # the file stays; nothing listens
                env = {k: v for k, v in os.environ.items() if k != "TMUX"}
                env["TMUX_TMPDIR"] = tmp
                err = io.StringIO()
                with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(err):
                    self.assertFalse(mw.inject(f"nosuch-t4313-{uuid.uuid4().hex}:0", "wake check"))
                self.assertEqual(_dead_target_reason(err.getvalue()), want, err.getvalue())


class WakeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)
        self.new = self.inbox / "new"
        self.marker = self.inbox / mw._MARKER_NAME
        self.injected = []  # records (pane, prompt, harness) the stub was asked to send
        # T-4120: the failed-kick pane re-check sleeps between samples; not in tests.
        p = mock.patch.object(mw, "RECHECK_INTERVAL_SECS", 0, create=True)
        p.start(); self.addCleanup(p.stop)
        # T-4800: the exit-8 path logs the pane bottom; never capture a real pane here.
        p = mock.patch.object(mw, "pane_tail", lambda pane: [])
        p.start(); self.addCleanup(p.stop)
        # T-4792: never read a real seat's runtime.json; each test owns this root.
        self.state_root = Path(self._tmp.name) / "personas"
        p = mock.patch.object(mw.live_harness, "default_state_root", lambda home=None: self.state_root)
        p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _ok_inject(self, pane, prompt, harness=mw._DEFAULT_HARNESS):
        self.injected.append((pane, prompt, harness))
        return True

    def _fail_inject(self, pane, prompt, harness=mw._DEFAULT_HARNESS):
        self.injected.append((pane, prompt, harness))
        return False

    def _run(self, inject, session="S1", now=1000.0, pane_idle=None):
        # Deterministic session identity + clock: these are pure-logic tests, no
        # live tmux query (T-1901 added both knobs to run(); T-2158 added the third).
        # pane_idle DEFAULTS TO None so every pre-existing case keeps legacy
        # behaviour AND so no test ever captures a real pane — the default
        # pane_is_idle would shell out to tmux against a live agent session.
        # T-4292: UNKNOWN now needs positive evidence to inject. These cases test the
        # decide/marker logic, not that policy, so the fallback is stubbed permissive.
        return mw.run("recovery", inbox=self.inbox, do_inject=inject,
                      session_key_fn=lambda pane: session, now=now,
                      pane_idle_fn=lambda pane: pane_idle,
                      unknown_pane_ok_fn=lambda pane, harness: True)

    # -------------------------------------------------------------- empty/peek
    def test_empty_inbox_no_wake(self):
        rc = self._run(self._ok_inject)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [], "empty inbox must not kick")

    # ------------------------------------------------- backoff-aware count (T-2384)
    # The defect these cover: T-2386 stopped a poison message from SPINNING but not
    # from WAKING. A message inside its retry-backoff window is one the drain will
    # refuse to claim, yet a raw readdir still counts it as unread — so the waker
    # re-kicks it once per REKICK_TTL forever, which is archivist's 242 consecutive
    # true-but-useless "1 unread" wakes across three days. peek() stays raw; the
    # WAKE decision must ask the same question the drain asks.
    def test_backed_off_message_does_not_wake(self):
        name = mailbox.send(self.inbox, {"i": 1}, sender="inbox_consumer")
        mailbox._record_backoff(self.inbox, name)     # drain refuses to claim it now
        self.assertEqual(mw.peek(self.new), [name], "peek stays the raw readdir")
        self.assertEqual(mw.deliverable(self.new), [],
                         "a backed-off message is not deliverable")
        rc = self._run(self._ok_inject)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [],
                         "waking for a message the drain will not hand over burns a "
                         "turn and delivers nothing")
        self.assertEqual(sorted(p.name for p in self.new.iterdir()), [name],
                         "and the message must still be sitting unread in new/")

    def test_backoff_expiry_releases_the_wake(self):
        name = mailbox.send(self.inbox, {"i": 1}, sender="inbox_consumer")
        not_before = mailbox._record_backoff(self.inbox, name)
        self.assertEqual(mw.deliverable(self.new), [])
        # Same file, same readdir — only the clock moved past the window.
        self.assertEqual(mailbox.backoff_active(self.inbox, name, now=not_before + 1),
                         False)
        self.assertEqual(mw.deliverable(self.new, now=not_before + 1), [name])
        rc = self._run(self._ok_inject, now=not_before + 1)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1, "a lapsed backoff must wake normally")

    def test_only_the_deliverable_messages_are_counted(self):
        stuck = mailbox.send(self.inbox, {"i": 1}, sender="inbox_consumer")
        fresh = mailbox.send(self.inbox, {"i": 2}, sender="orchestrator")
        mailbox._record_backoff(self.inbox, stuck)
        self.assertEqual(mw.deliverable(self.new), [fresh])
        rc = self._run(self._ok_inject)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1)
        text = self.injected[0][1]
        self.assertIn("You have 1 unread inter-agent message", text)
        self.assertIn("orchestrator", text)
        self.assertNotIn("(from inbox_consumer", text,
                         "the undeliverable message must not be named as the reason "
                         "for the kick")

    def test_unreadable_backoff_sidecar_still_wakes(self):
        # Fail-soft direction: a broken backoff store degrades to a spurious wake,
        # never to silence. Silence here would be lost mail.
        name = mailbox.send(self.inbox, {"i": 1}, sender="orchestrator")
        sidecar = self.inbox / ".backoff" / name
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("{not json at all")
        self.assertEqual(mw.deliverable(self.new), [name])
        rc = self._run(self._ok_inject)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1)

    def test_deliverable_degrades_to_peek_without_the_transport_module(self):
        # The mis-import this guards is real: a bare `import mailbox` off-path binds
        # Python's STDLIB mailbox, which has none of our helpers. Counting nothing
        # would silence every wake on the box.
        name = mailbox.send(self.inbox, {"i": 1}, sender="orchestrator")
        mailbox._record_backoff(self.inbox, name)
        original = mw._mailbox_module
        mw._mailbox_module = lambda: None
        try:
            self.assertEqual(mw.deliverable(self.new), [name],
                             "no transport module → count it (pre-T-2384 behaviour)")
        finally:
            mw._mailbox_module = original

    def test_mailbox_module_rejects_the_stdlib_impostor(self):
        mod = mw._mailbox_module()
        self.assertIsNotNone(mod)
        self.assertTrue(hasattr(mod, "backoff_active"))
        # Identity is NOT the assertion: `tools.mailbox` and `mailbox` are two
        # distinct module objects for one file (the double-import trap mailbox.py
        # documents). What must hold is that we resolved OUR file and not the
        # stdlib's — same-file is the property, sameness of object is not.
        self.assertEqual(Path(mod.__file__).resolve(),
                         (Path(__file__).parent / "mailbox.py").resolve())

    def test_peek_skips_dotfiles_and_is_sorted(self):
        mailbox.send(self.inbox, {"i": 1}, sender="orchestrator")
        mailbox.send(self.inbox, {"i": 2}, sender="specialist")
        (self.new / ".hidden").write_text("x")  # lock/marker-style noise
        names = mw.peek(self.new)
        self.assertEqual(len(names), 2)
        self.assertEqual(names, sorted(names))
        self.assertFalse(any(n.startswith(".") for n in names))

    # --------------------------------------------------------------- wake path
    def test_wake_injects_and_advances_marker(self):
        mailbox.send(self.inbox, {"task": "T-0767"}, sender="orchestrator")
        rc = self._run(self._ok_inject)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1, "a fresh message must kick once")
        self.assertIn("orchestrator", self.injected[0][1])
        self.assertEqual(self.injected[0][0], "claude-telegram-bot-fallback:0")
        self.assertTrue(self.marker.exists(), "marker advances on a landed kick")

    def test_wake_atomically_replaces_unwritable_stale_marker(self):
        self.marker.write_text("stale\n", encoding="utf-8")
        self.marker.chmod(0)

        mw._write_marker(self.marker, "newest", "S1", 1000.0)

        self.assertEqual(
            mw._read_marker(self.marker),
            {
                "newest": "newest",
                "session": "S1",
                "ts": 1000.0,
                "idle_kicked": False,
            },
        )
        self.assertEqual(self.marker.stat().st_mode & 0o777, 0o660)
        self.assertEqual(list(self.inbox.glob(f"{self.marker.name}.*")), [])

    def test_failed_atomic_marker_replace_preserves_original_and_cleans_temp(self):
        self.marker.write_text("original\n", encoding="utf-8")

        with mock.patch.object(
            mw.os,
            "replace",
            side_effect=PermissionError("synthetic replace denial"),
        ):
            with self.assertRaisesRegex(PermissionError, "synthetic replace denial"):
                mw._write_marker(self.marker, "newest", "S1", 1000.0)

        self.assertEqual(self.marker.read_text(encoding="utf-8"), "original\n")
        self.assertEqual(list(self.inbox.glob(f"{self.marker.name}.*")), [])

    def test_codex_pane_passes_codex_harness(self):
        # T-1106: codex's pane must select the codex submit-verify profile, else
        # send_keys_submit false-passes on the always-absent claude-code sentinel.
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        # Stub pane state so the harness assertion is isolated from the real pane
        # (T-2681 added a confirmed-busy skip to run(); without a stub this shells
        # out to the live pane and skips the inject whenever that pane is busy).
        mw.run("codex", inbox=self.inbox, do_inject=self._ok_inject,
               pane_idle_fn=lambda pane: None, unknown_pane_ok_fn=lambda pane, harness: True)
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.injected[0][2], "codex", "codex pane must pass --harness codex")
        self.assertEqual(self.injected[0][0], "codex-session")

    def test_claude_persona_passes_default_harness(self):
        # A Claude-Code persona (recovery) keeps the default claude-code profile.
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        # Stub pane state — see test_codex_pane_passes_codex_harness (T-2681).
        mw.run("recovery", inbox=self.inbox, do_inject=self._ok_inject,
               pane_idle_fn=lambda pane: None, unknown_pane_ok_fn=lambda pane, harness: True)
        self.assertEqual(self.injected[0][2], "claude-code")

    def _record_runtime(self, agent, harness):
        rc = self.state_root / agent / "runtime-controller"
        rc.mkdir(parents=True, exist_ok=True)
        (rc / "runtime.json").write_text(json.dumps(
            {"schema": "pantheon-runtime-state/v1", "harness": harness}), encoding="utf-8")

    def test_claude_persona_failed_over_to_codex_passes_codex_harness(self):
        # T-4792: the controller's recorded runtime wins over the static map. A
        # Claude-Code persona running on codex must get the codex profile, else the
        # wake paste sits unsubmitted (specialist, 2026-10-13).
        self._record_runtime("recovery", "codex")
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        mw.run("recovery", inbox=self.inbox, do_inject=self._ok_inject,
               pane_idle_fn=lambda pane: None, unknown_pane_ok_fn=lambda pane, harness: True)
        self.assertEqual(self.injected[0][2], "codex")

    def test_unknown_pane_check_uses_live_harness(self):
        self._record_runtime("recovery", "codex")
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        seen = []
        mw.run("recovery", inbox=self.inbox, do_inject=self._ok_inject, pane_idle_fn=lambda pane: None,
               unknown_pane_ok_fn=lambda pane, harness: seen.append(harness) or True)
        self.assertEqual(seen, ["codex"])

    def test_wake_only_never_acks(self):
        # The load-bearing invariant: the waker leaves every message in new/ (no move).
        mid = mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject)
        self.assertTrue((self.new / mid).exists(), "waker must NOT move the message out of new/")
        self.assertFalse((self.inbox / "cur" / mid).exists(), "waker must NOT ack to cur/")

    # ----------------------------------------------------------------- debounce
    def test_debounce_no_rekick_while_unchanged(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject)            # first → wake
        self._run(self._ok_inject)            # second, nothing newer → debounced
        self.assertEqual(len(self.injected), 1, "must not re-kick while newest is unchanged")

    def test_newer_message_rekicks(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject)            # wake (1)
        mailbox.send(self.inbox, {"x": 2}, sender="specialist")  # genuinely newer
        self._run(self._ok_inject)            # wake again (2)
        self.assertEqual(len(self.injected), 2, "a newer message must re-nudge")

    # ----------------------------------------------------------- failed inject
    def test_failed_kick_does_not_advance_marker(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        rc = self._run(self._fail_inject)
        self.assertEqual(rc, 8, "a failed kick returns non-zero")
        self.assertFalse(self.marker.exists(), "marker must NOT advance on a dropped kick")
        # Next fire retries (marker never advanced) — message still in new/.
        rc2 = self._run(self._ok_inject)
        self.assertEqual(rc2, 0)
        self.assertEqual(len(self.injected), 2, "dropped kick is retried on the next fire")

    def test_failed_kick_pane_now_busy_soft_exits(self):
        """T-3554: idle→busy RACE — pane idle at decide-time (so it wakes + injects),
        but the agent starts a turn before submit confirms, so the kick fails 'submit
        unconfirmed'. Re-checking finds the pane BUSY now: that is the same benign
        collision T-2681 defers, seen one sample late — soft-exit 0, NO OnFailure alarm,
        marker NOT advanced (mail retained, retried next fire)."""
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        calls = {"n": 0}

        def pane_idle_fn(pane):        # idle at decide (call 1), busy at re-check (call 2+)
            calls["n"] += 1
            return True if calls["n"] == 1 else False

        rc = mw.run("recovery", inbox=self.inbox, do_inject=self._fail_inject,
                    session_key_fn=lambda pane: "S1", now=1000.0,
                    pane_idle_fn=pane_idle_fn)
        self.assertEqual(rc, 0, "idle→busy race must NOT alarm — it's a live agent mid-turn")
        self.assertGreaterEqual(calls["n"], 2, "the failure path must RE-CHECK the pane")
        self.assertFalse(self.marker.exists(), "marker must NOT advance — mail retained for retry")
        rc2 = self._run(self._ok_inject)  # next fire delivers; nothing was lost
        self.assertEqual(rc2, 0)
        self.assertEqual(len(self.injected), 2, "retried on the next fire, nothing lost")

    def test_failed_kick_on_idle_pane_still_alarms(self):
        """A pane that reads IDLE at BOTH decide-time and the failure re-check yet still
        rejects the submit is a genuine anomaly (idle pane not accepting, or a dead/gone
        seat) — the T-2245 send-liveness rung. Must still exit 8 so OnFailure fires."""
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        rc = self._run(self._fail_inject, pane_idle=True)  # True at decide AND re-check
        self.assertEqual(rc, 8, "an idle pane that rejects the submit is a real anomaly — alarm")
        self.assertFalse(self.marker.exists(), "marker must NOT advance on a dropped kick")

    def test_failed_kick_unknown_then_busy_soft_exits(self):
        """T-4120: in the submit->generate transition the pane is briefly
        UNCLASSIFIABLE. A single re-read landing there (None) used to exit 8 and page
        recovery although the kick had landed (2026-09-28 10:28Z). Busy on ANY re-check
        sample is the T-3554 race: soft-exit 0, marker not advanced."""
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        seq = iter([True, None, False])   # decide: idle; re-checks: unknown, then busy

        def pane_idle_fn(pane):
            return next(seq, False)

        rc = mw.run("recovery", inbox=self.inbox, do_inject=self._fail_inject,
                    session_key_fn=lambda pane: "S1", now=1000.0,
                    pane_idle_fn=pane_idle_fn)
        self.assertEqual(rc, 0, "unknown-then-busy is the idle->busy race, not a dead seat")
        self.assertFalse(self.marker.exists(), "marker must NOT advance — mail retained for retry")

    def test_failed_kick_sustained_unknown_still_alarms(self):
        """T-4120 must keep the dead-seat alarm: a wedged or gone seat never reads busy,
        so idle/unknown on EVERY re-check sample still exits 8, after a bounded number
        of samples."""
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        calls = {"n": 0}

        def pane_idle_fn(pane):
            calls["n"] += 1
            return True if calls["n"] == 1 else None

        rc = mw.run("recovery", inbox=self.inbox, do_inject=self._fail_inject,
                    session_key_fn=lambda pane: "S1", now=1000.0,
                    pane_idle_fn=pane_idle_fn)
        self.assertEqual(rc, 8, "sustained unknown after a failed kick is still an anomaly")
        self.assertEqual(calls["n"], 1 + mw.RECHECK_SAMPLES, "re-check is bounded")

    def test_exit8_logs_the_pane_bottom(self):
        """T-4800: a false exit 8 (archivist 2026-10-09) could not be diagnosed from
        verdicts alone. The exit-8 path prints the pane's last lines to the journal."""
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        calls = {"n": 0}

        def pane_idle_fn(pane):
            calls["n"] += 1
            return True if calls["n"] == 1 else None

        seen = []

        def tail_fn(pane):
            seen.append(pane)
            return ["\u276f ", "\u23f8 manual mode on \u00b7 \u2190 for agents"]

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = mw.run("recovery", inbox=self.inbox, do_inject=self._fail_inject,
                        session_key_fn=lambda pane: "S1", now=1000.0,
                        pane_idle_fn=pane_idle_fn, pane_tail_fn=tail_fn)
        self.assertEqual(rc, 8)
        self.assertEqual(seen, [mw.AGENT_PANE["recovery"]["pane_target"]], "one capture, of the seat's pane")
        self.assertIn("pane bottom at the last re-check (2 non-blank line(s)", err.getvalue())
        self.assertIn("[mailbox_wake]   | \u23f8 manual mode on", err.getvalue())

    # T-5002 specimen: pantheon-mailbox-wake@orchestrator exit 8 at 2026-10-20 05:17:48Z. The kick
    # landed (the spinner is the turn it started), every -e re-check read UNKNOWN, and the
    # plain capture taken in the same second for the T-4800 log shows a generating pane.
    T5002_TAIL = [
        "  is lost if you finish your current turn first.",
        "\u273b Deciphering\u2026 (running UserPromptSubmit hook \u00b7 3s)",
        "\u2500" * 80,
        "\u276f",
        "\u2500" * 80,
        "  paste again to expand                  \u2714 Update installed \u00b7 Restart to apply",
    ]

    def test_t5002_plain_tail_showing_a_generating_pane_soft_exits(self):
        """Busy on ANY sample is the idle->busy race (T-4120), and the plain capture the exit-8
        path already takes is one more sample: a spinner there means the kick landed."""
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        calls = {"n": 0}

        def pane_idle_fn(pane):
            calls["n"] += 1
            return True if calls["n"] == 1 else None

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = mw.run("recovery", inbox=self.inbox, do_inject=self._fail_inject,
                        session_key_fn=lambda pane: "S1", now=1000.0,
                        pane_idle_fn=pane_idle_fn, pane_tail_fn=lambda pane: list(self.T5002_TAIL))
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("T-5002", err.getvalue())
        self.assertTrue(list(self.new.iterdir()), "mail is retained for the next fire")
        self.assertFalse(self.marker.exists(), "the marker is not advanced on a soft exit")

    def test_t5002_plain_tail_that_is_idle_still_exits_8(self):
        """The extra sample may only ADD busy evidence: an idle or unreadable tail keeps the
        dead-seat alarm."""
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        for tail in (["\u276f ", "  ? for shortcuts"], [], ["garbage"]):
            with self.subTest(tail=tail):
                calls = {"n": 0}

                def pane_idle_fn(pane):
                    calls["n"] += 1
                    return True if calls["n"] == 1 else None

                with contextlib.redirect_stderr(io.StringIO()):
                    rc = mw.run("recovery", inbox=self.inbox, do_inject=self._fail_inject,
                                session_key_fn=lambda pane: "S1", now=1000.0,
                                pane_idle_fn=pane_idle_fn, pane_tail_fn=lambda pane, t=tail: list(t))
                self.assertEqual(rc, 8)

    def test_soft_exit_does_not_capture_the_pane(self):
        """The pane bottom is logged only on exit 8, not on the benign busy race."""
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        seq = iter([True, False])
        seen = []
        rc = mw.run("recovery", inbox=self.inbox, do_inject=self._fail_inject,
                    session_key_fn=lambda pane: "S1", now=1000.0,
                    pane_idle_fn=lambda pane: next(seq, False),
                    pane_tail_fn=lambda pane: seen.append(pane) or [])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [])

    # ------------------------------------------------ UNKNOWN pane (T-4292)
    def _run_unknown(self, agent="recovery", ok_fn=None, pane_idle_fn=None):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        kw = {} if ok_fn is None else {"unknown_pane_ok_fn": ok_fn}
        return mw.run(agent, inbox=self.inbox, do_inject=self._ok_inject,
                      session_key_fn=lambda pane: "S1", now=1000.0,
                      pane_idle_fn=pane_idle_fn or (lambda pane: None), **kw)

    def test_unknown_claude_pane_defers_by_default(self):
        """T-4292: UNKNOWN used to inject, so a permission dialog got paste plus Enter.
        The REAL default policy is used here: for a claude pane it must say no without
        touching tmux (a patched subprocess.run proves it is never called)."""
        with mock.patch.object(mw.subprocess, "run", side_effect=AssertionError("no tmux")):
            rc = self._run_unknown("recovery")
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [], "an UNKNOWN claude pane must not be injected")
        self.assertFalse(self.marker.exists(), "deferral keeps the marker (mail retried)")

    def test_permission_modal_capture_defers_end_to_end(self):
        """Through the real classifier: a tool-permission prompt reads UNKNOWN, and the
        wake defers instead of pasting into it."""
        from tools.inbox_consumer import pane_idle_state
        modal = (" Do you want to proceed?\n ❯ 1. Yes\n   2. No, and tell Claude what to do"
                 " differently (esc)\n Esc to cancel · Tab to amend\n")
        rc = self._run_unknown("recovery", pane_idle_fn=lambda pane: pane_idle_state(modal))
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [])

    def test_unknown_codex_pane_injects_only_with_ready_evidence(self):
        seen = []
        rc = self._run_unknown("codex", ok_fn=lambda pane, harness: seen.append(harness) or True)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["codex"], "the fallback must be asked with the codex harness")
        self.assertEqual(len(self.injected), 1)

    def test_unknown_codex_pane_without_evidence_defers(self):
        rc = self._run_unknown("codex", ok_fn=lambda pane, harness: False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [])
        self.assertFalse(self.marker.exists())

    def test_idle_pane_still_injects_without_consulting_fallback(self):
        rc = self._run_unknown("recovery", pane_idle_fn=lambda pane: True,
                               ok_fn=lambda pane, harness: self.fail("fallback consulted"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1)

    # unknown_pane_deliverable itself (subprocess patched: no live pane is read)
    _RULE = "─" * 60
    CODEX_READY = f"─ Worked for 1h 15m {_RULE}\n› Use /skills to list available skills\ngpt-5.6-sol high · ~ · Main [default]\n"

    def _deliverable(self, text, rc=0, harness="codex"):
        out = mock.Mock(returncode=rc, stdout=text)
        with mock.patch.object(mw.subprocess, "run", return_value=out):
            return mw.unknown_pane_deliverable("codex-session", harness)

    def test_deliverable_codex_ready_composer(self):
        self.assertTrue(self._deliverable(self.CODEX_READY))

    def test_deliverable_codex_update_modal_is_no(self):
        modal = self.CODEX_READY + "Update available! 0.150 -> 0.151\n› 3. Skip until next version\n"
        self.assertFalse(self._deliverable(modal))

    def test_deliverable_codex_model_migration_modal_is_no(self):
        """rev-t4284 round 2, B3: an Enter here would accept the model migration. The
        live capture alone is not ready-for-submission, which would mask the clause,
        so it is drawn under the ready composer's scrollback (ready, AND a modal)."""
        from submit_verify import ready_for_submission
        live = (pathlib.Path(__file__).resolve().parent / "testdata"
                / "codex_migration_modal_live_markdown_pane.txt").read_text(encoding="utf-8")
        pane = self.CODEX_READY + live
        self.assertTrue(ready_for_submission(pane))  # precondition: only the modal clause says no
        self.assertFalse(self._deliverable(pane))

    def test_deliverable_confirm_dialog_is_no(self):
        self.assertFalse(self._deliverable(self.CODEX_READY + "Enter to confirm · Esc to cancel\n"))

    def test_deliverable_capture_failure_is_no(self):
        self.assertFalse(self._deliverable("", rc=1))

    def test_deliverable_claude_harness_is_no(self):
        self.assertFalse(self._deliverable(self.CODEX_READY, harness="claude-code"))
        self.assertFalse(self._deliverable(self.CODEX_READY, harness="claude"))

    # --------------------------------------------------------------- malformed
    def test_malformed_message_does_not_break_waker(self):
        (self.new / "00000000000000000001.1.0.x").write_text("not json\n")
        rc = self._run(self._ok_inject)  # _senders must swallow the parse error
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1, "still wakes despite an unparseable message")

    def test_unknown_agent_rejected(self):
        self.assertEqual(mw.run("nobody", inbox=self.inbox, do_inject=self._ok_inject), 64)

    # ------------------------------------------------- T-1901: session identity
    def test_session_restart_rekicks_same_newest(self):
        # The Mason first-retask hole: kick landed, session restarted, newest-id
        # unchanged — the fresh session must be re-nudged.
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject, session="S1")   # wake into S1
        self._run(self._ok_inject, session="S1")   # same session → debounced
        self._run(self._ok_inject, session="S2")   # restarted pane → re-kick
        self.assertEqual(len(self.injected), 2, "a session change must re-nudge")

    def test_session_query_failure_falls_back_to_legacy_debounce(self):
        # A flaky/absent tmux (session None) must NOT cause kick spam: newest-only
        # debounce governs, exactly the pre-T-1901 behavior.
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject, session="S1")
        self._run(self._ok_inject, session=None)   # can't read identity → debounce
        self.assertEqual(len(self.injected), 1)

    def test_legacy_plaintext_marker_wakes_once_then_upgrades(self):
        # Pre-upgrade marker can't prove the kick reached the CURRENT session:
        # wake once (un-strands a stuck seat), rewrite marker in JSON form.
        mid = mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self.marker.write_text(mid + "\n")          # legacy format, newest matches
        self._run(self._ok_inject, session="S1")    # → wake (marker predates tracking)
        self.assertEqual(len(self.injected), 1)
        self.assertIn('"session": "S1"', self.marker.read_text(), "marker upgraded to JSON")
        self._run(self._ok_inject, session="S1")    # now tracked → debounced
        self.assertEqual(len(self.injected), 1)

    def test_legacy_marker_without_session_key_stays_debounced(self):
        # Legacy marker AND no readable session identity → exact legacy behavior.
        mid = mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self.marker.write_text(mid + "\n")
        self._run(self._ok_inject, session=None)
        self.assertEqual(self.injected, [], "no regression when tmux is unqueryable")

    # ---------------------------------------------------- T-1901: re-kick TTL
    def test_ttl_expiry_rekicks_unconsumed_mail(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject, now=1000.0)                          # wake
        self._run(self._ok_inject, now=1000.0 + mw.REKICK_TTL_SECONDS - 1)  # inside → debounced
        self._run(self._ok_inject, now=1000.0 + mw.REKICK_TTL_SECONDS + 1)  # past → re-kick
        self.assertEqual(len(self.injected), 2, "unconsumed mail re-nudges once per TTL")

    def test_ttl_rekick_is_bounded_one_per_window(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        t0 = 1000.0
        self._run(self._ok_inject, now=t0)                                   # wake 1
        t1 = t0 + mw.REKICK_TTL_SECONDS + 1
        self._run(self._ok_inject, now=t1)                                   # wake 2 (TTL)
        self._run(self._ok_inject, now=t1 + 10)                              # fresh marker → debounced
        self.assertEqual(len(self.injected), 2, "TTL re-kick must not spam every fire")

    # ------------------------------------------------- decide() reason surface
    def test_decide_reports_reasons(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        action, names, reason = mw.decide(self.new, self.marker, "S1", 1000.0)
        self.assertEqual(action, "wake")
        self.assertIn("newer", reason)

    # ---------------------------------------------------------- session_key()
    def test_session_key_swallows_bad_target(self):
        # Nonexistent pane (or no tmux at all) → None, never an exception.
        self.assertIsNone(mw.session_key("definitely-not-a-real-session-xyz:0"))

    # ------------------------------------------- T-2245: the REAL inject, for once
    def test_real_inject_into_a_dead_seat_fails_and_leaves_the_mail_unread(self):
        """T-2245 acceptance 6: send-side liveness at the wake rung, exercised for real.

        THE GAP THIS CLOSES. A sender cannot tell that a dispatch went into a seat
        with no live session — silence back is indistinguishable from a busy agent.
        This arc's own dispatch was lost exactly that way on 2026-07-28: sent to a
        dormant seat, the wake unit failed, and it surfaced only because an
        OnFailure alert routed to a third party. `run()`'s rc 8 IS that alert's
        trigger, so it is the load-bearing rung.

        WHAT IS ACTUALLY NEW HERE, stated so this does not imply more than it is:
        rc == 8 is ALREADY covered by test_failed_kick_does_not_advance_marker,
        which stubs inject. The lift is that this drives the REAL `mw.inject` —
        real subprocess, real send_keys_submit.sh, real tmux — against a target
        that does not exist, plus the effect assertions (marker not advanced, mail
        still in new/). Nothing else in this file touches the real inject path.

        The stderr assertion is deliberate: `inject() is False` on its own is also
        what a missing helper, an absent tmux, or any unrelated subprocess failure
        returns, so a bare False would not prove the DEAD-TARGET path was the one
        exercised. tmux's own `can't find session` for our uuid target does.

        Skip-not-fail where the substrate is absent, per
        test_restart_drain.test_validate_on_live_fleet_topology: a red here on a
        bare CI container measures the box, not the code; a skip says plainly that
        the check did not run.
        """
        if shutil.which("tmux") is None:
            self.skipTest("tmux absent — this exercises the real inject subprocess; "
                          "a failure here would measure the box, not the code")
        if not mw._SEND_KEYS_HELPER.exists():
            self.skipTest(f"{mw._SEND_KEYS_HELPER} absent — same reason")

        # T-4320: never the ambient tmux server. The helper stages the prompt in a
        # named buffer (send_keys_submit) that real wakes share, and a failed paste
        # leaves it behind, so this test used to leave wake-prompt residue on the
        # live server. TMUX is unset and TMUX_TMPDIR points at a private dir with a
        # throwaway server (no user config), killed on cleanup. With a server up and
        # our session absent, tmux answers session_gone; the no-server forms are
        # DeadTargetReasonTest.test_real_tmux_no_server_forms.
        tmp = tempfile.mkdtemp(prefix="t4320", dir="/tmp")  # short: socket paths are length-limited
        self.addCleanup(shutil.rmtree, tmp, True)
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        env["TMUX_TMPDIR"] = tmp
        server = subprocess.run(
            ["tmux", "-f", "/dev/null", "new-session", "-d", "-s", f"t4320-{uuid.uuid4().hex}"],
            env=env, capture_output=True, text=True)
        if server.returncode != 0:
            self.skipTest(f"can't start a private tmux server: {server.stderr.strip()!r}")
        self.addCleanup(subprocess.run, ["tmux", "kill-server"], env=env, capture_output=True)
        private_tmux = mock.patch.dict(os.environ, env, clear=True)
        private_tmux.start()
        self.addCleanup(private_tmux.stop)

        # uuid4 so this provably cannot name any live agent pane. The waker is
        # never allowed to address cfg["pane_target"] in this test.
        dead_seat = f"nosuch-t2245-{uuid.uuid4().hex}:0"

        def real_inject_at_dead_seat(pane, prompt, harness=mw._DEFAULT_HARNESS):
            return mw.inject(dead_seat, prompt, harness)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(mw.inject(dead_seat, "wake check"),
                             "injecting into a nonexistent tmux target must report failure")
        diag = err.getvalue()
        # The private server is up, so the only dead-target form is session_gone
        # (T-4313's _dead_target_reason). It proves inject() failed for the reason this
        # test exists to exercise, not an unrelated subprocess failure. It must also
        # name OUR uuid target, so a session-gone error for some other pane can't
        # satisfy it.
        self.assertEqual(
            _dead_target_reason(diag), "session_gone",
            f"inject returned False, but not because the target session is gone. "
            f"stderr was: {diag!r}")
        self.assertIn(dead_seat.split(":")[0], diag,
                      "the session-gone failure must name OUR uuid target, not some other pane")

        mid = mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = self._run(real_inject_at_dead_seat)
        self.assertEqual(rc, 8, "a wake into a dead seat must exit non-zero (8) so the "
                               "unit's OnFailure fires — silence is not an outcome")
        self.assertFalse(self.marker.exists(),
                         "a failed kick must NOT advance the marker, or the next fire "
                         "debounces and the message is stranded")
        self.assertTrue((self.new / mid).exists(),
                        "the message must still be unread in new/ — the waker never acks")


class WakeLockTest(unittest.TestCase):
    """T-5283: mailbox_wake.run and the consume wake path both kick the same pane. A
    non-blocking flock on <box>/.wake.lock serializes them from decide through the
    marker write. Real fcntl on a per-test temp dir, never a mock of flock."""

    setUp = WakeTest.setUp
    tearDown = WakeTest.tearDown
    _ok_inject = WakeTest._ok_inject
    _run = WakeTest._run

    def _lock_path(self):
        return self.inbox / mw._WAKE_LOCK_NAME

    def _try_lock(self):
        """Take the lock on a fresh open file description; return the fd or None if busy."""
        fd = os.open(self._lock_path(), os.O_RDWR | os.O_CREAT, 0o660)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd

    def _release(self, fd):
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def test_lock_held_elsewhere_is_debounced(self):
        mailbox.send(self.inbox, {"i": 1}, sender="tester")
        held = self._try_lock()
        self.assertIsNotNone(held)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = self._run(self._ok_inject, pane_idle=True)
        finally:
            self._release(held)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [], "a busy wake lock means another waker is kicking")
        self.assertFalse(self.marker.exists(), "the marker belongs to the lock holder")
        self.assertIn(mw.WAKE_LOCK_BUSY_REASON, out.getvalue())

    def test_lock_unopenable_fails_open(self):
        # A directory where the lock file should be: os.open(O_RDWR) raises EISDIR.
        self._lock_path().mkdir()
        mailbox.send(self.inbox, {"i": 1}, sender="tester")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self._run(self._ok_inject, pane_idle=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1, "a wake is never lost to the lock")
        self.assertTrue(self.marker.is_file())
        self.assertIn("unlocked", err.getvalue())

    def test_lock_permission_error_fails_open(self):
        real_open = os.open
        lock = str(self._lock_path())

        def guarded(path, *args, **kwargs):
            if os.fspath(path) == lock:
                raise PermissionError(13, "Permission denied", lock)
            return real_open(path, *args, **kwargs)

        mailbox.send(self.inbox, {"i": 1}, sender="tester")
        with mock.patch.object(mw.os, "open", guarded), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = self._run(self._ok_inject, pane_idle=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1)

    def test_normal_wake_holds_lock_during_inject_and_releases_after(self):
        mailbox.send(self.inbox, {"i": 1}, sender="tester")
        seen = []

        def inject(pane, prompt, harness=mw._DEFAULT_HARNESS):
            seen.append(self._try_lock())  # None: the waker holds it mid-kick
            return self._ok_inject(pane, prompt, harness)

        rc = self._run(inject, pane_idle=True)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [None], "the lock must be held across the inject")
        self.assertEqual(len(self.injected), 1)
        self.assertTrue(self.marker.is_file())
        fd = self._try_lock()
        self.assertIsNotNone(fd, "the lock is released after the run")
        self._release(fd)
        self.assertEqual(self._lock_path().stat().st_mode & 0o777, 0o660)

    def test_lock_released_when_inject_raises(self):
        mailbox.send(self.inbox, {"i": 1}, sender="tester")

        def boom(pane, prompt, harness=mw._DEFAULT_HARNESS):
            raise RuntimeError("inject blew up")

        with self.assertRaises(RuntimeError):
            self._run(boom, pane_idle=True)
        fd = self._try_lock()
        self.assertIsNotNone(fd)
        self._release(fd)

    def test_lock_fd_not_inheritable(self):
        with mw.wake_lock(self.inbox) as state:
            self.assertEqual(state, "held")
            fds = [int(n) for n in os.listdir("/proc/self/fd")] if os.path.isdir("/proc/self/fd") else []
            lock_fds = [fd for fd in fds
                        if os.path.realpath(f"/proc/self/fd/{fd}") == str(self._lock_path().resolve())]
            for fd in lock_fds:
                self.assertFalse(os.get_inheritable(fd))


class IdleRekickCondition5Test(unittest.TestCase):
    """T-2158: a kick injected MID-TURN submit-verifies (the input line clears) but is
    never acted on, and the debounce then correctly counts it delivered. Specialist sat 32
    minutes on a 12-message backlog with every log line green. Condition 5 re-kicks on
    the busy->idle TRANSITION so the failure self-heals within one timer period instead
    of waiting out the 1800s TTL backstop.

    The load-bearing test in here is test_busy_with_unread_still_debounces: condition 5
    must NEVER interrupt an agent that is legitimately working, which was the whole
    reason declining to kick specialist was the right call at 04:19 that night.
    """

    # Own fixture rather than subclassing WakeTest: inheriting it would silently
    # RE-RUN all 19 base cases under this class's name, inflating the count with
    # duplicates — the count-stability lesson from T-2170, one layer up.
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)
        self.new = self.inbox / "new"
        self.marker = self.inbox / mw._MARKER_NAME
        self.injected = []
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.object(mw, "RECHECK_INTERVAL_SECS", 0, create=True)  # T-4120
        p.start(); self.addCleanup(p.stop)

    def _ok_inject(self, pane, prompt, harness=mw._DEFAULT_HARNESS):
        self.injected.append((pane, prompt, harness))
        return True

    def _fail_inject(self, pane, prompt, harness=mw._DEFAULT_HARNESS):
        self.injected.append((pane, prompt, harness))
        return False

    def _run(self, inject, session="S1", now=1000.0, pane_idle=None):
        # T-4292: UNKNOWN now needs positive evidence to inject. These cases test the
        # decide/marker logic, not that policy, so the fallback is stubbed permissive.
        return mw.run("recovery", inbox=self.inbox, do_inject=inject,
                      session_key_fn=lambda pane: session, now=now,
                      pane_idle_fn=lambda pane: pane_idle,
                      unknown_pane_ok_fn=lambda pane, harness: True)

    def _kicked_state(self, now=1000.0, idle_kicked=False):
        """Deliver one message and record a kick for it, as a prior sweep would."""
        mailbox.send(str(self.inbox), payload="p", sender="orchestrator", subject="s")
        names = mw.peek(self.new)
        mw._write_marker(self.marker, names[-1], "S1", now, idle_kicked=idle_kicked)
        return names

    def test_idle_with_unread_after_kick_wakes(self):
        self._kicked_state()
        action, _, reason = mw.decide(self.new, self.marker, "S1", 1100.0, pane_idle=True)
        self.assertEqual(action, "wake")
        self.assertEqual(reason, mw.IDLE_REKICK_REASON)

    def test_busy_with_unread_still_debounces(self):
        """THE REGRESSION GUARD: a generating agent must not be interrupted."""
        self._kicked_state()
        action, _, _ = mw.decide(self.new, self.marker, "S1", 1100.0, pane_idle=False)
        self.assertEqual(action, "debounced")

    def test_unknown_idle_state_is_legacy_behaviour(self):
        """None = UNKNOWN must never wake — same contract as session_key()."""
        self._kicked_state()
        action, _, _ = mw.decide(self.new, self.marker, "S1", 1100.0, pane_idle=None)
        self.assertEqual(action, "debounced")

    def test_idle_rekick_fires_only_once_per_newest_message(self):
        """Firing on the TRANSITION is what removes the need for a spam floor."""
        self._kicked_state(idle_kicked=True)
        action, _, _ = mw.decide(self.new, self.marker, "S1", 1100.0, pane_idle=True)
        self.assertEqual(action, "debounced")

    def test_ttl_backstop_still_fires_independently_when_idle_kick_is_spent(self):
        """Condition 4 must remain the backstop for a swallowed idle kick."""
        self._kicked_state(idle_kicked=True)
        late = 1000.0 + mw.REKICK_TTL_SECONDS + 1
        action, _, reason = mw.decide(self.new, self.marker, "S1", late, pane_idle=True)
        self.assertEqual(action, "wake")
        self.assertIn("older than", reason)

    def test_ttl_backstop_still_fires_when_pane_is_busy(self):
        self._kicked_state()
        late = 1000.0 + mw.REKICK_TTL_SECONDS + 1
        action, _, _ = mw.decide(self.new, self.marker, "S1", late, pane_idle=False)
        self.assertEqual(action, "wake")

    def test_newer_message_still_wins_over_idle_state(self):
        self._kicked_state()
        mailbox.send(str(self.inbox), payload="p2", sender="specialist", subject="s2")
        action, _, reason = mw.decide(self.new, self.marker, "S1", 1100.0, pane_idle=True)
        self.assertEqual((action, reason), ("wake", "newer message"))

    def test_no_marker_means_no_idle_rekick_path(self):
        """Condition 5 requires a RECORDED kick; a fresh inbox wakes as 'newer message'."""
        mailbox.send(str(self.inbox), payload="p", sender="orchestrator", subject="s")
        action, _, reason = mw.decide(self.new, self.marker, "S1", 1100.0, pane_idle=True)
        self.assertEqual((action, reason), ("wake", "newer message"))

    # ------------------------------------------------------------- marker state
    def test_idle_triggered_kick_is_recorded_on_the_marker(self):
        self._kicked_state()
        rc = self._run(self._ok_inject, now=1100.0, pane_idle=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1)
        self.assertTrue(mw._read_marker(self.marker)["idle_kicked"])

    def test_other_wake_reasons_reset_idle_kicked(self):
        """A fresh cause deserves a fresh idle re-kick if that one is swallowed too."""
        self._kicked_state(idle_kicked=True)
        mailbox.send(str(self.inbox), payload="p2", sender="specialist", subject="s2")
        self._run(self._ok_inject, now=1100.0, pane_idle=True)
        self.assertFalse(mw._read_marker(self.marker)["idle_kicked"])

    def test_failed_idle_kick_does_not_advance_the_marker(self):
        """Advance-only-on-success holds for condition 5 too, or the retry is lost."""
        self._kicked_state()
        before = mw._read_marker(self.marker)
        self._run(self._fail_inject, now=1100.0, pane_idle=True)
        self.assertEqual(mw._read_marker(self.marker), before)

    def test_busy_pane_defers_wake_without_failing_or_injecting(self):
        """T-2681: a CONFIRMED-busy pane must DEFER (exit 0), not attempt a doomed
        inject that fails 'submit unconfirmed' and records a FALSE unit failure (8).
        The marker stays put so the next .path fire retries once the pane is idle."""
        self._kicked_state()
        # A newer message makes decide() return 'wake' regardless of pane state.
        mailbox.send(str(self.inbox), payload="p2", sender="specialist", subject="s2")
        before = mw._read_marker(self.marker)
        rc = self._run(self._ok_inject, now=1100.0, pane_idle=False)
        self.assertEqual(rc, 0, "a busy pane defers with exit 0, not a unit failure")
        self.assertEqual(self.injected, [], "must NOT inject into a confirmed-busy pane")
        self.assertEqual(mw._read_marker(self.marker), before,
                         "marker not advanced → the next fire retries when idle")

    def test_idle_pane_inject_failure_still_returns_8(self):
        """T-2681: the busy-skip must NOT swallow a GENUINE failure. An idle pane
        that rejects the submit (or a dead seat, pane_idle unknown) still exits 8
        so the unit's OnFailure fires — only the confirmed-busy collision is benign."""
        self._kicked_state()
        mailbox.send(str(self.inbox), payload="p2", sender="specialist", subject="s2")
        rc = self._run(self._fail_inject, now=1100.0, pane_idle=True)
        self.assertEqual(rc, 8, "an idle pane rejecting the submit is a real anomaly")
        self.assertEqual(len(self.injected), 1, "an idle pane IS injected (then fails)")

    def test_legacy_plaintext_marker_reads_idle_kicked_false(self):
        self.marker.write_text("some-old-name\n", encoding="utf-8")
        self.assertFalse(mw._read_marker(self.marker)["idle_kicked"])

    def test_marker_roundtrips_idle_kicked(self):
        mw._write_marker(self.marker, "n", "S1", 1.0, idle_kicked=True)
        self.assertTrue(mw._read_marker(self.marker)["idle_kicked"])

    # --------------------------------------------------------- the impure probe
    def test_pane_is_idle_returns_none_when_tmux_fails(self):
        """Fail-safe contract: ANY failure is UNKNOWN, never a wake."""
        self.assertIsNone(mw.pane_is_idle("no-such-session-t2158:0"))

    def test_pane_is_idle_returns_none_on_empty_target(self):
        self.assertIsNone(mw.pane_is_idle(""))

    def test_pane_is_idle_wakes_a_turn_ended_pane_with_running_agents(self):
        """T-4284: input submits once the main turn has ended, background agents or
        not. Pins the delivery default (background_agents_busy=False) at this caller."""
        testdata = pathlib.Path(__file__).resolve().parent / "testdata"
        for name in ("recovery_turn_ended_bg_agent_pane.txt", "orchestrator_turn_ended_bg_agents_pane.txt"):
            pane = (testdata / name).read_text(encoding="utf-8")
            done = mw.subprocess.CompletedProcess([], 0, stdout=pane, stderr="")
            with self.subTest(name), mock.patch.object(mw.subprocess, "run", return_value=done):
                self.assertIs(mw.pane_is_idle("x:0"), True)

    def test_pane_is_idle_never_wakes_generation_under_the_plain_footer(self):
        """T-4284: a generating footer can omit "esc to interrupt". Both live specialist
        captures must read BUSY on the wake path (a wake would paste into the turn),
        while specialist's idle capture under the same footer reads idle."""
        import test_kick_verify
        testdata = pathlib.Path(__file__).resolve().parent / "testdata"
        cases = (
            ("specialist 09-28 generating", test_kick_verify.SPECIALIST_WORKING_2026_09_28, False),
            ("specialist 07:40 generating",
             (testdata / "specialist_generating_no_esc_pane.txt").read_text(encoding="utf-8"), False),
            ("specialist 07:41 idle",
             (testdata / "specialist_idle_mode_footer_pane.txt").read_text(encoding="utf-8"), True),
        )
        for name, pane, want in cases:
            done = mw.subprocess.CompletedProcess([], 0, stdout=pane, stderr="")
            with self.subTest(name), mock.patch.object(mw.subprocess, "run", return_value=done):
                self.assertIs(mw.pane_is_idle("x:0"), want)

    def test_pane_is_idle_captures_with_escapes_and_wakes_past_a_dim_suggestion(self):
        """T-4284: artisan sat idle with a DIM prompt suggestion in its composer, and
        every wake deferred as 'pane busy'. The capture must keep attributes (-e) so
        the classifier can drop the suggestion."""
        testdata = pathlib.Path(__file__).resolve().parent / "testdata"
        pane = (testdata / "artisan_idle_ghost_suggestion_pane_ansi.txt").read_text(encoding="utf-8")
        done = mw.subprocess.CompletedProcess([], 0, stdout=pane, stderr="")
        with mock.patch.object(mw.subprocess, "run", return_value=done) as run:
            self.assertIs(mw.pane_is_idle("x:0"), True)
        self.assertIn("-e", run.call_args.args[0])


class QueuedSubjectsInTheKickTest(unittest.TestCase):
    """T-2266 (archivist): the COUNT is the defect. Delivery is FIFO at one message
    per turn, so a correction is authored after the claim it corrects and therefore
    consumed after it — every time. A faster drain narrows that window; it cannot
    close it. "You have 4 unread messages" gives the receiving turn no way to know
    that message 4 retracts message 1."""

    def test_kick_names_what_is_queued_in_order(self):
        text = mw.build_kick("recovery", ["a", "b"], ["orchestrator"],
                             ["orchestrator: CLAIM: the runner is wedged",
                              "orchestrator: RETRACTION: it was queued, not wedged"])
        self.assertIn("RETRACTION: it was queued, not wedged", text,
                      "a correction sitting behind the claim it corrects must be "
                      "visible BEFORE the claim is acted on")
        self.assertIn("CLAIM: the runner is wedged", text)
        self.assertLess(text.index("CLAIM: the runner"),
                        text.index("RETRACTION: it was"),
                        "oldest first — the reader needs the real delivery order")
        self.assertIn("FIFO", text, "say WHY the order matters, or it reads as decoration")

    def test_truncation_is_declared_not_silent(self):
        """A silently clipped list reintroduces exactly the invisibility this fixes."""
        subjects = [f"peer: subject {i}" for i in range(8)]
        text = mw.build_kick("recovery", [str(i) for i in range(20)], ["peer"], subjects)
        self.assertIn("and 12 more", text)

    def test_no_subjects_leaves_the_kick_unchanged(self):
        """Back-compat: the waker must still work when subjects cannot be read."""
        text = mw.build_kick("recovery", ["a"], ["orchestrator"], None)
        self.assertIn("You have 1 unread inter-agent message", text)
        self.assertNotIn("QUEUED", text)

    def test_queued_subjects_reads_sender_and_subject_from_real_files(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as d:
            nd = pathlib.Path(d)
            (nd / "m1").write_text(json.dumps({"sender": "specialist", "subject": "hello"}))
            (nd / "m2").write_text("not json")            # must not break the waker
            got = mw._queued_subjects(nd, ["m1", "m2"])
        self.assertEqual(got, ["specialist: hello"])


class StuckOwnNoticeTest(unittest.TestCase):
    """T-4983: our own wake notice left unsubmitted in a composer deadlocked every later
    wake (UNKNOWN or busy, then defer). Fixtures are built from the 2026-10-15 specialist
    capture: a `›` composer line holding build_kick's text, wrapped over many lines, then
    codex's footer."""

    FOOTER = "  gpt-5.6-sol high · ~/pantheon-config"

    @staticmethod
    def _wrap(prefix, text, width=96):
        import textwrap
        lines = textwrap.wrap(text, width=width)
        return [prefix + lines[0]] + ["  " + ln for ln in lines[1:]]

    def _kick(self):
        return mw.build_kick("codex", ["m1"], ["archivist"], ["archivist: merge notice"])

    def codex_stuck(self, text=None, extra=(), after=()):
        body = self._wrap("› ", text if text is not None else self._kick())
        return "\n".join(["• Worked for 3m 10s", "", *body, "", *extra, self.FOOTER, *after, ""])

    def claude_stuck(self, text=None, footer="  ⏵⏵ bypass permissions on (shift+tab to cycle)"):
        rule = "─" * 80
        body = self._wrap("❯ ", text if text is not None else self._kick())
        return "\n".join(["● Done.", rule, *body, rule, footer, ""])

    # --- the pure detector
    def test_kick_opens_with_marker(self):
        self.assertTrue(self._kick().startswith(mw.WAKE_NOTICE_MARKER))

    def test_codex_stuck_composer_is_ours(self):
        self.assertTrue(mw.stuck_own_notice(self.codex_stuck(), "codex"))

    def test_legacy_opening_is_ours(self):
        legacy = self._kick()[len(mw.WAKE_NOTICE_MARKER):]
        self.assertTrue(mw.stuck_own_notice(self.codex_stuck(legacy), "codex"))

    def test_other_text_in_composer_is_not_ours(self):
        for txt in ("fix the failing test in tools/foo.py",
                    "please quote: " + self._kick(),     # ours, but not at the start
                    "Use /skills to list available skills"):
            with self.subTest(txt=txt[:30]):
                self.assertFalse(mw.stuck_own_notice(self.codex_stuck(txt), "codex"))

    def test_empty_composer_is_not_ours(self):
        pane = "• Worked for 3m\n› Use /skills to list available skills\n" + self.FOOTER + "\n"
        self.assertFalse(mw.stuck_own_notice(pane, "codex"))

    def test_notice_in_history_above_an_empty_composer_is_not_ours(self):
        pane = self.codex_stuck() + "\n› \n" + self.FOOTER + "\n"
        self.assertFalse(mw.stuck_own_notice(pane, "codex"))

    def test_modal_evidence_blocks_recovery(self):
        cases = {
            "confirm": dict(extra=["Enter to confirm · Esc to cancel"]),
            "update picker": dict(extra=["Update available! 0.150 -> 0.151",
                                         "  3. Skip until next version"]),
            "live migration modal": dict(after=pathlib.Path(__file__).resolve().parent
                                         .joinpath("testdata", "codex_migration_modal_live_markdown_pane.txt")
                                         .read_text(encoding="utf-8").splitlines()),
        }
        for name, kw in cases.items():
            with self.subTest(name=name):
                self.assertFalse(mw.stuck_own_notice(self.codex_stuck(**kw), "codex"))

    def test_rate_limit_modal_blocks_recovery(self):
        extra = ["Approaching rate limits", "Keep current model", "Esc to go back"]
        self.assertFalse(mw.stuck_own_notice(self.codex_stuck(extra=extra), "codex"))

    def test_busy_turn_blocks_recovery(self):
        pane = self.codex_stuck(extra=["Working (12s • esc to interrupt)"])
        self.assertFalse(mw.stuck_own_notice(pane, "codex"))

    def test_unsupported_harness_and_empty_pane(self):
        self.assertFalse(mw.stuck_own_notice(self.codex_stuck(), "hermes"))
        self.assertFalse(mw.stuck_own_notice("", "codex"))

    # --- Claude seats: measured with the real classifier
    def test_claude_stuck_composer_reads_busy_not_idle(self):
        """The Claude-seat finding: a notice stuck under `❯` reads pane_idle_state False
        (busy), so the same deadlock reaches Claude seats through the busy deferral."""
        from tools.inbox_consumer import pane_idle_state
        self.assertIs(pane_idle_state(self.claude_stuck()), False)

    def test_claude_stuck_composer_is_ours(self):
        self.assertTrue(mw.stuck_own_notice(self.claude_stuck(), "claude-code"))

    def test_claude_dialog_or_generating_blocks(self):
        dialog = self.claude_stuck(footer="  Esc to cancel · Tab to amend")
        self.assertFalse(mw.stuck_own_notice(dialog, "claude-code"))
        gen = self.claude_stuck(footer="  ⏵⏵ bypass permissions on · esc to interrupt")
        self.assertFalse(mw.stuck_own_notice(gen, "claude-code"))
        plan = self.claude_stuck() + "  ❯ 3. No, keep planning\n"
        self.assertFalse(mw.stuck_own_notice(plan, "claude-code"))


class StuckRecoveryRunTest(unittest.TestCase):
    """T-4983: run() recovers a stuck own notice with one Enter, and alerts when it cannot."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)
        self.marker = self.inbox / mw._MARKER_NAME
        self.state = self.inbox / mw._DEFER_STATE_NAME
        self.injected, self.enters, self.alerts = [], [], []
        self.state_root = Path(self._tmp.name) / "personas"
        p = mock.patch.object(mw.live_harness, "default_state_root",
                              lambda home=None: self.state_root)
        p.start(); self.addCleanup(p.stop)
        mailbox.send(self.inbox, {"x": 1}, sender="archivist")
        self.helper = StuckOwnNoticeTest()
        self.stuck = self.helper.codex_stuck()
        self.clean = "• ok\n› Use /skills to list available skills\n" + StuckOwnNoticeTest.FOOTER + "\n"

    def _run(self, agent="codex", captures=None, idle=None, enter_ok=True, now=1000.0):
        caps = list(captures if captures is not None else [self.stuck, self.clean])

        def capture(pane):
            return caps.pop(0) if len(caps) > 1 else caps[0]

        def enter(pane):
            self.enters.append(pane)
            return enter_ok
        return mw.run(agent, inbox=self.inbox,
                      do_inject=lambda p, t, h: self.injected.append((p, t, h)) or True,
                      session_key_fn=lambda pane: "S1", now=now,
                      pane_idle_fn=lambda pane: idle,
                      unknown_pane_ok_fn=lambda pane, harness: False,
                      capture_fn=capture, enter_fn=enter, sleep_fn=lambda s: None,
                      alert_fn=lambda a, m: self.alerts.append((a, m)))

    def test_stuck_codex_gets_one_enter_and_no_paste(self):
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.enters), 1)
        self.assertEqual(self.injected, [], "recovery is an Enter, never a re-paste")
        self.assertFalse(self.marker.exists(), "mail is not marked woken by a recovery")

    def test_modal_present_defers_without_enter(self):
        pane = self.helper.codex_stuck(extra=["Enter to confirm · Esc to cancel"])
        self.assertEqual(self._run(captures=[pane]), 0)
        self.assertEqual(self.enters, [])
        self.assertEqual(self.injected, [])

    def test_other_text_defers_without_enter(self):
        pane = self.helper.codex_stuck("rewrite the parser please")
        self._run(captures=[pane])
        self.assertEqual(self.enters, [])

    def test_enter_that_does_not_submit_is_counted(self):
        self._run(captures=[self.stuck])
        self.assertEqual(len(self.enters), 1)
        self.assertEqual(json.loads(self.state.read_text())["count"], 1)

    def test_claude_busy_defer_with_stuck_notice_is_recovered(self):
        claude = self.helper.claude_stuck()
        cleared = self.helper.claude_stuck("Try something")
        self._run(agent="recovery", captures=[claude, cleared], idle=False)
        self.assertEqual(len(self.enters), 1)

    def test_claude_genuinely_busy_is_untouched(self):
        busy = self.helper.claude_stuck(footer="  ⏵⏵ bypass permissions on · esc to interrupt")
        self._run(agent="recovery", captures=[busy], idle=False)
        self.assertEqual(self.enters, [])
        self.assertFalse(self.state.exists(), "ordinary busy deferrals are not counted")

    def test_claude_unknown_nonmatching_pane_defers_as_before(self):
        self._run(agent="recovery", captures=["some modal\nEsc to cancel · Tab to amend\n"])
        self.assertEqual(self.enters, [])
        self.assertEqual(self.injected, [])

    # --- the N-defer alert
    def test_alert_fires_once_at_threshold_for_the_same_unread_set(self):
        for i in range(mw.STUCK_ALERT_THRESHOLD + 3):
            self._run(captures=["weird screen nobody classifies\n"], now=1000.0 + 120 * i)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0][0], "codex")

    def test_no_alert_below_threshold(self):
        for i in range(mw.STUCK_ALERT_THRESHOLD - 1):
            self._run(captures=["weird\n"], now=1000.0 + 120 * i)
        self.assertEqual(self.alerts, [])

    def test_new_unread_set_restarts_the_count(self):
        for i in range(mw.STUCK_ALERT_THRESHOLD - 1):
            self._run(captures=["weird\n"])
        mailbox.send(self.inbox, {"x": 2}, sender="orchestrator")
        self._run(captures=["weird\n"])
        self.assertEqual(self.alerts, [])
        self.assertEqual(json.loads(self.state.read_text())["count"], 1)

    def test_successful_wake_clears_the_count(self):
        self._run(captures=["weird\n"])
        self.assertTrue(self.state.exists())
        mw.run("codex", inbox=self.inbox, do_inject=lambda p, t, h: True,
               session_key_fn=lambda pane: "S1", now=2000.0, pane_idle_fn=lambda p: True)
        self.assertFalse(self.state.exists())

    def test_recovery_alert_routes_to_recovery_self_failure_path_not_recovery_inbox(self):
        """A deaf recovery cannot read an alert mailed to recovery. route_failure_alert sends
        agent == recovery to orchestrator's mailbox (recovery-self-failure)."""
        fn = mw._load_route_failure_alert()
        ic = sys.modules[fn.__module__]
        with mock.patch.object(ic, "_persona_is_muted", return_value=False), \
             mock.patch.object(ic, "_recovery_self_alert_decision", return_value=(True, 1)), \
             mock.patch.object(ic, "_deliver_recovery_self_alert_to_orchestrator", return_value=True) as orchestrator, \
             mock.patch.object(ic, "_send_alert_via_recovery") as via_recovery:
            mw.alert_stuck("recovery", "recovery is deaf")
            orchestrator.assert_called_once()
            self.assertEqual(orchestrator.call_args[0][1], mw.STUCK_ALERT_VOCAB)
            via_recovery.assert_not_called()
            mw.alert_stuck("specialist", "specialist is deaf")
            via_recovery.assert_called_once()


class CollapsedPlaceholderTest(unittest.TestCase):
    """T-5047: a composer holding ONLY a collapsed-paste placeholder is our stuck notice
    iff our own last inject here was unconfirmed, recent, and same-session. Also the
    anchor-at-end rule and the alert count. Placeholder shapes per submit_verify
    (Claude "[Pasted text #N +K lines]", codex "[Pasted Content N chars]")."""

    CLAUDE_PH = "[Pasted text #3 +3 lines]"
    CODEX_PH = "[Pasted Content 1020 chars]"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)
        self.record = self.inbox / mw._INJECT_RECORD_NAME
        self.state = self.inbox / mw._DEFER_STATE_NAME
        self.enters, self.alerts, self.injected = [], [], []
        self.state_root = Path(self._tmp.name) / "personas"
        p = mock.patch.object(mw.live_harness, "default_state_root",
                              lambda home=None: self.state_root)
        p.start(); self.addCleanup(p.stop)
        mailbox.send(self.inbox, {"x": 1}, sender="archivist")
        self.h = StuckOwnNoticeTest()

    def claude_ph(self, ph=None, footer="  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)"):
        rule = "\u2500" * 80
        return "\n".join(["\u25cf Done.", rule, "\u276f " + (ph or self.CLAUDE_PH), rule, footer, ""])

    def codex_ph(self, text=None, extra=()):
        return "\n".join(["\u2022 Worked for 3m", "", "\u203a " + (text or self.CODEX_PH), "",
                          *extra, StuckOwnNoticeTest.FOOTER, ""])

    def rec(self, ts=940.0, session="S1", confirmed=False):
        mw.write_inject_record(self.record, ts, session, confirmed)

    def _run(self, agent, pane, idle, now=1000.0, cleared=None):
        caps = [pane, cleared if cleared is not None else
                ("\u276f Try something\n" if agent == "recovery" else "\u203a Use /skills\n")]

        def capture(_):
            return caps.pop(0) if len(caps) > 1 else caps[0]

        def enter(pane_target):
            self.enters.append(pane_target)
            return True
        return mw.run(agent, inbox=self.inbox,
                      do_inject=lambda p, t, h: self.injected.append(t) or True,
                      session_key_fn=lambda _: "S1", now=now,
                      pane_idle_fn=lambda _: idle,
                      unknown_pane_ok_fn=lambda pane_, harness: False,
                      capture_fn=capture, enter_fn=enter, sleep_fn=lambda s: None,
                      alert_fn=lambda a, m: self.alerts.append((a, m)))

    def _count(self):
        return json.loads(self.state.read_text())["count"] if self.state.exists() else 0

    # --- recovery with vouching evidence
    def test_claude_placeholder_with_recent_unconfirmed_inject_gets_one_enter(self):
        self.rec()
        self._run("recovery", self.claude_ph(), idle=False)
        self.assertEqual(len(self.enters), 1)
        self.assertEqual(self.injected, [])
        self.assertFalse(self.record.exists(), "spent evidence is removed")

    def test_codex_placeholder_with_recent_unconfirmed_inject_gets_one_enter(self):
        self.rec()
        self._run("codex", self.codex_ph(), idle=None)
        self.assertEqual(len(self.enters), 1)
        self.assertEqual(self.injected, [])

    # --- no / bad evidence: defer, no Enter, counted
    def test_placeholder_without_record_defers_and_counts(self):
        for agent, pane, idle in (("recovery", self.claude_ph(), False),
                                  ("codex", self.codex_ph(), None)):
            with self.subTest(agent=agent):
                self.state.unlink(missing_ok=True)
                self._run(agent, pane, idle)
                self.assertEqual(self.enters, [])
                self.assertEqual(self._count(), 1)

    def test_placeholder_with_bad_record_defers_and_counts(self):
        cases = {"expired": dict(ts=1000.0 - mw.INJECT_RECORD_TTL_S - 1),
                 "confirmed": dict(confirmed=True),
                 "other session": dict(session="S0"),
                 "future ts": dict(ts=2000.0)}
        for name, kw in cases.items():
            with self.subTest(name=name):
                self.state.unlink(missing_ok=True)
                self.rec(**kw)
                self._run("recovery", self.claude_ph(), idle=False)
                self.assertEqual(self.enters, [])
                self.assertEqual(self._count(), 1)

    def test_ttl_boundary_is_inclusive(self):
        self.rec(ts=1000.0 - mw.INJECT_RECORD_TTL_S)
        self._run("recovery", self.claude_ph(), idle=False)
        self.assertEqual(len(self.enters), 1)

    def test_placeholder_with_dialog_defers_without_enter(self):
        self.rec()
        self._run("recovery", self.claude_ph(footer="  Esc to cancel \u00b7 Tab to amend"), idle=False)
        self._run("codex", self.codex_ph(extra=["Enter to confirm \u00b7 Esc to cancel"]), idle=None)
        self.assertEqual(self.enters, [])

    def test_placeholder_during_a_running_turn_is_untouched_and_not_counted(self):
        self.rec()
        pane = self.claude_ph(footer="  \u23f5\u23f5 bypass permissions on \u00b7 esc to interrupt")
        self._run("recovery", pane, idle=False)
        self.assertEqual(self.enters, [])
        self.assertEqual(self._count(), 0)

    def test_placeholder_plus_typed_text_is_never_submitted(self):
        self.rec()
        self._run("codex", self.codex_ph(self.CODEX_PH + ", the hub must be in your"), idle=None)
        self._run("recovery", self.claude_ph(self.CLAUDE_PH + " and also do X"), idle=False)
        self.assertEqual(self.enters, [])
        self.assertEqual(self._count(), 2, "alert-only: both counted")

    def test_enter_that_leaves_the_placeholder_is_failed(self):
        self.rec()
        out = mw.recover_stuck_notice(
            "recovery", "p", "claude-code", capture_fn=lambda p: self.claude_ph(),
            enter_fn=lambda p: True, sleep_fn=lambda s: None,
            record=mw.read_inject_record(self.record), now=1000.0, session="S1")
        self.assertEqual(out, "failed")

    # --- the inject record
    def test_inject_record_written_confirmed_or_not(self):
        mw.run("codex", inbox=self.inbox, do_inject=lambda p, t, h: True,
               session_key_fn=lambda _: "S1", now=500.0, pane_idle_fn=lambda _: True)
        self.assertEqual(mw.read_inject_record(self.record),
                         {"ts": 500.0, "session": "S1", "confirmed": True})
        mw.run("codex", inbox=self.inbox, do_inject=lambda p, t, h: False,
               session_key_fn=lambda _: "S1", now=600.0, pane_idle_fn=lambda _: True,
               pane_tail_fn=lambda _: [])
        self.assertEqual(mw.read_inject_record(self.record),
                         {"ts": 600.0, "session": "S1", "confirmed": False})

    # --- anchor at the end
    def test_notice_plus_appended_user_text_is_not_ours(self):
        kick = self.h._kick()
        for tail in (" also please fix the build", "\nand then ship it"):
            with self.subTest(tail=tail):
                self.assertFalse(mw.stuck_own_notice(
                    self.h.codex_stuck(kick + tail), "codex"))
                self.assertFalse(mw.stuck_own_notice(
                    self.h.claude_stuck(kick + tail), "claude-code"))

    def test_notice_with_trailing_space_is_still_ours(self):
        self.assertTrue(mw.stuck_own_notice(self.h.codex_stuck(self.h._kick() + " "), "codex"))

    def test_kick_ends_with_the_tail_constant(self):
        self.assertTrue(self.h._kick().endswith(mw.WAKE_NOTICE_TAIL))

    # --- REBOUND 1: footer shape, record trust, one-shot spend
    def _codex_trailing(self, line):
        body = StuckOwnNoticeTest._wrap("\u203a ", self.h._kick())
        return "\n".join(["\u2022 Worked for 3m", "", *body, "", line, ""])

    def test_user_text_with_middle_dot_after_notice_is_not_ours(self):
        for line in ("rm -rf x \u00b7 y", "echo a \u00b7 b", "make deploy now \u00b7 /tmp",
                     "\u00b7", "a \u00b7", "\u00b7 ~/x"):
            with self.subTest(line=line):
                self.assertFalse(mw.stuck_own_notice(self._codex_trailing(line), "codex"))
                self.assertEqual(mw.stuck_composer_state(
                    self._codex_trailing(line), "codex"), None)

    def test_real_codex_footer_after_notice_is_still_ours(self):
        for line in ("  gpt-5.6-sol high \u00b7 ~/pantheon-config",
                     "  gpt-5.6-sol \u00b7 /home/x/y"):
            with self.subTest(line=line):
                self.assertTrue(mw.stuck_own_notice(self._codex_trailing(line), "codex"))

    def _write_raw(self, mode, payload=None):
        self.record.unlink(missing_ok=True)
        payload = payload or {"ts": 940.0, "session": "S1", "confirmed": False}
        self.record.write_text(json.dumps(payload))
        os.chmod(self.record, mode)

    def test_record_with_group_or_other_write_is_rejected(self):
        for mode in (0o660, 0o664, 0o602, 0o666):
            with self.subTest(mode=oct(mode)):
                self.state.unlink(missing_ok=True)
                self._write_raw(mode)
                self.assertIsNone(mw.read_inject_record(self.record))
                self._run("recovery", self.claude_ph(), idle=False)
                self.assertEqual(self.enters, [])
                self.assertEqual(self._count(), 1)

    def test_own_private_record_is_accepted(self):
        self._write_raw(0o600)
        self.assertIsNotNone(mw.read_inject_record(self.record))
        self._write_raw(0o644)  # readable by others is fine; only writability matters
        self.assertIsNotNone(mw.read_inject_record(self.record))

    def test_record_owned_by_another_uid_is_rejected(self):
        self._write_raw(0o600)
        other = os.geteuid() + 1
        with mock.patch.object(mw.os, "geteuid", lambda: other):
            self.assertIsNone(mw.read_inject_record(self.record))

    def test_symlinked_record_is_rejected(self):
        real = self.inbox / "elsewhere.json"
        real.write_text(json.dumps({"ts": 940.0, "session": "S1", "confirmed": False}))
        os.chmod(real, 0o600)
        self.record.unlink(missing_ok=True)
        self.record.symlink_to(real)
        self.assertIsNone(mw.read_inject_record(self.record))
        self._run("recovery", self.claude_ph(), idle=False)
        self.assertEqual(self.enters, [])

    def test_record_with_empty_or_missing_session_never_vouches(self):
        for sess in (None, ""):
            with self.subTest(session=sess):
                self.assertFalse(mw._record_vouches(
                    {"ts": 940.0, "session": sess, "confirmed": False}, 1000.0, sess))
        self.assertFalse(mw._record_vouches({"ts": 940.0, "confirmed": False}, 1000.0, None))
        # end to end: tmux session key unavailable (None) on both sides
        self.rec(session=None)
        mw.run("recovery", inbox=self.inbox, do_inject=lambda p, t, h: True,
               session_key_fn=lambda _: None, now=1000.0, pane_idle_fn=lambda _: False,
               unknown_pane_ok_fn=lambda p, h: False,
               capture_fn=lambda p: self.claude_ph(),
               enter_fn=lambda p: self.enters.append(p) or True,
               sleep_fn=lambda s: None, alert_fn=lambda a, m: None)
        self.assertEqual(self.enters, [])

    def test_first_enter_spends_the_record_even_when_it_fails(self):
        self.rec()
        self._run("recovery", self.claude_ph(), idle=False, cleared=self.claude_ph())
        self.assertEqual(len(self.enters), 1)
        self.assertFalse(self.record.exists(), "spent on the first Enter, not only on recovered")
        # a later human paste inside the TTL window is not submitted on the retry
        self._run("recovery", self.claude_ph(), idle=False, now=1010.0)
        self.assertEqual(len(self.enters), 1)

    def test_unverified_placeholder_sends_no_enter_so_spends_nothing(self):
        self.rec()
        self._run("recovery", self.claude_ph(self.CLAUDE_PH + " and X"), idle=False)
        self.assertEqual(self.enters, [])
        self.assertTrue(self.record.exists(), "no Enter sent, nothing spent")

    # --- the alert counts the collapsed case
    def test_stuck_placeholder_reaches_the_wake_stuck_alert(self):
        for i in range(mw.STUCK_ALERT_THRESHOLD + 2):
            self._run("recovery", self.claude_ph(), idle=False, now=1000.0 + 120 * i)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.alerts[0][0], "recovery")


class SeatMaintenanceGateTest(unittest.TestCase):
    """T-5277: a held seat's mail stays queued; no kick and no wake-stuck streak."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inbox = self.tmp / "box"
        mailbox.provision(self.inbox)
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self.injected, self.alerts = [], []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, held, now=1000.0):
        return mw.run("recovery", inbox=self.inbox,
                      do_inject=lambda p, t, h: self.injected.append(t) or True,
                      session_key_fn=lambda _: None, now=now,
                      pane_idle_fn=lambda _: None,
                      unknown_pane_ok_fn=lambda pane, harness: False,
                      alert_fn=lambda a, m: self.alerts.append((a, m)),
                      maintenance_fn=lambda agent: held)

    def test_held_seat_never_raises_wake_stuck(self):
        for i in range(mw.STUCK_ALERT_THRESHOLD + 3):
            self.assertEqual(self._run(True, now=1000.0 + 120 * i), 0)
        self.assertEqual(self.alerts, [])
        self.assertEqual(self.injected, [])
        self.assertEqual(len(mw.peek(self.inbox / "new")), 1, "mail stays queued for the lift")

    def test_the_same_unknown_pane_unheld_does_raise_wake_stuck(self):
        for i in range(mw.STUCK_ALERT_THRESHOLD + 1):
            self._run(False, now=1000.0 + 120 * i)
        self.assertEqual(len(self.alerts), 1)

    def test_registry_read_is_fail_open(self):
        reg = self.tmp / "reg.json"
        reg.write_text("{not json")
        with mock.patch.dict(os.environ, {"PANTHEON_MAINTENANCE_REGISTRY": str(reg)}):
            self.assertFalse(mw.seat_in_maintenance("mason"))

    def test_an_armed_window_reads_held(self):
        import notify_policy
        reg = self.tmp / "reg.json"
        notify_policy.enter_maintenance("mason", minutes=30, by="test", registry=reg)
        with mock.patch.dict(os.environ, {"PANTHEON_MAINTENANCE_REGISTRY": str(reg)}):
            self.assertTrue(mw.seat_in_maintenance("mason"))
            self.assertFalse(mw.seat_in_maintenance("recovery"))


class SeatIntakeHoldTest(unittest.TestCase):
    """ORG-106 L2 (T-5525): the T-5277 hold generalised through the seat-state reader.
    Q2 / decision 2: paused-intake queues normal mail and still wakes for urgent mail;
    maintenance, the OOM hold and deep-sleep hold even urgent mail."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inbox = self.tmp / "box"
        mailbox.provision(self.inbox)
        self.injected, self.alerts = [], []
        # The waker's environment has the urgent phase-2 flag OFF; prove it.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(mailbox._URGENT_PHASE2_ENV, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self, **kw):
        from tools import seat_state as ss
        sources = kw.pop("sources")
        return ss.EffectiveSeatState(
            state=kw.pop("state", "active"), intake_blocked=kw.pop("intake_blocked", True),
            launch_blocked=kw.pop("launch_blocked", False),
            remediation_vetoed=kw.pop("remediation_vetoed", False),
            expected_down=False, sources=tuple(sources), owner=None, reason=None,
            expires=None, fault=None)

    def _run(self, state, now=1000.0):
        return mw.run("recovery", inbox=self.inbox,
                      do_inject=lambda p, t, h: self.injected.append(t) or True,
                      session_key_fn=lambda _: None, now=now,
                      pane_idle_fn=lambda _: None,
                      unknown_pane_ok_fn=lambda pane, harness: True,
                      alert_fn=lambda a, m: self.alerts.append((a, m)),
                      seat_state_fn=lambda agent: state)

    def _paused(self, sources=("seat-state",)):
        return self._state(sources=sources, state="paused-intake")

    def test_paused_normal_mail_queues(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self.assertEqual(self._run(self._paused()), 0)
        self.assertEqual(self.injected, [])
        self.assertEqual(len(mw.peek(self.inbox / "new")), 1, "mail stays queued")

    def test_paused_urgent_mail_wakes_with_flag_unset(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator", subject="s", severity="urgent")
        self.assertNotIn(mailbox._URGENT_PHASE2_ENV, os.environ)
        self._run(self._paused())
        self.assertEqual(len(self.injected), 1)

    def test_paused_urgent_behind_normal_still_wakes(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator", severity="urgent")
        mailbox.send(self.inbox, {"x": 2}, sender="orchestrator")
        self._run(self._paused())
        self.assertEqual(len(self.injected), 1)

    def test_paused_by_autonomy_off_only_urgent_wakes_normal_queues(self):
        st = self._paused(sources=("autonomy_off",))
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(st)
        self.assertEqual(self.injected, [])
        mailbox.send(self.inbox, {"x": 2}, sender="orchestrator", severity="urgent")
        self._run(st, now=2000.0)
        self.assertEqual(len(self.injected), 1)

    def test_maintenance_oom_and_deep_sleep_hold_even_urgent_mail(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator", severity="urgent")
        cases = {
            "maintenance": self._state(sources=("maintenance",), remediation_vetoed=True),
            "oom-hold": self._state(sources=("oom-hold",), launch_blocked=True),
            "deep-sleep": self._state(sources=("seat-state",), state="deep-sleep",
                                      launch_blocked=True, remediation_vetoed=True),
            "paused+maintenance": self._state(sources=("maintenance", "seat-state"),
                                              remediation_vetoed=True),
        }
        for name, st in cases.items():
            with self.subTest(name):
                self.assertEqual(self._run(st), 0)
                self.assertEqual(self.injected, [])
                self.assertEqual(len(mw.peek(self.inbox / "new")), 1)

    def test_held_seat_never_raises_wake_stuck(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        for i in range(mw.STUCK_ALERT_THRESHOLD + 3):
            self._run(self._paused(), now=1000.0 + 120 * i)
        self.assertEqual(self.alerts, [])

    def test_not_held_wakes_as_before(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(None)
        self.assertEqual(len(self.injected), 1)

    def test_seat_hold_reader_is_fail_open(self):
        with mock.patch("tools.seat_state.effective_for", side_effect=RuntimeError("x")):
            self.assertIsNone(mw.seat_hold("recovery"))

    def test_seat_hold_reads_the_record(self):
        from tools import seat_state as ss
        c = ss.Contribution(source="seat-state", state="paused-intake", intake_blocked=True)
        with mock.patch("tools.seat_state._read_seat_state", return_value=c), \
                mock.patch("tools.seat_state._read_seat_state_cross", return_value=c):
            st = mw.seat_hold("recovery")
        self.assertTrue(st.intake_blocked)
        self.assertEqual(st.sources, ("seat-state",))


class DuplicatedCodexComposerTest(unittest.TestCase):
    """T-4987 (a), the watchman 2026-10-22 specimen: the codex composer rendered twice,
    the second copy below the footer (scrolled by one row, cut off at the pane bottom).
    The own-notice recovery read the duplicate as the composer, saw no marker, and never
    fired: 12 UNKNOWN defers, then the wake-stuck alert. Fixture: SYNTHETIC, derived from
    the real watchman capture watchman_idle_2026-10-15.txt (see
    test_submit_verify.derive_duplicated_composer, which pins the derivation)."""

    FIX = Path(__file__).resolve().parent / "fixtures" / "codex_panes"
    FOOTER = "  gpt-5.6-sol high · ~/pantheon-config"

    def setUp(self):
        self.pane = (self.FIX / "watchman_duplicated_composer_2026-10-22_synthetic.txt"
                     ).read_text(encoding="utf-8")
        self.notice = (self.FIX / "watchman_wake_notice_2026-10-22_synthetic.injected.txt"
                       ).read_text(encoding="utf-8")
        self.idle = (self.FIX / "watchman_idle_2026-10-15.txt").read_text(encoding="utf-8")
        self.above, self.below = self.pane.split(self.FOOTER, 1)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)
        self.state = self.inbox / mw._DEFER_STATE_NAME
        self.enters, self.injected, self.alerts = [], [], []
        p = mock.patch.object(mw.live_harness, "default_state_root",
                              lambda home=None: Path(self._tmp.name) / "personas")
        p.start(); self.addCleanup(p.stop)
        mailbox.send(self.inbox, {"x": 1}, sender="inbox_consumer")

    def _run(self, captures, idle=None):
        caps = list(captures)

        def capture(_):
            return caps.pop(0) if len(caps) > 1 else caps[0]

        def enter(pane_target):
            self.enters.append(pane_target)
            return True
        return mw.run("watchman", inbox=self.inbox,
                      do_inject=lambda p, t, h: self.injected.append(t) or True,
                      session_key_fn=lambda _: "S1", now=1000.0,
                      pane_idle_fn=lambda _: idle,
                      unknown_pane_ok_fn=lambda pane_, harness: False,
                      capture_fn=capture, enter_fn=enter, sleep_fn=lambda s: None,
                      alert_fn=lambda a, m: self.alerts.append(m),
                      maintenance_fn=lambda a: False,
                      route_fn=lambda *a: ("off", "test"))

    # --- the pure detector
    def test_duplicated_composer_is_ours(self):
        self.assertEqual(mw.stuck_composer_state(self.pane, "codex"), "ours")

    def test_real_watchman_idle_is_not_ours(self):
        self.assertIsNone(mw.stuck_composer_state(self.idle, "codex"))

    def test_fragment_below_footer_without_notice_above_is_not_ours(self):
        # The live composer is codex's placeholder: an Enter there could run it.
        pane = self.idle.split(self.FOOTER, 1)[0] + self.FOOTER + self.below
        self.assertIsNone(mw.stuck_composer_state(pane, "codex"))

    def test_foreign_text_below_footer_is_not_ours(self):
        for extra in ("› rm -rf build and push to main please\n",
                      "› (from inbox_consumer).\n  and also delete the branch\n"):
            with self.subTest(extra=extra[:20]):
                pane = self.above + self.FOOTER + "\n" + extra
                self.assertIsNone(mw.stuck_composer_state(pane, "codex"))

    def test_user_text_after_notice_above_footer_is_not_ours(self):
        pane = self.above.rstrip("\n") + "\n  and then rm -rf ~\n\n" + self.FOOTER + self.below
        self.assertIsNone(mw.stuck_composer_state(pane, "codex"))

    def test_modal_or_busy_evidence_blocks(self):
        for extra in ("Enter to confirm · Esc to cancel",
                      "◦ Working (12s • esc to interrupt)"):
            with self.subTest(extra=extra[:12]):
                self.assertIsNone(mw.stuck_composer_state(self.pane + extra + "\n", "codex"))

    def test_submitted_then_redrawn_is_not_ours(self):
        pane = self.pane + "\n› Improve documentation in @filename\n\n" + self.FOOTER + "\n"
        self.assertIsNone(mw.stuck_composer_state(pane, "codex"))

    def test_claude_harness_unchanged(self):
        self.assertIsNone(mw.stuck_composer_state(self.pane.replace("›", "❯"),
                                                  "claude-code"))

    # --- run(): the UNKNOWN path recovers with one Enter
    def test_unknown_pane_recovers_with_one_enter_and_no_paste(self):
        self.assertEqual(self._run([self.pane, self.idle]), 0)
        self.assertEqual(len(self.enters), 1)
        self.assertEqual(self.injected, [])
        self.assertFalse(self.state.exists(), "a recovery is not a counted deferral")

    def test_enter_that_leaves_the_duplicate_is_counted_failed(self):
        self._run([self.pane, self.pane])
        self.assertEqual(len(self.enters), 1)
        self.assertEqual(json.loads(self.state.read_text())["count"], 1)


if __name__ == "__main__":
    unittest.main()
