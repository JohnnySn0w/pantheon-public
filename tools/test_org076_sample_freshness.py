"""ORG-076 BT3b: per-seat sample freshness (T-4709). The journal is ALWAYS a fixture/fake port here:
nothing reads the real journal. Fixture lines are real ``journalctl -o json`` start records captured
from a seat's own user journal (cursor/boot ids redacted)."""
from __future__ import annotations

import json
import unittest

from tools import episode_store as es
from tools import org076_sample_freshness as fr
from tools import org076_store_runner as run
from tools import quota_probe_detector as qpd
from tools.test_org076_reconciler import Rig as _Rig

# real shape (captured 2026-10-08 from a seat's user journal; JOB_START for a templated user unit)
REAL_START_LINES = "\n".join([
    '{"JOB_TYPE": "start", "MESSAGE_ID": "7d4958e842da4a758f6c1cdc7b36dcc5", "USER_UNIT": "pantheon-inbox-consumer@orchestrator.service", "_BOOT_ID": "REDACTED", "__CURSOR": "REDACTED", "__MONOTONIC_TIMESTAMP": "6185876378756", "__REALTIME_TIMESTAMP": "1791440571663192", "__SEQNUM": "57045695", "__SEQNUM_ID": "REDACTED"}',
    '{"JOB_TYPE": "start", "MESSAGE_ID": "7d4958e842da4a758f6c1cdc7b36dcc5", "USER_UNIT": "pantheon-inbox-consumer@orchestrator.service", "_BOOT_ID": "REDACTED", "__CURSOR": "REDACTED", "__MONOTONIC_TIMESTAMP": "6186476451415", "__REALTIME_TIMESTAMP": "1791441171735851", "__SEQNUM": "57052311", "__SEQNUM_ID": "REDACTED"}',
    '{"JOB_TYPE": "start", "MESSAGE_ID": "7d4958e842da4a758f6c1cdc7b36dcc5", "USER_UNIT": "pantheon-inbox-consumer@orchestrator.service", "_BOOT_ID": "REDACTED", "__CURSOR": "REDACTED", "__MONOTONIC_TIMESTAMP": "6187076555472", "__REALTIME_TIMESTAMP": "1791441771839908", "__SEQNUM": "57058871", "__SEQNUM_ID": "REDACTED"}',
])
REAL_CONTROL_LINE = '{"__CURSOR": "REDACTED", "__REALTIME_TIMESTAMP": "1791442011854404", "__MONOTONIC_TIMESTAMP": "6187316569968"}'
NOW = 1_791_450_000.0


class FakePort:
    def __init__(self, starts=(), control_ok=True, error=None, boom=None):
        self.result = fr.JournalResult(list(starts), control_ok, error)
        self.boom = boom
        self.calls = []

    def starts(self, uid, unit, since):
        self.calls.append((uid, unit, since))
        if self.boom:
            raise self.boom
        return self.result


def v(seen_ago, port, uid=1001):
    return fr.verdict("specialist", seen_epoch=NOW - seen_ago, now_epoch=NOW, account="claude", journal=port,
                      uid_of=lambda a: uid)


class VerdictTable(unittest.TestCase):
    def test_fresh_is_healthy_and_never_touches_the_journal(self):
        p = FakePort(error="would be unreadable")
        self.assertIsInstance(v(fr.STALE_SECS, p), es.Healthy)
        self.assertEqual(p.calls, [])

    def test_stale_and_unit_ran_is_unknown_silent_with_the_right_query(self):
        p = FakePort(starts=[NOW - 1800])
        out = v(fr.STALE_SECS + 1, p)
        self.assertEqual((type(out), out.fault_id), (es.Unknown, "sample-freshness-silent:specialist"))
        self.assertEqual(p.calls, [(1001, "pantheon-persona-quota@specialist.service", NOW - fr.STALE_SECS - 1)])
        self.assertIn("pantheon-persona-quota@specialist.service", out.evidence)

    def test_stale_but_unit_did_not_run_is_healthy_vacuous(self):
        self.assertIsInstance(v(7200, FakePort(starts=[])), es.Healthy)

    def test_a_start_inside_the_grace_window_does_not_count(self):
        self.assertIsInstance(v(7200, FakePort(starts=[NOW - fr.GRACE_SECS + 5])), es.Healthy)

    def test_a_start_before_the_last_record_does_not_count(self):
        self.assertIsInstance(v(7200, FakePort(starts=[NOW - 7300])), es.Healthy)

    def test_unreadable_journal_is_unknown_with_its_own_fault_id_never_healthy(self):
        for port in (FakePort(control_ok=False), FakePort(error="journalctl rc=1: boom"),
                     FakePort(boom=RuntimeError("x")), FakePort(starts=[NOW - 1800], control_ok=False)):
            with self.subTest(port=vars(port)):
                out = v(7200, port)
                self.assertEqual((type(out), out.fault_id), (es.Unknown, "sample-freshness-journal-unreadable:specialist"))

    def test_unresolvable_account_is_unknown(self):
        out = fr.verdict("specialist", seen_epoch=NOW - 7200, now_epoch=NOW, account="nobody-here",
                         journal=FakePort(), uid_of=lambda a: None)
        self.assertEqual(out.fault_id, "sample-freshness-journal-unreadable:specialist")


class JournalctlPortParsing(unittest.TestCase):
    def runner(self, start_out=REAL_START_LINES, control_out=REAL_CONTROL_LINE, rc=0, crc=0):
        calls = []

        def run(argv):
            calls.append(argv)
            if "-n" in argv:
                return crc, control_out, ""
            return rc, start_out, ""
        run.calls = calls
        return run

    def test_parses_real_start_records_and_builds_the_documented_query(self):
        r = self.runner()
        res = fr.JournalctlPort(r).starts(1001, "pantheon-persona-quota@specialist.service", 1791440000)
        self.assertEqual(res.starts, [1791440571.663192, 1791441171.735851, 1791441771.839908])
        self.assertTrue(res.control_ok)
        self.assertIsNone(res.error)
        q = r.calls[0]
        for want in ("_UID=1001", "USER_UNIT=pantheon-persona-quota@specialist.service",
                     f"MESSAGE_ID={fr.JOB_START_MESSAGE_ID}", "--since=@1791440000", "json"):
            self.assertIn(want, q)
        self.assertNotIn("--user", q)  # recovery reads OTHER accounts' journals: no --user
        self.assertIn("-n", r.calls[1])

    def test_empty_control_means_unreadable_not_idle(self):
        res = fr.JournalctlPort(self.runner(start_out="", control_out="")).starts(1001, "u", 0)
        self.assertEqual((res.starts, res.control_ok), ([], False))

    def test_nonzero_rc_timeout_and_garbage_are_errors(self):
        self.assertIsNotNone(fr.JournalctlPort(self.runner(rc=1)).starts(1, "u", 0).error)
        self.assertIsNotNone(fr.JournalctlPort(self.runner(crc=1)).starts(1, "u", 0).error)
        self.assertIsNotNone(fr.JournalctlPort(self.runner(start_out="{bad")).starts(1, "u", 0).error)

        def boom(argv):
            raise TimeoutError("slow")
        self.assertIn("TimeoutError", fr.JournalctlPort(boom).starts(1, "u", 0).error)

    def test_fixture_is_real_journal_shape(self):
        for line in REAL_START_LINES.splitlines():
            d = json.loads(line)
            self.assertEqual(d["MESSAGE_ID"], fr.JOB_START_MESSAGE_ID)
            self.assertIn("@", d["USER_UNIT"])  # template instance is carried in USER_UNIT


class FreshRig(_Rig):
    def setUp(self):
        super().setUp()
        self.port = FakePort()
        self.cfg.journal = self.port
        self.cfg.uid_of = lambda account: 1001

    def now(self):
        return self.clock._t.timestamp()

    def enroll(self, persona="recovery", account="claude-fallback"):
        self.drop_sample(persona, kind=qpd.NOT_MEASURED, account=account)
        rep = self.cycle()
        self.assertEqual(rep.not_measured, 1)

    def set_seen(self, ago, persona="recovery"):
        st = run._load_state(self.cfg)
        st["seen"][persona] = self.now() - ago
        run._save_state(self.cfg, st)

    def fdomain(self, persona="recovery"):
        return es.load_domain(fr.domain_key(persona), self.state)

    def fepisodes(self, persona="recovery"):
        return list(self.fdomain(persona)["episodes"].values())


class Enrollment(FreshRig):
    def test_a_never_dropped_seat_is_not_enrolled_and_never_flagged(self):
        self.port.result = fr.JournalResult([self.now() - 1800])
        for _ in range(4):
            self.cycle()
        self.assertFalse(es.store_path(fr.domain_key("recovery"), self.state).exists())
        self.assertEqual(self.port.calls, [])

    def test_not_measured_enrolls_refreshes_freshness_and_is_never_observed(self):
        observed = []
        real = es.observe

        def spy(spec, verdict, **kw):
            observed.append(spec.detector)
            return real(spec, verdict, **kw)

        self.drop_sample("recovery", kind=qpd.NOT_MEASURED)
        from unittest import mock
        with mock.patch.object(es, "observe", spy):
            rep = self.cycle()
        self.assertEqual((rep.not_measured, rep.ingested, rep.exit_code), (1, 0, 0))
        self.assertNotIn(qpd.DETECTOR, observed)           # never fed to the readability domain
        self.assertFalse(es.store_path(f"{qpd.DETECTOR}:recovery", self.state).exists())
        self.assertIn(fr.DETECTOR, observed)               # but freshness saw the seat
        st = run._load_state(self.cfg)
        self.assertEqual(st["personas"], {"recovery": "claude-fallback"})
        self.assertIn("recovery", st["seen"])
        self.assertEqual([p for p in self.drop.iterdir() if p.is_file()], [])

    def test_a_rejected_file_from_an_enrolled_seat_still_proves_its_writes_work(self):
        self.enroll()
        self.set_seen(7200)
        (self.drop / "00000000T000000000000__recovery__nr__1.json").write_text("{not json")
        self.cycle()
        st = run._load_state(self.cfg)
        self.assertLess(self.now() - st["seen"]["recovery"], 600)

    def test_a_malformed_file_from_an_unknown_name_does_not_enroll(self):
        (self.drop / "00000000T000000000000__ghost__nr__1.json").write_text("{not json")
        self.cycle()
        st = run._load_state(self.cfg)
        self.assertEqual((st["personas"], st["seen"]), ({}, {}))


class SilentSeat(FreshRig):
    def test_silent_seat_whose_unit_ran_opens_one_unknown_episode_and_one_ticket(self):
        self.enroll()
        self.set_seen(7200)
        self.port.result = fr.JournalResult([self.now() - 1800])  # the quota unit started 30 min ago
        for _ in range(3):
            self.cycle()
        (ep,) = self.fepisodes()
        self.assertEqual((ep["fault_id"], ep["verdict_kind"]), ("sample-freshness-silent:recovery", "unknown"))
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), (es.EMITTED, es.VERIFIED))
        (rel,) = self.fx.origin_ticket_files()
        txt = self.origin_text(rel)
        self.assertIn("requester: recovery", txt)
        self.assertIn("repair_route: watchman@codex", txt)
        self.assertEqual(self.port.calls[0][1], "pantheon-persona-quota@recovery.service")
        self.assertEqual(self.port.calls[0][0], 1001)

    def test_a_fresh_seat_is_healthy_even_if_the_unit_keeps_running(self):
        self.enroll()
        self.port.result = fr.JournalResult([self.now() - 1800])
        for _ in range(4):
            self.cycle()
        self.assertEqual(self.fepisodes(), [])
        self.assertEqual(self.port.calls, [])  # fresh => the journal is not even consulted

    def test_stale_but_unit_never_ran_is_healthy(self):
        self.enroll()
        self.set_seen(7200)
        for _ in range(4):
            self.cycle()
        self.assertEqual(self.fepisodes(), [])

    def test_unreadable_journal_is_unknown_with_its_own_fault_id_never_healthy(self):
        self.enroll()
        self.set_seen(7200)
        self.port.result = fr.JournalResult([], control_ok=False)
        for _ in range(3):
            self.cycle()
        (ep,) = self.fepisodes()
        self.assertEqual((ep["fault_id"], ep["verdict_kind"]), ("sample-freshness-journal-unreadable:recovery", "unknown"))

    def test_records_resume_then_the_episode_closes_after_m_healthy(self):
        self.enroll()
        self.set_seen(7200)
        self.port.result = fr.JournalResult([self.now() - 1800])
        for _ in range(3):
            self.cycle()
        (ep,) = self.fepisodes()
        self.drop_sample("recovery", kind=qpd.NOT_MEASURED)  # the seat's writes work again
        for _ in range(2):
            self.cycle()
        (ep2,) = self.fepisodes()
        self.assertEqual((ep2["episode_id"], ep2["state"], ep2["resolve_reason"]),
                         (ep["episode_id"], es.RESOLVED, "hysteresis-close"))

    def test_runner_does_not_crash_when_the_journal_port_raises(self):
        self.enroll()
        self.set_seen(7200)
        self.cfg.journal = FakePort(boom=OSError("journald gone"))
        rep = self.cycle()
        self.assertEqual(rep.exit_code, 0)  # a port that raises is an Unknown verdict, not a runner error
        self.assertEqual(self.fdomain()["runs"][fr.DETECTOR]["fault_id"], "sample-freshness-journal-unreadable:recovery")


class ForgetPersona(FreshRig):
    def test_forget_deenrolls_and_closes_active_episodes_in_both_domains(self):
        self.enroll()
        self.set_seen(7200)
        self.port.result = fr.JournalResult([self.now() - 1800])
        for _ in range(3):
            self.cycle()
        (fep,) = self.fepisodes()
        qep = self.open_episode()  # also a readability episode for the same seat
        rc = run.forget_persona(self.cfg, self.ports(), "recovery")
        self.assertEqual(rc, 0)
        st = run._load_state(self.cfg)
        self.assertEqual((st["personas"], st["seen"]), ({}, {}))
        for doc in (self.fdomain(), self.domain()):
            for e in doc["episodes"].values():
                self.assertEqual((e["state"], e["resolve_reason"], e["healed_under_ticket"]),
                                 (es.RESOLVED, "persona-forgotten", False))
        before = self.origin_sha()
        for _ in range(3):
            self.cycle()
        self.assertEqual(self.origin_sha(), before)  # no ticket mutation, nothing new filed
        self.assertEqual(rc, run.forget_persona(self.cfg, self.ports(), "recovery"))  # idempotent

    def test_forget_rejects_a_bad_name(self):
        self.assertEqual(run.forget_persona(self.cfg, self.ports(), "../x"), run.EXIT_MISMATCH)

    def test_cli_requires_exactly_one_of_run_or_forget(self):
        for argv in ([], ["--run", "--forget-persona", "recovery"]):
            with self.assertRaises(SystemExit) as cm:
                run.main(argv)
            self.assertEqual(cm.exception.code, 2)

    def test_runner_lock_serialises_a_cycle_and_a_forget(self):
        with run.runner_lock(self.cfg, blocking=False):
            with self.assertRaises(run.RunnerBusy):
                with run.runner_lock(self.cfg, blocking=False):
                    pass
            with self.assertRaises(run.RunnerBusy):
                with run.runner_lock(self.cfg, blocking=True, timeout=0.1):
                    pass
        with run.runner_lock(self.cfg, blocking=False):  # released
            pass


class Docs(unittest.TestCase):
    def test_docstring_states_the_group_dependency_and_limits(self):
        self.assertIn("systemd-journal", fr.__doc__)
        self.assertIn("HONEST LIMITS", fr.__doc__)
        self.assertIn("NEVER Healthy", fr.__doc__)


if __name__ == "__main__":
    unittest.main()
