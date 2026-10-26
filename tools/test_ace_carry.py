"""Tests for tools/ace_carry.py (T-5349): ACE phase-1 advisory carry-forward
with the C3 7-cycle expiry and the once-only HO surface.

Covers: carry on drop, re-confirm clears, done retires, expiry at exactly
N=7 cycles, the once-only surface across repeated runs, no live send under
test, corrupt/missing state recovery, a missing tickets root, the path
guard, never writing HANDOFF.md, and the opt-in off path being a no-op.

Run: python3 -m pytest tools/test_ace_carry.py
Fixtures are built in tmp dirs (no seat-home or /tmp reads).
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ace_carry as cf  # noqa: E402

PREV = """\
# UPDATE 2026-10-21
- T-100 PUSHED, awaiting merge (agent/T-100).
- T-200 next: wire the probe.
- T-300 resolved, nothing to do.
- Plain prose bullet with no ticket.

## Basics
- RULE: always gate before push.
"""

# The rewrite drops the T-100 and T-200 bullets and the prose bullet.
NEW_DROP = """\
# UPDATE 2026-10-22
- Started T-400.

## Basics
- RULE: always gate before push.
"""


def make_tickets(root: Path, inbox=(), active=(), done=()) -> Path:
    for sub, ids in (("inbox", inbox), ("active", active), ("done", done)):
        d = root / sub
        d.mkdir(parents=True, exist_ok=True)
        for t in ids:
            (d / f"{t}.md").write_text(f"---\nid: {t}\n---\n", encoding="utf-8")
    return root


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.tickets = make_tickets(self.tmp / "tickets", inbox=["T-200"], active=["T-100", "T-400"],
                                    done=["T-300"])
        self.out = self.tmp / "out"
        self.out.mkdir()
        # Chronicle root pointed into the tmp dir so nothing reaches seat state.
        self._env = mock.patch.dict(os.environ, {
            "PANTHEON_CHRONICLE_ROOT": str(self.tmp / "chronicle"),
            "PANTHEON_ACE_CARRY": "",
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._td.cleanup()

    def sets(self):
        return cf.open_ticket_set(self.tickets), cf.done_ticket_set(self.tickets)


# ---------------------------------------------------------------- state machine
class StepTests(_Tmp):
    def test_open_set_is_inbox_and_active_minus_done(self):
        make_tickets(self.tickets, done=["T-200"])  # T-200 in inbox AND done -> done wins
        self.assertEqual(cf.open_ticket_set(self.tickets), {"T-100", "T-400"})

    def test_carry_on_drop(self):
        st, rep = cf.step(cf.new_state(), PREV, NEW_DROP, *self.sets())
        ids = sorted(t for it in st["items"] for t in it["ids"])
        self.assertEqual(ids, ["T-100", "T-200"])
        self.assertEqual(len(rep["carried_new"]), 2)
        for it in st["items"]:
            self.assertEqual(it["first_cycle"], 1)
        # terminal T-300 and the un-ticketed prose bullet are not carried
        texts = " ".join(it["text"] for it in st["items"])
        self.assertNotIn("T-300", texts)
        self.assertNotIn("Plain prose", texts)

    def test_not_carried_when_id_still_in_new_text(self):
        new = NEW_DROP + "- T-100 still pending, reworded.\n"
        st, _ = cf.step(cf.new_state(), PREV, new, *self.sets())
        self.assertEqual(sorted(t for it in st["items"] for t in it["ids"]), ["T-200"])

    def test_reconfirm_clears_silently(self):
        st, _ = cf.step(cf.new_state(), PREV, NEW_DROP, *self.sets())
        new2 = NEW_DROP + "- T-200 picked up again.\n"
        st, rep = cf.step(st, NEW_DROP, new2, *self.sets())
        self.assertEqual([t for it in st["items"] for t in it["ids"]], ["T-100"])
        self.assertEqual(len(rep["reconfirmed"]), 1)
        self.assertEqual(rep["retired"], [])
        self.assertEqual(rep["expired"], [])

    def test_done_retires_with_reason(self):
        st, _ = cf.step(cf.new_state(), PREV, NEW_DROP, *self.sets())
        make_tickets(self.tickets, done=["T-100"])
        (self.tickets / "active" / "T-100.md").unlink()
        st, rep = cf.step(st, NEW_DROP, NEW_DROP, *self.sets())
        self.assertEqual([t for it in st["items"] for t in it["ids"]], ["T-200"])
        self.assertEqual(len(rep["retired"]), 1)
        self.assertEqual(rep["retired"][0]["reason"], "ticket done")

    def test_expiry_at_exactly_seven_cycles(self):
        st, _ = cf.step(cf.new_state(), PREV, NEW_DROP, *self.sets())  # cycle 1, age 1
        for cycle in range(2, 8):  # cycles 2..7: still carried, ages 2..7
            st, rep = cf.step(st, NEW_DROP, NEW_DROP + f"<!-- {cycle} -->\n", *self.sets())
            self.assertEqual(rep["expired"], [], f"expired early at cycle {cycle}")
            self.assertEqual(len(st["items"]), 2)
            self.assertEqual({cf.age(st, it) for it in st["items"]}, {cycle})
        st, rep = cf.step(st, NEW_DROP, NEW_DROP + "<!-- 8 -->\n", *self.sets())  # 8th cycle
        self.assertEqual(len(rep["expired"]), 2)
        self.assertEqual(st["items"], [])
        self.assertEqual(len(st["pending_surface"]), 2)

    def test_expiry_threshold_is_seven(self):
        self.assertEqual(cf.EXPIRY_CYCLES, 7)

    def test_same_input_twice_is_not_a_new_cycle(self):
        st, _ = cf.step(cf.new_state(), PREV, NEW_DROP, *self.sets())
        st2, rep = cf.step(st, PREV, NEW_DROP, *self.sets())
        self.assertTrue(rep["noop"])
        self.assertEqual(st2["cycle"], 1)

    def test_expired_item_is_not_recarried(self):
        st = cf.new_state()
        st, _ = cf.step(st, PREV, NEW_DROP, *self.sets())
        for c in range(2, 9):
            st, _ = cf.step(st, NEW_DROP, NEW_DROP + f"<!-- {c} -->\n", *self.sets())
        cf.surface_expired(st, "artisan", sender=lambda *a, **k: "id-1")
        # the seat re-writes the same bullets, then drops them again
        st, _ = cf.step(st, NEW_DROP + "<!-- 8 -->\n", PREV, *self.sets())
        st, rep = cf.step(st, PREV, NEW_DROP, *self.sets())
        self.assertEqual(st["items"], [])
        self.assertEqual(rep["carried_new"], [])


# --------------------------------------------------------------------- surface
class SurfaceTests(_Tmp):
    def _expire_two(self):
        st, _ = cf.step(cf.new_state(), PREV, NEW_DROP, *self.sets())
        for c in range(2, 9):
            st, _ = cf.step(st, NEW_DROP, NEW_DROP + f"<!-- {c} -->\n", *self.sets())
        return st

    def test_once_only_across_repeated_runs(self):
        st = self._expire_two()
        calls = []

        def sender(*a, **k):
            calls.append((a, k))
            return "msg-1"

        self.assertEqual(cf.surface_expired(st, "artisan", sender=sender), 2)
        for c in range(9, 14):
            st, _ = cf.step(st, NEW_DROP, NEW_DROP + f"<!-- {c} -->\n", *self.sets())
            self.assertEqual(cf.surface_expired(st, "artisan", sender=sender), 0)
        self.assertEqual(len(calls), 1, "one batched message, never re-sent")
        self.assertEqual(st["pending_surface"], [])
        self.assertEqual(len(st["surfaced"]), 2)

    def test_failed_send_retries_next_run_then_stops(self):
        st = self._expire_two()

        def boom(*a, **k):
            raise OSError("hub down")

        self.assertEqual(cf.surface_expired(st, "artisan", sender=boom), 0)
        self.assertEqual(len(st["pending_surface"]), 2)
        calls = []
        cf.surface_expired(st, "artisan", sender=lambda *a, **k: calls.append(1) or "ok")
        cf.surface_expired(st, "artisan", sender=lambda *a, **k: calls.append(1) or "ok")
        self.assertEqual(len(calls), 1)

    def test_no_live_send_under_test(self):
        st = self._expire_two()
        with mock.patch.object(cf, "_mailbox_module") as mm:
            sent = cf.surface_expired(st, "artisan")  # default live sender
            mm.assert_not_called()
        self.assertEqual(sent, 0)
        self.assertTrue(cf.live_send_suppressed())

    def test_suppression_env_override(self):
        with mock.patch.dict(os.environ, {"PANTHEON_DISABLE_LIVE_TELEGRAM": "1"}):
            self.assertTrue(cf.live_send_suppressed())

    def test_message_goes_to_orchestrator(self):
        st = self._expire_two()
        seen = {}

        def sender(inbox, payload, sender_name, **k):
            seen.update(inbox=str(inbox), payload=payload, sender=sender_name, **k)
            return "m"

        cf.surface_expired(st, "artisan", sender=sender)
        self.assertTrue(seen["inbox"].endswith("/orchestrator"))
        self.assertIn("T-100", seen["payload"])
        self.assertIn("T-200", seen["payload"])
        self.assertIn("artisan", seen["subject"])


# ------------------------------------------------------------------------- CLI
class CliTests(_Tmp):
    def run_cli(self, *extra, prev=PREV, new=NEW_DROP, sender=None):
        pf = self.tmp / "prev.md"
        pf.write_text(prev, encoding="utf-8")
        hf = self.tmp / "HANDOFF.md"
        hf.write_text(new, encoding="utf-8")
        err, out = io.StringIO(), io.StringIO()
        argv = ["run", "--handoff-file", str(hf), "--prev-file", str(pf), "--agent", "artisan",
                "--tickets-root", str(self.tickets), "--force", *extra]
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = cf.main(argv, sender=sender)
        return rc, out.getvalue(), err.getvalue(), hf

    def test_out_dir_outputs(self):
        rc, out, err, _ = self.run_cli("--out-dir", str(self.out))
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["carry-forward.md", "carry-state.json", "retire-list.md"])
        cfm = (self.out / "carry-forward.md").read_text(encoding="utf-8")
        self.assertIn("## Carried forward (advisory)", cfm)
        self.assertIn("T-100", cfm)
        self.assertIn("1/7", cfm)
        st = json.loads((self.out / "carry-state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["cycle"], 1)

    def test_expired_record_written_and_state_carries_across_runs(self):
        self.run_cli("--out-dir", str(self.out))
        calls = []
        snd = lambda *a, **k: calls.append(1) or "m"  # noqa: E731
        prev = NEW_DROP
        for c in range(2, 9):
            new = NEW_DROP + f"<!-- {c} -->\n"
            rc, _, err, _ = self.run_cli("--out-dir", str(self.out), prev=prev, new=new, sender=snd)
            self.assertEqual(rc, 0, err)
            prev = new
        exp = (self.out / "expired-carry.md").read_text(encoding="utf-8")
        self.assertIn("T-100", exp)
        self.assertEqual(len(calls), 1)
        rc, _, _, _ = self.run_cli("--out-dir", str(self.out), prev=prev, new=NEW_DROP + "x\n", sender=snd)
        self.assertEqual(len(calls), 1)

    def test_chronicle_companions(self):
        rc, _, err, _ = self.run_cli("--ts", "2026-10-22T09:00:00Z", "--uuid", "abc")
        self.assertEqual(rc, 0, err)
        day = self.tmp / "chronicle" / "artisan" / "2026-10-22"
        names = sorted(p.name for p in day.iterdir())
        self.assertEqual(names, ["2026-10-22T090000Z-abc.carry-forward.md",
                                 "2026-10-22T090000Z-abc.carry-state.json",
                                 "2026-10-22T090000Z-abc.retire-list.md"])
        # the next cycle reads the latest state companion back
        rc, _, err, _ = self.run_cli("--ts", "2026-10-22T10:00:00Z", "--uuid", "def",
                                     prev=NEW_DROP, new=NEW_DROP + "y\n")
        st = json.loads((day / "2026-10-22T100000Z-def.carry-state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["cycle"], 2)
        self.assertEqual(len(st["items"]), 2)

    def test_corrupt_state_recovers(self):
        (self.out / "carry-state.json").write_text("{not json", encoding="utf-8")
        rc, _, err, _ = self.run_cli("--out-dir", str(self.out))
        self.assertEqual(rc, 0)
        self.assertIn("starting fresh", err)
        st = json.loads((self.out / "carry-state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["cycle"], 1)

    def test_wrong_shape_state_recovers(self):
        (self.out / "carry-state.json").write_text('{"items": "nope"}', encoding="utf-8")
        rc, _, err, _ = self.run_cli("--out-dir", str(self.out))
        self.assertEqual(rc, 0)
        self.assertIn("starting fresh", err)

    def test_missing_state_is_fresh(self):
        rc, _, err, _ = self.run_cli("--out-dir", str(self.out))
        self.assertEqual((rc, err), (0, ""))

    def test_missing_tickets_root(self):
        err, out = io.StringIO(), io.StringIO()
        hf = self.tmp / "HANDOFF.md"
        hf.write_text(NEW_DROP, encoding="utf-8")
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = cf.main(["run", "--handoff-file", str(hf), "--agent", "artisan", "--force",
                          "--tickets-root", str(self.tmp / "nope"), "--out-dir", str(self.out)])
        self.assertEqual(rc, 0)
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_missing_handoff_file(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cf.main(["run", "--handoff-file", str(self.tmp / "absent.md"), "--agent", "artisan",
                          "--force", "--tickets-root", str(self.tickets), "--out-dir", str(self.out)])
        self.assertEqual(rc, 0)
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)

    def test_bad_args_never_raise(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cf.main(["run", "--no-such-flag"])
        self.assertEqual(rc, 0)

    def test_internal_exception_returns_zero_one_line(self):
        with mock.patch.object(cf, "step", side_effect=RuntimeError("kaboom")):
            rc, _, err, _ = self.run_cli("--out-dir", str(self.out))
        self.assertEqual(rc, 0)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("kaboom", err)

    def test_path_guard_refuses_canonical_out_dir(self):
        for bad in ("tools", "tickets", "rulebook", "memory"):
            d = self.tmp / bad
            d.mkdir(exist_ok=True)
            before = sorted(d.iterdir())
            rc, _, err, _ = self.run_cli("--out-dir", str(d))
            self.assertEqual(rc, 0)
            self.assertIn("refusing", err)
            self.assertEqual(sorted(d.iterdir()), before, bad)

    def test_path_guard_unit(self):
        with self.assertRaises(PermissionError):
            cf.guarded_path(self.out / "HANDOFF.md", [self.out])
        with self.assertRaises(PermissionError):
            cf.guarded_path(self.tmp / "elsewhere" / "carry-forward.md", [self.out])

    def test_never_writes_handoff(self):
        rc, _, _, hf = self.run_cli("--out-dir", str(self.out))
        before = hf.read_bytes(), hf.stat().st_mtime_ns
        rc, _, _, _ = self.run_cli("--out-dir", str(self.tmp), new=NEW_DROP)  # out-dir == HANDOFF's dir
        self.assertEqual(hf.read_text(encoding="utf-8"), NEW_DROP)
        self.assertNotIn("HANDOFF.md", [p.name for p in self.out.iterdir()])
        self.assertEqual(before[0], NEW_DROP.encode())

    def test_no_send_when_cli_runs_under_test(self):
        prev = PREV
        with mock.patch.object(cf, "_mailbox_module") as mm:
            for c in range(1, 10):
                new = NEW_DROP + f"<!-- {c} -->\n"
                self.run_cli("--out-dir", str(self.out), prev=prev, new=new)
                prev = new
            mm.assert_not_called()


class PersistOrderTests(_Tmp):
    """Review r1 M1 / L1 / L4: persist pending -> send -> persist surfaced."""

    def setUp(self):
        super().setUp()
        self.calls = []

    def sender(self, inbox, payload, name, **kw):
        self.calls.append((kw.get("subject"), payload))
        return f"m{len(self.calls)}"

    def cycle(self, prev, new, **kw):
        pf, hf = self.tmp / "prev.md", self.tmp / "HANDOFF.md"
        pf.write_text(prev, encoding="utf-8")
        hf.write_text(new, encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cf.main(["run", "--handoff-file", str(hf), "--prev-file", str(pf), "--agent", "artisan",
                          "--tickets-root", str(self.tickets), "--force", "--out-dir", str(self.out)],
                         sender=kw.get("sender", self.sender))
        self.assertEqual(rc, 0)
        return err.getvalue()

    def age_to_eve_of_expiry(self):
        """Run cycles 1..7 (nothing expires yet); return the last new text."""
        self.cycle(PREV, NEW_DROP)
        prev = NEW_DROP
        for c in range(2, 8):
            new = NEW_DROP + f"<!-- {c} -->\n"
            self.cycle(prev, new)
            prev = new
        self.assertEqual(self.calls, [])
        return prev

    def _fail_state_write(self, which):
        """Patch _write_plain so the ``which``-th carry-state.json write of the
        run raises (1 = the pre-send persist, 2 = the post-send persist)."""
        real, n = cf._write_plain, {"k": 0}

        def flaky(dest, text):
            if dest.name == "carry-state.json":
                n["k"] += 1
                if n["k"] == which:
                    raise OSError("disk full")
            return real(dest, text)
        return mock.patch.object(cf, "_write_plain", flaky)

    def test_first_write_fails_no_send(self):
        prev = self.age_to_eve_of_expiry()
        with self._fail_state_write(1):
            err = self.cycle(prev, NEW_DROP + "<!-- 8 -->\n")
        self.assertEqual(self.calls, [])
        self.assertIn("nothing sent", err)

    def test_send_lands_then_write_fails_at_most_one_identical_resend(self):
        """Design: the batch is persisted pending (attempts=1) before the send.
        If the post-send write fails, the next run resends ONCE, byte-identical
        subject + payload (stable batch id, so mailbox pending-dedupe can
        collapse it); after that the batch is closed and never sent again."""
        prev = self.age_to_eve_of_expiry()
        with self._fail_state_write(2):
            err = self.cycle(prev, NEW_DROP + "<!-- 8 -->\n")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("not recorded", err)
        self.cycle(NEW_DROP + "<!-- 8 -->\n", NEW_DROP + "<!-- 9 -->\n")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0], self.calls[1])
        self.assertRegex(self.calls[0][0], r"\[batch [0-9a-f]{10}\]")
        for c in range(10, 14):
            self.cycle(NEW_DROP + f"<!-- {c - 1} -->\n", NEW_DROP + f"<!-- {c} -->\n")
        self.assertEqual(len(self.calls), 2)

    def test_post_send_write_failing_every_time_is_bounded(self):
        prev = self.age_to_eve_of_expiry()
        with self._fail_state_write(2):
            for c in range(8, 16):
                new = NEW_DROP + f"<!-- {c} -->\n"
                self.cycle(prev, new)
                prev = new
        self.assertLessEqual(len(self.calls), cf.MAX_SEND_ATTEMPTS)

    def test_failed_send_does_not_use_up_an_attempt(self):
        """A known pre-delivery failure (missing inbox) gives the attempt back."""
        prev = self.age_to_eve_of_expiry()
        mnf = cf._mailbox_module().MailboxNotFound

        def boom(*a, **k):
            raise mnf("hub moved")
        for c in range(8, 12):
            new = NEW_DROP + f"<!-- {c} -->\n"
            self.cycle(prev, new, sender=boom)
            prev = new
        self.cycle(prev, NEW_DROP + "<!-- z -->\n")
        self.assertEqual(len(self.calls), 1)

    def test_send_lands_then_raises_every_cycle_is_bounded(self):
        """r2 review MED: mailbox.send can raise AFTER the rename into new/
        (_fsync_dir). That outcome is unknown, so the attempt stays counted:
        at most MAX_SEND_ATTEMPTS sends, then the batch is closed and listed."""
        prev = self.age_to_eve_of_expiry()

        def lands_then_raises(*a, **k):
            self.calls.append((k.get("subject"), a[1]))
            raise OSError("EIO after rename")
        errs = []
        for c in range(8, 28):
            new = NEW_DROP + f"<!-- {c} -->\n"
            errs.append(self.cycle(prev, new, sender=lands_then_raises))
            prev = new
        self.assertEqual(len(self.calls), 2)
        self.assertIn("delivery unknown, attempt kept", errs[0])
        self.assertEqual(sum("surfaced-unconfirmed" in e for e in errs), 1, "closed once, never re-closed")

    def test_unconfirmed_closure_listed_in_expired_carry(self):
        prev = self.age_to_eve_of_expiry()

        def lands_then_raises(*a, **k):
            self.calls.append(1)
            raise OSError("EIO after rename")
        for c in (8, 9):
            new = NEW_DROP + f"<!-- {c} -->\n"
            self.cycle(prev, new, sender=lands_then_raises)
            prev = new
        (self.out / "expired-carry.md").unlink()
        err = self.cycle(prev, NEW_DROP + "<!-- 10 -->\n", sender=lands_then_raises)  # the closing cycle
        self.assertEqual(len(self.calls), 2)
        self.assertIn("surfaced-unconfirmed", err)
        exp = (self.out / "expired-carry.md").read_text(encoding="utf-8")
        self.assertIn("## Closed without a confirmed send", exp)
        self.assertIn("T-100", exp)
        self.assertIn("T-200", exp)
        self.assertRegex(exp, r"\[batch [0-9a-f]{10} · 2 attempts")

    def test_pre_delivery_classification(self):
        mnf = cf._mailbox_module().MailboxNotFound
        self.assertTrue(cf._pre_delivery(ValueError("bad sender")))
        self.assertTrue(cf._pre_delivery(TypeError("not json")))
        self.assertTrue(cf._pre_delivery(mnf("gone")))
        self.assertFalse(cf._pre_delivery(OSError("EIO")))
        self.assertFalse(cf._pre_delivery(FileNotFoundError("tmp vanished")))

    def test_suppressed_outside_tests_says_so(self):
        real = cf._in_test_context
        err = io.StringIO()
        with mock.patch.object(cf, "_in_test_context", lambda: False), \
                mock.patch.dict(os.environ, {"PANTHEON_DISABLE_LIVE_TELEGRAM": "1"}), \
                mock.patch.object(cf, "_mailbox_module") as mm, contextlib.redirect_stderr(err):
            self.assertIsNone(cf._live_sender("/x/orchestrator", "p", "ace_carry", subject="s"))
            mm.assert_not_called()
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertIn("suppressed", err.getvalue())
        err2 = io.StringIO()
        with contextlib.redirect_stderr(err2):
            self.assertTrue(real())
            cf._live_sender("/x/orchestrator", "p", "ace_carry", subject="s")  # under test: silent
        self.assertEqual(err2.getvalue(), "")

    def test_resend_collapses_in_a_real_mailbox(self):
        sys.path.insert(0, str(Path(cf.__file__).resolve().parent))
        mb = cf._mailbox_module()
        inbox = self.tmp / "hub" / "orchestrator"
        mb.provision(inbox, mode=0o700)
        st, _ = cf.step(cf.new_state(), PREV, NEW_DROP, *self.sets())
        for c in range(2, 9):
            st, _ = cf.step(st, NEW_DROP, NEW_DROP + f"<!-- {c} -->\n", *self.sets())
        bid, rows = cf.plan_surface(st)
        env = {"PANTHEON_ORCHESTRATOR_MAILBOX": str(inbox), "CLAUDE_CODE_SESSION_ID": ""}
        with mock.patch.dict(os.environ, env):
            m1, _ = cf.send_batch(st, "artisan", bid, rows, sender=mb.send)
            m2, _ = cf.send_batch(st, "artisan", bid, rows, sender=mb.send)  # crash-resend, still unconsumed
        self.assertEqual(m1, m2)
        self.assertEqual(len(list((inbox / "new").iterdir())), 1)

    def test_unreadable_newer_state_withholds_send(self):
        """L1, Chronicle mode: the newest state companion is corrupt, so the
        run falls back to the older one and does not send that cycle."""
        def run(ts, prev, new):
            pf, hf = self.tmp / "prev.md", self.tmp / "HANDOFF.md"
            pf.write_text(prev, encoding="utf-8")
            hf.write_text(new, encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                cf.main(["run", "--handoff-file", str(hf), "--prev-file", str(pf), "--agent", "artisan",
                         "--tickets-root", str(self.tickets), "--force", "--ts", ts, "--uuid", "u"],
                        sender=self.sender)
            return err.getvalue()
        run("2026-10-01T00:00:00Z", PREV, NEW_DROP)
        prev = NEW_DROP
        for c in range(2, 8):
            new = NEW_DROP + f"<!-- {c} -->\n"
            run(f"2026-10-{c:02d}T00:00:00Z", prev, new)
            prev = new
        day = self.tmp / "chronicle" / "artisan" / "2026-10-07"
        (day / "2026-10-07T000000Z-u.carry-state.json.2").write_text("{torn", encoding="utf-8")
        err = run("2026-10-08T00:00:00Z", prev, NEW_DROP + "<!-- 8 -->\n")
        self.assertEqual(self.calls, [])
        self.assertEqual(sum("withheld" in ln for ln in err.splitlines()), 1)
        self.assertIn("skipped unreadable newer state", err)
        err = run("2026-10-09T00:00:00Z", NEW_DROP + "<!-- 8 -->\n", NEW_DROP + "<!-- 9 -->\n")
        self.assertEqual(len(self.calls), 1)

    def test_state_companion_order_counts_numerically(self):
        day = self.tmp / "c" / "2026-10-01"
        day.mkdir(parents=True)
        for suf in ("", ".2", ".10", ".3"):
            (day / f"2026-10-01T000000Z-u.carry-state.json{suf}").write_text("{}", encoding="utf-8")
        names = [p.name.rsplit(".json", 1)[1] for p in cf._state_companions(self.tmp / "c")]
        self.assertEqual(names, [".10", ".3", ".2", ""])

    def test_mail_capped_at_fifty(self):
        st = cf.new_state()
        st["cycle"] = 9
        st["pending_surface"] = [{"key": f"k{i:03d}", "ids": [f"T-{1000 + i}"], "text": f"- T-{1000 + i} item",
                                  "first_cycle": 1, "expired_cycle": 9, "carried_cycles": 8}
                                 for i in range(57)]
        cf.surface_expired(st, "artisan", sender=self.sender)
        body = self.calls[0][1]
        self.assertEqual(body.count("\n- T-"), 50)
        self.assertIn("and 7 more, see expired-carry.md", body)
        self.assertEqual(len(st["surfaced"]), 57)


class OptInTests(_Tmp):
    def _yaml(self, flag):
        p = self.tmp / "instance.yaml"
        extra = "    ace_carry_forward: true\n" if flag else ""
        p.write_text("roles:\n  eo2:\n    persona: artisan\n    account: suborch2\n" + extra
                     + "  eo1:\n    persona: mason\n    account: suborch1\n", encoding="utf-8")
        return p

    def test_opted_in_reads_instance_yaml(self):
        y = self._yaml(True)
        self.assertTrue(cf.opted_in("artisan", y))
        self.assertFalse(cf.opted_in("mason", y))
        self.assertFalse(cf.opted_in("artisan", self._yaml(False)))
        self.assertFalse(cf.opted_in("artisan", self.tmp / "missing.yaml"))

    def test_env_kill_switch_and_force(self):
        y = self._yaml(True)
        with mock.patch.dict(os.environ, {"PANTHEON_ACE_CARRY": "off"}):
            self.assertFalse(cf.opted_in("artisan", y))
        with mock.patch.dict(os.environ, {"PANTHEON_ACE_CARRY": "on"}):
            self.assertTrue(cf.opted_in("mason", y))

    def test_live_manifest_enables_artisan_only(self):
        repo_yaml = Path(cf.__file__).resolve().parent.parent / "instance.yaml"
        self.assertTrue(cf.opted_in("artisan", repo_yaml))
        for other in ("orchestrator", "mason", "specialist", "recovery", "archivist"):
            self.assertFalse(cf.opted_in(other, repo_yaml), other)

    def test_off_path_is_a_noop(self):
        y = self._yaml(False)
        hf = self.tmp / "HANDOFF.md"
        hf.write_text(NEW_DROP, encoding="utf-8")
        pf = self.tmp / "prev.md"
        pf.write_text(PREV, encoding="utf-8")
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = cf.main(["run", "--handoff-file", str(hf), "--prev-file", str(pf), "--agent", "artisan",
                          "--tickets-root", str(self.tickets), "--instance-yaml", str(y),
                          "--out-dir", str(self.out)])
        self.assertEqual((rc, out.getvalue(), err.getvalue()), (0, "", ""))
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertFalse((self.tmp / "chronicle").exists())


# ----------------------------------------------------------- retire list (T-5357)
# T-300 and T-301 are done; T-100 and T-400 are open (see _Tmp and the setUps below).
R1 = """\
# UPDATE 2026-10-21
- T-300 resolved and merged.
- T-300 and T-100 half done: T-100 still open.
- T-301 shipped; T-300 follow-up closed too.
- Plain prose with no ticket id at all.
- T-400 active work.
"""

# The seat removes the T-300 bullet (R1 -> R2), then re-adds it (R2 -> R3, case and spacing differ).
R2 = """\
# UPDATE 2026-10-22
- T-300 and T-100 half done: T-100 still open.
- T-301 shipped; T-300 follow-up closed too.
- T-400 active work.
"""

R3 = """\
# UPDATE 2026-10-23
-   t-300 RESOLVED and   merged.
- T-300 and T-100 half done: T-100 still open.
- T-301 shipped; T-300 follow-up closed too.
- T-400 active work.
"""


class RetireTests(_Tmp):
    def setUp(self):
        super().setUp()
        make_tickets(self.tickets, done=["T-301"])

    def cyc(self, st, prev, new):
        """One full cycle as _run drives it: the carry step, then the retire step."""
        open_ids, done_ids = self.sets()
        st, _ = cf.step(st, prev, new, open_ids, done_ids)
        return cf.retire_step(st, prev, new, done_ids)

    def test_norm_text_collapses_whitespace_casefolds_and_strips_marker(self):
        self.assertEqual(cf.norm_text("-   T-300 RESOLVED\n  and   merged."), "t-300 resolved and merged.")
        self.assertEqual(cf.norm_text("* t-300 resolved and merged."),
                         cf.norm_text("- T-300 Resolved and merged."))

    def test_list_contents(self):
        rows = cf.retire_candidates(R1, {"T-300", "T-301"})
        texts = [r["text"] for r in rows]
        self.assertEqual(len(rows), 2, texts)
        self.assertIn("T-300 resolved and merged", texts[0])  # all-done id: listed
        self.assertEqual(rows[0]["ids"], ["T-300"])
        self.assertEqual(rows[1]["ids"], ["T-300", "T-301"])  # several ids, all done: listed
        joined = "\n".join(texts)
        self.assertNotIn("T-100", joined)        # mixed open/done: not listed
        self.assertNotIn("Plain prose", joined)  # no ids: never listed
        self.assertNotIn("T-400", joined)        # open: not listed
        self.assertNotIn("# UPDATE", joined)     # headings never listed

    def test_still_present_is_not_a_remention(self):
        st, rep = self.cyc(cf.new_state(), None, R1)
        self.assertEqual(rep["listed_new"], 2)
        st, rep = self.cyc(st, R1, "<!-- 2 -->\n\n" + R1)
        st, rep = self.cyc(st, "<!-- 2 -->\n\n" + R1, "<!-- 3 -->\n\n" + R1)
        self.assertEqual(rep["rementions"], [])
        self.assertEqual(rep["still_present"], 2)
        self.assertEqual(st["retire"]["totals"]["rementions"], 0)
        self.assertEqual(st["retire"]["totals"]["still_present"], 4)
        for t in st["retire"]["tracked"].values():
            self.assertEqual(t["rementions"], 0)

    def test_remention_counted_on_reappearance(self):
        st, _ = self.cyc(cf.new_state(), None, R1)
        st, rep = self.cyc(st, R1, R2)  # the seat removes the T-300 bullet
        self.assertEqual(rep["rementions"], [])
        self.assertEqual(rep["still_present"], 1)
        st, rep = self.cyc(st, R2, R3)  # ... and re-adds it
        self.assertEqual([(r["how"], r["ids"]) for r in rep["rementions"]], [("reappeared", ["T-300"])])
        self.assertEqual(st["retire"]["totals"]["rementions"], 1)
        self.assertEqual(st["retire"]["totals"]["reappeared"], 1)
        st, rep = self.cyc(st, R3, "<!-- 4 -->\n\n" + R3)  # still there: not another re-mention
        self.assertEqual(rep["rementions"], [])
        self.assertEqual(st["retire"]["totals"]["rementions"], 1)

    def test_remention_counted_for_new_prose(self):
        st, _ = self.cyc(cf.new_state(), None, R1)
        new = R1 + "- Note: T-301 needs a follow-up after all.\n"
        st, rep = self.cyc(st, R1, new)
        self.assertEqual([(r["how"], r["ids"]) for r in rep["rementions"]], [("new_prose", ["T-300", "T-301"])])
        self.assertEqual(st["retire"]["totals"]["new_prose"], 1)
        self.assertEqual(rep["listed_new"], 1)  # the new bullet is itself all-done, so it is listed too
        st, rep = self.cyc(st, new, "<!-- 3 -->\n\n" + new)  # the same prose next cycle is not new
        self.assertEqual(rep["rementions"], [])

    def test_no_remention_on_the_listing_cycle(self):
        st, rep = self.cyc(cf.new_state(), "- unrelated\n", R1)
        self.assertEqual(rep["rementions"], [])

    def test_state_round_trip(self):
        st, _ = self.cyc(cf.new_state(), None, R1)
        st, _ = self.cyc(st, R1, R2)
        back = json.loads(json.dumps(st))
        self.assertTrue(cf._valid_state(back))
        back, rep = self.cyc(back, R2, R3)
        self.assertEqual(back["retire"]["totals"]["rementions"], 1)

    def test_corrupt_retire_key_resets_only_the_retire_state(self):
        st, _ = self.cyc(cf.new_state(), None, R1)
        st["retire"] = {"tracked": "nope"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            st, rep = self.cyc(st, R1, R2)
        self.assertIn("retire state unreadable", err.getvalue())
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertEqual(rep["listed_new"], 1)
        self.assertTrue(cf._valid_state(st))
        self.assertEqual(st["cycle"], 2)  # the carry state survives

    def test_old_state_without_retire_key_is_accepted(self):
        st, _ = cf.step(cf.new_state(), None, R1, *self.sets())
        st.pop("retire", None)
        st, rep = cf.retire_step(st, None, R1, self.sets()[1])
        self.assertEqual(rep["listed_new"], 2)

    def test_tracked_set_is_bounded(self):
        st, _ = self.cyc(cf.new_state(), None, R1)
        with mock.patch.object(cf, "RETIRE_TRACK_KEEP", 1):
            st, _ = self.cyc(st, R1, "<!-- 2 -->\n\n" + R1)
        self.assertEqual(len(st["retire"]["tracked"]), 1)

    def test_render_is_advisory_and_lists_ids(self):
        st, rep = self.cyc(cf.new_state(), None, R1)
        md = cf.render_retire_list(st, rep)
        self.assertIn("## Retire list (advisory)", md)
        self.assertIn("HANDOFF.md is unchanged", md)
        self.assertIn("[T-300]", md)
        self.assertIn("[T-300, T-301]", md)
        self.assertIn("Re-mentions", md)
        self.assertNotIn("T-100 still open", md)


class RetireCliTests(_Tmp):
    """The retire list through main(): companion, no overwrite, never-block, off seats."""

    run_cli = CliTests.run_cli

    def setUp(self):
        super().setUp()
        make_tickets(self.tickets, done=["T-301"])

    def test_retire_list_written_to_out_dir(self):
        rc, out, err, _ = self.run_cli("--out-dir", str(self.out), prev=R1, new=R2)
        self.assertEqual(rc, 0, err)
        md = (self.out / "retire-list.md").read_text(encoding="utf-8")
        self.assertIn("[T-300, T-301]", md)
        st = json.loads((self.out / "carry-state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(st["retire"]["tracked"]), 1)

    def test_remention_persists_across_runs(self):
        self.run_cli("--out-dir", str(self.out), prev="- start\n", new=R1)
        self.run_cli("--out-dir", str(self.out), prev=R1, new=R2)
        rc, _, err, _ = self.run_cli("--out-dir", str(self.out), prev=R2, new=R3)
        self.assertEqual(rc, 0, err)
        st = json.loads((self.out / "carry-state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["retire"]["totals"]["rementions"], 1)
        md = (self.out / "retire-list.md").read_text(encoding="utf-8")
        self.assertIn("re-mentions this cycle: 1", md)

    def test_chronicle_companion_never_overwritten(self):
        rc, _, err, _ = self.run_cli("--ts", "2026-10-22T09:00:00Z", "--uuid", "abc", prev=R1, new=R2)
        self.assertEqual(rc, 0, err)
        day = self.tmp / "chronicle" / "artisan" / "2026-10-22"
        first = day / "2026-10-22T090000Z-abc.retire-list.md"
        before = first.read_bytes()
        rc, _, err, _ = self.run_cli("--ts", "2026-10-22T09:00:00Z", "--uuid", "abc", prev=R2, new=R3)
        self.assertEqual(rc, 0, err)
        self.assertEqual(first.read_bytes(), before)
        self.assertTrue((day / "2026-10-22T090000Z-abc.retire-list.md.2").is_file())

    def test_retire_failure_never_blocks_the_carry(self):
        with mock.patch.object(cf, "retire_step", side_effect=RuntimeError("kaboom")):
            rc, out, err, _ = self.run_cli("--out-dir", str(self.out))
        self.assertEqual(rc, 0)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("retire list skipped", err)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["carry-forward.md", "carry-state.json"])

    def test_retire_list_never_mailed(self):
        calls = []
        snd = lambda *a, **k: calls.append(a) or "m"  # noqa: E731
        prev = R1
        for c in range(1, 10):
            new = R2 + f"<!-- {c} -->\n"
            self.run_cli("--out-dir", str(self.out), prev=prev, new=new, sender=snd)
            prev = new
        self.assertEqual(calls, [])  # nothing open was dropped, so no carry mail; the retire list never mails
        for a in calls:
            self.assertNotIn("Retire list", a[1])
            self.assertNotIn("T-301", a[1])

    def test_never_writes_handoff_with_retire_list(self):
        rc, _, _, hf = self.run_cli("--out-dir", str(self.tmp), prev=R1, new=R2)
        self.assertEqual(hf.read_text(encoding="utf-8"), R2)
        self.assertTrue((self.tmp / "retire-list.md").is_file())

    def test_off_seat_writes_no_retire_list(self):
        y = self.tmp / "instance.yaml"
        y.write_text("roles:\n  eo1:\n    persona: mason\n    account: suborch1\n", encoding="utf-8")
        hf, pf = self.tmp / "HANDOFF.md", self.tmp / "prev.md"
        hf.write_text(R2, encoding="utf-8")
        pf.write_text(R1, encoding="utf-8")
        e, o = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(e), contextlib.redirect_stdout(o):
            rc = cf.main(["run", "--handoff-file", str(hf), "--prev-file", str(pf), "--agent", "mason",
                          "--tickets-root", str(self.tickets), "--instance-yaml", str(y),
                          "--out-dir", str(self.out)])
        self.assertEqual((rc, o.getvalue(), e.getvalue()), (0, "", ""))
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertFalse((self.tmp / "chronicle").exists())

class RetireR2Tests(_Tmp):
    """Review r1 on T-5357: L1 element validation, L3 heading prose, the re-mention class split."""

    def setUp(self):
        super().setUp()
        make_tickets(self.tickets, done=["T-301"])

    cyc = RetireTests.cyc

    def _bad(self, mutate):
        st, _ = self.cyc(cf.new_state(), None, R1)
        t = next(iter(st["retire"]["tracked"].values()))
        mutate(t)
        return st

    def test_element_types_invalidate_and_reset_only_retire(self):
        cases = {
            "ids nested list": lambda t: t.update(ids=[[1]]),
            "ids not T-NNNN": lambda t: t.update(ids=["nope"]),
            "text missing": lambda t: t.pop("text"),
            "text not str": lambda t: t.update(text=7),
            "count not int": lambda t: t.update(rementions="1"),
            "optional count not int": lambda t: t.update(reappeared=None),
        }
        for name, mutate in cases.items():
            st = self._bad(mutate)
            self.assertFalse(cf._valid_retire(st["retire"]), name)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                st2, rep = self.cyc(st, R1, R2)
            self.assertEqual(len(err.getvalue().strip().splitlines()), 1, name)
            self.assertIn("retire state unreadable", err.getvalue(), name)
            self.assertEqual(st2["cycle"], 2, name)
            self.assertTrue(cf._valid_retire(st2["retire"]), name)
            cf.render_retire_list(st2, rep)  # renders after the reset

    def test_new_heading_prose_counts_as_new_prose(self):
        st, _ = self.cyc(cf.new_state(), None, R1)
        new = R1 + "\n## Follow-up on T-301\n"
        st, rep = self.cyc(st, R1, new)
        self.assertEqual([(r["how"], r["heading"]) for r in rep["rementions"]], [("new_prose", True)])
        self.assertEqual(st["retire"]["totals"]["new_prose_heading"], 1)

    def test_class_kept_block(self):
        # T-300 is still in R1's mixed open/done bullet, which a retired copy keeps
        st, _ = self.cyc(cf.new_state(), None, R1)
        new = R1 + "- Aside: T-300 docs need a link.\n"
        st, rep = self.cyc(st, R1, new)
        self.assertEqual({r["class"] for r in rep["rementions"]}, {"kept_block"})
        self.assertEqual(st["retire"]["totals"]["kept_block"], len(rep["rementions"]))

    def test_class_retired_only(self):
        p = "- T-301 shipped.\n- T-400 active.\n"
        st, _ = self.cyc(cf.new_state(), None, p)
        st, rep = self.cyc(st, p, p + "- T-301 needs one more note.\n")
        self.assertEqual([(r["how"], r["class"]) for r in rep["rementions"]], [("new_prose", "retired_only")])
        self.assertEqual(st["retire"]["totals"]["retired_only"], 1)

    def test_class_retired_only_heading_keeps(self):
        # the same id under a heading in the previous HANDOFF: a retired copy keeps headings
        p = "## T-301 wrap-up\n- T-301 shipped.\n- T-400 active.\n"
        st, _ = self.cyc(cf.new_state(), None, p)
        st, rep = self.cyc(st, p, p + "- T-301 needs one more note.\n")
        self.assertEqual([r["class"] for r in rep["rementions"]], ["kept_block"])

    def test_class_returned_for_new_prose_and_reappearance(self):
        p1, p2 = "- T-301 shipped.\n- T-400 active.\n", "- T-400 active.\n"
        st, _ = self.cyc(cf.new_state(), None, p1)
        st, _ = self.cyc(st, p1, p2)
        st_a, rep = self.cyc(st, p2, p2 + "- T-301 back in new words.\n")
        self.assertEqual([(r["how"], r["class"]) for r in rep["rementions"]], [("new_prose", "returned")])
        st_b, rep = self.cyc(st, p2, p2 + "- T-301 shipped.\n")
        self.assertEqual([(r["how"], r["class"]) for r in rep["rementions"]], [("reappeared", "returned")])
        self.assertEqual(st_b["retire"]["totals"]["returned"], 1)

    def test_class_uses_the_done_set_of_the_previous_cycle(self):
        # T-100 open at cycle 1, so the mixed bullet is kept then; T-100 closes before cycle 2.
        p = "- T-301 shipped.\n- T-301 and T-100 together.\n"
        st, _ = self.cyc(cf.new_state(), None, p)
        make_tickets(self.tickets, done=["T-100"])
        (self.tickets / "active" / "T-100.md").unlink()
        st, rep = self.cyc(st, p, p + "- T-301 again.\n")
        self.assertEqual([r["class"] for r in rep["rementions"]], ["kept_block"])

    def test_split_in_history_and_render(self):
        p = "- T-301 shipped.\n- T-400 active.\n"
        st, _ = self.cyc(cf.new_state(), None, p)
        st, rep = self.cyc(st, p, p + "- T-301 needs one more note.\n")
        self.assertEqual(st["retire"]["history"][-1]["retired_only"], 1)
        md = cf.render_retire_list(st, rep)
        self.assertIn("kept_block 0, retired_only 1, returned 0", md)


class RetireNeverBlockR2Tests(_Tmp):
    """Review r1 MED-1: a render failure must not abort the carry persist or the HO send."""

    run_cli = CliTests.run_cli

    def test_render_failure_keeps_carry_state_forward_and_send(self):
        calls = []
        snd = lambda *a, **k: calls.append(1) or "m"  # noqa: E731
        with mock.patch.object(cf, "render_retire_list", side_effect=RuntimeError("render kaboom")):
            rc, _, err, _ = self.run_cli("--out-dir", str(self.out), sender=snd)
            self.assertEqual(rc, 0, err)
            self.assertEqual(len(err.strip().splitlines()), 1)
            self.assertIn("retire list skipped", err)
            prev = NEW_DROP
            for c in range(2, 9):
                new = NEW_DROP + f"<!-- {c} -->\n"
                rc, _, err, _ = self.run_cli("--out-dir", str(self.out), prev=prev, new=new, sender=snd)
                self.assertEqual(rc, 0, err)
                prev = new
        st = json.loads((self.out / "carry-state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["cycle"], 8)
        self.assertTrue((self.out / "carry-forward.md").is_file())
        self.assertTrue((self.out / "expired-carry.md").is_file())
        self.assertEqual(len(calls), 1, "the HO send still happens")
        self.assertFalse((self.out / "retire-list.md").exists())

    def test_reviewer_repro_tracked_entry_without_text(self):
        st = cf.new_state()
        rs = cf.new_retire_state()
        rs["tracked"]["deadbeefdeadbeef"] = {"ids": ["T-300"], "present": False, "first_listed_cycle": 0,
                                             "last_seen_cycle": 0, "rementions": 1, "still_present_cycles": 0}
        st["retire"] = rs
        (self.out / "carry-state.json").write_text(json.dumps(st), encoding="utf-8")
        rc, _, err, _ = self.run_cli("--out-dir", str(self.out))
        self.assertEqual(rc, 0, err)
        self.assertIn("retire state unreadable", err)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["carry-forward.md", "carry-state.json", "retire-list.md"])
        st = json.loads((self.out / "carry-state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["cycle"], 1)
        self.assertTrue(cf._valid_retire(st["retire"]))

if __name__ == "__main__":
    unittest.main()
