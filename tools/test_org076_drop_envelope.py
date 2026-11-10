"""ORG-076 BT5.0 (T-5999): the generic schema-2 drop envelope + runner-side detector registry.

Hermetic, on the BT3a runner rig: tmp drop dir, tmp clone + bare tmp origin, injected page, tmp
heartbeat/state, injected journal port and registry. Nothing touches real /var/lib, $HOME, the
journal, Telegram, real tickets or a real push. The production ``REGISTRY`` is empty; every test
injects its own entry through ``Config.registry``.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import episode_store as es
from tools import org076_sample_freshness as fresh
from tools import org076_store_runner as run
from tools import quota_probe_detector as qpd
from tools import repair_evidence as rev
from tools._episode_fixtures import FakeClock
from tools.test_org076_store_runner import T0, Rig, _child

ROOT = Path(__file__).resolve().parent.parent
QUOTA_FIXTURE = ROOT / "tools" / "fixtures" / "org076" / "quota-probe-sample-v1.json"
# The file name the real writer gave the captured fixture (qpd.feed, 2026-11-02).
QUOTA_FIXTURE_NAME = "20261102T203512125508__recovery__20261102T2030Z__1544105e47f46.json"

DET = "test-mail-staleness"
MAIL = run.DropDetector(detector=DET, subsystem="mail-delivery", harness="codex",
                        fault_kinds=frozenset({"mail-stale", "mail-unreadable"}))
FDET = "test-tg-inbound"
FRESH = run.DropDetector(detector=FDET, subsystem="telegram-inbound", harness="claude",
                         fault_kinds=frozenset({"tg-inbound-silent"}),
                         freshness_unit="pantheon-test-tg@{persona}.service")


class FakeJournal:
    def __init__(self):
        self.calls = []

    def starts(self, uid, unit, since_epoch):
        self.calls.append(unit)
        return fresh.JournalResult([since_epoch + 100], control_ok=True)


class EnvelopeRig(Rig):
    def setUp(self):
        super().setUp()
        self.journal = FakeJournal()
        self.cfg.registry = {DET: MAIL, FDET: FRESH}
        self.cfg.journal = self.journal
        self.cfg.uid_of = lambda account: 1000

    def body(self, persona="recovery", verdict="broken", kind="mail-stale", run_id="auto", account="claude-fallback",
             detector=DET, **over):
        self.seq += 1
        if run_id == "auto":
            run_id = f"R{self.seq:06d}"
        d = {"schema": run.ENVELOPE_SCHEMA, "kind": run.ENVELOPE_KIND, "detector": detector,
             "domain_key": f"{detector}:{persona}", "persona": persona, "account": account, "verdict": verdict,
             "fault_id": None if verdict == "healthy" else f"{kind}:{persona}", "detail": "oldest unread 2h",
             "run_id": run_id, "sample_id": f"s-{self.seq}",
             "sampled_at": (T0 + timedelta(seconds=self.seq)).isoformat().replace("+00:00", "Z")}
        d.update(over)
        return d

    def drop_v2(self, raw=None, persona="recovery", **kw):
        d = self.body(persona=persona, **kw) if raw is None else raw
        self.seq += 1
        stamp = (T0 + timedelta(seconds=self.seq)).strftime("%Y%m%dT%H%M%S%f")
        name = f"{stamp}__{persona}__r{self.seq}__{self.seq:04d}.json"
        payload = d if isinstance(d, (bytes, str)) else json.dumps(d, sort_keys=True) + "\n"
        (self.drop / name).write_bytes(payload.encode("utf-8") if isinstance(payload, str) else payload)
        return name

    def mdomain(self, persona="recovery", det=DET):
        return es.load_domain(f"{det}:{persona}", self.state)

    def mn(self, persona="recovery", det=DET):
        r = self.mdomain(persona, det)["runs"].get(det)
        return r["n"] if r else 0

    def runner_state(self):
        return json.loads((self.state / "org076-runner" / "state.json").read_text())


class NewKindIngest(EnvelopeRig):
    def test_new_kind_ingested_opens_one_episode_and_ticket(self):
        for _ in range(3):
            self.drop_v2()
        rep = self.cycle()
        self.assertEqual((rep.ingested, rep.rejected, rep.exit_code), (3, 0, 0))
        self.assertEqual(self.dropped(), [])
        (ep,) = self.mdomain()["episodes"].values()
        self.assertEqual((ep["fault_id"], ep["verdict_kind"]), ("mail-stale:recovery", "broken"))
        self.assertEqual(ep["delivery"]["phase"], es.VERIFIED)
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)

    def test_same_run_id_twice_dedupes(self):
        self.drop_v2(run_id="B1")
        self.drop_v2(run_id="B1")
        rep = self.cycle()
        self.assertEqual((rep.ingested, rep.deduped, self.mn()), (1, 1, 1))

    def test_no_run_id_dedupes_on_sample_id_and_two_personas_count_separately(self):
        self.drop_v2(raw=self.body(run_id=None, sample_id="same"))
        self.drop_v2(raw=self.body(run_id=None, sample_id="same"))
        self.drop_v2(persona="specialist", run_id="B1", account="claude")
        rep = self.cycle()
        self.assertEqual((rep.ingested, rep.deduped, self.mn("recovery"), self.mn("specialist")), (2, 1, 1, 1))

    def test_unknown_and_healthy_verdicts_map(self):
        self.drop_v2(verdict="unknown", kind="mail-unreadable")
        self.cycle()
        run_ = self.mdomain()["runs"][DET]
        self.assertEqual((run_["n"], run_["fault_id"]), (1, "mail-unreadable:recovery"))
        self.drop_v2(verdict="healthy")
        self.cycle()
        self.assertNotIn(DET, self.mdomain()["runs"])

    def test_schema2_never_touches_quota_enrollment_or_freshness(self):
        self.drop_v2()
        self.cycle()
        st = self.runner_state()
        self.assertEqual((st["personas"], st["seen"]), ({}, {}))
        self.assertEqual(st["detectors"], {DET: {"recovery": "claude-fallback"}})
        self.assertNotIn(DET, st.get("detector_seen", {}))
        self.assertFalse(es.store_path(f"{qpd.DETECTOR}:recovery", self.state).exists())
        self.assertFalse(es.store_path(fresh.domain_key("recovery"), self.state).exists())
        self.assertFalse(es.store_path(fresh.domain_key("recovery", DET), self.state).exists())
        self.assertEqual(self.journal.calls, [], "no freshness without opt-in")

    def test_enrolled_specs_are_reconciled_and_swept(self):
        self.drop_v2()
        self.cycle()
        seen = []
        real = es.reconcile

        def spy(specs, **kw):
            seen.extend(s.domain_key for s in specs)
            return real(specs, **kw)

        with mock.patch.object(es, "reconcile", spy):
            self.cycle()
        self.assertIn(f"{DET}:recovery", seen)

    def test_busy_domain_holds_later_files_of_same_persona(self):
        self.drop_v2(run_id="B1")
        self.drop_v2(run_id="B2")
        with es._DomainLock(es.store_path(f"{DET}:recovery", self.state), 5):
            rep = self.cycle(lock_timeout=0.05)
        self.assertEqual((rep.ingested, rep.kept, len(self.dropped())), (0, 2, 2))
        rep = self.cycle()
        self.assertEqual((rep.ingested, self.mn(), self.dropped()), (2, 2, []))

    def test_production_registry_holds_only_bt5_detectors_so_other_schema2_is_quarantined(self):
        self.cfg.registry = None
        self.assertEqual(set(run.REGISTRY), {"mail-delivery", "telegram-inbound"})  # T-6001, T-6000; BT5 tickets add theirs
        self.drop_v2()
        rep = self.cycle()
        self.assertEqual((rep.rejected, rep.ingested, len(self.pages)), (1, 0, 1))


class SeatLookup(EnvelopeRig):  # T-6001: runner-side per-persona harness/account; the record is never trusted
    def setUp(self):
        super().setUp()
        seats = {"specialist": ("codex", "claude"), "recovery": ("claude", "claude-fallback")}
        self.entry = run.DropDetector(detector=DET, subsystem="mail-delivery", harness="claude",
                                      fault_kinds=frozenset({"mail-stale"}), seat_for=seats.get)
        self.cfg.registry = {DET: self.entry}

    def test_harness_is_the_seats_not_the_fixed_one_and_validation_still_runs(self):
        self.assertEqual(self.entry.spec_for("specialist", "claude").harness, "codex")
        self.assertEqual(self.entry.spec_for("nobody", "x").harness, "claude")  # fallback for enrolled-then-gone
        with self.assertRaises(ValueError):
            run.DropDetector(detector=DET, subsystem="s", harness="bogus", fault_kinds=frozenset({"k"}),
                             seat_for=lambda p: None)

    def test_no_seat_or_account_mismatch_is_rejected_and_paged(self):
        self.drop_v2(persona="specialist", account="claude")           # ok
        self.drop_v2(persona="nobody", account="claude")          # no seat
        self.drop_v2(persona="recovery", account="claude")            # account differs from the runner's seat
        rep = self.cycle()
        self.assertEqual((rep.ingested, rep.rejected, len(self.pages)), (1, 2, 2))

    def test_without_seat_for_nothing_changes(self):
        self.assertIsNone(MAIL.seat_for)
        self.assertEqual(MAIL.spec_for("recovery", "claude-fallback").harness, "codex")


class Quarantine(EnvelopeRig):
    CASES = {
        "unknown detector": dict(detector="not-registered"),
        "reserved quota detector via schema 2": dict(detector=qpd.DETECTOR),
        "bad verdict": dict(verdict="sick"),
        "missing verdict": dict(verdict=None),
        "healthy with fault_id": dict(verdict="healthy", fault_id="mail-stale:recovery"),
        "broken without fault_id": dict(fault_id=None),
        "unregistered fault kind": dict(fault_id="disk-full:recovery"),
        "fault_id names another persona": dict(fault_id="mail-stale:specialist"),
        "domain_key not the registry's": dict(domain_key=f"{qpd.DETECTOR}:recovery"),
        "bad persona": dict(persona="../etc"),
        "bad account": dict(account="Root Account"),
        "detail too long": dict(detail="x" * (run.DETAIL_LIMIT + 1)),
        "bad run_id": dict(run_id=""),
        "bad sample_id": dict(sample_id=None),
        "bad sampled_at": dict(sampled_at=7),
        "wrong kind for schema 2": dict(kind="quota-probe-sample"),
        "schema 2 kind with schema 3": dict(schema=3),
    }

    def test_each_malformed_record_is_quarantined_and_paged_and_queue_not_wedged(self):
        for why, over in self.CASES.items():
            self.drop_v2(raw=self.body(**over))
        (self.drop / "00000000T000000000000__x__nr__1.json").write_text("{not json")
        self.drop_v2(run_id="GOOD")
        rep = self.cycle()
        n_bad = len(self.CASES) + 1
        self.assertEqual((rep.rejected, rep.ingested, self.mn()), (n_bad, 1, 1))
        self.assertEqual(len(list((self.drop / run.REJECTED_DIR).iterdir())), n_bad)
        self.assertEqual(len(self.pages), n_bad)
        self.assertEqual(self.dropped(), [])
        self.assertEqual(self.runner_state()["personas"], {})

    def test_parse_rejects_each_case_with_a_reason(self):
        for why, over in self.CASES.items():
            with self.subTest(why=why):
                raw = json.dumps(self.body(**over)).encode()
                with self.assertRaises(run.SampleError):
                    run.parse_sample(raw, self.cfg.registry)

    def test_domain_slug_that_would_truncate_is_refused(self):
        long_persona = "p" * 60
        with self.assertRaises(run.SampleError):
            run.parse_sample(json.dumps(self.body(persona=long_persona)).encode(), self.cfg.registry)

    def test_schema1_naming_another_detector_is_still_rejected(self):
        d = {"schema": 1, "kind": "quota-probe-sample", "detector": DET, "persona": "recovery",
             "account": "claude-fallback", "outcome_kind": "read", "run_id": None, "sample_id": "s"}
        with self.assertRaises(run.SampleError):
            run.parse_sample(json.dumps(d).encode(), self.cfg.registry)

    def test_registry_entry_validation(self):
        bad = [dict(detector=qpd.DETECTOR), dict(detector="has:colon"), dict(harness="gpt"),
               dict(fault_kinds=frozenset()), dict(fault_kinds=frozenset({"a:b"})),
               dict(freshness_unit="no-placeholder.service")]
        for over in bad:
            kw = dict(detector="ok-det", subsystem="s", harness="claude", fault_kinds=frozenset({"k"}))
            kw.update(over)
            with self.subTest(over=over), self.assertRaises(ValueError):
                run.DropDetector(**kw)


class CrashSafety(EnvelopeRig):
    def test_kill_after_observe_before_unlink_replays_exactly_once(self):  # RFC-004 s6.2 shape
        self.drop_v2(run_id="B1")

        def target():
            def kill(point):
                if point == "after_observe_before_unlink":
                    os._exit(137)
            run.run_cycle(self.cfg, self.ports(), kill_hook=kill)
            os._exit(0)

        self.assertEqual(_child(target), 137)
        self.assertEqual(len(self.dropped()), 1, "file survives the crash")
        self.assertEqual(self.mn(), 1, "observe was durable")
        rep = self.cycle()
        self.assertEqual((rep.deduped, rep.ingested, self.mn(), self.dropped()), (1, 0, 1, []))

    def test_kill_before_observe_is_durable_counts_once_on_replay(self):
        self.drop_v2(run_id="B1")

        def target():
            def die(*a, **k):
                os._exit(137)
            with mock.patch.object(es, "observe", die):
                run.run_cycle(self.cfg, self.ports())
            os._exit(0)

        self.assertEqual(_child(target), 137)
        self.assertEqual((len(self.dropped()), self.mn()), (1, 0))
        rep = self.cycle()
        self.assertEqual((rep.ingested, rep.deduped, self.mn(), self.dropped()), (1, 0, 1, []))


class Routing(EnvelopeRig):  # RFC-004 s6.6 / s5 codex row
    def test_registry_spec_uses_repair_candidates_watchman_first(self):
        spec = MAIL.spec_for("recovery", "claude-fallback")
        self.assertEqual(spec.candidates, (rev.RECOVERY_CANDIDATE, rev.RECOVERY_CANDIDATE))
        self.assertEqual(spec.any_live_fallback, ())  # T-5998: registry specs unchanged
        self.assertEqual((spec.harness, spec.requester, spec.domain_key), ("codex", "recovery", f"{DET}:recovery"))

    def test_codex_faulted_drop_episode_routes_other_harness_to_recovery(self):
        for _ in range(3):
            self.drop_v2(persona="specialist", account="claude")
        self.cycle()
        (ep,) = self.mdomain("specialist")["episodes"].values()
        self.assertEqual((ep["delivery"]["route"]["kind"], ep["delivery"]["route"]["target"]),
                         ("other_harness", "recovery"))

    def test_recovery_subject_drop_episode_never_routes_to_recovery(self):  # T-5999 Recovery condition
        for _ in range(3):
            self.drop_v2()  # fault_id mail-stale:recovery
        self.cycle()
        (ep,) = self.mdomain()["episodes"].values()
        route = ep["delivery"]["route"]
        self.assertEqual((route["kind"], route["target"], route["excluded_subject"]),
                         ("any_live", "watchman", ["recovery"]))


class Freshness(EnvelopeRig):
    def test_opt_in_detector_gets_its_own_freshness_domain(self):
        self.drop_v2(detector=FDET, kind="tg-inbound-silent")
        rep = run.run_cycle(self.cfg, self.ports(clock=FakeClock(step=4000)))
        self.assertEqual(self.journal.calls, ["pantheon-test-tg@recovery.service"])
        self.assertIn((f"{FDET}/recovery", "counted"), rep.freshness)
        doc = es.load_domain(fresh.domain_key("recovery", FDET), self.state)
        self.assertEqual(doc["runs"][fresh.DETECTOR]["fault_id"], f"sample-freshness-silent:{FDET}:recovery")
        self.assertFalse(es.store_path(fresh.domain_key("recovery"), self.state).exists(), "legacy domain untouched")
        self.assertIn("recovery", self.runner_state()["detector_seen"][FDET])

    def test_without_opt_in_no_freshness_even_when_stale(self):
        self.drop_v2()
        run.run_cycle(self.cfg, self.ports(clock=FakeClock(step=4000)))
        self.assertEqual(self.journal.calls, [])
        self.assertFalse(es.store_path(fresh.domain_key("recovery", DET), self.state).exists())


class Forget(EnvelopeRig):
    def test_forget_persona_closes_schema2_and_its_freshness_domains(self):
        for _ in range(3):
            self.drop_v2()
            self.drop_v2(detector=FDET, kind="tg-inbound-silent")
        self.cycle()
        self.assertTrue(any(e["state"] in (es.OPEN, es.EMITTED) for e in self.mdomain()["episodes"].values()))
        self.assertEqual(run.forget_persona(self.cfg, self.ports(), "recovery"), run.EXIT_OK)
        st = self.runner_state()
        self.assertEqual((st["detectors"][DET], st["detectors"][FDET]), ({}, {}))
        self.assertNotIn("recovery", st["detector_seen"].get(FDET, {}))
        for det in (DET, FDET):
            self.assertFalse([e for e in self.mdomain(det=det)["episodes"].values()
                              if e["state"] in (es.OPEN, es.EMITTED)], det)


class QuotaRealFormat(EnvelopeRig):
    """The schema-1 quota record, captured from the REAL writer (``qpd.feed``), still parses + ingests."""

    def test_fixture_is_the_real_writer_shape(self):
        d = json.loads(QUOTA_FIXTURE.read_text())
        live = qpd.sample_body(SimpleNamespace(persona="recovery", account="claude-fallback"),
                               qpd.ProbeOutcome(qpd.PROBE_ERROR, "x", "d"), T0)
        self.assertEqual(set(d), set(live), "writer drifted from the captured fixture")
        self.assertEqual((d["schema"], d["kind"], d["detector"]), (qpd.SAMPLE_SCHEMA, qpd.SAMPLE_KIND, qpd.DETECTOR))

    def test_real_fixture_parses_under_every_registry(self):
        raw = QUOTA_FIXTURE.read_bytes()
        for reg in (None, {}, self.cfg.registry):
            self.assertEqual(run.parse_sample(raw, reg), json.loads(raw))

    def test_real_fixture_ingests_on_the_legacy_path(self):
        shutil.copyfile(QUOTA_FIXTURE, self.drop / QUOTA_FIXTURE_NAME)
        rep = self.cycle()
        self.assertEqual((rep.ingested, rep.rejected), (1, 0))
        self.assertEqual(self.n("recovery"), 1)
        st = self.runner_state()
        self.assertEqual(st["personas"], {"recovery": "claude-fallback"})
        self.assertNotIn("detectors", st)
        spec = qpd.spec_for(SimpleNamespace(persona="recovery", account="claude-fallback"))
        self.assertEqual(spec.candidates, (rev.RECOVERY_CANDIDATE,))


class _Unchecked(run.DropDetector):
    """A registry entry that skips the name checks, to prove the RUNTIME layers stop an alias on their own."""

    def __post_init__(self):
        pass


PAGE_CORRUPT = "ORG-076 store domain corrupt or aliased"


class SlugAliases(EnvelopeRig):  # T-5999 review MED
    def file_bytes(self, key):
        return es.store_path(key, self.state).read_bytes()

    def unchecked_probe_registry(self):
        self.cfg.registry = {"claude-quota-probe": _Unchecked(
            detector="claude-quota-probe", subsystem="s", harness="claude", fault_kinds=frozenset({"k"}))}

    def test_reserved_prefix_aliases_are_refused_at_registration(self):
        for name in ("claude-quota-probe", "claude-quota-probe-readability-x", "org076-sample",
                     "org076-sample-freshness-x", "org076-store", "org076-cycle"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                run.DropDetector(detector=name, subsystem="s", harness="claude", fault_kinds=frozenset({"k"}))

    def test_exact_probe_case_is_rejected_and_the_quota_document_is_untouched(self):
        self.drop_sample(persona="recovery")
        self.cycle()
        quota_key = f"{qpd.DETECTOR}:recovery"
        before = self.file_bytes(quota_key)
        self.unchecked_probe_registry()
        self.drop_v2(raw=self.body(persona="readability-recovery", detector="claude-quota-probe", kind="k"),
                     persona="readability-recovery")
        rep = self.cycle()
        self.assertEqual((rep.rejected, rep.ingested), (1, 0))
        self.assertEqual(self.file_bytes(quota_key), before, "quota recovery document untouched")
        self.assertTrue(any("aliases the existing store file" in body for _s, body in self.pages))

    def test_reverse_order_alias_fails_closed_in_the_store(self):
        self.unchecked_probe_registry()
        self.drop_v2(raw=self.body(persona="readability-recovery", detector="claude-quota-probe", kind="k"),
                     persona="readability-recovery")
        self.cycle()
        alias_key = "claude-quota-probe:readability-recovery"
        before = self.file_bytes(alias_key)
        self.drop_sample(persona="recovery")
        rep = self.cycle()
        self.assertEqual((rep.ingested, rep.kept), (0, 1))
        self.assertEqual(self.file_bytes(alias_key), before)
        self.assertTrue(any("store-path slug alias" in e for e in rep.errors))

    def test_mail_colon_stale_recovery_vs_mail_stale_colon_recovery(self):
        mk = lambda d: run.DropDetector(detector=d, subsystem="s", harness="codex", fault_kinds=frozenset({"k"}))  # noqa: E731
        self.cfg.registry = {"mail": mk("mail"), "mail-stale": mk("mail-stale")}
        self.drop_v2(raw=self.body(persona="stale-recovery", detector="mail", kind="k"), persona="stale-recovery")
        self.assertEqual(self.cycle().ingested, 1)
        before = self.file_bytes("mail:stale-recovery")
        self.drop_v2(raw=self.body(persona="recovery", detector="mail-stale", kind="k"), persona="recovery")
        rep = self.cycle()
        self.assertEqual((rep.rejected, rep.ingested), (1, 0))
        self.assertEqual(self.file_bytes("mail:stale-recovery"), before)
        with self.assertRaises(es.StoreCorruptionError):
            es.load_domain("mail-stale:recovery", self.state)

    def test_legacy_freshness_vs_per_detector_freshness_alias(self):
        legacy = fresh.domain_key("test-tg-inbound-recovery")
        es.observe(fresh.spec_for("test-tg-inbound-recovery", "claude-fallback"),
                   es.Unknown(fresh.fault_silent("test-tg-inbound-recovery"), "e\n"), ports=self.ports(),
                   state_dir=self.state)
        before = self.file_bytes(legacy)
        self.assertEqual(es.store_path(fresh.domain_key("recovery", FDET), self.state), es.store_path(legacy, self.state))
        self.drop_v2(detector=FDET, kind="tg-inbound-silent")
        rep = self.cycle()
        self.assertEqual((rep.rejected, rep.ingested), (1, 0))
        self.assertEqual(self.file_bytes(legacy), before)

    def test_load_mismatch_raises_and_own_key_loads(self):
        es.observe(MAIL.spec_for("recovery", "claude-fallback"), es.Unknown("mail-stale:recovery", "e\n"),
                   ports=self.ports(), state_dir=self.state)
        self.assertEqual(es.load_domain(f"{DET}:recovery", self.state)["domain_key"], f"{DET}:recovery")
        for alias in (f"{DET}-recovery", f"{DET}_recovery", f"{DET.replace('-', ':', 1)}:recovery"):
            with self.subTest(alias=alias), self.assertRaises(es.StoreCorruptionError):
                es.load_domain(alias, self.state)

    def test_a_mismatched_file_does_not_wedge_other_domains_in_the_cycle(self):
        self.drop_sample(persona="recovery")
        self.drop_sample(persona="specialist", account="claude")
        self.drop_v2(persona="orchestrator", account="claude")
        self.cycle()
        path = es.store_path(f"{qpd.DETECTOR}:recovery", self.state)
        doc = json.loads(path.read_text())
        doc["domain_key"] = "someone-else:recovery"
        path.write_text(json.dumps(doc))
        self.drop_sample(persona="recovery")
        self.drop_sample(persona="specialist", account="claude")
        self.drop_v2(persona="orchestrator", account="claude")
        rep = self.cycle()
        self.assertEqual((rep.ingested, rep.kept, rep.exit_code), (2, 1, 4))
        self.assertEqual((self.n("specialist"), self.mn("orchestrator")), (2, 2))
        self.assertTrue(any(e.startswith(f"reconcile {qpd.DETECTOR}:recovery") for e in rep.errors))
        self.assertEqual(len([p for p in self.pages if p[0] == PAGE_CORRUPT]), 1)
        self.cycle()
        self.assertEqual(len([p for p in self.pages if p[0] == PAGE_CORRUPT]), 1, "paged once per domain")
        self.assertEqual(run.forget_persona(self.cfg, self.ports(), "recovery"), run.EXIT_PARTIAL)


class RejectedSchema2DoesNotRefreshQuotaSeen(EnvelopeRig):  # T-5999 review LOW
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()  # ONE clock across cycles, so a refresh would visibly move ``seen``

    def test_rejected_schema2_file_does_not_refresh_quota_seen(self):
        self.drop_sample(persona="recovery")
        self.cycle(clock=self.clock)
        seen = self.runner_state()["seen"]["recovery"]
        self.drop_v2(detector="not-registered")  # the file name carries __recovery__
        rep = self.cycle(clock=self.clock)
        self.assertEqual(rep.rejected, 1)
        self.assertEqual(self.runner_state()["seen"]["recovery"], seen)

    def test_rejected_unparseable_file_still_refreshes_as_in_bt3b(self):
        self.drop_sample(persona="recovery")
        self.cycle(clock=self.clock)
        seen = self.runner_state()["seen"]["recovery"]
        (self.drop / "00000000T000000000000__recovery__nr__1.json").write_text("{not json")
        self.cycle(clock=self.clock)
        self.assertGreater(self.runner_state()["seen"]["recovery"], seen)
