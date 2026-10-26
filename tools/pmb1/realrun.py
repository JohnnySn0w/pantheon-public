"""A batched, lock-aware REAL-model run driver for PMB-1 (T-2518 step 2g).

Runs the tier-1 gate with real local backbones on the shared A2000 without starving the SuperNote
ingest (``/run/lock/supernote-ingest.lock``: the ingest waits <= 300 s, then skips to its 05:17Z cron).

* **Unit** = (fixture version, size, seed, backbone, substrate). One record per unit, written to
  ``<out>/seed-N/<family>/<substrate>.json`` (one backbone per family per run, as the gate requires). A unit
  whose complete, valid record from THAT model exists is SKIPPED, so a killed run resumes.
* **Lock sessions** are a pluggable context manager (``LockSession``), one backbone spec (one model) each.
  The budget is ``min(max_lock_s, seconds to the window)``, taken AFTER the lock is acquired; a session is
  not started unless it fits the cold load plus one call. Every real call's HTTP timeout is capped at what is
  left of the budget (floor ``MIN_CALL_TIMEOUT_S``) and each unload at ``UNLOAD_TIMEOUT_S``, so the lock is
  held at most ``max_lock_s + MIN_CALL_TIMEOUT_S + UNLOAD_TIMEOUT_S`` per model. Sessions are released for
  ``gap_s`` between them and never STARTED inside 04:55-05:50Z.
* **Probe batching by journal replay.** The tracks are deterministic given the backbone's replies and mutate
  a substrate as they go (supersede, forget), so a probe slice cannot be cut out of one. Instead every
  backbone call is journalled (``<out>/.journal``). When the session budget cannot fit another model call,
  the unit raises ``BatchBoundary``, the session is released, and the next session RE-RUNS the unit: journalled
  calls are replayed for free, new ones go to the model. The final run is a full, ordinary ``run_one``, so its
  record is the unbatched record by construction (only the volatile ``created`` / ``*_seconds`` fields differ).
  A replayed call whose arguments differ from the journal raises ``JournalMismatch``: never a silent splice.
  Each journal starts with a header {spec, model, num_ctx, fixture sha}; a resume under any other value, or a
  journal without a header, raises ``JournalMismatch`` too. num_ctx is the max over the invocation's
  ``--seeds``, so resume with the same ``--seeds``.
  A live call the session budget clamped (its timeout below ``call_timeout``) that times out is a batch
  boundary too: nothing is journalled and the unit resumes next session. A timeout at the full ``call_timeout``,
  or on a session's first call (the next session's budget would be no larger), stays an error.
* **Real embedder (step 2g-2): precompute, then replay.** With ``RunConfig.embedder`` (``ollama:<model>@<digest>``)
  the RAG substrate's texts (enumerated by ``embedphase.enumerate_texts``: backbone-independent, proven in
  ``test_pmb1_embedder``) are embedded FIRST, in lock sessions of their own (same ``LockSession``, budget, window
  and gap rules; one model per session; explicit unload on release), into ``<out>/.embed-cache/<name>/``. The
  embedder never loads inside a backbone session. The backbone units then run the RAG substrate over a
  ``CachedEmbedder`` in replay-only mode: a miss raises ``EmbeddingCacheMiss``, never a call to the model. A cache
  made under another embedder name or dim raises on load (``EmbeddingCacheMismatch``); each embed session checks
  the loaded weights' digest (``/api/ps``) after its first batch, before caching it, and a RAG unit's journal
  header carries its ``embedding_model``, so a resume never splices two embedders into one record.
  T-2518 semleg: "the RAG substrate" above reads "every substrate in ``runner.EMBEDDING_SUBSTRATES``", which now
  includes the episodic tier (its semantic leg): its texts are enumerated per (seed, substrate) and replayed
  from the same cache, fail closed on a miss.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import socket
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ContextManager, Sequence

from .backbone import ABSTAIN, CHARS_PER_TOKEN_CONSERVATIVE, NUM_PREDICT, Backbone, normalize
from .embedder import (
    KNOWN_DIMS, CachedEmbedder, Embedder, EmbedderError, EmbeddingCache, HashingEmbedder, is_lexical_stub, missing,
    parse_embedder_spec,
)
from .embedphase import DEFAULT_BATCH, batches, enumerate_texts, precompute
from .fixtures_v3 import generate_by_version
from .records import validate_record
from .runner import EMBEDDING_SUBSTRATES, SUBSTRATES, GateRunRefused, make_substrate, rank_stability, run_one
from .tracks_v4 import is_pattern_stub_extractor

MAX_LOCK_S = 240.0
GAP_S = 45.0
EST_CALL_S = 10.0   # per-call estimate for a model with no measurement below, until a call has been measured
# Measured on the A2000 (2026-10-10): warm (short, long-prompt) seconds per call, and the cold load charged once per session.
MODEL_EST = {"qwen2.5:14b": (0.5, 5.0), "llama3.1:8b": (0.3, 2.0)}
# COLD_S is the page-cache-WARM load (planning): the measured warm max on gpu-ml (qwen, 11-35 s). A truly cold
# disk load (blob not in the page cache) takes 5-10 min; the lock session pre-warms the page cache so it never
# happens inside a budget, and every call, the first included, keeps the F3 clamp.
COLD_S = 35.0
LONG_WORDS = 1000   # a prompt of at least this many words is a "long" call
KEEP_ALIVE = "2m"   # warm inside a session; the driver unloads explicitly on release
WINDOW = (dt.time(4, 55), dt.time(5, 50))   # UTC: never START a session inside it
DRIVER = "pmb1-realrun/1"
NUM_CTX_HEADROOM = 512
MIN_CALL_TIMEOUT_S = 5.0   # a real call's HTTP timeout is min(call_timeout, max(session left, this))
_SYSTEM_WORDS = 40  # the generate() system prompt, with room to spare
# The embed precompute (captured 2026-10-13, nomic-embed-text on gpu-ml): the disk-cold load took ~27 s (~1 s
# page-cache warm) and a warm 4-item batch ~2 s, so 0.5 s per item is the planning figure until measured.
EMBED_COLD_S = 27.0
EMBED_ITEM_S = 0.5


class InWindow(RuntimeError):
    """A session would start inside the ingest window, or too close to it to fit one call. The run is
    resumable; rerun after ``until``."""

    def __init__(self, until: dt.datetime, why: str = "inside 04:55-05:50Z") -> None:
        super().__init__(f"refusing to start a lock session {why}; rerun after {until:%H:%M}Z")
        self.until = until


class BudgetTooSmall(ValueError):
    """``max_lock_s`` cannot fit a cold load plus one call: no session could ever start."""


class BatchBoundary(Exception):
    """Internal: the session budget cannot fit another model call. Not an error."""


class UnloadFailed(RuntimeError):
    """A model could not be evicted on session release: it may be resident outside a session."""


def _is_timeout(e: BaseException) -> bool:
    return isinstance(e, (TimeoutError, socket.timeout)) or (
        isinstance(e, urllib.error.URLError) and isinstance(e.reason, (TimeoutError, socket.timeout)))


class JournalMismatch(RuntimeError):
    """A replayed call differs from the journal: the unit is not deterministic, so batching is invalid."""


class Clock:
    def utc(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    def mono(self) -> float:
        return time.monotonic()

    def sleep(self, s: float) -> None:
        time.sleep(s)


class LockSession:
    """A context manager holding the GPU lock. The real one (archivist's) runs ``ssh archivist@gpu-ml flock
    /run/lock/supernote-ingest.lock cat`` with stdin held open plus an ``-L`` forward to 127.0.0.1:11434, and
    releases by closing stdin. The driver only relies on enter = held, exit = released."""

    def __enter__(self) -> "LockSession":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


# ---------------------------------------------------------------------------------------------------- windows

def in_window(now: dt.datetime) -> bool:
    return WINDOW[0] <= now.astimezone(dt.timezone.utc).time() < WINDOW[1]


def window_end(now: dt.datetime) -> dt.datetime:
    n = now.astimezone(dt.timezone.utc)
    return n.replace(hour=WINDOW[1].hour, minute=WINDOW[1].minute, second=0, microsecond=0)


def seconds_to_window(now: dt.datetime) -> float:
    """Seconds until the next window start (always > 0 outside the window)."""
    n = now.astimezone(dt.timezone.utc)
    start = n.replace(hour=WINDOW[0].hour, minute=WINDOW[0].minute, second=0, microsecond=0)
    if start <= n:
        start += dt.timedelta(days=1)
    return (start - n).total_seconds()


# ---------------------------------------------------------------------------------------------------- num_ctx

def required_num_ctx(fx: object) -> int:
    """num_ctx for the largest prompt this fixture can send: the full-context ceiling (every text the
    substrate could hold) plus the longest question, at the guard's conservative chars per token, plus the
    reply's NUM_PREDICT, plus headroom, rounded up to a multiple of 1024 (floor 2048). The truncation guard
    in OllamaBackbone uses the same ratio, so a prompt this sized for passes its pre-check.
    v4 (T-2518 S6a): the slot pairs' second episodes and the derived sources are texts a substrate can hold too,
    and the slot / derived queries are questions; a v2/v3 fixture has none, so its num_ctx is unchanged."""
    texts = [t for _, _, t in getattr(fx, "episodes", [])]
    texts += [t for _, _, t in getattr(fx, "restricted", [])]
    texts += [s.new_text for s in getattr(fx, "supersedes", [])]
    pairs = list(getattr(fx, "changes", [])) + list(getattr(fx, "conflicts", [])) + list(getattr(fx, "controls", []))
    texts += [p.other_text for p in pairs] + [d.text for d in getattr(fx, "derived", [])]
    questions = [p.query for p in getattr(fx, "probes", [])] + [p.query for p in getattr(fx, "forgets", [])]
    questions += [p.query for p in getattr(fx, "gov_probes", [])] + [s.query for s in getattr(fx, "supersedes", [])]
    questions += [p.query(m) for p in pairs for m in _v4_modes(fx)] + [d.query for d in getattr(fx, "derived", [])]
    chars = sum(len(t) + 3 for t in texts) + max((len(q) for q in questions), default=0) + 12 * _SYSTEM_WORDS
    est = math.ceil(chars / CHARS_PER_TOKEN_CONSERVATIVE) + NUM_PREDICT + NUM_CTX_HEADROOM
    return max(2048, math.ceil(est / 1024) * 1024)


def _v4_modes(fx: object) -> tuple[str, ...]:
    if getattr(fx, "version", 1) < 4:
        return ()
    from .fixtures_v4 import MODES
    return tuple(MODES)


# ---------------------------------------------------------------------------------------------------- journal

def model_of(name: str) -> str:
    return name[len("ollama:"):] if name.startswith("ollama:") else name


def backbone_name(spec: str) -> str:
    """The ``name`` the backbone a spec builds carries: ``ollama:<model>:<family>`` -> ``ollama:<model>``."""
    return "ollama:" + ":".join(spec.split(":")[1:-1]) if spec.startswith("ollama:") else spec


def is_long(question: str, evidence: Sequence[str]) -> bool:
    return len(question.split()) + sum(len(e.split()) for e in evidence) >= LONG_WORDS


@dataclass
class Estimator:
    """Per-call latency by (model, short|long): the measured table first, replaced by the worst latency actually
    observed (shared across units so batches are sized from measurement)."""
    default_s: float = EST_CALL_S
    worst: dict[tuple[str, bool], float] = field(default_factory=dict)

    def get(self, name: str, long: bool) -> float:
        w = self.worst.get((name, long))
        if w is not None:
            return w
        t = MODEL_EST.get(model_of(name))
        return t[1 if long else 0] if t else self.default_s

    def observe(self, name: str, long: bool, s: float) -> None:
        self.worst[(name, long)] = max(self.worst.get((name, long), 0.0), s)


def cold_load_s(name: str) -> float:
    return COLD_S if name.startswith("ollama:") else 0.0


def session_need_s(spec: str, est: Estimator) -> float:
    """What a session must fit to start: the cold load plus the first call (its long estimate: which call
    comes first is not known before replay)."""
    return cold_load_s(spec) + est.get(backbone_name(spec), True)


@dataclass
class Session:
    """``left`` is the budget minus the elapsed time. The cold load is not charged separately: it is inside the
    elapsed time of the session's first model call (which is why that call is never observed as a latency)."""
    clock: Clock
    start: float
    budget_s: float
    model_calls: int = 0

    def left(self) -> float:
        return self.budget_s - (self.clock.mono() - self.start)


class JournalBackbone(Backbone):
    """Wraps a real backbone: replays journalled calls, journals new ones, and raises ``BatchBoundary`` before
    a model call the session budget cannot fit. Its ``calls`` advances by what the inner backbone's own counter
    advanced on that call (journalled as ``c``), replayed or not, so the record's ``backbone_calls`` equals an
    unbatched run's. Each entry also keeps the raw ``prompt_eval_count`` (``pec``) for recalibration."""

    def __init__(self, inner: Backbone, path: Path, est: Estimator, clock: Clock, header: dict) -> None:
        super().__init__()
        self.inner, self.path, self.est, self.clock = inner, path, est, clock
        self.name, self.family, self.knows_lexicon = inner.name, inner.family, inner.knows_lexicon
        self.header = header
        self.entries = self._load()
        self.pos = 0
        self.session: Session | None = None
        self.touched = False   # did this backbone attempt a real model call (so its model may be resident)?

    def _load(self) -> list[dict]:
        """The journal's entries after its header. A trailing partial LAST line (a kill mid-write) is dropped and
        cut from the file; any other unparseable line raises. A header that differs from this run's, or none
        (a legacy journal), raises: replaying another model's replies would splice two models into one record."""
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        lines = raw.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        parsed: list[dict] = []
        for i, line in enumerate(lines):
            try:
                parsed.append(json.loads(line))
            except ValueError:
                if i != len(lines) - 1:
                    raise JournalMismatch(f"{self.path}: line {i + 1} is corrupt (not the last line)") from None
                with self.path.open("r+b") as f:
                    f.truncate(sum(len(x) + 1 for x in lines[:i]))
                return self._check_header(parsed)
        if parsed and not raw.endswith(b"\n"):
            with self.path.open("ab") as f:   # a complete but unterminated last line: terminate it
                f.write(b"\n")
        return self._check_header(parsed)

    def _check_header(self, parsed: list) -> list[dict]:
        if not parsed:
            return []
        h = parsed[0].get("header") if isinstance(parsed[0], dict) else None
        if not isinstance(h, dict):
            raise JournalMismatch(f"{self.path}: legacy journal without a header (spec, model, num_ctx, fixture sha); "
                                  "delete it to rerun the unit")
        # both ways: a key only one side has (a RAG unit's embedding_model) is a mismatch too
        for k in sorted(set(h) - set(self.header)):
            raise JournalMismatch(f"{self.path}: journal has {k}={h[k]!r}, this run has none")
        for k, v in self.header.items():
            if h.get(k) != v:
                hint = "; num_ctx is the max over this invocation's --seeds; resume with the same --seeds" if k == "num_ctx" else ""
                raise JournalMismatch(f"{self.path}: journal has {k}={h.get(k)!r}, this run has {k}={v!r}{hint}")
        return parsed[1:]

    def _append(self, obj: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def begin_replay(self, session: Session) -> None:
        self.pos, self.calls, self.session = 0, 0, session

    def _step(self, kind: str, args: list, model_call: bool, fn: Callable[[], object], long: bool = False) -> object:
        if self.pos < len(self.entries):
            e = self.entries[self.pos]
            if e["k"] != kind or e["a"] != args:
                raise JournalMismatch(f"call {self.pos}: journal has {e['k']} {e['a']!r}, run asked {kind} {args!r}")
            self.pos += 1
            self.calls += e["c"]
            return e["r"]
        s = self.session
        assert s is not None
        if model_call and s.model_calls > 0 and s.left() < self.est.get(self.name, long):
            raise BatchBoundary()
        first = s.model_calls == 0
        clamped = False
        if model_call:
            self.touched = True
            if hasattr(self.inner, "timeout_cap"):   # the hard lock-hold cap: never wait past the budget
                self.inner.timeout_cap = max(s.left(), MIN_CALL_TIMEOUT_S)
                clamped = self.inner.timeout_cap < self.inner.timeout  # type: ignore[attr-defined]
        if hasattr(self.inner, "last_prompt_eval_count"):
            self.inner.last_prompt_eval_count = None
        c0 = self.inner.calls
        t0 = self.clock.mono()
        try:
            r = fn()
        except Exception as e:
            # the budget, not the model, cut this call short: end the batch (nothing journalled, the unit resumes
            # next session). A session's first call has the whole budget, so the next session could not do better.
            if clamped and not first and _is_timeout(e):
                raise BatchBoundary() from e
            raise
        lat = self.clock.mono() - t0
        if model_call:
            s.model_calls += 1
            if not first:   # the first call of a session includes the model load
                self.est.observe(self.name, long, lat)
        e = {"k": kind, "a": args, "r": r, "s": round(lat, 4), "m": model_call, "c": self.inner.calls - c0,
             "pec": getattr(self.inner, "last_prompt_eval_count", None)}
        if not self.path.exists() or self.path.stat().st_size == 0:
            self._append({"header": self.header})
        self._append(e)
        self.entries.append(e)
        self.pos += 1
        self.calls += e["c"]
        return r

    def generate(self, question: str, evidence: Sequence[str]) -> str:
        ev = list(evidence)
        return str(self._step("g", [question, ev], True, lambda: self.inner.generate(question, ev), is_long(question, ev)))

    def judge(self, question: str, answer: str, gold: str) -> bool:
        # an exact match costs no model call (OllamaBackbone short-circuits it), so it never ends a batch; whether
        # it counts as a call is whatever the inner backbone's counter says (journalled as "c")
        model = normalize(answer) != normalize(gold)
        return bool(self._step("j", [question, answer, gold], model, lambda: self.inner.judge(question, answer, gold)))


# ---------------------------------------------------------------------------------------------------- plan

@dataclass(frozen=True)
class Unit:
    seed: int
    family: str
    spec: str          # backbone spec, ollama:<model>:<family>
    substrate: str

    def rel(self) -> Path:
        safe = lambda s: "".join(c if c.isalnum() or c in "-_." else "_" for c in s)  # noqa: E731
        return Path(f"seed-{self.seed}") / safe(self.family) / f"{safe(self.substrate)}.json"

    def label(self) -> str:
        return f"seed-{self.seed}/{self.family}/{self.substrate}"


def backbone_family(spec: str) -> str:
    if spec in ("stub", "stub-lenient"):
        return f"stub-{'strict' if spec == 'stub' else 'lenient'}"
    parts = spec.split(":")
    if parts[0] != "ollama" or len(parts) < 3:
        raise ValueError(f"unknown backbone {spec!r}; ollama:<model>:<family>")
    return parts[-1]


def plan_units(seeds: Sequence[int], backbones: Sequence[str], substrates: Sequence[str]) -> list[Unit]:
    """Backbone-major (the fewest model swaps on a card that fits one at a time), then seed, then substrate."""
    unknown = [s for s in substrates if s not in SUBSTRATES]
    if unknown:
        raise ValueError(f"unknown substrate(s) {unknown}; one of {sorted(SUBSTRATES)}")
    return [Unit(sd, backbone_family(b), b, s) for b in backbones for sd in seeds for s in substrates]


def unit_complete(path: Path, fx: object, unit: Unit, embedder: str | None = None,
                  extractor_is_pattern_stub: bool = False) -> bool:
    """A complete, validated record for THIS fixture, model, substrate and embedder (a record from another model
    of the same family, or a RAG record from another embedder, is not complete: it is rerun). On v4 the record's
    R6 declaration (``extractor_is_pattern_stub``) must be this run's too, so a resume never mixes declarations."""
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    name, model = _sub_ident(unit.substrate, fx, embedder)
    return (not validate_record(rec) and rec["fixture"]["sha256"] == fx.sha256  # type: ignore[attr-defined]
            and rec["backbone"]["family"] == unit.family and rec["backbone"]["name"] == backbone_name(unit.spec)
            and rec["substrate"]["name"] == name and rec.get("embedding_model") == model
            and (getattr(fx, "version", 1) < 4 or rec.get("extractor_is_pattern_stub") is bool(extractor_is_pattern_stub)))


class NameOnlyEmbedder(Embedder):
    """Carries an embedder's name for probes (substrate name / embedding_model / the stub check). Never embeds."""

    def __init__(self, name: str) -> None:
        self.name = name

    def embed(self, text: str) -> list[float]:
        raise EmbedderError(f"NameOnlyEmbedder({self.name}) is a probe and never embeds")


_IDENTS: dict[tuple[str, str | None], tuple[str, str | None]] = {}


def _sub_ident(substrate: str, fx: object, embedder: str | None = None) -> tuple[str, str | None]:
    """(substrate name, embedding_model) as a record made under ``embedder`` (None: the default) carries them."""
    key = (substrate, embedder if substrate in EMBEDDING_SUBSTRATES else None)
    if key not in _IDENTS:
        s = make_substrate(substrate, fx, NameOnlyEmbedder(embedder) if embedder else None)  # type: ignore[arg-type]
        try:
            _IDENTS[key] = (s.name, s.embedding_model)
        finally:
            s.close()
    return _IDENTS[key]


def _sub_name(substrate: str, fx: object) -> str:
    return _sub_ident(substrate, fx)[0]


class _CountingStub(Backbone):
    """Dry-run only: counts the calls a unit would make. Local, deterministic, no model."""

    def __init__(self) -> None:
        super().__init__()
        self.name, self.family, self.knows_lexicon = "dry-run", "dry-run", True
        self.seq: list[tuple[str, bool]] = []   # ("g", long) | ("j", False), in call order

    def generate(self, question: str, evidence: Sequence[str]) -> str:
        self.calls += 1
        self.seq.append(("g", is_long(question, evidence)))
        return ABSTAIN

    def judge(self, question: str, answer: str, gold: str) -> bool:
        self.calls += 1
        self.seq.append(("j", False))
        return False


def estimate_calls(fx: object, substrate: str, embedder_stub: bool, extractor_stub: bool = False) -> list[tuple[str, bool]]:
    """The (kind, long) sequence of calls a unit makes on THIS substrate, long by the driver's own ``is_long``
    rule. The real judge calls the model only on a mismatch, so counting every judge is an UPPER bound."""
    b = _CountingStub()
    run_one(lambda: SUBSTRATES[substrate](fx), b, fx, gate=True, embedder_is_lexical_stub=embedder_stub,  # type: ignore[arg-type]
            extractor_is_pattern_stub=extractor_stub)
    return b.seq


def simulate_sessions(unit_costs: Sequence[tuple[str, Sequence[float]]], cold_s: float, cap_s: float) -> tuple[int, float]:
    """Pack (backbone spec, per-call seconds) units into sessions with the driver's arithmetic: one spec per
    session, ``left = cap - elapsed`` where the session's first call carries the cold load, a later call must
    fit what is left (the first call of a session always runs). Returns (sessions, seconds of lock held)."""
    sessions, held, left, cur = 0, 0.0, 0.0, None
    for spec, costs in unit_costs:
        for c in costs:
            if spec != cur or left < c:
                sessions, cur, left = sessions + 1, spec, cap_s
                c += cold_s
            left -= c
            held += c
    return sessions, held


# ---------------------------------------------------------------------------------------------------- driver

@dataclass
class RunConfig:
    out: Path
    fixture_size: str
    fixture_version: int
    seeds: Sequence[int]
    backbones: Sequence[str]
    substrates: Sequence[str]
    embedder_is_lexical_stub: bool
    max_lock_s: float = MAX_LOCK_S
    gap_s: float = GAP_S
    est_call_s: float = EST_CALL_S
    embedder: str | None = None       # ollama:<model>@<digest>; None = the hashing stub (unchanged)
    embed_batch: int = DEFAULT_BATCH
    extractor_is_pattern_stub: bool = False   # v4 (R6): declare a pattern-stub relation extractor (runner.run's flag)


def embedder_name(spec: str) -> str:
    model, digest = parse_embedder_spec(spec)
    return f"ollama:{model}@{digest}"


def embed_cache_path(cfg: RunConfig) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in embedder_name(cfg.embedder or ""))
    return cfg.out / ".embed-cache" / safe


def embed_dim(spec: str) -> int | None:
    return KNOWN_DIMS.get(parse_embedder_spec(spec)[0].split(":", 1)[0])


def _preconditions(cfg: RunConfig, fixtures: dict[int, object]) -> None:
    if cfg.embedder is not None:
        if cfg.embedder_is_lexical_stub:
            raise ValueError("--embedder and --lexical-stub-embedder are mutually exclusive")
        if is_lexical_stub(embedder_name(cfg.embedder)):   # parses the spec too (ValueError on a bad one)
            raise ValueError(f"--embedder {cfg.embedder} names the lexical stub")
        if cfg.embed_batch < 1:
            raise ValueError("embed_batch must be >= 1")
    fams = [backbone_family(b) for b in cfg.backbones]
    if len(set(fams)) < 2:
        raise GateRunRefused(f"a gate run needs backbones of at least 2 different families (rule 6.1.4); got {sorted(set(fams))}")
    dup = sorted({f for f in fams if fams.count(f) > 1})
    if dup:
        raise GateRunRefused(f"more than one backbone of family {dup}: records are one per (substrate, family, seed) "
                             "(the gate refuses duplicates) and would overwrite each other")
    if cfg.fixture_version < 2:
        raise ValueError("realrun gates on a v2+ fixture")
    check_pattern = cfg.fixture_version >= 4 and not cfg.extractor_is_pattern_stub   # runner.run's R6 refusal
    if not cfg.embedder_is_lexical_stub or check_pattern:
        fx = next(iter(fixtures.values()))
        probe_emb = NameOnlyEmbedder(embedder_name(cfg.embedder)) if cfg.embedder else None
        for s in cfg.substrates:
            probe = make_substrate(s, fx, probe_emb)  # type: ignore[arg-type]
            try:
                if not cfg.embedder_is_lexical_stub and is_lexical_stub(probe.embedding_model):
                    raise GateRunRefused(f"the gate run refuses the lexical stub embedder ({probe.embedding_model}) "
                                         "unless declared with --lexical-stub-embedder")
                ext = probe.describe().get("extractor")
                if check_pattern and is_pattern_stub_extractor(ext):
                    raise GateRunRefused(f"a v4 gate run refuses a pattern-stub extractor ({ext}) unless declared "
                                         "with --pattern-stub-extractor (R6)")
            finally:
                probe.close()


def _atomic_write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _session_budget(cfg: RunConfig, now: dt.datetime, need: float) -> float:
    """The budget a session started ``now`` gets, or a refusal when it cannot fit ``need``."""
    budget = min(cfg.max_lock_s, seconds_to_window(now))
    if budget < need:
        if cfg.max_lock_s < need:
            raise BudgetTooSmall(f"max_lock_s={cfg.max_lock_s:g} cannot fit a cold load plus one call ({need:.1f}s)")
        raise InWindow(window_end(now), f"that cannot fit one call ({need:.1f}s) before the 04:55Z window")
    return budget


def _release_models(used: dict[str, Backbone], log: Callable[[str], None]) -> None:
    """Evict every model this session used, while the lock is still held. Never stops at the first failure."""
    errs = []
    for name, b in used.items():
        try:
            b.unload()
            log(f"  unloaded {name}")
        except Exception as e:  # noqa: BLE001 (every model gets its attempt)
            errs.append(f"{name}: {e}")
    if errs:
        raise UnloadFailed("; ".join(errs))


def _before_session(cfg: RunConfig, clock: Clock, sessions: int, need: float) -> None:
    """The window check, the gap after a previous session, and the budget refusal, all before asking for the lock."""
    now = clock.utc()
    if in_window(now):
        raise InWindow(window_end(now))
    if sessions:
        clock.sleep(cfg.gap_s)
        now = clock.utc()
        if in_window(now):
            raise InWindow(window_end(now))
    _session_budget(cfg, now, need)


def embed_texts_for(cfg: RunConfig, fixtures: dict[int, object], units: Sequence[Unit]) -> set[tuple[str, str]]:
    """Every (kind, text) the embedding substrates of ``units`` will embed: one enumeration per (seed, embedding
    substrate) among them (the RAG baseline and the episodic tier's semantic leg embed different texts)."""
    out: set[tuple[str, str]] = set()
    for sd, sub in sorted({(u.seed, u.substrate) for u in units if u.substrate in EMBEDDING_SUBSTRATES}):
        out |= enumerate_texts(fixtures[sd], substrate=sub)
    return out


def _embed_phase(
    cfg: RunConfig, texts: set[tuple[str, str]], embedder_factory: Callable[[str], Embedder],
    lock_factory: Callable[[], ContextManager], clock: Clock, log: Callable[[str], None], sessions: int,
) -> tuple[int, dict]:
    """Fill the embed cache for ``texts`` in lock sessions of its own (one model, the embedder). Returns the
    session count so far and a summary. Runs before any backbone session, so the two never co-reside."""
    assert cfg.embedder is not None
    name = embedder_name(cfg.embedder)
    cache = EmbeddingCache(embed_cache_path(cfg), name, embed_dim(cfg.embedder))
    todo = missing(cache, texts)
    summary = {"texts": len(texts), "cached_before": len(texts) - len(todo), "sessions": 0, "requests": 0}
    item_s = EMBED_ITEM_S
    while todo:
        need = EMBED_COLD_S + item_s * min(cfg.embed_batch, len(todo))
        _before_session(cfg, clock, sessions, need)
        with lock_factory():
            now = clock.utc()   # flock may have blocked
            if in_window(now):
                raise InWindow(window_end(now))
            budget = _session_budget(cfg, now, need)
            sessions += 1
            summary["sessions"] += 1
            inner = embedder_factory(cfg.embedder)
            if inner.name != name:
                raise ValueError(f"embedder for {cfg.embedder} is named {inner.name!r}, expected {name!r}")
            sess = Session(clock, clock.mono(), budget)
            used: dict[str, object] = {}
            prev: list[tuple[float, int]] = []   # (start, size) of each batch this session

            def before(i: int, n: int) -> bool:
                nonlocal item_s
                now = clock.mono()
                if len(prev) >= 2:   # the session's first batch carries the load: never a latency sample
                    t, k = prev[-1]
                    item_s = max(item_s, (now - t) / k)
                if i > 0 and sess.left() < item_s * n:
                    return False
                if hasattr(inner, "timeout_cap"):   # the hard lock-hold cap, as for backbone calls
                    inner.timeout_cap = max(sess.left(), MIN_CALL_TIMEOUT_S)  # type: ignore[attr-defined]
                used[name] = inner
                prev.append((now, n))
                return True

            log(f"session {sessions}: embed {name}, {len(todo)} texts pending, budget {budget:.0f}s")
            try:
                r = precompute(inner, cache, todo, batch_size=cfg.embed_batch, before_batch=before,
                               after_embed=lambda i: _verify_digest(inner, i, log))
            except BaseException as e:
                try:
                    _release_models(used, log)  # type: ignore[arg-type]
                except UnloadFailed as ue:   # never mask the original error
                    log(f"  UNLOAD FAILED while handling {type(e).__name__}: {ue}")
                    e.add_note(f"also: {type(ue).__name__}: {ue}")
                raise
            _release_models(used, log)  # type: ignore[arg-type]
            summary["requests"] += r["requests"]
            log(f"  embedded {r['embedded']}, {r['remaining']} remaining")
        todo = missing(cache, texts)
    return sessions, summary


def _verify_digest(inner: Embedder, i: int, log: Callable[[str], None]) -> None:
    """After a session's FIRST embedded batch (the model is now loaded) and before it is cached: the loaded
    weights must be the ones the embedder's name claims (``OllamaEmbedder.verify_digest``; fail closed)."""
    if i == 0 and hasattr(inner, "verify_digest"):
        log(f"  loaded digest verified: {inner.verify_digest()}")  # type: ignore[attr-defined]


def realrun(
    cfg: RunConfig, backbone_factory: Callable[[str, int], Backbone], lock_factory: Callable[[], ContextManager],
    *, clock: Clock | None = None, log: Callable[[str], None] = lambda s: None,
    embedder_factory: Callable[[str], Embedder] | None = None,
) -> dict:
    """Run every pending unit. ``backbone_factory(spec, num_ctx)`` builds the real backbone (keep_alive 2m, that
    num_ctx); ``lock_factory()`` returns one lock session; ``embedder_factory(spec)`` builds the real embedder
    when ``cfg.embedder`` is set. Returns a summary."""
    clock = clock or Clock()
    fixtures = {sd: generate_by_version(sd, cfg.fixture_size, cfg.fixture_version) for sd in cfg.seeds}
    _preconditions(cfg, fixtures)
    if cfg.embedder is not None and embedder_factory is None:
        raise ValueError("cfg.embedder is set but no embedder_factory was given")
    num_ctx = num_ctx_by_spec(cfg.backbones, fixtures)
    est = Estimator(default_s=cfg.est_call_s)
    units = plan_units(cfg.seeds, cfg.backbones, cfg.substrates)
    pending = [u for u in units if not unit_complete(cfg.out / u.rel(), fixtures[u.seed], u, cfg.embedder and embedder_name(cfg.embedder),
                                                     cfg.extractor_is_pattern_stub)]
    skipped = len(units) - len(pending)
    timing: dict[Unit, dict] = {}
    sessions = 0
    embed_summary: dict | None = None
    replay: CachedEmbedder | None = None
    if cfg.embedder is not None and any(u.substrate in EMBEDDING_SUBSTRATES for u in pending):
        assert embedder_factory is not None
        sessions, embed_summary = _embed_phase(cfg, embed_texts_for(cfg, fixtures, pending), embedder_factory,
                                               lock_factory, clock, log, sessions)
        replay = CachedEmbedder(embedder_name(cfg.embedder), embed_cache_path(cfg), embed_dim(cfg.embedder))
    while pending:
        spec = pending[0].spec   # one model per session: 5.2 + 9.2 GB does not fit the card
        need = session_need_s(spec, est)
        _before_session(cfg, clock, sessions, need)   # refuse before even asking for the lock
        with lock_factory():
            now = clock.utc()   # flock may have blocked: the window may be closer now
            if in_window(now):
                raise InWindow(window_end(now))
            budget = _session_budget(cfg, now, need)
            sessions += 1
            used: dict[str, Backbone] = {}
            try:
                sess = Session(clock, clock.mono(), budget)
                log(f"session {sessions}: {spec}, {len(pending)} units pending, budget {budget:.0f}s")
                while pending and pending[0].spec == spec:
                    u = pending[0]
                    fx = fixtures[u.seed]
                    t0 = clock.mono()
                    inner = backbone_factory(u.spec, num_ctx[u.spec])
                    if inner.name != backbone_name(u.spec):
                        raise ValueError(f"backbone for {u.spec} is named {inner.name!r}, expected {backbone_name(u.spec)!r}")
                    header = {"spec": u.spec, "model": inner.name, "num_ctx": getattr(inner, "num_ctx", None),
                              "fixture_sha256": fx.sha256}  # type: ignore[attr-defined]
                    emb = replay if u.substrate in EMBEDDING_SUBSTRATES else None
                    if emb is not None:   # only a real-embedder unit (RAG or episodic): existing journals keep their header
                        header["embedding_model"] = emb.name
                    bb = JournalBackbone(inner, cfg.out / ".journal" / u.rel().with_suffix(".jsonl"), est, clock, header)
                    bb.begin_replay(sess)
                    t = timing.setdefault(u, {"sessions": 0})
                    t["sessions"] += 1
                    try:
                        rec = run_one(lambda s=u.substrate, fx=fx, emb=emb: make_substrate(s, fx, emb), bb, fx, gate=True,  # type: ignore[arg-type]
                                      embedder_is_lexical_stub=cfg.embedder_is_lexical_stub,
                                      extractor_is_pattern_stub=cfg.extractor_is_pattern_stub)
                    except BatchBoundary:
                        t["wall_clock_s"] = round(t.get("wall_clock_s", 0.0) + clock.mono() - t0, 3)
                        log(f"  {u.label()}: batch boundary after {sess.model_calls} model calls this session")
                        break
                    finally:
                        if bb.touched:
                            used[bb.name] = bb.inner
                    t["wall_clock_s"] = round(t.get("wall_clock_s", 0.0) + clock.mono() - t0, 3)
                    rec["realrun"] = {
                        "driver": DRIVER, "num_ctx": getattr(bb.inner, "num_ctx", None),
                        "keep_alive": getattr(bb.inner, "keep_alive", None),
                        "truncation_guard": getattr(bb.inner, "truncation_guard", None),
                    }
                    problems = validate_record(rec)
                    if problems:
                        raise RuntimeError(f"realrun produced an invalid record: {problems}")
                    lats = [e["s"] for e in bb.entries if e["m"]]
                    t.update({"model_calls": len(lats), "latency_s": lats, "latency_max_s": max(lats, default=0.0),
                              "latency_mean_s": round(sum(lats) / len(lats), 4) if lats else 0.0})
                    _atomic_write(cfg.out / u.rel(), rec)
                    _atomic_write(cfg.out / ".timing" / u.rel(), t)
                    log(f"  {u.label()}: done, transport retries {getattr(bb.inner, 'retries', 0)}")
                    pending.pop(0)
            except BaseException as e:
                try:
                    _release_models(used, log)
                except UnloadFailed as ue:   # never mask the original error; keep the unload failure on it
                    log(f"  UNLOAD FAILED while handling {type(e).__name__}: {ue}")
                    e.add_note(f"also: {type(ue).__name__}: {ue}")
                raise
            _release_models(used, log)
    # rank stability per seed, from the records on disk (resumed units included)
    for sd in cfg.seeds:
        recs = [json.loads((cfg.out / u.rel()).read_text(encoding="utf-8")) for u in units if u.seed == sd]
        _atomic_write(cfg.out / f"seed-{sd}" / "rank_stability.json", rank_stability(recs))
    out = {"units": len(units), "skipped": skipped, "ran": len(units) - skipped, "sessions": sessions, "num_ctx": num_ctx}
    if cfg.embedder is not None:
        out["embed"] = embed_summary or {"texts": 0, "note": "no pending embedding-substrate unit (RAG or episodic): nothing to embed"}
    return out


def num_ctx_by_spec(backbones: Sequence[str], fixtures: dict[int, object]) -> dict[str, int]:
    """num_ctx per model: the max over all seeds, so a seed change never reloads the model mid-session."""
    n = max(required_num_ctx(fx) for fx in fixtures.values())
    return {b: n for b in backbones}


def _embed_numbers(cfg: RunConfig, fixtures: dict[int, object], units: Sequence[Unit]) -> tuple[int, int, int, int, float]:
    """(texts, cached, batches, sessions, held seconds) for the embed precompute of ``units``. Read-only:
    an absent cache counts as empty (nothing is created)."""
    assert cfg.embedder is not None
    texts = embed_texts_for(cfg, fixtures, units)
    p = embed_cache_path(cfg)
    todo = missing(EmbeddingCache(p, embedder_name(cfg.embedder), embed_dim(cfg.embedder)), texts) if p.exists() else sorted(texts)
    bs = batches(todo, cfg.embed_batch)
    n, held = simulate_sessions([(cfg.embedder, [EMBED_ITEM_S * len(b) for _, b in bs])], EMBED_COLD_S, cfg.max_lock_s)
    return len(texts), len(texts) - len(todo), len(bs), n, held


def dry_run_plan(cfg: RunConfig) -> str:
    """The unit plan, calls, sessions and wall clock. Calls no model, no network. Per-call seconds come from the
    measured table (short vs long, by the driver's ``is_long`` rule on each substrate's real prompts, counted
    with an always-abstaining reader); judge calls are all counted (an upper bound)."""
    fixtures = {sd: generate_by_version(sd, cfg.fixture_size, cfg.fixture_version) for sd in cfg.seeds}
    _preconditions(cfg, fixtures)
    units = plan_units(cfg.seeds, cfg.backbones, cfg.substrates)
    est = Estimator(default_s=cfg.est_call_s)
    seqs: dict[tuple[int, str], list[tuple[str, bool]]] = {}
    lines = [f"fixture size={cfg.fixture_size} version={cfg.fixture_version} seeds={','.join(map(str, cfg.seeds))}",
             f"max_lock_s={cfg.max_lock_s:g} gap_s={cfg.gap_s:g} keep_alive={KEEP_ALIVE} cold_load_s={COLD_S:g} "
             f"window=04:55-05:50Z one model per session"]
    for spec, n in num_ctx_by_spec(cfg.backbones, fixtures).items():
        lines.append(f"model {backbone_name(spec)}: num_ctx={n}")
    unit_costs: list[tuple[str, list[float]]] = []
    n_calls = n_run = 0
    for u in units:
        fx = fixtures[u.seed]
        name = backbone_name(u.spec)
        if unit_complete(cfg.out / u.rel(), fx, u, cfg.embedder and embedder_name(cfg.embedder), cfg.extractor_is_pattern_stub):
            lines.append(f"  {u.label():<44} SKIP (complete)")
            continue
        seq = seqs.setdefault((u.seed, u.substrate), estimate_calls(fx, u.substrate, cfg.embedder_is_lexical_stub,
                                                                    cfg.extractor_is_pattern_stub))
        costs = [est.get(name, long) for _, long in seq]
        g = sum(1 for k, _ in seq if k == "g")
        n_long = sum(1 for _, long in seq if long)
        unit_costs.append((u.spec, costs))
        n_calls += len(seq)
        n_run += 1
        lines.append(f"  {u.label():<44} <= {len(seq)} calls ({g} generate [{n_long} long] + {len(seq) - g} judge), "
                     f"~{sum(costs):.0f}s")
    cold = cold_load_s(units[0].spec) if units else 0.0
    n_sessions, held = simulate_sessions(unit_costs, cold, cfg.max_lock_s)
    if cfg.embedder is not None:
        name = embedder_name(cfg.embedder)
        texts, cached, nb, e_sessions, e_held = _embed_numbers(
            cfg, fixtures, [u for u in units if not unit_complete(cfg.out / u.rel(), fixtures[u.seed], u, name,
                                                                  cfg.extractor_is_pattern_stub)])
        lines.insert(2, f"embed precompute {name}: {texts} texts ({cached} cached, {texts - cached} to embed) in {nb} "
                        f"batches of <= {cfg.embed_batch}; estimated embed sessions: {e_sessions} (~{e_held:.0f}s held; "
                        f"cold load {EMBED_COLD_S:g}s, {EMBED_ITEM_S:g}s per text), all before any backbone session")
        n_sessions, held = n_sessions + e_sessions, held + e_held
    gaps = max(0, n_sessions - 1) * cfg.gap_s
    lines.append(f"units: {len(units)} ({n_run} to run); estimated model calls <= {n_calls}; "
                 f"estimated lock sessions: {n_sessions}; lock held ~{held:.0f}s + gaps {gaps:g}s = "
                 f"estimated wall clock ~{held + gaps:.0f}s (~{(held + gaps) / 3600:.1f}h)")
    return "\n".join(lines)
