"""Pluggable generation + judging. No cloud calls, ever (brief section 0).

``StubBackbone`` is the deterministic one every CI test uses. ``OllamaBackbone``
is for real runs on VM210 later; it is never constructed at import time and
needs ``PMB1_OLLAMA_URL``. Never shell out to ``claude -p``.

Truncation guard, measured (captured 2026-10-12, Ollama 0.34.2, llama3.1:8b + qwen2.5:14b, synthetic tiny-v3
prompts):
* ``prompt_eval_count`` is the FULL prompt's token count even on a warm prefix-cache hit (llama 1486 cold,
  1485 cached), so a lower bound is safe.
* Ollama truncates an oversize prompt to about HALF the window: 22226 chars at num_ctx=3072 reported 1538,
  far below num_ctx (the same prompt at 16384 reported 5782 / 5872). So ``n >= num_ctx - NUM_PREDICT`` alone
  misses truncation; ``n < words * TRUNCATION_MARGIN`` is the check that catches it (1538 < 0.9 * 3677).
* ~3.8 chars per token and ~1.58 tokens per whitespace word when honest: 2.5 chars per token stays a safely
  conservative constant, and the 0.9 margin has no false positives.
Every call's raw ``prompt_eval_count`` is kept (``last_prompt_eval_count``) so the realrun journal records it.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Sequence

from .fixtures_v2 import PARAPHRASE

ABSTAIN = "UNKNOWN"

_QUESTION = re.compile(r"what is (\w+)'s (\w+)\?", re.IGNORECASE)
_FACT = re.compile(r"(\w+)'s (\w+) is (\w+)\.")
# v2 paraphrase queries carry no attribute word; a reader that knows the fixed
# lexicon (as any LLM would) maps them back. Records say so (reader_knows_lexicon).
_PARAPHRASE_Q = [
    (re.compile(re.escape(t).replace(re.escape("{e}"), r"(\w+)"), re.IGNORECASE), a)
    for a, t in PARAPHRASE.items()
]


def normalize(s: str) -> str:
    return " ".join(s.lower().split()).strip(" .")


class Backbone(ABC):
    name: str
    family: str
    knows_lexicon: bool = False  # can it map a v2 paraphrase query back to its attribute?

    def __init__(self) -> None:
        self.calls = 0

    @abstractmethod
    def generate(self, question: str, evidence: Sequence[str]) -> str: ...

    def unload(self) -> None:
        """Release whatever the backbone holds resident (a model on a shared GPU). Default: nothing."""

    def judge(self, question: str, answer: str, gold: str) -> bool:
        """Is ``answer`` the gold answer? Default: normalised exact match."""
        self.calls += 1
        return normalize(answer) == normalize(gold)


class StubBackbone(Backbone):
    """A deterministic extractive reader over the synthetic fact grammar.

    ``strict``: answers only from an evidence sentence matching the asked
    entity AND attribute (the first such sentence in evidence order), else
    abstains. Evidence order is the substrate's presentation order, which is
    not always its ranking: the episodic tier presents best LAST (T-4937).
    ``lenient``: as strict, but when nothing matches and any evidence exists it
    answers from the first evidence item: an over-trusting reader. The two have
    different ``family`` values so the two-family gate rule is testable.
    """

    knows_lexicon = True

    def __init__(self, mode: str = "strict") -> None:
        super().__init__()
        if mode not in ("strict", "lenient"):
            raise ValueError(f"unknown stub mode {mode!r}")
        self.mode = mode
        self.name = "stub" if mode == "strict" else "stub-lenient"
        self.family = f"stub-{mode}"

    def generate(self, question: str, evidence: Sequence[str]) -> str:
        self.calls += 1
        q = _QUESTION.search(question)
        ask = (q.group(1).lower(), q.group(2).lower()) if q else None
        for rx, a in ([] if ask else _PARAPHRASE_Q):
            pm = rx.search(question)
            if pm:
                ask = (pm.group(1).lower(), a)
                break
        facts = [m.groups() for e in evidence for m in _FACT.finditer(e)]  # v2 evidence holds several sentences
        if ask:
            ent, attr = ask
            for e, a, v in facts:
                if e.lower() == ent and a.lower() == attr:
                    return v
        if self.mode == "lenient" and facts:
            return facts[0][2]
        return ABSTAIN


class TruncatedPrompt(RuntimeError):
    """A call whose prompt Ollama may have cut to fit ``num_ctx``. Never scored (D11)."""


DEFAULT_NUM_CTX = 8192
# A conservative LOWER bound on chars per token (English BPE runs ~4), so chars / this over-estimates the
# prompt's tokens. One shared constant: realrun.required_num_ctx sizes num_ctx with it too.
CHARS_PER_TOKEN_CONSERVATIVE = 2.5
NUM_PREDICT = 128      # the reply's token cap, sent as options.num_predict and reserved inside num_ctx
UNLOAD_TIMEOUT_S = 10.0   # an unload never waits longer; keep_alive is the backstop
# A 5xx retry under a session budget (``timeout_cap``) gets only what the first attempt left of it, and is skipped
# (the original error raised) when that is under this floor: never hold the shared lock past the budget (T-4984).
# Equal to realrun.MIN_CALL_TIMEOUT_S, the floor of the cap itself.
RETRY_FLOOR_S = 5.0
_now = time.monotonic   # the retry's elapsed-time clock (a module seam, so tests can fake it)
# An honest prompt evaluates to ~1.58 tokens per whitespace word (captured), so fewer tokens than this share
# of its words means Ollama cut it.
TRUNCATION_MARGIN = 0.9


class OllamaBackbone(Backbone):
    """HTTP ``/api/chat`` against a local Ollama (VM210). Base URL from env
    ``PMB1_OLLAMA_URL``. ``keep_alive`` (``"5m"``; ``"2m"`` warm inside a lock session, with an explicit ``unload()`` on release) and
    ``num_ctx`` are sent on every request. With ``truncation_guard`` a call whose estimated prompt (chars /
    ``CHARS_PER_TOKEN_CONSERVATIVE``) plus ``NUM_PREDICT`` exceeds ``num_ctx``, or whose reported
    ``prompt_eval_count`` reaches ``num_ctx - NUM_PREDICT``, falls below ``TRUNCATION_MARGIN`` x the prompt's
    words, or is missing, RAISES ``TruncatedPrompt``: a ceiling measured on a truncated prompt is not a
    ceiling. ``timeout_cap``, when set (the realrun driver sets
    it to what is left of a lock session), caps each request's HTTP timeout."""

    knows_lexicon = True  # an LLM reads the paraphrase directly

    def __init__(
        self, model: str, family: str, *, base_url: str | None = None,
        keep_alive: str | int = "5m", timeout: float = 120.0, num_ctx: int = DEFAULT_NUM_CTX,
        truncation_guard: bool = True, num_predict: int = NUM_PREDICT,
    ) -> None:
        super().__init__()
        url = base_url or os.environ.get("PMB1_OLLAMA_URL", "")
        if not url:
            raise ValueError("OllamaBackbone needs PMB1_OLLAMA_URL (or base_url)")
        if num_ctx <= 0:
            raise ValueError("num_ctx must be positive")
        if num_predict <= 0:
            raise ValueError("num_predict must be positive")
        self.base_url = url.rstrip("/")
        self.model, self.family = model, family
        self.name = f"ollama:{model}"
        self.keep_alive, self.timeout = keep_alive, timeout
        self.num_ctx, self.truncation_guard = num_ctx, truncation_guard
        # The reply's token cap. NUM_PREDICT (128) suits the short-answer tracks; Track B (IFEval) passes its own,
        # far larger cap (trackb.NUM_PREDICT), and every guard below reserves whatever this instance sends.
        self.num_predict = num_predict
        self.timeout_cap: float | None = None
        self.last_prompt_eval_count: int | None = None
        self.retries = 0   # /api/chat requests retried once after an HTTP 5xx (T-4984)
        # Extra TOP-LEVEL /api/chat body fields (Track B sends {"think": false}); empty for every other caller.
        self.request_extra: dict = {}

    def unload(self) -> None:
        """Evict the model now: POST /api/generate {model, keep_alive: 0}. The driver calls this on every lock
        session release, so nothing stays resident outside a session."""
        body = json.dumps({"model": self.model, "keep_alive": 0}).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/api/generate", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=UNLOAD_TIMEOUT_S) as resp:  # noqa: S310 (operator-set local URL)
            resp.read()

    def options(self) -> dict:
        return {"temperature": 0, "num_ctx": self.num_ctx, "num_predict": self.num_predict}

    def request_timeout(self) -> float:
        return self.timeout if self.timeout_cap is None else min(self.timeout, self.timeout_cap)

    def _chat(self, system: str | None, user: str) -> str:
        """``system=None`` sends the user message alone, with no system message at all (Track B's IFEval prompts)."""
        self.last_prompt_eval_count = None
        sys_text = system or ""
        if self.truncation_guard:
            est = math.ceil((len(sys_text) + len(user)) / CHARS_PER_TOKEN_CONSERVATIVE)
            if est + self.num_predict > self.num_ctx:
                raise TruncatedPrompt(f"prompt is ~{est} tokens (chars / {CHARS_PER_TOKEN_CONSERVATIVE}) + "
                                      f"num_predict {self.num_predict}, over num_ctx={self.num_ctx}")
        messages = [{"role": "user", "content": user}]
        if system is not None:
            messages.insert(0, {"role": "system", "content": system})
        body = json.dumps({
            "model": self.model, "stream": False, "keep_alive": self.keep_alive,
            "options": self.options(),
            "messages": messages,
            **self.request_extra,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/api/chat", data=body, headers={"Content-Type": "application/json"}
        )
        t0 = _now()
        try:
            data = self._post_chat(req, self.request_timeout())
        except urllib.error.HTTPError as e:
            # T-4984: a transient server error (one 500) aborted a 70-minute real run. Retry the IDENTICAL request
            # once on a 5xx; temperature is 0, so a retry cannot bias the measurement. A 4xx, a second 5xx and any
            # timeout (not an HTTPError) still raise. Under a session budget (timeout_cap) the retry gets only the
            # REMAINING time, cap minus the first attempt's elapsed time, and is skipped (the original error is
            # raised) when that is under RETRY_FLOOR_S. With no cap, the retry gets the full timeout.
            if e.code < 500:
                raise
            timeout = self.timeout
            if self.timeout_cap is not None:
                left = self.timeout_cap - (_now() - t0)
                if left < RETRY_FLOOR_S:
                    raise
                timeout = min(self.timeout, left)
            e.close()
            self.retries += 1
            data = self._post_chat(req, timeout)
        n = data.get("prompt_eval_count")
        self.last_prompt_eval_count = n if isinstance(n, int) and not isinstance(n, bool) else None
        if self.truncation_guard:
            self._check_not_truncated(data, len(sys_text.split()) + len(user.split()))
        return str(data["message"]["content"]).strip()

    def _post_chat(self, req: urllib.request.Request, timeout: float) -> dict:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-set local URL)
            return json.loads(resp.read())

    def _check_not_truncated(self, data: dict, words: int) -> None:
        n = data.get("prompt_eval_count")
        if isinstance(n, bool) or not isinstance(n, int):
            raise TruncatedPrompt("response has no prompt_eval_count: cannot verify the prompt was not truncated")
        if n >= self.num_ctx - self.num_predict:
            raise TruncatedPrompt(f"prompt_eval_count={n} reached num_ctx={self.num_ctx} - num_predict {self.num_predict}: "
                                  "the prompt may be cut")
        if n < words * TRUNCATION_MARGIN:
            raise TruncatedPrompt(f"prompt_eval_count={n} is below {TRUNCATION_MARGIN} x the prompt's {words} words: "
                                  "Ollama cut the prompt")

    def generate(self, question: str, evidence: Sequence[str]) -> str:
        self.calls += 1
        ctx = "\n".join(f"- {e}" for e in evidence) or "(no evidence)"
        return self._chat(
            f"Answer using ONLY the evidence. If it does not contain the answer, reply exactly {ABSTAIN}. "
            "Reply with the bare answer only.",
            f"Evidence:\n{ctx}\n\nQuestion: {question}",
        )

    def judge(self, question: str, answer: str, gold: str) -> bool:
        if normalize(answer) == normalize(gold):
            return True
        self.calls += 1
        verdict = self._chat(
            "Reply yes or no only.",
            f"Question: {question}\nGold answer: {gold}\nCandidate answer: {answer}\n"
            "Does the candidate state the same answer as the gold?",
        )
        return verdict.lower().startswith("yes")
