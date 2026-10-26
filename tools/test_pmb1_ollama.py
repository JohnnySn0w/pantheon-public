#!/usr/bin/env python3
"""PMB-1 OllamaBackbone (T-2518): the HTTP shape with urlopen mocked. The one
module allowed to construct it; the network stays blocked regardless."""
from __future__ import annotations

import math
import os
import unittest
from unittest import mock

from tools.pmb1 import backbone as bbm
from tools.pmb1 import cli, offline

_GUARD = None


def setUpModule() -> None:
    global _GUARD
    _GUARD = offline.install(allow_ollama=True)


def tearDownModule() -> None:
    _GUARD.close()


class TestOllamaNeedsItsUrl(unittest.TestCase):
    def test_ollama_needs_its_url(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
            bbm.OllamaBackbone("m", "fam")

    def test_make_backbone_ollama_spec(self) -> None:
        with mock.patch.dict(os.environ, {"PMB1_OLLAMA_URL": "http://x.invalid"}):
            b = cli.make_backbone("ollama:qwen2.5:7b:qwen")
        self.assertEqual((b.model, b.family), ("qwen2.5:7b", "qwen"))

    def test_the_guard_blocks_the_real_network(self) -> None:
        import socket
        with self.assertRaises(AssertionError):
            socket.socket().connect(("192.0.2.1", 9))


class TestOllamaBackboneOffline(unittest.TestCase):

    def test_chat_request_shape_and_keep_alive(self) -> None:
        seen = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"message": {"content": " yes "}, "prompt_eval_count": 40}'

        def fake(req, timeout):
            import json
            seen["url"], seen["body"], seen["timeout"] = req.full_url, json.loads(req.data), timeout
            return Resp()

        with mock.patch.dict("os.environ", {"PMB1_OLLAMA_URL": "http://ollama.invalid:11434/"}), \
                mock.patch("urllib.request.urlopen", fake):
            b = bbm.OllamaBackbone("qwen-x", "qwen", keep_alive="30m")
            self.assertEqual(b.generate("What is A's b?", ["A's b is cccccc."]), "yes")
            self.assertTrue(b.judge("q", "different", "other"))
        self.assertEqual(seen["url"], "http://ollama.invalid:11434/api/chat")
        self.assertEqual(seen["body"]["keep_alive"], "30m")
        self.assertIs(seen["body"]["stream"], False)
        self.assertEqual(seen["body"]["model"], "qwen-x")
        self.assertEqual((b.name, b.family), ("ollama:qwen-x", "qwen"))


class TestTruncationGuard(unittest.TestCase):
    """T-2518 review F1 as corrected from the 2026-10-12 capture (pre-check on chars, post-check at
    num_ctx - num_predict AND the words lower bound) and F3
    (per-call timeout cap, short unload timeout)."""

    def _backbone(self, seen: list, pec=10, **kw):
        import json

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"message": {"content": "c"}, "prompt_eval_count": pec}).encode()

        def fake(req, timeout):
            seen.append((req.full_url, timeout))
            return Resp()

        with mock.patch.dict("os.environ", {"PMB1_OLLAMA_URL": "http://x.invalid"}):
            b = bbm.OllamaBackbone("m", "f", **kw)
        return b, mock.patch("urllib.request.urlopen", fake)

    def test_the_pre_check_counts_chars_not_words(self) -> None:
        seen: list = []
        b, patch = self._backbone(seen, num_ctx=1000)
        fits = int((1000 - bbm.NUM_PREDICT) * bbm.CHARS_PER_TOKEN_CONSERVATIVE)   # 2180 chars ~ 872 tokens
        with patch:
            self.assertEqual(b._chat("", "x" * fits), "c")
            with self.assertRaises(bbm.TruncatedPrompt):
                b._chat("", "x" * (fits + 1))
            with self.assertRaises(bbm.TruncatedPrompt):   # one "word", far too many chars
                bbm.OllamaBackbone("m", "f", base_url="http://x.invalid").generate("q", ["y" * 30000])
        self.assertEqual(len(seen), 1)   # the refused prompts were never sent

    def test_the_post_check_boundary(self) -> None:
        edge = 4096 - bbm.NUM_PREDICT
        for pec, refused in ((edge - 1, False), (edge, True), (4096, True)):
            b, patch = self._backbone([], pec=pec, num_ctx=4096)
            with self.subTest(pec=pec), patch:
                if refused:
                    with self.assertRaises(bbm.TruncatedPrompt):
                        b._chat("s", "u")
                else:
                    self.assertEqual(b._chat("s", "u"), "c")

    def test_the_lower_bound_refuses_a_count_below_the_words(self) -> None:
        b, patch = self._backbone([], pec=1799)
        with patch:
            with self.assertRaises(bbm.TruncatedPrompt):   # 1799 < 0.9 * 2001
                b._chat("s", "word " * 2000)
        self.assertEqual(b.last_prompt_eval_count, 1799)   # the raw count is kept for the journal, even when refused
        b, patch = self._backbone([], pec=1801)
        with patch:
            self.assertEqual(b._chat("s", "word " * 2000), "c")

    # CAPTURED values, not invented (D11): gpu-ml, Ollama 0.34.2, synthetic tiny-v3 prompts, 2026-10-12, relayed
    # by head_memory_orchestrator in the T-2518 REBOUND 2 F1 correction. Same chars/words/num_ctx for both models.
    # (label, model, chars, words, num_ctx, prompt_eval_count, truncated)
    CAPTURED = [
        ("A", "llama3.1:8b", 5690, 941, 3072, 1486, False),
        ("B warm prefix cache", "llama3.1:8b", 5683, 939, 3072, 1485, False),
        ("D oversize", "llama3.1:8b", 22226, 3677, 3072, 1538, True),
        ("E", "llama3.1:8b", 22226, 3677, 16384, 5782, False),
        ("A", "qwen2.5:14b", 5690, 941, 3072, 1507, False),
        ("B warm prefix cache", "qwen2.5:14b", 5683, 939, 3072, 1506, False),
        ("D oversize", "qwen2.5:14b", 22226, 3677, 3072, 1538, True),
        ("E", "qwen2.5:14b", 22226, 3677, 16384, 5872, False),
    ]

    def test_the_post_check_on_captured_values(self) -> None:
        for label, model, chars, words, num_ctx, n, truncated in self.CAPTURED:
            b, _ = self._backbone([], num_ctx=num_ctx)
            with self.subTest(f"{model} {label}"):
                if truncated:
                    # Ollama kept ~half the window: num_ctx - num_predict alone would pass it; the words bound catches it
                    self.assertLess(n, num_ctx - bbm.NUM_PREDICT)
                    with self.assertRaises(bbm.TruncatedPrompt) as cm:
                        b._check_not_truncated({"prompt_eval_count": n}, words)
                    self.assertIn("words", str(cm.exception))
                else:
                    b._check_not_truncated({"prompt_eval_count": n}, words)
                    # the char pre-check admits every prompt the capture shows fit, and 2.5 stays conservative
                    self.assertLessEqual(math.ceil(chars / bbm.CHARS_PER_TOKEN_CONSERVATIVE) + bbm.NUM_PREDICT, num_ctx)
                    self.assertGreater(chars / n, bbm.CHARS_PER_TOKEN_CONSERVATIVE)

    def test_num_predict_is_sent(self) -> None:
        self.assertEqual(bbm.NUM_PREDICT, 128)
        b, _ = self._backbone([])
        self.assertEqual(b.options()["num_predict"], bbm.NUM_PREDICT)

    def test_the_call_timeout_is_capped_and_unload_is_short(self) -> None:
        seen: list = []
        b, patch = self._backbone(seen, timeout=120.0)
        with patch:
            b._chat("s", "u")
            b.timeout_cap = 7.0
            b._chat("s", "u")
            b.timeout_cap = 500.0
            b._chat("s", "u")
            b.unload()
        self.assertEqual([t for _, t in seen], [120.0, 7.0, 120.0, bbm.UNLOAD_TIMEOUT_S])
        self.assertEqual(bbm.UNLOAD_TIMEOUT_S, 10)


class TestTransportRetry(unittest.TestCase):
    """T-4984: one transient HTTP 5xx from /api/chat is retried ONCE with the identical request (temperature 0, so
    it cannot bias the measurement) and counted on ``retries``; a 4xx, a second 5xx and a timeout still raise."""

    def _run(self, script: list):
        import io
        import json
        import urllib.error

        sent: list = []
        self.timeouts: list = []

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"message": {"content": "ok"}, "prompt_eval_count": 40}).encode()

        def fake(req, timeout):
            sent.append((req.full_url, req.data))
            self.timeouts.append(timeout)
            step = script.pop(0)
            if isinstance(step, int):
                raise urllib.error.HTTPError(req.full_url, step, "err", {}, io.BytesIO(b""))
            if isinstance(step, BaseException):
                raise step
            return Resp()

        with mock.patch.dict("os.environ", {"PMB1_OLLAMA_URL": "http://x.invalid"}):
            b = bbm.OllamaBackbone("m", "f")
        self.assertEqual(b.retries, 0)
        return b, sent, mock.patch("urllib.request.urlopen", fake)

    def test_a_500_then_200_succeeds_with_one_retry_of_the_identical_request(self) -> None:
        b, sent, patch = self._run([500, "ok"])
        with patch:
            self.assertEqual(b.generate("What is A's b?", ["A's b is cccccc."]), "ok")
        self.assertEqual(b.retries, 1)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])   # the identical request, byte for byte

    def test_two_500s_raise(self) -> None:
        import urllib.error
        b, sent, patch = self._run([500, 503])
        with patch, self.assertRaises(urllib.error.HTTPError) as cm:
            b.generate("q", ["e"])
        self.assertEqual(cm.exception.code, 503)
        self.assertEqual((b.retries, len(sent)), (1, 2))

    def test_a_400_raises_without_a_retry(self) -> None:
        import urllib.error
        b, sent, patch = self._run([400, "ok"])
        with patch, self.assertRaises(urllib.error.HTTPError) as cm:
            b.generate("q", ["e"])
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual((b.retries, len(sent)), (0, 1))

    def test_a_timeout_raises_without_a_retry(self) -> None:
        import urllib.error
        for err in (TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))):
            b, sent, patch = self._run([err, "ok"])
            with patch, self.assertRaises(type(err)):
                b.generate("q", ["e"])
            self.assertEqual((b.retries, len(sent)), (0, 1))

    def _capped(self, elapsed: float, script: list):
        """cap 60 s; the first attempt fakes ``elapsed`` seconds on the backbone's clock."""
        b, sent, patch = self._run(script)
        b.timeout_cap = 60.0
        return b, sent, patch, mock.patch.object(bbm, "_now", side_effect=[1000.0, 1000.0 + elapsed])

    def test_under_a_cap_the_retry_gets_only_the_remaining_time(self) -> None:
        b, sent, patch, clock = self._capped(55.0, [500, "ok"])
        with patch, clock:
            self.assertEqual(b.generate("q", ["e"]), "ok")
        self.assertEqual(self.timeouts, [60.0, 5.0])       # 60 - 55: never past the session budget
        self.assertLessEqual(self.timeouts[1], 60.0 - 55.0)
        self.assertEqual((b.retries, len(sent)), (1, 2))
        self.assertEqual(sent[0], sent[1])

    def test_under_a_cap_with_less_than_the_floor_left_the_original_error_is_raised(self) -> None:
        import urllib.error
        b, sent, patch, clock = self._capped(58.0, [500, "ok"])
        with patch, clock, self.assertRaises(urllib.error.HTTPError) as cm:
            b.generate("q", ["e"])
        self.assertEqual(cm.exception.code, 500)            # the ORIGINAL error
        self.assertEqual((b.retries, len(sent), self.timeouts), (0, 1, [60.0]))
        self.assertEqual(bbm.RETRY_FLOOR_S, 5.0)

    def test_without_a_cap_the_retry_gets_the_full_timeout(self) -> None:
        b, sent, patch = self._run([500, "ok"])
        with patch, mock.patch.object(bbm, "_now", side_effect=[0.0, 10_000.0]):
            b.generate("q", ["e"])
        self.assertIsNone(b.timeout_cap)
        self.assertEqual(self.timeouts, [b.timeout, b.timeout])

    def test_retries_accumulate_across_calls(self) -> None:
        b, sent, patch = self._run([500, "ok", "ok", 502, "ok"])
        with patch:
            for _ in range(3):
                b.generate("q", ["e"])
        self.assertEqual((b.retries, len(sent)), (2, 5))
