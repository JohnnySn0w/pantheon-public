#!/usr/bin/env python3
"""PMB-1 Track B (T-2518): the IFEval capability-regression instrument. Synthetic data and a FAKE model only; the
network stays blocked (``offline.install``).

The checker / scoring tests need the vendored checker's deps (nltk >= 3.9.1 with punkt_tab, langdetect,
immutabledict). Run them with the Track B venv:

    NLTK_DATA=~/.venvs/pmb1-trackb/nltk_data ~/.venvs/pmb1-trackb/bin/python -m unittest tools.test_pmb1_trackb

Under a python without them those tests SKIP with the reason; everything else runs anywhere.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib
import io
import json
import os
import re
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tools.pmb1 import backbone as bbm
from tools.pmb1 import cli, offline
from tools.pmb1 import realrun as rr
from tools.pmb1 import trackb as tb

_GUARD = None


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install(allow_ollama=True)   # IFEvalOllama's request shape is tested; urlopen stays blocked


def tearDownModule() -> None:
    _GUARD.close()


def _checker_missing() -> str | None:
    try:
        import immutabledict  # noqa: F401
        import langdetect  # noqa: F401
        import nltk
        nltk.data.find("tokenizers/punkt_tab/english/")
    except (ImportError, LookupError) as e:
        return f"vendored IFEval checker deps absent ({type(e).__name__}: {e}); run under ~/.venvs/pmb1-trackb with NLTK_DATA"
    return None


CHECKER_SKIP = _checker_missing()
needs_checker = unittest.skipIf(CHECKER_SKIP is not None, CHECKER_SKIP or "")

GEN_SENTINEL = "ZQXGENSENTINEL"
BLOCK_SENTINEL = "ZQXBLOCKSENTINEL"


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
    def __init__(self) -> None:
        self.entered = 0
        self.held = False

    def __call__(self):
        lock = self

        @contextlib.contextmanager
        def session():
            lock.entered += 1
            lock.held = True
            try:
                yield
            finally:
                lock.held = False
        return session()


def reply(content: str, done_reason: str = "stop", eval_count: int = 20) -> dict:
    return {"model": "fake", "message": {"role": "assistant", "content": content}, "done": True,
            "done_reason": done_reason, "prompt_eval_count": 30, "eval_count": eval_count}


class FakeGen:
    """The fake model: ``policy(user_message) -> raw /api/chat reply``. Each call advances the fake clock."""

    def __init__(self, policy, clock: FakeClock | None = None, step: float = 1.0, digest: str = "sha256:" + "a" * 64,
                 lock: FakeLock | None = None) -> None:
        self.policy, self.clock, self.step, self.digest, self.lock = policy, clock, step, digest, lock
        self.name, self.timeout, self.timeout_cap = "ollama:fake", 300.0, None
        self.asked: list[str] = []
        self.unloads = 0

    def ask(self, user: str) -> dict:
        if self.lock is not None:
            assert self.lock.held, "a generation ran outside the lock"
        self.asked.append(user)
        if self.clock is not None:
            self.clock.t += self.step
        return self.policy(user)

    def loaded_digest(self) -> str:
        return self.digest

    def unload(self) -> None:
        self.unloads += 1


def no_comma_prompts(n: int) -> list[dict]:
    """IFEval-shaped records (real instruction id and kwargs shape, from the dataset) for punctuation:no_comma."""
    return [{"key": 1000 + i, "prompt": f"Write about item {i} without commas.",
             "instruction_id_list": ["punctuation:no_comma"], "kwargs": [{}]} for i in range(n)]


def write_jsonl(path: Path, rows) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


class Fixture:
    """A temp dir with a prompt file and on/random inject files."""

    def __init__(self, n: int = 4, arms=("on", "random"), drop: tuple[str, int] | None = None) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.prompts = no_comma_prompts(n)
        self.prompt_file = write_jsonl(d / "prompts.jsonl", self.prompts)
        rows = [{"key": p["key"], "arm": a, "block": f"{BLOCK_SENTINEL} {a} memory for {p['key']}"}
                for a in arms for p in self.prompts if (a, p["key"]) != drop]
        self.inject = write_jsonl(d / "inject.jsonl", rows)
        self.out = d / "run"

    def cfg(self, **kw) -> tb.GenConfig:
        base = dict(model="fake:1b", out=self.out, prompts=self.prompt_file, inject=[self.inject], est_gen_s=5.0)
        base.update(kw)
        return tb.GenConfig(**base)

    def close(self) -> None:
        self.tmp.cleanup()


def arm_of(user: str) -> str:
    m = re.match(rf"{BLOCK_SENTINEL} (\w+) memory", user)
    return m.group(1) if m else "off"


def key_of(user: str) -> int:
    return 1000 + int(re.search(r"item (\d+)", user).group(1))


# ----------------------------------------------------------------------------------------------- the checker

@needs_checker
class TestVendoredChecker(unittest.TestCase):
    """Known pass/fail responses per instruction family; ids and kwargs shapes are taken from real IFEval records."""

    CASES = [
        ("keywords:existence", {"keywords": ["correlated", "experiencing"]},
         "We are experiencing correlated events.", "Nothing relevant here."),
        ("keywords:forbidden_words", {"forbidden_words": ["rock"]}, "I like jazz.", "I like rock music."),
        ("language:response_language", {"language": "en"},
         "This is a plain English sentence about the weather today and tomorrow.",
         "Ceci est une phrase entièrement en français sur la météo de demain matin."),
        ("length_constraints:number_words", {"relation": "less than", "num_words": 5},
         "one two three", "one two three four five six"),
        ("length_constraints:number_paragraphs", {"num_paragraphs": 2}, "first\n***\nsecond", "only one"),
        ("detectable_content:postscript", {"postscript_marker": "P.S."}, "Hi there.\nP.S. bye", "Hi there."),
        ("detectable_format:title", {}, "<<My Title>>\nbody", "no title at all"),
        ("detectable_format:number_bullet_lists", {"num_bullets": 2}, "* a\n* b", "* a"),
        ("detectable_format:json_format", {}, '{"a": 1}', "not json"),
        ("combination:repeat_prompt", {"prompt_to_repeat": "Say hi."}, "Say hi. Hi!", "Hi!"),
        ("startend:quotation", {}, '"hello"', "hello"),
        ("startend:end_checker", {"end_phrase": "Is there anything else?"},
         "Done. Is there anything else?", "Done."),
        ("change_case:english_lowercase", {}, "all lower case here", "Not Lower Case"),
        ("punctuation:no_comma", {}, "no commas here", "a, b"),
    ]

    def test_known_pass_and_fail_per_family(self) -> None:
        with tb._quiet_checker():
            for iid, kw, good, bad in self.CASES:
                p = {"key": 1, "prompt": "p", "instruction_id_list": [iid], "kwargs": [kw]}
                with self.subTest(iid=iid):
                    self.assertTrue(tb.score_one(p, good)["prompt_strict"], f"{iid} should pass: {good!r}")
                    self.assertFalse(tb.score_one(p, bad)["prompt_strict"], f"{iid} should fail: {bad!r}")
        families = {c[0].split(":")[0] for c in self.CASES}
        self.assertEqual(families, {"keywords", "language", "length_constraints", "detectable_content",
                                    "detectable_format", "combination", "startend", "change_case", "punctuation"})

    def test_loose_is_an_upper_bound_of_strict(self) -> None:
        p = {"key": 1, "prompt": "p", "instruction_id_list": ["startend:quotation"], "kwargs": [{}]}
        with tb._quiet_checker():
            r = tb.score_one(p, 'Sure:\n"hello"')
        self.assertFalse(r["prompt_strict"])
        self.assertTrue(r["prompt_loose"])

    def test_empty_response_fails_every_instruction(self) -> None:
        p = {"key": 1, "prompt": "p", "instruction_id_list": ["punctuation:no_comma"], "kwargs": [{}]}
        with tb._quiet_checker():
            r = tb.score_one(p, "   ")
        self.assertEqual((r["prompt_strict"], r["prompt_loose"]), (False, False))

    def test_every_dataset_instruction_id_is_registered(self) -> None:
        from tools.pmb1.ifeval import instructions_registry as reg
        ids = {i for p in tb.load_prompts(tb.DEFAULT_PROMPTS) for i in p["instruction_id_list"]}
        self.assertLessEqual(ids, set(reg.INSTRUCTION_DICT))

    def test_missing_punkt_fails_closed_and_never_downloads(self) -> None:
        import nltk
        from tools.pmb1.ifeval import instructions_util as iu
        downloads: list = []
        try:
            with mock.patch.object(nltk.data, "find", side_effect=LookupError("no punkt_tab")), \
                    mock.patch.object(nltk, "download", side_effect=lambda *a, **k: downloads.append(a)):
                with self.assertRaises(RuntimeError) as cm:   # reload makes a NEW NltkResourceMissing class
                    importlib.reload(iu)
            self.assertEqual(type(cm.exception).__name__, "NltkResourceMissing")
        finally:
            importlib.reload(iu)
        self.assertEqual(downloads, [])


class TestVendoredSource(unittest.TestCase):
    """Static: the vendored copy never downloads and never imports lm_eval. Runs without the checker's deps."""

    DIR = Path(tb.__file__).resolve().parent / "ifeval"

    def test_no_download_anywhere_in_the_vendored_code(self) -> None:
        for f in sorted(self.DIR.glob("*.py")):
            src = f.read_text(encoding="utf-8")
            with self.subTest(file=f.name):
                self.assertNotRegex(src, r"\.download\s*\(")
                self.assertNotIn("lm_eval", re.sub(r"^#.*$", "", src, flags=re.M).replace("lm_eval 0.4.8", ""))

    def test_the_prompt_file_is_the_pinned_release(self) -> None:
        self.assertEqual(tb.sha256_file(tb.DEFAULT_PROMPTS),
                         "6a85310ca8ce15eff755aa08a3a4ff931c7e273e7515ebb3c492ea85fd8288f2")
        self.assertEqual(len(tb.load_prompts(tb.DEFAULT_PROMPTS)), 541)


# ------------------------------------------------------------------------------------------------ inputs, arms

class TestInputs(unittest.TestCase):
    def test_render(self) -> None:
        self.assertEqual(tb.render("off", "P", None), "P")
        self.assertEqual(tb.render("on", "P", "B"), "B\n\nP")
        with self.assertRaises(tb.TrackBError):
            tb.render("on", "P", None)

    def test_a_missing_block_is_fatal_before_any_call(self) -> None:
        fx = Fixture(n=3, drop=("on", 1001))
        try:
            gen = FakeGen(lambda u: reply("x"))
            with self.assertRaisesRegex(tb.TrackBError, "arm 'on' is missing blocks for 1 of 3"):
                tb.generate(fx.cfg(), FakeLock(), clock=FakeClock(), generator_factory=lambda *a: gen)
            self.assertEqual(gen.asked, [])
        finally:
            fx.close()

    def test_bad_inject_rows_are_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            for rows, msg in [([{"key": 1, "arm": "off", "block": "b"}], "arm 'off'"),
                              ([{"key": 1, "arm": "on", "block": "b"}, {"key": 1, "arm": "on", "block": "c"}], "duplicate"),
                              ([{"key": "1", "arm": "on", "block": "b"}], "key: int")]:
                with self.subTest(msg=msg), self.assertRaisesRegex(tb.TrackBError, msg):
                    tb.load_inject([write_jsonl(Path(d) / "i.jsonl", rows)])
            blocks, _ = tb.load_inject([write_jsonl(Path(d) / "i.jsonl", [{"key": 99, "arm": "on", "block": "b"}])])
            with self.assertRaisesRegex(tb.TrackBError, "not in the prompt file"):
                tb.check_blocks(blocks, ["off", "on"], [], [1])

    def test_arms_must_include_off(self) -> None:
        fx = Fixture(n=2)
        try:
            with self.assertRaisesRegex(tb.TrackBError, "include off"):
                tb.plan(fx.cfg(arms=["on"]))
        finally:
            fx.close()


class TestRandomArm(unittest.TestCase):
    POOL = [f"doc {i} {{braces}}" for i in range(20)]

    def test_deterministic_and_seeded(self) -> None:
        a = tb.build_random([1, 2, 3], self.POOL, 3, 7, "M:\n{docs}", "\n- ")
        b = tb.build_random([1, 2, 3], self.POOL, 3, 7, "M:\n{docs}", "\n- ")
        c = tb.build_random([1, 2, 3], self.POOL, 3, 8, "M:\n{docs}", "\n- ")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual({r["arm"] for r in a}, {"random"})

    def test_a_keys_block_depends_only_on_seed_and_key(self) -> None:
        full = {r["key"]: r["block"] for r in tb.build_random([1, 2, 3], self.POOL, 2, 7, "{docs}", "|")}
        sub = {r["key"]: r["block"] for r in tb.build_random([3], self.POOL, 2, 7, "{docs}", "|")}
        self.assertEqual(sub[3], full[3])

    def test_rendering_uses_k_distinct_docs_and_keeps_braces(self) -> None:
        r = tb.build_random([5], self.POOL, 4, 1, "<mem>{docs}</mem>", "|")[0]
        inner = r["block"][len("<mem>"):-len("</mem>")].split("|")
        self.assertEqual(len(set(inner)), 4)
        self.assertTrue(all(d in self.POOL for d in inner))

    def test_template_from_on_metadata_wins_and_a_contradiction_is_refused(self) -> None:
        self.assertEqual(tb.resolve_template({"template": "A{docs}", "doc_sep": "|"}, None, None), ("A{docs}", "|"))
        self.assertEqual(tb.resolve_template(None, "B{docs}", None), ("B{docs}", "\n\n"))
        with self.assertRaises(tb.TrackBError):
            tb.resolve_template({"template": "A{docs}"}, "B{docs}", None)
        with self.assertRaises(tb.TrackBError):
            tb.resolve_template(None, None, None)
        with self.assertRaises(tb.TrackBError):
            tb.build_random([1], self.POOL, 2, 1, "no placeholder", "|")
        with self.assertRaises(tb.TrackBError):
            tb.build_random([1], self.POOL[:2], 3, 1, "{docs}", "|")

    def test_cli_build_random_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            on = write_jsonl(Path(d) / "on.jsonl", [{"meta": {"template": "MEM {docs}", "doc_sep": " / "}}]
                             + [{"key": k, "arm": "on", "block": "x"} for k in (1, 2)])
            pool = write_jsonl(Path(d) / "pool.jsonl", [{"text": t} for t in self.POOL])
            outs = []
            for name in ("a", "b"):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = cli.main(["trackb", "build-random", "--on", str(on), "--pool", str(pool), "--k", "2",
                                   "--seed", "3", "--out", str(Path(d) / f"{name}.jsonl")])
                self.assertEqual(rc, 0)
                outs.append((Path(d) / f"{name}.jsonl").read_bytes())
            self.assertEqual(outs[0], outs[1])
            blocks, meta = tb.load_inject([Path(d) / "a.jsonl"])
            self.assertEqual(sorted(blocks), [("random", 1), ("random", 2)])
            self.assertTrue(all(b.startswith("MEM ") for b in blocks.values()))
            self.assertEqual(meta["k"], 2)


# ------------------------------------------------------------------------------------------------- generation

class TestClassify(unittest.TestCase):
    def test_only_a_stopped_uncut_reply_is_a_response(self) -> None:
        self.assertEqual(tb.classify(reply(" a ")), (" a ", None))   # unstripped: IFEval checks raw bytes
        self.assertEqual(tb.classify(reply("a", done_reason="length")), (None, "output_truncated"))
        self.assertEqual(tb.classify(reply("a", eval_count=tb.NUM_PREDICT)), (None, "output_truncated"))
        self.assertEqual(tb.classify(dict(reply("a"), done_reason=None)), (None, "bad_done_reason"))
        self.assertEqual(tb.classify({"done_reason": "stop"}), (None, "malformed"))
        self.assertEqual(tb.classify({"_error": "prompt_truncated"}), (None, "prompt_truncated"))


class TestGenerate(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture(n=4)

    def tearDown(self) -> None:
        self.fx.close()

    def run_gen(self, gen: FakeGen, clock: FakeClock | None = None, lock: FakeLock | None = None, **kw) -> dict:
        return tb.generate(self.fx.cfg(**kw), lock or FakeLock(), clock=clock or FakeClock(),
                           generator_factory=lambda *a: gen)

    def test_every_arm_and_key_once_under_the_lock_then_unload(self) -> None:
        lock = FakeLock()
        gen = FakeGen(lambda u: reply(GEN_SENTINEL), lock=lock)
        s = self.run_gen(gen, lock=lock)
        self.assertEqual((s["items"], s["new_ok"], s["skipped_done"]), (12, 12, 0))
        self.assertEqual(sorted(arm_of(u) for u in gen.asked), ["off"] * 4 + ["on"] * 4 + ["random"] * 4)
        self.assertEqual(gen.unloads, s["sessions"])
        self.assertTrue(gen.asked[1].startswith(f"{BLOCK_SENTINEL} on memory for 1000\n\nWrite about item 0"))
        man = json.loads((self.fx.out / "manifest.json").read_text())
        self.assertEqual(man["options"]["num_predict"], tb.NUM_PREDICT)
        self.assertEqual(man["options"]["temperature"], 0)
        self.assertEqual(man["model_digest"], gen.digest)
        self.assertEqual(man["prompts_sha256"], tb.sha256_file(self.fx.prompt_file))
        self.assertEqual(man["inject"][0]["sha256"], tb.sha256_file(self.fx.inject))

    def test_resume_never_redoes_journalled_work(self) -> None:
        calls = {"n": 0}

        def policy(u: str) -> dict:
            calls["n"] += 1
            if calls["n"] == 6:   # transient (the server went away): stops the run, nothing journalled
                raise urllib.error.URLError(ConnectionRefusedError("killed mid-run"))
            return reply("ok")

        with self.assertRaises(urllib.error.URLError):
            self.run_gen(FakeGen(policy))
        self.assertEqual(len(tb.load_journal(self.fx.out)), 5)
        gen2 = FakeGen(lambda u: reply("ok"))
        s = self.run_gen(gen2)
        self.assertEqual((s["skipped_done"], s["new_ok"], len(gen2.asked)), (5, 7, 7))
        gen3 = FakeGen(lambda u: self.fail("a finished run asked the model again"))
        s3 = self.run_gen(gen3)
        self.assertEqual((s3["skipped_done"], s3["new_ok"], s3["sessions"]), (12, 0, 0))

    def test_a_torn_last_line_is_cut_and_redone(self) -> None:
        self.run_gen(FakeGen(lambda u: reply("ok")))
        j = self.fx.out / "journal.jsonl"
        lines = j.read_text().splitlines()
        j.write_text("\n".join(lines[:5]) + "\n" + lines[5][:17])   # killed mid-write of item 6
        gen = FakeGen(lambda u: reply("ok"))
        s = self.run_gen(gen)
        self.assertEqual((s["skipped_done"], len(gen.asked)), (5, 7))
        self.assertEqual(len(tb.load_journal(self.fx.out)), 12)

    def test_errors_are_journalled_and_never_redone(self) -> None:
        self.run_gen(FakeGen(lambda u: reply("cut", done_reason="length")))
        j = tb.load_journal(self.fx.out)
        self.assertEqual({e["status"] for e in j.values()}, {"error"})
        self.assertTrue(all("response" not in e for e in j.values()))
        gen2 = FakeGen(lambda u: reply("ok"))
        self.run_gen(gen2)
        self.assertEqual(gen2.asked, [])

    def test_a_resume_under_other_inputs_or_weights_is_refused(self) -> None:
        self.run_gen(FakeGen(lambda u: reply("ok")))
        with self.assertRaises(tb.ManifestMismatch):
            self.run_gen(FakeGen(lambda u: reply("ok")), arms=["off", "on"])
        fx2 = Fixture(n=4)
        try:
            tb.generate(fx2.cfg(), FakeLock(), clock=FakeClock(), generator_factory=lambda *a: FakeGen(lambda u: reply("ok")))
            j = fx2.out / "journal.jsonl"
            lines = j.read_text().splitlines()
            j.write_text("\n".join(lines[:3]) + "\n")
            with self.assertRaisesRegex(tb.ManifestMismatch, "digest"):
                tb.generate(fx2.cfg(), FakeLock(), clock=FakeClock(),
                            generator_factory=lambda *a: FakeGen(lambda u: reply("ok"), digest="sha256:" + "b" * 64))
        finally:
            fx2.close()

    def test_refuses_to_start_inside_the_ingest_window(self) -> None:
        lock = FakeLock()
        gen = FakeGen(lambda u: reply("ok"))
        with self.assertRaises(rr.InWindow):
            self.run_gen(gen, clock=FakeClock("2026-10-10T05:00:00"), lock=lock)
        self.assertEqual((lock.entered, gen.asked), (0, []))
        with self.assertRaises(rr.InWindow):   # too close to it to fit one call
            self.run_gen(gen, clock=FakeClock("2026-10-10T04:54:30"), lock=lock)
        self.assertEqual(lock.entered, 0)

    def test_sessions_batch_within_the_lock_budget_and_unload_each(self) -> None:
        clock = FakeClock()
        lock = FakeLock()
        gen = FakeGen(lambda u: reply("ok"), clock=clock, step=30.0, lock=lock)
        s = self.run_gen(gen, clock=clock, lock=lock, max_lock_s=100.0, gap_s=10.0, est_gen_s=40.0)
        self.assertGreater(s["sessions"], 1)
        self.assertEqual((lock.entered, gen.unloads, s["new_ok"]), (s["sessions"], s["sessions"], 12))
        self.assertEqual(clock.slept, [10.0] * (s["sessions"] - 1))

    def test_a_clamped_timeout_is_a_batch_boundary_but_a_first_call_timeout_raises(self) -> None:
        calls = {"n": 0}

        def policy(u: str) -> dict:
            calls["n"] += 1
            if calls["n"] == 3:   # the session budget (240 s) clamps the 300 s call timeout: the budget cut it
                raise TimeoutError("timed out")
            return reply("ok")
        gen = FakeGen(policy)
        s = self.run_gen(gen)
        self.assertEqual((s["sessions"], s["new_ok"], len(gen.asked)), (2, 12, 13))
        self.assertEqual(gen.unloads, 2)
        fx2 = Fixture(n=1)
        try:
            gen2 = FakeGen(lambda u: (_ for _ in ()).throw(TimeoutError("timed out")))
            with self.assertRaises(TimeoutError):
                tb.generate(fx2.cfg(), FakeLock(), clock=FakeClock(), generator_factory=lambda *a: gen2)
            self.assertEqual(gen2.unloads, 1)   # released and unloaded even on the way out
            self.assertEqual(tb.load_journal(fx2.out), {})
        finally:
            fx2.close()

    def test_a_truncated_generation_is_never_scored(self) -> None:
        """The reply passes no_comma, but Ollama cut it at the cap: an error, excluded from the matched set."""
        def policy(u: str) -> dict:
            if arm_of(u) == "on" and key_of(u) == 1001:
                return reply("no commas at all", done_reason="length", eval_count=tb.NUM_PREDICT)
            return reply("no commas at all")
        self.run_gen(FakeGen(policy))
        j = tb.load_journal(self.fx.out)
        e = j[("fake:1b", "on", 1001)]
        self.assertEqual((e["status"], e["error"]), ("error", "output_truncated"))
        self.assertNotIn("response", e)
        keys = tb.matched_keys(self.fx.prompts, j, "fake:1b", ["off", "on", "random"])
        self.assertEqual(keys, [1000, 1002, 1003])


class TestIFEvalOllamaShape(unittest.TestCase):
    def test_one_user_message_its_own_cap_and_the_raw_reply(self) -> None:
        seen = {}

        class Resp:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.body

        def fake(req, timeout):
            seen["body"] = json.loads(req.data)
            return Resp(json.dumps(reply("  raw reply  ", done_reason="length")).encode())

        with mock.patch.dict(os.environ, {"PMB1_OLLAMA_URL": "http://x.invalid"}), \
                mock.patch("urllib.request.urlopen", fake):
            g = tb.IFEvalOllama("qwen-x", num_ctx=4096)
            raw = g.ask("hello world")
        self.assertEqual(seen["body"]["messages"], [{"role": "user", "content": "hello world"}])
        self.assertEqual(seen["body"]["options"], {"temperature": 0, "num_ctx": 4096, "num_predict": tb.NUM_PREDICT})
        self.assertEqual(raw["message"]["content"], "  raw reply  ")
        self.assertEqual(tb.classify(raw), (None, "output_truncated"))
        self.assertEqual(bbm.NUM_PREDICT, 128)   # the other tracks' cap is untouched

    def test_the_guard_reserves_trackb_cap(self) -> None:
        with mock.patch.dict(os.environ, {"PMB1_OLLAMA_URL": "http://x.invalid"}):
            g = tb.IFEvalOllama("qwen-x", num_ctx=2048)
        raw = g.ask("x" * int((2048 - tb.NUM_PREDICT) * bbm.CHARS_PER_TOKEN_CONSERVATIVE + 10))
        self.assertEqual((raw["_error"], raw["_reached"]), ("prompt_truncated", False))

    def test_num_ctx_fits_the_longest_rendered_message(self) -> None:
        pl = tb.plan(tb.GenConfig(model="m", out=Path("/nonexistent"), arms=["off"]))
        longest = max(len(p["prompt"]) for p in pl.prompts)
        self.assertGreaterEqual(pl.num_ctx, longest / bbm.CHARS_PER_TOKEN_CONSERVATIVE + tb.NUM_PREDICT)


REPEAT_BODY = b'{"error":"prediction aborted, token repeat limit reached"}'   # captured, real Ollama 2026-10-19


class TestRepeatLimitAbort(unittest.TestCase):
    """T-2518: Ollama's deterministic "token repeat limit reached" HTTP 500 is a journalled per-item error; every
    other HTTPError still raises (transient semantics)."""

    def _ask(self, code: int, body: bytes | None):
        calls = {"n": 0}

        def fake(req, timeout):
            calls["n"] += 1
            fp = io.BytesIO(body) if body is not None else None
            raise urllib.error.HTTPError(req.full_url, code, "err", {}, fp)

        with mock.patch.dict(os.environ, {"PMB1_OLLAMA_URL": "http://x.invalid"}), \
                mock.patch("urllib.request.urlopen", fake):
            g = tb.IFEvalOllama("qwen-x", num_ctx=4096)
            return g, g.ask("hello world"), calls

    def test_a_repeat_limit_500_is_a_journalled_error(self) -> None:
        g, raw, calls = self._ask(500, REPEAT_BODY)
        self.assertEqual(raw["_error"], "repeat_aborted")
        self.assertIs(raw["_reached"], True)
        self.assertEqual(raw["_detail"], "prediction aborted, token repeat limit reached")
        self.assertEqual(tb.classify(raw), (None, "repeat_aborted"))
        self.assertEqual((g.retries, calls["n"]), (1, 2))

    def test_another_500_error_still_raises(self) -> None:
        with self.assertRaises(urllib.error.HTTPError):
            self._ask(500, b'{"error":"something else"}')

    def test_a_500_with_an_unparseable_body_still_raises(self) -> None:
        for body in (b"not json at all", b"", None, b'["token repeat limit reached"]',
                     b'{"error": 7}'):
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError):
                self._ask(500, body)

    def test_a_503_with_the_repeat_body_still_raises(self) -> None:
        with self.assertRaises(urllib.error.HTTPError):
            self._ask(503, REPEAT_BODY)


# ---------------------------------------------------------------------------------------------------- scoring

class TestVerdict(unittest.TestCase):
    def test_the_threshold_is_more_than_five_points(self) -> None:
        self.assertEqual(tb.verdict_of(-6.0), "FAIL")
        self.assertEqual(tb.verdict_of(-5.000001), "FAIL")
        self.assertEqual(tb.verdict_of(-5.0), "PASS")
        self.assertEqual(tb.verdict_of(-4.0), "PASS")
        self.assertEqual(tb.verdict_of(0.0), "PASS")
        self.assertEqual(tb.verdict_of(+6.0), "PASS")

    def test_matched_instances_drop_a_prompt_one_arm_failed(self) -> None:
        j = {("m", a, k): {"status": "ok", "response": "r"} for a in ("off", "on") for k in (1, 2, 3)}
        j[("m", "on", 2)] = {"status": "error", "error": "output_truncated"}
        del j[("m", "off", 3)]
        prompts = [{"key": k} for k in (1, 2, 3)]
        self.assertEqual(tb.matched_keys(prompts, j, "m", ["off", "on"]), [1])


def _run_and_score(n: int, on_fails: int, on_errors: int = 0, off_errors: int = 0, limit: int | None = None):
    """A full fake run over n no_comma prompts: off always complies, on adds a comma on ``on_fails`` prompts,
    random always complies. The LAST ``on_errors`` prompts' on replies are cut at the cap (errors); the FIRST
    ``off_errors`` prompts' off replies are too. Returns (score result, rc, captured stdout+stderr of the CLI)."""
    fx = Fixture(n=n)
    try:
        def policy(u: str) -> dict:
            arm, k = arm_of(u), key_of(u) - 1000
            if arm == "on" and k >= n - on_errors:
                return reply(GEN_SENTINEL, done_reason="length")
            if arm == "off" and k < off_errors:
                return reply(GEN_SENTINEL, done_reason="length")
            if arm == "on" and k < on_fails:
                return reply(f"{GEN_SENTINEL}, with a comma")
            return reply(f"{GEN_SENTINEL} with no comma")
        out = io.StringIO()
        gen = FakeGen(policy)
        with mock.patch.object(tb, "make_generator", lambda *a: gen), mock.patch.object(tb, "Clock", FakeClock), \
                mock.patch.object(cli, "_lock_factory", lambda spec: FakeLock()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc_gen = cli.main(["trackb", "gen", "--model", "fake:1b", "--out", str(fx.out), "--prompts",
                               str(fx.prompt_file), "--inject", str(fx.inject), "--lock-session", "fake:lock"]
                              + (["--limit", str(limit)] if limit else []))
            rc = cli.main(["trackb", "score", "--out", str(fx.out)])
            cli.main(["trackb", "score", "--out", str(fx.out), "--json"])
        assert rc_gen == 0, out.getvalue()
        return tb.score(fx.out), rc, out.getvalue()
    finally:
        fx.close()


@needs_checker
class TestScoring(unittest.TestCase):
    def test_minus_six_fails(self) -> None:
        res, rc, _ = _run_and_score(100, 6)
        self.assertEqual((res["verdict"], res["verdict_delta_pp"], rc), ("FAIL", -6.0, 1))

    def test_minus_four_passes(self) -> None:
        res, rc, _ = _run_and_score(100, 4)
        self.assertEqual((res["verdict"], res["verdict_delta_pp"], rc), ("PASS", -4.0, 0))

    def test_exactly_minus_five_passes(self) -> None:
        res, rc, _ = _run_and_score(100, 5)
        self.assertEqual((res["verdict"], res["verdict_delta_pp"], rc), ("PASS", -5.0, 0))

    def test_rates_ci_and_the_random_control(self) -> None:
        res, _, _ = _run_and_score(20, 2)
        self.assertEqual(res["rates_pct"]["off"]["prompt_strict"], 100.0)
        self.assertEqual(res["rates_pct"]["on"]["prompt_strict"], 90.0)
        d = res["deltas_pp"]["on-off"]["prompt_strict"]
        self.assertLessEqual(d["lo"], d["diff"])
        self.assertLessEqual(d["diff"], d["hi"])
        self.assertEqual(res["deltas_pp"]["random-off"]["prompt_strict"]["diff"], 0.0)

    def test_an_on_arm_error_is_scored_as_a_fail_not_dropped(self) -> None:
        res, _, _ = _run_and_score(10, 0, on_errors=1)
        self.assertEqual((res["n_prompts"], res["n_scored"], res["n_dropped_off_error"]), (10, 10, 0))
        self.assertEqual(res["counts"]["on"]["errors_by_kind"], {"output_truncated": 1})
        self.assertEqual(res["verdict_delta_pp"], -10.0)
        self.assertEqual(res["deltas_pp"]["on-off"]["prompt_strict_matched_only"], {"diff": 0.0, "n": 9})

    def test_on_arm_truncations_count_against_the_verdict_and_flip_it(self) -> None:
        """4 complied-wrongly + 2 truncated of 100: matched-only says -4.08 (would PASS); ITT says -6 (FAIL)."""
        res, rc, text = _run_and_score(100, 4, on_errors=2)
        self.assertEqual((res["verdict"], res["verdict_delta_pp"], rc), ("FAIL", -6.0, 1))
        self.assertEqual(res["deltas_pp"]["on-off"]["prompt_strict_matched_only"], {"diff": -4.0816, "n": 98})
        self.assertIn("matched-only (informational, not gated) -4.08 pp", text)

    def test_an_off_arm_error_drops_the_prompt_from_every_arm(self) -> None:
        res, rc, _ = _run_and_score(40, 0, off_errors=2)   # exactly 5% of the file: still a verdict
        self.assertEqual((res["n_scored"], res["n_dropped_off_error"], res["n_dropped_incomplete"]), (38, 2, 0))
        for arm in ("on", "random"):
            self.assertEqual(res["deltas_pp"][f"{arm}-off"]["prompt_strict"]["n"], 38)
        self.assertEqual(res["rates_pct"]["on"]["prompt_strict"], 100.0)
        self.assertEqual((res["verdict"], rc), ("PASS", 0))

    def test_off_errors_over_five_percent_give_no_verdict(self) -> None:
        res, rc, text = _run_and_score(40, 10, off_errors=3)   # 3 of 40 > 5%; on would otherwise FAIL
        self.assertEqual((res["verdict"], rc, res["n_dropped_off_error"]), ("NO_VERDICT", 2, 3))
        self.assertIn("baseline unreliable: the off arm errored on 3 of 40 prompts", text)
        self.assertNotIn("verdict_delta_pp", res)

    def test_a_limit_run_is_a_smoke_run_with_no_verdict(self) -> None:
        res, rc, text = _run_and_score(20, 10, limit=10)   # on would FAIL by -100 pp
        self.assertEqual((res["verdict"], rc, res["n_prompts"], res["n_prompt_file"]), ("NO_VERDICT", 2, 10, 20))
        self.assertIn("smoke run: 10 of 20 prompts", text)
        self.assertNotIn("VERDICT FAIL", text)

    def test_a_partial_run_is_incomplete_never_a_verdict(self) -> None:
        fx = Fixture(n=4)
        try:
            calls = {"n": 0}

            def policy(u: str) -> dict:
                calls["n"] += 1
                if calls["n"] == 6:
                    raise urllib.error.URLError(ConnectionRefusedError("killed"))
                return reply("a, comma" if arm_of(u) == "on" else "no comma")   # on would FAIL if scored
            with self.assertRaises(urllib.error.URLError):
                tb.generate(fx.cfg(), FakeLock(), clock=FakeClock(), generator_factory=lambda *a: FakeGen(policy))
            res = tb.score(fx.out)
            self.assertEqual((res["verdict"], res["n_scored"], res["n_dropped_incomplete"]), ("INCOMPLETE", 1, 3))
            self.assertIn("VERDICT INCOMPLETE", tb.render_summary(res))
        finally:
            fx.close()

    def test_scoring_logs_nothing_even_when_langdetect_fails(self) -> None:
        """L5: instructions.py logs the response TEXT at ERROR when langdetect finds no features."""
        import logging
        p = {"key": 1, "prompt": "p", "instruction_id_list": ["language:response_language"], "kwargs": [{"language": "en"}]}
        records: list = []

        class Grab(logging.Handler):
            def emit(self, record): records.append(record)
        root = logging.getLogger()
        h = Grab(level=logging.DEBUG)
        root.addHandler(h)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err), tb._quiet_checker():
                r = tb.score_one(p, "12345")
        finally:
            root.removeHandler(h)
        self.assertTrue(r["prompt_strict"])   # upstream: undetectable language counts as following
        self.assertEqual((records, err.getvalue()), ([], ""))

    def test_itt_keys_drop_only_off_errors(self) -> None:
        j = {("m", a, k): {"status": "ok"} for a in ("off", "on") for k in (1, 2, 3)}
        j[("m", "on", 2)] = {"status": "error"}
        j[("m", "off", 3)] = {"status": "error"}
        self.assertEqual(tb.itt_keys([{"key": k} for k in (1, 2, 3)], j, "m", ["off", "on"]), [1, 2])

    def test_nothing_on_stdout_contains_generation_or_block_text(self) -> None:
        _, _, text = _run_and_score(6, 2, on_errors=1)
        self.assertIn("VERDICT", text)
        self.assertNotIn(GEN_SENTINEL, text)
        self.assertNotIn(BLOCK_SENTINEL, text)
        self.assertNotIn("comma", text)


class TestLockFailsClosed(unittest.TestCase):
    def test_a_real_gen_without_lock_session_is_refused(self) -> None:
        fx = Fixture(n=2)
        try:
            err = io.StringIO()
            built: list = []
            with mock.patch.object(tb, "make_generator", lambda *a: built.append(a)), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = cli.main(["trackb", "gen", "--model", "fake:1b", "--out", str(fx.out), "--prompts",
                               str(fx.prompt_file), "--inject", str(fx.inject)])
            self.assertEqual(rc, 2)
            self.assertIn("needs --lock-session", err.getvalue())
            self.assertEqual(built, [])
            self.assertFalse(fx.out.exists())
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = cli.main(["trackb", "gen", "--model", "fake:1b", "--out", str(fx.out), "--prompts",
                               str(fx.prompt_file), "--inject", str(fx.inject), "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertIn("trackb plan", out.getvalue())
        finally:
            fx.close()


class TestThink(unittest.TestCase):
    def _body(self, **kw) -> dict:
        seen = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(reply("x")).encode()

        def fake(req, timeout):
            seen["body"] = json.loads(req.data)
            return Resp()

        with mock.patch.dict(os.environ, {"PMB1_OLLAMA_URL": "http://x.invalid"}), \
                mock.patch("urllib.request.urlopen", fake):
            tb.IFEvalOllama("qwen3.5:4b-bf16", num_ctx=4096, **kw).ask("hi")
        return seen["body"]

    def test_off_sends_top_level_think_false_and_model_default_sends_none(self) -> None:
        self.assertIs(self._body()["think"], False)
        self.assertIs(self._body(think="off")["think"], False)
        self.assertNotIn("think", self._body(think="model-default"))
        self.assertNotIn("think", self._body(think="model-default")["options"])
        with self.assertRaises(ValueError):
            tb.IFEvalOllama("m", num_ctx=4096, base_url="http://x.invalid", think="on")

    def test_other_callers_send_no_think_field(self) -> None:
        with mock.patch.dict(os.environ, {"PMB1_OLLAMA_URL": "http://x.invalid"}):
            b = bbm.OllamaBackbone("m", "f")
        self.assertEqual(b.request_extra, {})

    def test_the_thinking_field_is_never_the_response_nor_scored(self) -> None:
        fx = Fixture(n=3)
        try:
            def policy(u: str) -> dict:
                r = reply("no commas in the answer")
                r["message"]["thinking"] = f"{GEN_SENTINEL}, reasoning, with, commas"
                return r
            tb.generate(fx.cfg(), FakeLock(), clock=FakeClock(), generator_factory=lambda *a: FakeGen(policy))
            j = tb.load_journal(fx.out)
            self.assertTrue(all(e["response"] == "no commas in the answer" and e["thinking_present"] for e in j.values()))
            self.assertNotIn(GEN_SENTINEL, (fx.out / "journal.jsonl").read_text())
            if CHECKER_SKIP is None:
                res = tb.score(fx.out)
                self.assertEqual(res["rates_pct"]["on"]["prompt_strict"], 100.0)   # the commas were in thinking only
        finally:
            fx.close()

    def test_think_is_recorded_and_a_resume_under_another_value_is_refused(self) -> None:
        fx = Fixture(n=2)
        try:
            seen: list = []
            tb.generate(fx.cfg(), FakeLock(), clock=FakeClock(),
                        generator_factory=lambda *a: seen.append(a) or FakeGen(lambda u: reply("ok")))
            self.assertEqual(seen[0][-1], "off")
            man = json.loads((fx.out / "manifest.json").read_text())
            self.assertEqual((man["think"], man["request_extra"]), ("off", {"think": False}))
            with self.assertRaisesRegex(tb.ManifestMismatch, "think"):
                tb.generate(fx.cfg(think="model-default"), FakeLock(), clock=FakeClock(),
                            generator_factory=lambda *a: FakeGen(lambda u: reply("ok")))
            with self.assertRaises(tb.TrackBError):
                tb.plan(fx.cfg(think="on"))
        finally:
            fx.close()


class TestStdoutWithoutChecker(unittest.TestCase):
    def test_gen_prints_counts_only(self) -> None:
        fx = Fixture(n=3)
        try:
            out = io.StringIO()
            gen = FakeGen(lambda u: reply(GEN_SENTINEL))
            with mock.patch.object(tb, "make_generator", lambda *a: gen), mock.patch.object(tb, "Clock", FakeClock), \
                    mock.patch.object(cli, "_lock_factory", lambda spec: FakeLock()), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = cli.main(["trackb", "gen", "--model", "fake:1b", "--out", str(fx.out), "--prompts",
                               str(fx.prompt_file), "--inject", str(fx.inject), "--lock-session", "fake:lock"])
            self.assertEqual(rc, 0)
            self.assertIn('"new_ok": 9', out.getvalue())
            self.assertNotIn(GEN_SENTINEL, out.getvalue())
            self.assertNotIn(BLOCK_SENTINEL, out.getvalue())
        finally:
            fx.close()


if __name__ == "__main__":
    unittest.main()
