#!/usr/bin/env python3
"""PMB-1 realrun driver (T-2518 step 2g): fakes only. A fake clock, a fake lock session, a fake Ollama (urlopen
mocked), the stub backbones. No network, no real Ollama."""
from __future__ import annotations

import contextlib
import datetime as dt
import glob
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.pmb1 import backbone as bbm
from tools.pmb1 import cli, offline, realrun as rr
from tools.pmb1.backbone import StubBackbone
from tools.pmb1.fixtures_v3 import generate_by_version
from tools.pmb1.runner import SUBSTRATES, GateRunRefused, run_one

_GUARD = None


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install(allow_ollama=True)


def tearDownModule() -> None:
    _GUARD.close()


class FakeClock(rr.Clock):
    def __init__(self, start: str = "2026-10-10T12:00:00") -> None:
        self.t = 0.0
        self.base = dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc)
        self.slept: list[float] = []

    def utc(self) -> dt.datetime:
        return self.base + dt.timedelta(seconds=self.t)

    def mono(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


class FakeLock:
    """Records every session: when it was held, when released, the clock at both. ``wait``: fake seconds the
    flock blocks before it is acquired."""

    def __init__(self, clock: FakeClock, events: list | None = None, wait: float = 0.0) -> None:
        self.clock, self.sessions, self.wait = clock, [], wait
        self.events = events if events is not None else []

    def __call__(self):
        lock = self

        class S(rr.LockSession):
            def __enter__(s):
                lock.clock.t += lock.wait
                s.rec = {"start": lock.clock.t, "start_utc": lock.clock.utc(), "end": None}
                lock.sessions.append(s.rec)
                lock.events.append(("enter",))
                return s

            def __exit__(s, *e):
                s.rec["end"] = lock.clock.t
                lock.events.append(("exit",))
        return S()


class SlowStub(StubBackbone):
    """A stub whose every call costs ``cost`` fake seconds."""

    def __init__(self, clock: FakeClock, cost: float, mode: str = "strict", die_after: int | None = None,
                 events: list | None = None, cold: float = 0.0) -> None:
        super().__init__(mode)
        self.clock, self.cost, self.die_after, self.n = clock, cost, die_after, 0
        self.events, self.cold, self.loaded = events, cold, False

    def unload(self) -> None:
        if self.events is not None:
            self.events.append(("unload", self.name))
        self.loaded = False

    def _tick(self) -> None:
        if not self.loaded:
            self.loaded = True
            self.clock.t += self.cold
        self.n += 1
        if self.die_after is not None and self.n > self.die_after:
            raise KeyboardInterrupt("killed")
        self.clock.t += self.cost

    def generate(self, question, evidence):
        self._tick()
        return super().generate(question, evidence)

    def judge(self, question, answer, gold):
        r = super().judge(question, answer, gold)
        if not r:  # only a mismatch is a model call, like OllamaBackbone
            self._tick()
        return r


class SpecStub(SlowStub):
    """A SlowStub named as the backbone a real spec builds; residency is shared per model (``resident``), so the
    cold load is paid on the first call after each unload, i.e. once per session."""

    def __init__(self, clock: FakeClock, cost: float, spec: str, resident: set | None = None, cold: float = 0.0,
                 die_after: int | None = None, events: list | None = None) -> None:
        super().__init__(clock, cost, die_after=die_after, events=events, cold=cold)
        self.name, self.family = rr.backbone_name(spec), rr.backbone_family(spec)
        self.resident = resident if resident is not None else set()

    def unload(self) -> None:
        super().unload()
        self.resident.discard(self.name)

    def _tick(self) -> None:
        if self.name not in self.resident:
            self.resident.add(self.name)
            self.clock.t += self.cold
        self.n += 1
        if self.die_after is not None and self.n > self.die_after:
            raise KeyboardInterrupt("killed")
        self.clock.t += self.cost


def cfg_for(out: str, **kw) -> rr.RunConfig:
    d = dict(out=Path(out), fixture_size="tiny", fixture_version=3, seeds=[1], backbones=["stub", "stub-lenient"],
             substrates=["amnesiac", "random"], embedder_is_lexical_stub=True, max_lock_s=60.0, gap_s=45.0, est_call_s=5.0)
    d.update(kw)
    return rr.RunConfig(**d)


def strip(rec: dict) -> dict:
    r = json.loads(json.dumps(rec))
    r.pop("created", None)
    r.pop("realrun", None)
    for k in list(r["cost"]):
        if k.endswith("_seconds"):
            r["cost"][k] = 0
    return r


class TestOllamaOptions(unittest.TestCase):
    def _call(self, resp: dict, **kw):
        seen = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(resp).encode()

        def fake(req, timeout):
            seen["body"] = json.loads(req.data)
            return Resp()

        with mock.patch.dict("os.environ", {"PMB1_OLLAMA_URL": "http://x.invalid"}), mock.patch("urllib.request.urlopen", fake):
            b = bbm.OllamaBackbone("m", "f", **kw)
            out = b.generate("What is A's b?", ["A's b is cccccc."])
        return seen["body"], out

    def test_num_ctx_and_keep_alive_reach_the_request(self) -> None:
        body, _ = self._call({"message": {"content": "c"}, "prompt_eval_count": 60}, keep_alive="2m", num_ctx=4096)
        self.assertEqual(body["options"], {"temperature": 0, "num_ctx": 4096, "num_predict": bbm.NUM_PREDICT})
        self.assertEqual(body["keep_alive"], "2m")

    def test_unload_posts_keep_alive_zero_for_the_model(self) -> None:
        seen = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"{}"

        def fake(req, timeout):
            seen["url"], seen["body"] = req.full_url, json.loads(req.data)
            return Resp()

        with mock.patch.dict("os.environ", {"PMB1_OLLAMA_URL": "http://x.invalid:11434"}), mock.patch("urllib.request.urlopen", fake):
            bbm.OllamaBackbone("qwen2.5:14b", "qwen2").unload()
        self.assertEqual(seen["url"], "http://x.invalid:11434/api/generate")
        self.assertEqual(seen["body"], {"model": "qwen2.5:14b", "keep_alive": 0})

    def test_a_short_prompt_eval_count_raises(self) -> None:
        # Ollama keeps ~half the window when it truncates, so a count far below the prompt's words is a cut prompt
        with self.assertRaises(bbm.TruncatedPrompt):
            self._call({"message": {"content": "c"}, "prompt_eval_count": 1})

    def test_a_prompt_that_reached_num_ctx_minus_num_predict_raises(self) -> None:
        edge = 4096 - bbm.NUM_PREDICT
        with self.assertRaises(bbm.TruncatedPrompt):
            self._call({"message": {"content": "c"}, "prompt_eval_count": edge}, num_ctx=4096)
        _, out = self._call({"message": {"content": "c"}, "prompt_eval_count": edge - 1}, num_ctx=4096)
        self.assertEqual(out, "c")

    def test_a_missing_prompt_eval_count_raises(self) -> None:
        with self.assertRaises(bbm.TruncatedPrompt):
            self._call({"message": {"content": "c"}})

    def test_a_prompt_over_num_ctx_is_never_sent(self) -> None:
        with mock.patch.dict("os.environ", {"PMB1_OLLAMA_URL": "http://x.invalid"}), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("sent")):
            b = bbm.OllamaBackbone("m", "f", num_ctx=10)
            with self.assertRaises(bbm.TruncatedPrompt):
                b.generate("q", ["word " * 50])

    def test_the_guard_can_be_turned_off(self) -> None:
        body, out = self._call({"message": {"content": "c"}}, truncation_guard=False)
        self.assertEqual(out, "c")


class TestResume(unittest.TestCase):
    def test_kill_after_unit_k_then_rerun_skips_and_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            made = []

            def factory_dying(spec, num_ctx):
                b = SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient", die_after=20 if len(made) >= 2 else None)
                made.append(spec)
                return b

            cfg = cfg_for(out, max_lock_s=1e9)
            with self.assertRaises(KeyboardInterrupt):
                rr.realrun(cfg, factory_dying, FakeLock(clock), clock=clock)
            done = sorted(glob.glob(os.path.join(out, "seed-*", "*", "*.json")))
            self.assertGreaterEqual(len(done), 2)
            self.assertLess(len(done), 4)
            mtimes = {p: os.stat(p).st_mtime_ns for p in done}
            s = rr.realrun(cfg, lambda spec, n: SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient"), FakeLock(clock), clock=clock)
            self.assertEqual((s["skipped"], s["units"]), (len(done), 4))
            self.assertEqual(len(glob.glob(os.path.join(out, "seed-*", "*", "*.json"))), 4)
            self.assertEqual({p: os.stat(p).st_mtime_ns for p in done}, mtimes)  # skipped, not rewritten
            s = rr.realrun(cfg, lambda spec, n: self.fail("nothing is pending"), FakeLock(clock), clock=clock)
            self.assertEqual(s["ran"], 0)

    def test_a_kill_inside_a_unit_resumes_from_the_journal(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            cfg = cfg_for(out, substrates=["amnesiac"], max_lock_s=1e9)
            with self.assertRaises(KeyboardInterrupt):
                rr.realrun(cfg, lambda spec, n: SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient", die_after=50), FakeLock(clock), clock=clock)
            fresh = []
            rr.realrun(cfg, lambda spec, n: fresh.append(SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient")) or fresh[-1], FakeLock(clock), clock=clock)
            self.assertLess(fresh[0].n, 346)  # the journalled 50 calls were not asked of the model again

    def test_an_invalid_or_foreign_record_is_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            cfg = cfg_for(out, substrates=["amnesiac"], max_lock_s=1e9)
            mk = lambda spec, n: SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient")  # noqa: E731
            rr.realrun(cfg, mk, FakeLock(clock), clock=clock)
            p = Path(out) / "seed-1" / "stub-strict" / "amnesiac.json"
            p.write_text('{"truncated": ')
            s = rr.realrun(cfg, mk, FakeLock(clock), clock=clock)
            self.assertEqual(s["ran"], 1)


class TestLockDiscipline(unittest.TestCase):
    def run_it(self, out: str, clock: FakeClock, cost: float = 4.0, **kw):
        lock = FakeLock(clock)
        cfg = cfg_for(out, **kw)
        s = rr.realrun(cfg, lambda spec, n: SlowStub(clock, cost, "strict" if spec == "stub" else "lenient"), lock, clock=clock)
        return s, lock

    def test_no_session_exceeds_the_budget_all_released_gaps_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            s, lock = self.run_it(out, clock, cost=4.0, max_lock_s=60.0, gap_s=45.0, est_call_s=4.0)
            self.assertGreater(len(lock.sessions), 3)
            for x in lock.sessions:
                self.assertIsNotNone(x["end"], "every session is released")
                self.assertLessEqual(x["end"] - x["start"], 60.0)
            for a, b in zip(lock.sessions, lock.sessions[1:]):
                self.assertGreaterEqual(b["start"] - a["end"], 45.0)
            self.assertEqual(len(lock.sessions), s["sessions"])

    def test_a_session_is_released_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            lock = FakeLock(clock)
            with self.assertRaises(KeyboardInterrupt):
                rr.realrun(cfg_for(out), lambda spec, n: SlowStub(clock, 1.0, die_after=5), lock, clock=clock)
            self.assertTrue(all(x["end"] is not None for x in lock.sessions))

    def test_no_session_starts_in_the_ingest_window(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock("2026-10-10T04:56:00")
            lock = FakeLock(clock)
            with self.assertRaises(rr.InWindow) as cm:
                rr.realrun(cfg_for(out), lambda spec, n: SlowStub(clock, 1.0), lock, clock=clock)
            self.assertEqual(lock.sessions, [])
            self.assertEqual(cm.exception.until.strftime("%H:%M"), "05:50")

    def test_a_gap_that_lands_in_the_window_stops_the_run_resumably(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock("2026-10-10T04:52:00")
            lock = FakeLock(clock)
            cfg = cfg_for(out, max_lock_s=120.0, est_call_s=4.0)
            with self.assertRaises(rr.InWindow):
                rr.realrun(cfg, lambda spec, n: SlowStub(clock, 4.0, "strict" if spec == "stub" else "lenient"), lock, clock=clock)
            for x in lock.sessions:
                self.assertFalse(rr.in_window(x["start_utc"]))
                # and a session started before the window never runs into it
                self.assertLessEqual(x["end"] - x["start"], 3 * 60 + 1)
            self.assertTrue(lock.sessions)

    def test_the_first_call_of_a_session_is_always_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            s, lock = self.run_it(out, clock, cost=100.0, max_lock_s=10.0, est_call_s=4.0, substrates=["amnesiac"], backbones=["stub", "stub-lenient"])
            self.assertEqual(s["ran"], 2)  # progress despite calls (unexpectedly) longer than the whole budget


def sessions_of(events: list) -> list[list]:
    out, cur = [], None
    for e in events:
        if e[0] == "enter":
            cur = []
        elif e[0] == "exit":
            out.append(cur)
            cur = None
        else:
            assert cur is not None, "event outside a session"
            cur.append(e)
    assert cur is None
    return out


class TestResidency(unittest.TestCase):
    def test_every_used_model_is_unloaded_once_per_session_before_release_one_family_each(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock, events = FakeClock(), []
            made = []

            def mk(spec, n):
                b = SlowStub(clock, 4.0, "strict" if spec == "stub" else "lenient", events=events)
                made.append(b)
                return b

            cfg = cfg_for(out, max_lock_s=60.0, est_call_s=4.0, gap_s=1.0)
            s = rr.realrun(cfg, mk, FakeLock(clock, events), clock=clock)
            segs = sessions_of(events)
            self.assertEqual(len(segs), s["sessions"])
            self.assertGreater(len(segs), 4)
            for seg in segs:
                names = [e[1] for e in seg if e[0] == "unload"]
                self.assertEqual(len(names), 1)   # exactly one model resident per session, unloaded once
            self.assertEqual({e[1] for seg in segs for e in seg}, {"stub", "stub-lenient"})
            # the sessions of a family are contiguous (backbone-major): never both models in one session
            order = [seg[0][1] for seg in segs]
            self.assertEqual(order, sorted(order, key=lambda n: n != "stub"))

    def test_unload_runs_on_the_exception_path(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock, events = FakeClock(), []
            with self.assertRaises(KeyboardInterrupt):
                rr.realrun(cfg_for(out), lambda spec, n: SlowStub(clock, 1.0, die_after=5, events=events), FakeLock(clock, events), clock=clock)
            segs = sessions_of(events)
            self.assertEqual(segs, [[("unload", "stub")]])

    def test_an_unload_failure_is_loud_and_does_not_skip_the_other_models(self) -> None:
        class Bad(SlowStub):
            def unload(self):
                raise OSError("ollama down")

        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            with self.assertRaises(rr.UnloadFailed):
                rr.realrun(cfg_for(out), lambda spec, n: Bad(clock, 1.0), FakeLock(clock), clock=clock)

    def test_cold_load_is_charged_once_per_session(self) -> None:
        # ollama: specs, so cold_load_s > 0 (review F4); the fake model loads (18 s) on its first call per session
        with tempfile.TemporaryDirectory() as out:
            clock, resident = FakeClock(), set()
            specs = ["ollama:fake-a:fa", "ollama:fake-b:fb"]
            self.assertEqual(rr.cold_load_s(specs[0]), rr.COLD_S)
            lock = FakeLock(clock)
            cfg = cfg_for(out, backbones=specs, substrates=["amnesiac"], max_lock_s=60.0, gap_s=1.0, est_call_s=2.0)
            s = rr.realrun(cfg, lambda spec, n: SpecStub(clock, 2.0, spec, resident, cold=rr.COLD_S), lock, clock=clock)
            held = [x["end"] - x["start"] for x in lock.sessions]
            self.assertTrue(all(h <= 60.0 for h in held))
            self.assertGreater(max(held), 56.0)   # charging the load twice would stop near 60 - COLD_S
            # the dry-run simulator packs the same calls into the same number of sessions
            costs = []
            for u in rr.plan_units(cfg.seeds, specs, cfg.substrates):
                ents = [json.loads(x) for x in (Path(out) / ".journal" / u.rel().with_suffix(".jsonl")).read_text().splitlines()[1:]]
                costs.append((u.spec, [2.0] * sum(1 for e in ents if e["m"])))
            self.assertEqual(rr.simulate_sessions(costs, rr.COLD_S, 60.0)[0], s["sessions"])


class TestSizing(unittest.TestCase):
    def test_measured_table_then_observed_latency(self) -> None:
        e = rr.Estimator()
        self.assertEqual((e.get("ollama:llama3.1:8b", False), e.get("ollama:llama3.1:8b", True)), (0.3, 2.0))
        self.assertEqual((e.get("ollama:qwen2.5:14b", False), e.get("ollama:qwen2.5:14b", True)), (0.5, 5.0))
        self.assertEqual(e.get("ollama:other:x", True), rr.EST_CALL_S)
        e.observe("ollama:qwen2.5:14b", True, 7.5)
        self.assertEqual(e.get("ollama:qwen2.5:14b", True), 7.5)
        self.assertEqual(e.get("ollama:qwen2.5:14b", False), 0.5)

    def test_num_ctx_uses_the_guards_conservative_chars_per_token(self) -> None:
        self.assertIs(rr.CHARS_PER_TOKEN_CONSERVATIVE, bbm.CHARS_PER_TOKEN_CONSERVATIVE)   # one shared constant
        self.assertEqual(bbm.CHARS_PER_TOKEN_CONSERVATIVE, 2.5)
        fx = generate_by_version(1, "small", 3)
        chars = sum(len(t) + 3 for _, _, t in fx.episodes + fx.restricted) + sum(len(s.new_text) + 3 for s in fx.supersedes)
        n = rr.required_num_ctx(fx)
        floor = chars / 2.5 + bbm.NUM_PREDICT + 512
        self.assertEqual(n % 1024, 0)
        self.assertGreaterEqual(n, floor)
        self.assertLess(n, floor + 2048)

    def test_simulate_sessions_packs_one_family_per_session(self) -> None:
        n, held = rr.simulate_sessions([("a", [1.0] * 10), ("b", [1.0] * 10)], cold_s=18.0, cap_s=30.0)
        self.assertEqual(n, 2)   # 12s of room per session: a (10 calls) fits one, b cannot share it
        self.assertEqual(held, 2 * 18.0 + 20.0)
        n, _ = rr.simulate_sessions([("a", [5.0] * 10)], cold_s=18.0, cap_s=30.0)
        self.assertEqual(n, 5)   # 2 calls per 12s


class TestBatchMerge(unittest.TestCase):
    def test_batched_record_equals_the_unbatched_one(self) -> None:
        fx = generate_by_version(1, "tiny", 3)
        for sub in ("amnesiac", "random", "full", "rag"):
            with tempfile.TemporaryDirectory() as o1, tempfile.TemporaryDirectory() as o2:
                c1, c2 = FakeClock(), FakeClock()
                kw = dict(substrates=[sub], backbones=["stub", "stub-lenient"])
                mk = lambda c: (lambda spec, n: SlowStub(c, 3.0, "strict" if spec == "stub" else "lenient"))  # noqa: E731
                l1, l2 = FakeLock(c1), FakeLock(c2)
                rr.realrun(cfg_for(o1, max_lock_s=1e9, **kw), mk(c1), l1, clock=c1)
                rr.realrun(cfg_for(o2, max_lock_s=30.0, est_call_s=3.0, gap_s=1.0, **kw), mk(c2), l2, clock=c2)
                self.assertEqual(len(l1.sessions), 2)  # one family per session
                self.assertGreater(len(l2.sessions), 5)
                for fam, mode in (("stub-strict", "strict"), ("stub-lenient", "lenient")):
                    a = json.loads((Path(o1) / "seed-1" / fam / f"{sub}.json").read_text())
                    b = json.loads((Path(o2) / "seed-1" / fam / f"{sub}.json").read_text())
                    self.assertEqual(strip(a), strip(b))
                    # and equal to a plain, unwrapped run_one (the record is the unbatched record by construction)
                    plain = run_one(lambda: SUBSTRATES[sub](fx), StubBackbone(mode), fx, gate=True, embedder_is_lexical_stub=True)
                    self.assertEqual(strip(plain), strip(b))

    def test_a_nondeterministic_replay_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            cfg = cfg_for(out, substrates=["amnesiac"], max_lock_s=1e9)
            with self.assertRaises(KeyboardInterrupt):
                rr.realrun(cfg, lambda spec, n: SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient", die_after=20), FakeLock(clock), clock=clock)
            j = next(Path(out, ".journal").rglob("*.jsonl"))
            lines = j.read_text().splitlines()
            e = json.loads(lines[3])
            e["a"][0] = "something else"
            lines[3] = json.dumps(e)
            j.write_text("\n".join(lines) + "\n")
            with self.assertRaises(rr.JournalMismatch):
                rr.realrun(cfg, lambda spec, n: SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient"), FakeLock(clock), clock=clock)


class TestRecordsAndPreconditions(unittest.TestCase):
    def test_record_carries_num_ctx_keep_alive_and_timing(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()

            unloaded = []

            def factory(spec, num_ctx):
                with mock.patch.dict("os.environ", {"PMB1_OLLAMA_URL": "http://x.invalid"}):
                    b = bbm.OllamaBackbone(spec.split(":")[1], spec.split(":")[-1], keep_alive="2m", num_ctx=num_ctx)
                b._chat = lambda system, user: "UNKNOWN"
                b.unload = lambda: unloaded.append(b.model)
                return b

            rr.realrun(cfg_for(out, backbones=["ollama:m:a", "ollama:n:b"], substrates=["amnesiac"], max_lock_s=1e9), factory, FakeLock(clock), clock=clock)
            rec = json.loads((Path(out) / "seed-1" / "a" / "amnesiac.json").read_text())
            self.assertEqual(rec["realrun"]["keep_alive"], "2m")
            self.assertEqual(rec["realrun"]["num_ctx"], rr.required_num_ctx(generate_by_version(1, "tiny", 3)))
            self.assertIs(rec["realrun"]["truncation_guard"], True)
            t = json.loads((Path(out) / ".timing" / "seed-1" / "a" / "amnesiac.json").read_text())
            self.assertIn("latency_max_s", t)
            self.assertIn("wall_clock_s", t)
            self.assertTrue(json.loads((Path(out) / "seed-1" / "rank_stability.json").read_text()))

    def test_num_ctx_covers_the_full_context_prompt(self) -> None:
        for size in ("tiny", "small", "medium"):
            fx = generate_by_version(1, size, 3)
            full = " ".join(t for _, _, t in fx.episodes)
            self.assertGreater(rr.required_num_ctx(fx), len(full.split()))

    def test_one_family_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as out, self.assertRaises(GateRunRefused):
            rr.realrun(cfg_for(out, backbones=["ollama:a:x", "ollama:b:x"]), lambda s, n: None, FakeLock(FakeClock()), clock=FakeClock())

    def test_the_hashing_embedder_is_refused_unless_declared(self) -> None:
        with tempfile.TemporaryDirectory() as out, self.assertRaises(GateRunRefused):
            rr.realrun(cfg_for(out, substrates=["rag"], embedder_is_lexical_stub=False), lambda s, n: None, FakeLock(FakeClock()), clock=FakeClock())


def call(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class TestCliDryRun(unittest.TestCase):
    LINE = ["realrun", "--backbones", "ollama:qwen2.5:14b:qwen2,ollama:llama3.1:8b:llama",
            "--substrate", "governed,episodic,rag,random,full,amnesiac", "--lexical-stub-embedder"]

    def test_dry_run_prints_the_plan_and_calls_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as out, mock.patch("urllib.request.urlopen", side_effect=AssertionError("called")), \
                mock.patch.object(bbm.OllamaBackbone, "__init__", side_effect=AssertionError("constructed")):
            rc, stdout, _ = call(self.LINE + ["--fixture", "size=tiny,version=3", "--seeds", "1,2,3", "--out", out, "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertIn("seed-3/llama/amnesiac", stdout)
            self.assertIn("seed-1/qwen2/governed", stdout)
            self.assertRegex(stdout, r"units: 36 \(36 to run\)")
            self.assertRegex(stdout, r"estimated lock sessions: \d+")
            self.assertIn("num_ctx=", stdout)
            self.assertEqual(os.listdir(out), [])  # a dry run writes nothing

    def test_dry_run_skips_complete_units(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            rr.realrun(cfg_for(out, seeds=[1], backbones=["stub", "stub-lenient"], substrates=["amnesiac"], max_lock_s=1e9),
                       lambda spec, n: SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient"), FakeLock(clock), clock=clock)
            rc, stdout, _ = call(["realrun", "--backbones", "stub,stub-lenient", "--substrate", "amnesiac", "--lexical-stub-embedder",
                                  "--fixture", "size=tiny,version=3", "--seeds", "1", "--out", out, "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertIn("SKIP (complete)", stdout)
            self.assertIn("units: 2 (0 to run)", stdout)

    def test_the_cli_refuses_one_family_and_reports_the_window(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            rc, _, err = call(["realrun", "--backbones", "ollama:a:x", "--fixture", "size=tiny,version=3", "--lexical-stub-embedder", "--out", out, "--dry-run"])
            self.assertEqual(rc, 2)
            self.assertIn("2 different families", err)


LLAMA, LLAMA2, QWEN = "ollama:llama3.1:8b:llama", "ollama:llama3.2:3b:llama", "ollama:qwen2.5:14b:qwen"


def journal_of(out: str, spec: str, sub: str = "amnesiac", seed: int = 1) -> Path:
    return Path(out) / ".journal" / rr.Unit(seed, rr.backbone_family(spec), spec, sub).rel().with_suffix(".jsonl")


class TestModelIdentity(unittest.TestCase):
    """review F2: a resume never splices or skips across models of the same family."""

    def test_the_journal_starts_with_its_header(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            rr.realrun(cfg_for(out, backbones=[LLAMA, QWEN], substrates=["amnesiac"], max_lock_s=1e9),
                       lambda spec, n: SpecStub(clock, 0.0, spec), FakeLock(clock), clock=clock)
            h = json.loads(journal_of(out, LLAMA).read_text().splitlines()[0])["header"]
            fx = generate_by_version(1, "tiny", 3)
            self.assertEqual(h, {"spec": LLAMA, "model": "ollama:llama3.1:8b", "num_ctx": None, "fixture_sha256": fx.sha256})

    def test_a_resume_with_another_model_of_the_same_family_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            cfg = cfg_for(out, backbones=[LLAMA, QWEN], substrates=["amnesiac"], max_lock_s=1e9)
            with self.assertRaises(KeyboardInterrupt):
                rr.realrun(cfg, lambda spec, n: SpecStub(clock, 0.0, spec, die_after=20), FakeLock(clock), clock=clock)
            cfg2 = cfg_for(out, backbones=[LLAMA2, QWEN], substrates=["amnesiac"], max_lock_s=1e9)
            with self.assertRaises(rr.JournalMismatch) as cm:
                rr.realrun(cfg2, lambda spec, n: SpecStub(clock, 0.0, spec), FakeLock(clock), clock=clock)
            self.assertIn("llama3.1:8b", str(cm.exception))
            self.assertIn("llama3.2:3b", str(cm.exception))

    def test_a_changed_num_ctx_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            cfg = cfg_for(out, backbones=[LLAMA, QWEN], substrates=["amnesiac"], max_lock_s=1e9)

            def mk(extra):
                def f(spec, n):
                    b = SpecStub(clock, 0.0, spec, die_after=20 if extra == 0 else None)
                    b.num_ctx = n + extra
                    return b
                return f
            with self.assertRaises(KeyboardInterrupt):
                rr.realrun(cfg, mk(0), FakeLock(clock), clock=clock)
            with self.assertRaises(rr.JournalMismatch) as cm:
                rr.realrun(cfg, mk(1024), FakeLock(clock), clock=clock)
            self.assertIn("num_ctx", str(cm.exception))
            self.assertIn("resume with the same --seeds", str(cm.exception))

    def test_a_legacy_journal_without_a_header_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            cfg = cfg_for(out, backbones=[LLAMA, QWEN], substrates=["amnesiac"], max_lock_s=1e9)
            with self.assertRaises(KeyboardInterrupt):
                rr.realrun(cfg, lambda spec, n: SpecStub(clock, 0.0, spec, die_after=20), FakeLock(clock), clock=clock)
            j = journal_of(out, LLAMA)
            j.write_text("\n".join(j.read_text().splitlines()[1:]) + "\n")
            with self.assertRaises(rr.JournalMismatch) as cm:
                rr.realrun(cfg, lambda spec, n: SpecStub(clock, 0.0, spec), FakeLock(clock), clock=clock)
            self.assertIn("legacy", str(cm.exception))

    def test_a_stale_record_from_another_model_is_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            rr.realrun(cfg_for(out, backbones=[LLAMA, QWEN], substrates=["amnesiac"], max_lock_s=1e9),
                       lambda spec, n: SpecStub(clock, 0.0, spec), FakeLock(clock), clock=clock)
            fx = generate_by_version(1, "tiny", 3)
            path = Path(out) / "seed-1" / "llama" / "amnesiac.json"
            self.assertTrue(rr.unit_complete(path, fx, rr.Unit(1, "llama", LLAMA, "amnesiac")))
            self.assertFalse(rr.unit_complete(path, fx, rr.Unit(1, "llama", LLAMA2, "amnesiac")))
            journal_of(out, LLAMA).unlink()
            s = rr.realrun(cfg_for(out, backbones=[LLAMA2, QWEN], substrates=["amnesiac"], max_lock_s=1e9),
                           lambda spec, n: SpecStub(clock, 0.0, spec), FakeLock(clock), clock=clock)
            self.assertEqual((s["skipped"], s["ran"]), (1, 1))   # qwen skipped, the stale llama record rerun
            self.assertEqual(json.loads(path.read_text())["backbone"]["name"], "ollama:llama3.2:3b")

    def test_a_backbone_not_named_for_its_spec_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()

            def mk(spec, n):
                b = SpecStub(clock, 0.0, spec)
                b.name = "ollama:something-else"
                return b
            with self.assertRaises(ValueError):
                rr.realrun(cfg_for(out, backbones=[LLAMA, QWEN], substrates=["amnesiac"]), mk, FakeLock(clock), clock=clock)


class TestJournalTail(unittest.TestCase):
    """review F8: only a trailing partial LAST line is forgiven."""

    def _killed(self, out: str) -> tuple[rr.RunConfig, FakeClock]:
        clock = FakeClock()
        cfg = cfg_for(out, substrates=["amnesiac"], max_lock_s=1e9)
        with self.assertRaises(KeyboardInterrupt):
            rr.realrun(cfg, lambda spec, n: SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient", die_after=20), FakeLock(clock), clock=clock)
        return cfg, clock

    def test_a_trailing_partial_line_is_dropped_and_the_run_resumes(self) -> None:
        fx = generate_by_version(1, "tiny", 3)
        with tempfile.TemporaryDirectory() as out:
            cfg, clock = self._killed(out)
            j = next(Path(out, ".journal").rglob("*.jsonl"))
            n_good = len(j.read_text().splitlines())
            with j.open("a") as f:
                f.write('{"k": "g", "a": ["What is')
            rr.realrun(cfg, lambda spec, n: SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient"), FakeLock(clock), clock=clock)
            lines = j.read_text().splitlines()
            self.assertGreater(len(lines), n_good)
            for line in lines:
                json.loads(line)   # the partial line was cut, not concatenated onto
            rec = json.loads((Path(out) / "seed-1" / "stub-strict" / "amnesiac.json").read_text())
            plain = run_one(lambda: SUBSTRATES["amnesiac"](fx), StubBackbone("strict"), fx, gate=True, embedder_is_lexical_stub=True)
            self.assertEqual(strip(plain), strip(rec))

    def test_a_bad_line_that_is_not_the_last_raises(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            cfg, clock = self._killed(out)
            j = next(Path(out, ".journal").rglob("*.jsonl"))
            lines = j.read_text().splitlines()
            lines[5] = '{"k": "g", "a": ['
            j.write_text("\n".join(lines) + "\n")
            with self.assertRaises(rr.JournalMismatch) as cm:
                rr.realrun(cfg, lambda spec, n: SlowStub(clock, 0.0, "strict" if spec == "stub" else "lenient"), FakeLock(clock), clock=clock)
            self.assertIn("line 6", str(cm.exception))


class FakeOllama:
    """urlopen mocked against a fake clock: a request takes ``lat(kind, n)`` fake seconds, or times out at its
    ``timeout`` (the clock advances by the timeout, then TimeoutError), as a real socket would."""

    def __init__(self, clock: FakeClock, lat, pec: int = 37) -> None:
        self.clock, self.lat, self.pec = clock, lat, pec
        self.n = 0
        self.timeouts: list[tuple[str, float]] = []
        self.timed_out = 0

    def __call__(self, req, timeout):
        kind = "unload" if req.full_url.endswith("/api/generate") else "chat"
        if kind == "chat":
            self.n += 1
        self.timeouts.append((kind, timeout))
        lat = self.lat(kind, self.n)
        self.clock.t += min(lat, timeout)
        if lat > timeout:
            self.timed_out += 1
            raise TimeoutError(f"{kind} timed out after {timeout}s")
        body = json.dumps({"message": {"content": "UNKNOWN"}, "prompt_eval_count": self.pec}).encode()

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return body
        return Resp()

    def factory(self, spec: str, num_ctx: int):
        with mock.patch.dict("os.environ", {"PMB1_OLLAMA_URL": "http://x.invalid"}):
            parts = spec.split(":")
            return bbm.OllamaBackbone(":".join(parts[1:-1]), parts[-1], keep_alive="2m", num_ctx=num_ctx, timeout=120.0)


class TestHardLockCap(unittest.TestCase):
    """review F3: a hung model or unload can never hold the lock past the budget plus one unload per model."""

    BOUND_EPS = rr.MIN_CALL_TIMEOUT_S   # a call that starts with < 5 s left may run to its 5 s timeout floor

    def _run(self, out: str, clock: FakeClock, fake: FakeOllama, **kw):
        lock = FakeLock(clock)
        cfg = cfg_for(out, backbones=[LLAMA, QWEN], substrates=["amnesiac"], **kw)
        with mock.patch("urllib.request.urlopen", fake):
            try:
                return rr.realrun(cfg, fake.factory, lock, clock=clock), lock, None
            except BaseException as e:  # noqa: BLE001
                return None, lock, e

    def test_worst_case_hold_is_bounded(self) -> None:
        cases = {
            "first call and unload hang": (lambda kind, n: 1e6, TimeoutError),
            # a clamped later call that times out is a batch boundary (R3-1b); the hung unload then surfaces
            "a late call hangs past the estimate": (lambda kind, n: 1e6 if kind == "unload" or n > 40 else 1.0, rr.UnloadFailed),
            "calls slower than estimated, unload hangs": (lambda kind, n: 1e6 if kind == "unload" else 9.0, rr.UnloadFailed),
        }
        for label, (lat, want) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as out:
                clock = FakeClock()
                fake = FakeOllama(clock, lat)
                _, lock, err = self._run(out, clock, fake, max_lock_s=60.0, est_call_s=1.0, gap_s=1.0)
                self.assertIsInstance(err, want)
                if want is TimeoutError:   # the original error surfaces, the unload failure rides on it (F9)
                    self.assertTrue(any("UnloadFailed" in n for n in getattr(err, "__notes__", [])))
                self.assertTrue(lock.sessions)
                for x in lock.sessions:
                    self.assertLessEqual(x["end"] - x["start"], 60.0 + bbm.UNLOAD_TIMEOUT_S * 1 + self.BOUND_EPS)
                self.assertTrue(all(t == bbm.UNLOAD_TIMEOUT_S for k, t in fake.timeouts if k == "unload"))
                self.assertTrue(all(t <= 60.0 for k, t in fake.timeouts if k == "chat"))

    def test_a_healthy_run_caps_each_call_timeout_at_what_is_left(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            fake = FakeOllama(clock, lambda kind, n: 1.0)
            s, lock, err = self._run(out, clock, fake, max_lock_s=60.0, est_call_s=1.0, gap_s=1.0)
            self.assertIsNone(err)
            self.assertGreater(len(lock.sessions), 2)
            chats = [t for k, t in fake.timeouts if k == "chat"]
            self.assertEqual(max(chats), 60.0)
            self.assertLess(min(chats), 60.0)
            self.assertGreaterEqual(min(chats), rr.MIN_CALL_TIMEOUT_S)
            for x in lock.sessions:
                self.assertLessEqual(x["end"] - x["start"], 60.0 + self.BOUND_EPS)
            # every journalled model call keeps its raw prompt_eval_count (F1 recalibration)
            ents = [json.loads(x) for x in journal_of(out, LLAMA).read_text().splitlines()[1:]]
            self.assertTrue(ents)
            self.assertTrue(all(e["pec"] == 37 for e in ents if e["m"]))

    def test_a_clamped_call_that_times_out_is_a_batch_boundary(self) -> None:
        # call 30 takes 50 s: under call_timeout (120 s) but over what is left (~31 s), so its clamped timeout fires
        fx = generate_by_version(1, "tiny", 3)
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            fake = FakeOllama(clock, lambda kind, n: 50.0 if kind == "chat" and n == 30 else 1.0)
            s, lock, err = self._run(out, clock, fake, max_lock_s=60.0, est_call_s=1.0, gap_s=1.0)
            self.assertIsNone(err)
            self.assertEqual(fake.timed_out, 1)
            self.assertEqual(s["ran"], 2)   # the run completed in later sessions
            for x in lock.sessions:
                self.assertLessEqual(x["end"] - x["start"], 60.0 + self.BOUND_EPS)
            # nothing was journalled for the timed-out attempt: the record equals an all-healthy run's
            o2 = Path(out) / "healthy"
            c2 = FakeClock()
            f2 = FakeOllama(c2, lambda kind, n: 1.0)
            self._run(str(o2), c2, f2, max_lock_s=1e9, est_call_s=1.0, gap_s=1.0)
            for fam in ("llama", "qwen"):
                a = json.loads((Path(out) / "seed-1" / fam / "amnesiac.json").read_text())
                b = json.loads((o2 / "seed-1" / fam / "amnesiac.json").read_text())
                self.assertEqual(strip(a), strip(b))
                self.assertEqual(a["fixture"]["sha256"], fx.sha256)

    def test_a_call_hanging_past_call_timeout_still_raises(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            fake = FakeOllama(clock, lambda kind, n: 1e6 if kind == "chat" and n > 5 else 1.0)
            _, lock, err = self._run(out, clock, fake, max_lock_s=240.0, est_call_s=1.0, gap_s=1.0)
            self.assertIsInstance(err, TimeoutError)   # its timeout was the full call_timeout, not a budget clamp
            self.assertIn(("chat", 120.0), fake.timeouts[5:6])
            self.assertTrue(all(x["end"] is not None for x in lock.sessions))

    def test_a_budget_that_cannot_fit_one_call_starts_no_session(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            lock = FakeLock(clock)
            cfg = cfg_for(out, backbones=[LLAMA, QWEN], substrates=["amnesiac"], max_lock_s=19.0, est_call_s=5.0)
            with self.assertRaises(rr.BudgetTooSmall):   # cold 18 + llama's measured long call 2.0 > 19
                rr.realrun(cfg, lambda spec, n: SpecStub(clock, 1.0, spec), lock, clock=clock)
            self.assertEqual(lock.sessions, [])

    def test_too_close_to_the_window_starts_no_session(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock("2026-10-10T04:54:45")
            lock = FakeLock(clock)
            cfg = cfg_for(out, backbones=[LLAMA, QWEN], substrates=["amnesiac"], max_lock_s=60.0, est_call_s=5.0)
            with self.assertRaises(rr.InWindow) as cm:
                rr.realrun(cfg, lambda spec, n: SpecStub(clock, 1.0, spec), lock, clock=clock)
            self.assertEqual(lock.sessions, [])
            self.assertEqual(cm.exception.until.strftime("%H:%M"), "05:50")

    def test_the_budget_is_taken_after_a_blocking_flock(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock("2026-10-10T04:50:00")
            lock = FakeLock(clock, wait=200.0)   # acquired at 04:53:20: 100 s to the window, not 300
            cfg = cfg_for(out, max_lock_s=240.0, est_call_s=4.0)
            with self.assertRaises(rr.InWindow):
                rr.realrun(cfg, lambda spec, n: SlowStub(clock, 4.0, "strict" if spec == "stub" else "lenient"), lock, clock=clock)
            self.assertTrue(lock.sessions)
            x = lock.sessions[0]
            self.assertLessEqual(x["end"] - x["start"], 100.0)
            self.assertLessEqual(x["end"], 300.0)   # released by 04:55:00Z, the window start


class TestSessionsPerSpec(unittest.TestCase):
    """review F5: one model per session; num_ctx per model, the max over every seed."""

    def test_two_specs_of_one_family_get_separate_sessions(self) -> None:
        n, held = rr.simulate_sessions([("ollama:a:x", [1.0] * 3), ("ollama:b:x", [1.0] * 3)], cold_s=18.0, cap_s=100.0)
        self.assertEqual((n, held), (2, 2 * 18.0 + 6.0))

    def test_two_specs_of_one_family_are_refused_up_front(self) -> None:
        with tempfile.TemporaryDirectory() as out, self.assertRaises(GateRunRefused):
            rr.realrun(cfg_for(out, backbones=[LLAMA, LLAMA2, QWEN]), lambda s, n: None, FakeLock(FakeClock()), clock=FakeClock())

    def test_num_ctx_is_per_model_the_max_over_seeds(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock, seen = FakeClock(), []

            def mk(spec, n):
                seen.append((spec, n))
                return SpecStub(clock, 0.0, spec)
            s = rr.realrun(cfg_for(out, seeds=[1, 2, 3], backbones=[LLAMA, QWEN], substrates=["amnesiac"], max_lock_s=1e9), mk, FakeLock(clock), clock=clock)
            top = max(rr.required_num_ctx(generate_by_version(sd, "tiny", 3)) for sd in (1, 2, 3))
            self.assertEqual({n for _, n in seen}, {top})
            self.assertEqual(s["num_ctx"], {LLAMA: top, QWEN: top})


class ShortCircuitStub(SlowStub):
    """Judges like OllamaBackbone: an exact match is answered without a model call and is NOT counted."""

    def judge(self, question, answer, gold):
        if bbm.normalize(answer) == bbm.normalize(gold):
            return True
        self.calls += 1
        self._tick()
        return False


class TestCallCounting(unittest.TestCase):
    def test_backbone_calls_match_an_unbatched_run_with_a_short_circuiting_judge(self) -> None:
        fx = generate_by_version(1, "tiny", 3)
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            lock = FakeLock(clock)
            rr.realrun(cfg_for(out, substrates=["random"], max_lock_s=30.0, est_call_s=3.0, gap_s=1.0),
                       lambda spec, n: ShortCircuitStub(clock, 3.0, "strict" if spec == "stub" else "lenient"), lock, clock=clock)
            self.assertGreater(len(lock.sessions), 2)
            for fam, mode in (("stub-strict", "strict"), ("stub-lenient", "lenient")):
                rec = json.loads((Path(out) / "seed-1" / fam / "random.json").read_text())
                plain = run_one(lambda: SUBSTRATES["random"](fx), ShortCircuitStub(FakeClock(), 0.0, mode), fx, gate=True, embedder_is_lexical_stub=True)
                counting = run_one(lambda: SUBSTRATES["random"](fx), StubBackbone(mode), fx, gate=True, embedder_is_lexical_stub=True)
                self.assertEqual(rec["cost"]["backbone_calls"], plain["cost"]["backbone_calls"])
                self.assertLess(plain["cost"]["backbone_calls"], counting["cost"]["backbone_calls"])   # the short circuit fired
                self.assertEqual(strip(plain), strip(rec))


class TestErrorsSurface(unittest.TestCase):
    """review F9."""

    def test_an_unload_failure_does_not_mask_the_original_error(self) -> None:
        class Bad(SlowStub):
            def unload(self):
                raise OSError("ollama down")

        with tempfile.TemporaryDirectory() as out:
            clock, logged = FakeClock(), []
            with self.assertRaises(KeyboardInterrupt) as cm:
                rr.realrun(cfg_for(out), lambda spec, n: Bad(clock, 1.0, die_after=5), FakeLock(clock), clock=clock, log=logged.append)
            self.assertTrue(any("UnloadFailed" in n for n in cm.exception.__notes__))
            self.assertTrue(any("UNLOAD FAILED" in line for line in logged))

    def test_the_cli_refuses_a_truncated_prompt_and_an_unload_failure_cleanly(self) -> None:
        argv = ["realrun", "--backbones", f"{LLAMA},{QWEN}", "--fixture", "size=tiny,version=3", "--lexical-stub-embedder"]
        for exc, rc, word in ((bbm.TruncatedPrompt("too long"), 2, "refused: too long"),
                              (rr.UnloadFailed("ollama down"), 4, "unload failed")):
            with self.subTest(type(exc).__name__), tempfile.TemporaryDirectory() as out, \
                    mock.patch.object(cli, "realrun", side_effect=exc):
                got, _, err = call(argv + ["--out", out])
                self.assertEqual(got, rc)
                self.assertIn(word, err)
                self.assertNotIn("Traceback", err)

    def test_dry_run_classifies_long_calls_with_the_drivers_rule(self) -> None:
        fx = generate_by_version(1, "tiny", 3)
        self.assertFalse(any(long for _, long in rr.estimate_calls(fx, "amnesiac", True)))   # no evidence, short prompts
        with mock.patch.object(rr, "LONG_WORDS", 1):
            seq = rr.estimate_calls(fx, "amnesiac", True)
        self.assertTrue(all(long for k, long in seq if k == "g"))
        self.assertTrue(rr.is_long("a " * 1000, []))
        self.assertFalse(rr.is_long("a " * 999, []))


class TestRetriesSurface(unittest.TestCase):
    """T-4984: each unit's ``done`` log line carries its backbone's transport-retry count."""

    def test_the_done_line_carries_the_retry_count(self) -> None:
        class Retried(SlowStub):
            retries = 1

        with tempfile.TemporaryDirectory() as out:
            clock, logged = FakeClock(), []
            rr.realrun(cfg_for(out), lambda spec, n: (Retried if spec == "stub" else SlowStub)(
                clock, 0.0, "strict" if spec == "stub" else "lenient"), FakeLock(clock), clock=clock, log=logged.append)
        done = [line for line in logged if line.endswith(tuple(f": done, transport retries {n}" for n in (0, 1)))]
        self.assertEqual(len(done), 4)   # 1 seed x 2 substrates x 2 backbones
        self.assertEqual(sorted(line[-1] for line in done), ["0", "0", "1", "1"])


# ---------------------------------------------------------------------------------------------------- v4 (T-2518 S6a)
# DEV seeds only: 28-36 are reserved for the real run and are never generated here.
V4_SEEDS = (1, 2, 3)
V4_FAMS = ("ollama:fake-a:fa", "ollama:fake-b:fb")   # two fake families; never a real model
V4_GATE_SUBS = ["semantic", "episodic", "rag", "random", "full", "amnesiac"]   # the candidate, its over, the controls


class GuardedSpecStub(SpecStub):
    """A stub reader named for its spec (strict for the first family, lenient for the second) that ALSO drives every
    generate prompt through a real ``OllamaBackbone`` at the driver's ``num_ctx`` with its truncation guard on (the
    HTTP call is a fake answering with the prompt's word count). A v4 prompt num_ctx cannot hold raises."""

    def __init__(self, clock: FakeClock, spec: str, num_ctx: int, die_after: int | None = None) -> None:
        super().__init__(clock, 0.0, spec, die_after=die_after)
        self.mode = "strict" if spec == V4_FAMS[0] else "lenient"
        self.num_ctx = num_ctx
        with mock.patch.dict("os.environ", {"PMB1_OLLAMA_URL": "http://x.invalid"}):
            self.guard = bbm.OllamaBackbone("fake", "fake", num_ctx=num_ctx)
        self.guarded = 0

    @staticmethod
    def _fake(req, timeout):
        body = json.loads(req.data)
        words = sum(len(m["content"].split()) for m in body["messages"])

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"message": {"content": "x"}, "prompt_eval_count": words}).encode()
        return Resp()

    def generate(self, question, evidence):
        with mock.patch("urllib.request.urlopen", self._fake):
            self.guard.generate(question, evidence)   # TruncatedPrompt if num_ctx cannot hold it
        self.guarded += 1
        return super().generate(question, evidence)


def v4_cfg(out: str, **kw) -> rr.RunConfig:
    d = dict(out=Path(out), fixture_size="tiny", fixture_version=4, seeds=[1], backbones=list(V4_FAMS),
             substrates=["semantic"], embedder_is_lexical_stub=True, extractor_is_pattern_stub=True, max_lock_s=1e9,
             gap_s=1.0, est_call_s=5.0)
    d.update(kw)
    return rr.RunConfig(**d)


def v4_factory(clock: FakeClock, made: list, die: dict | None = None):
    def f(spec, num_ctx):
        made.append(GuardedSpecStub(clock, spec, num_ctx, die_after=(die or {}).get(spec)))
        return made[-1]
    return f


def canon(path: Path) -> bytes:
    """A record's bytes with only its volatile fields (``created``, ``realrun``, ``*_seconds``) zeroed."""
    return json.dumps(strip(json.loads(path.read_text())), indent=2, sort_keys=True).encode()


class TestV4GateRun(unittest.TestCase):
    """A v4 tiny realrun, DEV seeds 1-3, two fake families, the semantic tier with its over (episodic) and the
    controls, the pattern stub declared: every record validates and ``gate(2)`` gives a verdict."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        clock, cls.made = FakeClock(), []
        cls.summary = rr.realrun(v4_cfg(cls.tmp.name, seeds=list(V4_SEEDS), substrates=V4_GATE_SUBS),
                                 v4_factory(clock, cls.made), FakeLock(clock), clock=clock)
        cls.recs = [json.loads(p.read_text()) for p in sorted(cls.out.glob("seed-*/*/*.json"))]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_records_validate_and_declare_the_pattern_stub(self) -> None:
        from tools.pmb1.records import validate_record
        self.assertEqual(len(self.recs), len(V4_SEEDS) * len(V4_FAMS) * len(V4_GATE_SUBS))
        self.assertEqual((self.summary["units"], self.summary["ran"]), (36, 36))
        for r in self.recs:
            self.assertEqual(validate_record(r), [])
            self.assertEqual(r["fixture"]["version"], 4)
            self.assertIs(r["extractor_is_pattern_stub"], True)
            self.assertIn("A2c", r["tracks"])
            self.assertIn(r["realrun"]["num_ctx"], set(self.summary["num_ctx"].values()))

    def test_every_v4_prompt_fit_num_ctx_under_the_real_guard(self) -> None:
        self.assertGreater(sum(b.guarded for b in self.made), 0)
        n = max(rr.required_num_ctx(generate_by_version(sd, "tiny", 4)) for sd in V4_SEEDS)
        self.assertEqual(set(self.summary["num_ctx"].values()), {n})

    def test_gate_2_gives_a_verdict(self) -> None:
        from tools.pmb1.gate import gate
        v = gate(2, self.recs)
        self.assertEqual((v.tier, v.candidate), (2, "semantic-tier"))
        self.assertEqual(v.failed, (), v.render())   # the stub readers' all-real tier-2 gate passes (test_pmb1_semantic)
        self.assertTrue(any(n.startswith("R6:") and "semantic-tier" in n for n in v.notes))


class TestV4Preconditions(unittest.TestCase):
    def test_an_undeclared_pattern_stub_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            cfg = v4_cfg(out, extractor_is_pattern_stub=False)
            with self.assertRaisesRegex(GateRunRefused, "pattern-stub"):
                rr.realrun(cfg, lambda s, n: self.fail("no backbone before the refusal"), FakeLock(FakeClock()), clock=FakeClock())
            with self.assertRaisesRegex(GateRunRefused, "pattern-stub"):
                rr.dry_run_plan(cfg)
            self.assertEqual(os.listdir(out), [])

    def test_a_v4_run_without_a_pattern_stub_needs_no_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            clock = FakeClock()
            s = rr.realrun(v4_cfg(out, substrates=["amnesiac", "random"], extractor_is_pattern_stub=False),
                           v4_factory(clock, []), FakeLock(clock), clock=clock)
            self.assertEqual(s["ran"], 4)
            rec = json.loads((Path(out) / "seed-1" / "fa" / "amnesiac.json").read_text())
            self.assertIs(rec["extractor_is_pattern_stub"], False)
            # a resume under the other declaration reruns the unit: one batch never mixes R6 declarations
            u = rr.Unit(1, "fa", V4_FAMS[0], "amnesiac")
            fx = generate_by_version(1, "tiny", 4)
            self.assertTrue(rr.unit_complete(Path(out) / u.rel(), fx, u, None, False))
            self.assertFalse(rr.unit_complete(Path(out) / u.rel(), fx, u, None, True))

    def test_the_cli_flag_reaches_the_config_and_its_absence_refuses(self) -> None:
        argv = ["realrun", "--backbones", ",".join(V4_FAMS), "--substrate", "semantic", "--lexical-stub-embedder",
                "--fixture", "size=tiny,version=4", "--seeds", "1"]
        with tempfile.TemporaryDirectory() as out:
            rc, _, err = call(argv + ["--out", out])
            self.assertEqual(rc, 2)
            self.assertIn("--pattern-stub-extractor", err)
            with mock.patch.object(cli, "realrun", return_value={}) as m:
                rc, _, _ = call(argv + ["--out", out, "--pattern-stub-extractor"])
            self.assertEqual(rc, 0)
            self.assertIs(m.call_args.args[0].extractor_is_pattern_stub, True)

    def test_v2_and_v3_num_ctx_terms_are_unchanged_and_v4_holds_every_text(self) -> None:
        for v in (2, 3):
            fx = generate_by_version(1, "small", v)
            self.assertFalse(any(hasattr(fx, k) for k in ("changes", "conflicts", "controls", "derived")))
        fx = generate_by_version(1, "tiny", 4)
        texts = ([t for _, _, t in fx.episodes + fx.restricted] + [s.new_text for s in fx.supersedes]
                 + [p.other_text for p in fx.changes + fx.conflicts + fx.controls] + [d.text for d in fx.derived])
        self.assertGreaterEqual(rr.required_num_ctx(fx), sum(len(t) + 3 for t in texts) / 2.5 + bbm.NUM_PREDICT)


class TestV4ResumeAndAsOf(unittest.TestCase):
    """An interrupt mid-A2c (just after an as_of call was journalled) then a resume gives the uninterrupted run's
    records and journals; a journal whose plain and as_of entries are swapped is refused, never replayed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ref = Path(cls.tmp.name) / "ref"
        clock = FakeClock()
        rr.realrun(v4_cfg(str(cls.ref)), v4_factory(clock, []), FakeLock(clock), clock=clock)
        cls.jref = journal_of(str(cls.ref), V4_FAMS[0], "semantic")
        cls.ents = [json.loads(x) for x in cls.jref.read_text().splitlines()[1:]]
        ents = cls.ents
        # an A2c pair: g(q, plain evidence), j, g(q, as_of evidence): the SAME question, different evidence
        cls.pair = next(i for i in range(len(ents) - 2) if ents[i]["k"] == ents[i + 2]["k"] == "g"
                        and ents[i]["a"][0] == ents[i + 2]["a"][0] and ents[i]["a"][1] != ents[i + 2]["a"][1])
        cls.kill_after = sum(1 for e in ents[:cls.pair + 3] if e["m"])   # model calls up to the as_of generate

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_interrupt_then_resume_is_identical_to_uninterrupted(self) -> None:
        out = Path(self.tmp.name) / "resumed"
        clock = FakeClock()
        with self.assertRaises(KeyboardInterrupt):
            rr.realrun(v4_cfg(str(out)), v4_factory(clock, [], {V4_FAMS[0]: self.kill_after}), FakeLock(clock), clock=clock)
        self.assertFalse((out / "seed-1" / "fa" / "semantic.json").exists())
        made: list = []
        s = rr.realrun(v4_cfg(str(out)), v4_factory(clock, made), FakeLock(clock), clock=clock)
        self.assertEqual(s["ran"], 2)
        self.assertEqual(made[0].n, sum(1 for e in self.ents if e["m"]) - self.kill_after)   # journalled calls replayed
        for fam, spec in (("fa", V4_FAMS[0]), ("fb", V4_FAMS[1])):
            self.assertEqual(canon(out / "seed-1" / fam / "semantic.json"), canon(self.ref / "seed-1" / fam / "semantic.json"))
            self.assertEqual(journal_of(str(out), spec, "semantic").read_bytes(),
                             journal_of(str(self.ref), spec, "semantic").read_bytes())

    def test_a_swapped_as_of_entry_is_refused(self) -> None:
        import shutil
        out = Path(self.tmp.name) / "swapped"
        shutil.copytree(self.ref, out)
        j = journal_of(str(out), V4_FAMS[0], "semantic")
        lines = j.read_text().splitlines()
        a, b = self.pair + 1, self.pair + 3   # +1: the header line
        lines[a], lines[b] = lines[b], lines[a]
        j.write_text("\n".join(lines) + "\n")
        (out / "seed-1" / "fa" / "semantic.json").unlink()
        clock = FakeClock()
        with self.assertRaises(rr.JournalMismatch):
            rr.realrun(v4_cfg(str(out)), v4_factory(clock, []), FakeLock(clock), clock=clock)


class TestV4BatchMerge(unittest.TestCase):
    def test_a_batched_v4_record_equals_the_unbatched_one(self) -> None:
        fx = generate_by_version(1, "tiny", 4)
        for sub in ("full", "rag", "random", "amnesiac"):
            with self.subTest(sub), tempfile.TemporaryDirectory() as o1, tempfile.TemporaryDirectory() as o2:
                c1, c2 = FakeClock(), FakeClock()
                l2 = FakeLock(c2)
                mk = lambda c: (lambda spec, n: SpecStub(c, 3.0, spec))  # noqa: E731
                rr.realrun(v4_cfg(o1, substrates=[sub]), mk(c1), FakeLock(c1), clock=c1)
                rr.realrun(v4_cfg(o2, substrates=[sub], max_lock_s=60.0, est_call_s=3.0), mk(c2), l2, clock=c2)
                self.assertGreater(len(l2.sessions), 5)
                for fam in ("fa", "fb"):
                    self.assertEqual(canon(Path(o1) / "seed-1" / fam / f"{sub}.json"),
                                     canon(Path(o2) / "seed-1" / fam / f"{sub}.json"))
                plain = run_one(lambda: SUBSTRATES[sub](fx), StubBackbone("strict"), fx, gate=True,
                                embedder_is_lexical_stub=True, extractor_is_pattern_stub=True)
                b = strip(json.loads((Path(o2) / "seed-1" / "fa" / f"{sub}.json").read_text()))
                plain["backbone"] = b["backbone"]   # SpecStub is the strict stub, named for its spec
                self.assertEqual(strip(plain), b)


if __name__ == "__main__":
    unittest.main()
